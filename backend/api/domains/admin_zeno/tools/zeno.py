"""Zeno intelligence tools: AI usage and cost, not answer quality.

There is no response-quality evaluation mechanism, so these tools never
claim one. Administrative Jarvis usage is reported separately from
customer-facing Zeno usage: the two share a provider chain but must not be
blended into one "Zeno usage" number.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.permissions import Permission
from api.database import ZenoConversation, ZenoTurn
from api.domains.admin_zeno.evidence import unavailable_evidence
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import simple
from api.models.platform_cost import CostKind, PlatformCost

ADMIN_CALL_SITE_PREFIX = "admin_zeno"

# Cost rows are read in bounded pages. The total is always exact (a SQL
# count/sum); only the customer-vs-administrative split needs row-level
# metadata, and when the ledger is larger than this bound that split is
# reported as unavailable rather than as a partial total.
COST_PAGE_SIZE = 1000
MAX_COST_ROWS = 20000


class ZenoUsageArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)


async def _count(db: AsyncSession, column, *where) -> int:
    return int((await db.execute(select(func.count(column)).where(*where))).scalar() or 0)


def _ai_rows(rows) -> dict:
    """Split AI cost rows into customer-facing and administrative usage."""
    buckets = {
        "customer": {"amount_minor": 0, "calls": 0, "unpriced": 0, "providers": {}},
        "admin": {"amount_minor": 0, "calls": 0, "unpriced": 0, "providers": {}},
    }
    for row in rows:
        meta = row.meta or {}
        bucket = buckets[
            "admin" if str(meta.get("call_site") or "").startswith(ADMIN_CALL_SITE_PREFIX)
            else "customer"
        ]
        bucket["amount_minor"] += int(row.amount_minor or 0)
        bucket["calls"] += 1
        if meta.get("unpriced") or meta.get("tokens_unknown"):
            bucket["unpriced"] += 1
        provider = row.provider or "unknown"
        bucket["providers"][provider] = bucket["providers"].get(provider, 0) + 1
    return buckets


async def _ai_cost_facts(
    db: AsyncSession, start: datetime, end: datetime
) -> tuple[int, int, dict, bool]:
    """(exact row count, exact amount, bounded split, truncated).

    The count and sum are computed by the database, so they stay exact at any
    ledger size. Only the split by call site reads rows, in pages bounded by
    ``MAX_COST_ROWS``; ``truncated`` says the split is a partial sample.
    """
    where = (
        PlatformCost.kind == CostKind.AI,
        PlatformCost.created_at >= start,
        PlatformCost.created_at < end,
    )
    total_rows = int((await db.execute(
        select(func.count(PlatformCost.id)).where(*where)
    )).scalar() or 0)
    total_amount = int((await db.execute(
        select(func.coalesce(func.sum(PlatformCost.amount_minor), 0)).where(*where)
    )).scalar() or 0)

    rows: list[PlatformCost] = []
    offset = 0
    limit = min(total_rows, MAX_COST_ROWS)
    while offset < limit:
        page = (await db.execute(
            select(PlatformCost)
            .where(*where)
            .order_by(PlatformCost.id)
            .offset(offset)
            .limit(min(COST_PAGE_SIZE, limit - offset))
        )).scalars().all()
        rows.extend(page)
        if len(page) < COST_PAGE_SIZE:
            break
        offset += len(page)
    return total_rows, total_amount, _ai_rows(rows), total_rows > len(rows)


async def zeno_usage(db: AsyncSession, args: ZenoUsageArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)

    conversations = await _count(
        db, ZenoConversation.id,
        ZenoConversation.created_at >= start, ZenoConversation.created_at < end,
    )
    user_turns = int((await db.execute(
        select(func.count(ZenoTurn.id))
        .join(ZenoConversation, ZenoConversation.id == ZenoTurn.conversation_id)
        .where(ZenoTurn.role == "user",
               ZenoTurn.created_at >= start, ZenoTurn.created_at < end)
    )).scalar() or 0)

    total_calls, total_amount, buckets, truncated = await _ai_cost_facts(db, start, end)

    truncation_reason = (
        f"The AI cost ledger exceeded the bounded analysis window "
        f"({MAX_COST_ROWS} rows); the customer/administrative split is not reported "
        f"from a partial sample."
    )

    def split_evidence(*, key: str, label: str, bucket: str, field: str,
                       unit: str = "count", definition: str, confidence: str = "measured"):
        value = buckets[bucket][field]
        if truncated:
            return unavailable_evidence(
                metric_key=f"zeno.{key}", label=label, source="platform_costs",
                reason=truncation_reason, definition=definition, unit=unit,
            )
        return simple(
            prefix="zeno", key=key, label=label, value=value, unit=unit,
            source="platform_costs", definition=definition,
            start=start, end=end, confidence=confidence, href="/zeno",
        )

    evidence = [
        simple(prefix="zeno", key="conversations", label="Customer Zeno conversations",
               value=conversations, source="api.database.ZenoConversation",
               definition="Customer-facing Zeno conversations created in the period.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="user_turns", label="Customer Zeno user messages",
               value=user_turns, source="api.database.ZenoTurn",
               definition="Messages a customer sent to Zeno in the period.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="ai_calls_total", label="AI provider calls (all call sites)",
               value=total_calls, source="platform_costs",
               definition="Exact database count of AI cost-ledger rows in the period.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="ai_cost_total_minor", label="AI spend (all call sites)",
               value=total_amount, unit="kes_minor", source="platform_costs",
               definition="Exact database sum of AI cost-ledger amounts; estimated where a rate is assumed.",
               start=start, end=end, confidence="estimated", href="/revenue"),
        split_evidence(key="ai_calls_customer", label="Customer-facing AI calls",
                       bucket="customer", field="calls",
                       definition="Successful model calls attributed to customer-facing call sites."),
        split_evidence(key="ai_cost_customer_minor", label="Customer-facing AI spend",
                       bucket="customer", field="amount_minor", unit="kes_minor",
                       definition="Cost-ledger AI rows for customer-facing call sites. Estimated where the rate is assumed.",
                       confidence="estimated"),
        split_evidence(key="ai_calls_admin", label="Administrative Jarvis AI calls",
                       bucket="admin", field="calls",
                       definition="Successful model calls attributed to admin_zeno call sites."),
        split_evidence(key="ai_cost_admin_minor", label="Administrative Jarvis AI spend",
                       bucket="admin", field="amount_minor", unit="kes_minor",
                       definition="Cost-ledger AI rows for administrative Jarvis call sites.",
                       confidence="estimated"),
        split_evidence(key="ai_unpriced_calls", label="AI calls without a verified price",
                       bucket="customer", field="unpriced",
                       definition="Calls whose provider rate or token counts are unknown; their cost is not zero, it is unpriced."),
    ]
    limitations = [
        "Response quality is not measured: BROKA has no evaluation mechanism for Zeno answers.",
        "Provider usage is read from the cost ledger after a successful call; failed attempts "
        "and fall-throughs cost nothing and are not counted as calls.",
    ]
    if truncated:
        limitations.append(truncation_reason)
    return ToolOutput(
        evidence=evidence,
        data={
            "customer_ai": {**buckets["customer"], "complete": not truncated},
            "admin_ai": {**buckets["admin"], "complete": not truncated},
            "total_calls": total_calls,
            "total_amount_minor": total_amount,
            "truncated": truncated,
            "note": (
                "Administrative Jarvis usage is reported separately and is never included in "
                "customer-facing Zeno figures."
            ),
        },
        limitations=limitations,
    )


def register(registry) -> None:
    registry.register(ToolDefinition(
        name="zeno_usage",
        description=(
            "Customer Zeno and administrative Jarvis request volumes, AI provider calls and AI spend, "
            "reported separately. Does not measure answer quality."
        ),
        domain="zeno",
        permission=Permission.ADMIN_SUMMARY,
        input_model=ZenoUsageArgs,
        handler=zeno_usage,
        timeout_seconds=12.0,
        evidence_source="platform_costs / zeno_conversations",
        data_availability="Available from the cost ledger and Zeno conversation tables.",
    ))
