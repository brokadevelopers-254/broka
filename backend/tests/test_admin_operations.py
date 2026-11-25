"""The operations workspace: samples added across processes, durable rows for
payments, delivery and calls, and what is unavailable before sampling began.

Seeded around the real clock (the route reads utcnow), all inside the
default 24-hour window:

  samples   i-1 at -50, -40 and -2 minutes; i-2 at -30 minutes; one at -30 h
            for the window before.
  payments  M-Pesa: 3 success, 1 failed ("... 254712345678"); ZetuPay: 1 success
            whose webhook came 30 s later, 1 failed ("Request cancelled by user").
  delivery  4 chat messages: 3 confirmed on the phone, 1 push failed for good.
  calls     2 completed (2 and 4 minutes), 1 answered but never connected, 1 missed.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from api.core import operations, ops_metrics, ops_sampler, request_metrics
from api.database import (
    AccountType, AsyncSessionLocal, Deal, DealStatus, Listing, MpesaStatus, MpesaTransaction,
    NegotiationMessage, User, init_db, reset_engine,
)
from api.models.message_delivery import DELIVERED, PUSH_FAILED, MessageDelivery
from api.models.ops_sample import OpsSample
from api.models.zetupay import ZetuPayPayment, ZetuPayTransaction
from api.security import create_access_token
from main import app

NOW = datetime.utcnow()
MIN = timedelta(minutes=1)
BUCKETS = len(request_metrics.LATENCY_BOUNDS_MS) + 1


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_admin_operations.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    reset_engine()
    yield
    mp.undo()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db(set_test_db):
    # Depends on set_test_db. Autouse fixtures run in alphabetical order, and
    # seeded() sorts before set_test_db, so without this the schema is built
    # in the default database and the tests query an empty test database.
    await init_db()


@pytest_asyncio.fixture(scope="module")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _h(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


def _hist(**slots: int) -> list[int]:
    counts = [0] * BUCKETS
    for bound, n in slots.items():
        counts[request_metrics.LATENCY_BOUNDS_MS.index(int(bound[1:]))] = n
    return counts


def _sample(instance: str, at: datetime, *, requests: int, server_errors: int = 0, hist=None,
            counters=None, gauges=None, endpoints=None) -> OpsSample:
    return OpsSample(instance=instance, period_start=at, period_end=at + MIN, requests=requests,
                     server_errors=server_errors, client_errors=1, latency_histogram=hist or [0] * BUCKETS,
                     db_checked_out=2, db_max_connections=5, counters=counters or {}, gauges=gauges or {},
                     endpoints=endpoints or [])


def _iso(value: datetime) -> str:
    return value.isoformat() + "Z"


@pytest_asyncio.fixture(scope="module", autouse=True)
async def seeded(setup_db):
    users = [
        User(id="op-admin", name="Op Admin", phone=f"+25470{random.randint(1_000_000, 9_999_999)}",
             password_hash="x", is_admin=True, email="ops@broka.co.ke", account_type=AccountType.buyer),
        User(id="op-seller", name="Seller", phone=f"+25471{random.randint(1_000_000, 9_999_999)}",
             password_hash="x", account_type=AccountType.buyer_seller),
        User(id="op-buyer", name="Buyer", phone=f"+25472{random.randint(1_000_000, 9_999_999)}",
             password_hash="x", account_type=AccountType.buyer),
    ]
    async with AsyncSessionLocal() as db:
        db.add_all(users)
        await db.flush()
        db.add(Listing(id="op-l1", seller_id="op-seller", name="Phone", category="Electronics", price=1000.0,
                       lat=-1.29, lng=36.82, created_at=NOW - timedelta(days=3)))
        await db.flush()
        db.add(Deal(id="op-d1", listing_id="op-l1", seller_id="op-seller", buyer_id="op-buyer", agreed_price=1000.0,
                    commission=0.0, status=DealStatus.paid, created_at=NOW - timedelta(days=1)))
        await db.flush()
        endpoint = {"route": "GET /listings/{listing_id}", "requests": 80, "server_errors": 2, "client_errors": 1,
                    "avg_ms": 90.0, "max_ms": 400.0, "histogram": _hist(b100=60, b400=20)}
        db.add_all([
            _sample("i-1", NOW - 50 * MIN, requests=100, server_errors=2, hist=_hist(b50=80, b100=20),
                    counters={"db.slow_queries": 2, "db.query_failures": 1, "db.unavailable.pool_timeout": 1,
                              "push.devices": 10, "push.accepted": 8, "push.failed": 2,
                              "push.failed.UNAVAILABLE": 2, "push.tokens_removed": 1,
                              "loop.periodic.passes": 3, "loop.periodic.failed": 1,
                              "loop.periodic.failed.deal_timer_check": 1},
                    endpoints=[endpoint]),
            _sample("i-1", NOW - 40 * MIN, requests=50, hist=_hist(b50=50),
                    counters={"queue.notifications.done": 5, "queue.notifications.failed": 1,
                              "item_failed.auction_close": 1}),
            _sample("i-1", NOW - 2 * MIN, requests=10, server_errors=1, hist=_hist(b1000=10),
                    gauges={"queues": {"notifications": 2},
                            "loops": {"periodic": {"alive_at": _iso(NOW - MIN), "pass_finished_at": _iso(NOW - 2 * MIN),
                                                   "pass_ms": 1200.0},
                                      "nudge": {"alive_at": _iso(NOW - MIN), "pass_finished_at": _iso(NOW - timedelta(hours=1)),
                                                "pass_ms": 40.0}}}),
            _sample("i-2", NOW - 30 * MIN, requests=40, hist=_hist(b25=40)),
            _sample("i-1", NOW - timedelta(hours=30), requests=20, hist=_hist(b50=20)),
        ])
        db.add_all([
            *[MpesaTransaction(deal_id="op-d1", buyer_id="op-buyer", phone="254700000000", amount=1000.0,
                               status=MpesaStatus.success, created_at=NOW - timedelta(hours=1)) for _ in range(3)],
            MpesaTransaction(deal_id="op-d1", buyer_id="op-buyer", phone="254700000000", amount=1000.0,
                             status=MpesaStatus.failed, created_at=NOW - timedelta(hours=1),
                             last_error="The balance is insufficient for the transaction 254712345678: code 1"),
            ZetuPayPayment(id="op-z1", reference="REF-OP-1", user_id="op-buyer", purpose="listing_fee", amount=50,
                           phone="254700000000", target_id="op-l1", status="success",
                           created_at=NOW - timedelta(hours=2)),
            ZetuPayPayment(id="op-z2", reference="REF-OP-2", user_id="op-buyer", purpose="listing_fee", amount=50,
                           phone="254700000000", target_id="op-l1", status="failed",
                           failure_reason="Request cancelled by user", created_at=NOW - timedelta(hours=2)),
        ])
        await db.flush()
        db.add(ZetuPayTransaction(wave_transaction_id="w-1", status="success", payment_id="op-z1", source="webhook",
                                  outcome="applied", received_at=NOW - timedelta(hours=2) + timedelta(seconds=30)))
        created = NOW - timedelta(hours=3)
        db.add_all([
            *[MessageDelivery(message_id=f"op-m{i}", listing_id="op-l1", buyer_id="op-buyer", sender_id="op-buyer",
                              recipient_id="op-seller", recipient_role="seller", message_created_at=created,
                              status=DELIVERED, push_accepted_at=created + timedelta(seconds=2),
                              delivered_at=created + timedelta(seconds=10 * (i + 1))) for i in range(3)],
            MessageDelivery(message_id="op-m9", listing_id="op-l1", buyer_id="op-buyer", sender_id="op-buyer",
                            recipient_id="op-seller", recipient_role="seller", message_created_at=created,
                            status=PUSH_FAILED, push_attempts=5),
        ])

        def card(outcome: str, seconds=None) -> NegotiationMessage:
            return NegotiationMessage(listing_id="op-l1", buyer_id="op-buyer", sender_id="op-buyer", role="buyer",
                                      recipient_role=None, content=outcome, msg_type="call", duration_secs=seconds,
                                      created_at=NOW - timedelta(hours=5))
        db.add_all([card("completed", 120), card("completed", 240), card("failed_to_connect"), card("missed")])
        await db.commit()


def _section(report: dict, key: str) -> dict:
    return next(s for s in report["sections"] if s["key"] == key)


def _metric(section: dict, key: str) -> dict:
    return next(m for m in section["metrics"] if m["key"] == key)


@pytest.mark.asyncio
class TestOperationsReport:
    async def test_requests_errors_and_latency_add_up_across_processes(self, client):
        r = await client.get("/admin/operations?window_hours=24", headers=_h("op-admin"))
        assert r.status_code == 200, r.text
        report = r.json()
        api = _section(report, "api")
        assert _metric(api, "requests")["value"] == 200
        assert _metric(api, "requests")["previous"] == 20
        assert _metric(api, "server_errors")["value"] == 3
        assert _metric(api, "error_rate")["value"] == 1.5
        assert _metric(api, "client_errors")["value"] == 4
        # 200 timed requests: 40 <=25 ms, 130 <=50, 20 <=100, 10 <=1000. p50 falls in (25, 50].
        p50 = _metric(api, "p50_ms")["value"]
        assert 25 < p50 <= 50
        assert _metric(api, "p99_ms")["value"] > 600
        busiest = api["endpoints"]["busiest"][0]
        assert busiest["route"] == "GET /listings/{listing_id}"
        assert busiest["requests"] == 80 and busiest["server_errors"] == 2
        assert report["coverage"]["instances_in_window"] == 2
        assert report["coverage"]["instances_reporting"] == 1

    async def test_database_counts(self, client):
        report = (await client.get("/admin/operations", headers=_h("op-admin"))).json()
        db = _section(report, "database")
        assert _metric(db, "slow_queries")["value"] == 2
        assert _metric(db, "query_failures")["value"] == 1
        assert _metric(db, "db_unavailable")["value"] == 1
        assert _metric(db, "pool_peak_utilization")["value"] == 40.0
        assert db["unavailable_by_kind"] == {"pool_timeout": 1}

    async def test_payments_from_their_own_rows(self, client):
        report = (await client.get("/admin/operations", headers=_h("op-admin"))).json()
        pay = _section(report, "payments")
        assert _metric(pay, "payments_succeeded")["value"] == 4
        assert _metric(pay, "payments_failed")["value"] == 2
        assert _metric(pay, "payment_success_rate")["value"] == pytest.approx(66.67, abs=0.01)
        assert _metric(pay, "zetupay_callback_p95")["value"] == 30.0
        providers = {p["source"]: p for p in pay["providers"]}
        assert providers["mpesa"]["success"] == 3 and providers["zetupay"]["failed"] == 1
        patterns = {(e["source"], e["pattern"]) for e in pay["errors"]}
        # Digits are masked: a phone number never reaches the dashboard.
        assert ("mpesa", "The balance is insufficient for the transaction #") in patterns
        assert ("zetupay", "Request cancelled by user") in patterns
        assert "254712345678" not in str(pay)

    async def test_notifications_separate_acceptance_from_delivery(self, client):
        report = (await client.get("/admin/operations", headers=_h("op-admin"))).json()
        notes = _section(report, "notifications")
        assert _metric(notes, "push_devices")["value"] == 10
        assert _metric(notes, "push_accepted")["value"] == 8
        assert _metric(notes, "push_acceptance_rate")["value"] == 80.0
        assert "not delivery" in _metric(notes, "push_accepted")["definition"]
        assert _metric(notes, "messages_delivered_rate")["value"] == 75.0
        failed = _metric(notes, "messages_push_failed")
        assert failed["value"] == 1 and "gave up" in failed["caption"]
        assert notes["failure_codes"] == [{"code": "UNAVAILABLE", "count": 2}]

    async def test_calls_from_call_cards(self, client):
        report = (await client.get("/admin/operations", headers=_h("op-admin"))).json()
        calls = _section(report, "calls")
        assert _metric(calls, "calls")["value"] == 4
        assert _metric(calls, "calls_connected_rate")["value"] == pytest.approx(66.67, abs=0.01)
        assert _metric(calls, "calls_failed_to_connect")["value"] == 1
        assert _metric(calls, "call_median_minutes")["value"] == 3.0

    async def test_background_loops_queues_and_failures(self, client):
        report = (await client.get("/admin/operations", headers=_h("op-admin"))).json()
        bg = _section(report, "background")
        loops = {loop["name"]: loop for loop in bg["loops"]}
        assert loops["periodic"]["status"] == "ok"
        assert loops["nudge"]["status"] == "stale"
        assert loops["periodic"]["failing_tasks"] == [{"task": "deal_timer_check", "count": 1}]
        assert loops["activity"]["status"] == "no_pass"
        queues = {q["name"]: q for q in bg["queues"]}
        assert queues["notifications"] == {"name": "notifications", "depth": 2, "done": 5, "failed": 1, "dropped": 0}
        assert _metric(bg, "task_failures")["value"] == 1
        assert _metric(bg, "stale_loops")["value"] == 1
        assert bg["item_failures"] == [{"item": "auction_close", "count": 1}]

    async def test_series_leave_unsampled_buckets_empty(self, client):
        report = (await client.get("/admin/operations?window_hours=6", headers=_h("op-admin"))).json()
        series = report["series"]
        assert series["bucket_seconds"] == 300
        values = [p["v"] for p in series["requests"]]
        assert None in values
        assert sum(v for v in values if v is not None) == 200

    async def test_not_instrumented_is_said(self, client):
        report = (await client.get("/admin/operations", headers=_h("op-admin"))).json()
        names = {n["metric"] for n in report["not_instrumented"]}
        assert {"call_connection_delay", "mpesa_callback_delay"} <= names

    async def test_buyers_cannot_read_it(self, client):
        assert (await client.get("/admin/operations", headers=_h("op-buyer"))).status_code == 403

    async def test_a_window_with_no_sample_is_unavailable_not_zero(self):
        async with AsyncSessionLocal() as db:
            report = await operations.operations_report(db, window_hours=1, now=NOW - timedelta(days=10))
        api = _section(report, "api")
        requests = _metric(api, "requests")
        assert requests["available"] is False and requests["value"] is None
        assert "No operations sample in this window" in requests["unavailable_reason"]
        # Durable rows still answer.
        assert _metric(_section(report, "payments"), "payments_failed")["available"] is True


def test_error_patterns_mask_digits():
    assert operations.error_pattern("Timeout after 30s for 254700000000") == "Timeout after #s for #"
    assert operations.error_pattern(None) == "unknown"
    assert operations.error_pattern("HTTPStatusError: 500 Internal") == "HTTPStatusError"


@pytest.mark.asyncio
class TestSampler:
    async def test_a_sample_saves_complete_minutes_and_counters(self, monkeypatch):
        ops_metrics.reset_for_tests()
        ops_sampler.reset_for_tests()
        base = 25_000_000 * 60.0
        clock = [base]
        window = request_metrics.RequestWindow(clock=lambda: clock[0])
        monkeypatch.setattr(request_metrics, "recorder", window)
        window.record(200, 30, route="GET /x")
        window.record(500, 70, route="GET /x")
        ops_metrics.incr("push.accepted", 4)
        clock[0] = base + 90
        assert await ops_sampler.sample_once(now=clock[0]) is True
        start = datetime.fromtimestamp(base, tz=timezone.utc).replace(tzinfo=None)
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(OpsSample).where(OpsSample.period_start == start))).scalar_one()
        assert row.instance == ops_metrics.INSTANCE_ID
        assert row.requests == 2 and row.server_errors == 1
        assert row.counters["push.accepted"] == 4
        assert "queues" in row.gauges and "loops" in row.gauges
        assert row.endpoints[0]["route"] == "GET /x"
        # Nothing new in the same minute: no second row.
        assert await ops_sampler.sample_once(now=clock[0]) is False

    async def test_a_failed_save_loses_nothing(self, monkeypatch):
        ops_metrics.reset_for_tests()
        base = 26_000_000 * 60.0
        clock = [base]
        window = request_metrics.RequestWindow(clock=lambda: clock[0])
        monkeypatch.setattr(request_metrics, "recorder", window)
        window.record(200, 10, route="GET /y")
        ops_metrics.incr("push.failed", 2)
        clock[0] = base + 61

        class Broken:
            def __call__(self):
                raise RuntimeError("database down")

        import api.database as database
        monkeypatch.setattr(database, "AsyncSessionLocal", Broken())
        assert await ops_sampler.sample_once(now=clock[0]) is False
        assert ops_metrics.drain_counters() == {"push.failed": 2}
        period, _ = window.collect(now=clock[0])
        assert period["requests"] == 1

    async def test_prune_deletes_only_old_samples(self):
        async with AsyncSessionLocal() as db:
            db.add(_sample("i-old", NOW - timedelta(days=40), requests=1))
            await db.commit()
        removed = await ops_sampler.prune(now=NOW)
        assert removed >= 1
        async with AsyncSessionLocal() as db:
            old = (await db.execute(select(func.count(OpsSample.id)).where(OpsSample.instance == "i-old"))).scalar()
            recent = (await db.execute(select(func.count(OpsSample.id)).where(
                OpsSample.period_start >= NOW - timedelta(days=2)))).scalar()
        assert old == 0 and recent >= 5
