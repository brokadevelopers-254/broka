"""The admin operations workspace: is BROKA working, and if not, where not.

Sources, and what each can say:

  * ops_samples (api/core/ops_sampler.py): each API process's requests,
    4xx and 5xx answers, a latency histogram and its busiest route templates,
    database statement and pool counters, push results, queued-task counts
    and loop heartbeats, one row a minute. Nothing exists before sampling
    began, and a figure built on samples says so instead of showing zero.
  * Database rows, for any window: payments (M-Pesa intents and ZetuPay),
    ZetuPay webhook times, chat message delivery (message_deliveries) and
    call cards (negotiation_messages with msg_type "call", whose text is the
    call's outcome).
  * This process now: its pool and its last 15 minutes of requests.

Never claimed: FCM accepting a push is not delivery. Only a chat message the
app acknowledged (sync, chat, a push receipt or a read) counts as received.
A latency percentile over a window is estimated from histogram buckets.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import ops_metrics, request_metrics
from api.core.command_center import metric
from api.core.config import settings
from api.core.intelligence import _staff, iso

MAX_WINDOW_HOURS = 24 * 14
SAMPLE_CAP = 60_000
ROW_CAP = 20_000
STALE_PAYMENT_AFTER = timedelta(minutes=15)
PENDING_PAYMENT_STATUSES = ("pending", "initiated", "processing")
CALL_OUTCOMES = ("completed", "missed", "declined", "cancelled", "failed_to_connect")
MIN_ENDPOINT_REQUESTS = 20

# The loops api/core/workers.py runs, and how often each should come round.
def _loop_intervals() -> dict[str, int]:
    return {
        "periodic": 300,
        "nudge": 60,
        "message_delivery": 30,
        "activity": 60,
        "service_communications": int(getattr(settings, "service_communication_sweep_seconds", 15) or 15),
    }


LOOP_LABELS = {
    "periodic": "Five-minute sweep (deal timers, reconciliation, reports)",
    "nudge": "One-minute sweep (availability nudges, auctions)",
    "message_delivery": "Chat push retries",
    "activity": "Sessions and presence",
    "service_communications": "Service communications",
}

DEFINITIONS = {
    "window": "Half-open [from, to) in UTC; the comparison is the window of equal length just before.",
    "samples": ("Each API process writes one row a minute (ops_samples) with what it answered and ran. "
                "Figures built on samples cover only the minutes a process recorded."),
    "error_rate": "Answers with status 500-599 / all answers. A request that raised counts as 500.",
    "client_errors": "Answers with status 400-499: refused requests (a bad token, a missing record, validation).",
    "latency": ("From the request reaching the app to its response finishing, estimated from histogram "
                "buckets (edges in ms: " + ", ".join(str(b) for b in request_metrics.LATENCY_BOUNDS_MS)
                + "). Streams (/stream) are counted as requests but not timed."),
    "endpoint": "A route template such as GET /listings/{listing_id}; paths with ids are never stored.",
    "pool_utilization": ("Connections checked out / the pool's maximum at each sample (one moment a minute), "
                         "so it can miss a spike between samples; saturations count every time the pool was full."),
    "slow_query": "A statement that ran 500 ms or more (DB_SLOW_QUERY_MS). Its text is never stored.",
    "query_failures": "Statements the database refused or that failed: a constraint, a timeout, a lost connection.",
    "db_unavailable": "Requests answered 503 DB_UNAVAILABLE because no connection could be had.",
    "payment_success": "Successful / (successful + failed) payment attempts started in the window; pending ones are left out.",
    "callback_delay": "ZetuPay: its webhook's arrival minus the payment's creation. M-Pesa keeps no callback time.",
    "push_accepted": "FCM took the push for a phone. That is not delivery: nothing confirms the phone showed it.",
    "message_delivered": ("The app confirmed it had the chat message (it synced, opened the chat, acknowledged "
                          "the push or read it). The only device receipt BROKA has."),
    "calls": "Call cards written when a call ends: one per call, its outcome as the app's call history shows it.",
    "loop": "A background loop is stale when it has not finished a pass for three of its intervals.",
}

NOT_INSTRUMENTED = [
    {"metric": "call_connection_delay", "reason": "No per-call timing between answering and connecting is stored."},
    {"metric": "mpesa_callback_delay", "reason": "mpesa_transactions records no callback arrival time."},
    {"metric": "push_device_receipt", "reason": "Only chat messages have an app acknowledgement; other pushes (calls, alerts) do not."},
    {"metric": "db_waiting_connections", "reason": "The pool exposes no count of waiting requests; waits are counted as they end."},
    {"metric": "uptime", "reason": "The API cannot observe its own downtime; that needs an external monitor."},
]


# ── Samples ─────────────────────────────────────────────────────────────────

async def load_samples(db: AsyncSession, start: datetime, end: datetime) -> tuple[list[Any], bool]:
    from api.models.ops_sample import OpsSample

    rows = (await db.execute(
        select(OpsSample).where(OpsSample.period_start >= start, OpsSample.period_start < end)
        .order_by(OpsSample.period_start).limit(SAMPLE_CAP + 1)
    )).scalars().all()
    return list(rows[:SAMPLE_CAP]), len(rows) > SAMPLE_CAP


async def first_sample_at(db: AsyncSession) -> Optional[datetime]:
    from api.models.ops_sample import OpsSample

    return (await db.execute(select(func.min(OpsSample.period_start)))).scalar()


def _add(into: list[int], more: Iterable[int]) -> list[int]:
    more = list(more or [])
    if len(more) > len(into):
        into = into + [0] * (len(more) - len(into))
    return [a + (more[i] if i < len(more) else 0) for i, a in enumerate(into)]


def aggregate(samples: list[Any]) -> dict:
    """Totals over a set of samples, every process added together."""
    out = {
        "samples": len(samples), "instances": len({s.instance for s in samples}),
        "requests": 0, "server_errors": 0, "client_errors": 0,
        "histogram": [0] * (len(request_metrics.LATENCY_BOUNDS_MS) + 1),
        "counters": Counter(), "endpoints": {}, "utilization": [],
    }
    for s in samples:
        out["requests"] += int(s.requests or 0)
        out["server_errors"] += int(s.server_errors or 0)
        out["client_errors"] += int(s.client_errors or 0)
        out["histogram"] = _add(out["histogram"], s.latency_histogram or [])
        for name, value in (s.counters or {}).items():
            if isinstance(value, (int, float)):
                out["counters"][name] += int(value)
        if s.db_checked_out is not None and s.db_max_connections:
            out["utilization"].append(s.db_checked_out / s.db_max_connections * 100)
        for e in s.endpoints or []:
            name = e.get("route")
            if not name:
                continue
            into = out["endpoints"].setdefault(name, {
                "requests": 0, "server_errors": 0, "client_errors": 0, "timed": 0,
                "total_ms": 0.0, "max_ms": None, "histogram": [0] * (len(request_metrics.LATENCY_BOUNDS_MS) + 1)})
            into["requests"] += int(e.get("requests") or 0)
            into["server_errors"] += int(e.get("server_errors") or 0)
            into["client_errors"] += int(e.get("client_errors") or 0)
            hist = e.get("histogram") or []
            timed = sum(hist)
            into["histogram"] = _add(into["histogram"], hist)
            into["timed"] += timed
            if e.get("avg_ms") is not None:
                into["total_ms"] += float(e["avg_ms"]) * timed
            if e.get("max_ms") is not None:
                into["max_ms"] = max(into["max_ms"] or 0.0, float(e["max_ms"]))
    return out


def _counter(agg: dict, name: str) -> int:
    return int(agg["counters"].get(name, 0))


def _prefixed(agg: dict, prefix: str) -> dict[str, int]:
    return {name[len(prefix):]: int(v) for name, v in agg["counters"].items()
            if name.startswith(prefix) and v}


def _rate(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 2)


def _quantile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 1)


def _bucket_seconds(hours: float) -> int:
    if hours <= 6:
        return 300
    if hours <= 48:
        return 1800
    return 7200


def series(samples: list[Any], start: datetime, end: datetime, bucket_seconds: int) -> dict[str, list[dict]]:
    """Requests, 5xx rate and p95 per bucket. A bucket no process sampled is
    null, never zero: nothing was recorded for it."""
    count = max(1, math.ceil((end - start).total_seconds() / bucket_seconds))
    buckets: dict[int, list[Any]] = defaultdict(list)
    for s in samples:
        index = int((s.period_start - start).total_seconds() // bucket_seconds)
        if 0 <= index < count:
            buckets[index].append(s)
    out: dict[str, list[dict]] = {"requests": [], "error_rate": [], "p95_ms": [], "push_failed": [],
                                  "task_failures": []}
    for index in range(count):
        t = iso(start + timedelta(seconds=index * bucket_seconds))
        rows = buckets.get(index)
        if not rows:
            for key in out:
                out[key].append({"t": t, "v": None})
            continue
        agg = aggregate(rows)
        out["requests"].append({"t": t, "v": agg["requests"]})
        out["error_rate"].append({"t": t, "v": _rate(agg["server_errors"], agg["requests"])})
        out["p95_ms"].append({"t": t, "v": request_metrics.histogram_quantile(0.95, agg["histogram"])})
        out["push_failed"].append({"t": t, "v": _counter(agg, "push.failed")})
        out["task_failures"].append({"t": t, "v": sum(v for k, v in agg["counters"].items()
                                                      if k.startswith("loop.") and k.endswith(".failed"))})
    return out


def _no_samples(first: Optional[datetime]) -> str:
    if first is None:
        return ("No operations sample has been recorded yet. Each API process writes one a minute once "
                "this version of the API runs.")
    return f"No operations sample in this window. Sampling began {iso(first)}."


# ── Sections ────────────────────────────────────────────────────────────────

def api_section(cur: dict, prev: dict, reason: Optional[str]) -> dict:
    available = reason is None
    prev_ok = prev["samples"] > 0
    q = request_metrics.histogram_quantile

    def m(key, label, value, previous, **kw):
        return metric(key, label, value=value, previous=previous if prev_ok else None,
                      available=available, unavailable_reason=reason, **kw)

    metrics = [
        m("requests", "Requests answered", cur["requests"], prev["requests"], good="neutral",
          definition="Every HTTP answer the API gave in the window, all processes added together."),
        m("server_errors", "Server errors (5xx)", cur["server_errors"], prev["server_errors"], good="down",
          definition=DEFINITIONS["error_rate"]),
        m("error_rate", "Server error rate", _rate(cur["server_errors"], cur["requests"]),
          _rate(prev["server_errors"], prev["requests"]), unit="percent", good="down",
          definition=DEFINITIONS["error_rate"],
          caption=None if cur["requests"] else "No requests in the window."),
        m("client_errors", "Refused requests (4xx)", cur["client_errors"], prev["client_errors"], good="neutral",
          definition=DEFINITIONS["client_errors"]),
        m("p50_ms", "Latency p50", q(0.50, cur["histogram"]), q(0.50, prev["histogram"]), unit="ms", good="down",
          definition=DEFINITIONS["latency"]),
        m("p95_ms", "Latency p95", q(0.95, cur["histogram"]), q(0.95, prev["histogram"]), unit="ms", good="down",
          definition=DEFINITIONS["latency"]),
        m("p99_ms", "Latency p99", q(0.99, cur["histogram"]), q(0.99, prev["histogram"]), unit="ms", good="down",
          definition=DEFINITIONS["latency"]),
    ]
    endpoints = []
    for name, e in cur["endpoints"].items():
        endpoints.append({
            "route": name, "requests": e["requests"], "server_errors": e["server_errors"],
            "client_errors": e["client_errors"], "error_rate": _rate(e["server_errors"], e["requests"]),
            "avg_ms": round(e["total_ms"] / e["timed"], 1) if e["timed"] else None,
            "p95_ms": request_metrics.histogram_quantile(0.95, e["histogram"]),
            "max_ms": round(e["max_ms"], 1) if e["max_ms"] is not None else None,
        })
    busiest = sorted(endpoints, key=lambda e: (-e["requests"], e["route"]))[:15]
    slowest = sorted((e for e in endpoints if e["requests"] >= MIN_ENDPOINT_REQUESTS and e["p95_ms"] is not None),
                     key=lambda e: (-e["p95_ms"], e["route"]))[:10]
    failing = sorted((e for e in endpoints if e["server_errors"]), key=lambda e: (-e["server_errors"], e["route"]))[:10]
    return {"key": "api", "title": "API", "metrics": metrics,
            "endpoints": {"busiest": busiest, "slowest": slowest, "failing": failing,
                          "note": ("Each process records its 25 busiest routes a minute, so a rarely called "
                                   "route can be missing here.")}}


def database_section(cur: dict, prev: dict, reason: Optional[str], live: dict) -> dict:
    available = reason is None
    prev_ok = prev["samples"] > 0

    def m(key, label, value, previous, **kw):
        return metric(key, label, value=value, previous=previous if prev_ok else None,
                      available=available, unavailable_reason=reason, **kw)

    def unavailable_total(agg):
        return sum(_prefixed(agg, "db.unavailable.").values())

    peak = round(max(cur["utilization"]), 1) if cur["utilization"] else None
    peak_prev = round(max(prev["utilization"]), 1) if prev["utilization"] else None
    metrics = [
        m("pool_peak_utilization", "Pool use, highest sample", peak, peak_prev, unit="percent", good="down",
          definition=DEFINITIONS["pool_utilization"]),
        m("pool_saturations", "Pool full", _counter(cur, "db.saturations"), _counter(prev, "db.saturations"),
          good="down", definition="Checkouts that took the pool's last free connection; the next request waits."),
        m("pool_slow_waits", "Waits of 250 ms or more for a connection", _counter(cur, "db.slow_pool_waits"),
          _counter(prev, "db.slow_pool_waits"), good="down",
          definition="Checkouts that queued at least DB_POOL_WAIT_WARN_MS for a free connection."),
        m("slow_queries", "Slow statements", _counter(cur, "db.slow_queries"), _counter(prev, "db.slow_queries"),
          good="down", definition=DEFINITIONS["slow_query"]),
        m("query_failures", "Failed statements", _counter(cur, "db.query_failures"),
          _counter(prev, "db.query_failures"), good="down", definition=DEFINITIONS["query_failures"]),
        m("db_unavailable", "Requests refused: database unavailable", unavailable_total(cur), unavailable_total(prev),
          good="down", definition=DEFINITIONS["db_unavailable"]),
        m("statements", "Statements run", _counter(cur, "db.queries"), _counter(prev, "db.queries"), good="neutral",
          definition="Every SQL statement the API processes ran in the window."),
    ]
    return {"key": "database", "title": "Database", "metrics": metrics,
            "unavailable_by_kind": _prefixed(cur, "db.unavailable."),
            "live": {k: live.get(k) for k in ("checked_out", "max_connections", "size", "overflow", "timeout_s")},
            "live_note": "This process's pool at this moment; other processes have their own."}


async def payment_counts(db: AsyncSession, start: datetime, end: datetime) -> dict:
    """Payment attempts started in [start, end), by provider and status."""
    from api.domains.admin.router import _payment_union

    union = _payment_union()
    rows = (await db.execute(
        select(union.c.source, union.c.status, func.count())
        .where(union.c.created_at >= start, union.c.created_at < end)
        .group_by(union.c.source, union.c.status)
    )).all()
    out = {"success": 0, "failed": 0, "pending": 0, "by_source": {}}
    for source, status, n in rows:
        name = getattr(status, "value", status)
        bucket = "success" if name == "success" else "failed" if name == "failed" else \
            "pending" if name in PENDING_PAYMENT_STATUSES else None
        if bucket is None:
            continue
        src = "zetupay" if source == "zetupay" else "mpesa"
        entry = out["by_source"].setdefault(src, {"success": 0, "failed": 0, "pending": 0})
        entry[bucket] += int(n or 0)
        out[bucket] += int(n or 0)
    for entry in [out, *out["by_source"].values()]:
        entry["success_pct"] = _rate(entry["success"], entry["success"] + entry["failed"])
    return out


_DIGITS = re.compile(r"\d+")


def error_pattern(text: Optional[str]) -> str:
    """A provider error, shortened to its kind: digits (phone numbers,
    amounts, codes inside the text) masked, cut at its first colon."""
    cleaned = _DIGITS.sub("#", (text or "").strip())
    cleaned = re.split(r"[:\n]", cleaned, maxsplit=1)[0].strip()
    return cleaned[:80] or "unknown"


async def payments_section(db: AsyncSession, start: datetime, end: datetime, prev_start: datetime,
                           now: datetime) -> dict:
    from api.database import MpesaStatus, MpesaTransaction
    from api.models.zetupay import ZetuPayPayment, ZetuPayTransaction

    from api.domains.admin.router import _payment_union

    cur = await payment_counts(db, start, end)
    prev = await payment_counts(db, prev_start, start)
    union = _payment_union()
    stale = int((await db.execute(
        select(func.count()).select_from(union).where(
            union.c.created_at >= start, union.c.created_at < min(end, now - STALE_PAYMENT_AFTER),
            union.c.status.in_(PENDING_PAYMENT_STATUSES))
    )).scalar() or 0)

    delays = [
        (to - frm).total_seconds() for frm, to in (await db.execute(
            select(ZetuPayPayment.created_at, func.min(ZetuPayTransaction.received_at))
            .join(ZetuPayTransaction, ZetuPayTransaction.payment_id == ZetuPayPayment.id)
            .where(ZetuPayPayment.created_at >= start, ZetuPayPayment.created_at < end,
                   ZetuPayTransaction.source == "webhook")
            .group_by(ZetuPayPayment.id, ZetuPayPayment.created_at)
            .limit(ROW_CAP)
        )).all() if frm is not None and to is not None and to >= frm
    ]

    errors: Counter = Counter()
    for (reason,) in (await db.execute(
        select(ZetuPayPayment.failure_reason).where(
            ZetuPayPayment.created_at >= start, ZetuPayPayment.created_at < end,
            ZetuPayPayment.status == "failed").limit(ROW_CAP)
    )).all():
        errors[("zetupay", error_pattern(reason))] += 1
    for (text,) in (await db.execute(
        select(MpesaTransaction.last_error).where(
            MpesaTransaction.created_at >= start, MpesaTransaction.created_at < end,
            MpesaTransaction.status == MpesaStatus.failed).limit(ROW_CAP)
    )).all():
        errors[("mpesa", error_pattern(text))] += 1

    metrics = [
        metric("payments_succeeded", "Successful payments", value=cur["success"], previous=prev["success"],
               definition="Payment attempts started in the window that succeeded (M-Pesa intents and ZetuPay).",
               href="/payments?status=success"),
        metric("payments_failed", "Failed payments", value=cur["failed"], previous=prev["failed"], good="down",
               definition="Payment attempts started in the window that failed.", href="/payments?status=failed"),
        metric("payments_pending", "Still pending", value=cur["pending"], previous=prev["pending"], good="neutral",
               definition="Payment attempts started in the window with no final answer yet."),
        metric("payment_success_rate", "Payment success rate", value=cur["success_pct"],
               previous=prev["success_pct"], unit="percent", definition=DEFINITIONS["payment_success"],
               caption=None if cur["success"] + cur["failed"] else "No finished payment in the window."),
        metric("payments_stale", "Pending over 15 minutes", value=stale, good="down", kind="point",
               definition="Attempts from the window still pending 15 minutes or more after they started."),
        metric("zetupay_callback_p95", "ZetuPay callback delay p95", value=_quantile(delays, 0.95),
               unit="seconds", good="down", definition=DEFINITIONS["callback_delay"],
               caption=f"Median {_quantile(delays, 0.5)} s over {len(delays)} webhooks." if delays else
               "No ZetuPay webhook for a payment of this window."),
    ]
    return {"key": "payments", "title": "Payments", "metrics": metrics,
            "providers": [{"source": src, **counts} for src, counts in sorted(cur["by_source"].items())],
            "errors": [{"source": src, "pattern": pattern, "count": n}
                       for (src, pattern), n in errors.most_common(8)]}


def notifications_section(cur: dict, prev: dict, reason: Optional[str], deliveries: dict) -> dict:
    available = reason is None
    prev_ok = prev["samples"] > 0

    def m(key, label, value, previous, **kw):
        return metric(key, label, value=value, previous=previous if prev_ok else None,
                      available=available, unavailable_reason=reason, **kw)

    failures = _prefixed(cur, "push.failed.")
    metrics = [
        m("push_devices", "Phones pushed", _counter(cur, "push.devices"), _counter(prev, "push.devices"),
          good="neutral", definition="Phones FCM was asked to notify, every kind of push."),
        m("push_accepted", "Accepted by FCM", _counter(cur, "push.accepted"), _counter(prev, "push.accepted"),
          good="neutral", definition=DEFINITIONS["push_accepted"]),
        m("push_failed", "Refused or failed", _counter(cur, "push.failed"), _counter(prev, "push.failed"),
          good="down", definition="Phones FCM did not accept a push for, by FCM's code."),
        m("push_acceptance_rate", "FCM acceptance rate",
          _rate(_counter(cur, "push.accepted"), _counter(cur, "push.devices")),
          _rate(_counter(prev, "push.accepted"), _counter(prev, "push.devices")), unit="percent",
          definition="Accepted / phones pushed. Acceptance is not delivery."),
        m("push_tokens_removed", "Dead tokens removed", _counter(cur, "push.tokens_removed"),
          _counter(prev, "push.tokens_removed"), good="neutral",
          definition="Tokens FCM said were unregistered (app removed, data cleared); BROKA stops pushing to them."),
        m("push_no_devices", "Users with no phone to push", _counter(cur, "push.no_devices"),
          _counter(prev, "push.no_devices"), good="neutral",
          definition="Pushes not sent because the user has no registered phone."),
    ]
    metrics += deliveries["metrics"]
    return {"key": "notifications", "title": "Notifications", "metrics": metrics,
            "failure_codes": [{"code": code, "count": n} for code, n in Counter(failures).most_common(8)]}


async def deliveries(db: AsyncSession, start: datetime, end: datetime, prev_start: datetime) -> dict:
    """Chat message delivery from message_deliveries: the only push with an
    app acknowledgement."""
    from api.core.message_delivery import MAX_PUSH_ATTEMPTS
    from api.models.message_delivery import PUSH_FAILED, MessageDelivery as D

    async def counts(s: datetime, e: datetime) -> dict:
        row = (await db.execute(
            select(func.count(D.id), func.count(D.push_accepted_at), func.count(D.delivered_at),
                   func.count(D.read_at),
                   func.sum(case((D.status == PUSH_FAILED, 1), else_=0)),
                   func.sum(case((and_(D.status == PUSH_FAILED, D.push_attempts >= MAX_PUSH_ATTEMPTS), 1),
                                 else_=0)))
            .where(D.message_created_at >= s, D.message_created_at < e)
        )).one()
        total, accepted, delivered, read, failed, exhausted = (int(v or 0) for v in row)
        return {"total": total, "accepted": accepted, "delivered": delivered, "read": read,
                "failed": failed, "exhausted": exhausted}

    cur, prev = await counts(start, end), await counts(prev_start, start)
    timing = (await db.execute(
        select(D.message_created_at, D.push_accepted_at, D.delivered_at)
        .where(D.message_created_at >= start, D.message_created_at < end)
        .limit(ROW_CAP)
    )).all()
    to_accept = [(a - m).total_seconds() for m, a, _ in timing if m and a and a >= m]
    to_deliver = [(d - m).total_seconds() for m, _, d in timing if m and d and d >= m]
    metrics = [
        metric("messages_tracked", "Chat messages to deliver", value=cur["total"], previous=prev["total"],
               good="neutral", definition="One per message and recipient, written when the message is saved."),
        metric("messages_delivered_rate", "Confirmed on the phone", value=_rate(cur["delivered"], cur["total"]),
               previous=_rate(prev["delivered"], prev["total"]), unit="percent",
               definition=DEFINITIONS["message_delivered"]),
        metric("messages_push_failed", "Chat pushes that failed", value=cur["failed"], previous=prev["failed"],
               good="down", definition="Messages whose push FCM refused; retried up to the attempt limit.",
               caption=f"{cur['exhausted']} gave up after {MAX_PUSH_ATTEMPTS} attempts." if cur["failed"] else None),
        metric("message_push_p95", "Saved to accepted by FCM, p95", value=_quantile(to_accept, 0.95),
               unit="seconds", good="down",
               definition="Seconds from a chat message being saved to FCM accepting its push.",
               caption=f"Median {_quantile(to_accept, 0.5)} s." if to_accept else None),
        metric("message_delivery_p95", "Saved to confirmed on the phone, p95", value=_quantile(to_deliver, 0.95),
               unit="seconds", good="down",
               definition="Seconds from a chat message being saved to the app confirming it had it.",
               caption=f"Median {_quantile(to_deliver, 0.5)} s." if to_deliver else None),
    ]
    return {"metrics": metrics, "current": cur}


async def calls_section(db: AsyncSession, start: datetime, end: datetime, prev_start: datetime) -> dict:
    from api.database import NegotiationMessage

    async def outcomes(s: datetime, e: datetime) -> tuple[dict, list[int]]:
        # visibility-ok: call cards' outcome words and counts only, for operations figures.
        rows = (await db.execute(
            select(NegotiationMessage.content, func.count(NegotiationMessage.id)).where(
                NegotiationMessage.msg_type == "call", NegotiationMessage.created_at >= s,
                NegotiationMessage.created_at < e, NegotiationMessage.sender_id.not_in(_staff()))
            .group_by(NegotiationMessage.content)
        )).all()
        counts = {o: 0 for o in CALL_OUTCOMES}
        for outcome, n in rows:
            key = outcome if outcome in counts else "other"
            counts[key] = counts.get(key, 0) + int(n or 0)
        # visibility-ok: durations of completed calls only, no message content is read.
        durations = [int(d) for (d,) in (await db.execute(
            select(NegotiationMessage.duration_secs).where(
                NegotiationMessage.msg_type == "call", NegotiationMessage.content == "completed",
                NegotiationMessage.created_at >= s, NegotiationMessage.created_at < e,
                NegotiationMessage.duration_secs.is_not(None),
                NegotiationMessage.sender_id.not_in(_staff())).limit(ROW_CAP)
        )).all() if d is not None]
        return counts, durations

    cur, durations = await outcomes(start, end)
    prev, _ = await outcomes(prev_start, start)

    def total(c):
        return sum(c.values())

    def answered(c):
        return c.get("completed", 0) + c.get("failed_to_connect", 0)

    metrics = [
        metric("calls", "Calls", value=total(cur), previous=total(prev), good="neutral",
               definition=DEFINITIONS["calls"]),
        metric("calls_connected_rate", "Answered calls that connected", value=_rate(cur["completed"], answered(cur)),
               previous=_rate(prev["completed"], answered(prev)), unit="percent",
               definition="Completed / (completed + answered but never connected)."),
        metric("calls_failed_to_connect", "Answered, never connected", value=cur["failed_to_connect"],
               previous=prev["failed_to_connect"], good="down",
               definition="The callee answered and the audio never came up (network, TURN, permissions)."),
        metric("calls_missed", "Missed or declined", value=cur["missed"] + cur["declined"],
               previous=prev["missed"] + prev["declined"], good="neutral",
               definition="Calls nobody answered or the callee declined."),
        metric("call_median_minutes", "Median completed call", unit="minutes", good="neutral",
               value=round(statistics.median(durations) / 60, 1) if durations else None,
               definition="Median duration of completed calls, from the call cards."),
    ]
    return {"key": "calls", "title": "Calls", "metrics": metrics,
            "outcomes": [{"outcome": k, "count": v, "previous": prev.get(k, 0)} for k, v in cur.items()]}


def background_section(cur: dict, prev: dict, reason: Optional[str], latest: dict[str, Any], now: datetime) -> dict:
    """Loops from the newest sample of each process; counts over the window."""
    intervals = _loop_intervals()
    merged: dict[str, dict] = {}
    queue_depth: Counter = Counter()
    for sample in latest.values():
        gauges = sample.gauges or {}
        for name, depth in (gauges.get("queues") or {}).items():
            if isinstance(depth, int):
                queue_depth[name] += depth
        for name, state in (gauges.get("loops") or {}).items():
            into = merged.setdefault(name, {"alive_at": None, "pass_finished_at": None, "pass_ms": None})
            for field in ("alive_at", "pass_finished_at"):
                value = state.get(field)
                if value and (into[field] is None or value > into[field]):
                    into[field] = value
                    if field == "pass_finished_at":
                        into["pass_ms"] = state.get("pass_ms")
    loops = []
    for name in sorted(set(intervals) | set(merged)):
        state = merged.get(name, {})
        finished = state.get("pass_finished_at")
        interval = intervals.get(name)
        age = None
        if finished:
            try:
                age = round((now - datetime.fromisoformat(finished.rstrip("Z"))).total_seconds())
            except ValueError:
                age = None
        if reason is not None:
            status = "unknown"
        elif finished is None:
            status = "no_pass"
        elif interval and age is not None and age > 3 * interval + 60:
            status = "stale"
        else:
            status = "ok"
        failed = _prefixed(cur, f"loop.{name}.failed.")
        loops.append({
            "name": name, "label": LOOP_LABELS.get(name, name.replace("_", " ")),
            "interval_seconds": interval, "status": status,
            "alive_at": state.get("alive_at"), "last_pass_finished_at": finished,
            "last_pass_age_seconds": age, "last_pass_ms": state.get("pass_ms"),
            "passes": _counter(cur, f"loop.{name}.passes"),
            "failures": _counter(cur, f"loop.{name}.failed"),
            "failing_tasks": [{"task": t, "count": n} for t, n in sorted(failed.items(), key=lambda kv: (-kv[1], kv[0]))],
        })
    queues = []
    for name in sorted(set(queue_depth) | {k.split(".")[1] for k in cur["counters"] if k.startswith("queue.")}):
        queues.append({
            "name": name, "depth": queue_depth.get(name, 0) if latest else None,
            "done": _counter(cur, f"queue.{name}.done"), "failed": _counter(cur, f"queue.{name}.failed"),
            "dropped": _counter(cur, f"queue.{name}.dropped"),
        })
    available = reason is None
    prev_ok = prev["samples"] > 0

    def task_failures(agg):
        return sum(v for k, v in agg["counters"].items() if k.startswith("loop.") and k.endswith(".failed"))

    metrics = [
        metric("task_failures", "Failed background tasks", value=task_failures(cur),
               previous=task_failures(prev) if prev_ok else None, good="down", available=available,
               unavailable_reason=reason,
               definition="Tasks of the background loops that raised; each loop carries on with its next task."),
        metric("queue_failures", "Failed queued jobs", value=sum(q["failed"] for q in queues), good="down",
               available=available, unavailable_reason=reason,
               definition="In-process queue jobs (notifications, payments, fraud, AI, listings) that raised."),
        metric("queue_dropped", "Jobs dropped: queue full", value=sum(q["dropped"] for q in queues), good="down",
               available=available, unavailable_reason=reason,
               definition="Jobs refused because their queue held 500 waiting jobs."),
        metric("stale_loops", "Stale loops", value=sum(1 for loop in loops if loop["status"] == "stale"),
               good="down", kind="point", available=available, unavailable_reason=reason,
               definition=DEFINITIONS["loop"]),
    ]
    items = _prefixed(cur, "item_failed.")
    return {"key": "background", "title": "Background processing", "metrics": metrics,
            "loops": loops, "queues": queues,
            "item_failures": [{"item": k, "count": v} for k, v in sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))]}


async def operations_report(db: AsyncSession, *, window_hours: int = 24, now: Optional[datetime] = None) -> dict:
    from api.database import pool_status

    now = now or datetime.utcnow()
    hours = max(1, min(MAX_WINDOW_HOURS, int(window_hours)))
    end = now
    start = end - timedelta(hours=hours)
    prev_start = start - timedelta(hours=hours)

    samples, truncated = await load_samples(db, start, end)
    prev_samples, prev_truncated = await load_samples(db, prev_start, start)
    first = await first_sample_at(db)
    cur, prev = aggregate(samples), aggregate(prev_samples)
    reason = None if samples else _no_samples(first)

    latest: dict[str, Any] = {}
    for s in samples:
        if s.instance not in latest or s.period_end > latest[s.instance].period_end:
            latest[s.instance] = s
    reporting = sorted(i for i, s in latest.items() if (now - s.period_end) <= timedelta(minutes=3))

    try:
        live_pool = pool_status()
    except Exception:
        live_pool = {}

    delivery = await deliveries(db, start, end, prev_start)
    bucket = _bucket_seconds(hours)
    return {
        "generated_at": iso(now),
        "window": {"from": iso(start), "to": iso(end), "hours": hours,
                   "previous_from": iso(prev_start), "previous_to": iso(start)},
        "coverage": {
            "sampling_started_at": iso(first) if first else None,
            "samples": len(samples), "instances_in_window": cur["instances"],
            "instances_reporting": len(reporting), "sample_seconds": settings.ops_sample_seconds,
            "truncated": truncated or prev_truncated,
            "this_instance": ops_metrics.INSTANCE_ID,
        },
        "sections": [
            api_section(cur, prev, reason),
            database_section(cur, prev, reason, live_pool),
            await payments_section(db, start, end, prev_start, now),
            notifications_section(cur, prev, reason, delivery),
            await calls_section(db, start, end, prev_start),
            background_section(cur, prev, reason, latest, now),
        ],
        "series": {"bucket_seconds": bucket, **series(samples, start, end, bucket)},
        "live": {"instance": ops_metrics.INSTANCE_ID, "requests": request_metrics.recorder.summary()},
        "not_instrumented": NOT_INSTRUMENTED,
        "definitions": DEFINITIONS,
    }
