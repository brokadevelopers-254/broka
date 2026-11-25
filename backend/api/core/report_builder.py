"""The report builder: custom reports from figures BROKA already measures.

An administrator picks report categories or single metrics, a date range, a
comparison, filters, a grouping and a visualization. `build` turns that spec
into a payload that is stored as a ReportRun (api/models/report_builder.py)
and read back as it was written.

Every figure comes from the module that already defines it - pulse_between
(intelligence.py), engagement_summary (activity.py), the listing stages
(market_intel.py), revenue_report (revenue.py), payments, delivery and calls
(operations.py) - so a report cannot disagree with the dashboard page the
figure comes from. Nothing is estimated to fill a gap:

  * A figure that cannot be measured for the period is listed as
    unavailable with the reason, has no value, and is left out of the
    summary, the changes and the observations.
  * A point-in-time figure (live listings, open complaints) is as of the
    report's generation and has no comparison.
  * Dates are whole UTC days. The spec's end day is included; stored ranges
    are half-open [from, to). A range that reaches today ends at the moment
    of generation and is marked in progress.

The existing daily, weekly and monthly founder briefs (admin_reports.py) are
separate and unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.intelligence import _AGREED_STATES, _staff, iso

logger = logging.getLogger(__name__)

PAYLOAD_VERSION = 1
MAX_RANGE_DAYS = 366
ANOMALY_MAX_DAYS = 92
ANOMALY_WEEKS = 6
MAX_CHANGES = 8
MAX_OBSERVATIONS = 8
MAX_ANOMALIES = 10
CHANGE_MIN_PCT = 20.0
CHANGE_MIN_BASE = 10
CHANGE_MIN_POINTS = 5.0

COMPARISONS = ("previous_period", "previous_year", "none")
GROUPINGS = ("none", "day", "week", "month", "category", "county")
VISUALIZATIONS = ("auto", "line", "bar", "table")
TIME_GROUPINGS = ("day", "week", "month")


class SpecError(ValueError):
    """A spec the builder cannot run; the message says what to change."""


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    category: str
    source: str
    unit: str = "count"          # count | pct | kes_minor | minutes | ms
    good: str = "up"             # up | down | neutral
    kind: str = "period"         # period | point (as of generation) | period_end
    definition: str = ""
    origin: str = ""             # the table or function it is read from
    filters: tuple[str, ...] = ()
    series: Optional[str] = None
    action: Optional[str] = None


CATEGORIES: list[dict] = [
    {"key": "growth", "label": "Platform growth", "description": "Sign-ups, new sellers and listings."},
    {"key": "engagement", "label": "User engagement", "description": "Who used BROKA and how much."},
    {"key": "marketplace", "label": "Marketplace performance", "description": "Buyers, sellers, deals and liquidity."},
    {"key": "listings", "label": "Listing performance", "description": "Each buyer stage summed over listings; filters apply."},
    {"key": "sellers", "label": "Seller performance", "description": "How sellers answer and who engages."},
    {"key": "revenue", "label": "Revenue and payments", "description": "Income, costs and payment outcomes."},
    {"key": "trust", "label": "Fraud and moderation", "description": "Complaints, disputes, flags and account actions."},
    {"key": "system", "label": "System health", "description": "API, database, notifications and calls."},
    {"key": "business", "label": "Overall business performance", "description": "The headline figures from every area."},
]
CATEGORY_KEYS = tuple(c["key"] for c in CATEGORIES)

_L = ("category", "county")

METRICS: list[Metric] = [
    # Platform growth
    Metric("new_users", "New users", "growth", "pulse", series="registrations",
           definition="Accounts created in the period; administrators excluded.", origin="users.created_at",
           action="Check the sign-up and OTP flow and where new users come from."),
    Metric("new_sellers", "New sellers", "growth", "pulse",
           definition="Users whose first listing ever was posted in the period.", origin="listings.created_at"),
    Metric("new_listings", "New listings", "growth", "pulse", series="listings",
           definition="Listings created in the period by non-staff sellers.", origin="listings.created_at",
           action="Check the listing flow (photos, payment, categories) and recent app releases."),
    Metric("registered_users", "Registered users", "growth", "users_total", kind="period_end",
           definition="Accounts registered by the end of the period; administrators excluded.",
           origin="users.created_at"),
    Metric("live_listings", "Live listings", "growth", "pulse", kind="point",
           definition="Listings buyers can see at the time of generation. Not known for past dates, so never compared.",
           origin="listings (active, paid or free, not sold out)"),
    # User engagement
    Metric("active_users", "Active users", "engagement", "activity", series="active_users",
           definition="Distinct accounts with foreground activity (heartbeats) in the period.",
           origin="user_activity_days", action="Check the app's latest release and push delivery."),
    Metric("new_active_users", "First-time active users", "engagement", "activity",
           definition="Active users with no activity recorded before the period.", origin="user_activity_days"),
    Metric("returning_active_users", "Returning active users", "engagement", "activity",
           definition="Active users who had been active before the period.", origin="user_activity_days"),
    Metric("sessions_started", "Sessions started", "engagement", "activity",
           definition="Signed-in app sessions that began in the period.", origin="user_sessions"),
    Metric("avg_session_minutes", "Average session", "engagement", "activity", unit="minutes", good="neutral",
           definition="Mean foreground minutes of the sessions that began and closed in the period.",
           origin="user_sessions.foreground_seconds"),
    Metric("conversations_started", "Conversations started", "engagement", "pulse",
           definition="Buyer conversations whose first message was in the period.", origin="negotiation_messages",
           action="Check listing quality and whether buyers can reach sellers (chat and push health)."),
    Metric("messages", "Messages", "engagement", "pulse",
           definition="Buyer and seller messages on real listings; Zeno and staff excluded.",
           origin="negotiation_messages"),
    Metric("zeno_conversations", "Zeno conversations", "engagement", "pulse",
           definition="Conversations users opened with Zeno, the assistant.", origin="zeno_conversations"),
    # Marketplace performance
    Metric("active_buyers", "Active buyers", "marketplace", "pulse",
           definition="Distinct users who messaged a seller, showed interest or asked the Buying Agent.",
           origin="negotiation_messages, interests, buy_agent_requests"),
    Metric("active_sellers", "Active sellers", "marketplace", "pulse",
           definition="Distinct users who answered a buyer or posted a listing.", origin="negotiation_messages, listings"),
    Metric("deals_agreed", "Deals agreed", "marketplace", "pulse", series="deals_agreed",
           definition="Deals created in the period and now past negotiation (not cancelled); staff excluded.",
           origin="deals.created_at", action="Read recent negotiations for a blocking step; check escrow and payment errors."),
    Metric("deals_completed", "Deals completed", "marketplace", "deals", series="deals_completed",
           definition="Deals whose escrow was released to the seller in the period; staff excluded.",
           origin="deals.released_at", action="Check delivery confirmations and disputes holding releases."),
    Metric("deal_value_minor", "Value of deals agreed", "marketplace", "deals", unit="kes_minor",
           definition="Agreed price of the deals agreed in the period. Deal money is held in escrow, not BROKA income.",
           origin="deals.agreed_price"),
    Metric("liquidity_pct", "Listing liquidity", "marketplace", "pulse", unit="pct",
           definition="Share of the listings posted in the week before the period's last week that a buyer contacted within 7 days.",
           origin="listings, negotiation_messages, interests", action="Promote new listings and check search and feed availability."),
    Metric("buy_agent_requests", "Buying Agent requests", "marketplace", "pulse",
           definition="Requests buyers gave the Buying Agent.", origin="buy_agent_requests"),
    Metric("sold_out_reports", "Sold-out reports", "marketplace", "pulse", good="neutral",
           definition="Buyers reporting a listing as no longer available.", origin="sold_out_reports"),
    # Listing performance (filters apply)
    Metric("listings_engaged", "Listings engaged", "listings", "stages", filters=_L,
           definition="Listings at least one buyer viewed, saved, liked or contacted in the period.",
           origin="listing_views, wishlists, listing_likes, interests, negotiation_messages"),
    Metric("listing_viewers", "Listing viewers", "listings", "stages", filters=_L,
           definition="First-time viewers summed over listings: a buyer who viewed three listings counts three times.",
           origin="listing_views"),
    Metric("listing_savers", "Listing savers", "listings", "stages", filters=_L,
           definition="Buyers who saved a listing, summed over listings.", origin="wishlists"),
    Metric("listing_contacters", "Buyers in contact", "listings", "stages", filters=_L,
           definition="Buyers who messaged or showed interest, summed over listings.", origin="negotiation_messages, interests"),
    Metric("listing_negotiators", "Buyers negotiating", "listings", "stages", filters=_L,
           definition="Buyers whose conversation went both ways or who made an offer, summed over listings. Not a sale.",
           origin="negotiation_messages, interests.offer_price"),
    Metric("listing_deal_buyers", "Buyers who agreed a deal", "listings", "stages", filters=_L,
           definition="Buyers with a deal created in the period and past negotiation, summed over listings.", origin="deals"),
    Metric("listing_completed_buyers", "Buyers who completed a deal", "listings", "stages", filters=_L,
           definition="Buyers whose deal's escrow was released in the period, summed over listings.", origin="deals.released_at"),
    Metric("listing_view_to_contact_pct", "View to contact", "listings", "stages", unit="pct", filters=_L,
           definition="Buyers in contact / listing viewers, over every listing.", origin="listing stages",
           action="Review prices, photos and descriptions of the most viewed listings."),
    Metric("listing_contact_to_deal_pct", "Contact to deal", "listings", "stages", unit="pct", filters=_L,
           definition="Buyers who agreed a deal / buyers in contact. Contact is engagement; this is conversion.",
           origin="listing stages", action="Read the conversations that stalled; check stock and seller replies."),
    # Seller performance
    Metric("seller_reply_pct", "Sellers replying within 24 hours", "sellers", "pulse", unit="pct",
           definition="Buyer conversations started in the period that the seller answered within 24 hours.",
           origin="negotiation_messages", action="Nudge the sellers with buyers waiting (Seller performance)."),
    Metric("median_reply_minutes", "Median seller reply", "sellers", "pulse", unit="minutes", good="down",
           definition="Median minutes to the seller's first reply, for conversations started in the period.",
           origin="negotiation_messages"),
    Metric("sellers_engaged", "Sellers with buyer attention", "sellers", "perf",
           definition="Sellers whose listings a buyer viewed, saved or contacted in the period.", origin="listing stages"),
    Metric("buyers_engaged", "Buyers browsing", "sellers", "perf",
           definition="Buyers who viewed, saved or contacted at least one listing in the period.", origin="listing stages"),
    Metric("browsers_without_contact", "Buyers who never got in touch", "sellers", "perf", good="down",
           definition="Buyers who viewed a listing and contacted no seller in the period.", origin="listing stages"),
    # Revenue and payments
    Metric("income_minor", "Income", "revenue", "pulse", unit="kes_minor", series="income",
           definition="Money users paid BROKA: fees, plans, boosts, badges and Daraja deal commission. Not escrow.",
           origin="revenue_report", action="Check payment success and the fee flows (listings, plans, boosts)."),
    Metric("costs_minor", "Costs", "revenue", "pulse", unit="kes_minor", good="down", series="costs",
           definition="Recorded and estimated platform costs: SMS, AI, API, payment fees, infrastructure.",
           origin="revenue_report"),
    Metric("profit_minor", "Profit", "revenue", "pulse", unit="kes_minor", series="profit",
           definition="Income minus costs.", origin="revenue_report"),
    Metric("paying_customers", "Paying customers", "revenue", "paying",
           definition="Distinct users who paid BROKA anything in the period; test charges excluded.", origin="revenue_report"),
    Metric("payments_succeeded", "Successful payments", "revenue", "payments", series="payments_succeeded",
           definition="M-Pesa and ZetuPay payment attempts started in the period that succeeded.", origin="payments"),
    Metric("payments_failed", "Failed payments", "revenue", "payments", good="down", series="payments_failed",
           definition="Payment attempts started in the period that failed.", origin="payments",
           action="Check Daraja and ZetuPay status and the failure reasons on the Operations page."),
    Metric("payment_success_pct", "Payment success rate", "revenue", "payments", unit="pct",
           definition="Successful / (successful + failed) attempts; pending ones left out.", origin="payments",
           action="Check Daraja and ZetuPay status and the failure reasons on the Operations page."),
    # Fraud and moderation
    Metric("complaints_filed", "Complaints filed", "trust", "pulse", good="down",
           definition="Complaints users filed in the period.", origin="user_feedback",
           action="Read the newest complaints and the users complained about most."),
    Metric("disputes_opened", "Disputes opened", "trust", "pulse", good="down", series="disputes",
           definition="Dispute cases opened in the period.", origin="dispute_cases",
           action="Review the open disputes for a common cause (a seller, a category, delivery)."),
    Metric("fraud_flags", "Fraud flags", "trust", "pulse", good="neutral", series="fraud_flags",
           definition="Accounts flagged for fraud review in the period.", origin="fraud_events"),
    Metric("accounts_restricted", "Accounts restricted", "trust", "moderation", good="neutral",
           definition="Restrictions administrators applied in the period.", origin="account_moderation_events"),
    Metric("accounts_suspended", "Accounts suspended", "trust", "moderation", good="neutral",
           definition="Suspensions administrators applied in the period.", origin="account_moderation_events"),
    Metric("accounts_reinstated", "Accounts reinstated", "trust", "moderation", good="neutral",
           definition="Reinstatements administrators applied in the period.", origin="account_moderation_events"),
    Metric("complaints_open", "Complaints open", "trust", "complaints_now", kind="point", good="down",
           definition="Complaints not yet resolved at the time of generation.", origin="user_feedback"),
    Metric("complaints_sla_breached", "Complaints past their reply deadline", "trust", "complaints_now",
           kind="point", good="down", definition="Open complaints past their first-reply deadline at the time of generation.",
           origin="user_feedback"),
    # System health
    Metric("api_requests", "API requests", "system", "ops", good="neutral",
           definition="HTTP answers every API process gave, from the operations samples.", origin="ops_samples"),
    Metric("api_error_rate_pct", "API server error rate", "system", "ops", unit="pct", good="down",
           definition="Answers with status 500-599 / all answers.", origin="ops_samples",
           action="Open Operations for the failing endpoints."),
    Metric("api_p95_ms", "API latency p95", "system", "ops", unit="ms", good="down",
           definition="95th percentile of request time, estimated from the latency histogram.", origin="ops_samples",
           action="Open Operations for the slowest endpoints and the database's slow statements."),
    Metric("db_slow_queries", "Slow database statements", "system", "ops", good="down",
           definition="Statements that ran 500 ms or more.", origin="ops_samples"),
    Metric("db_query_failures", "Failed database statements", "system", "ops", good="down",
           definition="Statements the database refused or that failed.", origin="ops_samples"),
    Metric("db_unavailable", "Requests refused: database unavailable", "system", "ops", good="down",
           definition="Requests answered 503 because no database connection could be had.", origin="ops_samples"),
    Metric("push_accepted", "Pushes accepted by FCM", "system", "ops", good="neutral",
           definition="Phones FCM accepted a push for. Acceptance is not delivery.", origin="ops_samples"),
    Metric("push_failed", "Pushes FCM did not accept", "system", "ops", good="down",
           definition="Phones a push failed for, by FCM's answer.", origin="ops_samples",
           action="Check FCM configuration and device-token health."),
    Metric("background_task_failures", "Failed background tasks", "system", "ops", good="down",
           definition="Tasks of the background loops that raised.", origin="ops_samples",
           action="Open Operations, Background processing, for the failing tasks."),
    Metric("messages_delivered_pct", "Chat messages confirmed on the phone", "system", "deliveries", unit="pct",
           definition="Chat messages the app confirmed it had (sync, chat, push receipt or read).",
           origin="message_deliveries", action="Check push health and the app's sync."),
    Metric("calls_connected_pct", "Answered calls that connected", "system", "calls", unit="pct",
           definition="Completed calls / calls answered (completed or never connected).", origin="negotiation_messages (call cards)",
           action="Check TURN and signalling health and recent app releases."),
    Metric("calls_failed_to_connect", "Calls answered but never connected", "system", "calls", good="down",
           definition="Calls the callee answered whose audio never came up.", origin="negotiation_messages (call cards)"),
]
METRIC_BY_KEY = {m.key: m for m in METRICS}

BUSINESS_METRICS = ("new_users", "active_users", "deals_agreed", "deals_completed", "income_minor",
                    "profit_minor", "liquidity_pct", "payment_success_pct", "complaints_filed",
                    "api_error_rate_pct")

TABLE_TITLES = {
    "stages_by_category": "Listing stages by category",
    "stages_by_county": "Listing stages by county",
    "demand_by_category": "Demand and supply by category",
    "top_listings": "Most viewed listings",
    "top_sellers": "Sellers buyers contacted most",
    "income_by_stream": "Income by stream",
    "payments_by_provider": "Payments by provider",
    "complaints_by_category": "Complaints by category",
    "busiest_endpoints": "Busiest API endpoints",
}


def category_metrics(key: str) -> list[str]:
    if key == "business":
        return list(BUSINESS_METRICS)
    return [m.key for m in METRICS if m.category == key]


def catalog() -> dict:
    return {
        "categories": [{**c, "metrics": category_metrics(c["key"])} for c in CATEGORIES],
        "metrics": [{
            "key": m.key, "label": m.label, "category": m.category, "unit": m.unit, "good": m.good,
            "kind": m.kind, "definition": m.definition, "source": m.origin, "filters": list(m.filters),
            "series": m.series is not None,
        } for m in METRICS],
        "comparisons": [
            {"key": "previous_period", "label": "The period just before, same length"},
            {"key": "previous_year", "label": "The same dates a year earlier"},
            {"key": "none", "label": "No comparison"},
        ],
        "groupings": [
            {"key": "none", "label": "Figures only"},
            {"key": "day", "label": "By day"}, {"key": "week", "label": "By 7 days"},
            {"key": "month", "label": "By calendar month"},
            {"key": "category", "label": "By listing category"}, {"key": "county", "label": "By county"},
        ],
        "visualizations": [
            {"key": "auto", "label": "Chosen for the grouping"}, {"key": "line", "label": "Line chart"},
            {"key": "bar", "label": "Bar chart"}, {"key": "table", "label": "Table only"},
        ],
        "limits": {"max_range_days": MAX_RANGE_DAYS, "timezone": "UTC"},
        "definitions": GENERAL_NOTES,
    }


GENERAL_NOTES = [
    "Dates are whole UTC days (Nairobi is UTC+3); the end day is included.",
    "Administrator accounts are left out of every marketplace figure.",
    "A figure that cannot be measured for the period is listed as unavailable and left out of every summary.",
    "Point-in-time figures are as of the report's generation and are never compared.",
    "Filters apply to listing performance and the category and county breakdowns only; every other figure is platform-wide.",
]


# ── Specs ───────────────────────────────────────────────────────────────────

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_day(value: Optional[str], name: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _DAY.match(value):
        raise SpecError(f"{name} must be a date written YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SpecError(f"{name} is not a real date")


def normalize_spec(raw: dict, *, require_range: bool = True) -> dict:
    """The spec in canonical form: known keys only, metrics resolved from the
    categories, values checked. Raises SpecError."""
    if not isinstance(raw, dict):
        raise SpecError("the report spec must be an object")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise SpecError("give the report a title")
    categories = [c for c in (raw.get("categories") or []) if isinstance(c, str)]
    unknown = [c for c in categories if c not in CATEGORY_KEYS]
    if unknown:
        raise SpecError(f"unknown report category: {unknown[0]}")
    metrics = [m for m in (raw.get("metrics") or []) if isinstance(m, str)]
    unknown = [m for m in metrics if m not in METRIC_BY_KEY]
    if unknown:
        raise SpecError(f"unknown metric: {unknown[0]}")
    chosen: list[str] = []
    for key in [k for c in categories for k in category_metrics(c)] + metrics:
        if key not in chosen:
            chosen.append(key)
    if not chosen:
        raise SpecError("choose at least one category or metric")
    comparison = raw.get("comparison") or "previous_period"
    if comparison not in COMPARISONS:
        raise SpecError(f"comparison must be one of {', '.join(COMPARISONS)}")
    group_by = raw.get("group_by") or "none"
    if group_by not in GROUPINGS:
        raise SpecError(f"group_by must be one of {', '.join(GROUPINGS)}")
    visualization = raw.get("visualization") or "auto"
    if visualization not in VISUALIZATIONS:
        raise SpecError(f"visualization must be one of {', '.join(VISUALIZATIONS)}")
    filters = raw.get("filters") or {}
    if not isinstance(filters, dict):
        raise SpecError("filters must be an object")
    clean_filters = {}
    for name in ("category", "county"):
        value = filters.get(name)
        if value is not None and not isinstance(value, str):
            raise SpecError(f"the {name} filter must be text")
        value = (value or "").strip()
        if len(value) > 100:
            raise SpecError(f"the {name} filter is too long")
        clean_filters[name] = value or None

    date_from = _parse_day(raw.get("date_from"), "date_from")
    date_to = _parse_day(raw.get("date_to"), "date_to")
    last_days = raw.get("last_days")
    if last_days is not None:
        if isinstance(last_days, bool) or not isinstance(last_days, int) or not 1 <= last_days <= MAX_RANGE_DAYS:
            raise SpecError(f"last_days must be a whole number from 1 to {MAX_RANGE_DAYS}")
    if (date_from is None) != (date_to is None):
        raise SpecError("give both date_from and date_to, or neither")
    if date_from is not None and last_days is not None:
        raise SpecError("use either fixed dates or last_days, not both")
    if require_range and date_from is None and last_days is None:
        raise SpecError("choose a date range")
    if date_from is not None:
        if date_to < date_from:
            raise SpecError("date_to is before date_from")
        if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
            raise SpecError(f"a report covers at most {MAX_RANGE_DAYS} days")
    return {
        "title": title[:120],
        "categories": categories,
        "metrics": chosen,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "last_days": last_days,
        "comparison": comparison,
        "filters": clean_filters,
        "group_by": group_by,
        "visualization": visualization,
    }


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def spec_range(spec: dict, *, now: datetime) -> tuple[datetime, datetime]:
    """[start, end) for a spec: its fixed days, or its last N complete days."""
    today = _midnight(now.date())
    if spec.get("date_from"):
        start = _midnight(date.fromisoformat(spec["date_from"]))
        end = _midnight(date.fromisoformat(spec["date_to"])) + timedelta(days=1)
    elif spec.get("last_days"):
        end = today
        start = end - timedelta(days=int(spec["last_days"]))
    else:
        raise SpecError("choose a date range")
    if start >= now:
        raise SpecError("the report starts in the future")
    return start, min(end, now)


def _shift_year(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29 February
        return value.replace(year=value.year + years, day=28)


def comparison_range(kind: str, start: datetime, end: datetime) -> Optional[tuple[datetime, datetime]]:
    if kind == "previous_period":
        return start - (end - start), start
    if kind == "previous_year":
        return _shift_year(start, -1), _shift_year(end, -1)
    return None


def period_label(start: datetime, end: datetime) -> str:
    last = (end - timedelta(microseconds=1)).date()
    first = start.date()
    if first == last:
        return f"{first.day} {first:%b %Y}"
    if first.year == last.year and first.month == last.month:
        return f"{first.day}–{last.day} {last:%b %Y}"
    if first.year == last.year:
        return f"{first.day} {first:%b} – {last.day} {last:%b %Y}"
    return f"{first.day} {first:%b %Y} – {last.day} {last:%b %Y}"


# ── Sources: each returns {metric key: value} for one window ─────────────────

class _Context:
    def __init__(self, db: AsyncSession, now: datetime, filters: dict):
        self.db = db
        self.now = now
        self.filters = filters
        self.cache: dict[tuple, Any] = {}
        self.unavailable: dict[tuple, dict[str, str]] = defaultdict(dict)
        self.extras: dict[str, Any] = {}

    async def values(self, source: str, start: datetime, end: datetime) -> dict:
        key = (source, start, end)
        if key not in self.cache:
            self.cache[key] = await SOURCES[source](self, start, end)
        return self.cache[key]

    def mark(self, source: str, start: datetime, end: datetime, metric: str, reason: str) -> None:
        self.unavailable[(source, start, end)][metric] = reason


_PULSE_KEYS = ("new_users", "new_sellers", "new_listings", "live_listings", "active_buyers", "active_sellers",
               "conversations_started", "messages", "zeno_conversations", "deals_agreed", "sold_out_reports",
               "buy_agent_requests", "disputes_opened", "fraud_flags", "income_minor", "costs_minor",
               "profit_minor", "complaints_filed")


def _pulse_side(pulse: dict, side: str) -> dict:
    out = {k: pulse["metrics"].get(k, {}).get(side) for k in _PULSE_KEYS}
    out["liquidity_pct"] = ((pulse.get("liquidity") or {}).get(side) or {}).get("rate_pct")
    response = (pulse.get("seller_response") or {}).get(side) or {}
    out["seller_reply_pct"] = response.get("rate_pct")
    out["median_reply_minutes"] = response.get("median_reply_minutes")
    return out


async def _src_pulse(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core.intelligence import pulse_between

    pulse = await pulse_between(ctx.db, start, end, now=ctx.now)
    window = pulse.get("window") or {}
    try:
        prev_key = ("pulse", datetime.fromisoformat(str(window["previous_from"]).rstrip("Z")),
                    datetime.fromisoformat(str(window["previous_to"]).rstrip("Z")))
        # The comparison window is usually exactly the one pulse_between
        # already measured: reuse it rather than measure it again.
        ctx.cache.setdefault(prev_key, _pulse_side(pulse, "previous"))
    except (KeyError, ValueError):
        pass
    return _pulse_side(pulse, "current")


async def _src_users_total(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.database import User

    total = (await ctx.db.execute(
        select(func.count(User.id)).where(User.created_at < end, User.is_admin == False)  # noqa: E712
    )).scalar()
    return {"registered_users": int(total or 0)}


async def _src_activity(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core import activity

    summary = await activity.engagement_summary(ctx.db, start=start, end=end, now=ctx.now)
    coverage = summary.get("coverage_started_on")
    keys = ("active_users", "new_active_users", "returning_active_users", "sessions_started", "avg_session_minutes")
    # As the activity series does: a day is scored only after the first
    # recorded day, which may itself be partial.
    if coverage is None or date.fromisoformat(coverage) >= start.date():
        reason = ("Activity tracking had not started." if coverage is None else
                  f"Activity tracking began on {coverage}; this period starts before the first full day of it.")
        for k in keys:
            ctx.mark("activity", start, end, k, reason)
        return {k: None for k in keys}
    avg = summary.get("average_session_foreground_seconds")
    return {
        "active_users": summary.get("active_users_in_range"),
        "new_active_users": summary.get("new_active_users"),
        "returning_active_users": summary.get("returning_active_users"),
        "sessions_started": summary.get("sessions_started_in_range"),
        "avg_session_minutes": round(avg / 60, 1) if avg is not None else None,
    }


async def _src_stages(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core import market_intel

    stages = await market_intel.stage_totals(ctx.db, start, end, category=ctx.filters.get("category"),
                                             county=ctx.filters.get("county"))
    t = stages["totals"]
    if stages["truncated"]:
        ctx.extras["stages_truncated"] = True

    def rate(a, b):
        return None if not b else min(100.0, round(a / b * 100, 1))
    return {
        "listings_engaged": stages["listings_engaged"],
        "listing_viewers": t["views"], "listing_savers": t["saves"], "listing_contacters": t["contacts"],
        "listing_negotiators": t["negotiations"], "listing_deal_buyers": t["deals"],
        "listing_completed_buyers": t["completed"],
        "listing_view_to_contact_pct": rate(t["contacts"], t["views"]),
        "listing_contact_to_deal_pct": rate(t["deals"], t["contacts"]),
    }


async def _src_perf(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core import market_intel

    perf = await market_intel.performance(ctx.db, start=start, end=end, now=ctx.now, limit=10)
    ctx.extras.setdefault(("perf", start, end), perf)
    return {"sellers_engaged": perf["sellers"]["total"], "buyers_engaged": perf["buyers"]["total"],
            "browsers_without_contact": perf["buyers"]["browsers_without_contact"]}


async def _src_deals(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.database import Deal, DealStatus

    staff = _staff()
    completed = (await ctx.db.execute(
        select(func.count(Deal.id)).where(
            Deal.status == DealStatus.released, Deal.released_at >= start, Deal.released_at < end,
            Deal.seller_id.not_in(staff), Deal.buyer_id.not_in(staff))
    )).scalar()
    value = (await ctx.db.execute(
        select(func.sum(Deal.agreed_price)).where(
            Deal.created_at >= start, Deal.created_at < end, Deal.status.in_(_AGREED_STATES),
            Deal.seller_id.not_in(staff), Deal.buyer_id.not_in(staff))
    )).scalar()
    return {"deals_completed": int(completed or 0), "deal_value_minor": int(round(float(value or 0) * 100))}


async def _src_paying(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core.intelligence import paying_customers

    return {"paying_customers": await paying_customers(ctx.db, start, end)}


async def _src_payments(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core.operations import payment_counts

    counts = await payment_counts(ctx.db, start, end)
    ctx.extras.setdefault(("payments", start, end), counts)
    return {"payments_succeeded": counts["success"], "payments_failed": counts["failed"],
            "payment_success_pct": counts["success_pct"]}


async def _src_moderation(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.models.account_moderation import AccountModerationEvent as E

    rows = dict((await ctx.db.execute(
        select(E.action, func.count(E.id)).where(E.created_at >= start, E.created_at < end).group_by(E.action)
    )).all())
    return {"accounts_restricted": int(rows.get("restricted", 0)),
            "accounts_suspended": int(rows.get("suspended", 0)),
            "accounts_reinstated": int(rows.get("reinstated", 0))}


async def _src_complaints_now(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core.intelligence import complaints_overview

    overview = await complaints_overview(ctx.db, start, end, now=ctx.now)
    ctx.extras.setdefault(("complaints", start, end), overview)
    return {"complaints_open": overview.get("open"), "complaints_sla_breached": overview.get("sla_breached")}


async def _src_ops(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core import operations, request_metrics

    keys = ("api_requests", "api_error_rate_pct", "api_p95_ms", "db_slow_queries", "db_query_failures",
            "db_unavailable", "push_accepted", "push_failed", "background_task_failures")
    first = await operations.first_sample_at(ctx.db)
    if first is None or first > start:
        reason = ("Operations sampling has not started; it begins when this version of the API runs."
                  if first is None else
                  f"Operations sampling began {iso(first)}; this period starts before it.")
        for k in keys:
            ctx.mark("ops", start, end, k, reason)
        return {k: None for k in keys}
    samples, _ = await operations.load_samples(ctx.db, start, end)
    agg = operations.aggregate(samples)
    ctx.extras.setdefault(("ops", start, end), agg)
    c = agg["counters"]
    return {
        "api_requests": agg["requests"],
        "api_error_rate_pct": round(agg["server_errors"] / agg["requests"] * 100, 2) if agg["requests"] else None,
        "api_p95_ms": request_metrics.histogram_quantile(0.95, agg["histogram"]),
        "db_slow_queries": int(c.get("db.slow_queries", 0)),
        "db_query_failures": int(c.get("db.query_failures", 0)),
        "db_unavailable": sum(int(v) for k, v in c.items() if k.startswith("db.unavailable.")),
        "push_accepted": int(c.get("push.accepted", 0)),
        "push_failed": int(c.get("push.failed", 0)),
        "background_task_failures": sum(int(v) for k, v in c.items() if k.startswith("loop.") and k.endswith(".failed")),
    }


async def _src_deliveries(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core.message_delivery import RETENTION_DAYS
    from api.models.message_delivery import MessageDelivery as D

    if start < ctx.now - timedelta(days=RETENTION_DAYS - 1):
        ctx.mark("deliveries", start, end, "messages_delivered_pct",
                 f"Delivery records are kept {RETENTION_DAYS} days; this period starts before the oldest.")
        return {"messages_delivered_pct": None}
    total, delivered = (await ctx.db.execute(
        select(func.count(D.id), func.count(D.delivered_at))
        .where(D.message_created_at >= start, D.message_created_at < end)
    )).one()
    return {"messages_delivered_pct": round(delivered / total * 100, 1) if total else None}


async def _src_calls(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.database import NegotiationMessage

    # visibility-ok: call cards' outcome words and counts only, for report figures.
    rows = dict((await ctx.db.execute(
        select(NegotiationMessage.content, func.count(NegotiationMessage.id)).where(
            NegotiationMessage.msg_type == "call", NegotiationMessage.created_at >= start,
            NegotiationMessage.created_at < end, NegotiationMessage.sender_id.not_in(_staff()))
        .group_by(NegotiationMessage.content)
    )).all())
    completed = int(rows.get("completed", 0))
    failed = int(rows.get("failed_to_connect", 0))
    return {"calls_connected_pct": round(completed / (completed + failed) * 100, 1) if completed + failed else None,
            "calls_failed_to_connect": failed}


SOURCES: dict[str, Callable[[_Context, datetime, datetime], Awaitable[dict]]] = {
    "pulse": _src_pulse, "users_total": _src_users_total, "activity": _src_activity, "stages": _src_stages,
    "perf": _src_perf, "deals": _src_deals, "paying": _src_paying, "payments": _src_payments,
    "moderation": _src_moderation, "complaints_now": _src_complaints_now, "ops": _src_ops,
    "deliveries": _src_deliveries, "calls": _src_calls,
}


# ── Series ──────────────────────────────────────────────────────────────────

def time_buckets(start: datetime, end: datetime, group_by: str) -> list[tuple[datetime, datetime]]:
    """Whole UTC days, 7-day steps from the start, or calendar months, cut to [start, end)."""
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        if group_by == "day":
            nxt = cursor + timedelta(days=1)
        elif group_by == "week":
            nxt = cursor + timedelta(days=7)
        else:
            nxt = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        out.append((cursor, min(nxt, end)))
        cursor = nxt
    return out


def _series_specs() -> dict[str, dict]:
    from api.database import Deal, DealStatus, FraudEvent, Listing, User
    from api.models.dispute import DisputeCase

    staff = _staff()
    return {
        "registrations": {"column": User.created_at, "conds": [User.is_admin == False]},  # noqa: E712
        "listings": {"column": Listing.created_at, "conds": [Listing.seller_id.not_in(staff)]},
        "deals_agreed": {"column": Deal.created_at, "conds": [
            Deal.status.in_(_AGREED_STATES), Deal.seller_id.not_in(staff), Deal.buyer_id.not_in(staff)]},
        "deals_completed": {"column": Deal.released_at, "conds": [
            Deal.status == DealStatus.released, Deal.seller_id.not_in(staff), Deal.buyer_id.not_in(staff)]},
        "disputes": {"column": DisputeCase.created_at, "conds": []},
        "fraud_flags": {"column": FraudEvent.created_at, "conds": []},
    }


def _sum_days(daily: dict[str, float], buckets: list[tuple[datetime, datetime]]) -> list[Optional[float]]:
    out = []
    for b_start, b_end in buckets:
        total, day = 0.0, b_start.date()
        while _midnight(day) < b_end:
            total += daily.get(day.isoformat(), 0)
            day += timedelta(days=1)
        out.append(total)
    return out


async def _series_values(ctx: _Context, name: str, start: datetime, end: datetime,
                         buckets: list[tuple[datetime, datetime]], group_by: str) -> list[Optional[float]]:
    from api.core.market_models import daily_counts

    if name in ("income", "costs", "profit"):
        from api.core.revenue import revenue_report
        report = ctx.extras.get(("revenue_daily", start, end))
        if report is None:
            report = await revenue_report(ctx.db, from_raw=start.isoformat(), to_raw=end.isoformat(), bucket="day")
            ctx.extras[("revenue_daily", start, end)] = report
        field = f"{name}_minor"
        daily = {str(p["bucket_start"])[:10]: p[field] for p in report.get("series", [])}
        return _sum_days(daily, buckets)
    if name in ("payments_succeeded", "payments_failed"):
        from api.domains.admin.router import _payment_union
        union = _payment_union()
        wanted = "success" if name == "payments_succeeded" else "failed"
        day = func.date(union.c.created_at)
        rows = (await ctx.db.execute(
            select(day, union.c.status, func.count()).where(union.c.created_at >= start, union.c.created_at < end)
            .group_by(day, union.c.status)
        )).all()
        daily: dict[str, float] = defaultdict(float)
        for d, status, n in rows:
            if getattr(status, "value", status) == wanted:
                daily[str(d)[:10]] += int(n or 0)
        return _sum_days(daily, buckets)
    if name == "active_users":
        from api.core import activity
        series = await activity.active_users_series(ctx.db, buckets, "day" if group_by == "day" else group_by)
        values = [p["v"] for p in series["points"]]
        # Distinct users are counted by whole day: a bucket still in progress
        # would read low, so it is left unmeasured rather than shown short.
        if buckets and buckets[-1][1] != _midnight(buckets[-1][1].date()):
            values[-1] = None
        return values
    spec = _series_specs()[name]
    daily = await daily_counts(ctx.db, spec, start, end)
    return _sum_days(daily, buckets)


# ── Tables ──────────────────────────────────────────────────────────────────

_STAGE_COLUMNS = [
    {"key": "label", "label": "Group", "unit": "text"},
    {"key": "listings", "label": "Listings engaged", "unit": "count"},
    {"key": "views", "label": "Viewers", "unit": "count"},
    {"key": "saves", "label": "Savers", "unit": "count"},
    {"key": "contacts", "label": "In contact", "unit": "count"},
    {"key": "negotiations", "label": "Negotiating", "unit": "count"},
    {"key": "deals", "label": "Agreed a deal", "unit": "count"},
    {"key": "completed", "label": "Completed", "unit": "count"},
]


async def _tables(ctx: _Context, spec: dict, start: datetime, end: datetime, categories: set[str]) -> list[dict]:
    from api.core import market_intel

    group_by = spec["group_by"]
    viz = spec["visualization"]
    tables: list[dict] = []

    def table(key, columns, rows, *, chart_column=None, note=None, visual=None):
        tables.append({"key": key, "title": TABLE_TITLES[key], "columns": columns, "rows": rows,
                       "chart_column": chart_column, "note": note,
                       "visualization": visual or ("table" if viz == "table" else "bar" if chart_column else "table")})

    if group_by in ("category", "county"):
        stages = await market_intel.stage_totals(
            ctx.db, start, end, category=ctx.filters.get("category"), county=ctx.filters.get("county"),
            group_by=group_by)
        rows = [{"label": label, **values} for label, values in sorted(
            stages["groups"].items(), key=lambda kv: (-kv[1]["views"], kv[0]))]
        columns = [dict(_STAGE_COLUMNS[0], label="Category" if group_by == "category" else "County"),
                   *_STAGE_COLUMNS[1:]]
        table(f"stages_by_{group_by}", columns, rows[:30], chart_column="views",
              note="Each stage counts distinct buyers per listing, summed over the group's listings.")
        if group_by == "category":
            from api.core.intelligence import categories_between
            demand = await categories_between(ctx.db, start, end, now=ctx.now)
            rows = [{"label": c["label"], "demand": c["demand"], "supply_live": c["supply_live"],
                     "new_listings": c["new_listings"], "deals_agreed": c["deals_agreed"], "signal": c["signal"]}
                    for c in demand["categories"]][:30]
            table("demand_by_category", [
                {"key": "label", "label": "Category", "unit": "text"},
                {"key": "demand", "label": "Demand", "unit": "count"},
                {"key": "supply_live", "label": "Live listings now", "unit": "count"},
                {"key": "new_listings", "label": "New listings", "unit": "count"},
                {"key": "deals_agreed", "label": "Deals agreed", "unit": "count"},
                {"key": "signal", "label": "Signal", "unit": "text"},
            ], rows, chart_column="demand",
                note="Demand = buyer threads + interests + Buying Agent requests. Live listings are as of generation.")

    if "listings" in categories:
        ranked = await market_intel.rankings(ctx.db, start=start, end=end, now=ctx.now, limit=10,
                                             category=ctx.filters.get("category"), county=ctx.filters.get("county"))
        rows = [{"label": r["name"], "category": r["category"], "county": r["county"], "views": r["viewers"],
                 "contacts": r["contacters"], "negotiations": r["negotiators"], "deals": r["deal_buyers"]}
                for r in ranked["rankings"]["viewed"]]
        table("top_listings", [
            {"key": "label", "label": "Listing", "unit": "text"},
            {"key": "category", "label": "Category", "unit": "text"},
            {"key": "county", "label": "County", "unit": "text"},
            {"key": "views", "label": "Viewers", "unit": "count"},
            {"key": "contacts", "label": "In contact", "unit": "count"},
            {"key": "negotiations", "label": "Negotiating", "unit": "count"},
            {"key": "deals", "label": "Agreed a deal", "unit": "count"},
        ], rows, chart_column="views", visual="table")

    if "sellers" in categories:
        perf = ctx.extras.get(("perf", start, end)) or await market_intel.performance(
            ctx.db, start=start, end=end, now=ctx.now, limit=10)
        rows = [{"label": s.get("name") or "Unnamed seller", "listings": s["listings_engaged"],
                 "views": s["viewers"], "contacts": s["contacters"], "rate": s["contact_rate_pct"],
                 "deals": s["deals_agreed"], "completed": s.get("deals_completed"),
                 "reply_minutes": s.get("avg_first_reply_minutes")} for s in perf["sellers"]["top"]]
        table("top_sellers", [
            {"key": "label", "label": "Seller", "unit": "text"},
            {"key": "listings", "label": "Listings engaged", "unit": "count"},
            {"key": "views", "label": "Viewers", "unit": "count"},
            {"key": "contacts", "label": "In contact", "unit": "count"},
            {"key": "rate", "label": "Contact rate", "unit": "pct"},
            {"key": "deals", "label": "Deals agreed", "unit": "count"},
            {"key": "completed", "label": "Completed", "unit": "count"},
            {"key": "reply_minutes", "label": "First reply (min)", "unit": "minutes"},
        ], rows, visual="table")

    if "revenue" in categories:
        from api.core.revenue import revenue_report
        report = await revenue_report(ctx.db, from_raw=start.isoformat(), to_raw=end.isoformat(), bucket="day")
        ctx.extras[("revenue_daily", start, end)] = report
        rows = [{"label": s["label"], "amount_minor": s["amount_minor"], "count": s["count"]}
                for s in report["income"]["streams"]]
        table("income_by_stream", [
            {"key": "label", "label": "Stream", "unit": "text"},
            {"key": "amount_minor", "label": "Income", "unit": "kes_minor"},
            {"key": "count", "label": "Payments", "unit": "count"},
        ], rows, chart_column="amount_minor")
        counts = ctx.extras.get(("payments", start, end))
        if counts is None:
            from api.core.operations import payment_counts
            counts = await payment_counts(ctx.db, start, end)
        rows = [{"label": "M-Pesa (Daraja)" if src == "mpesa" else "ZetuPay", **values}
                for src, values in sorted(counts["by_source"].items())]
        table("payments_by_provider", [
            {"key": "label", "label": "Provider", "unit": "text"},
            {"key": "success", "label": "Succeeded", "unit": "count"},
            {"key": "failed", "label": "Failed", "unit": "count"},
            {"key": "pending", "label": "Pending", "unit": "count"},
            {"key": "success_pct", "label": "Success rate", "unit": "pct"},
        ], rows, visual="table")

    if "trust" in categories:
        overview = ctx.extras.get(("complaints", start, end))
        if overview is None:
            from api.core.intelligence import complaints_overview
            overview = await complaints_overview(ctx.db, start, end, now=ctx.now)
        rows = [{"label": k.replace("_", " ").capitalize(), "count": v}
                for k, v in sorted((overview.get("by_category") or {}).items(), key=lambda kv: (-kv[1], kv[0]))]
        table("complaints_by_category", [
            {"key": "label", "label": "Category", "unit": "text"},
            {"key": "count", "label": "Complaints", "unit": "count"},
        ], rows, chart_column="count", note="Complaints filed in the period, by the category the user chose.")

    if "system" in categories:
        agg = ctx.extras.get(("ops", start, end))
        if agg is not None:
            from api.core import request_metrics
            rows = []
            for name, e in sorted(agg["endpoints"].items(), key=lambda kv: (-kv[1]["requests"], kv[0]))[:12]:
                rows.append({"label": name, "requests": e["requests"], "server_errors": e["server_errors"],
                             "p95_ms": request_metrics.histogram_quantile(0.95, e["histogram"])})
            table("busiest_endpoints", [
                {"key": "label", "label": "Endpoint", "unit": "text"},
                {"key": "requests", "label": "Requests", "unit": "count"},
                {"key": "server_errors", "label": "5xx", "unit": "count"},
                {"key": "p95_ms", "label": "p95 (ms)", "unit": "ms"},
            ], rows, visual="table",
                note="Each process records its 25 busiest routes a minute, so rarely called routes may be missing.")
    return tables


# ── Anomalies ───────────────────────────────────────────────────────────────

async def _anomalies(ctx: _Context, start: datetime, end: datetime) -> dict:
    from api.core import market_models

    today = _midnight(ctx.now.date())
    # Whole days only: from the first midnight in the range to the last one,
    # and never today, which is not over.
    range_first = _midnight(start.date()) if start == _midnight(start.date()) else _midnight(start.date()) + timedelta(days=1)
    last = min(_midnight(end.date()), today)
    first = max(range_first, last - timedelta(days=ANOMALY_MAX_DAYS))
    if first >= last:
        return {"evaluated_days": 0, "from": None, "to": None, "items": [],
                "note": "No complete UTC day in the period to check."}
    found = []
    for key, spec in market_models._specs().items():
        values = await market_models.daily_counts(ctx.db, spec, first - timedelta(days=7 * ANOMALY_WEEKS), last)
        day = first
        while day < last:
            verdict = market_models.evaluate(values, day.date(), weeks=ANOMALY_WEEKS, concern=spec["concern"],
                                             label=spec["label"], source=spec["source"], action=spec["action"],
                                             key=key)
            if verdict["anomaly"]:
                a = verdict["anomaly"]
                found.append({"metric": key, "label": a["label"], "date": a["date"], "direction": a["direction"],
                              "observed": a["observed"], "baseline_median": a["baseline"]["median"],
                              "z_score": a["deviation"]["z_score"], "pct": a["deviation"]["pct"],
                              "severity": a["severity"], "impact": a["impact"], "source": a["evidence"]["source"],
                              "suggested_action": a["suggested_action"]})
            day += timedelta(days=1)
    found.sort(key=lambda a: (-abs(a["z_score"]), a["date"], a["metric"]))
    days = (last - first).days
    note = (f"Each day checked against the same weekday in the {ANOMALY_WEEKS} weeks before it (median and MAD)."
            + (f" Only the last {ANOMALY_MAX_DAYS} days of the period were checked." if first > range_first else ""))
    return {"evaluated_days": days, "from": iso(first), "to": iso(last), "items": found[:MAX_ANOMALIES],
            "total": len(found), "note": note}


# ── Text ────────────────────────────────────────────────────────────────────

def fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "–"
    if unit == "pct":
        return f"{value:.1f}%".replace(".0%", "%")
    if unit == "kes_minor":
        return f"KES {value / 100:,.0f}"
    if unit == "minutes":
        return f"{value:,.1f} min".replace(".0 min", " min")
    if unit == "ms":
        return f"{value:,.0f} ms"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _change_text(k: dict) -> str:
    if k["unit"] == "pct":
        return f"{k['delta']:+.1f} points"
    return f"{k['change_pct']:+.0f}%"


def _important_changes(kpis: list[dict]) -> list[dict]:
    out = []
    for k in kpis:
        if not k["available"] or k["kind"] == "point" or k["value"] is None or k["previous"] is None:
            continue
        if k["unit"] == "pct":
            if k["delta"] is None or abs(k["delta"]) < CHANGE_MIN_POINTS:
                continue
            size = abs(k["delta"])
        else:
            base = max(abs(k["value"]), abs(k["previous"]))
            threshold = CHANGE_MIN_BASE * (100 if k["unit"] == "kes_minor" else 1)
            if k["change_pct"] is None or abs(k["change_pct"]) < CHANGE_MIN_PCT or base < threshold:
                continue
            size = abs(k["change_pct"])
        rising = k["delta"] > 0
        direction = "neutral" if k["good"] == "neutral" else (
            "better" if (rising and k["good"] == "up") or (not rising and k["good"] == "down") else "worse")
        out.append({"key": k["key"], "label": k["label"], "unit": k["unit"], "value": k["value"],
                    "previous": k["previous"], "delta": k["delta"], "change_pct": k["change_pct"],
                    "direction": direction, "_size": size})
    out.sort(key=lambda c: (-c["_size"], c["key"]))
    for c in out:
        c.pop("_size")
    return out[:MAX_CHANGES]


def _observations(changes: list[dict], anomalies: dict, comparison_label: Optional[str]) -> list[dict]:
    out = []
    for c in changes:
        if c["direction"] != "worse":
            continue
        m = METRIC_BY_KEY[c["key"]]
        verb = "rose" if c["delta"] > 0 else "fell"
        out.append({
            "text": (f"{c['label']} {verb} {_change_text(c).lstrip('+-')} against {comparison_label}: "
                     f"{fmt(c['previous'], c['unit'])} to {fmt(c['value'], c['unit'])}."),
            "action": m.action,
            "evidence": [{"metric": c["key"], "label": c["label"], "value": c["value"], "previous": c["previous"],
                          "unit": c["unit"], "source": m.origin}],
        })
    for a in anomalies.get("items", [])[:3]:
        out.append({
            "text": (f"{a['label']} on {a['date']}: {a['observed']:g} against a usual {a['baseline_median']:g} "
                     f"for that weekday (z {a['z_score']:g})."),
            "action": a["suggested_action"],
            "evidence": [{"metric": a["metric"], "label": a["label"], "value": a["observed"],
                          "previous": a["baseline_median"], "unit": "count", "source": a["source"]}],
        })
    return out[:MAX_OBSERVATIONS]


def _summary(title: str, period: dict, comparison: Optional[dict], kpis: list[dict], changes: list[dict],
             anomalies: dict, unavailable: list[dict]) -> list[str]:
    lines = [f"{title} covers {period['label']} (UTC){' so far' if period['in_progress'] else ''}"
             + (f", compared with {comparison['label']}." if comparison else ".")]
    if changes:
        parts = [f"{c['label']} {fmt(c['value'], c['unit'])} ({_change_text(c)})" for c in changes[:3]]
        lines.append("Biggest moves: " + "; ".join(parts) + ".")
    elif comparison:
        lines.append(f"No measured figure moved by {CHANGE_MIN_PCT:g}% or more against {comparison['label']}.")
    worse = [c for c in changes if c["direction"] == "worse"]
    if worse:
        lines.append("Needs attention: " + ", ".join(c["label"].lower() for c in worse[:4]) + ".")
    if anomalies.get("items"):
        labels = sorted({a["label"].lower() for a in anomalies["items"]})
        lines.append(f"{anomalies.get('total', len(anomalies['items']))} day(s) stood out against the usual "
                     f"weekday: {', '.join(labels[:4])}.")
    if unavailable:
        lines.append(f"{len(unavailable)} figure(s) could not be measured for this period: "
                     + ", ".join(u["label"].lower() for u in unavailable[:5]) + ".")
    measured = sum(1 for k in kpis if k["available"])
    lines.append(f"{measured} of {len(kpis)} figures measured.")
    return lines


# ── Build ───────────────────────────────────────────────────────────────────

def _metric_out(m: Metric, value, previous, *, available: bool, reason: Optional[str], filtered: bool) -> dict:
    if not available:
        value = previous = None
    if m.kind == "point":
        previous = None
    delta = round(value - previous, 2) if value is not None and previous is not None else None
    change = None
    if m.unit != "pct" and value is not None and previous not in (None, 0):
        change = round((value - previous) / abs(previous) * 100, 1)
    return {"key": m.key, "label": m.label, "category": m.category, "unit": m.unit, "good": m.good,
            "kind": m.kind, "value": value, "previous": previous, "delta": delta, "change_pct": change,
            "available": available, "unavailable_reason": reason, "definition": m.definition,
            "source": m.origin, "filtered": filtered}


async def build(db: AsyncSession, spec: dict, *, start: datetime, end: datetime,
                now: Optional[datetime] = None) -> dict:
    """The report for a normalized spec over [start, end)."""
    now = now or datetime.utcnow()
    if (end - start).days > MAX_RANGE_DAYS:
        raise SpecError(f"a report covers at most {MAX_RANGE_DAYS} days")
    filters = spec.get("filters") or {}
    ctx = _Context(db, now, filters)
    compare = comparison_range(spec["comparison"], start, end)
    metrics = [METRIC_BY_KEY[k] for k in spec["metrics"]]
    has_filter = bool(filters.get("category") or filters.get("county"))

    kpis = []
    for m in metrics:
        values = await ctx.values(m.source, start, end)
        reason = ctx.unavailable[(m.source, start, end)].get(m.key)
        previous = None
        if compare is not None and m.kind != "point":
            prev_values = await ctx.values(m.source, *compare)
            previous = prev_values.get(m.key)
            # A comparison that could not be measured is no comparison.
            if ctx.unavailable[(m.source, *compare)].get(m.key):
                previous = None
        kpis.append(_metric_out(m, values.get(m.key), previous, available=reason is None, reason=reason,
                                filtered=has_filter and bool(m.filters)))

    group_by = spec["group_by"]
    viz = spec["visualization"]
    series = []
    if group_by in TIME_GROUPINGS:
        buckets = time_buckets(start, end, group_by)
        for m in metrics:
            if m.series is None:
                continue
            k = next(x for x in kpis if x["key"] == m.key)
            if not k["available"]:
                continue
            values = await _series_values(ctx, m.series, start, end, buckets, group_by)
            series.append({
                "key": m.key, "label": m.label, "unit": m.unit, "bucket": group_by,
                "visualization": "bar" if viz == "bar" else "table" if viz == "table" else "line",
                "points": [{"t": iso(b_start), "v": v, "days": max(1, round((b_end - b_start).total_seconds() / 86400, 2))}
                           for (b_start, b_end), v in zip(buckets, values)],
                "note": "Null means not measured, never zero." if any(v is None for v in values) else None,
            })

    categories = set(spec.get("categories") or []) | {m.category for m in metrics}
    tables = await _tables(ctx, spec, start, end, categories)
    anomalies = await _anomalies(ctx, start, end)
    changes = _important_changes(kpis) if compare is not None else []

    period = {"from": iso(start), "to": iso(end), "label": period_label(start, end),
              "days": round((end - start).total_seconds() / 86400, 2), "timezone": "UTC", "in_progress": end >= now}
    comparison = None
    if compare is not None:
        comparison = {"kind": spec["comparison"], "from": iso(compare[0]), "to": iso(compare[1]),
                      "label": ("the period before" if spec["comparison"] == "previous_period" else "a year earlier")
                      + f" ({period_label(*compare)})"}
    unavailable = [{"key": k["key"], "label": k["label"], "reason": k["unavailable_reason"]}
                   for k in kpis if not k["available"]]

    # The business category is a preset of other categories' metrics, so each
    # figure is shown once, in the section it belongs to.
    section_list = []
    for c in CATEGORIES:
        keys = [k["key"] for k in kpis if METRIC_BY_KEY[k["key"]].category == c["key"]]
        if not keys:
            continue
        section_list.append({"key": c["key"], "title": c["label"], "kpis": keys,
                             "series": [s["key"] for s in series if s["key"] in keys]})

    notes = list(GENERAL_NOTES)
    if ctx.extras.get("stages_truncated"):
        notes.append("Listing stage reads hit the row cap; those counts may be understated.")
    payload = {
        "version": PAYLOAD_VERSION,
        "title": spec["title"],
        "spec": spec,
        "period": period,
        "comparison": comparison,
        "generated_at": iso(now),
        "data_as_of": iso(min(now, end)),
        "executive_summary": _summary(spec["title"], period, comparison, kpis, changes, anomalies, unavailable),
        "kpis": kpis,
        "sections": section_list,
        "series": series,
        "tables": tables,
        "changes": changes,
        "anomalies": anomalies,
        "observations": _observations(changes, anomalies, comparison["label"] if comparison else None),
        "unavailable": unavailable,
        "filters": {**filters, "applies_to": [m.key for m in metrics if m.filters] if has_filter else [],
                    "note": None if not has_filter else
                    "Filters apply to listing performance and the category and county breakdowns only."},
        "definitions": {k["key"]: k["definition"] for k in kpis},
        "sources": {k["key"]: k["source"] for k in kpis},
        "notes": notes,
    }
    return payload


# ── Runs ────────────────────────────────────────────────────────────────────

def run_meta(run) -> dict:
    payload = run.payload or {}
    kpis = payload.get("kpis") or []
    return {
        "id": run.id, "definition_id": run.definition_id, "title": run.title,
        "period_start": iso(run.period_start), "period_end": iso(run.period_end),
        "period_label": (payload.get("period") or {}).get("label"),
        "comparison_start": iso(run.comparison_start) if run.comparison_start else None,
        "comparison_end": iso(run.comparison_end) if run.comparison_end else None,
        "trigger": run.trigger, "regenerated_from": run.regenerated_from,
        "generated_by": run.generated_by, "generated_at": iso(run.generated_at),
        "status": run.status, "error": run.error,
        "measured": sum(1 for k in kpis if k.get("available")), "metrics": len(kpis),
        "delivery": run.delivery or [],
    }


def run_detail(run) -> dict:
    return {**run_meta(run), "spec": run.spec, "payload": run.payload}


async def store_run(db: AsyncSession, *, spec: dict, start: datetime, end: datetime, trigger: str,
                    generated_by: str, definition_id: Optional[str] = None,
                    regenerated_from: Optional[str] = None, now: Optional[datetime] = None):
    """Build and add one run; the caller commits. A build that raises
    propagates, and nothing is added."""
    from api.models.report_builder import ReportRun

    now = now or datetime.utcnow()
    compare = comparison_range(spec["comparison"], start, end)
    payload = await build(db, spec, start=start, end=end, now=now)
    run = ReportRun(id=str(uuid.uuid4()), definition_id=definition_id, title=spec["title"], spec=spec, period_start=start,
                    period_end=end, comparison_start=compare[0] if compare else None,
                    comparison_end=compare[1] if compare else None, trigger=trigger,
                    regenerated_from=regenerated_from, generated_by=generated_by, generated_at=now,
                    status="ready", payload=payload)
    db.add(run)
    return run


# ── Definitions and schedules ───────────────────────────────────────────────

def definition_out(d) -> dict:
    return {
        "id": d.id, "name": d.name, "description": d.description, "spec": d.spec,
        "schedule": d.schedule, "schedule_every_days": d.schedule_every_days,
        "schedule_hour_utc": d.schedule_hour_utc, "schedule_weekday": d.schedule_weekday,
        "schedule_day": d.schedule_day, "recipients": d.recipients or [], "enabled": bool(d.enabled),
        "next_run_at": iso(d.next_run_at) if d.next_run_at else None,
        "last_run_at": iso(d.last_run_at) if d.last_run_at else None,
        "last_run_id": d.last_run_id, "last_status": d.last_status, "last_error": d.last_error,
        "created_by": d.created_by, "created_at": iso(d.created_at),
        "updated_by": d.updated_by, "updated_at": iso(d.updated_at), "version": d.version,
    }


def validate_schedule(schedule: str, *, every_days: Optional[int], hour: int, weekday: Optional[int],
                      day: Optional[int]) -> None:
    from api.models.report_builder import SCHEDULES

    if schedule not in SCHEDULES:
        raise SpecError(f"schedule must be one of {', '.join(SCHEDULES)}")
    if not 0 <= int(hour) <= 23:
        raise SpecError("schedule_hour_utc must be 0 to 23")
    if schedule == "custom" and (every_days is None or not 2 <= every_days <= 90):
        raise SpecError("a custom schedule runs every 2 to 90 days")
    if schedule == "weekly" and (weekday is None or not 0 <= weekday <= 6):
        raise SpecError("a weekly schedule needs a weekday, 0 (Monday) to 6 (Sunday)")
    if schedule == "monthly" and (day is None or not 1 <= day <= 28):
        raise SpecError("a monthly schedule needs a day of the month, 1 to 28")


def next_run_after(d, after: datetime) -> Optional[datetime]:
    """The first scheduled moment strictly after `after`, in UTC."""
    if d.schedule == "none" or not d.enabled:
        return None
    hour = int(d.schedule_hour_utc or 0)
    base = datetime(after.year, after.month, after.day, hour)
    if d.schedule == "daily":
        return base if base > after else base + timedelta(days=1)
    if d.schedule == "weekly":
        candidate = base + timedelta(days=(int(d.schedule_weekday) - base.weekday()) % 7)
        return candidate if candidate > after else candidate + timedelta(days=7)
    if d.schedule == "monthly":
        candidate = datetime(after.year, after.month, int(d.schedule_day), hour)
        if candidate <= after:
            year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
            candidate = datetime(year, month, int(d.schedule_day), hour)
        return candidate
    # custom: every N days from the last run, or from today
    every = timedelta(days=int(d.schedule_every_days or 7))
    if d.last_run_at is not None:
        candidate = datetime(d.last_run_at.year, d.last_run_at.month, d.last_run_at.day, hour) + every
        while candidate <= after:
            candidate += every
        return candidate
    return base if base > after else base + timedelta(days=1)


def scheduled_period(d, run_at: datetime) -> tuple[datetime, datetime]:
    """The complete UTC days a scheduled run reports on: the day, the 7 days,
    the calendar month or the N days before the run."""
    day = _midnight(run_at.date())
    if d.schedule == "daily":
        return day - timedelta(days=1), day
    if d.schedule == "weekly":
        return day - timedelta(days=7), day
    if d.schedule == "monthly":
        first = datetime(run_at.year, run_at.month, 1)
        prev = datetime(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
        return prev, first
    return day - timedelta(days=int(d.schedule_every_days or 7)), day


def email_text(title: str, payload: dict, link: Optional[str]) -> tuple[str, str]:
    """The subject and plain-text body of a scheduled report's email:
    aggregates only, never a name, a number or a message."""
    period = payload.get("period") or {}
    subject = f"BROKA report: {title} ({period.get('label', '')} UTC)"[:200]
    subject = " ".join(subject.split())
    lines = [title, f"{period.get('label', '')} (UTC)", ""]
    lines += payload.get("executive_summary") or []
    lines.append("")
    for k in (payload.get("kpis") or [])[:20]:
        if not k.get("available"):
            lines.append(f"{k['label']}: not measured ({k.get('unavailable_reason') or 'unavailable'})")
            continue
        change = ""
        if k.get("previous") is not None and k.get("delta") is not None:
            change = (f" ({k['delta']:+.1f} points)" if k["unit"] == "pct" else
                      f" ({k['change_pct']:+.0f}%)" if k.get("change_pct") is not None else "")
        lines.append(f"{k['label']}: {fmt(k.get('value'), k['unit'])}{change}")
    lines.append("")
    if link:
        lines.append(f"The full report, with charts, tables and definitions: {link}")
    else:
        lines.append("The full report, with charts, tables and definitions, is in the admin dashboard's report archive.")
    lines.append("Only aggregate figures are sent by email; no personal data.")
    return subject, "\n".join(lines)[:9_500]


async def email_run(run_id: str) -> list[dict]:
    """Email a stored run to its definition's administrators. Run after the
    run's commit, with no transaction open across the sends (AGENTS.md)."""
    from api.core.config import settings
    from api.core.email import send_admin_email
    from api.database import AsyncSessionLocal, User
    from api.models.report_builder import ReportDefinition, ReportRun

    async with AsyncSessionLocal() as db:
        run = await db.get(ReportRun, run_id)
        if run is None or not run.definition_id:
            return []
        definition = await db.get(ReportDefinition, run.definition_id)
        ids = [str(i) for i in ((definition.recipients if definition else None) or [])][:10]
        if not ids:
            return []
        users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()}
        targets = []
        for uid in ids:
            user = users.get(uid)
            if user is None or not user.is_admin:
                targets.append((uid, None, "not_an_administrator"))
            elif not user.email:
                targets.append((uid, None, "no_email"))
            else:
                targets.append((uid, user.email, None))
        payload, title = run.payload or {}, run.title
    link = f"{settings.admin_dashboard_url}/reports/builder?run={run_id}" if settings.admin_dashboard_url else None
    subject, message = email_text(title, payload, link)
    results = []
    for uid, address, failure in targets:
        if failure:
            results.append({"user_id": uid, "status": "skipped", "failure": failure})
            continue
        key = hashlib.sha256(f"report-run:{run_id}:{uid}".encode()).hexdigest()[:48]
        try:
            outcome = await send_admin_email(to=address, subject=subject, message=message, provider_key=key)
        except ValueError:
            results.append({"user_id": uid, "status": "skipped", "failure": "invalid_address"})
            continue
        results.append({"user_id": uid, "status": "sent" if outcome.ok else "failed",
                        "failure": None if outcome.ok else outcome.failure})
    async with AsyncSessionLocal() as db:
        run = await db.get(ReportRun, run_id)
        if run is not None:
            run.delivery = results
            from api.core.audit import record_audit
            counts = Counter(r["status"] for r in results)
            await record_audit(db, "system", "report_run_emailed", "report_run", run_id,
                               detail=f'{{"sent":{counts.get("sent", 0)},"failed":{counts.get("failed", 0)},'
                                      f'"skipped":{counts.get("skipped", 0)}}}')
            await db.commit()
    return results


async def run_due_schedules(*, now: Optional[datetime] = None, limit: int = 10) -> list[str]:
    """Run every saved report whose schedule is due, once each. Called by the
    5-minute sweep (one replica at a time, under its lease). A failed build
    is stored as a failed run, raises an admin alert and does not stop the
    next definition; its next run is still scheduled."""
    from api.core.audit import record_audit
    from api.core.config import settings
    from api.database import AsyncSessionLocal
    from api.models.report_builder import ReportDefinition, ReportRun

    if not settings.admin_reports_enabled:
        return []
    now = now or datetime.utcnow()
    async with AsyncSessionLocal() as db:
        due = [row[0] for row in (await db.execute(
            select(ReportDefinition.id).where(
                ReportDefinition.deleted_at.is_(None), ReportDefinition.enabled == True,  # noqa: E712
                ReportDefinition.schedule != "none", ReportDefinition.next_run_at.is_not(None),
                ReportDefinition.next_run_at <= now)
            .order_by(ReportDefinition.next_run_at).limit(limit)
        )).all()]
    written: list[str] = []
    for definition_id in due:
        failure: Optional[str] = None
        name = ""
        async with AsyncSessionLocal() as db:
            d = await db.get(ReportDefinition, definition_id)
            if d is None or d.deleted_at is not None or not d.enabled or d.next_run_at is None or d.next_run_at > now:
                continue
            due_at = d.next_run_at
            start, end = scheduled_period(d, due_at)
            try:
                spec = normalize_spec(dict(d.spec or {}), require_range=False)
                run = await store_run(db, spec=spec, start=start, end=end, trigger="schedule",
                                      generated_by="system", definition_id=d.id, now=now)
                run_id = run.id
            except Exception as exc:
                failure = type(exc).__name__[:200]
                logger.error("[report-builder] scheduled run failed definition=%s type=%s", definition_id, failure)
                await db.rollback()
                d = await db.get(ReportDefinition, definition_id)
                if d is None:
                    continue
                run_id = str(uuid.uuid4())
                db.add(ReportRun(id=run_id, definition_id=d.id, title=(d.spec or {}).get("title") or d.name,
                                 spec=d.spec or {}, period_start=start, period_end=end, trigger="schedule",
                                 generated_by="system", generated_at=now, status="failed", error=failure))
            d.last_status, d.last_error = ("failed", failure) if failure else ("ready", None)
            d.last_run_at = now
            d.last_run_id = run_id
            # Due again at the next moment after this one: a run missed while
            # the service was down is made once, not once per missed slot.
            d.next_run_at = next_run_after(d, max(now, due_at))
            name = d.name
            await record_audit(db, "system", "report_schedule_ran", "report_definition", d.id,
                               detail=f'{{"run_id":"{run_id}","status":"{d.last_status}"}}')
            await db.commit()
        if failure:
            await _alert_failure(definition_id, name, failure)
            continue
        written.append(run_id)
        try:
            await email_run(run_id)
        except Exception as exc:
            logger.error("[report-builder] report email failed run=%s type=%s", run_id, type(exc).__name__)
    return written


async def _alert_failure(definition_id: str, name: str, error: str) -> None:
    try:
        from api.core import admin_alert_subscribers
        await admin_alert_subscribers._create(
            "report_schedule_failed", "medium", f"Scheduled report failed: {name}"[:160],
            body=f"The scheduled report could not be built ({error}). It will try again at its next run.",
            href="/reports/builder", meta={"definition_id": definition_id, "error": error})
    except Exception as exc:
        logger.warning("[report-builder] failure alert not raised type=%s", type(exc).__name__)
