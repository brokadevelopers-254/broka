"""The Command Center: where BROKA stands, in five groups of measured figures.

Every figure says what it counts, over which period, and how it compares with
the period before. A figure BROKA does not measure is reported unavailable
with the reason, never estimated.

Periods: the current period is [now - window_days, now) and the previous one
the same length immediately before it - the windows of the marketplace pulse
(api/core/intelligence.pulse_between), whose figures are reused here, so the
two can never disagree. Administrator accounts are left out of the counts of
accounts, listings, activity and engagement, as in the pulse; payments,
income and refunds are counted as the payments queue and the revenue report
record them.

Kinds of figure:
  period         counted inside the period, compared with the previous period.
  point_in_time  the state now. Its previous value is the state when the period
                 began, where the history to rebuild it is kept, else null.
  live           measured as the request is answered (a probe, or this
                 process's own request counters), compared where it can be.

`delta` is value minus previous, in the figure's unit (percentage points for a
percent). `change_pct` is the relative change of a count or an amount; it is
null for percentages, and when the previous value is zero or unknown.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import distinct, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import activity, intelligence, request_metrics
from api.core.insights import MAX_INSIGHTS, SEVERITY_ORDER, _insight, build_insights, severity_counts

PAYMENT_STALE_AFTER = timedelta(minutes=15)
PAYMENT_STALE_LOOKBACK = timedelta(hours=24)
SCHEDULER_STALE_AFTER_SECONDS = 180
_PENDING_PAYMENT_STATUSES = ("pending", "initiated", "processing")

# Decision thresholds. Each signal names its evidence; none acts on an account.
FAILURE_SHARE_WARN_PCT = 20.0
FAILURE_SHARE_RISE_PP = 10.0
FAILURE_MIN_COUNT = 3
ACTIVE_DROP_WARN_PCT = -20.0
ACTIVE_DROP_MIN_PREVIOUS = 10
API_ERROR_WARN_PCT = 5.0
API_ERROR_MIN_REQUESTS = 50


def _iso(value: Optional[datetime]) -> Optional[str]:
    return intelligence.iso(value)


def metric(
    key: str,
    label: str,
    *,
    value,
    previous=None,
    unit: str = "count",
    kind: str = "period",
    good: str = "up",
    definition: str,
    caption: Optional[str] = None,
    href: Optional[str] = None,
    available: bool = True,
    unavailable_reason: Optional[str] = None,
) -> dict:
    """One figure. Not available means BROKA does not measure it here; a null
    value that is available means there was nothing to measure (no payments
    to take a success rate of), and the caption says so."""
    if not available:
        value = previous = None
    delta = None
    if value is not None and previous is not None:
        delta = round(value - previous, 2)
    change = None
    if unit != "percent" and value is not None and previous is not None:
        change = intelligence.change_pct(value, previous)
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "unit": unit,
        "good": good,
        "value": value,
        "previous": previous,
        "delta": delta,
        "change_pct": change,
        "definition": definition,
        "caption": caption,
        "href": href,
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100, 1) if whole else None


def _pulse_metric(pulse: dict, key: str) -> tuple:
    figure = pulse["metrics"][key]
    return figure["current"], figure["previous"]


def _status_text(value) -> Optional[str]:
    return getattr(value, "value", value)


def _as_datetime(value) -> Optional[datetime]:
    """MAX() over a DateTime column is a datetime on PostgreSQL and may be text on SQLite."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", ""))


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def operational_signals(figures: dict) -> list[dict]:
    """Decisions from operational figures, each traceable to the numbers it shows.

    Pure: takes the figures command_center measured, so the thresholds can be
    tested without a database. Every signal points somewhere to look; none
    changes an account, a payment or a setting."""
    out: list[dict] = []

    age = figures.get("scheduler_age_seconds")
    if age is not None and age > SCHEDULER_STALE_AFTER_SECONDS:
        out.append(_insight(
            "ops_scheduler_stale", "critical", "operations",
            f"Scheduled work last ran {round(age / 60)} minutes ago",
            "The one-minute scheduler writes a presence sample every pass. None has been "
            "written for over three minutes, so session expiry, suspension expiry and the "
            "other scheduled sweeps are probably not running on any replica.",
            action=("Service health", "/service-health"),
            evidence={"last_sample_at": figures.get("scheduler_last_sample_at"),
                      "age_seconds": age, "threshold_seconds": SCHEDULER_STALE_AFTER_SECONDS},
            weight=2.0,
        ))

    pay = figures.get("payments") or {}
    current, previous = pay.get("current") or {}, pay.get("previous") or {}
    failed, succeeded = int(current.get("failed", 0)), int(current.get("success", 0))
    share = _rate(failed, failed + succeeded)
    previous_share = _rate(int(previous.get("failed", 0)),
                           int(previous.get("failed", 0)) + int(previous.get("success", 0)))
    rose = share is not None and previous_share is not None and share - previous_share >= FAILURE_SHARE_RISE_PP
    if failed >= FAILURE_MIN_COUNT and share is not None and (share >= FAILURE_SHARE_WARN_PCT or rose):
        compared = f", up from {previous_share:g}% the period before" if previous_share is not None else ""
        out.append(_insight(
            "ops_payment_failures", "warning", "payments",
            f"{failed} payments failed: {share:g}% of completed attempts{compared}",
            "Look for a provider outage, a wrong amount or an expired prompt pattern before "
            "asking anyone to pay again. Pending payments are not counted either way.",
            action=("Failed payments", "/payments?status=failed"),
            evidence={"failed": failed, "succeeded": succeeded, "failure_share_pct": share,
                      "previous_failure_share_pct": previous_share},
            weight=1.0,
        ))

    stale = int(figures.get("stale_payments") or 0)
    if stale:
        out.append(_insight(
            "ops_payment_prompts_waiting", "warning", "payments",
            f"{stale} payment prompt{'s' if stale != 1 else ''} unanswered after 15 minutes",
            "Prompts from the last 24 hours that neither succeeded nor failed. A provider "
            "callback may be delayed or lost; the payments queue shows each one.",
            action=("Payments", "/payments"),
            evidence={"waiting": stale, "older_than_minutes": 15, "lookback_hours": 24},
            weight=1.0,
        ))

    active = figures.get("active_users") or {}
    now_active, before_active = active.get("current"), active.get("previous")
    if now_active is not None and before_active and before_active >= ACTIVE_DROP_MIN_PREVIOUS:
        change = intelligence.change_pct(now_active, before_active)
        if change is not None and change <= ACTIVE_DROP_WARN_PCT:
            out.append(_insight(
                "ops_active_users_down", "warning", "growth",
                f"Active accounts down {abs(change):g}% ({now_active} against {before_active})",
                "Fewer accounts used the app in the foreground than in the period before. "
                "Check for a broken release, a sign-in problem or a paused campaign.",
                action=("User intelligence", "/analytics/users"),
                evidence={"current": now_active, "previous": before_active, "change_pct": change},
                weight=1.0,
            ))

    api = figures.get("api") or {}
    requests, error_rate = int(api.get("requests") or 0), api.get("error_rate_pct")
    if requests >= API_ERROR_MIN_REQUESTS and error_rate is not None and error_rate >= API_ERROR_WARN_PCT:
        out.append(_insight(
            "ops_api_errors", "warning", "operations",
            f"{error_rate:g}% of requests failed on this API process in 15 minutes",
            "Server errors among the requests this process answered. Other replicas keep "
            "their own counts; the logs name each failing request.",
            action=("Service health", "/service-health"),
            evidence={"requests": requests, "server_errors": api.get("server_errors"),
                      "error_rate_pct": error_rate},
            weight=1.0,
        ))
    return out


async def command_center(db: AsyncSession, *, window_days: int = 7, now: Optional[datetime] = None) -> dict:
    from api.core.message_delivery import RETENTION_DAYS as DELIVERY_RETENTION_DAYS
    from api.database import (
        Deal, FraudEvent, Listing, ListingLike, ListingView, User, Wishlist, pool_status,
    )
    from api.domains.admin.router import _payment_union
    from api.models.account_moderation import AccountModerationEvent
    from api.models.dispute import CaseState, DisputeCase
    from api.models.message_delivery import DELIVERED, PUSH_FAILED, READ, MessageDelivery
    from api.models.scam_report import AccountRestriction, ScamReportDetail
    from api.models.user_activity import ActivitySample, UserActivityDay

    now = now or datetime.utcnow()
    width = timedelta(days=window_days)
    cs, ce = now - width, now
    ps, pe = cs - width, cs
    period = f"{window_days} day{'s' if window_days != 1 else ''}"
    staff = select(User.id).where(User.is_admin == True)  # noqa: E712

    pulse = await intelligence.pulse_between(db, cs, ce, now=now)

    async def count(column, *where) -> int:
        return int((await db.execute(select(func.count(column)).where(*where))).scalar() or 0)

    # ── Platform growth ──────────────────────────────────────────────────
    def registered_before(moment: datetime):
        return or_(User.created_at.is_(None), User.created_at < moment)

    users_now = await count(User.id, User.is_admin == False, registered_before(ce))  # noqa: E712
    users_at_start = await count(User.id, User.is_admin == False, registered_before(cs))  # noqa: E712
    users_at_previous_start = await count(User.id, User.is_admin == False, registered_before(ps))  # noqa: E712
    new_c, new_p = _pulse_metric(pulse, "new_users")

    coverage = await activity._coverage_start(db)

    def covered(start: datetime) -> bool:
        # The first recorded day may be partial, so a period is measured only
        # when tracking had begun before the day the period starts on.
        return coverage is not None and coverage < start.date()

    async def active_accounts(start: datetime, end: datetime) -> int:
        # A day row spans first_active_at..last_active_at inside one UTC day,
        # and a period is at least a day long, so a span overlapping the
        # period has an activity moment inside it.
        return int((await db.execute(
            select(func.count(distinct(UserActivityDay.user_id))).where(
                UserActivityDay.last_active_at >= start,
                UserActivityDay.first_active_at < end,
                UserActivityDay.user_id.not_in(staff),
            )
        )).scalar() or 0)

    async def returning_accounts(start: datetime, end: datetime) -> int:
        earlier = select(UserActivityDay.user_id).where(UserActivityDay.first_active_at < start)
        return int((await db.execute(
            select(func.count(distinct(UserActivityDay.user_id))).where(
                UserActivityDay.last_active_at >= start,
                UserActivityDay.first_active_at < end,
                UserActivityDay.user_id.not_in(staff),
                UserActivityDay.user_id.in_(earlier),
            )
        )).scalar() or 0)

    active_c = await active_accounts(cs, ce) if covered(cs) else None
    active_p = await active_accounts(ps, pe) if covered(ps) else None
    returning_c = await returning_accounts(cs, ce) if covered(cs) else None
    returning_p = await returning_accounts(ps, pe) if covered(ps) else None
    if coverage is None:
        activity_reason = "No foreground activity has been recorded yet."
    else:
        activity_reason = (
            f"Activity tracking began on {coverage.isoformat()}, too recently to measure the "
            "whole period; a partial count would look like a fall."
        )

    growth = [
        metric("registered_users", "Registered accounts", value=users_now, previous=users_at_start,
               kind="point_in_time", href="/users",
               definition="Accounts registered so far, administrators excluded. Previous: the total when the period began."),
        metric("new_registrations", "New registrations", value=new_c, previous=new_p, href="/users",
               definition=f"Accounts created in the last {period}, administrators excluded."),
        metric("registration_growth", "Registration growth", unit="percent",
               value=_rate(new_c, users_at_start), previous=_rate(new_p, users_at_previous_start),
               href="/growth",
               definition="New registrations in the period as a share of the accounts registered when it began."),
        metric("active_users", "Active accounts", value=active_c, previous=active_p,
               href="/analytics/users", available=coverage is not None and active_c is not None,
               unavailable_reason=None if active_c is not None else activity_reason,
               definition=("Accounts that sent a foreground heartbeat in the period. Background app "
                           "activity and sign-ins alone do not count.")),
        metric("returning_users", "Returning accounts", value=returning_c, previous=returning_p,
               href="/analytics/users", available=coverage is not None and returning_c is not None,
               unavailable_reason=None if returning_c is not None else activity_reason,
               definition=("Active accounts in the period that had also been active before it began, "
                           "as far back as activity tracking goes.")),
    ]

    # ── Marketplace health ───────────────────────────────────────────────
    def listed_before(moment: datetime):
        return or_(Listing.created_at.is_(None), Listing.created_at < moment)

    listings_now = await count(Listing.id, Listing.seller_id.not_in(staff), listed_before(ce))
    listings_at_start = await count(Listing.id, Listing.seller_id.not_in(staff), listed_before(cs))
    live_now, _ = _pulse_metric(pulse, "live_listings")
    new_listings_c, new_listings_p = _pulse_metric(pulse, "new_listings")
    sold_out_c, sold_out_p = _pulse_metric(pulse, "sold_out_reports")
    starts_c, starts_p = _pulse_metric(pulse, "conversations_started")

    async def unique_views(start: datetime, end: datetime) -> int:
        pairs = (
            select(ListingView.viewer_id, ListingView.listing_id)
            .where(ListingView.created_at >= start, ListingView.created_at < end,
                   ListingView.viewer_id.not_in(staff))
            .distinct().subquery()
        )
        return int((await db.execute(select(func.count()).select_from(pairs))).scalar() or 0)

    async def saves_and_likes(start: datetime, end: datetime) -> int:
        saves = await count(Wishlist.id, Wishlist.created_at >= start, Wishlist.created_at < end,
                            Wishlist.user_id.not_in(staff))
        likes = await count(ListingLike.id, ListingLike.created_at >= start, ListingLike.created_at < end,
                            ListingLike.user_id.not_in(staff))
        return saves + likes

    reply = pulse["seller_response"]
    marketplace = [
        metric("total_listings", "Listings posted", value=listings_now, previous=listings_at_start,
               kind="point_in_time", href="/listings",
               definition="Every listing ever posted by a non-staff seller, whatever its status now."),
        metric("live_listings", "Live listings", value=live_now, kind="point_in_time", href="/listings?status=active",
               definition="Active listings buyers can see now: paid for where a fee applies, and not an auction while auctions are off.",
               caption="Point in time: listing status history is not kept, so there is no previous value."),
        metric("new_listings", "New listings", value=new_listings_c, previous=new_listings_p, href="/listings",
               definition=f"Listings posted in the last {period} by non-staff sellers."),
        metric("unavailable_reports", "Reported unavailable", value=sold_out_c, previous=sold_out_p,
               good="down", href="/intelligence",
               definition="Buyer reports that a listing was no longer available, not withdrawn by the buyer."),
        metric("unique_listing_views", "Unique listing views",
               value=await unique_views(cs, ce), previous=await unique_views(ps, pe), href="/market",
               definition="Distinct viewer and listing pairs: one person opening one listing ten times counts once."),
        metric("saves_and_likes", "Saves and likes",
               value=await saves_and_likes(cs, ce), previous=await saves_and_likes(ps, pe), href="/market",
               definition="Wishlist saves plus likes added in the period by non-staff accounts."),
        metric("conversations_started", "Buyer conversations started", value=starts_c, previous=starts_p,
               href="/intelligence",
               definition="Listing chats a buyer opened in the period. Zeno's own messages do not start one."),
        metric("seller_reply_rate", "Seller replies within 24 h", unit="percent",
               value=reply["current"]["rate_pct"], previous=reply["previous"]["rate_pct"], href="/intelligence",
               caption=f"{reply['current']['replied_24h']} of {reply['current']['eligible']} eligible conversations",
               definition=intelligence.DEFINITIONS["seller_reply"]),
    ]

    # ── Financial performance ────────────────────────────────────────────
    income_c, income_p = _pulse_metric(pulse, "income_minor")

    def stream(which: str, key: str) -> int:
        return sum(s["amount_minor"] for s in pulse["revenue"][which]["streams"] if s["key"] == key)

    payments_union = _payment_union()

    async def payment_counts(start: datetime, end: datetime) -> dict:
        rows = (await db.execute(
            select(payments_union.c.status, func.count())
            .where(payments_union.c.created_at >= start, payments_union.c.created_at < end)
            .group_by(payments_union.c.status)
        )).all()
        out = {"success": 0, "failed": 0, "pending": 0}
        for status, n in rows:
            name = _status_text(status)
            if name == "success":
                out["success"] += int(n or 0)
            elif name == "failed":
                out["failed"] += int(n or 0)
            elif name in _PENDING_PAYMENT_STATUSES:
                out["pending"] += int(n or 0)
        return out

    pay_c, pay_p = await payment_counts(cs, ce), await payment_counts(ps, pe)
    refunds_c = await count(Deal.id, Deal.refunded_at >= cs, Deal.refunded_at < ce)
    refunds_p = await count(Deal.id, Deal.refunded_at >= ps, Deal.refunded_at < pe)
    financial = [
        metric("revenue", "Income", unit="kes_minor", value=income_c, previous=income_p, href="/revenue",
               definition="Money users paid BROKA in the period: fees, plans, boosts, badges and Daraja deal commission. Not deal money held in escrow."),
        metric("listing_fee_revenue", "Listing fee income", unit="kes_minor",
               value=stream("current", "listing_fee"), previous=stream("previous", "listing_fee"), href="/revenue",
               definition="The listing-fee stream of income in the period."),
        metric("successful_payments", "Successful payments", value=pay_c["success"], previous=pay_p["success"],
               href="/payments?status=success",
               definition="M-Pesa and ZetuPay payment attempts started in the period that succeeded, as on the payments queue."),
        metric("failed_payments", "Failed payments", value=pay_c["failed"], previous=pay_p["failed"], good="down",
               href="/payments?status=failed",
               definition="Payment attempts started in the period that failed: cancelled, timed out, refused or the wrong amount."),
        metric("payment_success_rate", "Payment success rate", unit="percent",
               value=_rate(pay_c["success"], pay_c["success"] + pay_c["failed"]),
               previous=_rate(pay_p["success"], pay_p["success"] + pay_p["failed"]), href="/payments",
               caption=(f"{pay_c['pending']} still pending, not counted" if pay_c["pending"]
                        else ("No completed payment attempts in the period" if not pay_c["success"] + pay_c["failed"] else None)),
               definition="Successful attempts out of attempts that finished (succeeded or failed). Pending attempts are left out."),
        metric("refunds", "Deal refunds", value=refunds_c, previous=refunds_p, good="down", href="/deals?status=refunded",
               definition="Deals whose escrow was refunded to the buyer in the period. E-Confirm holds that money, not BROKA."),
    ]

    # ── Trust and safety ─────────────────────────────────────────────────
    flagged_now = await count(User.id, User.is_flagged == True, User.is_admin == False)  # noqa: E712
    suspended_now = await count(
        User.id, User.account_status == "suspended",
        or_(User.suspended_until.is_(None), User.suspended_until > now))
    restricted_now = await count(User.id, User.account_status == "restricted")
    # Scam reports' protective restrictions are a separate mechanism (scoped,
    # always expiring - api/domains/scam_reports); counted beside, not inside.
    protective_now = await count(
        AccountRestriction.id, AccountRestriction.status == "active",
        AccountRestriction.expires_at > now, AccountRestriction.lifted_at.is_(None))

    history = (await db.execute(
        select(AccountModerationEvent.user_id, AccountModerationEvent.to_status,
               AccountModerationEvent.suspended_until)
        .where(AccountModerationEvent.created_at < cs)
        .order_by(AccountModerationEvent.created_at, AccountModerationEvent.id)
    )).all()
    last_before_start: dict[str, tuple] = {}
    for user_id, to_status, until in history:
        last_before_start[user_id] = (to_status, until)
    suspended_at_start = sum(
        1 for to_status, until in last_before_start.values()
        if to_status == "suspended" and (until is None or until > cs)
    )

    open_case_states = [state for state in CaseState if not state.is_terminal]
    open_disputes = await count(DisputeCase.id, DisputeCase.state.in_(open_case_states))
    disputes_c, _ = _pulse_metric(pulse, "disputes_opened")
    open_fraud = await count(FraudEvent.id, or_(FraudEvent.resolved.is_(None), FraudEvent.resolved == False))  # noqa: E712
    fraud_c, fraud_p = _pulse_metric(pulse, "fraud_flags")
    scam_c = await count(ScamReportDetail.feedback_id, ScamReportDetail.created_at >= cs, ScamReportDetail.created_at < ce)
    scam_p = await count(ScamReportDetail.feedback_id, ScamReportDetail.created_at >= ps, ScamReportDetail.created_at < pe)
    suspensions_c = await count(AccountModerationEvent.id, AccountModerationEvent.action == "suspended",
                                AccountModerationEvent.created_at >= cs, AccountModerationEvent.created_at < ce)
    suspensions_p = await count(AccountModerationEvent.id, AccountModerationEvent.action == "suspended",
                                AccountModerationEvent.created_at >= ps, AccountModerationEvent.created_at < pe)
    complaints = pulse["metrics"]["complaints_filed"]
    trust = [
        metric("flagged_accounts", "Flagged accounts", value=flagged_now, kind="point_in_time", good="down",
               href="/users?type=flagged",
               definition="Accounts an administrator or the fraud engine flagged. A flag records a concern; it does not block anything.",
               caption="Point in time: flag history is not kept."),
        metric("suspended_accounts", "Suspended accounts", value=suspended_now, previous=suspended_at_start,
               kind="point_in_time", good="down", href="/users",
               caption=(f"{restricted_now} restricted by an administrator (can sign in, cannot transact); "
                        f"{protective_now} under scam-report protective restrictions"),
               definition="Accounts refused sign-in and every authenticated request now. Previous: suspended when the period began, from the moderation history."),
        metric("suspensions_applied", "Suspensions applied", value=suspensions_c, previous=suspensions_p,
               good="neutral", href="/audit-logs",
               definition="Suspensions administrators applied in the period, from the moderation history."),
        metric("open_disputes", "Open disputes", value=open_disputes, kind="point_in_time", good="down", href="/disputes",
               caption=f"{disputes_c} opened in the period",
               definition="Dispute cases not yet closed, escalated ones waiting for a person included."),
        metric("open_fraud_events", "Open fraud events", value=open_fraud, kind="point_in_time", good="down", href="/fraud",
               caption=f"{fraud_c} raised in the period",
               definition="Fraud flags recorded by the engine that no administrator has resolved yet."),
        metric("fraud_flags", "Fraud flags raised", value=fraud_c, previous=fraud_p, good="down", href="/fraud",
               definition="Fraud events recorded in the period."),
        metric("scam_reports", "Scam reports", value=scam_c, previous=scam_p, good="down", href="/complaints",
               definition="Reports of a scam attempt filed by users in the period. Suspicious activity reported by people, not inferred."),
        metric("complaints_filed", "Complaints filed", value=complaints["current"], previous=complaints["previous"],
               good="down", href="/complaints",
               definition="Complaints users filed through the app in the period."),
    ]

    # ── Technical operations ─────────────────────────────────────────────
    probe_started = time.perf_counter()
    await db.execute(text("SELECT 1"))
    db_round_trip_ms = round((time.perf_counter() - probe_started) * 1000, 1)
    pool = pool_status()

    newest_sample = _as_datetime((await db.execute(select(func.max(ActivitySample.sampled_at)))).scalar())
    # A sample stamped after `now` (clock skew between replicas) is fresh, not negative.
    scheduler_age = max(0, int((now - newest_sample).total_seconds())) if newest_sample is not None else None

    api = request_metrics.recorder.summary()
    api_c, api_p = api["current"], api["previous"] or {}
    api_caption = (
        f"{api_c['requests']} requests answered by this process in 15 minutes"
        + ("" if api["current_complete"] else f"; process started {api['process_started_at']}")
    )

    stale_payments = int((await db.execute(
        select(func.count()).select_from(payments_union).where(
            payments_union.c.created_at >= now - PAYMENT_STALE_LOOKBACK,
            payments_union.c.created_at < now - PAYMENT_STALE_AFTER,
            payments_union.c.status.in_(_PENDING_PAYMENT_STATUSES),
        )
    )).scalar() or 0)

    # A day inside the retention limit: rows near it may already be pruned, and a
    # half-pruned period would read as a worse delivery rate than it was.
    delivery_horizon = now - timedelta(days=DELIVERY_RETENTION_DAYS - 1)

    async def delivery(start: datetime, end: datetime) -> Optional[dict]:
        if start < delivery_horizon:
            return None
        rows = (await db.execute(
            select(MessageDelivery.status, func.count())
            .where(MessageDelivery.message_created_at >= start, MessageDelivery.message_created_at < end)
            .group_by(MessageDelivery.status)
        )).all()
        by_status = {str(status): int(n or 0) for status, n in rows}
        total = sum(by_status.values())
        return {
            "total": total,
            "confirmed": by_status.get(DELIVERED, 0) + by_status.get(READ, 0),
            "push_failed": by_status.get(PUSH_FAILED, 0),
        }

    delivery_c, delivery_p = await delivery(cs, ce), await delivery(ps, pe)
    operations = [
        metric("api_availability", "API availability", unit="percent", kind="live", available=False,
               unavailable_reason="Needs an external uptime monitor: the API cannot record the time it was down.",
               href="/service-health",
               definition="Share of time the API answered its health check, measured from outside."),
        metric("api_error_rate", "API server errors", unit="percent", kind="live", good="down",
               value=api_c["error_rate_pct"], previous=api_p.get("error_rate_pct"), href="/service-health",
               caption=api_caption,
               definition=("Responses with status 500-599 among the requests this process answered in the last 15 "
                           "minutes, against the 15 before. Process-local: each replica counts its own, and the "
                           "counts reset when it restarts.")),
        metric("api_latency_p95", "API latency p95", unit="ms", kind="live", good="down",
               value=api_c["p95_ms"], previous=api_p.get("p95_ms"), href="/service-health",
               caption=(f"p50 {api_c['p50_ms']} ms · p99 {api_c['p99_ms']} ms" if api_c["p50_ms"] is not None
                        else "No requests in the last 15 minutes"),
               definition="95th percentile of request time on this process over the last 15 minutes, from at most 500 sampled requests a minute."),
        metric("database_round_trip", "Database round trip", unit="ms", kind="live", good="down",
               value=db_round_trip_ms, href="/service-health",
               caption=(f"{pool.get('checked_out', '?')} of {pool.get('max_connections', '?')} pooled connections in use"),
               definition="Time for SELECT 1 on this request's connection, as the page loads."),
        metric("scheduled_work", "Scheduled work last ran", unit="seconds", kind="live", good="down",
               value=scheduler_age, href="/service-health",
               caption=("Healthy under 3 minutes" if scheduler_age is not None else "No scheduler pass recorded yet"),
               definition=("Age of the newest presence sample. The one-minute scheduler writes one every pass on "
                           "whichever replica holds its lease, so this shows whether scheduled work runs anywhere.")),
        metric("payments_waiting", "Payment prompts waiting", kind="point_in_time", good="down",
               value=stale_payments, href="/payments",
               definition="Payment attempts from the last 24 hours still pending after 15 minutes: a callback may be late or lost."),
        metric("messages_confirmed", "Messages on the recipient's phone", unit="percent", good="up",
               value=_rate(delivery_c["confirmed"], delivery_c["total"]) if delivery_c else None,
               previous=_rate(delivery_p["confirmed"], delivery_p["total"]) if delivery_p else None,
               available=delivery_c is not None,
               unavailable_reason=None if delivery_c else f"Delivery rows are kept {DELIVERY_RETENTION_DAYS} days.",
               href="/service-health",
               caption=(f"{delivery_c['push_failed']} push failures among {delivery_c['total']} chat messages"
                        if delivery_c else None),
               definition=("Chat messages the recipient's app confirmed receiving or reading. Firebase accepting a "
                           "push is not counted: it does not prove the phone got it.")),
    ]

    figures = {
        "scheduler_age_seconds": scheduler_age,
        "scheduler_last_sample_at": _iso(newest_sample),
        "payments": {"current": pay_c, "previous": pay_p},
        "stale_payments": stale_payments,
        "active_users": {"current": active_c, "previous": active_p},
        "api": api_c,
    }
    categories = await intelligence.categories_between(
        db, now - timedelta(days=max(window_days, 30)), now, now=now)
    sellers = await intelligence.sellers_overview(db, now=now, limit=10)
    signals = operational_signals(figures)
    decisions = signals + build_insights(pulse=pulse, categories=categories, sellers=sellers)
    decisions.sort(key=lambda d: (SEVERITY_ORDER[d["severity"]], -d.get("_weight", 0.0), d["id"]))
    for decision in decisions:
        decision.pop("_weight", None)
    decisions = decisions[:MAX_INSIGHTS + len(signals)]

    return {
        "generated_at": _iso(now),
        "window": {
            "days": window_days, "from": _iso(cs), "to": _iso(ce),
            "previous_from": _iso(ps), "previous_to": _iso(pe),
        },
        "staff_excluded": True,
        "activity_coverage_started_on": _as_date(coverage).isoformat() if coverage else None,
        "groups": [
            {"key": "growth", "title": "Platform growth", "metrics": growth},
            {"key": "marketplace", "title": "Marketplace health", "metrics": marketplace},
            {"key": "financial", "title": "Financial performance", "metrics": financial},
            {"key": "trust", "title": "Trust and safety", "metrics": trust},
            {"key": "operations", "title": "Technical operations", "metrics": operations},
        ],
        "decisions": {
            "generated_at": _iso(now),
            "window_days": window_days,
            "insights": decisions,
            "counts": severity_counts(decisions),
        },
        "pulse": pulse,
    }
