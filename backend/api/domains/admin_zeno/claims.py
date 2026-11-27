"""Server-side validation of the figures a synthesized answer asserts.

A finding that cites a real evidence key can still state a wrong number, a
wrong unit, or a figure the model derived itself. This module closes that gap:

- every claim names an evidence key that was retrieved for this turn;
- the asserted value is compared with the metric's actual value (or with a
  derivation this backend knows how to compute: the previous value, the
  delta and the percentage change);
- the unit must match, and the claim's confidence may not be stronger than
  the evidence's confidence;
- free text (headline, summary, causes, recommendations, statements) may not
  contain a number that is not traceable to validated evidence.

Nothing here calls a model or the database, and error messages name metric
keys only - never customer data or prompt contents.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Sequence

from api.domains.admin_zeno.schemas import Claim, Evidence

# Stronger to weaker. A claim may be weaker than its evidence, never stronger.
CONFIDENCE_RANK = {
    "measured": 4,
    "derived": 3,
    "estimated": 2,
    "hypothesis": 1,
    "unavailable": 0,
}

DERIVATIONS = ("value", "previous_value", "delta", "pct_change")

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _close(actual: Any, asserted: Any, *, tolerance: float = 0.0) -> bool:
    if isinstance(actual, bool) or isinstance(asserted, bool):
        return isinstance(actual, bool) and isinstance(asserted, bool) and actual == asserted
    if not (_is_number(actual) and _is_number(asserted)):
        return False
    expected = float(actual)
    raw = float(asserted)
    slack = tolerance if tolerance else max(abs(expected) * 1e-9, 1e-9)
    return abs(raw - expected) <= slack


def expected_value(evidence: Evidence, derivation: str) -> Optional[Any]:
    """The value this backend can prove for a named derivation.

    ``None`` means the derivation is not possible for this evidence (for
    example a delta with no previous value). The model is never allowed to
    substitute its own arithmetic in that case.
    """
    if derivation == "value":
        return evidence.value
    if derivation == "previous_value":
        return evidence.previous_value
    current, previous = evidence.value, evidence.previous_value
    if not (_is_number(current) and _is_number(previous)):
        return None
    if derivation == "delta":
        return round(float(current) - float(previous), 2)
    if derivation == "pct_change":
        if float(previous) == 0:
            return None
        return round((float(current) - float(previous)) / float(previous) * 100, 1)
    return None


def claim_errors(
    *,
    claims: Sequence[Claim],
    finding_references: Iterable[str],
    evidence_by_key: dict[str, Evidence],
) -> list[str]:
    """Validate one finding's structured claims against retrieved evidence."""
    errors: list[str] = []
    cited = set(finding_references)
    for i, claim in enumerate(claims):
        prefix = f"claim[{i}]"
        if claim.derivation not in DERIVATIONS:
            errors.append(f"{prefix} uses an unsupported derivation")
            continue
        evidence = evidence_by_key.get(claim.evidence_reference)
        if evidence is None:
            # Either invented, or retrieved in a different investigation.
            errors.append(
                f"{prefix} references evidence that was not retrieved "
                f"({claim.evidence_reference})"
            )
            continue
        if claim.metric_key != evidence.metric_key:
            errors.append(f"{prefix} metric key does not match its evidence reference")
            continue
        if claim.evidence_reference not in cited:
            errors.append(f"{prefix} is not cited in the finding's evidence list")
        if not evidence.available or evidence.value is None:
            errors.append(
                f"{prefix} asserts a value for unavailable evidence ({claim.metric_key})"
            )
            continue
        if claim.unit != evidence.unit:
            errors.append(f"{prefix} unit does not match the evidence")
            continue
        rank_claim = CONFIDENCE_RANK.get(claim.confidence, 0)
        rank_evidence = CONFIDENCE_RANK.get(evidence.confidence, 0)
        if rank_claim > rank_evidence:
            errors.append(f"{prefix} confidence is stronger than the evidence supports")
        if evidence.confidence == "unavailable":
            errors.append(f"{prefix} asserts a value for unavailable evidence")
            continue
        expected = expected_value(evidence, claim.derivation)
        if expected is None:
            errors.append(f"{prefix} derivation is not possible for this evidence")
            continue
        tolerance = 0.05 if claim.derivation == "pct_change" else 0.0
        if not _close(expected, claim.asserted_value, tolerance=tolerance):
            # The metric key is safe to name; the values are not echoed.
            errors.append(f"{prefix} value does not match the evidence ({claim.metric_key})")
    return errors


def _norm(token: str) -> str:
    text = token.replace(",", "").lstrip("+-")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def numbers_in(text: Any) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {_norm(match) for match in _NUMBER.findall(text)}


def _number_tokens(value: Any, unit: str) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, bool):
        return {"1", "0"} if value else {"0"}
    if _is_number(value):
        number = float(value)
        for rendered in (
            f"{number:g}",
            f"{number:.1f}",
            f"{number:.2f}",
            f"{number:,.0f}",
            f"{abs(number):g}",
        ):
            tokens |= numbers_in(rendered)
        if unit in ("kes_minor", "kes"):
            minor = number / 100 if unit == "kes_minor" else number
            tokens |= numbers_in(f"{minor:,.0f}")
        return tokens
    return numbers_in(value) if isinstance(value, str) else set()


def allowed_numbers(
    *,
    evidence: Iterable[Evidence],
    claims: Iterable[Claim] = (),
    server_text: Iterable[str] = (),
    include_values: bool = True,
) -> set[str]:
    """Every number traceable to validated evidence or server-authored text.

    Free text may only repeat these figures. Labels and definitions are
    written by BROKA's own tools, so the period wording inside them
    ("within 24 hours") counts as supported.

    ``include_values=False`` withholds the raw metric values: used for a
    finding's own statement, where a figure must arrive through a structured
    claim rather than be lifted straight out of the cited evidence. There is
    no built-in allowance for small numbers - a number that is not proved is
    rejected however it is written.
    """
    allowed: set[str] = set()
    for item in evidence:
        if include_values:
            allowed |= _number_tokens(item.value, item.unit)
            allowed |= _number_tokens(item.previous_value, item.unit)
        allowed |= numbers_in(item.label)
        allowed |= numbers_in(item.definition)
        allowed |= numbers_in(item.unavailable_reason)
        if item.period.start and item.period.end:
            days = (item.period.end - item.period.start).days
            allowed |= numbers_in(str(max(days, 0)))
    for claim in claims:
        allowed |= _number_tokens(claim.asserted_value, claim.unit)
    for text in server_text:
        allowed |= numbers_in(text)
    return allowed


def unsupported_numbers(text: Any, allowed: set[str]) -> list[str]:
    """Numbers in free text that no validated evidence supports."""
    return sorted(numbers_in(text) - allowed)


def text_fields(payload: dict) -> list[tuple[str, str]]:
    """The answer-wide free-text fields, with a safe label.

    Finding statements are checked separately: a statement may only repeat
    figures from the evidence that finding itself cites, not any other
    metric retrieved in the same investigation. ``related_pages`` are not
    scanned for figures because a dashboard path is a route, not a metric;
    they are bounded and shape-checked by the orchestrator instead.
    """
    out: list[tuple[str, str]] = []
    for key in ("headline", "summary"):
        value = payload.get(key)
        if isinstance(value, str):
            out.append((key, value))
    for field_name in ("possible_causes", "recommendations"):
        for i, item in enumerate(payload.get(field_name) or []):
            if isinstance(item, dict):
                for key in ("statement", "action", "rationale"):
                    if isinstance(item.get(key), str):
                        out.append((f"{field_name}[{i}].{key}", item[key]))
    # Model-authored limitations are prose like any other: a figure the
    # evidence cannot prove is not made true by being phrased as a caveat.
    for i, item in enumerate(payload.get("limitations") or []):
        if isinstance(item, str):
            out.append((f"limitations[{i}]", item))
    for i, item in enumerate(payload.get("suggested_questions") or []):
        if isinstance(item, str):
            out.append((f"suggested_questions[{i}]", item))
    return out
