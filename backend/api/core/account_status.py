"""Account status: restriction and suspension, enforced by the backend.

Statuses on the user row:
  active       no restriction.
  restricted   may sign in and use the app, but not create listings, finalise
               deals, boost or buy verification. This is the same set of
               permissions a low trust score removes (api/core/permissions.py).
  suspended    may not sign in or refresh a session. Every bearer token of the
               account is refused, on HTTP and on WebSockets, by
               AccountStatusMiddleware. Its sessions, refresh tokens and push
               devices are ended when the suspension is applied.

A temporary suspension ends at suspended_until. From that moment it reads as
active, and the sweep records the expiry. A permanent suspension has no
suspended_until and never expires.

Enforcement reads the user row through a cache of CACHE_TTL_SECONDS in each
process. The process that applies a change forgets its entry at once; other
processes pick the change up within that window. If the status cannot be read,
an authenticated request is refused with 503 rather than allowed through.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from starlette.responses import JSONResponse

from api.security import decode_access_token

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 10.0
_CACHE_MAX_ENTRIES = 50_000
SUSPENDED_DETAIL = "This account is suspended. Contact BROKA support to find out more."
UNVERIFIED_DETAIL = "Account status could not be verified. Try again shortly."

# The durations an administrator can choose. None means permanent.
SUSPENSION_DURATIONS: dict[str, Optional[timedelta]] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "permanent": None,
}

_cache: dict[str, tuple[float, Optional[tuple[str, Optional[datetime]]]]] = {}


def _now() -> datetime:
    return datetime.utcnow()


def effective_status(status_value: Optional[str], suspended_until: Optional[datetime], now: datetime) -> str:
    """The status that applies at `now`. An ended temporary suspension is active."""
    current = status_value or "active"
    if current == "suspended" and suspended_until is not None and suspended_until <= now:
        return "active"
    return current


def is_suspended(status_value: Optional[str], suspended_until: Optional[datetime], now: Optional[datetime] = None) -> bool:
    return effective_status(status_value, suspended_until, now or _now()) == "suspended"


def ensure_not_suspended(status_value: Optional[str], suspended_until: Optional[datetime]) -> None:
    """Raise 403 when a sign-in or a refresh would give a suspended account a session."""
    if is_suspended(status_value, suspended_until):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=SUSPENDED_DETAIL)


def forget(user_id: str) -> None:
    """Drop this process's cached status for an account after a change."""
    _cache.pop(user_id, None)


async def _status_of(user_id: str) -> Optional[tuple[str, Optional[datetime]]]:
    """(status, suspended_until) from the cache or the database. None for an unknown account."""
    from api.database import AsyncSessionLocal, User

    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(User.account_status, User.suspended_until).where(User.id == user_id)
        )).first()
    value = (row[0] or "active", row[1]) if row is not None else None
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        _cache.clear()
    _cache[user_id] = (now + CACHE_TTL_SECONDS, value)
    return value


def _bearer_or_query_token(scope) -> Optional[str]:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1")
            return text[7:].strip() if text[:7].lower() == "bearer " else None
    if scope.get("type") == "websocket":
        # Socket routes take their access token in the query string.
        values = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("token")
        return values[0] if values else None
    return None


class AccountStatusMiddleware:
    """Refuses requests from a suspended account, on every HTTP and WebSocket route.

    It acts only on a valid access token, so public routes and routes that take
    other credentials pass through unchanged. Routes that check the token
    themselves still do, and a suspended account fails here first.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = _bearer_or_query_token(scope)
        claims = decode_access_token(token) if token else None
        if not claims or not claims.get("sub"):
            await self.app(scope, receive, send)
            return

        try:
            value = await _status_of(claims["sub"])
        except Exception as exc:  # the status is unknown: refuse rather than guess
            logger.error("[account-status] could not read status type=%s", type(exc).__name__)
            await _refuse(scope, receive, send, http_status.HTTP_503_SERVICE_UNAVAILABLE, UNVERIFIED_DETAIL)
            return

        if value is not None and is_suspended(value[0], value[1]):
            await _refuse(scope, receive, send, http_status.HTTP_403_FORBIDDEN, SUSPENDED_DETAIL)
            return
        await self.app(scope, receive, send)


async def _refuse(scope, receive, send, code: int, detail: str) -> None:
    if scope.get("type") == "websocket":
        # Closing before accept makes the server answer the handshake with 403.
        await send({"type": "websocket.close", "code": 4403, "reason": detail[:120]})
        return
    await JSONResponse({"detail": detail}, status_code=code)(scope, receive, send)


async def apply_change(
    db,
    target,
    *,
    actor_id: str,
    action: str,
    new_status: str,
    suspended_until: Optional[datetime],
    reason: str,
    notes: Optional[str],
    evidence_refs: Optional[list[str]],
    request_id: Optional[str],
    now: Optional[datetime] = None,
) -> None:
    """Change an account's status, record it, and end what a suspension must end.

    The caller commits, then calls forget(target.id). The row is updated with
    a version bump so the next change must name the version it saw. Ending
    sessions and tokens happens in the same transaction, so a commit either
    does all of it or none.
    """
    from api.core.audit import record_audit
    from api.core import activity
    from api.core import push_devices
    from api.domains.auth.refresh_router import revoke_all_refresh_tokens
    from api.models.account_moderation import AccountModerationEvent

    now = now or _now()
    previous = effective_status(target.account_status, target.suspended_until, now)
    target.account_status = new_status
    target.suspended_until = suspended_until
    target.status_reason = reason[:500]
    target.status_changed_at = now
    target.status_changed_by = actor_id
    target.status_version = (target.status_version or 0) + 1

    db.add(AccountModerationEvent(
        user_id=target.id,
        action=action,
        from_status=previous,
        to_status=new_status,
        suspended_until=suspended_until,
        reason=reason,
        notes=notes,
        evidence_refs=json.dumps(evidence_refs) if evidence_refs else None,
        actor_id=actor_id,
        request_id=request_id,
        created_at=now,
    ))
    detail = f"{previous} -> {new_status}"
    if suspended_until is not None:
        detail += f" until {suspended_until.isoformat()}Z"
    await record_audit(db, actor_id, f"account_{action}", "user", target.id, detail=detail)

    if new_status == "suspended":
        # Order matters: sessions first, so they record "suspended" rather than
        # the "revoked" the token revocation would otherwise give them.
        await activity.end_user_sessions(db, target.id, "suspended", now=now)
        await revoke_all_refresh_tokens(db, target.id)
        await push_devices.unregister_all(db, target.id)


async def expire_due_suspensions(*, now: Optional[datetime] = None) -> int:
    """Record the end of each temporary suspension whose time has come. Returns how many."""
    from api.database import AsyncSessionLocal, User

    now = now or _now()
    async with AsyncSessionLocal() as db:
        due = (await db.execute(
            select(User).where(
                User.account_status == "suspended",
                User.suspended_until.is_not(None),
                User.suspended_until <= now,
            )
        )).scalars().all()
        for user in due:
            await apply_change(
                db, user, actor_id="system", action="suspension_expired",
                new_status="active", suspended_until=None,
                reason="Temporary suspension ended", notes=None,
                evidence_refs=None, request_id=None, now=now,
            )
        await db.commit()
    for user in due:
        forget(user.id)
    return len(due)
