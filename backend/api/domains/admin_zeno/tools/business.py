"""Business intelligence tools.

Every figure comes from the Command Center or the founder-intelligence pulse;
this module only selects and translates. Nothing is recomputed with its own
query, so Jarvis cannot disagree with the dashboard it links to.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import command_center, intelligence
from api.core.insights import build_insights
from api.core.permissions import Permission
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import bounds, metric_evidence, simple

DOMAIN = "business"
SOURCE = "api.core.command_center"


class BusinessArgs(ToolInput):
    window_days: int = Field(default=7, ge=1, le=90, description="Look-back window in days.")


async def business_overview(db: AsyncSession, args: BusinessArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    report = await command_center.command_center(db, window_days=args.window_days, now=end)
    evidence = []
    for group in report.get("groups", []):
        for metric in group.get("metrics", []):
            evidence.append(metric_evidence(
                prefix=f"business.{group.get('key')}",
                metric=metric, source=SOURCE, start=start, end=end,
            ))
    decisions = (report.get("decisions") or {}).get("insights") or []
    limitations = [
        f"{e.label}: {e.unavailable_reason}" for e in evidence if not e.available
    ][:6]
    return ToolOutput(
        evidence=evidence[:32],
        data={
            "window_days": args.window_days,
            "decisions": [
                {"id": d.get("id"), "severity": d.get("severity"), "area": d.get("area"),
                 "title": d.get("title"), "href": d.get("href")}
                for d in decisions[:8]
            ],
            "activity_coverage_started_on": report.get("activity_coverage_started_on"),
        },
        limitations=limitations,
    )


class FounderInsightsArgs(ToolInput):
    window_days: int = Field(default=7, ge=1, le=90)


async def founder_insights(db: AsyncSession, args: FounderInsightsArgs) -> ToolOutput:
    now = datetime.utcnow()
    start = now - timedelta(days=args.window_days)
    pulse = await intelligence.pulse(db, window_days=args.window_days, now=now)
    categories = await intelligence.categories(db, window_days=max(args.window_days, 30), now=now)
    sellers = await intelligence.sellers_overview(db, now=now, limit=10)
    insights = build_insights(pulse=pulse, categories=categories, sellers=sellers)
    insights.sort(key=lambda d: ({"critical": 0, "warning": 1, "positive": 2}.get(d["severity"], 3),
                                 -float(d.get("weight") or 0)))
    evidence = [
        simple(
            prefix="business.insight", key=str(i["id"]).replace(":", "_")[:60],
            label=str(i["title"])[:160], value=i.get("evidence"),
            source="api.core.insights.build_insights",
            definition="Founder insight raised by the supplied evidence.",
            unit="json", start=start, end=now, confidence="derived",
            href=(i["action"][1] if isinstance(i.get("action"), (list, tuple)) and len(i["action"]) > 1 else None),
        )
        for i in insights[:10] if i.get("id")
    ]
    return ToolOutput(
        evidence=evidence,
        data={
            "insights": [
                {"severity": i.get("severity"), "area": i.get("area"), "title": i.get("title"),
                 "detail": i.get("detail"), "action": i.get("action"), "evidence": i.get("evidence")}
                for i in insights[:10]
            ]
        },
        limitations=[],
    )


class LiquidityArgs(ToolInput):
    window_days: int = Field(default=7, ge=1, le=90)


async def marketplace_liquidity(db: AsyncSession, args: LiquidityArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    pulse = await intelligence.pulse(db, window_days=args.window_days, now=end)
    cur, prev = pulse["liquidity"]["current"], pulse["liquidity"]["previous"]
    target = pulse["liquidity"].get("target_pct")
    evidence = [
        simple(
            prefix="business.liquidity", key="rate_pct",
            label="New listings that got a buyer within 7 days",
            value=cur.get("rate_pct"), previous=prev.get("rate_pct"),
            unit="percentage", source="api.core.intelligence.pulse",
            definition=intelligence.DEFINITIONS.get("liquidity", "Listings that received buyer contact in their first 7 days."),
            start=start, end=end, href="/intelligence",
        ),
        simple(
            prefix="business.liquidity", key="eligible_listings",
            label="Eligible new listings", value=cur.get("eligible"),
            previous=prev.get("eligible"), source="api.core.intelligence.pulse",
            definition="New listings old enough to be judged in the window.",
            start=start, end=end, href="/intelligence",
        ),
    ]
    if target is not None:
        evidence.append(simple(
            prefix="business.liquidity", key="target_pct", label="Liquidity target",
            value=target, unit="percentage", source="api.core.intelligence.pulse",
            definition="The bar BROKA sets before charging in a category.",
            start=start, end=end, href="/intelligence",
        ))
    return ToolOutput(
        evidence=evidence,
        data={"current": cur, "previous": prev, "target_pct": target},
    )


class SellerResponseArgs(ToolInput):
    window_days: int = Field(default=7, ge=1, le=90)


async def seller_responsiveness(db: AsyncSession, args: SellerResponseArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    pulse = await intelligence.pulse(db, window_days=args.window_days, now=end)
    block = pulse["seller_response"]
    cur, prev = block["current"], block["previous"]
    evidence = [
        simple(
            prefix="business.seller_response", key="rate_24h",
            label="Seller replies within 24 hours", value=cur.get("rate_pct"),
            previous=prev.get("rate_pct"), unit="percentage",
            source="api.core.intelligence.pulse",
            definition=intelligence.DEFINITIONS.get("seller_reply", ""),
            start=start, end=end, href="/intelligence",
        ),
        simple(
            prefix="business.seller_response", key="eligible",
            label="Eligible conversations", value=cur.get("eligible"),
            previous=prev.get("eligible"), source="api.core.intelligence.pulse",
            definition="Buyer conversations old enough to judge in the window.",
            start=start, end=end, href="/intelligence",
        ),
        simple(
            prefix="business.seller_response", key="replied_24h",
            label="Conversations replied to within 24 hours",
            value=cur.get("replied_24h"), previous=prev.get("replied_24h"),
            source="api.core.intelligence.pulse",
            definition="Buyer conversations whose seller answered within 24 hours.",
            start=start, end=end, href="/intelligence",
        ),
    ]
    limitations = []
    if block.get("truncated"):
        limitations.append("Seller reply times were truncated to the newest conversations in the window.")
    return ToolOutput(
        evidence=evidence,
        data={"current": cur, "previous": prev, "target_pct": block.get("target_pct")},
        limitations=limitations,
    )


class TargetsArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)


async def business_targets(db: AsyncSession, args: TargetsArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    pulse = await intelligence.pulse(db, window_days=args.window_days, now=end)
    evidence = [
        simple(
            prefix="business.target", key=str(t["key"]), label=str(t["label"]),
            value=t.get("actual"), unit=str(t.get("unit") or "count"),
            source="api.core.intelligence.pulse",
            definition=f"30-day target: {t.get('target')}. Progress {t.get('progress_pct')}%.",
            start=start, end=end, confidence="derived", href="/growth",
        )
        for t in pulse.get("targets", [])
    ]
    be = pulse.get("break_even") or {}
    if be:
        evidence.append(simple(
            prefix="business.break_even", key="net_30d_minor",
            label="Net contribution, trailing 30 days", value=be.get("net_30d_minor"),
            unit="kes_minor", source="api.core.intelligence.pulse",
            definition="Income minus recorded platform costs over 30 days. Not a bank balance.",
            start=start, end=end, confidence="derived", href="/revenue",
        ))
        evidence.append(simple(
            prefix="business.break_even", key="coverage_pct",
            label="Fixed monthly cost covered", value=be.get("coverage_pct"),
            unit="percentage", source="api.core.intelligence.pulse",
            definition="Net 30-day contribution as a share of the configured fixed monthly cost.",
            start=start, end=end, confidence="derived", href="/revenue",
        ))
    return ToolOutput(
        evidence=evidence,
        data={"targets": pulse.get("targets", []), "break_even": be},
        limitations=["Break-even is measured against a configured fixed cost, not an audited statement."],
    )


def register(registry) -> None:
    common = dict(
        domain=DOMAIN,
        permission=Permission.ADMIN_SUMMARY,
        evidence_source=SOURCE,
        data_availability="Available from the Command Center and founder-intelligence services.",
        timeout_seconds=12.0,
    )
    registry.register_all([
        ToolDefinition(
            name="business_overview",
            description=(
                "Overall business performance for a window: growth, marketplace health, "
                "financial performance, trust and technical operations as the Command "
                "Center presents them."
            ),
            input_model=BusinessArgs, handler=business_overview, **common,
        ),
        ToolDefinition(
            name="founder_insights",
            description="Current founder insights and decisions, ranked by severity.",
            input_model=FounderInsightsArgs, handler=founder_insights, **common,
        ),
        ToolDefinition(
            name="marketplace_liquidity",
            description="Share of new listings that hear from a buyer within 7 days, against target.",
            input_model=LiquidityArgs, handler=marketplace_liquidity, **common,
        ),
        ToolDefinition(
            name="seller_responsiveness",
            description="Seller reply rate within 24 hours and the conversations behind it.",
            input_model=SellerResponseArgs, handler=seller_responsiveness, **common,
        ),
        ToolDefinition(
            name="business_targets",
            description="Progress against BROKA's 30-day targets and break-even coverage.",
            input_model=TargetsArgs, handler=business_targets, **common,
        ),
    ])
