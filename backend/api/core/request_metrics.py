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


class _Bucket:
    __slots__ = ("requests", "server_errors", "samples", "seen")

    def __init__(self) -> None:
        self.requests = 0
        self.server_errors = 0
        self.samples: list[float] = []
        self.seen = 0


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

    def record(self, status: int, elapsed_ms: float, *, now: Optional[float] = None) -> None:
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
        if 500 <= status <= 599:
            bucket.server_errors += 1
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


recorder = RequestWindow()


def record(status: int, elapsed_ms: float) -> None:
    """Count one answered request. Never raises: counting must not fail a request."""
    try:
        recorder.record(status, elapsed_ms)
    except Exception:
        pass
