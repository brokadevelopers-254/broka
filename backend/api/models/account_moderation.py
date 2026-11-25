"""Account status changes as an append-only log (see api/core/account_status.py).

Rows are only ever inserted. Nothing in the application updates or deletes
them. The current status lives on the user row; this table is the history of
how it got there, with who did it, why, and the evidence references offered.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text

from api.database import Base


class AccountModerationEvent(Base):
    __tablename__ = "account_moderation_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    # restricted | suspended | suspension_expired | reinstated
    action = Column(String(32), nullable=False)
    from_status = Column(String(24), nullable=False)
    to_status = Column(String(24), nullable=False)
    suspended_until = Column(DateTime, nullable=True)
    reason = Column(Text, nullable=False)
    notes = Column(Text, nullable=True)
    # JSON list of references to evidence held elsewhere (a complaint id, a
    # case number). The evidence itself is never stored in this table.
    evidence_refs = Column(Text, nullable=True)
    # A user id, or "system" for an automatic expiry. Not a foreign key.
    actor_id = Column(String, nullable=False)
    request_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_account_moderation_events_created", "created_at"),
    )
