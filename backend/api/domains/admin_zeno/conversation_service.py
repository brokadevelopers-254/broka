"""Administrator-owned Jarvis conversation persistence.

Ownership is part of every read: a conversation is fetched with
``admin_user_id == actor_id`` in the same statement, so an unguessable id
alone cannot expose another administrator's investigation. Deletion is
scoped the same way and removes the turns explicitly (SQLite does not
enforce foreign keys by default; PostgreSQL would cascade only if the
constraint asked it to, which this schema does not rely on).

Only what is needed to continue the conversation is stored: the question,
the user-facing answer, its evidence references and safe metadata. No
prompt, credential, model reasoning trace or customer personal data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.domains.admin_zeno.constants import (
    CONVERSATION_RETENTION_DAYS,
    MAX_CONVERSATION_LIST_LIMIT,
    MAX_CONVERSATION_TURNS,
)
from api.domains.admin_zeno.models import AdminZenoConversation, AdminZenoTurn
from api.domains.admin_zeno.schemas import Evidence

log = logging.getLogger(__name__)


def _expiry(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.utcnow()) + timedelta(days=CONVERSATION_RETENTION_DAYS)


def _title_from(message: str) -> str:
    """A short, safe title. It is the administrator's own question, never
    retrieved customer text, and it is bounded."""
    text = " ".join((message or "").split())
    return (text[:120] or "New investigation")


async def create_conversation(
    db: AsyncSession, *, admin_user_id: str, title: Optional[str] = None
) -> AdminZenoConversation:
    now = datetime.utcnow()
    row = AdminZenoConversation(
        admin_user_id=admin_user_id,
        title=(title or "New investigation")[:200],
        created_at=now,
        updated_at=now,
        expires_at=_expiry(now),
    )
    db.add(row)
    await db.commit()
    return row


async def get_owned(
    db: AsyncSession, *, conversation_id: str, admin_user_id: str
) -> Optional[AdminZenoConversation]:
    """Fetch an unexpired, owned conversation.

    Expiry is enforced at read time, not only by the background sweep: a
    retention deadline that is only applied eventually is not a retention
    policy. An expired conversation is indistinguishable from a missing one.
    """
    return (await db.execute(
        select(AdminZenoConversation).where(
            AdminZenoConversation.id == conversation_id,
            AdminZenoConversation.admin_user_id == admin_user_id,
            AdminZenoConversation.expires_at > datetime.utcnow(),
        )
    )).scalar_one_or_none()


async def list_conversations(
    db: AsyncSession, *, admin_user_id: str, limit: int, offset: int
) -> tuple[list[AdminZenoConversation], int]:
    limit = max(1, min(int(limit), MAX_CONVERSATION_LIST_LIMIT))
    offset = max(0, int(offset))
    live = (
        AdminZenoConversation.admin_user_id == admin_user_id,
        AdminZenoConversation.expires_at > datetime.utcnow(),
    )
    total = int((await db.execute(
        select(func.count(AdminZenoConversation.id)).where(*live)
    )).scalar() or 0)
    rows = (await db.execute(
        select(AdminZenoConversation)
        .where(*live)
        .order_by(AdminZenoConversation.updated_at.desc(), AdminZenoConversation.id)
        .limit(limit)
        .offset(offset)
    )).scalars().all()
    return list(rows), total


async def turn_counts(
    db: AsyncSession, conversation_ids: Sequence[str]
) -> dict[str, int]:
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        select(AdminZenoTurn.conversation_id, func.count(AdminZenoTurn.id))
        .where(AdminZenoTurn.conversation_id.in_(list(conversation_ids)))
        .group_by(AdminZenoTurn.conversation_id)
    )).all()
    return {str(cid): int(n or 0) for cid, n in rows}


async def list_turns(
    db: AsyncSession, *, conversation_id: str, limit: int, offset: int
) -> list[AdminZenoTurn]:
    limit = max(1, min(int(limit), MAX_CONVERSATION_TURNS))
    offset = max(0, int(offset))
    return list((await db.execute(
        select(AdminZenoTurn)
        .where(AdminZenoTurn.conversation_id == conversation_id)
        .order_by(AdminZenoTurn.created_at, AdminZenoTurn.id)
        .limit(limit)
        .offset(offset)
    )).scalars().all())


async def recent_turns(
    db: AsyncSession, *, conversation_id: str, limit: int
) -> list[dict]:
    """The bounded context given to the model, oldest first.

    Assistant turns are stored as their user-facing answer, not a reasoning
    trace, so this cannot leak private chain-of-thought into a later prompt.
    """
    rows = list((await db.execute(
        select(AdminZenoTurn)
        .where(AdminZenoTurn.conversation_id == conversation_id)
        .order_by(AdminZenoTurn.created_at.desc(), AdminZenoTurn.id.desc())
        .limit(max(0, limit))
    )).scalars().all())
    rows.reverse()
    return [{"role": r.role, "content": r.content} for r in rows]


async def append_turn(
    db: AsyncSession,
    *,
    conversation_id: str,
    role: str,
    content: str,
    evidence: Optional[list[Evidence]] = None,
    metadata: Optional[dict] = None,
) -> AdminZenoTurn:
    now = datetime.utcnow()
    turn = AdminZenoTurn(
        conversation_id=conversation_id,
        role=role,
        content=content,
        evidence_json=[e.model_dump(mode="json") for e in (evidence or [])] or None,
        metadata_json=metadata or None,
        created_at=now,
    )
    db.add(turn)
    # Only a live conversation has its retention refreshed. Touching an
    # expired row (or a stale reference to one) must not resurrect it.
    conversation = (await db.execute(
        select(AdminZenoConversation).where(
            AdminZenoConversation.id == conversation_id,
            AdminZenoConversation.expires_at > now,
        )
    )).scalar_one_or_none()
    if conversation is not None:
        conversation.updated_at = now
        conversation.expires_at = _expiry(now)
    await db.commit()
    return turn


async def set_default_title(
    db: AsyncSession, *, conversation_id: str, title: str
) -> None:
    """Name a conversation after its first question, once.

    An UPDATE keeps this working after a tool's database recovery expired ORM
    objects loaded earlier in the same request (AGENTS.md: after a rollback,
    do not touch objects loaded before it).
    """
    await db.execute(
        update(AdminZenoConversation)
        .where(
            AdminZenoConversation.id == conversation_id,
            AdminZenoConversation.title.in_(("", "New investigation")),
        )
        .values(title=_title_from(title)[:200], updated_at=datetime.utcnow())
    )
    await db.commit()


async def delete_owned(
    db: AsyncSession, *, conversation_id: str, admin_user_id: str
) -> bool:
    conversation = await get_owned(
        db, conversation_id=conversation_id, admin_user_id=admin_user_id
    )
    if conversation is None:
        return False
    await db.execute(
        delete(AdminZenoTurn).where(AdminZenoTurn.conversation_id == conversation.id)
    )
    await db.execute(
        delete(AdminZenoConversation).where(
            AdminZenoConversation.id == conversation.id,
            AdminZenoConversation.admin_user_id == admin_user_id,
        )
    )
    await db.commit()
    return True


async def cleanup_expired(db: AsyncSession, *, now: Optional[datetime] = None) -> int:
    """Delete expired conversations and their turns. Safe to repeat."""
    moment = now or datetime.utcnow()
    expired_ids = list((await db.execute(
        select(AdminZenoConversation.id).where(AdminZenoConversation.expires_at <= moment)
    )).scalars().all())
    if not expired_ids:
        return 0
    await db.execute(
        delete(AdminZenoTurn).where(AdminZenoTurn.conversation_id.in_(expired_ids))
    )
    await db.execute(
        delete(AdminZenoConversation).where(AdminZenoConversation.id.in_(expired_ids))
    )
    await db.commit()
    log.info("[admin_zeno] retention removed %d expired conversations", len(expired_ids))
    return len(expired_ids)
