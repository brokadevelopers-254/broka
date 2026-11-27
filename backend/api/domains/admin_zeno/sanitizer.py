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

    # A stored answer can name a person without any role word or label in
    # front of it ("Jane Wanjiru has not replied"). The conservative fallback
    # runs here too, so a legacy turn is held to the same policy as today's
    # administrator message.
    placeholders = _Placeholders()

    def _named_role(match: re.Match[str]) -> str:
        role = match.group(1).lower()
        return "%s %s" % (
            match.group(1),
            placeholders.take("person", match.group(2), cleaned, match.start(),
                              role="administrator" if role == "admin" else role),
        )

    def _named_label(match: re.Match[str]) -> str:
        return "%s %s" % (
            match.group(1),
            placeholders.take("person", match.group(2), cleaned, match.start()),
        )

    cleaned, count = _NAMED_ROLE.subn(_named_role, cleaned)
    removed += count
    cleaned, count = _NAME_LABEL.subn(_named_label, cleaned)
    removed += count
    cleaned, count = _redact_titlecase_persons(cleaned, placeholders)
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


# ── The administrator's own message ─────────────────────────────────────────
#
# The functions above protect what Jarvis retrieved and what earlier turns
# stored. The administrator's current message was the last unprotected path:
# it is typed into a dashboard box and can name a seller, paste a phone number
# out of a WhatsApp thread or carry an API key. It reaches planning, synthesis
# and every repair call, so it is sanitized first and only the sanitized text
# is allowed near a prompt.

MAX_MESSAGE_CHARS = 2000

# Placeholder labels. They sort by kind so a reader can tell what was removed
# without seeing the value.
_KIND_LABELS = {
    "person": "PERSON",
    "phone": "PHONE",
    "email": "EMAIL",
    "account_id": "ID",
    "credential": "CREDENTIAL",
}

# Role words are business context, so they survive; the person next to one is
# replaced. They also break a name run so "seller John Doe" keeps "seller".
_ROLE_WORDS = frozenset({
    "seller", "buyer", "customer", "user", "account", "administrator",
    "admin", "vendor",
})

# Title-case runs made only of these words are domain vocabulary, places,
# brands or dates - not people. The list is deliberately a tuning surface: an
# unlisted multi-word name is redacted (fail closed), an over-listed phrase is
# merely less readable.
_SAFE_TITLE_WORDS = frozenset("""
    broka zeno jarvis mode mobile money mpesa kes kenya east africa african
    nairobi mombasa kisumu nakuru eldoret thika kiambu meru machakos
    county counties marketplace market escrow dashboard api ai sms usd gdp
    premium boost badge store stores storefront listing listings auction
    auctions phase investigation summary verified findings evidence insight
    overview report question new total metrics performance
    trust safety contact contacts rate rates complaint complaints dispute
    disputes order orders revenue cost costs profit margin growth retention
    engagement funnel cohort cohorts liquidity demand forecast forecasts
    anomaly anomalies seller sellers buyer buyers customer customers
    operations platform health integrations activity registrations ranking
    rankings ranked top weekly daily monthly quarter
    january february march april may june july august september october
    november december monday tuesday wednesday thursday friday saturday sunday
""".split())

# Contact details and credentials, matched by shape. Ordered most specific
# first by the caller, because an e-mail also contains a phone-shaped token.
_PHONE_INTL = re.compile(r"\+\d[\d\s().\-]{6,}\d")
_PHONE_KENYAN = re.compile(r"\b(?:254|0)(?:7|1)\d{8}\b")
_PHONE_SPACED = re.compile(r"(?<![\w.])\d{2,4}(?:[\s\-]\d{2,4}){2,4}(?![\w.])")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE)
_API_KEY = re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_\-]{10,}")
_ID_LABEL = re.compile(
    r"\b(?:user|seller|buyer|customer|account|admin|order|deal|listing|"
    r"conversation)[ _-]?id\b\s*[:=#]?\s*[A-Za-z0-9._\-]{3,}",
    re.IGNORECASE,
)
# A long token mixing letters and digits is an opaque identifier (a session
# token, a reference number) even when nothing labels it as one.
_OPAQUE_ID = re.compile(
    r"\b(?=[A-Za-z0-9._\-]{12,}\b)(?=[^\s]*\d)(?=[^\s]*[A-Za-z])[A-Za-z0-9._\-]{12,}\b"
)
_NAME_LABEL = re.compile(
    r"\b(full name|name|customer|seller|buyer|account holder)\s*[:=]\s*"
    r"([^\s,;]{2,40}(?:\s+[A-Z][a-z]+)?)",
    re.IGNORECASE,
)
_NAMED_ROLE = re.compile(
    r"\b(seller|buyer|customer|account|user|administrator|vendor)\s+"
    r"(?:named\s+|called\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
)
_TITLE_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


@dataclass(frozen=True)
class SubjectReference:
    """A non-identifying handle for something the administrator named.

    Only the handle, its kind and (when the question states one) the account
    role are allowed into a prompt. The real value stays in the caller's
    memory for this turn and is never serialized, logged or persisted.
    """

    reference: str
    kind: str
    role: Optional[str] = None

    def brief(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reference": self.reference, "kind": self.kind}
        if self.role:
            out["role"] = self.role
        return out


@dataclass
class SanitizedMessage:
    text: str
    subjects: list[SubjectReference] = field(default_factory=list)
    redactions: int = 0


class UnsafeModelContext(Exception):
    """A prompt could not be proven free of identity or secrets.

    Raised instead of sending the request. The message names a category only -
    never the matched value, the prompt or the payload - so the exception is
    safe to log and to turn into an API response.
    """


class _Placeholders:
    """Assign stable, per-kind placeholders and keep the backend-only map."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self.subjects: list[SubjectReference] = []

    def take(self, kind: str, value: str, source: str, offset: int,
             role: Optional[str] = None) -> str:
        key = f"{kind}:{value.strip().lower()}"
        existing = self._seen.get(key)
        if existing is not None:
            return existing
        count = self._counts.get(kind, 0) + 1
        self._counts[kind] = count
        reference = f"[{_KIND_LABELS[kind]}_{count}]"
        self._seen[key] = reference
        self.subjects.append(SubjectReference(
            reference=reference, kind=kind,
            role=role or _role_near(source, offset),
        ))
        return reference


def _role_near(text: str, offset: int, window: int = 60) -> Optional[str]:
    """The account role the administrator mentioned before the value."""
    preceding = text[max(0, offset - window):offset].lower()
    best: Optional[str] = None
    best_at = -1
    for word in _ROLE_WORDS:
        at = preceding.rfind(word)
        if at > best_at:
            best, best_at = word, at
    if best == "admin":
        return "administrator"
    return best


def _redact(text: str, pattern: re.Pattern[str], kind: str, ph: _Placeholders,
            *, valid=None) -> tuple[str, int]:
    def _sub(match: re.Match[str]) -> str:
        value = match.group(0)
        if valid is not None and not valid(value):
            return value
        return ph.take(kind, value, text, match.start())

    return pattern.subn(_sub, text)


def _enough_digits(value: str, minimum: int = 9) -> bool:
    return sum(char.isdigit() for char in value) >= minimum


def _is_title_token(token: str) -> bool:
    if len(token) < 2 or token.isupper():
        return False
    if not token[0].isupper():
        return False
    return all(part[:1].isupper() for part in token.split("-"))


def _redact_titlecase_persons(text: str, ph: _Placeholders) -> tuple[str, int]:
    """Catch a personal name even when no regex shape announced it.

    Two or three adjacent title-case words that are not domain vocabulary,
    places or dates are treated as a person's name. This is the conservative
    fallback: an unfamiliar name is redacted rather than trusted to a regex
    that did not recognise it.
    """
    tokens = list(_TITLE_WORD.finditer(text))
    spans: list[tuple[int, int, str]] = []
    index = 0
    while index < len(tokens):
        run: list[re.Match[str]] = []
        cursor = index
        while cursor < len(tokens) and len(run) < 3:
            token = tokens[cursor]
            if run and text[tokens[cursor - 1].end():token.start()].strip():
                break
            word = token.group(0)
            if word.lower() in _ROLE_WORDS:
                break
            if not _is_title_token(word) or word.lower() in _SAFE_TITLE_WORDS:
                break
            run.append(token)
            cursor += 1
        if len(run) >= 2:
            spans.append((run[0].start(), run[-1].end(),
                          " ".join(t.group(0) for t in run)))
            index = cursor
        else:
            index = max(cursor, index + 1)

    removed = 0
    for start, end, value in reversed(spans):
        text = text[:start] + ph.take("person", value, text, start) + text[end:]
        removed += 1
    return text, removed


_PLACEHOLDER = re.compile(r"\[(?:PERSON|PHONE|EMAIL|ID|CREDENTIAL)_\d+\]")


def _mask_placeholders(text: str) -> tuple[str, list[str]]:
    """Hide already-inserted placeholders from later detection passes.

    ``[CREDENTIAL_1]`` is twelve characters mixing letters and digits, which a
    later pass would otherwise read as an opaque id and redact again.
    """
    saved: list[str] = []

    def _save(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return f"\x00{len(saved) - 1}\x00"

    return _PLACEHOLDER.sub(_save, text), saved


def _restore_placeholders(text: str, saved: list[str]) -> str:
    for index, value in enumerate(saved):
        text = text.replace(f"\x00{index}\x00", value)
    return text


def sanitize_admin_message(message: str) -> SanitizedMessage:
    """The only path from the administrator's message into a prompt.

    Order matters: credentials and contact details are matched before names
    (an e-mail would otherwise be read as part of one), and a name label keeps
    its role word so the question still reads as a business question.
    """
    if not isinstance(message, str):
        raise UnsafeModelContext("the administrator message is not text")
    ph = _Placeholders()
    text = message[:MAX_MESSAGE_CHARS]
    redactions = 0

    for pattern in (_CREDENTIAL, _BEARER, _API_KEY, _PEM):
        text, count = _redact(text, pattern, "credential", ph)
        redactions += count
    text, count = _redact(text, _EMAIL, "email", ph)
    redactions += count
    text, count = _redact(text, _UUID, "account_id", ph)
    redactions += count
    text, count = _redact(text, _LONG_HEX_ID, "account_id", ph)
    redactions += count
    text, count = _redact(text, _ID_LABEL, "account_id", ph)
    redactions += count

    text, masked = _mask_placeholders(text)
    text, count = _redact(text, _OPAQUE_ID, "account_id", ph)
    redactions += count
    text, count = _redact(text, _PHONE_INTL, "phone", ph,
                          valid=lambda v: _enough_digits(v, 8))
    redactions += count
    text, count = _redact(text, _PHONE_KENYAN, "phone", ph)
    redactions += count
    text, count = _redact(text, _PHONE_SPACED, "phone", ph,
                          valid=lambda v: _enough_digits(v, 9))
    redactions += count

    def _named_role(match: re.Match[str]) -> str:
        role = match.group(1).lower()
        return "%s %s" % (
            match.group(1),
            ph.take('person', match.group(2), text, match.start(),
                    role="administrator" if role == "admin" else role),
        )

    text, count = _NAMED_ROLE.subn(_named_role, text)
    redactions += count

    def _named_label(match: re.Match[str]) -> str:
        return f"{match.group(1)} {ph.take('person', match.group(2), text, match.start())}"

    text, count = _NAME_LABEL.subn(_named_label, text)
    redactions += count

    text = _restore_placeholders(text, masked)
    text, masked = _mask_placeholders(text)
    text, count = _redact_titlecase_persons(text, ph)
    redactions += count
    text = _restore_placeholders(text, masked)

    cleaned = " ".join(text.split())
    if not any(char.isalpha() for char in _PLACEHOLDER.sub(" ", cleaned)):
        # Nothing but placeholders (or whitespace) survived: there is no safe
        # question left to ask, so the caller fails closed instead of sending
        # a prompt the model cannot interpret.
        raise UnsafeModelContext("the question could not be made safe to send")
    return SanitizedMessage(text=cleaned, subjects=ph.subjects, redactions=redactions)


def sanitize_dashboard_context(context: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Project the dashboard-supplied context onto the fields it may carry.

    ``ChatContext`` already refuses unknown keys, but the orchestrator is also
    called from code and tests, so the boundary does not rely on the request
    model having been used. An unknown value is dropped, not forwarded.
    """
    if not isinstance(context, dict):
        return {}
    out: dict[str, Any] = {}
    page = context.get("page")
    if isinstance(page, str):
        page = page.strip()
        if page.startswith("/") and not page.startswith("//") and " " not in page \
                and "://" not in page and len(page) <= 80 \
                and outbound_violation(page) is None:
            out["page"] = page[:80]
    period = context.get("period_days")
    if isinstance(period, int) and not isinstance(period, bool) and 1 <= period <= 90:
        out["period_days"] = period
    return out


# High-precision shapes that must never leave the backend. Used as the final
# transport guard, so the patterns avoid bare digit runs: a revenue total is
# JSON a number, while a pasted phone number keeps its prefix or separators.
_OUTBOUND_CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential", _PEM),
    ("credential", _BEARER),
    ("credential", _API_KEY),
    ("credential", _CREDENTIAL),
    ("email", _EMAIL),
    ("identifier", _UUID),
    ("identifier", _LONG_HEX_ID),
    ("identifier", _ID_LABEL),
    ("phone", _PHONE_INTL),
    ("phone", _PHONE_KENYAN),
    ("phone", _PHONE_SPACED),
)


def outbound_violation(text: str) -> Optional[str]:
    """The category of a high-confidence leak in model-bound text, if any.

    Deliberately returns a label rather than the match: the caller may log it,
    and the matched value is exactly what must not be logged.
    """
    if not isinstance(text, str) or not text:
        return None
    for category, pattern in _OUTBOUND_CHECKS:
        for match in pattern.finditer(text):
            if category == "phone" and not _enough_digits(match.group(0), 9):
                continue
            return category
    return None


@dataclass
class ModelContext:
    """Everything a model may see for one turn, already sanitized.

    Built once from the administrator's message and the stored transcript,
    then extended with the retrieved intelligence. Planning, synthesis and any
    repair call read from this object only, so a stage cannot re-introduce a
    path around the boundary.
    """

    question: str
    history: list[dict[str, Any]] = field(default_factory=list)
    dashboard_context: dict[str, Any] = field(default_factory=dict)
    subjects: list[SubjectReference] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    tool_data: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    removed: int = 0

    def subject_brief(self) -> list[dict[str, Any]]:
        """Non-identifying references: handle, kind and stated role only."""
        return [subject.brief() for subject in self.subjects]

    def with_intelligence(
        self,
        evidence: Iterable[Evidence],
        tool_data: Iterable[dict[str, Any]],
    ) -> "ModelContext":
        view = sanitize_model_context(evidence, tool_data)
        limitations = list(self.limitations)
        if view.removed:
            limitations.append(
                "Some retrieved fields were withheld from the model because they could "
                "identify a customer. The aggregated figures are unaffected."
            )
        return ModelContext(
            question=self.question,
            history=list(self.history),
            dashboard_context=dict(self.dashboard_context),
            subjects=list(self.subjects),
            evidence=view.evidence,
            tool_data=view.tool_data,
            limitations=limitations,
            removed=self.removed + view.removed,
        )


def prepare_model_context(
    *,
    message: str,
    history: Iterable[dict[str, Any]] = (),
    dashboard_context: Optional[dict[str, Any]] = None,
) -> ModelContext:
    """The one preparation step before any prompt is built.

    Sanitizes the current administrator message and the stored transcript and
    projects the dashboard context. Raises :class:`UnsafeModelContext` when a
    safe question cannot be produced.
    """
    sanitized = sanitize_admin_message(message)
    turns, history_redactions = sanitize_history(history)
    limitations: list[str] = []
    if history_redactions:
        limitations.append(
            "Earlier turns contained identifying details, which were withheld "
            "from the model before this answer was produced."
        )
    if sanitized.subjects:
        limitations.append(
            "The question named specific accounts. Their details were replaced with "
            "neutral references and no account records were sent to the model; the "
            "answer can only use aggregate evidence."
        )
    return ModelContext(
        question=sanitized.text,
        history=turns,
        dashboard_context=sanitize_dashboard_context(dashboard_context),
        subjects=list(sanitized.subjects),
        limitations=limitations,
        removed=sanitized.redactions + history_redactions,
    )
