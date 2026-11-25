"""Signed-in sessions, foreground activity and the admin figures built on them.

The rules are in api/core/activity.py. These tests pin the ones an operator
would notice if they broke: a background or legacy beat is not counted as
use, a silent session closes at its last heartbeat, a logout or revocation
cannot be reopened, rotation keeps the session, and the figures agree with
the activity records they are computed from.

Service-level tests use fixed timestamps (T0 and the isolated dates below),
so they do not depend on the clock or on each other. The HTTP tests use the
real clock and assert on what they create.
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from api.core import activity
from api.database import AsyncSessionLocal, RefreshToken, User, init_db, reset_engine
from api.domains.auth.refresh_router import ROTATE_AFTER
from api.models.user_activity import UserActivityDay, UserSession
from api.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
)
from main import app

PASSWORD = "Passw0rd!x"
# The module shares one database, so every fixed date here is kept away from
# every other test's 30-day window: the crediting tests used to write rows on
# a day inside the DAU/WAU/MAU test's week, and it counted them.
T0 = datetime(2023, 6, 14, 9, 0, 0)


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_user_activity.db"
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


# ── helpers ──────────────────────────────────────────────────────────────────

def _phone() -> str:
    return "+2547" + str(uuid.uuid4().int)[:8]


async def _user(*, admin: bool = False, name: str = "Activity", created_at: datetime | None = None) -> User:
    user = User(name=name, phone=_phone(), password_hash=hash_password(PASSWORD), is_admin=admin)
    if created_at is not None:
        user.created_at = created_at
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
    return user


async def _user_row(user_id: str) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


async def _session(
    user_id: str,
    *,
    started_at: datetime,
    last_active_at: datetime,
    foreground_seconds: int = 0,
    ended_at: datetime | None = None,
    end_reason: str | None = None,
) -> str:
    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(UserSession(
            id=session_id, user_id=user_id, started_at=started_at,
            last_active_at=last_active_at, foreground_seconds=foreground_seconds,
            ended_at=ended_at, end_reason=end_reason,
        ))
        await db.commit()
    return session_id


async def _session_row(session_id: str) -> UserSession:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(UserSession).where(UserSession.id == session_id)
        )).scalar_one()


async def _day_row(user_id: str, day: date) -> UserActivityDay | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(UserActivityDay).where(
            UserActivityDay.user_id == user_id,
            UserActivityDay.activity_date == day,
        ))).scalar_one_or_none()


async def _activity_day(user_id: str, day: date, seconds: int = 60) -> None:
    midnight = datetime.combine(day, time())
    async with AsyncSessionLocal() as db:
        db.add(UserActivityDay(
            user_id=user_id, activity_date=day, first_active_at=midnight,
            last_active_at=midnight, foreground_seconds=seconds,
        ))
        await db.commit()


async def _beat(user_id: str, session_id: str | None, *, now: datetime, foreground: bool = True) -> None:
    async with AsyncSessionLocal() as db:
        await activity.record_heartbeat(db, user_id, session_id, foreground=foreground, now=now)
        await db.commit()


def _auth(user_id: str, session_id: str | None = None) -> dict:
    claims = {"sub": user_id}
    if session_id:
        claims["sid"] = session_id
    return {"Authorization": f"Bearer {create_access_token(claims)}"}


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ── crediting foreground time ────────────────────────────────────────────────

@pytest.mark.asyncio
class TestCreditingForegroundTime:
    async def test_a_beat_credits_the_gap_since_the_previous_beat(self):
        user = await _user()
        sid = await _session(user.id, started_at=T0, last_active_at=T0)
        await _beat(user.id, sid, now=_at(60))

        row = await _session_row(sid)
        assert row.foreground_seconds == 60
        assert row.last_active_at == _at(60)
        day = await _day_row(user.id, T0.date())
        assert day is not None and day.foreground_seconds == 60

    async def test_beats_inside_the_write_interval_store_nothing(self):
        user = await _user()
        sid = await _session(user.id, started_at=T0, last_active_at=T0)
        await _beat(user.id, sid, now=_at(10))

        row = await _session_row(sid)
        assert row.foreground_seconds == 0
        assert row.last_active_at == T0
        assert await _day_row(user.id, T0.date()) is None

    async def test_a_gap_longer_than_the_continuity_limit_earns_nothing(self):
        user = await _user()
        sid = await _session(user.id, started_at=T0, last_active_at=T0)
        gap = activity.HEARTBEAT_CONTINUITY_SECONDS + 1
        await _beat(user.id, sid, now=_at(gap))

        row = await _session_row(sid)
        assert row.foreground_seconds == 0
        assert row.last_active_at == _at(gap)

    async def test_a_background_beat_is_not_activity(self):
        user = await _user()
        sid = await _session(user.id, started_at=T0, last_active_at=T0)
        await _beat(user.id, sid, now=_at(60), foreground=False)

        row = await _session_row(sid)
        assert row.foreground_seconds == 0
        assert row.last_active_at == T0
        assert (await _user_row(user.id)).last_seen is None

    async def test_a_token_without_a_session_counts_only_as_presence(self):
        user = await _user()
        await _beat(user.id, None, now=T0)

        assert (await _user_row(user.id)).last_seen == T0
        async with AsyncSessionLocal() as db:
            count = (await db.execute(
                select(func.count(UserSession.id)).where(UserSession.user_id == user.id)
            )).scalar()
        assert count == 0

    async def test_the_first_beat_of_a_day_opens_its_row_and_later_beats_add_to_it(self):
        user = await _user()
        sid = await _session(user.id, started_at=T0, last_active_at=T0)
        await _beat(user.id, sid, now=_at(60))
        await _beat(user.id, sid, now=_at(120))

        day = await _day_row(user.id, T0.date())
        assert day.foreground_seconds == 120
        assert day.first_active_at == _at(60)


# ── silent sessions ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSilentSessions:
    async def test_a_silent_session_closes_at_its_last_heartbeat(self):
        user = await _user()
        sid = await _session(
            user.id, started_at=T0 - timedelta(minutes=30), last_active_at=T0,
            foreground_seconds=600,
        )
        now = _at(activity.SESSION_IDLE_CLOSE_SECONDS + 60)
        await activity.run_sweep(now=now)

        row = await _session_row(sid)
        assert row.end_reason == "inactive"
        assert row.ended_at == T0, "silence is not usage: the close is at the last beat"
        assert row.foreground_seconds == 600

    async def test_a_heartbeat_reopens_an_inactivity_close_without_crediting_the_silence(self):
        user = await _user()
        sid = await _session(
            user.id, started_at=T0, last_active_at=T0, foreground_seconds=60,
            ended_at=T0, end_reason="inactive",
        )
        await _beat(user.id, sid, now=_at(2 * 3600))

        row = await _session_row(sid)
        assert row.ended_at is None and row.end_reason is None
        assert row.foreground_seconds == 60
        assert row.last_active_at == _at(2 * 3600)

    async def test_a_logout_is_never_reopened_by_a_heartbeat(self):
        user = await _user()
        sid = await _session(
            user.id, started_at=T0, last_active_at=T0,
            ended_at=_at(30), end_reason="logout",
        )
        await _beat(user.id, sid, now=_at(600))

        row = await _session_row(sid)
        assert row.end_reason == "logout"
        assert row.ended_at == _at(30)
        assert row.foreground_seconds == 0

    async def test_a_stronger_reason_replaces_an_inactivity_close_and_keeps_its_end_time(self):
        user = await _user()
        sid = await _session(
            user.id, started_at=T0, last_active_at=T0,
            ended_at=T0, end_reason="inactive",
        )
        async with AsyncSessionLocal() as db:
            await activity.end_session(db, sid, "suspended", now=_at(3600))
            await db.commit()

        row = await _session_row(sid)
        assert row.end_reason == "suspended"
        assert row.ended_at == T0


# ── signed-in sessions over HTTP ─────────────────────────────────────────────

async def _login(client, user: User) -> dict:
    r = await client.post("/auth/login", json={"phone": user.phone, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
class TestSignedInSessions:
    async def test_login_opens_a_session_and_the_access_token_carries_its_id(self, client):
        user = await _user()
        body = await _login(client, user)

        sid = decode_access_token(body["access_token"])["sid"]
        row = await _session_row(sid)
        assert row.user_id == user.id
        assert row.ended_at is None

    async def test_logout_ends_its_session_and_later_heartbeats_do_not_count(self, client):
        user = await _user()
        body = await _login(client, user)
        sid = decode_access_token(body["access_token"])["sid"]

        r = await client.post("/auth/token/revoke", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 204
        before = await _session_row(sid)
        assert before.end_reason == "logout"

        beat = await client.patch(
            "/auth/heartbeat", headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert beat.status_code == 200
        after = await _session_row(sid)
        assert after.last_active_at == before.last_active_at
        assert after.end_reason == "logout"

    async def test_revoke_all_ends_every_session_of_the_account(self, client):
        user = await _user()
        first = await _login(client, user)
        second = await _login(client, user)
        sids = [decode_access_token(body["access_token"])["sid"] for body in (first, second)]

        r = await client.post(
            "/auth/token/revoke-all", headers={"Authorization": f"Bearer {first['access_token']}"},
        )
        assert r.status_code == 204
        for sid in sids:
            row = await _session_row(sid)
            assert row.end_reason == "revoked"
            assert row.ended_at is not None

    async def test_rotation_keeps_the_same_session(self, client):
        user = await _user()
        body = await _login(client, user)
        sid = decode_access_token(body["access_token"])["sid"]
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user.id)
                .values(created_at=datetime.utcnow() - ROTATE_AFTER - timedelta(hours=1))
            )
            await db.commit()

        r = await client.post("/auth/token/refresh", json={"refresh_token": body["refresh_token"]})
        assert r.status_code == 200, r.text
        assert "refresh_token" in r.json(), "the token was old enough to rotate"
        assert decode_access_token(r.json()["access_token"])["sid"] == sid
        async with AsyncSessionLocal() as db:
            count = (await db.execute(
                select(func.count(UserSession.id)).where(UserSession.user_id == user.id)
            )).scalar()
        assert count == 1

    async def test_a_token_from_before_sessions_gets_one_on_its_first_exchange(self, client):
        user = await _user()
        token, expiry, jti = create_refresh_token(user.id)
        async with AsyncSessionLocal() as db:
            db.add(RefreshToken(
                user_id=user.id, jti=jti,
                expires_at=expiry.astimezone(timezone.utc).replace(tzinfo=None),
            ))
            await db.commit()

        r = await client.post("/auth/token/refresh", json={"refresh_token": token})
        assert r.status_code == 200, r.text
        sid = decode_access_token(r.json()["access_token"])["sid"]
        assert sid
        async with AsyncSessionLocal() as db:
            stored = (await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))).scalar_one()
        assert stored.session_id == sid
        assert (await _session_row(sid)).user_id == user.id

    async def test_a_legacy_token_heartbeat_updates_presence_only(self, client):
        user = await _user()
        r = await client.patch("/auth/heartbeat", headers=_auth(user.id))
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert (await _user_row(user.id)).last_seen is not None
        async with AsyncSessionLocal() as db:
            count = (await db.execute(
                select(func.count(UserSession.id)).where(UserSession.user_id == user.id)
            )).scalar()
        assert count == 0


# ── engagement figures ───────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestEngagementFigures:
    async def test_dau_wau_and_mau_count_distinct_accounts_on_their_days(self):
        day = date(2024, 3, 20)
        a, b, c, d = (await _user() for _ in range(4))
        await _activity_day(a.id, day)
        await _activity_day(a.id, day - timedelta(days=1))
        await _activity_day(b.id, day - timedelta(days=5))
        await _activity_day(c.id, day - timedelta(days=20))
        await _activity_day(d.id, day - timedelta(days=40))

        start = datetime.combine(day, time())
        async with AsyncSessionLocal() as db:
            figures = await activity.engagement_summary(
                db, start=start, end=start + timedelta(days=1), now=start + timedelta(days=1),
            )
        assert figures["dau"] == 1
        assert figures["wau"] == 2
        assert figures["mau"] == 3, "the account from 40 days back is outside the 30-day window"
        assert figures["dau_mau_ratio"] == round(1 / 3, 4)

    async def test_session_durations_average_and_median_closed_sessions_only(self):
        base = datetime(2024, 4, 2, 8, 0, 0)
        user = await _user()
        await _session(user.id, started_at=base, last_active_at=base, foreground_seconds=60,
                       ended_at=base, end_reason="logout")
        await _session(user.id, started_at=base + timedelta(hours=1),
                       last_active_at=base + timedelta(hours=1), foreground_seconds=120,
                       ended_at=base + timedelta(hours=1), end_reason="inactive")
        await _session(user.id, started_at=base + timedelta(hours=2),
                       last_active_at=base + timedelta(hours=2), foreground_seconds=600,
                       ended_at=base + timedelta(hours=2), end_reason="logout")
        await _session(user.id, started_at=base + timedelta(hours=3),
                       last_active_at=base + timedelta(hours=3), foreground_seconds=9999)

        start = datetime(2024, 4, 2)
        async with AsyncSessionLocal() as db:
            figures = await activity.engagement_summary(
                db, start=start, end=start + timedelta(days=1), now=start + timedelta(days=1),
            )
        assert figures["closed_sessions_in_range"] == 3
        assert figures["average_session_foreground_seconds"] == 260.0
        assert figures["median_session_foreground_seconds"] == 120.0

    async def test_new_and_returning_accounts_split_on_earlier_activity(self):
        day = date(2024, 7, 1)
        newcomer, returning = await _user(), await _user()
        await _activity_day(returning.id, day - timedelta(days=3))
        await _activity_day(newcomer.id, day)
        await _activity_day(returning.id, day)

        start = datetime.combine(day, time())
        async with AsyncSessionLocal() as db:
            figures = await activity.engagement_summary(
                db, start=start, end=start + timedelta(days=1), now=start + timedelta(days=1),
            )
        assert figures["active_users_in_range"] == 2
        assert figures["returning_active_users"] == 1
        assert figures["new_active_users"] == 1

    async def test_retention_scores_a_matured_cohort_registered_after_tracking_began(self):
        # Any activity before the cohort day marks tracking as begun; this
        # day is outside every other test's window.
        pioneer = await _user()
        await _activity_day(pioneer.id, date(2022, 1, 3))
        member = await _user(created_at=datetime(2024, 10, 1, 10, 0, 0))
        await _activity_day(member.id, date(2024, 10, 2))

        async with AsyncSessionLocal() as db:
            result = await activity.registration_retention(
                db,
                start=datetime(2024, 10, 1),
                end=datetime(2024, 10, 2),
                now=datetime(2024, 11, 20, 12, 0, 0),
            )
        cohort = next(c for c in result["cohorts"] if c["cohort_date"] == "2024-10-01")
        assert cohort["registered"] == 1
        assert cohort["d1_retained"] == 1 and cohort["d1_pct"] == 100.0
        assert cohort["d7_retained"] == 0 and cohort["d7_pct"] == 0.0

    async def test_active_users_series_is_null_before_tracking_and_counts_after(self):
        user, other = await _user(), await _user()
        first = date(2024, 5, 10)
        await _activity_day(user.id, first)
        await _activity_day(user.id, first + timedelta(days=2))
        await _activity_day(other.id, first + timedelta(days=2))
        buckets = [
            (datetime.combine(first + timedelta(days=i), time()),
             datetime.combine(first + timedelta(days=i + 1), time()))
            for i in range(4)
        ]
        async with AsyncSessionLocal() as db:
            series = await activity.active_users_series(db, buckets, "day")
            coverage = await activity._coverage_start(db)

        assert series["coverage_started_on"] == coverage.isoformat()
        values = {point["t"][:10]: point["v"] for point in series["points"]}
        for b_start, _ in buckets:
            if b_start.date() > coverage:
                assert isinstance(values[b_start.date().isoformat()], int)
            else:
                assert values[b_start.date().isoformat()] is None
        assert values["2024-05-10"] == 1
        assert values["2024-05-11"] == 0
        assert values["2024-05-12"] == 2


# ── admin reads ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAdminReads:
    async def test_live_counts_an_open_session_with_a_recent_heartbeat(self, client):
        admin = await _user(admin=True)
        member = await _user()
        now = datetime.utcnow()
        await _session(member.id, started_at=now - timedelta(minutes=2),
                       last_active_at=now - timedelta(minutes=1))

        r = await client.get("/admin/activity/live", headers=_auth(admin.id))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["active_users_now"] >= 1
        assert body["active_sessions_now"] >= 1
        assert set(body["active_users"]) == {"last_5m", "last_15m", "last_60m"}
        assert body["active_users"]["last_5m"] >= 1
        assert body["registered_users"] >= 2
        assert set(body["peak_concurrent_users_24h"]) == {
            "value", "sampled_at", "sample_interval_seconds", "samples_in_window",
        }

    async def test_the_figures_need_the_permission_for_their_kind(self, client):
        plain = await _user()
        for path in (
            "/admin/activity/live",
            "/admin/activity/engagement",
            "/admin/activity/retention",
            "/admin/activity/users",
        ):
            r = await client.get(path, headers=_auth(plain.id))
            assert r.status_code == 403, path

    async def test_the_user_table_and_profile_show_recent_activity(self, client):
        admin = await _user(admin=True)
        member = await _user(name="Table Member")
        now = datetime.utcnow()
        await _session(member.id, started_at=now - timedelta(hours=2),
                       last_active_at=now - timedelta(minutes=10),
                       ended_at=now - timedelta(minutes=10), end_reason="inactive")
        await _activity_day(member.id, now.date(), seconds=300)

        r = await client.get(
            "/admin/activity/users", params={"search": "Table Member"}, headers=_auth(admin.id),
        )
        assert r.status_code == 200, r.text
        assert r.headers["X-Total-Count"] == "1"
        row = r.json()[0]
        assert row["id"] == member.id
        assert row["sessions_30d"] == 1
        assert row["active_days_30d"] == 1
        assert row["foreground_seconds_30d"] == 300
        assert "phone" not in row and "email" not in row

        profile = await client.get(f"/admin/activity/users/{member.id}", headers=_auth(admin.id))
        assert profile.status_code == 200, profile.text
        body = profile.json()
        assert body["totals"]["sessions"] == 1
        assert body["last_30_days"]["foreground_seconds"] == 300
        assert body["recent_sessions"][0]["end_reason"] == "inactive"

        missing = await client.get("/admin/activity/users/no-such-user", headers=_auth(admin.id))
        assert missing.status_code == 404

    async def test_retention_refuses_a_range_longer_than_its_limit(self, client):
        admin = await _user(admin=True)
        r = await client.get(
            "/admin/activity/retention",
            params={"from": "2023-01-01T00:00:00Z", "to": "2024-01-01T00:00:00Z"},
            headers=_auth(admin.id),
        )
        assert r.status_code == 422

    async def test_engagement_answers_for_a_range(self, client):
        admin = await _user(admin=True)
        r = await client.get(
            "/admin/activity/engagement",
            params={"from": "2024-01-01T00:00:00Z", "to": "2024-01-08T00:00:00Z"},
            headers=_auth(admin.id),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {"dau", "wau", "mau", "dau_mau_ratio", "coverage_started_on"} <= set(body)
