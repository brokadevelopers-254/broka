"""Saved custom reports and the reports generated from them.

ReportDefinition - what an administrator built in the report builder: the
  categories, metrics, comparison, filters, grouping and visualization
  (`spec`), and optionally a schedule with admin recipients.
ReportRun - one generated report. Stored, never recomputed on read: some
  figures are as of the moment of generation (live listings, open
  complaints), and a report read later must say what was known then. The
  spec it was built from is copied into the run, so editing a definition
  never changes a report already written. Regenerating writes a new run
  that points at the one it replaces; the old one stays in the archive.

Only aggregates and the names admin screens already show are kept in a
payload - never contact details, message text or complaint bodies. The
fixed-length string columns that hold "system" or a user id are not
foreign keys (AGENTS.md).
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa

from api.database import Base

SCHEDULES = ("none", "daily", "weekly", "monthly", "custom")
TRIGGERS = ("manual", "schedule", "regenerate")
RUN_STATUSES = ("ready", "failed")


def _uuid() -> str:
    return str(uuid.uuid4())


class ReportDefinition(Base):
    __tablename__ = "report_definitions"

    id = sa.Column(sa.String(36), primary_key=True, default=_uuid)
    name = sa.Column(sa.String(120), nullable=False)
    description = sa.Column(sa.String(500), nullable=True)
    spec = sa.Column(sa.JSON, nullable=False)

    # none | daily | weekly | monthly | custom (every schedule_every_days days)
    schedule = sa.Column(sa.String(16), nullable=False, default="none")
    schedule_every_days = sa.Column(sa.Integer, nullable=True)
    schedule_hour_utc = sa.Column(sa.Integer, nullable=False, default=4)
    schedule_weekday = sa.Column(sa.Integer, nullable=True)     # 0 = Monday, weekly only
    schedule_day = sa.Column(sa.Integer, nullable=True)         # 1-28, monthly only
    # Administrator user ids, never addresses: the address is read when the
    # email goes, so a recipient who is no longer an administrator gets nothing.
    recipients = sa.Column(sa.JSON, nullable=False, default=list)
    enabled = sa.Column(sa.Boolean, nullable=False, default=True)
    next_run_at = sa.Column(sa.DateTime, nullable=True)
    last_run_at = sa.Column(sa.DateTime, nullable=True)
    last_run_id = sa.Column(sa.String(36), nullable=True)
    last_status = sa.Column(sa.String(16), nullable=True)
    last_error = sa.Column(sa.String(200), nullable=True)

    created_by = sa.Column(sa.String(64), nullable=False)
    created_at = sa.Column(sa.DateTime, nullable=False, default=datetime.utcnow)
    updated_by = sa.Column(sa.String(64), nullable=True)
    updated_at = sa.Column(sa.DateTime, nullable=False, default=datetime.utcnow)
    # An edit names the version it read; a stale one is refused (409).
    version = sa.Column(sa.Integer, nullable=False, default=1)
    deleted_at = sa.Column(sa.DateTime, nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "schedule IN ('none','daily','weekly','monthly','custom')",
            name="ck_report_definitions_schedule",
        ),
        sa.Index("ix_report_definitions_due", "enabled", "next_run_at"),
    )


class ReportRun(Base):
    __tablename__ = "report_runs"

    id = sa.Column(sa.String(36), primary_key=True, default=_uuid)
    definition_id = sa.Column(sa.String(36), nullable=True, index=True)
    title = sa.Column(sa.String(160), nullable=False)
    # The spec exactly as it was built, whatever the definition says now.
    spec = sa.Column(sa.JSON, nullable=False)
    # Naive UTC, half-open [period_start, period_end).
    period_start = sa.Column(sa.DateTime, nullable=False)
    period_end = sa.Column(sa.DateTime, nullable=False)
    comparison_start = sa.Column(sa.DateTime, nullable=True)
    comparison_end = sa.Column(sa.DateTime, nullable=True)
    # manual | schedule | regenerate
    trigger = sa.Column(sa.String(16), nullable=False, default="manual")
    regenerated_from = sa.Column(sa.String(36), nullable=True)
    # "system" for a scheduled run, else the administrator's user id.
    generated_by = sa.Column(sa.String(64), nullable=False)
    generated_at = sa.Column(sa.DateTime, nullable=False, default=datetime.utcnow, index=True)
    # ready | failed
    status = sa.Column(sa.String(16), nullable=False, default="ready")
    error = sa.Column(sa.String(200), nullable=True)
    payload = sa.Column(sa.JSON, nullable=True)
    # One entry per recipient of a scheduled email: {user_id, status, failure}.
    delivery = sa.Column(sa.JSON, nullable=True)

    __table_args__ = (
        sa.CheckConstraint("trigger IN ('manual','schedule','regenerate')", name="ck_report_runs_trigger"),
        sa.CheckConstraint("status IN ('ready','failed')", name="ck_report_runs_status"),
    )
