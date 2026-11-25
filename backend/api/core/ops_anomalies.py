"""Unusual changes in the operations figures, judged against their own history.

The Operations page shows what happened. This module says which figure moved
outside its usual range on the last complete UTC day. It reads the per-minute
samples the API writes (api/core/ops_sampler.py) through the reader the
Operations page uses (api/core/operations.py), one UTC day at a time.

Three rules keep a flag honest:

- A day counts only when at least MIN_SAMPLES_PER_DAY samples cover it. A day
  the sampler missed is "not measured": it is neither compared nor read as zero.
- A figure is judged only when at least MIN_BASELINE_DAYS of the BASELINE_DAYS
  before the evaluated day were measured. With less history it reads
  "insufficient_history" and raises nothing.
- A flag needs a robust z-score of at least Z_THRESHOLD against the median and
  the median absolute deviation of the baseline, a change of at least the
  figure's smallest meaningful size, and a move in the direction that matters
  for that figure. The median is used because one bad day in the baseline
  must not hide a real change, or invent one.

A flag describes a change; it does not act. Nothing here suspends, restricts,
pays or contacts anyone. A person decides what to do about it.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import operations

BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 7
# Each process samples once a minute, so a full day is 1440 samples per
# process. Half a day from one process is the least a day's rate can rest on.
MIN_SAMPLES_PER_DAY = 720
Z_THRESHOLD = 3.5
HIGH_Z = 6.0

# "concern" is the direction that is bad for the figure. "both" flags a move
# either way, for traffic, where a sudden drop can be an outage.
# "floor" keeps the spread from collapsing when the baseline is flat, and
# "min_change" is the smallest move worth a person's time.
FIGURES: list[dict] = [
    {"key": "api_error_rate", "label": "API error rate", "unit": "percent", "concern": "up",
     "floor": 0.5, "min_change": 1.0,
     "impact": "Requests failed with server errors, so some users saw errors.",
     "action": "Open Operations for that day and check the failing routes, then the database pool."},
    {"key": "api_p95_ms", "label": "API latency (95th percentile)", "unit": "ms", "concern": "up",
     "floor": 10.0, "min_change": 100.0,
     "impact": "Pages and actions were slower for users.",
     "action": "Open Operations for that day and check the slowest routes and the database pool."},
    {"key": "api_requests", "label": "Requests answered", "unit": "count", "concern": "both",
     "floor": 1.0, "min_change": 20.0,
     "impact": "Traffic moved far from its usual level. A sudden drop can mean an outage; a sudden rise can mean load.",
     "action": "Check Operations for failing routes and loops that day, and the sign-up figures for the same day."},
    {"key": "db_failed_statements", "label": "Failed database statements", "unit": "count", "concern": "up",
     "floor": 1.0, "min_change": 3.0,
     "impact": "Database statements were refused or failed, so some requests could not complete.",
     "action": "Open Operations, Database, for that day and check the refused requests and the pool."},
    {"key": "pool_saturations", "label": "Times the connection pool was full", "unit": "count", "concern": "up",
     "floor": 1.0, "min_change": 3.0,
     "impact": "Requests waited for a database connection.",
     "action": "Check the slow statements and the busiest routes for that day."},
    {"key": "push_failed", "label": "Pushes refused or failed", "unit": "count", "concern": "up",
     "floor": 1.0, "min_change": 5.0,
     "impact": "Pushes did not reach FCM or were refused, so users may miss messages while the app is closed.",
     "action": "Check the failure codes on Operations, Notifications, and the push device records."},
    {"key": "task_failures", "label": "Background task failures", "unit": "count", "concern": "up",
     "floor": 1.0, "min_change": 3.0,
     "impact": "Scheduled work (deal timers, reconciliation, reports) failed for some items.",
     "action": "Check the failing tasks and loops on Operations, Background."},
]

# Figures the marketplace scan judges from their own tables (api/core/market_models.py).
COVERED_ELSEWHERE = [
    {"figure": "New registrations, new listings, first-time listing views, M-Pesa payments and failed pushes",
     "where": "/admin/intelligence/market/anomalies",
     "reason": "The marketplace scan judges these from the records they come from."},
]
NOT_COVERED = [
    {"figure": "Active users", "reason": "No anomaly rule judges it yet."},
    {"figure": "Call connection outcomes",
     "reason": "There is no daily series of connection outcomes. The marketplace scan counts call log entries only."},
    {"figure": "Slow statements and ZetuPay payments", "reason": "Neither detector judges them yet."},
]


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format(unit: str, value: float) -> str:
    if unit == "percent":
        return f"{value:.1f}%"
    if unit == "ms":
        return f"{round(value):,} ms"
    return f"{round(value):,}"


def _day_figures(samples: list, day: datetime, truncated: bool) -> Optional[dict]:
    """One UTC day's figures, or None when the day is not measured.

    `truncated` means the reader stopped at its row cap, so the day is
    incomplete and must not be read as a low day.
    """
    if truncated or len(samples) < MIN_SAMPLES_PER_DAY:
        return None
    points = operations.series(samples, day, day + timedelta(days=1), 86400)

    def point(name: str) -> Optional[float]:
        rows = points.get(name) or []
        return rows[0]["v"] if rows else None

    def total(counter: str) -> float:
        return float(sum(int((getattr(s, "counters", None) or {}).get(counter, 0) or 0) for s in samples))

    return {
        "api_requests": point("requests"),
        "api_error_rate": point("error_rate"),
        "api_p95_ms": point("p95_ms"),
        "push_failed": point("push_failed"),
        "task_failures": point("task_failures"),
        "db_failed_statements": total("db.query_failures"),
        "pool_saturations": total("db.saturations"),
    }


def _judge(figure: dict, days: dict, evaluated: datetime) -> dict:
    key = figure["key"]
    out = {
        "key": key, "label": figure["label"], "unit": figure["unit"], "status": "not_measured",
        "reason": None, "observed": None, "baseline_median": None, "baseline_days": 0,
        "z_score": None, "deviation": None, "direction": None,
    }
    today = days.get(evaluated)
    if today is None or today.get(key) is None:
        out["reason"] = "The sampler recorded too few samples for this day, so it is not compared."
        return out

    baseline = [
        fig[key] for day, fig in days.items()
        if day != evaluated and fig is not None and fig.get(key) is not None
    ]
    out["baseline_days"] = len(baseline)
    if len(baseline) < MIN_BASELINE_DAYS:
        out.update(
            status="insufficient_history",
            reason=f"{len(baseline)} measured days before it; {MIN_BASELINE_DAYS} are needed to judge it.",
        )
        return out

    value = float(today[key])
    median = statistics.median(baseline)
    mad = statistics.median(abs(v - median) for v in baseline)
    spread = max(1.4826 * mad, figure["floor"], 0.1 * abs(median))
    delta = value - median
    z = delta / spread
    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    pct = round(delta / median * 100, 1) if median else None
    concerning = figure["concern"] in ("both", direction)
    flagged = abs(z) >= Z_THRESHOLD and abs(delta) >= figure["min_change"] and concerning
    out.update(
        status="flagged" if flagged else "normal",
        observed=round(value, 2),
        baseline_median=round(median, 2),
        z_score=round(z, 2),
        deviation={"absolute": round(delta, 2), "pct": pct},
        direction=direction,
    )
    if flagged:
        side = "above" if direction == "up" else "below"
        out.update(
            date=evaluated.date().isoformat(),
            severity="high" if abs(z) >= HIGH_Z else "medium",
            what_happened=(
                f"{figure['label']} was {_format(figure['unit'], value)} on {evaluated:%d %b %Y} (UTC), "
                f"{side} its usual {_format(figure['unit'], median)}."
            ),
            impact=figure["impact"],
            suggested_action=figure["action"],
            source="ops_samples",
            evidence=[
                {"label": figure["label"], "value": round(value, 2), "source": "ops_samples"},
                {"label": f"Median of the {len(baseline)} measured days before", "value": round(median, 2),
                 "source": "ops_samples"},
            ],
        )
    return out


async def detect(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    """Judge the last complete UTC day against the BASELINE_DAYS before it."""
    now = now or datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    evaluated = today - timedelta(days=1)
    first = evaluated - timedelta(days=BASELINE_DAYS)

    days: dict[datetime, Optional[dict]] = {}
    for offset in range(BASELINE_DAYS + 1):
        day = first + timedelta(days=offset)
        samples, truncated = await operations.load_samples(db, day, day + timedelta(days=1))
        days[day] = _day_figures(samples, day, truncated)

    figures = [_judge(figure, days, evaluated) for figure in FIGURES]
    return {
        "generated_at": _iso(now),
        "data_as_of": _iso(today),
        "evaluated_day": evaluated.date().isoformat(),
        "baseline": {"from": _iso(first), "to_exclusive": _iso(evaluated), "days": BASELINE_DAYS,
                     "min_measured_days": MIN_BASELINE_DAYS},
        "rules": {"min_samples_per_day": MIN_SAMPLES_PER_DAY, "z_threshold": Z_THRESHOLD, "high_z": HIGH_Z},
        "anomalies": [f for f in figures if f["status"] == "flagged"],
        "figures": figures,
        "covered_elsewhere": COVERED_ELSEWHERE,
        "not_covered": NOT_COVERED,
    }
