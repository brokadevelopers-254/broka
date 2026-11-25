"""Listing intelligence past the first contact: negotiation, deals, completion,
attention over time, buyers left waiting, demand growth and seller performance.

One seeded marketplace read at a fixed moment, NOW, so every figure has an
exact expected value. Current window [NOW-7d, NOW), previous [NOW-14d, NOW-7d).

    A  admin (staff: never counted)     S1 seller of lst-a, lst-b (Electronics, Nairobi)
    S2 seller of lst-c (Fashion, Mombasa)

    lst-a  current: viewed by b1 b2 b3 (and A, and S1 itself); b2 saves it;
                    b1 messages three times, S1 replies 2 h after the first;
                    b4 messages, S1 replies after 60 min; b2 offers KES 900;
                    b1's deal is paid; b3's deal (agreed last window) is released.
           older:   b4 viewed it 20 days ago (outside both windows).
    lst-b  previous: viewed by b1-b4, interests from b1-b5.
           current:  viewed by b5; b6 messages 30 h before NOW, no reply.
    lst-c  previous: viewed by b1-b5.  current: viewed by b6, interests b1-b3.

Scores (default weights views 1, saves 2, likes 2, contacts 4, negotiations 6, deals 10):
    lst-a  45 now (3 + 2 + 12 + 18 + 10), 10 before (b3's deal)
    lst-b   5 now (1 + 4),                24 before (4 + 20)
    lst-c  13 now (1 + 12),                5 before
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.core import market_intel
from api.database import (
    AccountType, AsyncSessionLocal, Deal, DealStatus, Interest, Listing, ListingView,
    NegotiationMessage, User, Wishlist, init_db, reset_engine,
)
from api.security import create_access_token
from main import app

NOW = datetime(2025, 6, 18, 12, 0, 0)
DAY = timedelta(days=1)


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_market_stages.db"
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


def _h(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


def _user(uid: str, *, admin: bool = False, seller: bool = False) -> User:
    return User(id=uid, name=uid.upper(), phone=f"+25470{random.randint(1_000_000, 9_999_999)}",
                password_hash="x", is_admin=admin, created_at=NOW - 60 * DAY,
                account_type=AccountType.buyer_seller if seller else AccountType.buyer)


def _msg(listing: str, buyer: str, sender: str, role: str, at: datetime) -> NegotiationMessage:
    return NegotiationMessage(listing_id=listing, buyer_id=buyer, sender_id=sender, role=role,
                              recipient_role="seller" if role == "buyer" else "buyer",
                              content="x", created_at=at)


@pytest_asyncio.fixture(scope="module")
async def market():
    users = [_user("u-admin", admin=True), _user("u-s1", seller=True), _user("u-s2", seller=True),
             *[_user(f"u-b{i}") for i in range(1, 7)]]

    def listing(lid: str, seller: str, category: str, county: str) -> Listing:
        return Listing(id=lid, seller_id=seller, name=lid.upper(), category=category, location_county=county,
                       price=1000.0, lat=-1.29, lng=36.82, created_at=NOW - 30 * DAY)

    t1, t4 = NOW - 3 * DAY, NOW - 4 * DAY
    async with AsyncSessionLocal() as db:
        db.add_all(users)
        await db.flush()
        db.add_all([listing("lst-a", "u-s1", "Electronics", "Nairobi"),
                    listing("lst-b", "u-s1", "Electronics", "Nairobi"),
                    listing("lst-c", "u-s2", "Fashion", "Mombasa")])
        await db.flush()
        db.add_all([
            *[ListingView(listing_id="lst-a", viewer_id=u, created_at=NOW - 3 * DAY)
              for u in ("u-b1", "u-b2", "u-b3", "u-admin", "u-s1")],
            ListingView(listing_id="lst-a", viewer_id="u-b4", created_at=NOW - 20 * DAY),
            Wishlist(user_id="u-b2", listing_id="lst-a", created_at=NOW - 2 * DAY),
            *[_msg("lst-a", "u-b1", "u-b1", "buyer", t1 + timedelta(minutes=m)) for m in (0, 10, 20)],
            _msg("lst-a", "u-b1", "u-s1", "seller", t1 + timedelta(hours=2)),
            _msg("lst-a", "u-b4", "u-b4", "buyer", t4),
            _msg("lst-a", "u-b4", "u-s1", "seller", t4 + timedelta(minutes=60)),
            Interest(listing_id="lst-a", buyer_id="u-b2", offer_price=900.0, created_at=NOW - 2 * DAY),
            Deal(listing_id="lst-a", seller_id="u-s1", buyer_id="u-b1", agreed_price=1000.0, commission=0.0,
                 status=DealStatus.paid, created_at=NOW - DAY),
            Deal(listing_id="lst-a", seller_id="u-s1", buyer_id="u-b3", agreed_price=1000.0, commission=0.0,
                 status=DealStatus.released, created_at=NOW - 10 * DAY, released_at=NOW - 2 * DAY),
            *[ListingView(listing_id="lst-b", viewer_id=f"u-b{i}", created_at=NOW - 10 * DAY) for i in range(1, 5)],
            *[Interest(listing_id="lst-b", buyer_id=f"u-b{i}", created_at=NOW - 9 * DAY) for i in range(1, 6)],
            ListingView(listing_id="lst-b", viewer_id="u-b5", created_at=NOW - 2 * DAY),
            _msg("lst-b", "u-b6", "u-b6", "buyer", NOW - timedelta(hours=30)),
            *[ListingView(listing_id="lst-c", viewer_id=f"u-b{i}", created_at=NOW - 10 * DAY) for i in range(1, 6)],
            ListingView(listing_id="lst-c", viewer_id="u-b6", created_at=NOW - DAY),
            *[Interest(listing_id="lst-c", buyer_id=f"u-b{i}", created_at=NOW - DAY) for i in range(1, 4)],
        ])
        await db.commit()
    return NOW


def _ids(rows: list[dict]) -> list[str]:
    return [r["listing_id"] for r in rows]


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["listing_id"]: r for r in rows}


async def _rankings(**kwargs) -> dict:
    async with AsyncSessionLocal() as db:
        return await market_intel.rankings(db, now=NOW, **kwargs)


# ── the stages a buyer reaches ───────────────────────────────────────────────

@pytest.mark.asyncio
class TestStages:
    async def test_each_stage_counts_distinct_buyers_and_leaves_out_staff_and_the_seller(self, market):
        body = await _rankings(window_days=7)
        a = _by_id(body["rankings"]["viewed"])["lst-a"]
        assert (a["viewers"], a["savers"], a["likers"], a["contacters"]) == (3, 1, 0, 3)
        assert (a["negotiators"], a["deal_buyers"], a["completed_buyers"]) == (3, 1, 1)
        # Three messages from b1 are one contact; the admin's and the seller's own views count for nothing.
        assert a["contact_to_deal_pct"] == round(1 / 3 * 100, 1)

    async def test_a_negotiation_is_a_two_way_conversation_or_an_offer_never_a_sale(self, market):
        body = await _rankings(window_days=7)
        assert _ids(body["rankings"]["negotiated"]) == ["lst-a"]
        # lst-c has three interested buyers, none negotiated, and no deal.
        c = _by_id(body["rankings"]["contacted"])["lst-c"]
        assert (c["contacters"], c["negotiators"], c["deal_buyers"]) == (3, 0, 0)

    async def test_conversion_counts_deals_and_completions_apart_from_engagement(self, market):
        body = await _rankings(window_days=7)
        assert _ids(body["rankings"]["converted"]) == ["lst-a"]
        stalled = body["conversion"]["engaged_not_converting"]
        assert _ids(stalled) == ["lst-c"]
        assert "none agreed a deal" in stalled[0]["note"]

    async def test_an_event_outside_the_window_is_not_counted(self, market):
        body = await _rankings(window_days=7)
        assert _by_id(body["rankings"]["viewed"])["lst-a"]["viewers"] == 3, "b4's view 20 days ago is outside"


# ── attention over time ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAttention:
    async def test_scores_follow_the_published_formula(self, market):
        body = await _rankings(window_days=7)
        rows = _by_id(body["rankings"]["trending"])
        assert rows["lst-a"]["score"] == 45 and rows["lst-a"]["previous_score"] == 10
        assert rows["lst-a"]["breakdown"] == {"views": 3, "saves": 2, "likes": 0, "contacts": 12,
                                              "negotiations": 18, "deals": 10}
        assert rows["lst-c"]["score"] == 13 and rows["lst-c"]["previous_score"] == 5
        assert body["weights"] == market_intel.DEFAULT_WEIGHTS
        assert body["formula"].startswith("Score = 1 x viewers + 2 x savers")

    async def test_trending_growing_and_losing_attention(self, market):
        body = await _rankings(window_days=7)
        assert _ids(body["rankings"]["trending"]) == ["lst-a", "lst-c"]
        growing = body["rankings"]["growing"]
        assert _ids(growing) == ["lst-a", "lst-c"]
        assert (growing[0]["growth_pct"], growing[1]["growth_pct"]) == (350.0, 160.0)
        losing = body["attention"]["losing_attention"]
        assert _ids(losing) == ["lst-b"]
        assert (losing[0]["score"], losing[0]["previous_score"]) == (5, 24)

    async def test_a_buyer_left_waiting_over_a_day_needs_the_seller(self, market):
        body = await _rankings(window_days=7)
        waiting = body["attention"]["needs_seller"]
        assert _ids(waiting) == ["lst-b"]
        assert (waiting[0]["waiting_buyers"], waiting[0]["oldest_wait_hours"]) == (1, 30.0)

    async def test_ties_are_broken_by_listing_id_so_rankings_repeat(self, market):
        first, second = await _rankings(window_days=7), await _rankings(window_days=7)
        assert first["rankings"] == second["rankings"]
        assert _ids(first["rankings"]["viewed"]) == ["lst-a", "lst-b", "lst-c"]
        assert _ids(first["rankings"]["contacted"]) == ["lst-a", "lst-c", "lst-b"]


# ── filters and windows ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestFilters:
    async def test_county_and_category_filters_ignore_case(self, market):
        by_county = await _rankings(window_days=7, county="mombasa")
        assert _ids(by_county["rankings"]["viewed"]) == ["lst-c"]
        assert by_county["attention"]["needs_seller"] == []
        by_category = await _rankings(window_days=7, category="ELECTRONICS")
        assert _ids(by_category["rankings"]["viewed"]) == ["lst-a", "lst-b"]

    async def test_a_custom_range_counts_only_its_own_events(self, market):
        body = await _rankings(start=NOW - 14 * DAY, end=NOW - 7 * DAY)
        b = _by_id(body["rankings"]["viewed"])["lst-b"]
        assert (b["viewers"], b["contacters"]) == (4, 5)
        assert body["window"]["from"] == "2025-06-04T12:00:00Z"
        assert body["window"]["previous_from"] == "2025-05-28T12:00:00Z"


# ── one listing, sellers, buyers, demand ─────────────────────────────────────

@pytest.mark.asyncio
class TestListingSellersBuyersDemand:
    async def test_listing_detail_reports_every_stage_and_its_seller(self, market):
        async with AsyncSessionLocal() as db:
            body = await market_intel.listing_detail(db, "lst-a", window_days=7, now=NOW)
        assert body["funnel"] == {"viewers": 3, "savers_or_likers": 1, "contacters": 3, "negotiators": 3,
                                  "deals_agreed": 1, "deals_completed": 1}
        assert body["deals_by_status"] == {"paid": 1}
        assert body["listing"]["seller_name"] == "U-S1"
        assert body["listing"]["county"] == "Nairobi" and "lat" not in body["listing"]
        assert body["snapshots"] == [], "no snapshot was recorded, and none is invented"

    async def test_seller_performance(self, market):
        async with AsyncSessionLocal() as db:
            body = await market_intel.performance(db, start=NOW - 7 * DAY, end=NOW, now=NOW)
        s1, s2 = body["sellers"]["top"]
        assert s1["seller_id"] == "u-s1" and s2["seller_id"] == "u-s2"
        assert (s1["viewers"], s1["contacters"], s1["negotiators"]) == (4, 4, 3)
        assert (s1["deals_agreed"], s1["deals_completed"], s1["live_listings"]) == (1, 1, 2)
        # First replies: 120 and 60 minutes, and b6 unanswered for 30 hours (1800).
        assert s1["avg_first_reply_minutes"] == 660.0
        assert (s1["contacters_previous"], s1["contacters_change_pct"]) == (5, -20.0)
        assert s2["avg_first_reply_minutes"] is None, "no conversations: unmeasured, not zero"
        assert s2["contacters_change_pct"] is None, "from zero there is no percentage"

    async def test_buyer_funnel_and_buyers(self, market):
        async with AsyncSessionLocal() as db:
            body = await market_intel.performance(db, start=NOW - 7 * DAY, end=NOW, now=NOW)
        funnel = {s["key"]: (s["buyers"], s["pct_of_viewed"]) for s in body["buyer_funnel"]}
        assert funnel == {"viewed": (5, 100.0), "saved_or_liked": (1, 20.0), "contacted": (5, 100.0),
                          "negotiated": (3, 60.0), "deal_agreed": (1, 20.0), "deal_completed": (1, 20.0)}
        assert body["buyers"]["total"] == 6 and body["buyers"]["browsers_without_contact"] == 1
        b1 = body["buyers"]["top"][0]
        assert b1["buyer_id"] == "u-b1"
        assert (b1["listings_contacted"], b1["listings_negotiated"], b1["deals_agreed"]) == (2, 1, 1)
        assert {n["metric"] for n in body["not_instrumented"]} == {"searches"}

    async def test_demand_against_the_window_before(self, market):
        async with AsyncSessionLocal() as db:
            body = await market_intel.demand(db, window_days=7, now=NOW)
        cats = {c["key"]: c for c in body["categories"]}
        e = cats["electronics"]
        assert (e["demand"], e["demand_previous"], e["demand_change_pct"]) == (4, 5, -20.0)
        assert (cats["fashion"]["demand"], cats["fashion"]["demand_change_pct"]) == (3, None)
        assert body["growth"] == {"rising": [], "falling": ["Electronics"]}
        counties = {c["key"]: c for c in body["counties"]}
        assert (counties["nairobi"]["buyer_threads"], counties["nairobi"]["buyer_threads_previous"]) == (3, 0)
        assert counties["mombasa"]["supply_live"] == 1


# ── rules without a database ─────────────────────────────────────────────────

class TestRules:
    def test_weights_take_overrides_and_ignore_what_they_cannot_use(self):
        assert market_intel.trending_weights("views=3, deals=0")["views"] == 3.0
        assert market_intel.trending_weights("views=3, deals=0")["deals"] == 0.0
        assert market_intel.trending_weights("bogus=5,views=-1,likes=abc,saves") == market_intel.DEFAULT_WEIGHTS
        assert market_intel.trending_weights("") == market_intel.DEFAULT_WEIGHTS

    def test_unusual_interest_is_relative_to_the_median(self):
        def row(lid: str, contacters: int) -> dict:
            return {"listing_id": lid, "viewers": 0, "contacters": contacters, "negotiators": 0,
                    "deal_buyers": 1, "view_to_contact_pct": None}
        rows = [row("l1", 1), row("l2", 1), row("l3", 1), row("l4", 9)]
        unusual = market_intel.conversion_insights(rows, 10)["unusual_interest"]
        assert _ids(unusual) == ["l4"] and "9.0 times the median of 1" in unusual[0]["note"]
        # Fewer than four contacted listings: no median worth comparing with.
        assert market_intel.conversion_insights(rows[:3], 10)["unusual_interest"] == []

    def test_growth_needs_a_real_base(self):
        weights = market_intel.DEFAULT_WEIGHTS
        meta = {lid: {"name": lid, "category": None, "county": None, "seller_id": "s"} for lid in ("new", "big")}
        current = {"new": {"views": {"a", "b", "c", "d", "e", "f"}}, "big": {"views": {"a"}}}
        previous = {"new": {"views": {"x"}}, "big": {"views": set("abcdefghijkl")}}
        changes = market_intel.attention_changes(current, previous, meta, weights, 10)
        assert _ids(changes["trending"]) == ["new"], "rose from 1 to 6"
        assert changes["growing"] == [], "a rise from a base of 1 is not growth"
        assert _ids(changes["losing_attention"]) == ["big"]


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRoutes:
    async def test_a_custom_range_and_a_county_over_http(self, client, market):
        r = await client.get("/admin/intelligence/market/rankings", headers=_h("u-admin"), params={
            "from": "2025-06-11T12:00:00Z", "to": "2025-06-18T12:00:00Z", "county": "Mombasa"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert _ids(body["rankings"]["viewed"]) == ["lst-c"]
        assert body["county"] == "Mombasa" and body["window"]["from"] == "2025-06-11T12:00:00Z"

    async def test_a_custom_range_is_validated(self, client, market):
        for params in ({"from": "2025-06-11T12:00:00Z"},
                       {"from": "2025-06-18T12:00:00Z", "to": "2025-06-11T12:00:00Z"},
                       {"from": "2025-01-01T00:00:00Z", "to": "2025-06-01T00:00:00Z"},
                       {"from": "2999-01-01T00:00:00Z", "to": "2999-01-02T00:00:00Z"},
                       {"from": "yesterday", "to": "today"}):
            r = await client.get("/admin/intelligence/market/rankings", headers=_h("u-admin"), params=params)
            assert r.status_code == 422, params

    async def test_demand_is_for_administrators(self, client, market):
        ok = await client.get("/admin/intelligence/market/demand", headers=_h("u-admin"))
        assert ok.status_code == 200, ok.text
        assert {"categories", "counties", "growth", "window"} <= set(ok.json())
        assert (await client.get("/admin/intelligence/market/demand", headers=_h("u-b1"))).status_code == 403
