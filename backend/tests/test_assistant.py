"""The Intelligence Assistant: each question goes to one intent, every answer
says what it covers and where to check it, instructions are refused, and only
administrators can ask."""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.core import activity, assistant, market_models, ops_anomalies
from api.database import AccountType, AsyncSessionLocal, User, init_db, reset_engine
from api.security import create_access_token
from main import app

NOW = datetime(2025, 6, 15, 12, 0)


@pytest.fixture(scope="module", autouse=True)
def set_test_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("data") / "test_assistant.db"
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


def _h(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"}


def _user(uid: str, phone: str, *, admin: bool, created: datetime = NOW) -> User:
    return User(id=uid, name=uid, phone=phone, password_hash="x", is_admin=admin, created_at=created,
                account_type=AccountType.buyer)


def test_each_question_goes_to_one_intent():
    cases = {
        "How many users are active right now?": "active_now",
        "Which areas of the platform require attention?": "anomalies",
        "Generate a weekly performance summary.": "summary",
        "How many people signed up last week?": "registrations",
        "Why did payment failures increase this week?": "payments",
        "Which listings are receiving the most buyer interest?": "listings",
        "Which category is growing fastest?": "categories",
        "What will demand look like next week?": "forecast",
        "How has user engagement changed over the last month?": "active_users",
        "Tell me a joke": "help",
        "Suspend user rb-b1 for the weekend": "action",
        "Please refund deal 42": "action",
    }
    for question, intent in cases.items():
        assert assistant.classify(question) == intent, question


@pytest.mark.asyncio
async def test_an_instruction_is_refused_and_names_the_page_without_reading_data():
    answer = await assistant.answer(None, "Suspend user rb-b1 for the weekend", now=NOW)
    assert answer["refused"] is True
    assert answer["intent"] == "action"
    assert answer["links"] == [{"label": "Users", "href": "/users"}]
    assert "confirm" in answer["answer"]
    assert answer["citations"] == []


@pytest.mark.asyncio
async def test_active_now_names_the_window_and_the_definition(monkeypatch):
    async def fake_snapshot(db, *, now=None):
        return {"active_users_now": 7, "active_sessions_now": 9, "presence_window_seconds": 300,
                "generated_at": "2025-06-15T12:00:00Z"}

    monkeypatch.setattr(activity, "live_snapshot", fake_snapshot)
    answer = await assistant.answer(None, "How many users are active right now?", now=NOW)
    assert answer["intent"] == "active_now"
    assert answer["answer"].startswith("7 users were active")
    citation = answer["citations"][0]
    assert "300 seconds" in citation["period"]
    assert citation["definition"] and citation["source"]
    assert answer["generated_at"] == "2025-06-15T12:00:00Z"


@pytest.mark.asyncio
async def test_flagged_figures_are_listed_with_their_day_and_source_and_nothing_is_changed(monkeypatch):
    async def fake_ops(db, *, now=None):
        return {
            "evaluated_day": "2025-06-14",
            "figures": [{"status": "insufficient_history"}, {"status": "normal"}],
            "not_covered": [{"figure": "Active users", "reason": "No anomaly rule judges it yet."}],
            "anomalies": [{"label": "API error rate", "date": "2025-06-14", "observed": 30.0,
                           "baseline_median": 1.0, "severity": "high", "impact": "Requests failed.",
                           "suggested_action": "Check the routes.", "evidence": []}],
        }

    async def fake_market(db, *, weeks=6, now=None):
        return {"evaluated_day": "2025-06-14", "anomalies": []}

    monkeypatch.setattr(ops_anomalies, "detect", fake_ops)
    monkeypatch.setattr(market_models, "detect", fake_market)
    answer = await assistant.answer(None, "Which areas of the platform require attention?", now=NOW)
    assert answer["intent"] == "anomalies"
    assert "1 figure moved outside its usual range on 2025-06-14" in answer["answer"]
    assert "Nothing was changed" in answer["answer"]
    assert answer["citations"][0]["source"] == "Operations (ops_samples)"
    assert answer["citations"][0]["href"] == "/operations"
    assert answer["citations"][0]["period"] == "2025-06-14 (UTC)"
    assert any("too little history" in p for p in answer["points"])
    assert answer["limitations"] == ["Active users: No anomaly rule judges it yet."]


@pytest.mark.asyncio
async def test_no_flag_says_so_for_the_day_it_checked(monkeypatch):
    async def quiet(db, *, now=None):
        return {"evaluated_day": "2025-06-14", "figures": [{"status": "normal"}], "anomalies": [],
                "not_covered": []}

    async def quiet_market(db, *, weeks=6, now=None):
        return {"evaluated_day": "2025-06-14", "anomalies": []}

    monkeypatch.setattr(ops_anomalies, "detect", quiet)
    monkeypatch.setattr(market_models, "detect", quiet_market)
    answer = await assistant.answer(None, "Any unusual changes?", now=NOW)
    assert answer["answer"] == "No figure moved outside its usual range on 2025-06-14 (UTC)."
    assert answer["citations"] == []


@pytest.mark.asyncio
async def test_forecasts_state_their_method_and_why_others_are_withheld(monkeypatch):
    async def fake_forecasts(db, *, now=None):
        return [
            {"metric": "registrations", "label": "New registrations", "validated": True, "withheld_reason": None,
             "backtest": {"days": 7, "actual_total": 70, "forecast_total": 66, "wape": 0.12, "naive_wape": 0.3},
             "forecast": [{"date": "2025-06-16", "expected": 10.0, "low": 8.0, "high": 12.0}] * 7},
            {"metric": "deals", "label": "Deals agreed", "validated": False,
             "withheld_reason": "Only 4 events in the backtest; 14 are needed.", "backtest": {}, "forecast": []},
        ]

    monkeypatch.setattr(market_models, "forecasts", fake_forecasts)
    answer = await assistant.answer(None, "What will demand look like next week?", now=NOW)
    assert answer["intent"] == "forecast"
    assert answer["points"][0].startswith("New registrations: about 70 in the next 7 days, band 56 to 84.")
    assert "Deals agreed: not published. Only 4 events in the backtest; 14 are needed." in answer["points"]
    assert "80%" in answer["citations"][0]["definition"]
    assert answer["citations"][0]["period"] == "7 days after 2025-06-15 (UTC)"
    assert any("7-day backtest" in line for line in answer["limitations"])


@pytest.mark.asyncio
async def test_sign_ups_are_counted_from_the_database_with_their_definition():
    async with AsyncSessionLocal() as db:
        db.add_all([_user(f"ast-new-{n}", f"+25471100041{n}", admin=False, created=datetime(2025, 6, 10, 9))
                    for n in range(3)])
        await db.commit()
    async with AsyncSessionLocal() as db:
        answer = await assistant.answer(db, "How many people signed up last week?", now=NOW)
    assert answer["intent"] == "registrations"
    citation = next(c for c in answer["citations"] if c["label"] == "New users")
    assert citation["value"] == "3"
    assert citation["period"].endswith("(UTC)")
    assert citation["definition"] and citation["source"]
    assert citation["href"] == "/growth"


@pytest.mark.asyncio
async def test_only_administrators_can_ask_and_unknown_fields_are_refused(client):
    async with AsyncSessionLocal() as db:
        db.add_all([_user("ast-admin", "+254711000401", admin=True), _user("ast-buyer", "+254711000402", admin=False)])
        await db.commit()
    ok = await client.post("/admin/assistant/ask", json={"question": "Tell me a joke"}, headers=_h("ast-admin"))
    assert ok.status_code == 200
    assert ok.json()["intent"] == "help"
    refused = await client.post("/admin/assistant/ask", json={"question": "Tell me a joke"}, headers=_h("ast-buyer"))
    assert refused.status_code == 403
    extra = await client.post("/admin/assistant/ask", json={"question": "x", "sql": "drop"}, headers=_h("ast-admin"))
    assert extra.status_code == 422
