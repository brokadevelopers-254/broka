"""The /admin/zeno API.

Every route requires an authenticated administrator with ADMIN_SUMMARY (the
per-tool permission is checked again inside the registry). Jarvis is off by
default in production: when ADMIN_ZENO_ENABLED is false every route answers
503 ADMIN_ZENO_DISABLED and the rest of the admin API is unaffected.

No route accepts an administrator id from the client; the actor is the
signed-in user. Conversation ownership is verified on every read and delete.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.permissions import Permission, require_permission
from api.core.rate_limit import admin_zeno_limiter
from api.database import User, get_db
from api.domains.admin_zeno import briefing_service, conversation_service
from api.domains.admin_zeno.constants import is_enabled
from api.domains.admin_zeno.model_gateway import ModelUnavailable, get_gateway
from api.domains.admin_zeno.orchestrator import (
    ConversationNotFound,
    OrchestrationTimeout,
    get_orchestrator,
)
from api.domains.admin_zeno.sanitizer import UnsafeModelContext
from api.domains.admin_zeno.schemas import (
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    StatusResponse,
    TurnOut,
    BriefingResponse,
)
from api.domains.admin_zeno.tool_registry import default_registry

log = logging.getLogger(__name__)
router = APIRouter()


async def require_enabled() -> None:
    """The ADMIN_ZENO_ENABLED switch, as its own dependency.

    Kept separate from the permission dependency so the generated repository
    map (and any reader) sees the real `require_permission` guard on each
    route rather than a wrapper that looks public. Each signature declares
    the permission dependency first, so a non-administrator is refused on
    identity before the deployment's feature state is revealed.
    """
    if not is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ADMIN_ZENO_DISABLED",
                "message": "Administrative Zeno is not enabled on this deployment.",
            },
        )


def _summary(row, turn_count: int = 0) -> ConversationSummary:
    return ConversationSummary(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        turn_count=turn_count,
    )


@router.get("/status", response_model=StatusResponse)
async def zeno_status(
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    _enabled: None = Depends(require_enabled),
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """Safe readiness: no keys, environment values or provider payloads."""
    gateway = get_gateway()
    registry = default_registry()
    return StatusResponse(
        enabled=True,
        model_gateway_ready=gateway.ready,
        providers_configured=gateway.providers_configured(),
        tool_registry_ready=bool(registry.names()),
        tool_count=len(registry.names()),
        last_error_category=gateway.last_error_category,
        generated_at=datetime.utcnow(),
    )


@router.get("/briefing", response_model=BriefingResponse)
async def zeno_briefing(
    window_days: int = Query(default=7, ge=1, le=30),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    _enabled: None = Depends(require_enabled),
    db: AsyncSession = Depends(get_db),
) -> BriefingResponse:
    """The deterministic intelligence briefing. No model call is required."""
    return await briefing_service.build_briefing(db, window_days=window_days)


@router.post("/chat", response_model=ChatResponse)
async def zeno_chat(
    body: ChatRequest,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    _enabled: None = Depends(require_enabled),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    # Keyed on the signed-in administrator, never on anything in the body. A
    # separate bucket: Jarvis usage must not eat a customer's Zeno allowance.
    await admin_zeno_limiter.check_and_record(admin.id)
    try:
        return await get_orchestrator().run(
            db,
            actor=admin,
            message=body.message,
            conversation_id=body.conversation_id,
            context=body.context.model_dump() if body.context else {},
        )
    except ConversationNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        ) from None
    except UnsafeModelContext:
        # A controlled refusal: the question or its context could not be made
        # safe to send, so nothing was sent. The response names no content and
        # no provider detail.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "MODEL_CONTEXT_UNSAFE",
                "message": (
                    "This question could not be prepared safely for the model. "
                    "Remove personal details or ask about aggregate figures."
                ),
            },
        ) from None
    except ModelUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "MODEL_UNAVAILABLE",
                "message": "The intelligence model is unavailable. No answer was generated.",
            },
        ) from None
    except OrchestrationTimeout:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "code": "INVESTIGATION_TIMEOUT",
                "message": "The investigation took too long. Try a narrower question.",
            },
        ) from None


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10000),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    _enabled: None = Depends(require_enabled),
    db: AsyncSession = Depends(get_db),
) -> ConversationList:
    rows, total = await conversation_service.list_conversations(
        db, admin_user_id=admin.id, limit=limit, offset=offset
    )
    counts = await conversation_service.turn_counts(db, [r.id for r in rows])
    return ConversationList(
        conversations=[_summary(r, counts.get(r.id, 0)) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    limit: int = Query(default=40, ge=1, le=40),
    offset: int = Query(default=0, ge=0, le=10000),
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    _enabled: None = Depends(require_enabled),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    row = await conversation_service.get_owned(
        db, conversation_id=conversation_id, admin_user_id=admin.id
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    turns = await conversation_service.list_turns(
        db, conversation_id=row.id, limit=limit, offset=offset
    )
    evidence = []
    for turn in turns:
        evidence.extend(_turn_evidence(turn))
    return ConversationDetail(
        conversation=_summary(row, len(turns)),
        turns=[
            TurnOut(
                id=t.id,
                role="user" if t.role == "user" else "assistant",
                content=t.content,
                evidence=_turn_evidence(t),
                metadata=t.metadata_json or {},
                created_at=t.created_at,
            )
            for t in turns
        ],
        limit=limit,
        offset=offset,
    )


def _turn_evidence(turn) -> list:
    from api.domains.admin_zeno.schemas import Evidence

    out = []
    for raw in (turn.evidence_json or []):
        try:
            out.append(Evidence.model_validate(raw))
        except Exception:
            # A malformed stored row is skipped, not reflected into the API.
            continue
    return out


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    admin: User = Depends(require_permission(Permission.ADMIN_SUMMARY)),
    _enabled: None = Depends(require_enabled),
    db: AsyncSession = Depends(get_db),
) -> dict:
    deleted = await conversation_service.delete_owned(
        db, conversation_id=conversation_id, admin_user_id=admin.id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return {"deleted": True, "id": conversation_id}
