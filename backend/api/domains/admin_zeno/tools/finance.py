"""Financial intelligence tools.

BROKA's revenue is what users pay BROKA: fees, plans, boosts, badges and its
own commission. Money users pay each other, and principal an external escrow
provider holds, are never BROKA revenue. That distinction is stated in the
tool output as a limitation so a synthesized answer cannot quietly blur it.

All figures come from ``api/core/revenue.py`` and the cost ledger. Estimated
rows are flagged estimated; an unpriced AI call is never priced at zero by
this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import revenue as revenue_service
from api.core import intelligence
from api.core.permissions import Permission
from api.domains.admin_zeno.evidence import make_evidence, unavailable_evidence
from api.domains.admin_zeno.schemas import Evidence, ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import simple
from api.models.platform_cost import CostKind, PlatformCost

BUSINESS_MODEL_NOTE = (
    "BROKA is not a payment processor, wallet or escrow custodian. Deal value and escrow "
    "principal are not BROKA revenue and must never be described as money BROKA holds."
)


class FinanceArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)


async def _report(db: AsyncSession, days: int) -> dict:
    end = datetime.utcnow()
    start = end - timedelta(days=days)
    return await revenue_service.revenue_report(
        db,
        from_raw=start.isoformat(),
        to_raw=end.isoformat(),
        bucket="day" if days <= 31 else "week",
    )


async def platform_revenue(db: AsyncSession, args: FinanceArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    report = await _report(db, args.window_days)
    income = report.get("income") or {}
    evidence = [
        simple(
            prefix="finance", key="income_total_minor", label="BROKA income",
            value=income.get("total_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report",
            definition="Money users paid BROKA in the period: fees, plans, boosts, badges and BROKA's own commission. Never deal money or escrow principal.",
            start=start, end=end, confidence="measured", href="/revenue",
        )
    ]
    for stream in income.get("streams", []):
        evidence.append(simple(
            prefix="finance.income", key=str(stream.get("key")), label=str(stream.get("label")),
            value=stream.get("amount_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report",
            definition=f"Income stream '{stream.get('key')}' over the period.",
            start=start, end=end, confidence="measured", href="/revenue",
        ))
    return ToolOutput(
        evidence=evidence,
        data={"income": income, "period": {"from": report.get("from"), "to": report.get("to")}},
        limitations=[BUSINESS_MODEL_NOTE],
    )


# A bounded read of zero-amount rows is enough to find unpriced calls: an
# unpriced provider call is recorded at amount 0 with meta.unpriced or
# meta.tokens_unknown (see api/core/cost_ledger.py). Never read the whole
# ledger to answer one question.
#
# Which rows are zero-amount is decided by the database (exact counts per
# kind), not by a bounded sample of the whole ledger, so a large period can
# no longer hide unpriced spend inside rows 5,001 onwards. Only the meta
# classification of those zero rows is bounded, and when even that does not
# fit, the affected figures are reported as unavailable rather than measured.
MAX_UNPRICED_SCAN = 5000

COST_CATEGORIES = (
    (CostKind.AI, "AI provider costs"),
    (CostKind.SMS, "SMS costs"),
    (CostKind.API, "API costs"),
    (CostKind.INFRA, "Infrastructure costs"),
    (CostKind.OTHER, "Other service costs"),
    (CostKind.PAYMENT_FEE, "Collection fees (derived)"),
)


async def _zero_amount_counts(db: AsyncSession, start: datetime, end: datetime) -> dict[str, int]:
    """Exact number of zero-amount rows per kind, aggregated by the database."""
    rows = (await db.execute(
        select(PlatformCost.kind, func.count(PlatformCost.id))
        .where(
            PlatformCost.created_at >= start,
            PlatformCost.created_at < end,
            PlatformCost.kind != CostKind.PAYMENT_FEE,
            PlatformCost.amount_minor == 0,
        )
        .group_by(PlatformCost.kind)
    )).all()
    return {str(kind): int(count or 0) for kind, count in rows}


async def _unpriced_by_kind(
    db: AsyncSession, start: datetime, end: datetime
) -> tuple[dict[str, int], dict[str, int], bool]:
    """(unpriced rows per kind, zero-amount rows per kind, complete).

    ``complete`` is False when the zero-amount rows exist but do not all fit
    the bounded read, so their meta could not be classified. A kind with no
    zero-amount rows is complete regardless: the database has already proved
    there is nothing to classify. When ``complete`` is False no figure that
    depends on unpriced spend may be presented as measured.
    """
    zero_counts = await _zero_amount_counts(db, start, end)
    total_zero = sum(zero_counts.values())
    if total_zero == 0:
        return {}, zero_counts, True
    complete = total_zero <= MAX_UNPRICED_SCAN
    if not complete:
        return {}, zero_counts, False
    rows = (await db.execute(
        select(PlatformCost.kind, PlatformCost.meta)
        .where(
            PlatformCost.created_at >= start,
            PlatformCost.created_at < end,
            PlatformCost.kind != CostKind.PAYMENT_FEE,
            PlatformCost.amount_minor == 0,
        )
        .limit(MAX_UNPRICED_SCAN)
    )).all()
    counts: dict[str, int] = {}
    for kind, meta in rows:
        meta = meta or {}
        if meta.get("unpriced") or meta.get("tokens_unknown"):
            counts[str(kind)] = counts.get(str(kind), 0) + 1
    return counts, zero_counts, True


def _cost_evidence(
    *,
    key: str,
    label: str,
    kind: dict | None,
    unpriced: int,
    zero_rows: int,
    classification_complete: bool,
    any_cost_rows: bool,
    start: datetime,
    end: datetime,
) -> Evidence:
    """Classify one cost category without ever turning absence into zero.

    The states are distinct: a measured zero (rows exist, all at zero, none
    unpriced), no rows for the category, incomplete tracking, a figure from
    an assumed rate, unpriced rows, and zero-amount rows that the bounded read
    never classified (which is incomplete tracking too - the total is a floor,
    not a measurement).
    """
    source = "api.core.revenue.revenue_report"
    if kind is None:
        if not classification_complete and zero_rows:
            reason = (
                f"{zero_rows} zero-amount rows could not be classified as priced or "
                "unpriced within the bounded read, so this category is not a verified "
                "zero and is not reported as one."
            )
        elif not any_cost_rows:
            reason = (
                "No cost rows were recorded at all in this period. Cost tracking may be "
                "incomplete, so this is not a verified zero."
            )
        else:
            reason = (
                "No cost rows of this kind were recorded in the period. An absent ledger "
                "entry is not evidence of zero spend."
            )
        return unavailable_evidence(
            metric_key=f"finance.cost.{key}", label=label, source=source, reason=reason,
            definition="Operating spend recorded in BROKA's cost ledger.", unit="kes_minor",
        )
    count = int(kind.get("count") or 0)
    amount = kind.get("amount_minor")
    if not classification_complete and zero_rows:
        # Zero-amount rows of this kind exist and their meta was never read:
        # some of them may be unpriced, so the recorded total is a floor, not
        # a measurement.
        return unavailable_evidence(
            metric_key=f"finance.cost.{key}", label=label, source=source,
            reason=(
                f"{zero_rows} zero-amount rows of this kind could not be classified "
                "within the bounded read, so this total may omit unpriced spend and is "
                "not reported as measured."
            ),
            definition="Operating spend recorded in BROKA's cost ledger.", unit="kes_minor",
        )
    if unpriced:
        return unavailable_evidence(
            metric_key=f"finance.cost.{key}", label=label, source=source,
            reason=(
                f"{unpriced} of {count} recorded rows are unpriced (provider rate or token "
                "counts unknown), so the total understates spend and is not reported as a "
                "measured figure."
            ),
            definition="Operating spend recorded in BROKA's cost ledger.", unit="kes_minor",
        )
    definition = f"{count} recorded rows in the period."
    if kind.get("estimated"):
        definition += " Priced from an assumed rate, not a provider statement."
    elif amount == 0:
        definition += " Every recorded row was zero: a measured zero, not an absent one."
    return make_evidence(
        metric_key=f"finance.cost.{key}", label=label, value=amount, unit="kes_minor",
        source=source, definition=definition, period_start=start, period_end=end,
        confidence="estimated" if kind.get("estimated") else "measured",
        href="/revenue",
    )


async def platform_costs(db: AsyncSession, args: FinanceArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    report = await _report(db, args.window_days)
    costs = report.get("costs") or {}
    by_kind = {k.get("key"): k for k in costs.get("kinds", [])}
    any_cost_rows = any(int(k.get("count") or 0) > 0 for k in by_kind.values())
    unpriced_by_kind, zero_by_kind, classification_complete = await _unpriced_by_kind(
        db, start, end
    )
    unpriced_total = sum(unpriced_by_kind.values())
    zero_rows_total = sum(zero_by_kind.values())
    estimated_total = int(costs.get("estimated_minor") or 0)

    evidence: list[Evidence] = []
    if not any_cost_rows:
        evidence.append(unavailable_evidence(
            metric_key="finance.costs_total_minor", label="Recorded platform costs",
            source="api.core.revenue.revenue_report", unit="kes_minor",
            reason=(
                "No cost rows were recorded in this period. Absence of ledger entries does "
                "not prove zero spend; cost tracking may be incomplete."
            ),
            definition="Operating spend recorded in BROKA's cost ledger.",
        ))
    elif not classification_complete:
        # Zero-amount rows exist but their meta was not read, so unpriced
        # spend cannot be separated from measured spend. The recorded total
        # is a floor and presenting it as the figure would overstate profit.
        evidence.append(unavailable_evidence(
            metric_key="finance.costs_total_minor", label="Recorded platform costs",
            source="api.core.revenue.revenue_report", unit="kes_minor",
            reason=(
                f"{zero_rows_total} zero-amount rows could not be classified as priced or "
                "unpriced within the bounded read, so unpriced spend cannot be separated "
                "from measured spend. The total is not reported as a measured figure."
            ),
            definition="Operating spend recorded in BROKA's cost ledger.",
        ))
    else:
        total_definition = "Recorded spend in the period: SMS, AI, API, fees, infrastructure and other."
        total_confidence = "measured"
        if unpriced_total:
            total_confidence = "estimated"
            total_definition += f" {unpriced_total} rows are unpriced, so this understates spend."
        elif estimated_total:
            total_confidence = "estimated"
            total_definition += " Some rows are priced from an assumed rate."
        evidence.append(make_evidence(
            metric_key="finance.costs_total_minor", label="Recorded platform costs",
            value=costs.get("total_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report", definition=total_definition,
            period_start=start, period_end=end, confidence=total_confidence,
            href="/revenue",
        ))
    evidence.append(make_evidence(
        metric_key="finance.costs_estimated_minor", label="Costs from assumed rates",
        value=estimated_total, unit="kes_minor",
        source="api.core.revenue.revenue_report",
        definition="Part of recorded costs priced from an assumed rate rather than a provider statement.",
        period_start=start, period_end=end, confidence="estimated", href="/revenue",
    ))
    profit_confidence = "estimated" if (unpriced_total or estimated_total or not any_cost_rows) else "derived"
    if not classification_complete:
        for key, label, unit in (
            ("finance.profit_minor", "Income less recorded costs", "kes_minor"),
            ("finance.margin_pct", "Margin", "percentage"),
        ):
            evidence.append(unavailable_evidence(
                metric_key=key, label=label,
                source="api.core.revenue.revenue_report", unit=unit,
                reason=(
                    "Cost completeness cannot be verified for this period, so a profit "
                    "figure would overstate the result and is not reported."
                ),
                definition="Income less recorded costs. A management figure, not an audited profit.",
            ))
    else:
        evidence.append(make_evidence(
            metric_key="finance.profit_minor", label="Income less recorded costs",
            value=report.get("profit_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report",
            definition=(
                "Income minus recorded costs. A management figure, not an audited profit"
                + ("; it excludes unpriced or unrecorded spend and therefore overstates profit."
                   if unpriced_total or not any_cost_rows else ".")
            ),
            period_start=start, period_end=end, confidence=profit_confidence, href="/revenue",
        ))
        evidence.append(make_evidence(
            metric_key="finance.margin_pct", label="Margin", value=report.get("margin_pct"),
            unit="percentage", source="api.core.revenue.revenue_report",
            definition="Income less recorded costs, as a share of income.",
            period_start=start, period_end=end, confidence=profit_confidence, href="/revenue",
        ))

    for kind_key, label in COST_CATEGORIES:
        evidence.append(_cost_evidence(
            key=kind_key, label=label, kind=by_kind.get(kind_key),
            unpriced=unpriced_by_kind.get(kind_key, 0),
            zero_rows=zero_by_kind.get(kind_key, 0),
            classification_complete=classification_complete,
            any_cost_rows=any_cost_rows, start=start, end=end,
        ))

    limitations = [
        BUSINESS_MODEL_NOTE,
        "AI costs are priced from real token counts at configured rates; peak multipliers, "
        "cache-hit ratios and FX moves are not tracked per call. Providers without a verified "
        "rate record an unpriced row, not zero.",
        "A category with no recorded rows is reported as unavailable, never as zero spend.",
    ]
    if not classification_complete:
        limitations.append(
            f"{zero_rows_total} zero-amount cost rows exceeded the bounded classification "
            "read. Totals, profit, margin and the affected categories are reported as "
            "unavailable rather than as measured figures."
        )
    return ToolOutput(
        evidence=evidence,
        data={
            "costs": costs,
            "notes": report.get("notes", []),
            "classification_complete": classification_complete,
            "zero_amount_rows": zero_rows_total,
            "categories": {
                kind_key: {
                    "available": next(
                        (e.available for e in evidence if e.metric_key == f"finance.cost.{kind_key}"),
                        False,
                    ),
                    "unpriced_rows": unpriced_by_kind.get(kind_key, 0),
                    "zero_amount_rows": zero_by_kind.get(kind_key, 0),
                    "complete": classification_complete
                                or zero_by_kind.get(kind_key, 0) == 0,
                }
                for kind_key, _label in COST_CATEGORIES
            },
        },
        limitations=limitations,
    )


class BreakEvenArgs(ToolInput):
    pass


async def break_even(db: AsyncSession, args: BreakEvenArgs) -> ToolOutput:
    now = datetime.utcnow()
    start = now - timedelta(days=30)
    pulse = await intelligence.pulse(db, window_days=30, now=now)
    be = pulse.get("break_even") or {}
    evidence = [
        simple(
            prefix="finance.break_even", key="fixed_monthly_cost_minor",
            label="Configured fixed monthly cost", value=be.get("fixed_monthly_cost_minor"),
            unit="kes_minor", source="api.core.intelligence.pulse",
            definition="The fixed monthly cost configured on Settings, used as the break-even bar.",
            start=start, end=now, confidence="estimated", href="/revenue",
        ),
        simple(
            prefix="finance.break_even", key="net_30d_minor",
            label="Net contribution, trailing 30 days", value=be.get("net_30d_minor"),
            unit="kes_minor", source="api.core.intelligence.pulse",
            definition="Income minus recorded costs over 30 days.",
            start=start, end=now, confidence="derived", href="/revenue",
        ),
        simple(
            prefix="finance.break_even", key="coverage_pct",
            label="Fixed cost covered", value=be.get("coverage_pct"), unit="percentage",
            source="api.core.intelligence.pulse",
            definition="Net contribution as a share of the configured fixed monthly cost.",
            start=start, end=now, confidence="derived", href="/revenue",
        ),
        simple(
            prefix="finance.break_even", key="gap_minor",
            label="Monthly gap to configured fixed cost", value=be.get("gap_minor"),
            unit="kes_minor", source="api.core.intelligence.pulse",
            definition="Configured fixed monthly cost minus net 30-day contribution, floored at zero.",
            start=start, end=now, confidence="derived", href="/revenue",
        ),
    ]
    return ToolOutput(
        evidence=evidence,
        data={"break_even": be},
        limitations=[
            BUSINESS_MODEL_NOTE,
            "Break-even uses a configured fixed cost, not an audited statement; costs priced from assumed rates may move it.",
        ],
    )


def register(registry) -> None:
    common = dict(
        domain="finance",
        permission=Permission.VIEW_TRANSACTIONS,
        evidence_source="api.core.revenue.revenue_report",
        data_availability="Available from the revenue report, cost ledger and founder pulse.",
        timeout_seconds=15.0,
    )
    registry.register_all([
        ToolDefinition(
            name="platform_revenue",
            description="BROKA's own income and its streams. Never deal value or escrow principal.",
            input_model=FinanceArgs, handler=platform_revenue, **common,
        ),
        ToolDefinition(
            name="platform_costs",
            description="Operating costs by kind (AI, SMS, API, infrastructure, other) and profit.",
            input_model=FinanceArgs, handler=platform_costs, **common,
        ),
        ToolDefinition(
            name="break_even",
            description="Progress against the configured fixed monthly cost.",
            input_model=BreakEvenArgs, handler=break_even, **common,
        ),
    ])
