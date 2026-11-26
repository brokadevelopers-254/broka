"""The report builder: specs, figures that match their sources, unavailable
figures, stored runs and regeneration, PDFs, saved reports and schedules.

Seeded for the week [2025-03-01, 2025-03-08) against [2025-02-22, 2025-03-01):

  users     3 buyers sign up in the week (2 on 2 March, 1 on 5 March), 1 the
            week before; an administrator signs up in the week (never counted).
  listings  2 posted in the week, 1 the week before (all Electronics, Nairobi).
  deals     one agreed in the week (paid, KES 1,000); one released in the week
            (agreed on 20 February).
  payments  M-Pesa: 2 succeeded and 1 failed in the week.
  accounts  one suspension and one reinstatement in the week.
  activity  none recorded, and no operations samples: those figures are
            unavailable, never zero.
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from api.core import report_builder as rb
from api.core.email import AdminEmailResult
from api.database import (
    AccountType, AsyncSessionLocal, AuditLog, Deal, DealStatus, Listing, MpesaStatus, MpesaTransaction, User,
    init_db, reset_engine,
)
from api.models.account_moderation import AccountModerationEvent
from api.models.admin_alert import AdminAlert
from api.models.report_builder import ReportDefinition, ReportRun
from api.security import create_access_token
from main import app

START = datetime(2025, 3, 1)
END = datetime(2025, 3, 8)
LATER = datetime(2025, 3, 20, 9, 0)
DAY = timedelta(days=1)

WEEK_SPEC = {
    "title": "Weekly business review", "categories": ["growth", "engagement", "marketplace", "revenue", "trust",
                                                      "system"],
    "date_from": "2025-03-01", "date_to": "2025-03-07", "comparison": "previous_period",
}


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_report_builder.db"
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


def _user(uid: str, at: datetime, *, admin: bool = False, email=None, seller: bool = False) -> User:
    return User(id=uid, name=uid.upper(), phone=f"+2547{random.randint(10_000_000, 99_999_999)}", password_hash="x",
                is_admin=admin, email=email, created_at=at,
                account_type=AccountType.buyer_seller if seller else AccountType.buyer)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def seeded(setup_db):
    async with AsyncSessionLocal() as db:
        db.add_all([
            _user("rb-admin", START + DAY, admin=True, email="rb.admin@broka.co.ke"),
            _user("rb-admin2", START - 90 * DAY, admin=True, email="second@broka.co.ke"),
            _user("rb-admin3", START - 90 * DAY, admin=True),
            _user("rb-seller", START - 60 * DAY, seller=True),
            _user("rb-b1", START + DAY), _user("rb-b2", START + DAY + timedelta(hours=5)),
            _user("rb-b3", START + 4 * DAY), _user("rb-b0", START - 4 * DAY),
        ])
        await db.flush()

        def listing(lid: str, at: datetime) -> Listing:
            return Listing(id=lid, seller_id="rb-seller", name=lid.upper(), category="Electronics",
                           location_county="Nairobi", price=1000.0, lat=-1.29, lng=36.82, created_at=at)
        db.add_all([listing("rb-l1", START + 2 * DAY), listing("rb-l2", START + 2 * DAY),
                    listing("rb-l0", START - 3 * DAY)])
        await db.flush()
        db.add_all([
            Deal(id="rb-d1", listing_id="rb-l1", seller_id="rb-seller", buyer_id="rb-b1", agreed_price=1000.0,
                 commission=0.0, status=DealStatus.paid, created_at=START + 3 * DAY),
            Deal(id="rb-d2", listing_id="rb-l0", seller_id="rb-seller", buyer_id="rb-b0", agreed_price=500.0,
                 commission=0.0, status=DealStatus.released, created_at=START - 9 * DAY,
                 released_at=START + 5 * DAY),
        ])
        await db.flush()
        db.add_all([
            *[MpesaTransaction(deal_id="rb-d1", buyer_id="rb-b1", phone="254711000111", amount=1000.0,
                               status=MpesaStatus.success, created_at=START + 3 * DAY) for _ in range(2)],
            MpesaTransaction(deal_id="rb-d1", buyer_id="rb-b1", phone="254711000111", amount=1000.0,
                             status=MpesaStatus.failed, created_at=START + 3 * DAY),
            AccountModerationEvent(user_id="rb-b3", action="suspended", from_status="active", to_status="suspended",
                                   reason="test", actor_id="rb-admin", created_at=START + 2 * DAY),
            AccountModerationEvent(user_id="rb-b3", action="reinstated", from_status="suspended", to_status="active",
                                   reason="test", actor_id="rb-admin", created_at=START + 5 * DAY),
        ])
        await db.commit()


def _kpi(payload: dict, key: str) -> dict:
    return next(k for k in payload["kpis"] if k["key"] == key)


async def _build(spec: dict, now: datetime = LATER) -> dict:
    normal = rb.normalize_spec(spec)
    start, end = rb.spec_range(normal, now=now)
    async with AsyncSessionLocal() as db:
        return await rb.build(db, normal, start=start, end=end, now=now)


# ── Specs, periods and schedules ────────────────────────────────────────────

class TestSpecs:
    def test_categories_expand_to_their_metrics_once(self):
        spec = rb.normalize_spec({**WEEK_SPEC, "categories": ["growth", "business"], "metrics": ["new_users"]})
        assert spec["metrics"][:3] == ["new_users", "new_sellers", "new_listings"]
        assert spec["metrics"].count("new_users") == 1
        assert "income_minor" in spec["metrics"]

    @pytest.mark.parametrize("change,message", [
        ({"metrics": ["searches"]}, "unknown metric"),
        ({"categories": ["weather"]}, "unknown report category"),
        ({"categories": [], "metrics": []}, "choose at least one"),
        ({"date_to": "2025-02-01"}, "date_to is before date_from"),
        ({"date_from": "2024-01-01", "date_to": "2025-03-07"}, "at most 366 days"),
        ({"date_to": None}, "both date_from and date_to"),
        ({"last_days": 7}, "not both"),
        ({"date_from": "2025-02-30"}, "not a real date"),
        ({"comparison": "last_decade"}, "comparison must be"),
        ({"title": "  "}, "title"),
    ])
    def test_a_bad_spec_says_what_to_change(self, change, message):
        with pytest.raises(rb.SpecError, match=message):
            rb.normalize_spec({**WEEK_SPEC, **change})

    def test_ranges_are_whole_utc_days_and_end_at_now(self):
        spec = rb.normalize_spec(WEEK_SPEC)
        assert rb.spec_range(spec, now=LATER) == (START, END)
        assert rb.spec_range(spec, now=START + 3 * DAY + timedelta(hours=6)) == (START, START + 3 * DAY + timedelta(hours=6))
        rolling = rb.normalize_spec({**WEEK_SPEC, "date_from": None, "date_to": None, "last_days": 7})
        assert rb.spec_range(rolling, now=LATER) == (datetime(2025, 3, 13), datetime(2025, 3, 20))

    def test_comparisons(self):
        assert rb.comparison_range("previous_period", START, END) == (START - 7 * DAY, START)
        assert rb.comparison_range("previous_year", datetime(2024, 2, 29), datetime(2024, 3, 1)) == (
            datetime(2023, 2, 28), datetime(2023, 3, 1))
        assert rb.comparison_range("none", START, END) is None

    def test_period_labels(self):
        assert rb.period_label(START, END) == "1–7 Mar 2025"
        assert rb.period_label(START - 7 * DAY, START) == "22–28 Feb 2025"
        assert rb.period_label(datetime(2024, 12, 30), datetime(2025, 1, 2)) == "30 Dec 2024 – 1 Jan 2025"
        assert rb.period_label(START, START + DAY) == "1 Mar 2025"

    def test_time_buckets(self):
        weeks = rb.time_buckets(START, START + 14 * DAY, "week")
        assert weeks == [(START, START + 7 * DAY), (START + 7 * DAY, START + 14 * DAY)]
        months = rb.time_buckets(datetime(2025, 1, 15), datetime(2025, 3, 10), "month")
        assert months == [(datetime(2025, 1, 15), datetime(2025, 2, 1)), (datetime(2025, 2, 1), datetime(2025, 3, 1)),
                          (datetime(2025, 3, 1), datetime(2025, 3, 10))]


class _Schedule:
    def __init__(self, schedule, *, hour=4, weekday=None, day=None, every=None, last=None, enabled=True):
        self.schedule, self.schedule_hour_utc, self.schedule_weekday = schedule, hour, weekday
        self.schedule_day, self.schedule_every_days, self.last_run_at, self.enabled = day, every, last, enabled


class TestSchedules:
    AFTER = datetime(2025, 3, 12, 5, 0)   # a Wednesday

    def test_next_runs(self):
        assert rb.next_run_after(_Schedule("daily"), self.AFTER) == datetime(2025, 3, 13, 4)
        assert rb.next_run_after(_Schedule("daily", hour=6), self.AFTER) == datetime(2025, 3, 12, 6)
        assert rb.next_run_after(_Schedule("weekly", weekday=0), self.AFTER) == datetime(2025, 3, 17, 4)
        assert rb.next_run_after(_Schedule("monthly", day=1), self.AFTER) == datetime(2025, 4, 1, 4)
        assert rb.next_run_after(_Schedule("monthly", day=1), datetime(2025, 12, 5)) == datetime(2026, 1, 1, 4)
        assert rb.next_run_after(_Schedule("custom", every=3, last=datetime(2025, 3, 10, 4)), self.AFTER) == \
            datetime(2025, 3, 13, 4)
        assert rb.next_run_after(_Schedule("none"), self.AFTER) is None
        assert rb.next_run_after(_Schedule("daily", enabled=False), self.AFTER) is None

    def test_scheduled_periods_are_the_complete_days_before(self):
        run = datetime(2025, 3, 11, 4)
        assert rb.scheduled_period(_Schedule("daily"), run) == (datetime(2025, 3, 10), datetime(2025, 3, 11))
        assert rb.scheduled_period(_Schedule("weekly", weekday=1), run) == (datetime(2025, 3, 4), datetime(2025, 3, 11))
        assert rb.scheduled_period(_Schedule("monthly", day=1), datetime(2025, 4, 1, 4)) == (
            datetime(2025, 3, 1), datetime(2025, 4, 1))
        assert rb.scheduled_period(_Schedule("monthly", day=1), datetime(2025, 1, 1, 4)) == (
            datetime(2024, 12, 1), datetime(2025, 1, 1))
        assert rb.scheduled_period(_Schedule("custom", every=3), run) == (datetime(2025, 3, 8), datetime(2025, 3, 11))

    @pytest.mark.parametrize("kwargs,message", [
        ({"schedule": "weekly", "weekday": None}, "weekday"),
        ({"schedule": "monthly", "day": 31}, "day of the month"),
        ({"schedule": "custom", "every_days": 1}, "every 2 to 90"),
        ({"schedule": "hourly"}, "schedule must be"),
    ])
    def test_bad_schedules(self, kwargs, message):
        fields = {"every_days": None, "hour": 4, "weekday": None, "day": None, **kwargs}
        with pytest.raises(rb.SpecError, match=message):
            rb.validate_schedule(fields.pop("schedule"), **fields)


class TestChanges:
    def _k(self, key, value, previous, unit="count", good="up"):
        m = rb.METRIC_BY_KEY[key]
        return rb._metric_out(rb.Metric(key, m.label, m.category, m.source, unit=unit, good=good), value, previous,
                              available=True, reason=None, filtered=False)

    def test_only_real_moves_are_called_out(self):
        kpis = [
            self._k("new_users", 30, 50),                       # -40% on a real base: worse
            self._k("new_sellers", 3, 1),                       # +200% on a base of 3: too small
            self._k("liquidity_pct", 52.0, 45.0, unit="pct"),   # +7 points: better
            self._k("complaints_filed", 40, 20, good="down"),   # +100%: worse
        ]
        changes = {c["key"]: c for c in rb._important_changes(kpis)}
        assert set(changes) == {"new_users", "liquidity_pct", "complaints_filed"}
        assert changes["new_users"]["direction"] == "worse"
        assert changes["liquidity_pct"]["direction"] == "better"
        assert changes["complaints_filed"]["direction"] == "worse"
        observations = rb._observations(list(changes.values()), {"items": []}, "the period before")
        assert {o["evidence"][0]["metric"] for o in observations} == {"new_users", "complaints_filed"}
        assert all(o["action"] for o in observations)


# ── Figures ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestBuild:
    async def test_figures_match_their_sources(self):
        payload = await _build(WEEK_SPEC)
        new_users = _kpi(payload, "new_users")
        assert (new_users["value"], new_users["previous"], new_users["change_pct"]) == (3, 1, 200.0)
        assert (_kpi(payload, "new_listings")["value"], _kpi(payload, "new_listings")["previous"]) == (2, 1)
        assert _kpi(payload, "deals_agreed")["value"] == 1
        assert _kpi(payload, "deals_completed")["value"] == 1
        assert _kpi(payload, "deal_value_minor")["value"] == 100_000
        assert _kpi(payload, "payments_succeeded")["value"] == 2
        assert _kpi(payload, "payments_failed")["value"] == 1
        assert _kpi(payload, "payment_success_pct")["value"] == pytest.approx(66.67, abs=0.01)
        assert _kpi(payload, "accounts_suspended")["value"] == 1
        assert _kpi(payload, "accounts_reinstated")["value"] == 1
        assert _kpi(payload, "accounts_restricted")["value"] == 0
        # Non-admin accounts registered by the end of the week: the seller, rb-b0 and three new buyers.
        assert _kpi(payload, "registered_users")["value"] == 5
        assert _kpi(payload, "registered_users")["previous"] == 2

    async def test_unmeasured_figures_are_unavailable_not_zero(self):
        payload = await _build(WEEK_SPEC)
        active = _kpi(payload, "active_users")
        assert active["available"] is False and active["value"] is None and active["previous"] is None
        assert "Activity tracking" in active["unavailable_reason"]
        api = _kpi(payload, "api_requests")
        assert api["available"] is False and "sampling" in api["unavailable_reason"].lower()
        unavailable = {u["key"] for u in payload["unavailable"]}
        assert {"active_users", "api_requests", "api_p95_ms"} <= unavailable
        assert not unavailable & {c["key"] for c in payload["changes"]}
        assert any("could not be measured" in line for line in payload["executive_summary"])

    async def test_point_in_time_figures_are_never_compared(self):
        payload = await _build(WEEK_SPEC)
        live = _kpi(payload, "live_listings")
        assert live["kind"] == "point" and live["previous"] is None and live["change_pct"] is None

    async def test_definitions_and_sources_for_every_figure(self):
        payload = await _build(WEEK_SPEC)
        for k in payload["kpis"]:
            assert payload["definitions"][k["key"]] and payload["sources"][k["key"]]
        assert payload["period"] == {"from": "2025-03-01T00:00:00Z", "to": "2025-03-08T00:00:00Z",
                                     "label": "1–7 Mar 2025", "days": 7.0, "timezone": "UTC", "in_progress": False}
        assert payload["comparison"]["from"] == "2025-02-22T00:00:00Z"
        assert {s["key"] for s in payload["sections"]} >= {"growth", "marketplace", "revenue", "trust"}

    async def test_daily_series_match_the_figure(self):
        payload = await _build({**WEEK_SPEC, "categories": ["growth"], "group_by": "day", "visualization": "bar"})
        series = next(s for s in payload["series"] if s["key"] == "new_users")
        assert [p["v"] for p in series["points"]] == [0, 2, 0, 0, 1, 0, 0]
        assert sum(p["v"] for p in series["points"]) == _kpi(payload, "new_users")["value"]
        assert series["visualization"] == "bar"
        assert not any(s["key"] == "live_listings" for s in payload["series"])

    async def test_weekly_series_add_the_days(self):
        payload = await _build({**WEEK_SPEC, "categories": ["growth"], "group_by": "week",
                                "date_from": "2025-02-22", "date_to": "2025-03-07"})
        series = next(s for s in payload["series"] if s["key"] == "new_users")
        assert [p["v"] for p in series["points"]] == [1, 3]

    async def test_category_breakdown_and_filters(self):
        payload = await _build({**WEEK_SPEC, "categories": ["listings"], "group_by": "category",
                                "filters": {"category": "Electronics"}})
        assert {t["key"] for t in payload["tables"]} >= {"stages_by_category", "demand_by_category", "top_listings"}
        assert _kpi(payload, "listing_viewers")["filtered"] is True
        assert "listing_viewers" in payload["filters"]["applies_to"]
        assert payload["filters"]["note"]

    async def test_no_comparison_means_no_changes(self):
        payload = await _build({**WEEK_SPEC, "comparison": "none"})
        assert payload["comparison"] is None and payload["changes"] == []
        assert _kpi(payload, "new_users")["previous"] is None


# ── Routes ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRoutes:
    async def test_catalog_lists_metrics_and_admin_recipients_only(self, client):
        r = await client.get("/admin/report-builder/catalog", headers=_h("rb-admin"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert [c["key"] for c in body["categories"]] == list(rb.CATEGORY_KEYS)
        assert len(body["metrics"]) == len(rb.METRICS)
        ids = {u["id"] for u in body["email"]["recipients"]}
        assert {"rb-admin", "rb-admin2"} <= ids and "rb-admin3" not in ids and "rb-b1" not in ids
        assert "rb.admin@broka.co.ke" not in r.text
        assert "Electronics" in body["filter_options"]["category"]

    async def test_buyers_cannot_build_reports(self, client):
        assert (await client.get("/admin/report-builder/catalog", headers=_h("rb-b1"))).status_code == 403
        assert (await client.post("/admin/report-builder/preview", json=WEEK_SPEC,
                                  headers=_h("rb-b1"))).status_code == 403

    async def test_preview_is_not_stored(self, client):
        r = await client.post("/admin/report-builder/preview", json=WEEK_SPEC, headers=_h("rb-admin"))
        assert r.status_code == 200, r.text
        assert r.json()["stored"] is False and r.json()["id"] is None
        async with AsyncSessionLocal() as db:
            assert (await db.execute(select(func.count(ReportRun.id)))).scalar() == 0

    async def test_preview_refuses_a_bad_spec(self, client):
        for body in ({**WEEK_SPEC, "metrics": ["searches"]}, {**WEEK_SPEC, "date_to": "2025-02-01"},
                     {**WEEK_SPEC, "extra": 1}, {**WEEK_SPEC, "date_from": "2099-01-01", "date_to": "2099-01-02"}):
            r = await client.post("/admin/report-builder/preview", json=body, headers=_h("rb-admin"))
            assert r.status_code == 422, body

    async def test_runs_are_stored_regenerated_alongside_and_audited(self, client):
        r = await client.post("/admin/report-builder/runs", json={"spec": WEEK_SPEC}, headers=_h("rb-admin"))
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["trigger"] == "manual" and first["generated_by"] == "rb-admin"
        assert first["period_start"] == "2025-03-01T00:00:00Z" and first["status"] == "ready"
        assert first["measured"] < first["metrics"]
        detail = (await client.get(f"/admin/report-builder/runs/{first['id']}", headers=_h("rb-admin"))).json()
        assert detail["payload"]["kpis"] == first["payload"]["kpis"]
        assert detail["spec"]["title"] == "Weekly business review"

        again = await client.post(f"/admin/report-builder/runs/{first['id']}/regenerate", headers=_h("rb-admin"))
        assert again.status_code == 200, again.text
        second = again.json()
        assert second["id"] != first["id"] and second["regenerated_from"] == first["id"]
        assert second["trigger"] == "regenerate" and second["period_end"] == first["period_end"]
        listing = (await client.get("/admin/report-builder/runs", headers=_h("rb-admin"))).json()
        assert listing["total"] == 2
        async with AsyncSessionLocal() as db:
            actions = {a for (a,) in (await db.execute(
                select(AuditLog.action).where(AuditLog.resource_type == "report_run"))).all()}
        assert {"report_run_generated", "report_run_regenerated"} <= actions
        assert (await client.get("/admin/report-builder/runs/nope", headers=_h("rb-admin"))).status_code == 404

    async def test_a_run_downloads_as_a_pdf(self, client):
        run = (await client.post("/admin/report-builder/runs", json={"spec": WEEK_SPEC}, headers=_h("rb-admin"))).json()
        r = await client.get(f"/admin/report-builder/runs/{run['id']}/pdf", headers=_h("rb-admin"))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF-")
        assert 'filename="broka-weekly-business-review-2025-03-01.pdf"' in r.headers["content-disposition"]

    async def test_saved_reports_with_schedules(self, client):
        base = {"name": "Monday review", "spec": {**WEEK_SPEC, "date_from": None, "date_to": None}}
        missing_day = await client.post("/admin/report-builder/definitions",
                                        json={**base, "schedule": "weekly"}, headers=_h("rb-admin"))
        assert missing_day.status_code == 422
        outsider = await client.post("/admin/report-builder/definitions",
                                     json={**base, "schedule": "weekly", "schedule_weekday": 0, "recipients": ["rb-b1"]},
                                     headers=_h("rb-admin"))
        assert outsider.status_code == 422
        no_email = await client.post("/admin/report-builder/definitions",
                                     json={**base, "schedule": "weekly", "schedule_weekday": 0,
                                           "recipients": ["rb-admin3"]}, headers=_h("rb-admin"))
        assert no_email.status_code == 422
        unscheduled_without_dates = await client.post("/admin/report-builder/definitions", json=base,
                                                      headers=_h("rb-admin"))
        assert unscheduled_without_dates.status_code == 422

        r = await client.post("/admin/report-builder/definitions",
                              json={**base, "schedule": "weekly", "schedule_weekday": 0, "schedule_hour_utc": 5,
                                    "recipients": ["rb-admin", "rb-admin2"]}, headers=_h("rb-admin"))
        assert r.status_code == 200, r.text
        saved = r.json()
        assert saved["version"] == 1 and saved["next_run_at"].endswith("T05:00:00Z")
        assert datetime.fromisoformat(saved["next_run_at"].rstrip("Z")).weekday() == 0
        assert saved["recipients"] == ["rb-admin", "rb-admin2"]

        stale = await client.patch(f"/admin/report-builder/definitions/{saved['id']}",
                                   json={"expected_version": 9, "enabled": False}, headers=_h("rb-admin"))
        assert stale.status_code == 409
        paused = await client.patch(f"/admin/report-builder/definitions/{saved['id']}",
                                    json={"expected_version": 1, "enabled": False}, headers=_h("rb-admin"))
        assert paused.status_code == 200 and paused.json()["version"] == 2 and paused.json()["next_run_at"] is None

        ran = await client.post(f"/admin/report-builder/definitions/{saved['id']}/run", headers=_h("rb-admin"))
        assert ran.status_code == 200, ran.text
        assert ran.json()["definition_id"] == saved["id"] and ran.json()["trigger"] == "manual"
        listing = (await client.get(f"/admin/report-builder/runs?definition_id={saved['id']}",
                                    headers=_h("rb-admin"))).json()
        assert listing["total"] == 1

        gone = await client.post(f"/admin/report-builder/definitions/{saved['id']}/delete", headers=_h("rb-admin"))
        assert gone.status_code == 200
        names = [d["id"] for d in (await client.get("/admin/report-builder/definitions",
                                                     headers=_h("rb-admin"))).json()["items"]]
        assert saved["id"] not in names
        after = await client.patch(f"/admin/report-builder/definitions/{saved['id']}",
                                   json={"expected_version": 3, "enabled": True}, headers=_h("rb-admin"))
        assert after.status_code == 404


# ── The schedule runner ─────────────────────────────────────────────────────

async def _due_definition(name: str, *, recipients: list[str]) -> str:
    async with AsyncSessionLocal() as db:
        d = ReportDefinition(name=name, spec=rb.normalize_spec({**WEEK_SPEC, "title": name, "date_from": None,
                                                                "date_to": None}, require_range=False),
                             schedule="daily", schedule_hour_utc=4, recipients=recipients, enabled=True,
                             next_run_at=datetime(2025, 3, 11, 4), created_by="rb-admin", version=1)
        db.add(d)
        await db.commit()
        return d.id


@pytest.mark.asyncio
class TestScheduleRunner:
    NOW = datetime(2025, 3, 11, 5, 0)

    async def test_a_due_report_runs_once_and_is_emailed_to_its_administrators(self, monkeypatch):
        from api.core import config, email

        monkeypatch.setattr(config, "settings", replace(config.settings, admin_reports_enabled=True))
        sent = []

        async def fake_send(*, to, subject, message, provider_key=None):
            sent.append({"to": to, "subject": subject, "message": message, "key": provider_key})
            return AdminEmailResult(ok=True, message_id="m-1")

        monkeypatch.setattr(email, "send_admin_email", fake_send)
        definition_id = await _due_definition("Daily pulse", recipients=["rb-admin", "rb-admin3"])
        written = await rb.run_due_schedules(now=self.NOW)
        assert len(written) == 1
        async with AsyncSessionLocal() as db:
            run = await db.get(ReportRun, written[0])
            d = await db.get(ReportDefinition, definition_id)
        assert run.trigger == "schedule" and run.generated_by == "system" and run.definition_id == definition_id
        assert (run.period_start, run.period_end) == (datetime(2025, 3, 10), datetime(2025, 3, 11))
        assert d.last_status == "ready" and d.last_run_id == run.id
        assert d.next_run_at == datetime(2025, 3, 12, 4)
        assert [s["to"] for s in sent] == ["rb.admin@broka.co.ke"]
        assert sent[0]["subject"].startswith("BROKA report: Daily pulse") and sent[0]["key"]
        assert "254711000111" not in sent[0]["message"] and "+2547" not in sent[0]["message"]
        assert {r["user_id"]: r["status"] for r in run.delivery} == {"rb-admin": "sent", "rb-admin3": "skipped"}
        # Not due again until tomorrow.
        assert await rb.run_due_schedules(now=self.NOW) == []

    async def test_a_failed_build_is_stored_alerted_and_rescheduled(self, monkeypatch):
        from api.core import config

        monkeypatch.setattr(config, "settings", replace(config.settings, admin_reports_enabled=True))

        async def broken(*args, **kwargs):
            raise RuntimeError("aggregate failed")

        monkeypatch.setattr(rb, "build", broken)
        definition_id = await _due_definition("Broken pulse", recipients=[])
        assert await rb.run_due_schedules(now=self.NOW) == []
        async with AsyncSessionLocal() as db:
            d = await db.get(ReportDefinition, definition_id)
            failed = (await db.execute(select(ReportRun).where(ReportRun.definition_id == definition_id))).scalar_one()
            alerts = (await db.execute(select(AdminAlert).where(AdminAlert.kind == "report_schedule_failed"))).scalars().all()
        assert failed.status == "failed" and failed.error == "RuntimeError" and failed.payload is None
        assert d.last_status == "failed" and d.next_run_at == datetime(2025, 3, 12, 4)
        assert any((a.meta or {}).get("definition_id") == definition_id for a in alerts)

    async def test_switched_off_with_the_founder_reports(self, monkeypatch):
        from api.core import config

        monkeypatch.setattr(config, "settings", replace(config.settings, admin_reports_enabled=False))
        await _due_definition("Paused pulse", recipients=[])
        assert await rb.run_due_schedules(now=self.NOW) == []


def test_email_text_is_aggregates_with_a_link():
    payload = {"period": {"label": "10 Mar 2025"}, "executive_summary": ["Daily pulse covers 10 Mar 2025 (UTC)."],
               "kpis": [{"key": "new_users", "label": "New users", "unit": "count", "available": True, "value": 12,
                         "previous": 10, "delta": 2, "change_pct": 20.0},
                        {"key": "active_users", "label": "Active users", "unit": "count", "available": False,
                         "unavailable_reason": "Activity tracking had not started."}]}
    subject, body = rb.email_text("Daily pulse", payload, "https://admin.broka.co.ke/reports/builder?run=r1")
    assert subject == "BROKA report: Daily pulse (10 Mar 2025 UTC)"
    assert "New users: 12 (+20%)" in body
    assert "Active users: not measured (Activity tracking had not started.)" in body
    assert "https://admin.broka.co.ke/reports/builder?run=r1" in body
    _, without = rb.email_text("Daily pulse", payload, None)
    assert "admin dashboard's report archive" in without
