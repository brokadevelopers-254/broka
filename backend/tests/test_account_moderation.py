"""Restriction, suspension and reinstatement, enforced by the backend.

The middleware, the sign-in paths and the refresh path are exercised through
the real app, so a suspended account is refused wherever it authenticates.
"""
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from api.core import account_status
from api.core.permissions import Permission, get_user_permissions
from api.database import AsyncSessionLocal, AuditLog, User, init_db, reset_engine
from api.models.account_moderation import AccountModerationEvent
from api.models.user_activity import UserSession
from api.security import create_access_token, decode_access_token, hash_password
from main import app

PASSWORD = "Passw0rd!x"


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_account_moderation.db"
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


async def _user(*, admin: bool = False) -> User:
    user = User(name="Moderated", phone=_phone(), password_hash=hash_password(PASSWORD), is_admin=admin)
    async with AsyncSessionLocal() as db:
        db.add(user)
        await db.commit()
    return user


async def _user_row(user_id: str) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


async def _login(client, user: User) -> dict:
    r = await client.post("/auth/login", json={"phone": user.phone, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


async def _version(client, admin: User, target: User) -> int:
    r = await client.get(f"/admin/users/{target.id}/moderation", headers=_auth(admin.id))
    assert r.status_code == 200, r.text
    return r.json()["status_version"]


async def _change(client, admin: User, target: User, action: str, **extra):
    body = {"reason": "Coordinated refund abuse", "expected_version": await _version(client, admin, target), **extra}
    return await client.post(f"/admin/users/{target.id}/{action}", json=body, headers=_auth(admin.id))


# ── suspension ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSuspension:
    async def test_a_suspended_account_cannot_sign_in_or_keep_its_access(self, client):
        admin = await _user(admin=True)
        target = await _user()
        tokens = await _login(client, target)

        r = await _change(client, admin, target, "suspend", duration="permanent")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "suspended"

        again = await client.post("/auth/login", json={"phone": target.phone, "password": PASSWORD})
        assert again.status_code == 403

        # The token issued before the suspension is refused on the next request.
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert me.status_code == 403
        beat = await client.patch("/auth/heartbeat", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert beat.status_code == 403

        # The refresh token was revoked by the suspension, and a refresh is refused.
        refresh = await client.post("/auth/token/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert refresh.status_code in (401, 403)

    async def test_suspension_ends_the_sessions_and_revokes_the_refresh_tokens(self, client):
        admin = await _user(admin=True)
        target = await _user()
        tokens = await _login(client, target)
        sid = decode_access_token(tokens["access_token"])["sid"]

        assert (await _change(client, admin, target, "suspend", duration="7d")).status_code == 200

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(UserSession).where(UserSession.id == sid))).scalar_one()
        assert row.end_reason == "suspended"
        assert row.ended_at is not None

    async def test_a_temporary_suspension_ends_by_itself_and_the_end_is_recorded(self, client):
        admin = await _user(admin=True)
        target = await _user()
        r = await _change(client, admin, target, "suspend", duration="24h")
        assert r.status_code == 200, r.text
        assert r.json()["suspended_until"] is not None

        # Time passes: the suspension is over without any action taken.
        async with AsyncSessionLocal() as db:
            await db.execute(update(User).where(User.id == target.id).values(
                suspended_until=datetime.utcnow() - timedelta(minutes=1)))
            await db.commit()
        account_status.forget(target.id)

        tokens = await _login(client, target)
        assert tokens["access_token"]
        expired = await account_status.expire_due_suspensions()
        assert expired >= 1

        history = (await client.get(f"/admin/users/{target.id}/moderation", headers=_auth(admin.id))).json()
        assert history["status"] == "active"
        assert any(event["action"] == "suspension_expired" for event in history["history"])

    async def test_a_permanent_suspension_does_not_expire(self, client):
        admin = await _user(admin=True)
        target = await _user()
        assert (await _change(client, admin, target, "suspend", duration="permanent")).status_code == 200
        await account_status.expire_due_suspensions()

        row = await _user_row(target.id)
        assert row.account_status == "suspended"
        assert row.suspended_until is None
        r = await client.post("/auth/login", json={"phone": target.phone, "password": PASSWORD})
        assert r.status_code == 403

    async def test_reinstating_restores_sign_in(self, client):
        admin = await _user(admin=True)
        target = await _user()
        assert (await _change(client, admin, target, "suspend", duration="30d")).status_code == 200
        reinstated = await _change(client, admin, target, "reinstate")
        assert reinstated.status_code == 200, reinstated.text
        assert reinstated.json()["status"] == "active"
        assert (await _login(client, target))["access_token"]

        history = (await client.get(f"/admin/users/{target.id}/moderation", headers=_auth(admin.id))).json()
        assert [event["action"] for event in history["history"]][:2] == ["reinstated", "suspended"]


# ── restriction ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRestriction:
    async def test_a_restricted_account_signs_in_but_loses_transactional_permissions(self, client):
        admin = await _user(admin=True)
        target = await _user()
        assert (await _change(client, admin, target, "restrict")).status_code == 200
        assert (await _login(client, target))["access_token"]

        row = await _user_row(target.id)
        permissions = get_user_permissions(row)
        assert Permission.CREATE_LISTING not in permissions
        assert Permission.FINALIZE_DEAL not in permissions
        assert Permission.OPEN_DISPUTE in permissions

    async def test_restricting_a_suspended_account_is_refused(self, client):
        admin = await _user(admin=True)
        target = await _user()
        assert (await _change(client, admin, target, "suspend", duration="7d")).status_code == 200
        r = await _change(client, admin, target, "restrict")
        assert r.status_code == 409


# ── who may act ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestWhoMayAct:
    async def test_an_ordinary_user_cannot_change_an_account_status(self, client):
        ordinary = await _user()
        target = await _user()
        r = await client.post(
            f"/admin/users/{target.id}/suspend",
            json={"reason": "not my call", "expected_version": 0, "duration": "24h"},
            headers=_auth(ordinary.id),
        )
        assert r.status_code == 403
        assert (await _user_row(target.id)).account_status == "active"

    async def test_an_administrator_cannot_suspend_themselves(self, client):
        admin = await _user(admin=True)
        r = await client.post(
            f"/admin/users/{admin.id}/suspend",
            json={"reason": "a mistake", "expected_version": 0, "duration": "24h"},
            headers=_auth(admin.id),
        )
        assert r.status_code == 403

    async def test_an_administrator_account_is_changed_only_after_demotion(self, client):
        admin = await _user(admin=True)
        other_admin = await _user(admin=True)
        r = await client.post(
            f"/admin/users/{other_admin.id}/suspend",
            json={"reason": "a dispute between admins", "expected_version": 0, "duration": "24h"},
            headers=_auth(admin.id),
        )
        assert r.status_code == 409
        assert (await _user_row(other_admin.id)).account_status == "active"

    async def test_a_stale_version_is_refused_and_nothing_changes(self, client):
        admin = await _user(admin=True)
        target = await _user()
        r = await client.post(
            f"/admin/users/{target.id}/suspend",
            json={"reason": "too late", "expected_version": 99, "duration": "24h"},
            headers=_auth(admin.id),
        )
        assert r.status_code == 409
        assert (await _user_row(target.id)).account_status == "active"

    async def test_an_unknown_account_is_not_found(self, client):
        admin = await _user(admin=True)
        r = await client.post(
            "/admin/users/no-such-user/restrict",
            json={"reason": "no such person", "expected_version": 0},
            headers=_auth(admin.id),
        )
        assert r.status_code == 404


# ── record ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRecord:
    async def test_every_change_is_in_the_audit_log_and_the_account_history(self, client):
        admin = await _user(admin=True)
        target = await _user()
        r = await client.post(
            f"/admin/users/{target.id}/suspend",
            json={
                "reason": "Chargeback pattern across three accounts",
                "notes": "Linked by device fingerprint",
                "expected_version": 0,
                "duration": "7d",
                "evidence_refs": ["case-2291"],
            },
            headers=_auth(admin.id),
        )
        assert r.status_code == 200, r.text
        async with AsyncSessionLocal() as db:
            audit = (await db.execute(select(func.count(AuditLog.id)).where(
                AuditLog.action == "account_suspended",
                AuditLog.resource_id == target.id,
                AuditLog.actor_id == admin.id,
            ))).scalar()
            events = (await db.execute(select(AccountModerationEvent).where(
                AccountModerationEvent.user_id == target.id,
            ))).scalars().all()
        assert audit == 1
        assert len(events) == 1
        assert events[0].actor_id == admin.id
        assert events[0].from_status == "active" and events[0].to_status == "suspended"
        assert events[0].evidence_refs == '["case-2291"]'

        history = (await client.get(f"/admin/users/{target.id}/moderation", headers=_auth(admin.id))).json()
        assert history["status_changed_by"] == admin.id
        assert history["history"][0]["reason"] == "Chargeback pattern across three accounts"

    async def test_an_expired_suspension_is_not_a_suspension_in_the_status_view(self):
        expired = account_status.effective_status("suspended", datetime.utcnow() - timedelta(seconds=1), datetime.utcnow())
        pending = account_status.effective_status("suspended", datetime.utcnow() + timedelta(hours=1), datetime.utcnow())
        forever = account_status.effective_status("suspended", None, datetime.utcnow())
        assert expired == "active"
        assert pending == "suspended"
        assert forever == "suspended"
