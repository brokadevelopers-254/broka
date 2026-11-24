"""Listing / marketplace intelligence, anomaly detection and models.

Seeded marketplace (all times relative to now, so every figure is hand-checkable):

    seller   owns L1 and L2
    admin    views L1 (staff: never counted)
    v1       views L1 and L2, wishlists L1, interests L1, messages L1 twice
    v2       views L1, likes L1, messages L1
    v3       views L1
    seller   also "views" L1 (the owner: never counted)

    L1: 3 viewers, 1 saver, 1 liker, 2 contacters (v1, v2) -> 66.7% contact rate
    L2: 1 viewer

    Sixty registrations sit on the same weekday as yesterday in each of the
    previous six weeks (10 per day) and none yesterday: a registration drop.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api.core import market_models
from api.database import (
    AccountType, AsyncSessionLocal, Interest, Listing, ListingLike, ListingView,
    NegotiationMessage, User, Wishlist, init_db, reset_engine,
)
from api.models.admin_alert import AdminAlert
from api.security import create_access_token
from main import app


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / f"test_market_{uuid.uuid4().hex}.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    mp.setenv("ENV", "test")
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


def _h(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


def _user(name, *, admin=False, created=None, seller=False):
    return User(
        name=name, phone=f"+2547{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@market.test",
        password_hash="x", is_admin=admin, created_at=created or datetime.utcnow(),
        account_type=AccountType.buyer_seller if seller else AccountType.buyer,
    )


@pytest_asyncio.fixture(scope="module")
async def market():
    now = datetime.utcnow()
    target = now.date() - timedelta(days=1)
    admin, seller = _user("Admin", admin=True), _user("Seller", seller=True)
    v1, v2, v3 = _user("V1"), _user("V2"), _user("V3")
    async with AsyncSessionLocal() as db:
        db.add_all([admin, seller, v1, v2, v3])
        for k in range(1, 7):
            noon = datetime.combine(target - timedelta(days=7 * k), datetime.min.time()) + timedelta(hours=12)
            db.add_all([_user(f"Reg {k}-{i}", created=noon) for i in range(10)])
        await db.flush()

        def listing(name):
            return Listing(seller_id=seller.id, name=name, category="Electronics", location_county="Nairobi",
                           price=1000.0, lat=-1.29, lng=36.82, created_at=now - timedelta(days=3))

        l1, l2 = listing("Phone X"), listing("Laptop Y")
        db.add_all([l1, l2])
        await db.flush()
        at = now - timedelta(minutes=30)

        def message(user):
            return NegotiationMessage(
                listing_id=l1.id, sender_id=user.id, role="buyer", recipient_role="seller",
                content="hi", buyer_id=user.id, created_at=at)

        db.add_all([
            *[ListingView(listing_id=l1.id, viewer_id=u.id, created_at=at) for u in (admin, seller, v1, v2, v3)],
            ListingView(listing_id=l2.id, viewer_id=v1.id, created_at=at),
            Wishlist(user_id=v1.id, listing_id=l1.id, created_at=at),
            ListingLike(user_id=v2.id, listing_id=l1.id, created_at=at),
            Interest(listing_id=l1.id, buyer_id=v1.id, created_at=at),
            message(v1), message(v1), message(v2),
        ])
        await db.commit()
        return {"admin": admin.id, "seller": seller.id, "v1": v1.id, "v2": v2.id, "v3": v3.id,
                "l1": l1.id, "l2": l2.id, "target": target.isoformat()}


@pytest.mark.asyncio
async def test_rankings_count_distinct_users_and_exclude_staff_and_owner(client, market):
    r = await client.get("/admin/intelligence/market/rankings?window_days=7", headers=_h(market["admin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    top = body["rankings"]["viewed"][0]
    assert top["listing_id"] == market["l1"]
    assert (top["viewers"], top["savers"], top["likers"], top["contacters"]) == (3, 1, 1, 2)
    assert top["view_to_contact_pct"] == 66.7
    assert body["rankings"]["contacted"][0]["listing_id"] == market["l1"]
    assert body["rankings"]["saved"][0]["savers"] == 1
    trending = body["rankings"]["trending"][0]
    assert trending["listing_id"] == market["l1"] and trending["score"] == 15 and trending["previous_score"] == 0
    assert body["window"]["days"] == 7 and "deduplicated" in body["definitions"]

    filtered = await client.get("/admin/intelligence/market/rankings?category=Fashion", headers=_h(market["admin"]))
    assert filtered.json()["rankings"]["viewed"] == []


@pytest.mark.asyncio
async def test_listing_detail_shows_funnel_and_daily_series(client, market):
    r = await client.get(f"/admin/intelligence/market/listings/{market['l1']}?window_days=7",
                         headers=_h(market["admin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["viewers"] == 3
    assert body["funnel"] == {"viewers": 3, "savers_or_likers": 2, "contacters": 2, "deals_agreed": 0}
    assert sum(d["views"] for d in body["daily"]) == 3
    assert sum(d["contacts"] for d in body["daily"]) == 2
    assert body["category_position"]["viewers_rank"] == 1
    missing = await client.get("/admin/intelligence/market/listings/nope", headers=_h(market["admin"]))
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_performance_ranks_sellers_and_buyers(client, market):
    r = await client.get("/admin/intelligence/market/performance?window_days=7", headers=_h(market["admin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    seller = body["sellers"]["top"][0]
    assert seller["seller_id"] == market["seller"]
    assert (seller["viewers"], seller["contacters"], seller["listings_engaged"]) == (3, 2, 2)
    buyer = body["buyers"]["top"][0]
    assert buyer["buyer_id"] == market["v1"]
    assert (buyer["listings_viewed"], buyer["listings_contacted"]) == (2, 1)
    assert body["buyers"]["total"] == 3 and body["buyers"]["browsers_without_contact"] == 1


def test_a_drop_against_the_same_weekday_is_an_anomaly():
    target = date(2026, 11, 11)
    values = {(target - timedelta(days=7 * k)).isoformat(): 10 for k in range(1, 7)}
    drop = market_models.evaluate({**values, target.isoformat(): 0}, target, concern="both", key="x")
    assert drop["anomaly"]["direction"] == "drop" and drop["anomaly"]["severity"] == "high"
    assert drop["anomaly"]["baseline"]["median"] == 10 and drop["anomaly"]["deviation"]["absolute"] == -10
    assert len(drop["anomaly"]["evidence"]["baseline_days"]) == 6
    assert "never suspends" in drop["anomaly"]["auto_action"]
    assert market_models.evaluate({**values, target.isoformat(): 9}, target)["anomaly"] is None
    # A drop is not a concern for a metric that only worries about spikes.
    assert market_models.evaluate({**values, target.isoformat(): 0}, target, concern="spike")["anomaly"] is None
    # One wild week does not move a median baseline.
    odd = {**values, (target - timedelta(days=7)).isoformat(): 50, target.isoformat(): 12}
    assert market_models.evaluate(odd, target)["anomaly"] is None


@pytest.mark.asyncio
async def test_anomaly_endpoint_and_scan_raise_one_deduplicated_alert(client, market):
    h = _h(market["admin"])
    r = await client.get("/admin/intelligence/market/anomalies", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["evaluated_day"] == market["target"]
    assert [a["metric"] for a in body["anomalies"]] == ["registrations"]
    assert {n["metric"] for n in body["not_instrumented"]} == {"api", "database"}

    first = await client.post("/admin/intelligence/market/anomalies/scan", headers=h)
    assert first.status_code == 200, first.text
    assert first.json()["alerts_created"] == 1
    second = await client.post("/admin/intelligence/market/anomalies/scan", headers=h)
    assert second.json() == {**second.json(), "alerts_created": 0, "alerts_existing": 1}
    async with AsyncSessionLocal() as db:
        alerts = (await db.execute(select(AdminAlert).where(AdminAlert.kind == "anomaly_registrations"))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].meta["baseline_median"] == 10 and alerts[0].meta["evidence"]["baseline_days"]
    assert "No automatic action" in alerts[0].body and "Suggested action" in alerts[0].body


def test_forecast_is_published_only_when_it_backtests():
    today = date(2026, 11, 11)
    steady = {(today - timedelta(days=i)).isoformat(): 5 for i in range(1, 43)}
    good = market_models.forecast_metric(steady, today)
    assert good["validated"] and len(good["forecast"]) == 7 and good["forecast"][0]["expected"] == 5
    assert good["backtest"]["wape"] == 0
    quiet = market_models.forecast_metric({}, today)
    assert not quiet["validated"] and quiet["forecast"] == [] and quiet["withheld_reason"]


def test_churn_rule_flags_users_who_went_quiet():
    cutoff = date(2026, 11, 30)
    days = {(cutoff - timedelta(days=d)).isoformat() for d in (15, 18, 20, 25)}
    recent = {(cutoff - timedelta(days=d)).isoformat() for d in (1, 3, 5, 7, 9)}
    flagged = market_models.churn_flags({"quiet": days, "active": days | recent}, cutoff)
    assert flagged == {"quiet": (4, 0)}


@pytest.mark.asyncio
async def test_models_report_traces_recommendations_to_evidence(client, market):
    r = await client.get("/admin/intelligence/market/models", headers=_h(market["admin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {f["metric"] for f in body["forecasts"]} == {"registrations", "listings", "messages", "deals", "payments_success"}
    assert all(not f["validated"] and f["forecast"] == [] for f in body["forecasts"])
    assert body["churn"]["method"] == "rule" and body["payments"]["status"] == "insufficient_data"
    rec = next(x for x in body["recommendations"] if x["id"] == "anomaly:registrations")
    assert rec["evidence"] and all({"label", "value", "source"} <= set(e) for e in rec["evidence"])


@pytest.mark.asyncio
async def test_market_intelligence_is_admin_only(client, market):
    for path in ("rankings", "performance", "anomalies", "models", f"listings/{market['l1']}"):
        r = await client.get(f"/admin/intelligence/market/{path}", headers=_h(market["v1"]))
        assert r.status_code == 403, path
    r = await client.post("/admin/intelligence/market/anomalies/scan", headers=_h(market["v1"]))
    assert r.status_code == 403
