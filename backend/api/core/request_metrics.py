"""Requests this process answered in the last minutes: count, server errors, latency.

The Command Center's API error rate and latency come from here. Every HTTP
request is already timed by LatencyLogMiddleware (api/core/observability.py),
which records it here as well. The figures are process-local: they live in
memory, reset when the process restarts, and each replica counts only the
requests it answered. Nothing here is durable, and the Command Center labels
the figures that way.

  * A server error is a response with status 500-599, including a request
    that raised before it could answer. 4xx answers are the client's.
  * Latency runs from the request reaching the app to its response finishing.
  * Percentiles come from at most SAMPLES_PER_BUCKET samples per minute, kept
    by reservoir sampling, so a busy minute is represented without keeping
    every request.
  * A request cancelled by its client (a dropped connection) is not counted.

For the operations workspace each minute also keeps exact counts: 4xx
answers, a latency histogram over every request (LATENCY_BOUNDS_MS) and the
same per route template ("GET /listings/{listing_id}", never the path with
its ids). api/core/ops_sampler.py drains complete minutes into `ops_samples`;
the Command Center's 15-minute summary is unchanged by that. A long-lived
stream (a route ending in /stream) is counted as a request but kept out of
latency: it stays open by design.
"""
from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone
from typing import Callable, Optional

BUCKET_SECONDS = 60
WINDOW_BUCKETS = 15          # a reported window: the last 15 minutes
SAMPLES_PER_BUCKET = 500
# Upper edges of the latency histogram, in ms; one more bucket holds the rest.
LATENCY_BOUNDS_MS = (5, 10, 25, 50, 75, 100, 150, 250, 400, 600, 1000, 1500,
                     2500, 4000, 6000, 10000, 20000, 30000)
# A minute keeps at most this many routes; the rest count under "other", so
# a scanner walking random paths cannot grow memory.
MAX_ROUTES_PER_BUCKET = 300
UNMATCHED_ROUTE = "unmatched"


def _histogram() -> list[int]:
    return [0] * (len(LATENCY_BOUNDS_MS) + 1)


def _slot(elapsed_ms: float) -> int:
    for index, bound in enumerate(LATENCY_BOUNDS_MS):
        if elapsed_ms <= bound:
            return index
    return len(LATENCY_BOUNDS_MS)


def histogram_quantile(q: float, counts: list[int]) -> Optional[float]:
    """The q-quantile of a latency histogram, interpolated inside its bucket
    (as Prometheus does). Past the last edge the edge itself is returned: the
    true value is at least that."""
    total = sum(counts)
    if not total:
        return None
    rank = q * total
    seen = 0
    for index, n in enumerate(counts):
        if n and seen + n >= rank:
            if index >= len(LATENCY_BOUNDS_MS):
                return float(LATENCY_BOUNDS_MS[-1])
            lower = LATENCY_BOUNDS_MS[index - 1] if index else 0
            upper = LATENCY_BOUNDS_MS[index]
            return round(lower + (upper - lower) * max(0.0, rank - seen) / n, 1)
        seen += n
    return float(LATENCY_BOUNDS_MS[-1])


class _Route:
    __slots__ = ("requests", "server_errors", "client_errors", "total_ms", "max_ms", "histogram")

    def __init__(self) -> None:
        self.requests = 0
        self.server_errors = 0
        self.client_errors = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.histogram = _histogram()


class _Bucket:
    __slots__ = ("requests", "server_errors", "samples", "seen", "client_errors", "histogram", "routes")

    def __init__(self) -> None:
        self.requests = 0
        self.server_errors = 0
        self.samples: list[float] = []
        self.seen = 0
        self.client_errors = 0
        self.histogram = _histogram()
        self.routes: dict[str, _Route] = {}


def _percentile(ordered: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile of already-sorted values."""
    if not ordered:
        return None
    rank = max(1, math.ceil(q * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 1)


def _stats(buckets: list[_Bucket]) -> dict:
    requests = sum(b.requests for b in buckets)
    errors = sum(b.server_errors for b in buckets)
    samples = sorted(s for b in buckets for s in b.samples)
    return {
        "requests": requests,
        "server_errors": errors,
        "error_rate_pct": round(errors / requests * 100, 2) if requests else None,
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
        "latency_samples": len(samples),
    }


class RequestWindow:
    """Per-minute request counts and latency samples, for the last two windows."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 rng: Optional[random.Random] = None) -> None:
        self._clock = clock
        self._rng = rng or random.Random()
        self._buckets: dict[int, _Bucket] = {}
        self.started_at = clock()
        # Minutes before this one have gone to a sample (ops_sampler).
        self._drained_until = int(self.started_at // BUCKET_SECONDS)

    def record(self, status: int, elapsed_ms: float, *, now: Optional[float] = None,
               route: Optional[str] = None) -> None:
        now = self._clock() if now is None else now
        minute = int(now // BUCKET_SECONDS)
        bucket = self._buckets.get(minute)
        if bucket is None:
            bucket = self._buckets[minute] = _Bucket()
            # Two windows are reported (the last one and the one before), so
            # nothing older than that is kept.
            oldest = minute - 2 * WINDOW_BUCKETS
            for key in [key for key in self._buckets if key <= oldest]:
                del self._buckets[key]
        bucket.requests += 1
        server_error = 500 <= status <= 599
        client_error = 400 <= status <= 499
        if server_error:
            bucket.server_errors += 1
        if client_error:
            bucket.client_errors += 1
        name = route or UNMATCHED_ROUTE
        stream = name.endswith("/stream")
        if not stream:
            bucket.histogram[_slot(elapsed_ms)] += 1
        stat = bucket.routes.get(name)
        if stat is None:
            if len(bucket.routes) >= MAX_ROUTES_PER_BUCKET:
                name = "other"
                stat = bucket.routes.get(name)
            if stat is None:
                stat = bucket.routes[name] = _Route()
        stat.requests += 1
        stat.server_errors += int(server_error)
        stat.client_errors += int(client_error)
        if not stream:
            stat.total_ms += float(elapsed_ms)
            stat.max_ms = max(stat.max_ms, float(elapsed_ms))
            stat.histogram[_slot(elapsed_ms)] += 1
        bucket.seen += 1
        if len(bucket.samples) < SAMPLES_PER_BUCKET:
            bucket.samples.append(float(elapsed_ms))
        else:
            slot = self._rng.randrange(bucket.seen)
            if slot < SAMPLES_PER_BUCKET:
                bucket.samples[slot] = float(elapsed_ms)

    def summary(self, *, now: Optional[float] = None) -> dict:
        """The last WINDOW_BUCKETS minutes (the current minute included) and the
        same span before it. A window the process was not running for the whole
        of is marked incomplete; the earlier one is then not reported at all."""
        now = self._clock() if now is None else now
        minute = int(now // BUCKET_SECONDS)
        current = [b for key, b in self._buckets.items() if minute - WINDOW_BUCKETS < key <= minute]
        previous = [b for key, b in self._buckets.items()
                    if minute - 2 * WINDOW_BUCKETS < key <= minute - WINDOW_BUCKETS]
        span = WINDOW_BUCKETS * BUCKET_SECONDS
        uptime = now - self.started_at
        return {
            "window_seconds": span,
            "process_started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc)
            .replace(tzinfo=None).isoformat() + "Z",
            "current_complete": uptime >= span,
            "current": _stats(current),
            "previous": _stats(previous) if uptime >= 2 * span else None,
        }


    def collect(self, *, now: Optional[float] = None, top_routes: int = 25) -> tuple[dict, int]:
        """The minutes not yet sampled, up to (not including) the current one,
        and the mark that `mark_drained` takes once the sample is saved.

        Minutes older than the kept buckets are gone (a sampler stalled for
        half an hour); the sample's period still starts where the last one
        ended, so the gap shows as minutes with nothing counted."""
        now = self._clock() if now is None else now
        upto = int(now // BUCKET_SECONDS)
        start = self._drained_until
        minutes = [m for m in self._buckets if start <= m < upto]
        histogram = _histogram()
        out = {
            "period_start": _iso_minute(start), "period_end": _iso_minute(upto),
            "requests": 0, "server_errors": 0, "client_errors": 0,
        }
        routes: dict[str, _Route] = {}
        for minute in minutes:
            bucket = self._buckets[minute]
            out["requests"] += bucket.requests
            out["server_errors"] += bucket.server_errors
            out["client_errors"] += bucket.client_errors
            histogram = [a + b for a, b in zip(histogram, bucket.histogram)]
            for name, stat in bucket.routes.items():
                into = routes.get(name)
                if into is None:
                    into = routes[name] = _Route()
                into.requests += stat.requests
                into.server_errors += stat.server_errors
                into.client_errors += stat.client_errors
                into.total_ms += stat.total_ms
                into.max_ms = max(into.max_ms, stat.max_ms)
                into.histogram = [a + b for a, b in zip(into.histogram, stat.histogram)]
        out["latency_histogram"] = histogram
        out["latency_samples"] = sum(histogram)
        out["p50_ms"] = histogram_quantile(0.50, histogram)
        out["p95_ms"] = histogram_quantile(0.95, histogram)
        out["p99_ms"] = histogram_quantile(0.99, histogram)
        ranked = sorted(routes.items(), key=lambda kv: (-kv[1].requests, kv[0]))[:top_routes]
        out["endpoints"] = [{
            "route": name, "requests": stat.requests, "server_errors": stat.server_errors,
            "client_errors": stat.client_errors,
            "avg_ms": round(stat.total_ms / sum(stat.histogram), 1) if sum(stat.histogram) else None,
            "max_ms": round(stat.max_ms, 1) if sum(stat.histogram) else None,
            "histogram": stat.histogram,
        } for name, stat in ranked]
        return out, upto

    @property
    def drained_until(self) -> int:
        return self._drained_until

    def mark_drained(self, upto: int) -> None:
        self._drained_until = max(self._drained_until, upto)


def _iso_minute(minute: int) -> str:
    return datetime.fromtimestamp(minute * BUCKET_SECONDS, tz=timezone.utc).replace(tzinfo=None).isoformat()


recorder = RequestWindow()


def record(status: int, elapsed_ms: float, route: Optional[str] = None) -> None:
    """Count one answered request. Never raises: counting must not fail a request."""
    try:
        recorder.record(status, elapsed_ms, route=route)
    except Exception:
        pass
