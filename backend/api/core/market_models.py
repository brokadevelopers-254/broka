"""Anomaly detection, validated forecasts, churn / supply / payment models
and data-traceable recommendations for the admin dashboard.

Nothing here acts. An anomaly is an alert with the numbers behind it; no
account is ever suspended, flagged or changed by this module.

Anomaly method: for the last complete UTC day, the baseline is the same
weekday in each of the previous N weeks (default 6). The baseline centre is
their median and the spread is MAD x 1.4826 (floored so a flat history does
not make every wobble look extreme). A day is anomalous when the robust
z-score reaches Z_THRESHOLD AND the absolute gap is big enough to matter,
AND it moves in the direction that metric is worried about (a drop in
messages, a spike in failed payments).

Forecasts are same-weekday medians. A forecast is only published when it
passed a backtest on the last 7 complete days (WAPE under the limit and no
worse than "same as last week"); otherwise it is withheld with the reason.

API and database health have no durable history (only per-process
counters), so they are reported as not instrumented rather than guessed.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.admin_alerts import create_alert
from api.core.intelligence import _AGREED_STATES, ROW_CAP, _staff, categories_between, iso
from api.database import (
    Deal, FraudEvent, Interest, Listing, ListingLike, ListingView, MpesaStatus,
    MpesaTransaction, NegotiationMessage, User, Wishlist,
)
from api.models.message_delivery import PUSH_FAILED, MessageDelivery

Z_THRESHOLD = 3.5
MIN_ABSOLUTE = 3
MIN_RELATIVE = 0.25
HIGH_Z = 6.0
FORECAST_MAX_WAPE = 0.6
FORECAST_MIN_VOLUME = 14
MIN_PAYMENT_SAMPLE = 20

NOT_INSTRUMENTED = [
    {"metric": "api", "reason": "Request latency and error counts are process-local and reset on restart; no durable history to baseline."},
    {"metric": "database", "reason": "Pool and slow-query counters are process-local; no durable history to baseline."},
]


def _specs() -> dict[str, dict]:
    staff = _staff()
    return {
        "registrations": dict(
            label="New registrations", column=User.created_at, conds=[User.is_admin == False],  # noqa: E712
            concern="both", source="users.created_at",
            action="Check the sign-up and OTP flow; for a spike, review the new accounts for bot patterns before any action."),
        "messages": dict(
            label="Buyer and seller messages", column=NegotiationMessage.created_at,
            conds=[NegotiationMessage.role.in_(("buyer", "seller")), NegotiationMessage.sender_id.not_in(staff)],
            concern="drop", source="negotiation_messages.created_at",
            action="Check chat delivery and push health, then the app release history."),
        "listings": dict(
            label="New listings", column=Listing.created_at, conds=[Listing.seller_id.not_in(staff)],
            concern="drop", source="listings.created_at",
            action="Check the listing flow (photos, payment, categories) and recent app releases."),
        "views": dict(
            label="First-time listing views", column=ListingView.created_at,
            conds=[ListingView.viewer_id.not_in(staff)], concern="drop", source="listing_views.created_at",
            action="Check feed and search availability; check whether supply or traffic fell."),
        "deals": dict(
            label="Deals agreed", column=Deal.created_at, conds=[Deal.status.in_(list(_AGREED_STATES))],
            concern="drop", source="deals.created_at",
            action="Read recent negotiations for a blocking step; check escrow and payment errors."),
        "payments_failed": dict(
            label="Failed M-Pesa payments", column=MpesaTransaction.created_at,
            conds=[MpesaTransaction.status == MpesaStatus.failed], concern="spike",
            source="mpesa_transactions.created_at (status=failed)",
            action="Check Daraja status and callback delivery; review last_error on the failed rows."),
        "payments_success": dict(
            label="Successful M-Pesa payments", column=MpesaTransaction.created_at,
            conds=[MpesaTransaction.status == MpesaStatus.success], concern="drop",
            source="mpesa_transactions.created_at (status=success)",
            action="Check Daraja status and the STK push flow."),
        "fraud_flags": dict(
            label="Fraud flags", column=FraudEvent.created_at, conds=[], concern="spike",
            source="fraud_events.created_at",
            action="Review the flagged users in the fraud queue; resolve or escalate by hand."),
        "push_failures": dict(
            label="Failed push notifications", column=MessageDelivery.created_at,
            conds=[MessageDelivery.status == PUSH_FAILED], concern="spike",
            source="message_deliveries.created_at (status=PUSH_FAILED)",
            action="Check FCM configuration and device-token health."),
        "calls": dict(
            label="Call log entries", column=NegotiationMessage.created_at,
            conds=[NegotiationMessage.msg_type == "call", NegotiationMessage.sender_id.not_in(staff)],
            concern="drop", source="negotiation_messages.created_at (msg_type=call)",
            action="Check TURN / signalling health and recent app releases."),
    }


async def daily_counts(db: AsyncSession, spec: dict, start: datetime, end: datetime) -> dict[str, int]:
    day = func.date(spec["column"])
    rows = (await db.execute(
        select(day, func.count()).where(spec["column"] >= start, spec["column"] < end, *spec["conds"])
        .group_by(day))).all()
    return {str(d)[:10]: int(n) for d, n in rows}


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time.min)


def _baseline(values: dict[str, int], target: date, weeks: int) -> list[tuple[str, int]]:
    out = []
    for k in range(1, weeks + 1):
        key = (target - timedelta(days=7 * k)).isoformat()
        out.append((key, values.get(key, 0)))
    return out


def evaluate(values: dict[str, int], target: date, *, weeks: int = 6, concern: str = "both",
             label: str = "Metric", source: str = "", action: str = "", key: str = "") -> dict:
    """The verdict for one day of one metric. `anomaly` is None when it is normal."""
    base = _baseline(values, target, weeks)
    nums = [v for _, v in base]
    observed = values.get(target.isoformat(), 0)
    centre = statistics.median(nums)
    mad = statistics.median([abs(v - centre) for v in nums])
    scale = max(1.4826 * mad, 1.0, 0.1 * centre)
    deviation = observed - centre
    z = round(deviation / scale, 2)
    direction = "spike" if deviation > 0 else "drop"
    pct = round(deviation / centre * 100, 1) if centre else None
    checked = {"key": key, "label": label, "observed": observed, "baseline_median": centre,
               "z_score": z, "anomaly": False}
    big_enough = abs(deviation) >= max(MIN_ABSOLUTE, MIN_RELATIVE * centre)
    wanted = concern == "both" or concern == direction
    if abs(z) < Z_THRESHOLD or not big_enough or not wanted:
        return {"checked": checked, "anomaly": None}
    weekday = target.strftime("%A")
    word = "fewer" if direction == "drop" else "more"
    impact = f"{abs(deviation):g} {word} than the typical {weekday} ({label.lower()})"
    if pct is not None:
        impact += f", {abs(pct):g}% {'below' if direction == 'drop' else 'above'} normal"
    checked["anomaly"] = True
    return {"checked": checked, "anomaly": {
        "metric": key, "label": label, "date": target.isoformat(), "direction": direction,
        "observed": observed,
        "baseline": {"method": "same-weekday median / MAD", "weeks": weeks, "median": centre,
                     "mad": mad, "scale": round(scale, 2),
                     "values": [{"date": d, "value": v} for d, v in base]},
        "deviation": {"absolute": deviation, "pct": pct, "z_score": z},
        "impact": impact,
        "evidence": {"source": source, "observed_day": {"date": target.isoformat(), "value": observed},
                     "baseline_days": [{"date": d, "value": v} for d, v in base]},
        "suggested_action": action,
        "severity": "high" if abs(z) >= HIGH_Z or (pct is not None and abs(pct) >= 70) else "medium",
        "auto_action": "none: BROKA never suspends or changes an account from an anomaly.",
    }}


async def detect(db: AsyncSession, *, weeks: int = 6, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    target = now.date() - timedelta(days=1)
    start, end = _midnight(target - timedelta(days=7 * weeks)), _midnight(target + timedelta(days=1))
    anomalies, checked = [], []
    for key, spec in _specs().items():
        values = await daily_counts(db, spec, start, end)
        verdict = evaluate(values, target, weeks=weeks, concern=spec["concern"], label=spec["label"],
                           source=spec["source"], action=spec["action"], key=key)
        checked.append(verdict["checked"])
        if verdict["anomaly"]:
            anomalies.append(verdict["anomaly"])
    anomalies.sort(key=lambda a: (a["severity"] != "high", -abs(a["deviation"]["z_score"])))
    return {
        "generated_at": iso(now), "evaluated_day": target.isoformat(), "baseline_weeks": weeks,
        "thresholds": {"z_score": Z_THRESHOLD, "min_absolute": MIN_ABSOLUTE, "min_relative": MIN_RELATIVE},
        "anomalies": anomalies, "checked": checked, "not_instrumented": NOT_INSTRUMENTED,
        "definitions": {
            "baseline": "The same weekday in each of the previous weeks; centre = median, spread = 1.4826 x MAD.",
            "anomaly": "Robust z-score at or above the threshold, a meaningful absolute gap, in the direction the metric is worried about.",
            "action": "Alerts never suspend, flag or change any account or payment.",
        },
    }


def _alert_body(a: dict) -> str:
    base = ", ".join(f"{v['date']}: {v['value']}" for v in a["baseline"]["values"])
    return (f"Baseline: {a['baseline']['median']:g} (median of the same weekday over {a['baseline']['weeks']} weeks; "
            f"MAD {a['baseline']['mad']:g}).\n"
            f"Deviation: {a['deviation']['absolute']:+g} ({a['deviation']['pct']}%), robust z = {a['deviation']['z_score']}.\n"
            f"Impact: {a['impact']}.\nEvidence ({a['evidence']['source']}): {base}.\n"
            f"Suggested action: {a['suggested_action']}\nNo automatic action was taken.")


async def scan_and_alert(db: AsyncSession, *, weeks: int = 6, now: Optional[datetime] = None) -> dict:
    """Run detection and raise one deduplicated admin alert per anomaly.
    Does not commit; the caller owns the transaction."""
    started = datetime.utcnow()
    result = await detect(db, weeks=weeks, now=now)
    created = 0
    for a in result["anomalies"]:
        meta = {"metric": a["metric"], "date": a["date"], "observed": a["observed"],
                "baseline_median": a["baseline"]["median"], "z_score": a["deviation"]["z_score"],
                "impact": a["impact"], "evidence": a["evidence"],
                "suggested_action": a["suggested_action"]}
        row = await create_alert(
            db, kind=f"anomaly_{a['metric']}", severity=a["severity"],
            title=f"{a['label']} {a['direction']} on {a['date']}: {a['observed']} vs typical {a['baseline']['median']:g}",
            body=_alert_body(a), href="/market", meta=meta)
        if row.created_at is not None and row.created_at >= started:
            created += 1
    return {"evaluated_day": result["evaluated_day"], "anomalies": len(result["anomalies"]),
            "alerts_created": created, "alerts_existing": len(result["anomalies"]) - created}


# ── Forecasts ────────────────────────────────────────────────────────────────

def _same_weekday(values: dict[str, int], day: date, weeks: int = 4) -> tuple[float, float, float]:
    xs = [values.get((day - timedelta(days=7 * k)).isoformat(), 0) for k in range(1, weeks + 1)]
    centre = statistics.median(xs)
    spread = 1.4826 * statistics.median([abs(x - centre) for x in xs]) * 1.28
    return centre, max(0.0, centre - spread), centre + spread


def forecast_metric(values: dict[str, int], today: date) -> dict:
    """Seven days ahead (today onward), published only if it backtests."""
    days = [today - timedelta(days=i) for i in range(7, 0, -1)]
    actual = [values.get(d.isoformat(), 0) for d in days]
    predicted = [_same_weekday(values, d)[0] for d in days]
    naive = [values.get((d - timedelta(days=7)).isoformat(), 0) for d in days]
    total = sum(actual)
    wape = round(sum(abs(a - p) for a, p in zip(actual, predicted)) / total, 3) if total else None
    naive_wape = round(sum(abs(a - n) for a, n in zip(actual, naive)) / total, 3) if total else None
    validated = bool(total >= FORECAST_MIN_VOLUME and wape is not None and wape <= FORECAST_MAX_WAPE
                     and wape <= naive_wape)
    reason = None
    if not validated:
        reason = (f"fewer than {FORECAST_MIN_VOLUME} events in the 7-day backtest" if total < FORECAST_MIN_VOLUME
                  else f"backtest error too high (WAPE {wape}) or worse than last-week-same-day ({naive_wape})")
    forecast = []
    if validated:
        for j in range(7):
            d = today + timedelta(days=j)
            expected, low, high = _same_weekday(values, d)
            forecast.append({"date": d.isoformat(), "expected": round(expected, 1),
                             "low": round(low, 1), "high": round(high, 1)})
    return {"validated": validated, "withheld_reason": reason,
            "backtest": {"days": 7, "actual_total": total, "forecast_total": round(sum(predicted), 1),
                         "wape": wape, "naive_wape": naive_wape},
            "forecast": forecast}


async def forecasts(db: AsyncSession, *, now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.utcnow()
    today = now.date()
    start, end = _midnight(today - timedelta(days=42)), _midnight(today)
    specs = _specs()
    out = []
    for key in ("registrations", "listings", "messages", "deals", "payments_success"):
        values = await daily_counts(db, specs[key], start, end)
        out.append({"metric": key, "label": specs[key]["label"], **forecast_metric(values, today)})
    return out


# ── Churn ────────────────────────────────────────────────────────────────────

def _days_between(days: set[str], lo: date, hi: date) -> int:
    a, b = lo.isoformat(), hi.isoformat()
    return sum(1 for d in days if a <= d < b)


def churn_flags(activity: dict[str, set[str]], cutoff: date) -> dict[str, tuple[int, int]]:
    """Users active on 3+ days in the 14 days before the last 14, whose last
    14 days (up to `cutoff`) held a quarter of that or less."""
    flagged = {}
    for uid, days in activity.items():
        prior = _days_between(days, cutoff - timedelta(days=28), cutoff - timedelta(days=14))
        recent = _days_between(days, cutoff - timedelta(days=14), cutoff)
        if prior >= 3 and recent * 4 <= prior:
            flagged[uid] = (prior, recent)
    return flagged


async def _activity_days(db: AsyncSession, since: datetime) -> tuple[dict[str, set[str]], bool]:
    staff = _staff()
    sources = (
        (NegotiationMessage.sender_id, NegotiationMessage.created_at,
         [NegotiationMessage.role.in_(("buyer", "seller"))]),
        (Listing.seller_id, Listing.created_at, []),
        (Interest.buyer_id, Interest.created_at, []),
        (ListingView.viewer_id, ListingView.created_at, []),
        (Wishlist.user_id, Wishlist.created_at, []),
        (ListingLike.user_id, ListingLike.created_at, []),
    )
    days: dict[str, set[str]] = defaultdict(set)
    truncated = False
    for ucol, tcol, conds in sources:
        day = func.date(tcol)
        rows = (await db.execute(
            select(ucol, day).where(tcol >= since, ucol.is_not(None), ucol.not_in(staff), *conds)
            .group_by(ucol, day).limit(ROW_CAP + 1))).all()
        truncated = truncated or len(rows) > ROW_CAP
        for uid, d in rows[:ROW_CAP]:
            days[uid].add(str(d)[:10])
    return days, truncated


def churn_backtest(activity: dict[str, set[str]], today: date) -> dict:
    """Apply the rule 14 days ago and see who really went quiet since."""
    cutoff = today - timedelta(days=14)
    flagged = churn_flags(activity, cutoff)
    prior_active = [u for u, d in activity.items()
                    if _days_between(d, cutoff - timedelta(days=28), cutoff - timedelta(days=14)) >= 3]
    quiet = lambda uid: _days_between(activity[uid], cutoff, today) == 0  # noqa: E731
    hits = sum(1 for u in flagged if quiet(u))
    precision = round(hits / len(flagged), 3) if flagged else None
    base_rate = round(sum(1 for u in prior_active if quiet(u)) / len(prior_active), 3) if prior_active else None
    validated = bool(len(flagged) >= 5 and precision is not None and precision >= 0.5
                     and base_rate is not None and precision > base_rate)
    return {"flagged": len(flagged), "went_quiet": hits, "precision": precision,
            "base_rate": base_rate, "validated": validated}


async def churn(db: AsyncSession, *, now: Optional[datetime] = None, limit: int = 20) -> dict:
    now = now or datetime.utcnow()
    today = now.date()
    activity, truncated = await _activity_days(db, _midnight(today - timedelta(days=42)))
    flagged = churn_flags(activity, today)
    ranked = sorted(flagged.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
    names = {}
    if ranked:
        names = dict((await db.execute(select(User.id, User.name).where(User.id.in_([u for u, _ in ranked])))).all())
    backtest = churn_backtest(activity, today)
    return {
        "method": "rule", "validated": backtest["validated"], "truncated": truncated,
        "rule": "Active on 3+ days in days 15-28 ago, and on at most a quarter as many days in the last 14.",
        "backtest": backtest, "at_risk_total": len(flagged),
        "at_risk": [{"user_id": u, "name": names.get(u), "active_days_prior_14d": p, "active_days_last_14d": r}
                    for u, (p, r) in ranked],
    }


# ── Supply and payments ──────────────────────────────────────────────────────

async def supply(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    report = await categories_between(db, now - timedelta(days=30), now, now=now)
    weeks = 30 / 7
    gaps = []
    for c in report["categories"]:
        weekly = c["demand"] / weeks
        needed = math.ceil(weekly - c["supply_live"])
        if needed > 0:
            gaps.append({"category": c["label"], "demand_30d": c["demand"], "weekly_demand": round(weekly, 1),
                         "live_listings": c["supply_live"], "suggested_new_listings": needed,
                         "signal": c["signal"]})
    gaps.sort(key=lambda g: (-g["suggested_new_listings"], g["category"]))
    return {"method": "rule", "rule": "Weekly demand (threads + interests + Buying Agent requests) minus live listings, over 30 days.",
            "gaps": gaps[:10]}


async def payments(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    today = now.date()
    specs = _specs()
    start, end = _midnight(today - timedelta(days=28)), _midnight(today)
    ok = await daily_counts(db, specs["payments_success"], start, end)
    bad = await daily_counts(db, specs["payments_failed"], start, end)
    weeks = []
    for w in range(4):
        days = [(today - timedelta(days=28 - 7 * w - i)).isoformat() for i in range(7)]
        s, f = sum(ok.get(d, 0) for d in days), sum(bad.get(d, 0) for d in days)
        weeks.append({"week_start": days[0], "success": s, "failed": f,
                      "failure_rate": round(f / (s + f), 3) if s + f else None})
    recent, prior = weeks[-1], weeks[:-1]
    prior_total = sum(w["success"] + w["failed"] for w in prior)
    prior_rate = round(sum(w["failed"] for w in prior) / prior_total, 3) if prior_total else None
    recent_total = recent["success"] + recent["failed"]
    if recent_total < MIN_PAYMENT_SAMPLE or prior_rate is None:
        status = "insufficient_data"
    elif recent["failure_rate"] >= prior_rate + 0.10:
        status = "rising"
    else:
        status = "stable"
    return {"method": "rule", "status": status, "weeks": weeks, "prior_failure_rate": prior_rate,
            "recent_failure_rate": recent["failure_rate"],
            "projection": "Next week's failure rate is projected as this week's (persistence); no trend is extrapolated.",
            "min_sample": MIN_PAYMENT_SAMPLE}


# ── Recommendations ──────────────────────────────────────────────────────────

def recommendations(anomalies: list[dict], forecast_rows: list[dict], churn_report: dict,
                    supply_report: dict, payment_report: dict) -> list[dict]:
    """Every recommendation carries the numbers it came from."""
    recs: list[dict] = []
    for a in anomalies:
        recs.append({"id": f"anomaly:{a['metric']}", "priority": a["severity"],
                     "title": f"{a['label']} {a['direction']} on {a['date']}",
                     "action": a["suggested_action"],
                     "evidence": [
                         {"label": "Observed", "value": a["observed"], "source": a["evidence"]["source"]},
                         {"label": "Typical same weekday", "value": a["baseline"]["median"], "source": a["evidence"]["source"]},
                         {"label": "Robust z-score", "value": a["deviation"]["z_score"], "source": "market anomalies"}]})
    if churn_report["at_risk_total"]:
        bt = churn_report["backtest"]
        recs.append({"id": "churn:lapsing", "priority": "medium" if churn_report["validated"] else "low",
                     "title": f"{churn_report['at_risk_total']} previously active users are going quiet",
                     "action": "Send a personal check-in or a relevant listing alert." + ("" if churn_report["validated"] else " The rule has not validated yet; treat as a lead, not a finding."),
                     "evidence": [{"label": "At risk", "value": churn_report["at_risk_total"], "source": "activity tables (42 days)"},
                                  {"label": "Backtest precision", "value": bt["precision"], "source": "rule applied 14 days ago"},
                                  {"label": "Base rate", "value": bt["base_rate"], "source": "all previously active users"}]})
    for gap in supply_report["gaps"][:3]:
        recs.append({"id": f"supply:{gap['category'].lower()}", "priority": "medium",
                     "title": f"Recruit sellers in {gap['category']}",
                     "action": f"About {gap['suggested_new_listings']} more live listings would match weekly demand.",
                     "evidence": [{"label": "Weekly demand", "value": gap["weekly_demand"], "source": "threads + interests + agent requests"},
                                  {"label": "Live listings", "value": gap["live_listings"], "source": "listings"}]})
    if payment_report["status"] == "rising":
        recs.append({"id": "payments:failure_rate", "priority": "high",
                     "title": "M-Pesa failure rate is rising",
                     "action": "Review failed intents and Daraja status.",
                     "evidence": [{"label": "Recent failure rate", "value": payment_report["recent_failure_rate"], "source": "mpesa_transactions"},
                                  {"label": "Prior failure rate", "value": payment_report["prior_failure_rate"], "source": "mpesa_transactions"}]})
    for f in forecast_rows:
        if not f["validated"]:
            continue
        expected = sum(p["expected"] for p in f["forecast"])
        last_week = f["backtest"]["actual_total"]
        if last_week and abs(expected - last_week) / last_week >= 0.25:
            recs.append({"id": f"forecast:{f['metric']}", "priority": "low",
                         "title": f"{f['label']} expected to {'fall' if expected < last_week else 'rise'} next week",
                         "action": "Plan capacity or promotion ahead of the change.",
                         "evidence": [{"label": "Expected next 7 days", "value": round(expected, 1), "source": "same-weekday median forecast"},
                                      {"label": "Last 7 days", "value": last_week, "source": "backtest window"},
                                      {"label": "Backtest WAPE", "value": f["backtest"]["wape"], "source": "last 7 days"}]})
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    recs.sort(key=lambda r: (order.get(r["priority"], 9), r["id"]))
    return recs


async def models(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    anomalies = (await detect(db, now=now))["anomalies"]
    forecast_rows = await forecasts(db, now=now)
    churn_report = await churn(db, now=now)
    supply_report = await supply(db, now=now)
    payment_report = await payments(db, now=now)
    return {
        "generated_at": iso(now), "forecasts": forecast_rows, "churn": churn_report,
        "supply": supply_report, "payments": payment_report,
        "recommendations": recommendations(anomalies, forecast_rows, churn_report, supply_report, payment_report),
        "definitions": {
            "forecast": "Same-weekday median of the last 4 weeks; published only if it passed a 7-day backtest.",
            "validated": "A model is validated when its backtest meets its stated bar; unvalidated output is labelled, never hidden.",
            "recommendations": "Each one lists the figures and tables it was derived from.",
        },
    }
