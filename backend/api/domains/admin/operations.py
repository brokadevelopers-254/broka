"""GET /admin/operations - is BROKA working, and if not, where not.

Read-only. ADMIN_SUMMARY, like the other admin health reads. The figures,
their sources and what they cannot say are in api/core/operations.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import ops_anomalies, operations
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


@router.get("/operations/anomalies")
async def admin_operations_anomalies(
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    """Operations figures that moved outside their usual range on the last
    complete UTC day. Read-only: a flag describes a change and acts on nothing."""
    return await ops_anomalies.detect(db)
