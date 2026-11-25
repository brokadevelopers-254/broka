"""The Command Center's figures: what they count, and over which periods.

One seeded database, read at a fixed moment (NOW), so every figure has an
exact expected value. Seeded rows sit at fixed offsets from NOW; rows the
HTTP tests create carry the real clock, which is long after NOW, so they fall
outside every period measured here.
"""
import random
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.core import command_center as cc
from api.core import request_metrics
from api.core.observability import LatencyLogMiddleware
from api.database import (
    AsyncSessionLocal, FraudEvent, Listing, ListingLike, ListingView, User, Wishlist,
    init_db, reset_engine,
)
from api.models.account_moderation import AccountModerationEvent
from api.models.message_delivery import DELIVERED, PUSH_ACCEPTED, PUSH_FAILED, READ, MessageDelivery
from api.models.scam_report import ScamReportDetail
from api.models.user_activity import ActivitySample, UserActivityDay
from api.models.zetupay import ZetuPayPayment
from api.security import create_access_token, hash_password
from main import app

NOW = datetime(2025, 3, 1, 12, 0, 0)
LONG_AGO = NOW - timedelta(days=60)


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_command_center.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    reset_engine()
    yield
    mp.undo()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db():
    await init_db()


@pytest_asyncio.fixture(scope="module")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _phone() -> str:
    return "+2547" + str(uuid.uuid4().int)[:8]


def _user(*, created_at=LONG_AGO, **fields) -> User:
    return User(name="CC", phone=_phone(), password_hash=hash_password("Passw0rd!x"),
                created_at=created_at, **fields)


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


def _day(user_id: str, moment: datetime) -> UserActivityDay:
    return UserActivityDay(user_id=user_id, activity_date=moment.date(), first_active_at=moment,
                           last_active_at=moment, foreground_seconds=60)


def _payment(user_id: str, status: str, created_at: datetime) -> ZetuPayPayment:
    return ZetuPayPayment(
        reference=uuid.uuid4().hex[:24], user_id=user_id, purpose="listing_fee", amount=100,
        phone="254700000000", target_id=str(uuid.uuid4()), status=status,
        created_at=created_at, updated_at=created_at,
    )


def _delivery(status: str, created_at: datetime) -> MessageDelivery:
    return MessageDelivery(
        message_id=uuid.uuid4().hex, buyer_id="b", sender_id="s", recipient_id=uuid.uuid4().hex,
        recipient_role="buyer", message_created_at=created_at, status=status,
    )


SUPPORTING = 10  # accounts created LONG_AGO by _seed, before both periods


async def _seed() -> dict:
    """The world at NOW. Current period: [NOW-7d, NOW). Previous: [NOW-14d, NOW-7d)."""
    cur, prev = NOW - timedelta(days=3), NOW - timedelta(days=10)
    seller, viewer_a, viewer_b, active_a, active_b = (_user() for _ in range(5))
    flagged = _user(is_flagged=True)
    suspended = _user(account_status="suspended")
    lapsed = _user(account_status="suspended", suspended_until=NOW - timedelta(days=1))
    restricted = _user(account_status="restricted")
    payer = _user()
    supporting = [seller, viewer_a, viewer_b, active_a, active_b, flagged, suspended, lapsed, restricted, payer]
    assert len(supporting) == SUPPORTING
    registrations = [
        _user(created_at=NOW - timedelta(days=3)),
        _user(created_at=NOW - timedelta(days=9)),
        _user(created_at=NOW - timedelta(days=10)),
        _user(created_at=NOW - timedelta(days=2), is_admin=True),  # staff: never counted
    ]
    async with AsyncSessionLocal() as db:
        db.add_all(supporting + registrations)
        await db.flush()

        def listing(name: str, created_at: datetime) -> Listing:
            return Listing(seller_id=seller.id, name=name, category="Electronics", location_county="Nairobi",
                           price=1000.0, lat=-1.29, lng=36.82, created_at=created_at)

        old, new, previous_listing = listing("Old", NOW - timedelta(days=20)), listing("New", cur), listing("Prev", prev)
        db.add_all([old, new, previous_listing])
        await db.flush()

        db.add_all([
            # One person opening one listing twice is one view.
            ListingView(listing_id=old.id, viewer_id=viewer_a.id, created_at=cur),
            ListingView(listing_id=old.id, viewer_id=viewer_a.id, created_at=cur + timedelta(hours=1)),
            ListingView(listing_id=old.id, viewer_id=viewer_b.id, created_at=cur),
            ListingView(listing_id=old.id, viewer_id=viewer_a.id, created_at=prev),
            Wishlist(user_id=viewer_a.id, listing_id=old.id, created_at=cur),
            ListingLike(user_id=viewer_b.id, listing_id=old.id, created_at=cur),
            # Activity: tracking began 30 days back; A is active now and was before,
            # B is active in both periods but first appeared in the previous one.
            _day(active_a.id, NOW - timedelta(days=30)),
            _day(active_a.id, NOW - timedelta(days=2)),
            _day(active_b.id, NOW - timedelta(days=9)),
            _day(active_b.id, NOW - timedelta(days=1)),
            # Payments: 3 succeeded, 3 failed, 1 still waiting now; 4 succeeded before.
            *[_payment(payer.id, "success", cur) for _ in range(3)],
            *[_payment(payer.id, "failed", cur) for _ in range(3)],
            _payment(payer.id, "processing", NOW - timedelta(hours=2)),
            *[_payment(payer.id, "success", prev) for _ in range(4)],
            # Trust.
            FraudEvent(user_id=flagged.id, reason="velocity", resolved=False, created_at=cur),
            AccountModerationEvent(user_id=suspended.id, action="suspended", from_status="active",
                                   to_status="suspended", reason="chargebacks", actor_id="admin-x", created_at=cur),
            AccountModerationEvent(user_id=lapsed.id, action="suspended", from_status="active",
                                   to_status="suspended", suspended_until=NOW - timedelta(days=1),
                                   reason="spam", actor_id="admin-x", created_at=prev),
            ScamReportDetail(feedback_id=str(uuid.uuid4()), reporter_id=viewer_a.id,
                             reason_code="fake_payment", created_at=cur),
            # Messages: 2 of 3 confirmed on the phone now; FCM acceptance before is not delivery.
            _delivery(DELIVERED, cur), _delivery(READ, cur), _delivery(PUSH_FAILED, cur),
            _delivery(PUSH_ACCEPTED, prev),
            # The scheduler's last pass, ten minutes before NOW.
            ActivitySample(sampled_at=NOW - timedelta(minutes=10), active_users=0, active_sessions=0),
        ])
        await db.commit()
    return {"supporting": SUPPORTING}


@pytest_asyncio.fixture(scope="module")
async def report():
    await _seed()
    async with AsyncSessionLocal() as db:
        return await cc.command_center(db, window_days=7, now=NOW)


def _figures(report: dict) -> dict:
    return {m["key"]: m for group in report["groups"] for m in group["metrics"]}


# ── periods and shape ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPeriodsAndShape:
    async def test_the_previous_period_is_the_same_length_just_before(self, report):
        w = report["window"]
        assert w == {
            "days": 7,
            "from": "2025-02-22T12:00:00Z", "to": "2025-03-01T12:00:00Z",
            "previous_from": "2025-02-15T12:00:00Z", "previous_to": "2025-02-22T12:00:00Z",
        }
        assert report["pulse"]["window"]["from"] == w["from"], "the pulse it reuses measures the same period"

    async def test_five_groups_and_every_figure_says_what_it_counts(self, report):
        assert [g["key"] for g in report["groups"]] == ["growth", "marketplace", "financial", "trust", "operations"]
        for figure in _figures(report).values():
            assert set(figure) >= {"key", "label", "kind", "unit", "good", "value", "previous", "delta",
                                   "change_pct", "definition", "available", "unavailable_reason", "href"}
            assert figure["definition"].strip()
            if not figure["available"]:
                assert figure["value"] is None and figure["previous"] is None
                assert figure["unavailable_reason"]

    async def test_a_figure_nothing_measures_is_unavailable_with_its_reason(self, report):
        availability = _figures(report)["api_availability"]
        assert availability["available"] is False
        assert "uptime" in availability["unavailable_reason"]


# ── growth ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGrowth:
    async def test_registrations_exclude_staff_and_compare_equal_periods(self, report):
        f = _figures(report)
        assert f["new_registrations"]["value"] == 1
        assert f["new_registrations"]["previous"] == 2
        assert f["new_registrations"]["change_pct"] == -50.0
        assert f["registered_users"]["value"] == SUPPORTING + 3
        assert f["registered_users"]["previous"] == SUPPORTING + 2

    async def test_registration_growth_is_new_accounts_over_the_base_and_compares_in_points(self, report):
        growth = _figures(report)["registration_growth"]
        assert growth["value"] == round(1 / (SUPPORTING + 2) * 100, 1)
        assert growth["previous"] == round(2 / SUPPORTING * 100, 1)
        assert growth["change_pct"] is None, "a percentage compares in points, not percent of a percent"
        assert growth["delta"] == round(growth["value"] - growth["previous"], 2)

    async def test_active_and_returning_accounts_come_from_foreground_activity(self, report):
        f = _figures(report)
        assert (f["active_users"]["value"], f["active_users"]["previous"]) == (2, 1)
        assert (f["returning_users"]["value"], f["returning_users"]["previous"]) == (2, 0)

    async def test_activity_figures_are_unavailable_before_tracking_covers_the_period(self, report):
        async with AsyncSessionLocal() as db:
            early = await cc.command_center(db, window_days=7, now=NOW - timedelta(days=28))
        f = _figures(early)
        assert f["active_users"]["available"] is False
        assert "tracking began" in f["active_users"]["unavailable_reason"]


# ── marketplace ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMarketplace:
    async def test_listings_views_and_saves(self, report):
        f = _figures(report)
        assert (f["total_listings"]["value"], f["total_listings"]["previous"]) == (3, 2)
        assert (f["new_listings"]["value"], f["new_listings"]["previous"]) == (1, 1)
        assert (f["unique_listing_views"]["value"], f["unique_listing_views"]["previous"]) == (2, 1)
        assert (f["saves_and_likes"]["value"], f["saves_and_likes"]["previous"]) == (2, 0)
        assert f["live_listings"]["previous"] is None, "no status history is kept"


# ── money ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestFinancial:
    async def test_payment_outcomes_and_the_success_rate_leave_pending_out(self, report):
        f = _figures(report)
        assert (f["successful_payments"]["value"], f["successful_payments"]["previous"]) == (3, 4)
        assert (f["failed_payments"]["value"], f["failed_payments"]["previous"]) == (3, 0)
        assert f["payment_success_rate"]["value"] == 50.0
        assert f["payment_success_rate"]["previous"] == 100.0
        assert f["payment_success_rate"]["delta"] == -50.0
        assert "1 still pending" in f["payment_success_rate"]["caption"]
        assert f["refunds"]["value"] == 0


# ── trust ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTrust:
    async def test_suspended_now_and_when_the_period_began(self, report):
        suspended = _figures(report)["suspended_accounts"]
        # The lapsed suspension ended a day before NOW but was in force when the period began.
        assert (suspended["value"], suspended["previous"]) == (1, 1)
        assert "1 restricted" in suspended["caption"]

    async def test_flags_fraud_scam_reports_and_suspensions_applied(self, report):
        f = _figures(report)
        assert f["flagged_accounts"]["value"] == 1
        assert f["open_fraud_events"]["value"] == 1
        assert (f["fraud_flags"]["value"], f["fraud_flags"]["previous"]) == (1, 0)
        assert (f["scam_reports"]["value"], f["scam_reports"]["previous"]) == (1, 0)
        assert (f["suspensions_applied"]["value"], f["suspensions_applied"]["previous"]) == (1, 1)
        assert f["open_disputes"]["value"] == 0


# ── operations ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOperations:
    async def test_scheduled_work_age_waiting_prompts_and_confirmed_messages(self, report):
        f = _figures(report)
        assert f["scheduled_work"]["value"] == 600
        assert f["payments_waiting"]["value"] == 1
        confirmed = f["messages_confirmed"]
        assert confirmed["value"] == round(2 / 3 * 100, 1)
        assert confirmed["previous"] == 0.0, "an FCM acceptance is not a delivery"
        assert "1 push failures among 3" in confirmed["caption"]

    async def test_the_decisions_name_the_signals_and_are_ordered_by_severity(self, report):
        decisions = report["decisions"]["insights"]
        ids = [d["id"] for d in decisions]
        assert "ops_scheduler_stale" in ids
        assert "ops_payment_failures" in ids
        assert "ops_payment_prompts_waiting" in ids
        ranks = [cc.SEVERITY_ORDER[d["severity"]] for d in decisions]
        assert ranks == sorted(ranks)
        assert all("_weight" not in d for d in decisions)
        assert sum(report["decisions"]["counts"].values()) == len(decisions)
        failures = next(d for d in decisions if d["id"] == "ops_payment_failures")
        assert failures["evidence"]["failure_share_pct"] == 50.0
        assert failures["action"]["href"].startswith("/payments")


class TestOperationalSignals:
    def test_each_signal_fires_on_its_threshold_and_not_below(self):
        busy = {
            "scheduler_age_seconds": 600,
            "payments": {"current": {"success": 3, "failed": 3}, "previous": {"success": 4, "failed": 0}},
            "stale_payments": 2,
            "active_users": {"current": 10, "previous": 20},
            "api": {"requests": 100, "server_errors": 10, "error_rate_pct": 10.0},
        }
        ids = {s["id"] for s in cc.operational_signals(busy)}
        assert ids == {"ops_scheduler_stale", "ops_payment_failures", "ops_payment_prompts_waiting",
                       "ops_active_users_down", "ops_api_errors"}

        quiet = {
            "scheduler_age_seconds": 60,
            "payments": {"current": {"success": 10, "failed": 2}, "previous": {"success": 10, "failed": 2}},
            "stale_payments": 0,
            "active_users": {"current": 9, "previous": 9},
            "api": {"requests": 20, "server_errors": 5, "error_rate_pct": 25.0},
        }
        assert cc.operational_signals(quiet) == []

    def test_no_signal_is_raised_from_missing_figures(self):
        assert cc.operational_signals({"scheduler_age_seconds": None, "active_users": {"current": None}}) == []


# ── this process's request counters ──────────────────────────────────────────

class TestRequestWindow:
    def test_error_rate_and_percentiles_over_the_last_fifteen_minutes(self):
        clock = [1_000_020.0]
        window = request_metrics.RequestWindow(clock=lambda: clock[0], rng=random.Random(1))
        clock[0] += 31 * 60
        for ms in (10, 20, 30, 40):
            window.record(200, ms)
        window.record(503, 100)
        window.record(404, 5)
        window.record(500, 50, now=clock[0] - 20 * 60)
        summary = window.summary()
        current = summary["current"]
        assert (current["requests"], current["server_errors"]) == (6, 1)
        assert current["error_rate_pct"] == round(1 / 6 * 100, 2)
        assert (current["p50_ms"], current["p95_ms"], current["p99_ms"]) == (20.0, 100.0, 100.0)
        assert summary["current_complete"] is True
        assert summary["previous"]["requests"] == 1
        assert summary["previous"]["error_rate_pct"] == 100.0

    def test_a_new_process_reports_no_previous_window(self):
        window = request_metrics.RequestWindow(clock=lambda: 5_000_000.0)
        window.record(200, 12)
        summary = window.summary()
        assert summary["current_complete"] is False
        assert summary["previous"] is None
        assert summary["current"]["requests"] == 1

    def test_a_busy_minute_keeps_a_bounded_sample(self):
        window = request_metrics.RequestWindow(clock=lambda: 7_000_000.0, rng=random.Random(2))
        for i in range(2000):
            window.record(200, float(i))
        current = window.summary()["current"]
        assert current["requests"] == 2000
        assert current["latency_samples"] == request_metrics.SAMPLES_PER_BUCKET

    def test_old_minutes_are_dropped(self):
        clock = [9_000_000.0]
        window = request_metrics.RequestWindow(clock=lambda: clock[0])
        window.record(200, 1, now=clock[0] - 45 * 60)
        window.record(200, 1)
        assert len(window._buckets) == 1

    def test_counting_never_fails_a_request(self, monkeypatch):
        class Broken:
            def record(self, *args, **kwargs):
                raise RuntimeError("counter broken")
        monkeypatch.setattr(request_metrics, "recorder", Broken())
        request_metrics.record(200, 5.0)


@pytest.mark.asyncio
class TestLatencyMiddlewareCounts:
    async def test_answered_and_raised_requests_are_counted(self, monkeypatch):
        fresh = request_metrics.RequestWindow()
        monkeypatch.setattr(request_metrics, "recorder", fresh)

        async def ok(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def boom(scope, receive, send):
            raise RuntimeError("unhandled")

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            return None

        scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
        await LatencyLogMiddleware(ok)(scope, receive, send)
        with pytest.raises(RuntimeError):
            await LatencyLogMiddleware(boom)(scope, receive, send)
        current = fresh.summary()["current"]
        assert (current["requests"], current["server_errors"]) == (2, 1)


# ── the route ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRoute:
    async def test_an_administrator_reads_it(self, client, report):
        async with AsyncSessionLocal() as db:
            admin = _user(created_at=datetime.utcnow(), is_admin=True)
            db.add(admin)
            await db.commit()
        r = await client.get("/admin/command-center", params={"window_days": 30}, headers=_auth(admin.id))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["window"]["days"] == 30
        assert {"groups", "decisions", "pulse", "generated_at"} <= set(body)

    async def test_others_are_refused_and_the_window_is_bounded(self, client, report):
        async with AsyncSessionLocal() as db:
            plain, admin = _user(created_at=datetime.utcnow()), _user(created_at=datetime.utcnow(), is_admin=True)
            db.add_all([plain, admin])
            await db.commit()
        assert (await client.get("/admin/command-center", headers=_auth(plain.id))).status_code == 403
        for days in (0, 91):
            r = await client.get("/admin/command-center", params={"window_days": days}, headers=_auth(admin.id))
            assert r.status_code == 422
