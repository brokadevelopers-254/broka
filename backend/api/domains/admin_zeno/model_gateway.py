"""The administrative model adapter.

Reuses BROKA's existing provider chain, circuit breakers, model settings and
cost ledger - it does not introduce a second AI-provider implementation or new
keys. What it does not reuse is the customer-facing persona: every call here
is built from ``prompts.ADMIN_SYSTEM_PROMPT`` and returns structured JSON for
an administrative investigation.

Behaviour:
- Gemini -> DeepSeek -> OpenRouter -> Groq, skipping any provider with no key
  or an open circuit. The first usable reply wins.
- Each call is bounded by a timeout and the whole stage by an outer timeout.
- A successful reply records ``record_ai_cost`` with
  ``call_site="admin_zeno.chat"``, so administrative AI spend is identifiable
  separately from customer-facing Zeno.
- Failures are reported as :class:`ModelUnavailable`; no retry loop, no cached
  customer answer and never a fabricated reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import httpx

from api.core import gemini
from api.core.circuit_breaker import (
    CircuitOpenError,
    deepseek_breaker,
    gemini_breaker,
    groq_breaker,
    openrouter_breaker,
)
from api.core.config import settings
from api.core.cost_ledger import record_ai_cost
from api.domains.admin_zeno.constants import (
    MAX_MODEL_OUTPUT_CHARS,
    MODEL_CALL_TIMEOUT_SECONDS,
)
from api.domains.admin_zeno.sanitizer import UnsafeModelContext, outbound_violation

log = logging.getLogger(__name__)

CALL_SITE = "admin_zeno.chat"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _strings_in(value: Any) -> Iterator[str]:
    """Every string in a provider payload, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item)


def assert_payload_is_safe(payload: Any, *, stage: str) -> None:
    """The final boundary, at the transport rather than at the caller.

    Sanitization happens before a prompt is assembled; this check runs on the
    serialized payload each provider function is about to POST. A future call
    site that builds a prompt itself, or a tool that widens what it returns,
    therefore fails closed instead of reaching a provider. Only the category
    is ever named - never the value, the prompt or the payload.
    """
    for text in _strings_in(payload):
        category = outbound_violation(text)
        if category:
            log.warning(
                "[admin_zeno] outbound payload refused stage=%s category=%s",
                stage, category,
            )
            raise UnsafeModelContext(
                f"The model request was refused before transport ({category})."
            )


class ModelUnavailable(Exception):
    """No configured provider produced a usable reply."""


@dataclass
class GatewayReply:
    text: str
    provider: str
    model: str
    latency_ms: int


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply.

    Models occasionally wrap JSON in fences or add a sentence before it. This
    accepts that, but never repairs malformed JSON and never executes it.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(raw[start:end + 1])


class AdminModelGateway:
    def __init__(self) -> None:
        self.gemini_key = settings.gemini_api_key
        self.deepseek_key = settings.deepseek_api_key
        self.openrouter_key = settings.openrouter_api_key
        self.groq_key = settings.groq_api_key
        # The category of the last failure, for /status. Never a payload or key.
        self.last_error_category: Optional[str] = None

    # ── Readiness (safe surface) ──────────────────────────────────────────────

    def providers_configured(self) -> list[str]:
        out = []
        if self.gemini_key:
            out.append("gemini")
        if self.deepseek_key:
            out.append("deepseek")
        if self.openrouter_key:
            out.append("openrouter")
        if self.groq_key:
            out.append("groq")
        return out

    @property
    def ready(self) -> bool:
        """Configured, not necessarily healthy - /status says exactly that."""
        return bool(self.providers_configured())

    # ── Public call ───────────────────────────────────────────────────────────

    async def complete(self, prompt: str, *, stage: str, max_tokens: int = 1800) -> GatewayReply:
        """One administrative model call, with provider fallback and timing."""
        started = time.monotonic()
        attempted: list[str] = []
        errors: list[str] = []

        # Checked here as well as inside each provider function: a stage that
        # assembles a prompt itself cannot reach the transport with identity
        # or a credential in it.
        assert_payload_is_safe({"prompt": prompt}, stage=stage)

        chain = (
            ("gemini", self.gemini_key, gemini_breaker, self._call_gemini),
            ("deepseek", self.deepseek_key, deepseek_breaker, self._call_deepseek),
            ("openrouter", self.openrouter_key, openrouter_breaker, self._call_openrouter),
            ("groq", self.groq_key, groq_breaker, self._call_groq),
        )
        for name, key, breaker, fn in chain:
            if not key:
                continue
            attempted.append(name)
            try:
                text = await breaker.call(
                    self._bounded, fn, prompt, max_tokens
                )
            except CircuitOpenError:
                log.warning("[admin_zeno] provider=%s circuit open stage=%s", name, stage)
                continue
            except Exception as exc:
                # Type only: a provider body may contain sensitive request text.
                log.warning(
                    "[admin_zeno] provider=%s stage=%s failed error=%s",
                    name, stage, type(exc).__name__,
                )
                errors.append(f"{name}:{type(exc).__name__}")
                continue
            if not text or not text.strip():
                errors.append(f"{name}:empty")
                continue

            text = text[:MAX_MODEL_OUTPUT_CHARS]
            self.last_error_category = None
            return GatewayReply(
                text=text,
                provider=name,
                model=self._model_for(name),
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        self.last_error_category = "providers_unavailable"
        log.warning(
            "[admin_zeno] no provider answered stage=%s attempted=%s errors=%s",
            stage, attempted, errors,
        )
        raise ModelUnavailable(
            "No administrative model provider produced a usable response."
        )

    # ── Provider plumbing ─────────────────────────────────────────────────────

    @staticmethod
    def _model_for(provider: str) -> str:
        return {
            "gemini": settings.gemini_model,
            "deepseek": settings.deepseek_model,
            "openrouter": settings.openrouter_model,
            "groq": settings.groq_model,
        }[provider]

    @staticmethod
    async def _bounded(fn, prompt: str, max_tokens: int) -> str:
        """Provider call + stage timeout. A hung provider cannot hold the turn."""
        try:
            return await asyncio.wait_for(
                fn(prompt, max_tokens), timeout=MODEL_CALL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            raise TimeoutError("provider timed out") from None

    async def _call_gemini(self, prompt: str, max_tokens: int) -> str:
        url = gemini.endpoint()
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": gemini.output_budget(max_tokens),
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }
        assert_payload_is_safe(payload, stage="gemini")
        async with httpx.AsyncClient(timeout=MODEL_CALL_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{url}?key={self.gemini_key}", json=payload
            )
        response.raise_for_status()
        data = response.json()
        await record_ai_cost(
            provider="gemini", response_data=data,
            model=settings.gemini_model, call_site=CALL_SITE,
        )
        return gemini.reply_text(data)

    async def _call_deepseek(self, prompt: str, max_tokens: int) -> str:
        url = f"{settings.deepseek_base_url}/chat/completions"
        payload = {
            "model": settings.deepseek_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.deepseek_key}",
            "Content-Type": "application/json",
        }
        assert_payload_is_safe(payload, stage="deepseek")
        async with httpx.AsyncClient(timeout=settings.deepseek_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        await record_ai_cost(
            provider="deepseek", response_data=data,
            model=settings.deepseek_model, call_site=CALL_SITE,
        )
        return data["choices"][0]["message"]["content"]

    async def _call_openrouter(self, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": settings.openrouter_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://broka.co.ke",
            "X-Title": "BROKA Admin Zeno",
        }
        assert_payload_is_safe(payload, stage="openrouter")
        async with httpx.AsyncClient(timeout=MODEL_CALL_TIMEOUT_SECONDS) as client:
            response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        await record_ai_cost(
            provider="openrouter", response_data=data,
            model=settings.openrouter_model, call_site=CALL_SITE,
        )
        return data["choices"][0]["message"]["content"]

    async def _call_groq(self, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": settings.groq_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.groq_key}",
            "Content-Type": "application/json",
        }
        assert_payload_is_safe(payload, stage="groq")
        async with httpx.AsyncClient(timeout=MODEL_CALL_TIMEOUT_SECONDS) as client:
            response = await client.post(GROQ_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        await record_ai_cost(
            provider="groq", response_data=data,
            model=settings.groq_model, call_site=CALL_SITE,
        )
        return data["choices"][0]["message"]["content"]


_gateway: Optional[AdminModelGateway] = None


def get_gateway() -> AdminModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = AdminModelGateway()
    return _gateway
