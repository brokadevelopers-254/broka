"""Write this process's operations counters to `ops_samples` once a minute.

Every API process runs the loop, with no lease: each one only knows the
requests it answered (api/core/request_metrics.py), its own database pool
and statement counters (api/database.py), and the pushes, queued tasks and
loop passes it ran (api/core/ops_metrics.py). A row covers whole minutes,
from where the previous row ended to the start of the current minute.

A row that cannot be saved (the database is down) loses nothing: the
counters are put back, the request minutes stay undrained, and the next row
covers both stretches. Minutes older than the request window keeps (half an
hour) are gone by then, and the operations view shows that gap as a stretch
with no sample rather than as quiet minutes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete

from api.core import ops_metrics, request_metrics
from api.core.config import settings

logger = logging.getLogger(__name__)

# Cumulative counters in api/database.pool_status(), sampled as differences.
DB_COUNTERS = ("queries", "slow_queries", "query_failures", "pool_waits", "slow_pool_waits",
               "saturations", "slow_holds")
PRUNE_EVERY_PASSES = 60

_running = False
_last_db: dict[str, int] = {}


def _db_deltas(status: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in DB_COUNTERS:
        value = status.get(key)
        if not isinstance(value, int):
            continue
        before = _last_db.get(key, 0)
        # Smaller than last time: the counters restarted (a test replaced the
        # engine module state); count from zero rather than go negative.
        delta = value - before if value >= before else value
        if delta:
            out[f"db.{key}"] = delta
    return out


def _gauges() -> dict:
    queues: dict[str, int] = {}
    try:
        from api.core import workers
        queues = {name: queue.depth() for name, queue in workers._queues.items()}
    except Exception:
        pass
    return {
        "queues": queues,
        "loops": ops_metrics.loops_snapshot(),
        "process_started_at": ops_metrics.STARTED_AT.isoformat() + "Z",
    }


async def sample_once(*, now: Optional[float] = None) -> bool:
    """Save one row for the minutes since the last one. False when there was
    no complete minute to save yet, or the save failed."""
    from api.database import AsyncSessionLocal, pool_status
    from api.models.ops_sample import OpsSample

    recorder = request_metrics.recorder
    period, mark = recorder.collect(now=now)
    if mark <= recorder.drained_until:
        return False
    try:
        status = pool_status()
    except Exception:
        status = {}
    counters = ops_metrics.drain_counters()
    merged = dict(counters)
    merged.update(_db_deltas(status))
    checked_out = status.get("checked_out")
    max_connections = status.get("max_connections")
    row = OpsSample(
        instance=ops_metrics.INSTANCE_ID,
        period_start=datetime.fromisoformat(period["period_start"]),
        period_end=datetime.fromisoformat(period["period_end"]),
        requests=period["requests"],
        server_errors=period["server_errors"],
        client_errors=period["client_errors"],
        latency_histogram=period["latency_histogram"],
        p50_ms=period["p50_ms"],
        p95_ms=period["p95_ms"],
        p99_ms=period["p99_ms"],
        db_checked_out=checked_out if isinstance(checked_out, int) else None,
        db_max_connections=max_connections if isinstance(max_connections, int) else None,
        counters=merged,
        gauges=_gauges(),
        endpoints=period["endpoints"],
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(row)
            await db.commit()
    except Exception as exc:
        ops_metrics.restore_counters(counters)
        logger.warning("[ops] sample not saved type=%s", type(exc).__name__)
        return False
    recorder.mark_drained(mark)
    _last_db.update({key: status[key] for key in DB_COUNTERS if isinstance(status.get(key), int)})
    return True


async def prune(*, now: Optional[datetime] = None) -> int:
    """Delete samples past OPS_SAMPLE_RETENTION_DAYS. Safe on every process at once."""
    from api.database import AsyncSessionLocal
    from api.models.ops_sample import OpsSample

    cutoff = (now or datetime.utcnow()) - timedelta(days=settings.ops_sample_retention_days)
    async with AsyncSessionLocal() as db:
        result = await db.execute(delete(OpsSample).where(OpsSample.period_start < cutoff))
        await db.commit()
    return int(result.rowcount or 0)


async def run_loop(interval_seconds: Optional[int] = None) -> None:
    global _running
    _running = True
    interval = interval_seconds or settings.ops_sample_seconds
    passes = 0
    logger.info("[ops] sampling started instance=%s (every %ds)", ops_metrics.INSTANCE_ID, interval)
    while _running:
        await asyncio.sleep(interval)
        try:
            await sample_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[ops] sample failed type=%s", type(exc).__name__)
        passes += 1
        if passes % PRUNE_EVERY_PASSES == 1:
            try:
                await prune()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[ops] prune failed type=%s", type(exc).__name__)


def stop() -> None:
    global _running
    _running = False


def reset_for_tests() -> None:
    _last_db.clear()
