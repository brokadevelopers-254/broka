"""Signed-in sessions and foreground activity (see api/core/activity.py).

Every DateTime here is naive UTC, like the rest of the schema.

user_sessions       one row per signed-in device session: a login, a sign-up,
                    a sign-in by code, or a password change. Heartbeats move
                    last_active_at and foreground_seconds. A session that goes
                    quiet is closed by the sweep; logout, revocation and
                    suspension close it at once.
user_activity_days  one row per user per UTC calendar day with credited
                    foreground time. DAU, WAU, MAU and the activity trend are
                    counted from this table, so no query has to rebuild days
                    from session intervals.
activity_samples    active users and sessions counted once a minute. Peak
                    concurrency is the maximum over these samples; session
                    rows alone cannot recover it.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, Index, Integer, String

from api.database import Base


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_active_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Seconds of foreground time credited from consecutive heartbeats. This
    # is the session's usage duration; last_active_at - started_at is its
    # wall-clock span, which includes time the app was not in use.
    foreground_seconds = Column(Integer, nullable=False, default=0)
    ended_at = Column(DateTime, nullable=True)
    # logout | revoked | suspended | inactive. "inactive" is the only reason
    # a later heartbeat may reopen a session.
    end_reason = Column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_user_sessions_last_active", "last_active_at"),
        Index("ix_user_sessions_started", "started_at"),
    )


class UserActivityDay(Base):
    __tablename__ = "user_activity_days"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    activity_date = Column(Date, primary_key=True)
    first_active_at = Column(DateTime, nullable=False)
    last_active_at = Column(DateTime, nullable=False)
    foreground_seconds = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_user_activity_days_date", "activity_date"),
    )


class ActivitySample(Base):
    __tablename__ = "activity_samples"

    sampled_at = Column(DateTime, primary_key=True)
    active_users = Column(Integer, nullable=False)
    active_sessions = Column(Integer, nullable=False)
