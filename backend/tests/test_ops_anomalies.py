"""Operations anomalies: a flag for a real change, nothing for a normal day,
and no verdict where the history or the samples are missing.

Each test seeds its own samples. One sample an hour stands in for the
per-minute sampler, with MIN_SAMPLES_PER_DAY lowered to match.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from api.core import ops_anomalies, request_metrics
from api.database import AccountType, AsyncSessionLocal, User, init_db, reset_engine
from api.models.ops_sample import OpsSample
from api.security import create_access_token
from main import app

NOW = datetime(2025, 6, 15, 12, 0)
EVALUATED = datetime(2025, 6, 14)
FIRST = EVALUATED - timedelta(days=ops_anomalies.BASELINE_DAYS)
BUCKETS = len(request_metrics.LATENCY_BOUNDS_MS) + 1
MID = len(request_metrics.LATENCY_BOUNDS_MS) // 2
PER_DAY = 24


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_ops_anomalies.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    reset_engine()
    yield
    mp.undo()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_db(set_test_db):
    await init_db()


@pytest_asyncio.fixture(scope="module")
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def small_days(monkeypatch):
    monkeypatch.setattr(ops_anomalies, "MIN_SAMPLES_PER_DAY", PER_DAY)


def _h(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


def _sample(at: datetime, *, requests: int = 100, errors: int = 1, db_failures: int = 0) -> OpsSample:
    hist = [0] * BUCKETS
    hist[MID] = requests
    return OpsSample(
        instance="i-1", period_start=at, period_end=at + timedelta(minutes=1), requests=requests,
        server_errors=errors, client_errors=0, latency_histogram=hist, db_checked_out=1,
        db_max_connections=5, counters={"db.query_failures": db_failures} if db_failures else {},
        gauges={}, endpoints=[], created_at=at,
    )


def _day(day: datetime, **kwargs) -> list[OpsSample]:
    return [_sample(day + timedelta(hours=h), **kwargs) for h in range(PER_DAY)]


def _baseline(**kwargs) -> list[OpsSample]:
    rows: list[OpsSample] = []
    for offset in range(ops_anomalies.BASELINE_DAYS):
        rows += _day(FIRST + timedelta(days=offset), **kwargs)
    return rows


async def _store(rows: list[OpsSample]) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(OpsSample))
        db.add_all(rows)
        await db.commit()


async def _detect() -> dict:
    async with AsyncSessionLocal() as db:
        return await ops_anomalies.detect(db, now=NOW)


def _figure(report: dict, key: str) -> dict:
    return next(f for f in report["figures"] if f["key"] == key)


@pytest.mark.asyncio
async def test_a_normal_day_is_not_flagged():
    await _store(_baseline() + _day(EVALUATED))
    report = await _detect()
    assert _figure(report, "api_error_rate")["status"] == "normal"
    assert report["anomalies"] == []
    assert report["evaluated_day"] == "2025-06-14"


@pytest.mark.asyncio
async def test_an_error_spike_carries_what_an_alert_must_say():
    await _store(_baseline() + _day(EVALUATED, errors=30))
    report = await _detect()
    flagged = next(a for a in report["anomalies"] if a["key"] == "api_error_rate")
    assert flagged["status"] == "flagged"
    assert flagged["severity"] == "high"
    assert flagged["direction"] == "up"
    assert flagged["date"] == "2025-06-14"
    assert flagged["baseline_days"] == 28
    assert flagged["baseline_median"] == pytest.approx(1.0)
    assert flagged["observed"] == pytest.approx(30.0)
    assert flagged["deviation"]["absolute"] == pytest.approx(29.0)
    assert "30.0%" in flagged["what_happened"] and "14 Jun 2025" in flagged["what_happened"]
    assert flagged["impact"] and flagged["suggested_action"]
    assert flagged["source"] == "ops_samples"
    assert {e["source"] for e in flagged["evidence"]} == {"ops_samples"}


@pytest.mark.asyncio
async def test_failed_database_statements_are_flagged_against_a_quiet_baseline():
    await _store(_baseline() + _day(EVALUATED, db_failures=40))
    report = await _detect()
    keys = {a["key"] for a in report["anomalies"]}
    assert "db_failed_statements" in keys
    assert _figure(report, "api_error_rate")["status"] == "normal"


@pytest.mark.asyncio
async def test_too_little_history_is_reported_and_never_scored():
    rows: list[OpsSample] = []
    for back in (3, 2, 1):
        rows += _day(EVALUATED - timedelta(days=back))
    rows += _day(EVALUATED, errors=30)
    await _store(rows)
    report = await _detect()
    figure = _figure(report, "api_error_rate")
    assert figure["status"] == "insufficient_history"
    assert figure["baseline_days"] == 3
    assert figure["z_score"] is None
    assert report["anomalies"] == []


@pytest.mark.asyncio
async def test_a_day_the_sampler_missed_is_not_measured_and_not_zero():
    await _store(_baseline())
    report = await _detect()
    figure = _figure(report, "api_error_rate")
    assert figure["status"] == "not_measured"
    assert figure["observed"] is None
    assert report["anomalies"] == []


@pytest.mark.asyncio
async def test_the_route_answers_admins_and_refuses_buyers(client):
    async with AsyncSessionLocal() as db:
        db.add_all([
            User(id="ano-admin", name="Ano admin", phone="+254711000301", password_hash="x", is_admin=True,
                 created_at=NOW, account_type=AccountType.buyer),
            User(id="ano-buyer", name="Ano buyer", phone="+254711000302", password_hash="x", is_admin=False,
                 created_at=NOW, account_type=AccountType.buyer),
        ])
        await db.commit()
    ok = await client.get("/admin/operations/anomalies", headers=_h("ano-admin"))
    assert ok.status_code == 200
    body = ok.json()
    assert {"anomalies", "figures", "evaluated_day", "not_covered"} <= set(body)
    assert len(body["figures"]) == len(ops_anomalies.FIGURES)
    refused = await client.get("/admin/operations/anomalies", headers=_h("ano-buyer"))
    assert refused.status_code == 403
