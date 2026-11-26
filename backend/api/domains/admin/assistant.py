"""POST /admin/assistant/ask: a read-only answer from the verified analytics.

ADMIN_SUMMARY, like the other admin reads, and rate limited per administrator.
The question is not stored; the answer is computed for this request. The
intents and their rules are in api/core/assistant.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import assistant
from api.core.permissions import Permission, require_permission
from api.core.rate_limit import assistant_limiter
from api.database import User, get_db

router = APIRouter()


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=300)


@router.post("/ask")
async def ask(
    body: AskIn,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    db: AsyncSession = Depends(get_db),
):
    await assistant_limiter.check_and_record(admin.id)
    return await assistant.answer(db, body.question)
