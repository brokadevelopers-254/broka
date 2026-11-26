"""Evidence construction, inspection and validation.

Rules enforced here, not in a prompt:

- Every quantitative statement must name evidence that was actually
  retrieved for this turn. ``validate_references`` rejects anything else.
- A metric that cannot be measured travels with ``available=False`` and a
  reason - never as zero.
- A response that fails validation is not passed through; the orchestrator
  either asks the model to repair it once or uses the deterministic fallback
  built here from the real evidence.

Nothing in this module calls a model or the database.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Optional

from api.domains.admin_zeno.schemas import (
    Answer,
    ChatResponse,
    Evidence,
    Finding,
    Period,
)

_SAFE_KEY = re.compile(r"[^a-z0-9_.-]+")


def make_evidence(
    *,
    metric_key: str,
    label: str,
    value: Any,
    source: str,
    definition: str = "",
    unit: str = "count",
    previous_value: Any = None,
    period_start: Optional[datetime] = None,
    period_end: Optional[datetime] = None,
    confidence: str = "measured",
    available: bool = True,
    unavailable_reason: Optional[str] = None,
    href: Optional[str] = None,
) -> Evidence:
    """Build one evidence object with a stable, URL-safe id."""
    key = _SAFE_KEY.sub("_", metric_key.strip().lower())[:80] or "metric"
    return Evidence(
        id=f"ev.{key}",
        metric_key=metric_key,
        label=label,
        value=value if available else None,
        unit=unit,
        previous_value=previous_value if available else None,
        period=Period(start=period_start, end=period_end),
        source=source,
        definition=definition,
        generated_at=datetime.utcnow(),
        available=available,
        unavailable_reason=None if available else (unavailable_reason or "Not measured."),
        confidence="unavailable" if not available else confidence,  # type: ignore[arg-type]
        href=href,
    )


def unavailable_evidence(
    *,
    metric_key: str,
    label: str,
    source: str,
    reason: str,
    definition: str = "",
    unit: str = "count",
) -> Evidence:
    return make_evidence(
        metric_key=metric_key,
        label=label,
        value=None,
        source=source,
        definition=definition,
        unit=unit,
        available=False,
        unavailable_reason=reason,
    )


def dedupe(evidence: Iterable[Evidence]) -> list[Evidence]:
    """One entry per id, first wins. Keeps the prompt and response bounded."""
    seen: set[str] = set()
    out: list[Evidence] = []
    for item in evidence:
        base = item.id
        candidate, n = base, 2
        while candidate in seen:
            candidate = f"{base}.{n}"
            n += 1
        seen.add(candidate)
        out.append(item.model_copy(update={"id": candidate}))
    return out


def index_by_key(evidence: Iterable[Evidence]) -> dict[str, Evidence]:
    return {e.metric_key: e for e in evidence}


def validate_references(
    refs: Iterable[str], evidence_by_key: dict[str, Evidence]
) -> list[str]:
    """Return the references that do not correspond to supplied evidence."""
    return sorted({ref for ref in refs if ref not in evidence_by_key})


def _fmt(value: Any, unit: str) -> str:
    if value is None:
        return "not measured"
    if unit == "percentage":
        return f"{float(value):g}%"
    if unit in ("kes_minor", "kes"):
        return f"KES {float(value) / 100:,.0f}" if unit == "kes_minor" else f"KES {float(value):,.0f}"
    if unit == "ms":
        return f"{round(float(value)):,} ms"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.1f}" if not value.is_integer() else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def format_available(evidence: Iterable[Evidence]) -> list[str]:
    """Plain lines for the deterministic fallback. No interpretation."""
    lines = []
    for e in evidence:
        if e.available:
            previous = (
                f" (previously {_fmt(e.previous_value, e.unit)})"
                if e.previous_value is not None
                else ""
            )
            lines.append(f"{e.label}: {_fmt(e.value, e.unit)}{previous}.")
        else:
            lines.append(f"{e.label}: not measured. {e.unavailable_reason}")
    return lines


def fallback_response(
    *,
    conversation_id: str,
    evidence: list[Evidence],
    limitations: list[str],
    reason: str,
) -> ChatResponse:
    """A safe, evidence-only answer used when synthesis fails validation.

    It states what was measured and refuses to interpret beyond that. It is
    intentionally plain: a wrong, confident synthesis is worse than an
    honest list of verified figures.
    """
    lines = format_available(evidence)
    summary = " ".join(lines[:12]) if lines else "No verified figures were retrieved."
    reasons = [reason, *limitations]
    findings = [
        Finding(statement=f"{e.label} was {_fmt(e.value, e.unit)}.", evidence=[e.metric_key])
        for e in evidence
        if e.available and e.value is not None
    ][:10]
    return ChatResponse(
        conversation_id=conversation_id,
        answer=Answer(
            headline="Verified figures retrieved; interpretation unavailable",
            summary=summary[:4000],
        ),
        findings=findings,
        possible_causes=[],
        recommendations=[],
        evidence=evidence,
        limitations=[r for r in reasons if r][:8],
        related_pages=[],
        suggested_questions=[],
        generated_at=datetime.utcnow(),
    )
