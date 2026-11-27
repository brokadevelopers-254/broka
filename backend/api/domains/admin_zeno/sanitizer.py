"""The model-context data boundary.

Everything Jarvis sends to a model that is not server-authored passes through
here: the stored conversation history, the evidence objects and the
supplementary tool payloads, *after* they have been serialized to plain data. This is deliberately not a per-tool check - a new tool cannot
widen what reaches a provider by forgetting to redact in its own module.

Two layers:

1. An explicit allowlist of the supplementary ``data`` fields each registered
   tool may expose to a model (``SAFE_TOOL_DATA_FIELDS``). A tool with no
   entry exposes no supplementary data at all; a test fails if a registered
   tool is missing from the map, so a new tool is reviewed rather than
   silently trusted.
2. Defence in depth on every nested key and string: identifiers, names,
   contact details, credentials and message content are removed, as are
   values that look like UUIDs, e-mails, phone numbers or long opaque ids.
   Lists and dictionaries are bounded so one payload cannot blow the prompt.

Only the *count* of removed fields is ever reported. Contents are never
logged, echoed into an answer or surfaced in an error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from api.domains.admin_zeno.schemas import Evidence

MAX_DATA_DEPTH = 4
MAX_DICT_KEYS = 40
MAX_LIST_ITEMS = 25
MAX_STRING_CHARS = 300

# Conversation history is re-sent to a provider on every turn, so it is the
# one place where a *stored* string can leak after the data boundary that
# produced the original answer has moved on. Legacy turns were written before
# this boundary existed and are not trusted.
MAX_HISTORY_TURNS = 12
MAX_HISTORY_CHARS = 1200

# Supplemental tool data a model may see, per registered tool. Aggregate and
# non-identifying fields only. Everything else is dropped before prompting.
SAFE_TOOL_DATA_FIELDS: dict[str, tuple[str, ...]] = {
    "business_overview": ("window_days", "decisions", "decisions_count",
                          "activity_coverage_started_on"),
    "founder_insights": ("insights", "repeat_reported_accounts", "max_complaints_90d"),
    "marketplace_liquidity": ("current", "previous", "target_pct"),
    "seller_responsiveness": ("current", "previous", "target_pct"),
    "business_targets": ("targets", "break_even"),
    "marketplace_demand": ("categories", "unmet_categories", "definitions"),
    "listing_rankings": ("count", "listings", "definitions"),
    "marketplace_performance": ("sellers_total", "buyers_total",
                                "browsers_without_contact", "buyer_funnel",
                                "top_sellers"),
    "marketplace_anomalies": ("evaluated_day", "anomalies", "checked",
                              "not_instrumented"),
    "marketplace_forecasts": ("forecasts",),
    "operations_overview": ("window", "coverage", "not_instrumented", "live"),
    "operational_anomalies": ("evaluated_day", "anomalies", "not_covered",
                              "covered_elsewhere"),
    "platform_health": ("coverage", "live_pool", "process_requests"),
    "platform_integrations": ("configured",),
    "platform_revenue": ("income", "period"),
    "platform_costs": ("costs", "notes", "categories"),
    "break_even": ("break_even",),
    "trust_overview": ("by_category", "open_by_severity",
                       "median_response_hours_90d", "repeat_reported_accounts",
                       "max_complaints_90d", "sla_hours", "note"),
    "growth_registrations": ("metrics", "retention", "coverage_started_on"),
    "growth_engagement": ("engagement",),
    "growth_cohorts": ("cohorts", "definitions"),
    "growth_funnel": ("funnel", "definitions"),
    "zeno_usage": ("customer_ai", "admin_ai", "unpriced_calls_total", "note"),
}

# Exact key names that carry identity, contact details, credentials or
# message content. Applied at every depth.
FORBIDDEN_KEYS = {
    "id", "name", "full_name", "first_name", "last_name", "display_name",
    "business_display_name", "username", "email", "phone", "phone_number",
    "msisdn", "password", "password_hash", "token", "access_token",
    "refresh_token", "secret", "api_key", "address", "location",
    "content", "message", "messages", "body", "transcript", "recording",
    "call_contents", "session_id", "device_token", "otp", "pin",
    "related_user_id", "admin_user_id",
}

_FORBIDDEN_SUBSTRINGS = ("email", "phone", "password", "token", "secret",
                         "credential", "msisdn", "apikey", "api_key")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_HEX_ID = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s()-]{7,}\d(?![\w.])")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ].*)?$")
_PEM = re.compile(r"-----BEGIN [A-Z ]+-----")

# Free text in a stored turn has no keys to inspect, so identity has to be
# recognised by its shape: a labelled value ("name: Jane"), a name written
# after the role it belongs to ("seller Jane Wanjiru"), a credential
# assignment, an e-mail, an id or a phone number.
_LABELED_PII = re.compile(
    r"\b(full name|name|customer|seller|buyer|account holder|phone(?:\s*number)?|"
    r"e-?mail|id|account)\s*[:=]\s*[^\s,;]+(?:\s+[A-Z][a-z]+)?",
    re.IGNORECASE,
)
_NAMED_ACCOUNT = re.compile(
    r"\b(seller|buyer|customer|account|user|administrator)\s+"
    r"(?:named\s+|called\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+"
)
_CREDENTIAL = re.compile(
    r"\bbearer\s+[A-Za-z0-9._\-]{8,}"
    r"|\b(password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|"
    r"otp|pin)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


class _Drop:
    """Marks a value removed for privacy reasons."""


DROP = _Drop()


def unsafe_text(value: str) -> bool:
    """Whether a string looks like it carries identity or a secret."""
    if _EMAIL.search(value) or _UUID.search(value) or _LONG_HEX_ID.search(value):
        return True
    if _PEM.search(value):
        return True
    if _PHONE.search(value) and not _ISO_DATE.match(value.strip()):
        return True
    return False


def unsafe_key(key: Any) -> bool:
    if not isinstance(key, str) or not key:
        return True
    lowered = key.lower()
    if lowered in FORBIDDEN_KEYS or lowered.endswith("_id"):
        return True
    if any(part in lowered for part in _FORBIDDEN_SUBSTRINGS):
        return True
    return bool(_EMAIL.search(key) or _UUID.search(key) or _PHONE.search(key))


def scrub_free_text(value: str) -> tuple[str, int]:
    """Redact identity-shaped tokens in free text, in place.

    A stored turn cannot be dropped just because it mentions a phone number -
    it is often the context that makes the next question intelligible - so the
    identifying token is replaced and the rest of the sentence kept. Returns
    the scrubbed text and how many redactions were made; the contents are
    never logged.
    """
    cleaned = value
    removed = 0
    for pattern, replacement in (
        (_CREDENTIAL, "[credential]"),
        (_PEM, "[credential]"),
        (_EMAIL, "[email]"),
        (_UUID, "[id]"),
        (_LONG_HEX_ID, "[id]"),
        (_PHONE, "[number]"),
    ):
        cleaned, count = pattern.subn(replacement, cleaned)
        removed += count
    cleaned, count = _LABELED_PII.subn(
        lambda match: f"{match.group(1)}: [redacted]", cleaned
    )
    removed += count
    cleaned, count = _NAMED_ACCOUNT.subn(
        lambda match: f"{match.group(1)} [name]", cleaned
    )
    removed += count
    return cleaned, removed


def sanitize_history(
    turns: Iterable[dict[str, Any]],
    *,
    limit: int = MAX_HISTORY_TURNS,
    chars: int = MAX_HISTORY_CHARS,
) -> tuple[list[dict[str, Any]], int]:
    """The only path from stored conversation turns into a prompt.

    Planning, synthesis and repair all receive this list and nothing else, so
    an unsafe legacy turn cannot reach a provider through one stage while
    being withheld from another. Roles are re-derived here rather than copied,
    content is scrubbed and bounded, and a turn that cannot be proven safe is
    dropped whole.
    """
    out: list[dict[str, Any]] = []
    removed = 0
    for turn in list(turns)[-max(1, limit):]:
        if not isinstance(turn, dict):
            removed += 1
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            removed += 1
            continue
        cleaned, redactions = scrub_free_text(content)
        removed += redactions
        if not cleaned.strip():
            removed += 1
            continue
        out.append({"role": role, "content": cleaned[:chars]})
    return out, removed


@dataclass
class SanitizedContext:
    evidence: list[Evidence] = field(default_factory=list)
    tool_data: list[dict[str, Any]] = field(default_factory=list)
    removed: int = 0


def _sanitize(value: Any, depth: int) -> tuple[Any, int]:
    """Return the cleaned value and how many fields were removed."""
    if depth > MAX_DATA_DEPTH:
        return DROP, 1
    if value is None or isinstance(value, (bool, int, float)):
        return value, 0
    if isinstance(value, str):
        if unsafe_text(value):
            return DROP, 1
        return value[:MAX_STRING_CHARS], 0
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        removed = 0
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DICT_KEYS or unsafe_key(key):
                removed += 1
                continue
            cleaned, dropped = _sanitize(item, depth + 1)
            removed += dropped
            if cleaned is not DROP:
                out[str(key)] = cleaned
        return out, removed
    if isinstance(value, (list, tuple)):
        out_list = []
        removed = max(0, len(value) - MAX_LIST_ITEMS)
        for item in list(value)[:MAX_LIST_ITEMS]:
            cleaned, dropped = _sanitize(item, depth + 1)
            removed += dropped
            if cleaned is not DROP:
                out_list.append(cleaned)
        return out_list, removed
    # Anything else (datetime, Decimal, model objects) is not model-facing.
    return DROP, 1


def _sanitize_string_field(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if unsafe_text(value):
        return None
    return value[:MAX_STRING_CHARS]


def sanitize_evidence(items: Iterable[Evidence]) -> tuple[list[Evidence], int]:
    """Remove evidence whose text or value could identify a customer.

    An entry is dropped whole rather than partially rewritten: a metric whose
    label cannot be proven safe must not become citable evidence.
    """
    out: list[Evidence] = []
    removed = 0
    for item in items:
        label = _sanitize_string_field(item.label)
        definition = _sanitize_string_field(item.definition) or ""
        reason = _sanitize_string_field(item.unavailable_reason)
        key = _sanitize_string_field(item.metric_key)
        value, value_removed = _sanitize(item.value, 0)
        previous, previous_removed = _sanitize(item.previous_value, 0)
        removed += value_removed + previous_removed
        if (
            label is None or key is None or item.metric_key != key
            or (item.unavailable_reason is not None and reason is None)
            or value is DROP or previous is DROP
        ):
            removed += 1
            continue
        out.append(item.model_copy(update={
            "label": label,
            "definition": definition,
            "unavailable_reason": reason,
            "value": value,
            "previous_value": previous,
        }))
    return out, removed


def sanitize_tool_data(tool: str, data: Any) -> tuple[dict[str, Any], int]:
    """Project one tool's supplementary data onto its model-facing allowlist."""
    if not isinstance(data, dict):
        return {}, 1
    allowed = SAFE_TOOL_DATA_FIELDS.get(tool, ())
    if not allowed:
        return {}, len(data)
    out: dict[str, Any] = {}
    removed = 0
    for key, value in data.items():
        if key not in allowed:
            removed += 1
            continue
        cleaned, dropped = _sanitize(value, 0)
        removed += dropped
        if cleaned is DROP:
            removed += 1
            continue
        out[key] = cleaned
    return out, removed


def sanitize_model_context(
    evidence: Iterable[Evidence],
    tool_data: Iterable[dict[str, Any]],
) -> SanitizedContext:
    """The single boundary every prompt passes through."""
    clean_evidence, removed = sanitize_evidence(evidence)
    clean_data: list[dict[str, Any]] = []
    for entry in tool_data:
        if not isinstance(entry, dict):
            removed += 1
            continue
        tool = entry.get("tool")
        data, dropped = sanitize_tool_data(str(tool or ""), entry.get("data"))
        removed += dropped
        clean_data.append({"tool": tool, "data": data})
    return SanitizedContext(evidence=clean_evidence, tool_data=clean_data, removed=removed)
