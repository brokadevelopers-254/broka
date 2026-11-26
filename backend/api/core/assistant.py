"""The Intelligence Assistant: read-only answers from BROKA's verified analytics.

No model writes these answers. A question is matched to one intent by the
words in it, and each intent reads the analytics its dashboard page reads.
Every answer states the date range it covers, the definitions behind its
figures, and the page where the figures can be checked.

The rules this module keeps:

- Figures come only from those analytics. A figure that cannot be measured is
  reported as not measured, with its reason. It is never shown as zero.
- It reads aggregates only. No intent names or reads one person's record.
- It takes no action. A question that asks for one (suspend, refund, send,
  delete...) is answered with the page where it is done. Those pages ask for a
  separate confirmation before anything changes.
- Permission is checked by the route (ADMIN_SUMMARY), not here.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import activity, market_models, ops_anomalies
from api.core import report_builder as rb

# A question that opens with one of these words asks for information. One that
# opens with anything else and names an action is an instruction.
QUESTION_OPENERS = frozenset({
    "who", "what", "which", "how", "why", "when", "where", "is", "are", "was", "were", "did", "do",
    "does", "has", "have", "any", "show", "list", "tell", "give", "summarise", "summarize", "compare",
})

# Instructions the assistant will not carry out, and the page where each is done.
ACTIONS = {
    "suspend": "/users", "restrict": "/users", "reinstate": "/users", "ban": "/users",
    "block": "/users", "unblock": "/users", "flag": "/users", "unflag": "/users", "promote": "/users",
    "refund": "/payments", "payout": "/payments", "release": "/deals", "cancel": "/deals",
    "send": "/communications", "notify": "/communications", "broadcast": "/communications",
    "email": "/email-reply", "delete": "/", "remove": "/", "change": "/", "update": "/",
    "approve": "/", "reject": "/",
}

ANOMALY_WORDS = ("anomal", "unusual", "unexpected", "strange", "spike", "sudden", "abnormal", "surge",
                 "attention", "problem", "wrong", "issue")
FORECAST_WORDS = ("forecast", "predict", "projection", "next week", "next month", "expected to", "will there")
SUMMARY_WORDS = ("summary", "summarise", "summarize", "overview", "weekly performance", "how did we do",
                 "how are we doing", "business review")
CATEGORY_WORDS = ("category", "categories", "growing", "fastest", "demand")
PAYMENT_WORDS = ("payment", "paid", "m-pesa", "mpesa", "zetupay", "income", "revenue")
SIGNUP_WORDS = ("sign up", "signup", "sign-up", "signed up", "registration", "registered", "new user", "joined")
LISTING_WORDS = ("listing", "interest", "viewed", "views", "contact")
ACTIVE_WORDS = ("active", "engagement", "engaged")
NOW_WORDS = ("right now", "currently", "at the moment", "online")

SUMMARY_METRICS = ["new_users", "active_users", "deals_agreed", "income_minor", "payment_success_pct",
                   "api_error_rate_pct"]
PAYMENT_METRICS = ["payments_succeeded", "payments_failed", "payment_success_pct", "income_minor"]
SIGNUP_METRICS = ["new_users"]
ACTIVE_METRICS = ["active_users", "new_active_users"]
LISTING_METRICS = ["new_listings", "listing_viewers", "listing_contacters", "liquidity_pct"]

# Where each figure can be checked on the dashboard.
LINK_BY_METRIC = {
    "new_users": "/growth", "active_users": "/analytics/users", "new_active_users": "/analytics/users",
    "deals_agreed": "/deals", "income_minor": "/revenue", "payment_success_pct": "/payments",
    "payments_succeeded": "/payments", "payments_failed": "/payments", "api_error_rate_pct": "/operations",
    "new_listings": "/market", "listing_viewers": "/market", "listing_contacters": "/market",
    "liquidity_pct": "/market",
}
LINK_LABELS = {
    "/growth": "Growth", "/analytics/users": "User analytics", "/deals": "Deals", "/revenue": "Revenue",
    "/payments": "Payments", "/operations": "Operations", "/market": "Marketplace intelligence",
    "/users": "Users", "/communications": "Service communications", "/email-reply": "Email reply",
    "/reports/builder": "Report builder", "/": "Overview",
}

HELP_POINTS = [
    "How many users are active right now?",
    "Sign-ups, payments, active users and listings over the last 7 complete UTC days. Ask for 'last 30 days' to change the window.",
    "Which areas need attention, and which figures moved outside their usual range.",
    "Forecasts for the next 7 days, and which ones were withheld and why.",
    "A summary of the last 7 complete UTC days.",
]
FORECAST_DEFINITION = ("The same weekday's median over the last four weeks. The band is the central 80% when "
                       "spread is normal. It is a range, not a guarantee.")


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _instruction(tokens: list[str]) -> Optional[str]:
    if not tokens or tokens[0] in QUESTION_OPENERS:
        return None
    return next((t for t in tokens if t in ACTIONS), None)


def _intent(lowered: str) -> str:
    if "active" in lowered and any(w in lowered for w in NOW_WORDS):
        return "active_now"
    if any(w in lowered for w in ANOMALY_WORDS):
        return "anomalies"
    if any(w in lowered for w in FORECAST_WORDS):
        return "forecast"
    if any(w in lowered for w in SUMMARY_WORDS):
        return "summary"
    if any(w in lowered for w in CATEGORY_WORDS):
        return "categories"
    if any(w in lowered for w in PAYMENT_WORDS):
        return "payments"
    if any(w in lowered for w in SIGNUP_WORDS):
        return "registrations"
    if any(w in lowered for w in LISTING_WORDS):
        return "listings"
    if any(w in lowered for w in ACTIVE_WORDS):
        return "active_users"
    return "help"


def classify(question: str) -> str:
    """The intent a question goes to. 'action' is an instruction, never carried out."""
    text = " ".join(question.split())
    if _instruction(re.findall(r"[a-z]+", text.lower())):
        return "action"
    return _intent(text.lower())


def _window_days(lowered: str) -> int:
    match = re.search(r"last\s+(\d{1,3})\s+days?", lowered)
    if match:
        return max(1, min(int(match.group(1)), rb.MAX_RANGE_DAYS))
    if re.search(r"last month|past month|this month", lowered):
        return 30
    return 7


def _fmt(value, unit: str) -> str:
    if value is None:
        return "not measured"
    if unit == "kes_minor":
        return f"KES {value / 100:,.0f}"
    if unit == "pct":
        return f"{value:.1f}%"
    if unit == "ms":
        return f"{round(value):,} ms"
    if unit == "minutes":
        return f"{value:.1f} min"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{round(value):,}"


def _plain(value) -> str:
    if value is None:
        return "not measured"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{round(value):,}"


def _change(k: dict) -> str:
    delta = k["delta"] or 0
    direction = "up" if delta >= 0 else "down"
    if k["unit"] == "pct":
        return f"{direction} {abs(delta):.1f} points"
    if k["previous"] == 0:
        return f"{direction} from zero"
    if k["change_pct"] is None:
        return "no change to compare"
    return f"{direction} {abs(k['change_pct']):.0f}%"


def _line(k: dict) -> str:
    if not k["available"]:
        return f"{k['label']}: not measured. {k['unavailable_reason']}"
    value = _fmt(k["value"], k["unit"])
    if k["kind"] == "point" or k["previous"] is None:
        return f"{k['label']}: {value}."
    return f"{k['label']}: {value}, against {_fmt(k['previous'], k['unit'])} in the period before ({_change(k)})."


def _citation(k: dict, period: dict, href: str) -> dict:
    return {
        "label": k["label"],
        "value": _fmt(k["value"], k["unit"]) if k["available"] else "not measured",
        "period": f"{period['label']} (UTC)",
        "definition": k["definition"],
        "source": k["source"],
        "href": href,
    }


def _links(citations: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for c in citations:
        href = c.get("href")
        if href and href not in seen:
            seen.add(href)
            out.append({"label": LINK_LABELS.get(href, "Open this page"), "href": href})
    return out


def _answer(question: str, intent: str, now: datetime, text: str, *, points=(), citations=(),
            limitations=(), refused: bool = False, links: Optional[list[dict]] = None) -> dict:
    return {
        "question": question,
        "intent": intent,
        "refused": refused,
        "answer": text,
        "points": list(points),
        "citations": list(citations),
        "limitations": list(limitations),
        "links": links if links is not None else _links(list(citations)),
        "generated_at": _iso(now),
    }


async def _report(db: AsyncSession, title: str, metrics: list[str], days: int, now: datetime):
    """The report builder's figures for the last `days` complete UTC days."""
    spec = rb.normalize_spec({
        "title": title, "categories": [], "metrics": metrics, "last_days": days,
        "comparison": "previous_period", "filters": {"category": None, "county": None},
        "group_by": "none", "visualization": "auto",
    })
    start, end = rb.spec_range(spec, now=now)
    payload = await rb.build(db, spec, start=start, end=end, now=now)
    return payload, {k["key"]: k for k in payload["kpis"]}


async def _report_intent(db, text, lowered, now, *, intent: str, title: str, metrics: list[str], lead: str):
    days = _window_days(lowered)
    payload, kpis = await _report(db, title, metrics, days, now)
    period = payload["period"]
    points, citations = [], []
    for key in metrics:
        k = kpis.get(key)
        if k is None:
            continue
        points.append(_line(k))
        citations.append(_citation(k, period, LINK_BY_METRIC.get(key, "/reports/builder")))
    return _answer(
        text, intent, now, f"{lead} for {period['label']} (UTC).", points=points, citations=citations,
        limitations=["Dates are whole UTC days, the end day included. Today is not counted."],
    )


async def _registrations(db, text, lowered, now):
    return await _report_intent(db, text, lowered, now, intent="registrations", title="Assistant: sign-ups",
                                metrics=SIGNUP_METRICS, lead="Sign-ups")


async def _payments(db, text, lowered, now):
    return await _report_intent(db, text, lowered, now, intent="payments", title="Assistant: payments",
                                metrics=PAYMENT_METRICS, lead="Payments")


async def _active_users(db, text, lowered, now):
    return await _report_intent(db, text, lowered, now, intent="active_users", title="Assistant: active users",
                                metrics=ACTIVE_METRICS, lead="Active users")


async def _listings(db, text, lowered, now):
    return await _report_intent(db, text, lowered, now, intent="listings", title="Assistant: listings",
                                metrics=LISTING_METRICS, lead="Listings")


async def _summary(db, text, lowered, now):
    days = _window_days(lowered)
    payload, kpis = await _report(db, "Assistant: summary", SUMMARY_METRICS, days, now)
    period = payload["period"]
    citations = [_citation(kpis[k], period, LINK_BY_METRIC.get(k, "/reports/builder"))
                 for k in SUMMARY_METRICS if k in kpis]
    return _answer(
        text, "summary", now, f"Summary for {period['label']} (UTC), with the figures it rests on.",
        points=payload["executive_summary"], citations=citations,
        limitations=["Dates are whole UTC days, the end day included. Today is not counted."],
    )


async def _active_now(db, text, lowered, now):
    snap = await activity.live_snapshot(db, now=now)
    count = snap.get("active_users_now")
    window = snap.get("presence_window_seconds")
    sampled = snap.get("generated_at")
    citation = {
        "label": "Active users now",
        "value": _fmt(count, "count") if count is not None else "not measured",
        "period": f"the last {window} seconds, sampled at {sampled}" if window else f"sampled at {sampled}",
        "definition": "Signed-in users whose live session sent a heartbeat inside the presence window.",
        "source": "Live presence sampler (user sessions)",
        "href": "/analytics/users",
    }
    if count is None:
        answer_text = "Live presence has no sample yet, so the number of active users is not measured."
    else:
        answer_text = f"{_fmt(count, 'count')} users were active in the presence window: the last {window} seconds."
    return _answer(
        text, "active_now", now, answer_text, citations=[citation],
        limitations=["A live count is one sample taken at one moment. It is not a daily total."],
    )


async def _anomalies(db, text, lowered, now):
    ops = await ops_anomalies.detect(db, now=now)
    market = await market_models.detect(db, now=now)
    flagged = [
        {"label": a["label"], "date": a["date"], "observed": a["observed"], "median": a["baseline_median"],
         "severity": a["severity"], "action": a["suggested_action"], "impact": a["impact"],
         "source": "Operations (ops_samples)", "href": "/operations",
         "definition": "Compared with the median of the measured days before it; flagged at a robust z-score of 3.5 or more."}
        for a in ops["anomalies"]
    ] + [
        {"label": a.get("label") or a.get("metric", "Marketplace figure"),
         "date": a.get("date") or market["evaluated_day"], "observed": a.get("observed"),
         "median": (a.get("baseline") or {}).get("median"), "severity": a.get("severity"),
         "action": a.get("suggested_action"), "impact": a.get("impact"),
         "source": "Marketplace scan (daily counts)", "href": "/market",
         "definition": "Compared with the same weekday in the previous weeks; flagged at a robust z-score of 3.5 or more."}
        for a in market.get("anomalies", [])
    ]
    day = ops["evaluated_day"]
    counts = Counter(f["status"] for f in ops["figures"])
    points = []
    if flagged:
        n = len(flagged)
        head = (f"{n} figure{'s' if n != 1 else ''} moved outside "
                f"{'their' if n != 1 else 'its'} usual range on {day} (UTC). "
                "Nothing was changed: each one needs a person to check it.")
        points = [
            f"{f['label']} on {f['date']}: {_plain(f['observed'])} against a usual {_plain(f['median'])}, "
            f"{f['severity'] or 'unrated'} severity. Check: {f['action']}"
            for f in flagged[:8]
        ]
    else:
        head = f"No figure moved outside its usual range on {day} (UTC)."
    if counts.get("insufficient_history"):
        points.append(f"{counts['insufficient_history']} operations figures have too little history to judge.")
    if counts.get("not_measured"):
        points.append(f"{counts['not_measured']} operations figures were not measured on {day}.")
    citations = [
        {"label": f["label"], "value": f"{_plain(f['observed'])} (usual {_plain(f['median'])})",
         "period": f"{f['date']} (UTC)", "definition": f["definition"], "source": f["source"], "href": f["href"]}
        for f in flagged
    ]
    return _answer(
        text, "anomalies", now, head, points=points, citations=citations,
        limitations=[f"{n['figure']}: {n['reason']}" for n in ops["not_covered"]],
    )


async def _forecast(db, text, lowered, now):
    items = await market_models.forecasts(db, now=now)
    day = now.date().isoformat()
    points, citations = [], []
    for item in items:
        if item.get("validated"):
            steps = item.get("forecast") or []
            expected = round(sum(s["expected"] for s in steps))
            low = round(sum(s["low"] for s in steps))
            high = round(sum(s["high"] for s in steps))
            wape = (item.get("backtest") or {}).get("wape")
            wape_text = f"{wape:.0%}" if wape is not None else "not measured"
            points.append(
                f"{item['label']}: about {expected:,} in the next 7 days, band {low:,} to {high:,}. "
                f"Backtest error {wape_text}, against a limit of {market_models.FORECAST_MAX_WAPE:.0%}."
            )
            citations.append({
                "label": item["label"], "value": f"about {expected:,} over the next 7 days",
                "period": f"7 days after {day} (UTC)", "definition": FORECAST_DEFINITION,
                "source": "Daily counts from the platform tables", "href": "/market",
            })
        else:
            points.append(f"{item['label']}: not published. "
                          f"{item.get('withheld_reason') or 'Its backtest did not pass.'}")
    return _answer(
        text, "forecast", now,
        "Forecasts for the next 7 days. A forecast is shown only when its backtest passed; the rest are withheld.",
        points=points, citations=citations,
        limitations=[
            f"A forecast is published only after a 7-day backtest with an error of at most "
            f"{market_models.FORECAST_MAX_WAPE:.0%} over at least {market_models.FORECAST_MIN_VOLUME} events, "
            "and only when it beats the same weekday last week.",
            "Missing days count as zero in the history, so a quiet stretch can pull a forecast down.",
            "Payment failure and supply shortage forecasts are not published yet.",
        ],
    )


async def _categories(db, text, lowered, now):
    return _answer(
        text, "categories", now,
        "I don't rank categories yet. Category growth and demand are on Marketplace intelligence, under Demand.",
        links=[{"label": "Marketplace intelligence", "href": "/market"}],
    )


async def _help(db, text, lowered, now):
    return _answer(text, "help", now, "I can't answer that from the analytics yet. I can answer questions about:",
                   points=HELP_POINTS)


HANDLERS = {
    "active_now": _active_now,
    "anomalies": _anomalies,
    "forecast": _forecast,
    "summary": _summary,
    "categories": _categories,
    "payments": _payments,
    "registrations": _registrations,
    "listings": _listings,
    "active_users": _active_users,
    "help": _help,
}


async def answer(db: AsyncSession, question: str, *, now: Optional[datetime] = None) -> dict:
    """One answer to one question. Nothing is stored: the question is not kept."""
    now = now or datetime.utcnow()
    text = " ".join(question.split())
    if classify(text) == "action":
        verb = _instruction(re.findall(r"[a-z]+", text.lower()))
        href = ACTIONS[verb]
        return _answer(
            text, "action", now,
            f"I can't {verb} anything or change records from here. To {verb} it, open the page and use its "
            "controls. Those controls ask you to confirm before anything changes.",
            refused=True, links=[{"label": LINK_LABELS.get(href, "Open this page"), "href": href}],
        )
    return await HANDLERS[classify(text)](db, text, text.lower(), now)
