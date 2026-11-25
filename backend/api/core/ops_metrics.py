"""Operational counts and loop heartbeats this process keeps between samples.

Several things that decide whether BROKA works were visible only in the logs:
a push FCM refused, a background task that raised, a sweep pass that
stopped running. This module counts them in memory, and api/core/ops_sampler.py
writes the counts to `ops_samples` once a minute per process, so the admin
operations workspace and the reports can show them over time and across
replicas.

Rules:
  * Counter names are fixed strings built from codes BROKA or a provider
    defines ("push.failed.UNAVAILABLE", "loop.periodic.failed.deal_timers").
    Never an id, a token, a phone number or any text a user wrote: a name is
    cleaned to a short identifier, and an unexpected one becomes "other".
  * Nothing here may fail the work it counts: every entry point swallows its
    own errors.
  * Counts are per process and since the last sample. A count the sampler
    could not write is put back and goes out with the next sample.
"""
from __future__ import annotations

import os
import re
import socket
import time
from collections import Counter
from datetime import datetime
from typing import Optional

_SAFE = re.compile(r"[^A-Za-z0-9_]+")
_MAX_NAME = 48


def _part(value: object, *, upper: bool = False) -> str:
    text = _SAFE.sub("_", str(value or "")).strip("_")[:_MAX_NAME] or "other"
    return text.upper() if upper else text.lower()


def _instance_id() -> str:
    # The host name of a Render instance is its container id: no personal
    # data, and it tells replicas apart. The pid tells workers apart.
    try:
        host = socket.gethostname() or "host"
    except Exception:
        host = "host"
    return f"{_part(host)[:40]}-{os.getpid()}"


INSTANCE_ID = _instance_id()
STARTED_AT = datetime.utcnow()

_counters: Counter = Counter()


def incr(name: str, amount: int = 1) -> None:
    """Add `amount` to a counter. Never raises."""
    try:
        if amount:
            _counters[name] += int(amount)
    except Exception:
        pass


def drain_counters() -> dict[str, int]:
    """The counts since the last drain, which this resets."""
    out = {k: int(v) for k, v in _counters.items() if v}
    _counters.clear()
    return out


def restore_counters(counts: dict[str, int]) -> None:
    """Put back counts a sample could not save, so the next one carries them."""
    for name, value in (counts or {}).items():
        incr(name, value)


# ── Pushes ──────────────────────────────────────────────────────────────────

def push_result(route: str, *, devices: int, accepted: int, failures: list[str]) -> None:
    """One push_user call that had phones to try. Accepted means FCM took the
    push for a phone, never that the phone received it."""
    try:
        route_name = _part(route)
        incr("push.requests")
        incr(f"push.route.{route_name}")
        incr("push.devices", devices)
        incr("push.accepted", accepted)
        for code in failures:
            incr("push.failed")
            incr(f"push.failed.{_part(code, upper=True)}")
    except Exception:
        pass


def push_no_devices() -> None:
    incr("push.no_devices")


def push_token_removed() -> None:
    incr("push.tokens_removed")


# ── Background work ─────────────────────────────────────────────────────────

def queue_dropped(queue: str) -> None:
    incr(f"queue.{_part(queue)}.dropped")


def queue_failed(queue: str) -> None:
    incr(f"queue.{_part(queue)}.failed")


def queue_done(queue: str) -> None:
    incr(f"queue.{_part(queue)}.done")


# loop name -> its last known state in this process
_loops: dict[str, dict] = {}


def _loop(name: str) -> dict:
    key = _part(name)
    state = _loops.get(key)
    if state is None:
        state = _loops[key] = {
            "alive_at": None, "pass_started_at": None, "pass_finished_at": None,
            "pass_ms": None, "passes": 0, "failures": 0,
            "last_failure_task": None, "last_failure_type": None, "last_failure_at": None,
        }
    return state


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def loop_alive(name: str) -> None:
    """The loop came round, whether or not this replica held its lease."""
    try:
        _loop(name)["alive_at"] = _now_iso()
    except Exception:
        pass


def pass_started(name: str) -> float:
    """This replica holds the lease and starts a pass. Returns its start time."""
    try:
        _loop(name)["pass_started_at"] = _now_iso()
    except Exception:
        pass
    return time.monotonic()


def pass_finished(name: str, started: float) -> None:
    try:
        state = _loop(name)
        state["pass_finished_at"] = _now_iso()
        state["pass_ms"] = round((time.monotonic() - started) * 1000, 1)
        state["passes"] += 1
        incr(f"loop.{_part(name)}.passes")
    except Exception:
        pass


def task_failed(loop: str, task: str, exc: Optional[BaseException] = None) -> None:
    """One task of a loop's pass raised. Only the exception's type is kept:
    its text can quote values from the failing row."""
    try:
        state = _loop(loop)
        state["failures"] += 1
        state["last_failure_task"] = _part(task)
        state["last_failure_type"] = _part(type(exc).__name__ if exc is not None else "error")
        state["last_failure_at"] = _now_iso()
        incr(f"loop.{_part(loop)}.failed")
        incr(f"loop.{_part(loop)}.failed.{_part(task)}")
    except Exception:
        pass


def loops_snapshot() -> dict[str, dict]:
    return {name: dict(state) for name, state in _loops.items()}


def reset_for_tests() -> None:
    _counters.clear()
    _loops.clear()
