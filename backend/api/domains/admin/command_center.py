"""GET /admin/command-center - the Command Center's measured figures in one read.

Read-only. ADMIN_SUMMARY, the permission the Command Center already uses. The
figures, their periods and their definitions are in api/core/command_center.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import command_center
from api.core.permissions import Permission, require_permission
from api.database import User, get_db

router = APIRouter()


@router.get("/command-center")
async def admin_command_center(
    window_days: int = Query(7, ge=1, le=90),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Growth, marketplace, money, trust and operations for the period against
    the one before, the decisions they call for, and the marketplace pulse the
    page's other sections read."""
    return await command_center.command_center(db, window_days=window_days)
