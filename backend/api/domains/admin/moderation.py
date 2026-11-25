"""Restrict, suspend and reinstate accounts (api/core/account_status.py).

Flagging (MANAGE_USERS, in router.py) is a signal. These routes change what an
account may do. Each change needs MANAGE_ACCOUNT_STATUS and a reason, names the
status version it was based on, and is written to the audit log and to the
account's append-only moderation history.

Refused, whatever the permissions:
  * changing your own account (a suspension you could undo is not a control);
  * changing an administrator's account: demote them first, as a separate act;
  * acting on a version the account has already moved past.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import account_status
from api.core.observability import request_id_var
from api.core.permissions import Permission, require_permission
from api.database import User, get_db
from api.models.account_moderation import AccountModerationEvent

router = APIRouter()

_MAX_EVIDENCE = 20


class RestrictIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=2000)
    expected_version: int = Field(ge=0)
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=_MAX_EVIDENCE,
    )


class SuspendIn(RestrictIn):
    duration: Literal["24h", "7d", "30d", "permanent"]


class ReinstateIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=2000)
    expected_version: int = Field(ge=0)


def _state(user: User, now: datetime) -> dict:
    return {
        "user_id": user.id,
        "status": account_status.effective_status(user.account_status, user.suspended_until, now),
        "suspended_until": user.suspended_until.isoformat() + "Z" if user.suspended_until else None,
        "status_reason": user.status_reason,
        "status_changed_at": user.status_changed_at.isoformat() + "Z" if user.status_changed_at else None,
        "status_changed_by": user.status_changed_by,
        "status_version": user.status_version or 0,
    }


async def _target(db: AsyncSession, user_id: str, admin: User) -> User:
    if user_id == admin.id:
        raise HTTPException(status_code=403, detail="You cannot change your own account status.")
    # Row lock: two administrators acting at once take turns, and the second sees
    # the version the first wrote.
    user = (await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        raise HTTPException(
            status_code=409,
            detail="Demote this administrator before changing their account status.",
        )
    return user


def _expect_version(user: User, expected: int) -> None:
    current = user.status_version or 0
    if current != expected:
        raise HTTPException(
            status_code=409,
            detail=f"This account changed since you loaded it (now version {current}). Reload and try again.",
        )


async def _apply(
    db: AsyncSession,
    user: User,
    admin: User,
    *,
    action: str,
    new_status: str,
    suspended_until: Optional[datetime],
    body,
) -> dict:
    now = datetime.utcnow()
    await account_status.apply_change(
        db, user,
        actor_id=admin.id,
        action=action,
        new_status=new_status,
        suspended_until=suspended_until,
        reason=body.reason,
        notes=body.notes,
        evidence_refs=getattr(body, "evidence_refs", None),
        request_id=request_id_var.get(None),
        now=now,
    )
    await db.commit()
    account_status.forget(user.id)
    return _state(user, now)


@router.get("/users/{user_id}/moderation")
async def account_moderation_history(
    user_id: str,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    db: AsyncSession = Depends(get_db),
):
    """The account's status and its history of changes, newest first."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    events = (await db.execute(
        select(AccountModerationEvent)
        .where(AccountModerationEvent.user_id == user_id)
        .order_by(AccountModerationEvent.created_at.desc(), AccountModerationEvent.id.desc())
        .limit(100)
    )).scalars().all()
    now = datetime.utcnow()
    return {
        **_state(user, now),
        "history": [
            {
                "id": event.id,
                "action": event.action,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "suspended_until": event.suspended_until.isoformat() + "Z" if event.suspended_until else None,
                "reason": event.reason,
                "notes": event.notes,
                "evidence_refs": event.evidence_refs,
                "actor_id": event.actor_id,
                "request_id": event.request_id,
                "created_at": event.created_at.isoformat() + "Z",
            }
            for event in events
        ],
    }


@router.post("/users/{user_id}/restrict")
async def restrict_user(
    user_id: str,
    body: RestrictIn,
    admin: User = Depends(require_permission(Permission.MANAGE_ACCOUNT_STATUS)),
    db: AsyncSession = Depends(get_db),
):
    user = await _target(db, user_id, admin)
    _expect_version(user, body.expected_version)
    current = account_status.effective_status(user.account_status, user.suspended_until, datetime.utcnow())
    if current == "suspended":
        raise HTTPException(status_code=409, detail="Reinstate this account before restricting it.")
    if current == "restricted":
        raise HTTPException(status_code=409, detail="This account is already restricted.")
    return await _apply(
        db, user, admin, action="restricted", new_status="restricted",
        suspended_until=None, body=body,
    )


@router.post("/users/{user_id}/suspend")
async def suspend_user(
    user_id: str,
    body: SuspendIn,
    admin: User = Depends(require_permission(Permission.MANAGE_ACCOUNT_STATUS)),
    db: AsyncSession = Depends(get_db),
):
    user = await _target(db, user_id, admin)
    _expect_version(user, body.expected_version)
    duration = account_status.SUSPENSION_DURATIONS[body.duration]
    now = datetime.utcnow()
    until = now + duration if duration is not None else None
    return await _apply(
        db, user, admin, action="suspended", new_status="suspended",
        suspended_until=until, body=body,
    )


@router.post("/users/{user_id}/reinstate")
async def reinstate_user(
    user_id: str,
    body: ReinstateIn,
    admin: User = Depends(require_permission(Permission.MANAGE_ACCOUNT_STATUS)),
    db: AsyncSession = Depends(get_db),
):
    user = await _target(db, user_id, admin)
    _expect_version(user, body.expected_version)
    current = account_status.effective_status(user.account_status, user.suspended_until, datetime.utcnow())
    if current == "active":
        raise HTTPException(status_code=409, detail="This account is not restricted or suspended.")
    return await _apply(
        db, user, admin, action="reinstated", new_status="active",
        suspended_until=None, body=body,
    )
