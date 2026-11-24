"""Admin reads of signed-in sessions and foreground activity (api/core/activity.py).

Aggregate figures need ADMIN_SUMMARY, the permission the Command Centre uses.
Per-account rows need MANAGE_USERS, the permission the user directory uses.
Every route here is read-only, and none returns contact details beyond what
the user directory already shows.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import activity
from api.core.permissions import Permission, require_permission
from api.core.revenue import _parse_range
from api.database import User, get_db

router = APIRouter()


def _range(from_: Optional[str], to: Optional[str]):
    try:
        return _parse_range(from_, to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/activity/live")
async def activity_live(
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Active accounts and sessions right now, and over the last 5, 15 and 60 minutes."""
    return await activity.live_snapshot(db)


@router.get("/activity/engagement")
async def activity_engagement(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """DAU, WAU and MAU, session durations and new versus returning accounts for a range."""
    start, end = _range(from_, to)
    return await activity.engagement_summary(db, start=start, end=end)


@router.get("/activity/retention")
async def activity_retention(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Day-1, day-7 and day-30 return rates, by the day each account registered."""
    start, end = _range(from_, to)
    if end - start > timedelta(days=activity.RETENTION_MAX_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"a retention range is at most {activity.RETENTION_MAX_DAYS} days",
        )
    return await activity.registration_retention(db, start=start, end=end)


@router.get("/activity/users")
async def activity_users(
    response: Response,
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    db: AsyncSession = Depends(get_db),
):
    """Accounts with their presence and 30-day activity. X-Total-Count is the filtered total."""
    total, rows = await activity.user_activity_table(
        db, search=search, limit=limit, offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


@router.get("/activity/users/{user_id}")
async def activity_user_profile(
    user_id: str,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    db: AsyncSession = Depends(get_db),
):
    """One account's sessions, totals and the last 30 days of activity."""
    profile = await activity.user_activity_profile(db, user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="User not found")
    return profile
