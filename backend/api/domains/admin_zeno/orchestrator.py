"""The Jarvis orchestration engine.

One turn:
    validate conversation ownership
    -> load bounded context
    -> model plans an investigation from the registered tools
    -> validate names/arguments and execute approved tools sequentially
    -> collect evidence and limitations
    -> model synthesizes a structured answer from that evidence
    -> validate every evidence reference
    -> persist the question and the answer
    -> return

The model never executes anything itself. Tool names come from a closed
registry, arguments are validated by the tool's schema, permissions are
checked server-side, and execution is capped and timed out. A synthesis that
fails validation gets one repair attempt and then a deterministic,
evidence-only fallback - never a second chance to invent an answer.

No chain-of-thought is requested, stored or returned. The ``investigation``
metadata names only the tools, provider, model and timing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import User
from api.domains.admin_zeno import conversation_service, prompts
from api.domains.admin_zeno.constants import (
    MAX_CONTEXT_CHARS_PER_TURN,
    MAX_CONTEXT_TURNS,
    MAX_EVIDENCE_FOR_MODEL,
    MAX_REPAIR_ATTEMPTS,
    MAX_TOOL_EXECUTIONS_PER_TURN,
    TOTAL_ORCHESTRATION_TIMEOUT_SECONDS,
)
from api.domains.admin_zeno.evidence import (
    dedupe,
    fallback_response,
    index_by_key,
    validate_references,
)
from api.domains.admin_zeno.model_gateway import (
    AdminModelGateway,
    GatewayReply,
    ModelUnavailable,
    extract_json,
    get_gateway,
)
from api.domains.admin_zeno.schemas import (
    Answer,
    ChatResponse,
    Evidence,
    Finding,
    Interpretation,
    InvestigationMeta,
    Recommendation,
)
from api.domains.admin_zeno.tool_registry import ToolRegistry, default_registry

log = logging.getLogger(__name__)


class ConversationNotFound(Exception):
    """The conversation does not exist or belongs to another administrator."""


class OrchestrationTimeout(Exception):
    pass


class ResponseValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:6]))
        self.errors = errors


class AdminZenoOrchestrator:
    def __init__(
        self,
        *,
        gateway: Optional[AdminModelGateway] = None,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.gateway = gateway or get_gateway()
        self.registry = registry or default_registry()

    async def run(
        self,
        db: AsyncSession,
        *,
        actor: User,
        message: str,
        conversation_id: Optional[str],
        context: Optional[dict[str, Any]] = None,
    ) -> ChatResponse:
        started = time.monotonic()
        is_new = conversation_id is None
        conversation = await self._conversation(db, actor, conversation_id, message)
        try:
            response = await asyncio.wait_for(
                self._run(db, actor=actor, message=message, conversation=conversation,
                          context=context or {}, started=started),
                timeout=TOTAL_ORCHESTRATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("[admin_zeno] orchestration timed out")
            await self._discard_new(db, conversation, is_new)
            raise OrchestrationTimeout(
                "The investigation exceeded its time limit. Try a narrower question."
            ) from None
        except ModelUnavailable:
            # A conversation with no turns is noise; only one created by this
            # failed turn is removed. An existing conversation is kept because
            # its earlier turns are still valid history.
            await self._discard_new(db, conversation, is_new)
            raise

        await conversation_service.append_turn(
            db,
            conversation_id=conversation.id,
            role="user",
            content=message,
            metadata={"context": context or {}},
        )
        await conversation_service.append_turn(
            db,
            conversation_id=conversation.id,
            role="assistant",
            content=f"{response.answer.headline}\n\n{response.answer.summary}",
            evidence=response.evidence,
            metadata={
                "investigation": response.investigation.model_dump(mode="json"),
                "limitations": response.limitations,
            },
        )
        if conversation.title in ("", "New investigation"):
            conversation.title = " ".join(message.split())[:120] or "Investigation"
            await db.commit()
        return response

    # ── Turn internals ────────────────────────────────────────────────────────

    async def _discard_new(self, db, conversation, is_new: bool) -> None:
        if not is_new or conversation is None:
            return
        try:
            await conversation_service.delete_owned(
                db, conversation_id=conversation.id, admin_user_id=conversation.admin_user_id
            )
        except Exception as exc:  # cleanup must not mask the original failure
            log.warning("[admin_zeno] conversation cleanup failed: %s", type(exc).__name__)

    async def _conversation(self, db, actor, conversation_id, message):
        if conversation_id:
            conversation = await conversation_service.get_owned(
                db, conversation_id=conversation_id, admin_user_id=actor.id
            )
            if conversation is None:
                # Same answer whether it is missing or someone else's.
                raise ConversationNotFound("Conversation not found")
            return conversation
        return await conversation_service.create_conversation(
            db, admin_user_id=actor.id,
            title=" ".join(message.split())[:120] or "New investigation",
        )

    async def _run(
        self,
        db: AsyncSession,
        *,
        actor: User,
        message: str,
        conversation,
        context: dict[str, Any],
        started: float,
    ) -> ChatResponse:
        history = await conversation_service.recent_turns(
            db, conversation_id=conversation.id, limit=MAX_CONTEXT_TURNS
        )
        # No transaction stays open across a model call (AGENTS.md): the pool
        # is five connections, and a model round trip is seconds to a minute.
        from api.database import release_connection
        await release_connection(db, before="admin_zeno.plan")

        plan, plan_reply, plan_repaired = await self._plan(
            message=message, history=history, actor=actor, context=context
        )
        action = plan.get("action")
        if action not in ("investigate", "clarify", "unsupported"):
            raise ModelUnavailable("The planner returned an unusable action.")

        if action == "clarify":
            return self._simple_response(
                conversation_id=conversation.id,
                headline="More detail needed",
                summary=str(plan.get("clarification") or plan.get("summary") or
                            "Please clarify the period or area you want investigated.")[:1200],
                limitations=["No data was retrieved because the question was ambiguous."],
                reply=plan_reply,
                synthesis="clarify",
                started=started,
            )

        if action == "unsupported":
            return self._simple_response(
                conversation_id=conversation.id,
                headline="This cannot be answered from BROKA's verified data",
                summary=str(plan.get("summary") or
                            "No registered intelligence tool covers this question yet.")[:1200],
                limitations=["No data was retrieved, so no figures are reported."],
                reply=plan_reply,
                synthesis="no_evidence",
                started=started,
            )

        requested = plan.get("tools") or []
        if not isinstance(requested, list):
            raise ModelUnavailable("The planner returned an unusable tool list.")

        limitations: list[str] = []
        if len(requested) > MAX_TOOL_EXECUTIONS_PER_TURN:
            requested = requested[:MAX_TOOL_EXECUTIONS_PER_TURN]
            limitations.append(
                f"Only the first {MAX_TOOL_EXECUTIONS_PER_TURN} requested tools were run."
            )

        evidence: list[Evidence] = []
        tool_data: list[dict[str, Any]] = []
        tools_used: list[str] = []
        tool_errors: list[str] = []
        for item in requested:
            if not isinstance(item, dict):
                tool_errors.append("invalid_tool_request")
                continue
            name = item.get("name")
            arguments = item.get("arguments") or {}
            if not isinstance(name, str):
                tool_errors.append("invalid_tool_request")
                continue
            execution = await self.registry.execute(
                name, arguments, db=db, actor=actor
            )
            tools_used.append(name)
            if not execution.ok:
                tool_errors.append(f"{name}:{execution.error_category}")
                limitations.append(
                    f"{name} did not return data ({execution.error_category})."
                )
                continue
            evidence.extend(execution.evidence)
            limitations.extend(execution.limitations)
            tool_data.append({"tool": name, "data": execution.data})

        evidence = dedupe(evidence)[:MAX_EVIDENCE_FOR_MODEL]
        if not evidence:
            return self._simple_response(
                conversation_id=conversation.id,
                headline="No verified evidence was retrieved",
                summary=(
                    "The investigation did not return usable evidence, so no figures are "
                    "reported. This is a data or tooling gap, not a zero result."
                ),
                limitations=(limitations + [
                    "Unavailable metrics are not shown as zero.",
                ])[:8],
                reply=plan_reply,
                synthesis="no_evidence",
                started=started,
                tool_errors=tool_errors,
                tools_used=tools_used,
            )

        await release_connection(db, before="admin_zeno.synthesis")
        answer = await self._synthesize(
            message=message,
            history=history,
            context=context,
            evidence=evidence,
            tool_data=tool_data,
            limitations=limitations,
            # One repair per turn in total: if planning already spent it, a
            # malformed synthesis falls straight to the evidence-only fallback.
            allow_repair=not plan_repaired,
        )
        if answer is None:
            response = fallback_response(
                conversation_id=conversation.id,
                evidence=evidence,
                limitations=limitations,
                reason=(
                    "The model's synthesized answer could not be validated, so only the "
                    "verified figures are reported."
                ),
            )
            response.limitations = response.limitations[:8]
            response.investigation = InvestigationMeta(
                tools_used=tools_used,
                tool_errors=tool_errors,
                provider=plan_reply.provider,
                model=plan_reply.model,
                latency_ms=int((time.monotonic() - started) * 1000),
                synthesis="fallback",
            )
            return response

        payload, synthesis_reply = answer
        try:
            response = self._build_response(
                conversation_id=conversation.id,
                payload=payload,
                evidence=evidence,
                limitations=limitations,
                tool_data=tool_data,
            )
        except ResponseValidationError:
            response = fallback_response(
                conversation_id=conversation.id,
                evidence=evidence,
                limitations=limitations,
                reason="The synthesized answer could not be assembled safely; only verified figures are reported.",
            )
            response.investigation = InvestigationMeta(
                tools_used=tools_used, tool_errors=tool_errors,
                provider=synthesis_reply.provider, model=synthesis_reply.model,
                latency_ms=int((time.monotonic() - started) * 1000), synthesis="fallback",
            )
            return response
        response.investigation = InvestigationMeta(
            tools_used=tools_used,
            tool_errors=tool_errors,
            provider=synthesis_reply.provider,
            model=synthesis_reply.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            synthesis="model",
        )
        return response

    async def _plan(self, *, message, history, actor, context):
        tools = self.registry.prompt_specs(actor)
        prompt = prompts.planning_prompt(
            question=message,
            tools=tools,
            context=context,
            history=history,
            max_tools=MAX_TOOL_EXECUTIONS_PER_TURN,
            max_context_turns=MAX_CONTEXT_TURNS,
            max_context_chars=MAX_CONTEXT_CHARS_PER_TURN,
        )
        reply = await self.gateway.complete(prompt, stage="planning", max_tokens=1600)
        try:
            plan = extract_json(reply.text)
            if not isinstance(plan, dict):
                raise ValueError("plan is not an object")
            return plan, reply, False
        except ValueError as exc:
            log.warning("[admin_zeno] planning returned invalid JSON: %s", type(exc).__name__)

        # One bounded repair attempt. It sees only its own output and the
        # error, never new data, so it cannot widen the investigation.
        repaired = await self._repair(
            previous=reply.text,
            errors=["the reply was not a single JSON object"],
            stage="planning_repair",
            max_tokens=1600,
        )
        try:
            plan = extract_json(repaired.text)
            if not isinstance(plan, dict):
                raise ValueError("plan is not an object")
            return plan, repaired, True
        except ValueError:
            raise ModelUnavailable("The planner did not return valid JSON.") from None

    async def _synthesize(
        self, *, message, history, context, evidence, tool_data, limitations,
        allow_repair: bool = True,
    ) -> Optional[tuple[dict, GatewayReply]]:
        payload = [
            e.model_dump(mode="json") for e in evidence
        ]
        prompt = prompts.synthesis_prompt(
            question=message,
            evidence=payload,
            tool_data=tool_data,
            limitations=limitations[:10],
            context=context,
            history=history,
            max_context_turns=MAX_CONTEXT_TURNS,
            max_context_chars=MAX_CONTEXT_CHARS_PER_TURN,
        )
        reply = await self.gateway.complete(prompt, stage="synthesis", max_tokens=2400)
        try:
            answer = extract_json(reply.text)
            self._validate_payload(answer, evidence)
            return answer, reply
        except (ValueError, ResponseValidationError) as exc:
            errors = exc.errors if isinstance(exc, ResponseValidationError) else [str(exc)]
            log.warning("[admin_zeno] synthesis rejected: %s", errors[:4])

        if MAX_REPAIR_ATTEMPTS < 1 or not allow_repair:
            return None
        repaired = await self._repair(
            previous=reply.text,
            errors=errors[:6],
            stage="synthesis_repair",
            max_tokens=2400,
        )
        try:
            answer = extract_json(repaired.text)
            self._validate_payload(answer, evidence)
            return answer, repaired
        except (ValueError, ResponseValidationError) as exc:
            errors = exc.errors if isinstance(exc, ResponseValidationError) else [str(exc)]
            log.warning("[admin_zeno] repaired synthesis rejected: %s", errors[:4])
            return None

    async def _repair(self, *, previous, errors, stage, max_tokens) -> GatewayReply:
        return await self.gateway.complete(
            prompts.repair_prompt(previous=previous, errors=errors),
            stage=stage,
            max_tokens=max_tokens,
        )

    # ── Validation and response assembly ──────────────────────────────────────

    def _validate_payload(self, payload: Any, evidence: list[Evidence]) -> None:
        if not isinstance(payload, dict):
            raise ResponseValidationError(["the answer is not an object"])
        by_key = index_by_key(evidence)
        errors: list[str] = []

        headline = payload.get("headline")
        summary = payload.get("summary")
        if not isinstance(headline, str) or not headline.strip():
            errors.append("headline is required")
        if not isinstance(summary, str) or not summary.strip():
            errors.append("summary is required")

        findings = payload.get("findings") or []
        if not isinstance(findings, list):
            errors.append("findings must be a list")
            findings = []
        if len(findings) > 6:
            errors.append("too many findings")
        for i, finding in enumerate(findings[:8]):
            if not isinstance(finding, dict):
                errors.append(f"findings[{i}] is not an object")
                continue
            refs = finding.get("evidence")
            if not isinstance(refs, list) or not refs:
                errors.append(f"findings[{i}] has no evidence reference")
                continue
            if any(not isinstance(r, str) for r in refs):
                errors.append(f"findings[{i}] has a non-string evidence reference")
                continue
            invalid = validate_references(refs, by_key)
            if invalid:
                errors.append(f"findings[{i}] cites unknown evidence: {', '.join(invalid[:3])}")

        for field in ("possible_causes", "recommendations", "limitations",
                      "related_pages", "suggested_questions"):
            value = payload.get(field)
            if value is not None and not isinstance(value, list):
                errors.append(f"{field} must be a list")

        if errors:
            raise ResponseValidationError(errors)

    def _build_response(
        self,
        *,
        conversation_id: str,
        payload: dict,
        evidence: list[Evidence],
        limitations: list[str],
        tool_data: list[dict[str, Any]],
    ) -> ChatResponse:
        try:
            findings = [
                Finding(statement=str(f.get("statement") or "")[:800],
                        evidence=[str(r) for r in (f.get("evidence") or [])][:12],
                        confidence=f.get("confidence") or "measured")
                for f in (payload.get("findings") or [])[:6]
                if isinstance(f, dict) and f.get("statement")
            ]
            causes = [
                Interpretation(statement=str(c.get("statement") or "")[:800])
                for c in (payload.get("possible_causes") or [])[:4]
                if isinstance(c, dict) and c.get("statement")
            ]
            recommendations = [
                Recommendation(
                    action=str(r.get("action") or "")[:600],
                    rationale=str(r.get("rationale") or "")[:800],
                    page=self._safe_page(r.get("page")),
                )
                for r in (payload.get("recommendations") or [])[:4]
                if isinstance(r, dict) and r.get("action")
            ]
        except ValidationError as exc:
            raise ResponseValidationError(
                [f"{err['loc']}: {err['msg']}" for err in exc.errors()[:4]]
            ) from None

        model_limitations = [
            str(x)[:800] for x in (payload.get("limitations") or []) if isinstance(x, str)
        ]
        all_limitations = (model_limitations + limitations)[:6]
        unavailable = [
            f"{e.label}: {e.unavailable_reason}" for e in evidence if not e.available
        ]
        for note in unavailable:
            if note not in all_limitations and len(all_limitations) < 6:
                all_limitations.append(note)

        related = []
        for page in list(payload.get("related_pages") or [])[:6]:
            safe = self._safe_page(page)
            if safe and safe not in related:
                related.append(safe)
        if not related:
            for e in evidence:
                if e.href and e.href.startswith("/") and e.href not in related:
                    related.append(e.href)
                if len(related) >= 4:
                    break
        del tool_data  # supporting context is never copied into the response

        return ChatResponse(
            conversation_id=conversation_id,
            answer=Answer(
                headline=str(payload.get("headline") or "")[:300] or "Investigation complete",
                summary=str(payload.get("summary") or "")[:4000],
            ),
            findings=findings,
            possible_causes=causes,
            recommendations=recommendations,
            evidence=evidence,
            limitations=all_limitations,
            related_pages=related,
            suggested_questions=[
                str(q)[:200] for q in (payload.get("suggested_questions") or [])[:4]
                if isinstance(q, str)
            ],
            generated_at=datetime.utcnow(),
        )

    @staticmethod
    def _safe_page(page: Any) -> Optional[str]:
        """Dashboard paths only: one leading slash, no scheme, bounded."""
        if not isinstance(page, str):
            return None
        page = page.strip()
        if not page.startswith("/") or page.startswith("//") or len(page) > 240:
            return None
        if "://" in page or " " in page:
            return None
        return page

    def _simple_response(
        self,
        *,
        conversation_id: str,
        headline: str,
        summary: str,
        limitations: list[str],
        reply: GatewayReply,
        synthesis: str,
        started: float,
        tool_errors: Optional[list[str]] = None,
        tools_used: Optional[list[str]] = None,
    ) -> ChatResponse:
        return ChatResponse(
            conversation_id=conversation_id,
            answer=Answer(headline=headline[:300], summary=summary[:4000]),
            findings=[],
            possible_causes=[],
            recommendations=[],
            evidence=[],
            limitations=limitations[:8],
            related_pages=[],
            suggested_questions=[],
            generated_at=datetime.utcnow(),
            investigation=InvestigationMeta(
                tools_used=tools_used or [],
                tool_errors=tool_errors or [],
                provider=reply.provider,
                model=reply.model,
                latency_ms=int((time.monotonic() - started) * 1000),
                synthesis=synthesis,  # type: ignore[arg-type]
            ),
        )


_orchestrator: Optional[AdminZenoOrchestrator] = None


def get_orchestrator() -> AdminZenoOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AdminZenoOrchestrator()
    return _orchestrator
