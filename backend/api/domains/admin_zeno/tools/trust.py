"""Trust and safety intelligence tools.

Aggregates only: counts of reports, complaints and moderation activity. No
message content, call content, phone number or other customer-identifying
data is read or sent to a model. Individual account investigation needs the
existing narrower admin routes and is deliberately out of scope here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import intelligence
from api.core.permissions import Permission
from api.database import FraudEvent
from api.domains.admin_zeno.schemas import ToolInput, ToolOutput
from api.domains.admin_zeno.tool_registry import ToolDefinition
from api.domains.admin_zeno.tools._common import simple
from api.models.account_moderation import AccountModerationEvent
from api.models.dispute import CaseState, DisputeCase
from api.models.scam_report import ScamReportDetail

SOURCE = "api.core.intelligence.complaints_overview"
PII_NOTE = (
    "Aggregates only. No message content, call content, password, phone number or "
    "unnecessary customer-identifying data is read or sent to a model."
)


class TrustArgs(ToolInput):
    window_days: int = Field(default=30, ge=1, le=90)


async def _count(db: AsyncSession, column, *where) -> int:
    return int((await db.execute(select(func.count(column)).where(*where))).scalar() or 0)


async def trust_overview(db: AsyncSession, args: TrustArgs) -> ToolOutput:
    end = datetime.utcnow()
    start = end - timedelta(days=args.window_days)
    complaints = await intelligence.complaints_overview(db, start, end, now=end)
    filed = complaints.get("filed") or {}

    disputed_states = [s.value for s in CaseState]
    open_states = [
        s.value for s in CaseState
        if s.value not in ("resolved", "rejected", "cancelled", "closed", "refunded", "released")
    ]
    fraud_reports = await _count(
        db, ScamReportDetail.feedback_id,
        ScamReportDetail.created_at >= start, ScamReportDetail.created_at < end,
    )
    disputes_opened = await _count(
        db, DisputeCase.id, DisputeCase.created_at >= start, DisputeCase.created_at < end,
    )
    disputes_open = await _count(
        db, DisputeCase.id, DisputeCase.state.in_(open_states),
    ) if open_states else 0
    fraud_flags = await _count(
        db, FraudEvent.id, FraudEvent.created_at >= start, FraudEvent.created_at < end,
    )
    moderation_actions = await _count(
        db, AccountModerationEvent.id,
        AccountModerationEvent.created_at >= start, AccountModerationEvent.created_at < end,
    )

    evidence = [
        simple(prefix="trust", key="complaints_filed", label="Complaints filed",
               value=filed.get("current"), previous=filed.get("previous"),
               source=SOURCE, definition="Complaints filed in the period, against the period before.",
               start=start, end=end, href="/complaints"),
        simple(prefix="trust", key="complaints_open", label="Complaints open now",
               value=complaints.get("open"), source=SOURCE,
               definition="Complaints still open, whatever their age.", href="/complaints"),
        simple(prefix="trust", key="complaints_unanswered", label="Open complaints never answered",
               value=complaints.get("unanswered"), source=SOURCE,
               definition="Open complaints with no reply from BROKA.", href="/complaints"),
        simple(prefix="trust", key="complaints_sla_breached", label="Complaints past the first-reply deadline",
               value=complaints.get("sla_breached"), source=SOURCE,
               definition="Open, unanswered complaints older than their severity's deadline.",
               href="/complaints?queue=overdue"),
        simple(prefix="trust", key="scam_reports_filed", label="Fraud reports filed",
               value=fraud_reports, source="api.models.scam_report",
               definition="Fraud reports filed in the period.", start=start, end=end,
               href="/scam-reports"),
        simple(prefix="trust", key="disputes_opened", label="Disputes opened",
               value=disputes_opened, source="api.models.dispute",
               definition="Dispute cases opened in the period.", start=start, end=end,
               href="/disputes"),
        simple(prefix="trust", key="disputes_open", label="Dispute cases open now",
               value=disputes_open, source="api.models.dispute",
               definition="Dispute cases in an active state now.", href="/disputes"),
        simple(prefix="trust", key="fraud_flags_raised", label="Fraud flags raised",
               value=fraud_flags, source="api.models.fraud",
               definition="Times the fraud engine flagged an account in the period.",
               start=start, end=end, href="/users?flagged=1"),
        simple(prefix="trust", key="moderation_actions", label="Account moderation actions",
               value=moderation_actions, source="api.models.account_moderation",
               definition="Restrictions, suspensions and reinstatements recorded in the period.",
               start=start, end=end, href="/users"),
    ]
    # Dispute states are an internal enum; exposing the valid set is useful
    # to nobody and confusing to read, so only the aggregate leaves the tool.
    del disputed_states
    return ToolOutput(
        evidence=evidence,
        data={
            "by_category": complaints.get("by_category", {}),
            "open_by_severity": complaints.get("open_by_severity", {}),
            "median_response_hours_90d": complaints.get("median_response_hours_90d"),
            "repeat_reported_users": complaints.get("repeat_reported_users", []),
            "sla_hours": complaints.get("sla_hours", {}),
            "note": PII_NOTE,
        },
        limitations=[PII_NOTE, "Individual account investigations are not available through Jarvis."],
    )


def register(registry) -> None:
    registry.register(ToolDefinition(
        name="trust_overview",
        description=(
            "Aggregate trust and safety figures: complaints, overdue complaints, fraud reports, "
            "disputes and moderation activity. No customer-identifying detail."
        ),
        domain="trust",
        permission=Permission.VIEW_ALL_DISPUTES,
        input_model=TrustArgs,
        handler=trust_overview,
        timeout_seconds=12.0,
        evidence_source=SOURCE,
        data_availability="Available from complaints, dispute, fraud and moderation aggregates.",
    ))
