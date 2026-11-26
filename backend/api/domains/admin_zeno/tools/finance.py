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
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import revenue as revenue_service
from api.core import intelligence
from api.core.permissions import Permission
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import simple

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


async def platform_costs(db: AsyncSession, args: FinanceArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    report = await _report(db, args.window_days)
    costs = report.get("costs") or {}
    evidence = [
        simple(
            prefix="finance", key="costs_total_minor", label="Recorded platform costs",
            value=costs.get("total_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report",
            definition="Recorded spend in the period: SMS, AI, API, payment fees, infrastructure and other.",
            start=start, end=end, confidence="measured", href="/revenue",
        ),
        simple(
            prefix="finance", key="costs_estimated_minor", label="Costs from assumed rates",
            value=costs.get("estimated_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report",
            definition="Part of recorded costs priced from an assumed rate rather than a provider statement.",
            start=start, end=end, confidence="estimated", href="/revenue",
        ),
        simple(
            prefix="finance", key="profit_minor", label="Income less recorded costs",
            value=report.get("profit_minor"), unit="kes_minor",
            source="api.core.revenue.revenue_report",
            definition="Income minus recorded costs. A management figure, not an audited profit.",
            start=start, end=end, confidence="derived", href="/revenue",
        ),
        simple(
            prefix="finance", key="margin_pct", label="Margin",
            value=report.get("margin_pct"), unit="percentage",
            source="api.core.revenue.revenue_report",
            definition="Income less recorded costs, as a share of income.",
            start=start, end=end, confidence="derived", href="/revenue",
        ),
    ]
    by_kind = {k.get("key"): k for k in costs.get("kinds", [])}
    for key, label in (
        ("ai", "AI provider costs"),
        ("sms", "SMS costs"),
        ("api", "API costs"),
        ("infra", "Infrastructure costs"),
        ("other", "Other service costs"),
    ):
        kind = by_kind.get(key)
        if kind is None:
            evidence.append(simple(
                prefix="finance.cost", key=key, label=label, value=0, unit="kes_minor",
                source="api.core.revenue.revenue_report",
                definition="No cost rows of this kind were recorded in the period.",
                start=start, end=end, confidence="measured", href="/revenue",
            ))
        else:
            evidence.append(simple(
                prefix="finance.cost", key=key, label=label,
                value=kind.get("amount_minor"), unit="kes_minor",
                source="api.core.revenue.revenue_report",
                definition=f"{kind.get('count')} recorded rows; estimated={bool(kind.get('estimated'))}.",
                start=start, end=end,
                confidence="estimated" if kind.get("estimated") else "measured",
                href="/revenue",
            ))
    return ToolOutput(
        evidence=evidence,
        data={"costs": costs, "notes": report.get("notes", [])},
        limitations=[
            BUSINESS_MODEL_NOTE,
            "AI costs are priced from real token counts at configured rates; peak multipliers, "
            "cache-hit ratios and FX moves are not tracked per call. Providers without a verified "
            "rate record an unpriced row, not zero.",
        ],
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
