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
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import simple
from api.models.platform_cost import CostKind, PlatformCost

ADMIN_CALL_SITE_PREFIX = "admin_zeno"


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

    rows = (await db.execute(
        select(PlatformCost)
        .where(PlatformCost.kind == CostKind.AI,
               PlatformCost.created_at >= start, PlatformCost.created_at < end)
        .limit(20000)
    )).scalars().all()
    buckets = _ai_rows(rows)

    evidence = [
        simple(prefix="zeno", key="conversations", label="Customer Zeno conversations",
               value=conversations, source="api.database.ZenoConversation",
               definition="Customer-facing Zeno conversations created in the period.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="user_turns", label="Customer Zeno user messages",
               value=user_turns, source="api.database.ZenoTurn",
               definition="Messages a customer sent to Zeno in the period.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="ai_calls_customer", label="Customer-facing AI calls",
               value=buckets["customer"]["calls"], source="platform_costs",
               definition="Successful model calls attributed to customer-facing call sites.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="ai_cost_customer_minor",
               label="Customer-facing AI spend",
               value=buckets["customer"]["amount_minor"], unit="kes_minor",
               source="platform_costs",
               definition="Cost-ledger AI rows for customer-facing call sites. Estimated where the rate is assumed.",
               start=start, end=end, confidence="estimated", href="/revenue"),
        simple(prefix="zeno", key="ai_calls_admin", label="Administrative Jarvis AI calls",
               value=buckets["admin"]["calls"], source="platform_costs",
               definition="Successful model calls attributed to admin_zeno call sites.",
               start=start, end=end, href="/zeno"),
        simple(prefix="zeno", key="ai_cost_admin_minor",
               label="Administrative Jarvis AI spend",
               value=buckets["admin"]["amount_minor"], unit="kes_minor",
               source="platform_costs",
               definition="Cost-ledger AI rows for administrative Jarvis call sites.",
               start=start, end=end, confidence="estimated", href="/revenue"),
        simple(prefix="zeno", key="ai_unpriced_calls",
               label="AI calls without a verified price",
               value=buckets["customer"]["unpriced"] + buckets["admin"]["unpriced"],
               source="platform_costs",
               definition="Calls whose provider rate or token counts are unknown; their cost is not zero, it is unpriced.",
               start=start, end=end, href="/revenue"),
    ]
    return ToolOutput(
        evidence=evidence,
        data={
            "customer_ai": buckets["customer"],
            "admin_ai": buckets["admin"],
            "note": (
                "Administrative Jarvis usage is reported separately and is never included in "
                "customer-facing Zeno figures."
            ),
        },
        limitations=[
            "Response quality is not measured: BROKA has no evaluation mechanism for Zeno answers.",
            "Provider usage is read from the cost ledger after a successful call; failed attempts "
            "and fall-throughs cost nothing and are not counted as calls.",
        ],
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
