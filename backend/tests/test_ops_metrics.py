"""Operations counters: what each process counts between samples.

The request window's exact per-minute counts and histogram, the route
template the middleware names a request by, push results, queued jobs and
loop heartbeats. The samples they are written to are covered in
test_admin_operations.py.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.core import ops_metrics, push_devices, request_metrics, workers
from api.core.observability import LatencyLogMiddleware

BASE = 6_000_000 * 60.0  # a whole minute, in seconds


@pytest.fixture(autouse=True)
def fresh_counters():
    ops_metrics.reset_for_tests()
    yield
    ops_metrics.reset_for_tests()


class TestHistogram:
    def test_quantiles_interpolate_inside_their_bucket(self):
        counts = [0] * (len(request_metrics.LATENCY_BOUNDS_MS) + 1)
        counts[request_metrics.LATENCY_BOUNDS_MS.index(100)] = 100   # (75, 100]
        assert request_metrics.histogram_quantile(0.5, counts) == 87.5
        assert request_metrics.histogram_quantile(1.0, counts) == 100.0

    def test_an_empty_histogram_has_no_percentile(self):
        assert request_metrics.histogram_quantile(0.95, [0, 0, 0]) is None

    def test_past_the_last_edge_the_edge_is_the_floor(self):
        counts = [0] * (len(request_metrics.LATENCY_BOUNDS_MS) + 1)
        counts[-1] = 3
        assert request_metrics.histogram_quantile(0.99, counts) == float(request_metrics.LATENCY_BOUNDS_MS[-1])


class TestRequestWindowCollect:
    def _window(self, clock):
        return request_metrics.RequestWindow(clock=lambda: clock[0])

    def test_complete_minutes_only_with_exact_counts(self):
        clock = [BASE]
        window = self._window(clock)
        window.record(200, 40, route="GET /listings/{listing_id}")
        window.record(404, 8, route="GET /listings/{listing_id}")
        window.record(503, 900, route="POST /deal/{deal_id}/pay")
        clock[0] = BASE + 61
        window.record(200, 12, route="GET /health")          # the current minute: not collected
        period, mark = window.collect()
        assert period["requests"] == 3
        assert period["server_errors"] == 1 and period["client_errors"] == 1
        assert period["latency_samples"] == 3
        assert period["period_start"].endswith(":00") and period["period_start"] < period["period_end"]
        routes = {e["route"]: e for e in period["endpoints"]}
        assert routes["GET /listings/{listing_id}"]["requests"] == 2
        assert routes["GET /listings/{listing_id}"]["client_errors"] == 1
        assert routes["POST /deal/{deal_id}/pay"]["server_errors"] == 1
        assert "GET /health" not in routes
        window.mark_drained(mark)
        again, mark2 = window.collect()
        assert again["requests"] == 0 and mark2 == mark

    def test_a_stream_is_counted_but_not_timed(self):
        clock = [BASE]
        window = self._window(clock)
        window.record(200, 3_600_000, route="GET /admin/alerts/stream")
        clock[0] = BASE + 60
        period, _ = window.collect()
        assert period["requests"] == 1
        assert period["latency_samples"] == 0 and period["p95_ms"] is None

    def test_unmatched_paths_share_one_name(self):
        clock = [BASE]
        window = self._window(clock)
        for path in range(5):
            window.record(404, 2, route=None)
        clock[0] = BASE + 60
        period, _ = window.collect()
        assert [e["route"] for e in period["endpoints"]] == [request_metrics.UNMATCHED_ROUTE]

    def test_route_names_are_capped_per_minute(self, monkeypatch):
        monkeypatch.setattr(request_metrics, "MAX_ROUTES_PER_BUCKET", 3)
        clock = [BASE]
        window = self._window(clock)
        for i in range(10):
            window.record(200, 5, route=f"GET /r{i}")
        bucket = next(iter(window._buckets.values()))
        assert len(bucket.routes) == 4 and bucket.routes["other"].requests == 7

    def test_the_command_center_summary_is_unchanged(self):
        clock = [BASE]
        window = self._window(clock)
        window.record(500, 10, route="GET /x")
        summary = window.summary()
        assert summary["current"]["requests"] == 1 and summary["current"]["server_errors"] == 1


@pytest.mark.asyncio
class TestMiddlewareRouteNames:
    async def test_requests_are_named_by_route_template_not_path(self, monkeypatch):
        fresh = request_metrics.RequestWindow()
        monkeypatch.setattr(request_metrics, "recorder", fresh)
        app = FastAPI()

        @app.get("/items/{item_id}")
        async def item(item_id: str):
            return {"id": item_id}

        wrapped = LatencyLogMiddleware(app)
        async with AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://t") as client:
            assert (await client.get("/items/abc-123")).status_code == 200
            assert (await client.get("/nowhere/at/all")).status_code == 404
        names = {name for bucket in fresh._buckets.values() for name in bucket.routes}
        assert "GET /items/{item_id}" in names
        assert request_metrics.UNMATCHED_ROUTE in names
        assert not any("abc-123" in name for name in names)


class TestCounters:
    def test_drain_resets_and_restore_puts_back(self):
        ops_metrics.incr("push.accepted", 2)
        ops_metrics.incr("push.accepted")
        drained = ops_metrics.drain_counters()
        assert drained == {"push.accepted": 3}
        assert ops_metrics.drain_counters() == {}
        ops_metrics.restore_counters(drained)
        assert ops_metrics.drain_counters() == {"push.accepted": 3}

    def test_names_are_cleaned_to_identifiers(self):
        ops_metrics.task_failed("periodic", "deal timers: check", ValueError("phone +254700111222 refused"))
        names = ops_metrics.drain_counters()
        assert "loop.periodic.failed.deal_timers_check" in names
        assert names["loop.periodic.failed"] == 1
        state = ops_metrics.loops_snapshot()["periodic"]
        # Only the exception's type is kept, never its text.
        assert state["last_failure_type"] == "valueerror"
        assert "254700111222" not in str(state)

    def test_pushes_count_phones_and_fcm_codes(self):
        ops_metrics.push_result("chat message", devices=3, accepted=1, failures=["UNAVAILABLE", "unregistered"])
        names = ops_metrics.drain_counters()
        assert names["push.devices"] == 3 and names["push.accepted"] == 1 and names["push.failed"] == 2
        assert names["push.failed.UNAVAILABLE"] == 1 and names["push.failed.UNREGISTERED"] == 1
        assert names["push.route.chat_message"] == 1

    def test_loop_passes_are_timed(self):
        started = ops_metrics.pass_started("nudge")
        ops_metrics.pass_finished("nudge", started)
        ops_metrics.loop_alive("nudge")
        state = ops_metrics.loops_snapshot()["nudge"]
        assert state["passes"] == 1 and state["pass_ms"] is not None and state["alive_at"]
        assert ops_metrics.drain_counters()["loop.nudge.passes"] == 1


@pytest.mark.asyncio
class TestPushCounts:
    async def test_push_user_counts_what_fcm_did(self):
        class Refused:
            unregistered = False
            error_code = "UNAVAILABLE"
            retryable = True

            def __bool__(self):
                return False

        async def send(*, token, **_):
            return True if token == "t-ok" else Refused()

        sent = await push_devices.push_user("u-1", title="t", body="b", data={"type": "chat_message"},
                                            send=send, tokens=["t-ok", "t-down"])
        assert sent == 1
        names = ops_metrics.drain_counters()
        assert names["push.devices"] == 2 and names["push.accepted"] == 1
        assert names["push.failed.UNAVAILABLE"] == 1

    async def test_no_phone_is_counted_as_such(self):
        await push_devices.push_user("u-1", title="t", body="b", data={}, tokens=[])
        assert ops_metrics.drain_counters() == {"push.no_devices": 1}


@pytest.mark.asyncio
class TestQueues:
    async def test_done_failed_and_dropped_jobs_are_counted(self):
        queue = workers.BackgroundWorker("testq", concurrency=1, max_queue=1)
        await queue.start()

        async def ok():
            return None

        async def boom():
            raise RuntimeError("nope")

        await queue.enqueue(ok)
        await queue._queue.join()
        await queue.enqueue(boom)
        await queue._queue.join()
        await queue.stop()
        # Stopped: nothing takes from the queue, so the second job finds it full.
        blocked = workers.BackgroundWorker("testq", concurrency=1, max_queue=1)
        await blocked.enqueue(ok)
        await blocked.enqueue(ok)
        assert blocked.depth() == 1
        names = ops_metrics.drain_counters()
        assert names["queue.testq.done"] == 1
        assert names["queue.testq.failed"] == 1
        assert names["queue.testq.dropped"] == 1
        await asyncio.sleep(0)


def test_every_sweep_task_reports_its_failure():
    """Each task of the five-minute pass is in its own try; each except must
    count the failure, or the operations page shows a failing task as healthy."""
    source = inspect.getsource(workers._periodic_sweep_pass)
    handlers = len(re.findall(r"except Exception as exc:", source))
    counted = len(re.findall(r'ops_metrics\.task_failed\("periodic", "[a-z0-9_]+", exc\)', source))
    assert handlers >= 18 and counted == handlers
