"""Persistence for administrator-owned Jarvis conversations.

Two tables, separate from the customer-facing ``zeno_conversations`` /
``zeno_turns`` rows: an administrative conversation holds business evidence
and administrator questions and must never be reachable through a buyer's or
seller's Zeno history.

Both tables are created by ``Base.metadata.create_all()`` (registered in
``api/database.py``). There are no changes to existing tables, so no ALTER
statement is needed.

Retention is fixed at 30 days: ``expires_at`` is refreshed on every turn and
``conversation_service.cleanup_expired`` deletes expired conversations
together with their turns. Deleting a conversation deletes its turns
explicitly rather than relying on database-level cascade, because SQLite (the
test and development database) does not enforce foreign keys by default.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa

from api.database import Base


def _new_id() -> str:
    # UUID4 is unguessable, which is what makes a conversation id safe to use
    # in a URL: ownership is still checked on every access, but a caller
    # cannot enumerate other administrators' conversations.
    return str(uuid.uuid4())


class AdminZenoConversation(Base):
    __tablename__ = "admin_zeno_conversations"

    id = sa.Column(sa.String, primary_key=True, default=_new_id)
    admin_user_id = sa.Column(
        sa.String, sa.ForeignKey("users.id"), nullable=False, index=True
    )
    title = sa.Column(sa.String(200), nullable=False, default="New investigation")
    created_at = sa.Column(sa.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = sa.Column(
        sa.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    # 30-day retention, refreshed on every turn.
    expires_at = sa.Column(sa.DateTime, nullable=False, index=True)

    __table_args__ = (
        sa.Index("ix_admin_zeno_conversations_owner_updated", "admin_user_id", "updated_at"),
        sa.Index("ix_admin_zeno_conversations_owner_expires", "admin_user_id", "expires_at"),
    )


class AdminZenoTurn(Base):
    __tablename__ = "admin_zeno_turns"

    id = sa.Column(sa.String, primary_key=True, default=_new_id)
    conversation_id = sa.Column(
        sa.String,
        sa.ForeignKey("admin_zeno_conversations.id"),
        nullable=False,
        index=True,
    )
    # "user" or "assistant". The assistant turn stores the user-facing answer
    # and its evidence references, never the model's private reasoning.
    role = sa.Column(sa.String(16), nullable=False)
    content = sa.Column(sa.Text, nullable=False, default="")
    evidence_json = sa.Column(sa.JSON, nullable=True)
    metadata_json = sa.Column(sa.JSON, nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        sa.Index("ix_admin_zeno_turns_conversation_created", "conversation_id", "created_at"),
    )
