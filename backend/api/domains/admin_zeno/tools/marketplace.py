"""Marketplace intelligence tools.

Reuses ``api.core.market_intel`` and ``api.core.market_models``. Forecasts
are only reported when the existing backtest published them; withheld
forecasts are reported as withheld, never replaced with a guess.

Seller and listing identities stop here. The underlying services return
seller ids, names and listing ids; this module keeps only ranks and the
aggregate performance figures, so no customer identity can reach a prompt.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import market_intel, market_models
from api.core.permissions import Permission
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import bounds, simple

SOURCE = "api.core.market_intel"


class DemandArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)


async def marketplace_demand(db: AsyncSession, args: DemandArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    report = await market_intel.demand(db, window_days=args.window_days, now=end)
    rows = report.get("categories", [])
    evidence = []
    for c in rows[:8]:
        evidence.append(simple(
            prefix="market.demand", key=f"{c.get('key')}.demand",
            label=f"Demand: {c.get('label')}", value=c.get("demand"),
            previous=c.get("demand_previous"), unit="count",
            source="api.core.market_intel.demand",
            definition="Buyer threads, interests and Buying Agent requests for the category.",
            start=start, end=end, confidence="derived", href="/market",
        ))
        evidence.append(simple(
            prefix="market.demand", key=f"{c.get('key')}.supply",
            label=f"Live supply: {c.get('label')}", value=c.get("supply_live"),
            unit="count", source="api.core.market_intel.demand",
            definition="Listings buyers can see in the category now.",
            start=start, end=end, href="/market",
        ))
    gaps = [c for c in rows if (c.get("demand") or 0) > 0 and (c.get("supply_live") or 0) == 0]
    return ToolOutput(
        evidence=evidence,
        data={
            "categories": [
                {"key": c.get("key"), "label": c.get("label"), "demand": c.get("demand"),
                 "demand_change_pct": c.get("demand_change_pct"), "supply_live": c.get("supply_live"),
                 "signal": c.get("signal"), "recommendation": c.get("recommendation")}
                for c in rows[:12]
            ],
            "unmet_categories": [c.get("label") for c in gaps][:8],
            "definitions": report.get("definitions", {}),
        },
        limitations=["Live supply is the state now; BROKA keeps no history of which listings were live previously."],
    )


class RankingArgs(ToolInput):
    window_days: int = Field(default=7, ge=1, le=90)
    limit: int = Field(default=10, ge=1, le=25)


async def listing_rankings(db: AsyncSession, args: RankingArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    report = await market_intel.rankings(db, window_days=args.window_days, limit=args.limit, now=end)
    listings = report.get("listings") or report.get("rankings") or report.get("top") or []
    evidence = []
    for rank, row in enumerate(listings[: args.limit], start=1):
        # Ranked, not named: a listing id or title would identify its seller.
        evidence.append(simple(
            prefix="market.listing", key=f"rank{rank}.score",
            label=f"Attention score of listing ranked {rank}",
            value=row.get("score"), unit="score",
            source="api.core.market_intel.rankings",
            definition="Weighted views, saves, likes, buyer contacts and negotiations.",
            start=start, end=end, confidence="derived", href="/market",
        ))
    return ToolOutput(
        evidence=evidence,
        data={
            "count": len(listings),
            "listings": [
                {"rank": rank, **{k: row.get(k) for k in (
                    "category", "county", "score", "views", "savers", "contacters",
                    "negotiators", "deal_buyers",
                ) if row.get(k) is not None}}
                for rank, row in enumerate(listings[: args.limit], start=1)
            ],
            "definitions": report.get("definitions", {}),
        },
        limitations=["Rankings describe observed engagement, not listing quality or seller conduct."],
    )


class PerformanceArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)
    limit: int = Field(default=10, ge=1, le=25)


async def marketplace_performance(db: AsyncSession, args: PerformanceArgs) -> ToolOutput:
    start, end = bounds(args.window_days)
    report = await market_intel.performance(
        db, window_days=args.window_days, limit=args.limit, now=end,
    )
    evidence = []
    for step in report.get("buyer_funnel", []):
        evidence.append(simple(
            prefix="market.funnel", key=str(step.get("key")), label=str(step.get("label")),
            value=step.get("buyers"), unit="count",
            source="api.core.market_intel.performance",
            definition="Distinct buyers reaching this stage inside the window.",
            start=start, end=end, confidence="derived", href="/market",
        ))
    for rank, seller in enumerate((report.get("sellers") or {}).get("top", [])[:5], start=1):
        # Sellers are ranked, never named or keyed by id: the model sees a
        # performance distribution, not a person to accuse.
        evidence.append(simple(
            prefix="market.seller", key=f"rank{rank}.contact_rate_pct",
            label=f"Buyer contact rate of seller ranked {rank}",
            value=seller.get("contact_rate_pct"), unit="percentage",
            source="api.core.market_intel.performance",
            definition="Distinct contacters divided by distinct viewers across the seller's listings.",
            start=start, end=end, confidence="derived", href="/market",
        ))
        if seller.get("avg_first_reply_minutes") is not None:
            evidence.append(simple(
                prefix="market.seller", key=f"rank{rank}.first_reply_minutes",
                label=f"Average first reply time of seller ranked {rank}",
                value=seller.get("avg_first_reply_minutes"), unit="minutes",
                source="api.core.market_intel.performance",
                definition="Minutes from a buyer's first message to the seller's first reply.",
                start=start, end=end, confidence="derived", href="/market",
            ))
    return ToolOutput(
        evidence=evidence,
        data={
            "sellers_total": (report.get("sellers") or {}).get("total"),
            "buyers_total": (report.get("buyers") or {}).get("total"),
            "browsers_without_contact": (report.get("buyers") or {}).get("browsers_without_contact"),
            "buyer_funnel": report.get("buyer_funnel", []),
            "top_sellers": [
                {"rank": rank, **{k: s.get(k) for k in (
                    "contact_rate_pct", "contacters", "viewers",
                    "deals_agreed", "deals_completed", "avg_first_reply_minutes",
                ) if s.get(k) is not None}}
                for rank, s in enumerate(
                    (report.get("sellers") or {}).get("top", [])[: args.limit], start=1
                )
            ],
        },
        limitations=[
            "Buyer funnel stages need not nest: a buyer can message without a recorded first view in the same window."
        ],
    )


class MarketAnomalyArgs(ToolInput):
    weeks: int = Field(default=6, ge=2, le=12)


async def marketplace_anomalies(db: AsyncSession, args: MarketAnomalyArgs) -> ToolOutput:
    report = await market_models.detect(db, weeks=args.weeks)
    evidence = []
    for a in report.get("anomalies", [])[:10]:
        evidence.append(simple(
            prefix="market.anomaly", key=str(a.get("key"))[:60],
            label=f"Marketplace anomaly: {a.get('label') or a.get('key')}",
            value=(a.get("deviation") or {}).get("z_score"), unit="z_score",
            source="api.core.market_models.detect",
            definition=str(a.get("definition") or "Robust z-score against the same weekday in prior weeks."),
            confidence="measured", href=a.get("action") or "/market",
        ))
    return ToolOutput(
        evidence=evidence,
        data={
            "evaluated_day": report.get("evaluated_day"),
            "anomalies": [
                {"key": a.get("key"), "label": a.get("label"), "severity": a.get("severity"),
                 "impact": a.get("impact"), "suggested_action": a.get("suggested_action")}
                for a in report.get("anomalies", [])[:10]
            ],
            "checked": report.get("checked", []),
            "not_instrumented": report.get("not_instrumented", []),
        },
        limitations=["Alerts never change an account, listing or payment."],
    )


class ForecastArgs(ToolInput):
    pass


async def validated_forecasts(db: AsyncSession, args: ForecastArgs) -> ToolOutput:
    rows = await market_models.forecasts(db)
    evidence = []
    for row in rows:
        key = str(row.get("metric"))
        if row.get("validated"):
            total = sum(float(day.get("expected") or 0) for day in row.get("forecast", []))
            evidence.append(simple(
                prefix="market.forecast", key=key, label=f"7-day forecast: {row.get('label')}",
                value=round(total, 1), unit="count",
                source="api.core.market_models.forecasts",
                definition="Same-weekday median of the last four weeks, published only after a 7-day backtest.",
                confidence="estimated", href="/market",
            ))
        else:
            from api.domains.admin_zeno.tools._common import missing
            evidence.append(missing(
                prefix="market.forecast", key=key,
                label=f"7-day forecast: {row.get('label')}",
                source="api.core.market_models.forecasts",
                reason=str(row.get("withheld_reason") or "Forecast withheld: the backtest did not pass."),
                definition="A withheld forecast is not a zero and is not replaced with an estimate.",
            ))
    return ToolOutput(
        evidence=evidence,
        data={"forecasts": rows},
        limitations=["A forecast is a range, not a guarantee; unvalidated series are withheld."],
    )


def register(registry) -> None:
    common = dict(
        domain="marketplace",
        permission=Permission.ADMIN_SUMMARY,
        evidence_source=SOURCE,
        data_availability="Available from marketplace intelligence and validated models.",
        timeout_seconds=15.0,
    )
    registry.register_all([
        ToolDefinition(
            name="marketplace_demand",
            description="Demand, live supply and gaps by category, against the previous window.",
            input_model=DemandArgs, handler=marketplace_demand, **common,
        ),
        ToolDefinition(
            name="listing_rankings",
            description="Listings ranked by buyer attention and conversion signals.",
            input_model=RankingArgs, handler=listing_rankings, **common,
        ),
        ToolDefinition(
            name="marketplace_performance",
            description="Seller and buyer performance, including the buyer funnel and reply times.",
            input_model=PerformanceArgs, handler=marketplace_performance, **common,
        ),
        ToolDefinition(
            name="marketplace_anomalies",
            description="Marketplace metrics outside their usual range for the last complete day.",
            input_model=MarketAnomalyArgs, handler=marketplace_anomalies, **common,
        ),
        ToolDefinition(
            name="marketplace_forecasts",
            description="Validated 7-day marketplace forecasts; unvalidated ones are reported as withheld.",
            input_model=ForecastArgs, handler=validated_forecasts, **common,
        ),
    ])
