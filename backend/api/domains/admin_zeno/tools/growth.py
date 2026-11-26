"""Growth intelligence tools.

Reuses ``api.core.activity`` and ``api.core.intelligence``. Activity counts
only cover the period after tracking began; a period before that is reported
as unavailable, never as zero.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import activity, intelligence
from api.core.permissions import Permission
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import simple

DEFAULT_WINDOW_NOTE = (
    "Foreground activity tracking began at a known date; periods before it are reported as "
    "unavailable, not as zero activity."
)


class GrowthArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)


async def growth_registrations(db: AsyncSession, args: GrowthArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    pulse = await intelligence.pulse(db, window_days=args.window_days, now=end)
    retention = await activity.registration_retention(db, start=start, end=end, now=end)
    metrics = pulse["metrics"]
    evidence = [
        simple(
            prefix="growth", key="new_users", label="New registrations",
            value=(metrics.get("new_users") or {}).get("current"),
            previous=(metrics.get("new_users") or {}).get("previous"),
            source="api.core.intelligence.pulse",
            definition="Accounts created in the period, administrators excluded.",
            start=start, end=end, href="/growth",
        ),
        simple(
            prefix="growth", key="new_sellers", label="New sellers",
            value=(metrics.get("new_sellers") or {}).get("current"),
            previous=(metrics.get("new_sellers") or {}).get("previous"),
            source="api.core.intelligence.pulse",
            definition="Accounts whose first listing ever was posted in the period.",
            start=start, end=end, href="/growth",
        ),
    ]
    cohorts = retention.get("cohorts", [])
    if cohorts:
        latest = cohorts[-1]
        evidence.append(simple(
            prefix="growth", key="latest_cohort_registered", label="Latest cohort size",
            value=latest.get("registered"), source="api.core.activity.registration_retention",
            definition=f"Accounts registered on {latest.get('cohort_date')}.",
            start=start, end=end, href="/analytics/users",
        ))
    return ToolOutput(
        evidence=evidence,
        data={
            "metrics": {k: metrics.get(k) for k in (
                "new_users", "new_sellers", "new_listings", "active_buyers", "active_sellers",
            )},
            "retention": retention,
            "coverage_started_on": retention.get("coverage_started_on"),
        },
        limitations=[DEFAULT_WINDOW_NOTE],
    )


async def growth_engagement(db: AsyncSession, args: GrowthArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    summary = await activity.engagement_summary(db, start=start, end=end, now=end)
    evidence = []
    for key, label in (
        ("dau", "Daily active accounts"),
        ("wau", "Weekly active accounts"),
        ("mau", "Monthly active accounts"),
    ):
        if key in summary:
            evidence.append(simple(
                prefix="growth.engagement", key=key, label=label,
                value=summary.get(key), source="api.core.activity.engagement_summary",
                definition="Distinct accounts with a recorded foreground activity day.",
                start=start, end=end, href="/analytics/users",
            ))
    return ToolOutput(
        evidence=evidence,
        data={"engagement": summary},
        limitations=[DEFAULT_WINDOW_NOTE],
    )


class CohortArgs(ToolInput):
    weeks: int = Field(default=8, ge=1, le=26)


async def growth_cohorts(db: AsyncSession, args: CohortArgs) -> ToolOutput:
    now = datetime.utcnow()
    report = await intelligence.cohorts(db, weeks=args.weeks, now=now)
    cohorts = report.get("cohorts", [])
    evidence = []
    for row in cohorts[-6:]:
        evidence.append(simple(
            prefix="growth.cohort", key=str(row.get("cohort") or row.get("week") or "unknown"),
            label=f"Signup cohort {row.get('cohort') or row.get('week')}",
            value=row.get("size") or row.get("signups") or row.get("registered"),
            unit="count", source="api.core.intelligence.cohorts",
            definition="Accounts in the weekly signup cohort; later weeks show the share active again.",
            start=now - timedelta(weeks=args.weeks), end=now,
            confidence="measured", href="/growth",
        ))
    return ToolOutput(
        evidence=evidence,
        data={"cohorts": cohorts, "definitions": report.get("definitions", {})},
        limitations=["Cohort cells are null until the cohort's N-day mark has passed and activity tracking covered it."],
    )


async def growth_funnel(db: AsyncSession, args: GrowthArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    report = await intelligence.funnel(db, start=start, end=end)
    evidence = []
    for step in report.get("steps", []):
        evidence.append(simple(
            prefix="growth.funnel", key=str(step.get("key")), label=str(step.get("label")),
            value=step.get("count"), unit="count",
            source="api.core.intelligence.funnel",
            definition="Accounts from the signup cohort reaching this stage.",
            start=start, end=end, confidence="derived", href="/growth",
        ))
    return ToolOutput(
        evidence=evidence,
        data={"funnel": {k: v for k, v in report.items() if k != "definitions"},
              "definitions": report.get("definitions", {})},
        limitations=[],
    )


def register(registry) -> None:
    common = dict(
        domain="growth",
        permission=Permission.ADMIN_SUMMARY,
        evidence_source="api.core.activity / api.core.intelligence",
        data_availability="Available after activity tracking began; earlier periods are unavailable.",
        timeout_seconds=15.0,
    )
    registry.register_all([
        ToolDefinition(
            name="growth_registrations",
            description="New registrations, new sellers and registration retention by cohort.",
            input_model=GrowthArgs, handler=growth_registrations, **common,
        ),
        ToolDefinition(
            name="growth_engagement",
            description="Daily, weekly and monthly active accounts.",
            input_model=GrowthArgs, handler=growth_engagement, **common,
        ),
        ToolDefinition(
            name="growth_cohorts",
            description="Weekly signup cohorts and their later activity.",
            input_model=CohortArgs, handler=growth_cohorts, **common,
        ),
        ToolDefinition(
            name="growth_funnel",
            description="A signup cohort followed through listing, buying and deal stages.",
            input_model=GrowthArgs, handler=growth_funnel, **common,
        ),
    ])
