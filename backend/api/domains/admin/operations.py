"""GET /admin/operations - is BROKA working, and if not, where not.

Read-only. ADMIN_SUMMARY, like the other admin health reads. The figures,
their sources and what they cannot say are in api/core/operations.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import operations
from api.core.permissions import Permission, require_permission
from api.database import User, get_db

router = APIRouter()


@router.get("/operations")
async def admin_operations(
    window_hours: int = Query(24, ge=1, le=operations.MAX_WINDOW_HOURS),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """API, database, payments, notifications, calls and background work for
    the window against the one before."""
    return await operations.operations_report(db, window_hours=window_hours)
