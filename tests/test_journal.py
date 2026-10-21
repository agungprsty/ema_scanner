import hashlib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.api import security
from src.services import db

pytestmark = pytest.mark.unit

ADMIN_SESSION = "journal_test_session_token"


@pytest.fixture
def journal_store(monkeypatch):
    store = {"saved": [], "deleted": []}

    async def fake_upsert(signal_id, status, entry_fill=None, exit_fill=None, notes=""):
        if signal_id == 404:
            return None
        store["saved"].append((signal_id, status, entry_fill, exit_fill, notes))
        return {"id": signal_id, "journal": {"status": status}}

    async def fake_delete(signal_id):
        store["deleted"].append(signal_id)
        return signal_id != 404

    async def fake_get_journal(status=None, symbol=None, limit=50, offset=0):
        store["query"] = (status, symbol, limit, offset)
        return [{"id": 9, "journal": {"status": "TAKEN"}}], 31

    async def fake_get_journal_summary():
        return db._empty_journal_summary()

    async def fake_get_session_user(token_hash):
        if token_hash == hashlib.sha256(ADMIN_SESSION.encode("utf-8")).hexdigest():
            return {"id": 1, "username": "admin", "role": "administrator", "email": "admin@example.com"}
        return None

    monkeypatch.setattr(main, "upsert_journal_entry", fake_upsert)
    monkeypatch.setattr(main, "delete_journal_entry", fake_delete)
    monkeypatch.setattr(main, "get_journal", fake_get_journal)
    monkeypatch.setattr(main, "get_journal_summary", fake_get_journal_summary)
    monkeypatch.setattr(security, "get_session_user", fake_get_session_user)
    return store


async def _request(method, url, json=None, auth=True):
    cookies = {"st_session": ADMIN_SESSION} if auth else {}
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
        return await client.request(method, url, json=json)


@pytest.mark.asyncio
async def test_save_taken_entry(journal_store):
    resp = await _request(
        "PUT", "/api/signals/7/journal",
        {"status": "TAKEN", "entry_fill": 100.5, "exit_fill": 104, "notes": "  late fill  "},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": 7, "journal": {"status": "TAKEN"}}
    assert journal_store["saved"] == [(7, "TAKEN", 100.5, 104.0, "late fill")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "MAYBE"},
        {"status": "TAKEN", "entry_fill": -1},
        {"status": "TAKEN", "entry_fill": 0},
        {"status": "TAKEN", "exit_fill": 101},
        {"status": "TAKEN", "notes": "n" * 501},
        {},
    ],
)
async def test_save_rejects_invalid_payload(journal_store, payload):
    resp = await _request("PUT", "/api/signals/7/journal", payload)
    assert resp.status_code == 422
    assert resp.json()["message"] == "Validation failed" and resp.json()["errors"]
    assert journal_store["saved"] == []


@pytest.mark.asyncio
async def test_save_requires_api_key(journal_store):
    resp = await _request("PUT", "/api/signals/7/journal", {"status": "SKIPPED"}, auth=False)
    assert resp.status_code == 401
    resp = await _request("DELETE", "/api/signals/7/journal", auth=False)
    assert resp.status_code == 401
    assert journal_store["saved"] == [] and journal_store["deleted"] == []


@pytest.mark.asyncio
async def test_unknown_signal_returns_404(journal_store):
    resp = await _request("PUT", "/api/signals/404/journal", {"status": "SKIPPED"})
    assert resp.status_code == 404 and resp.json()["message"] == "Signal not found"
    resp = await _request("DELETE", "/api/signals/404/journal")
    assert resp.status_code == 404 and resp.json()["message"] == "No journal entry for this signal"


@pytest.mark.asyncio
async def test_delete_entry(journal_store):
    resp = await _request("DELETE", "/api/signals/7/journal")
    assert resp.status_code == 200
    assert resp.json()["data"] is None and journal_store["deleted"] == [7]


@pytest.mark.asyncio
async def test_list_journal_passes_filters(journal_store):
    unauth = await _request("GET", "/api/journal", auth=False)
    assert unauth.status_code == 401

    resp = await _request("GET", "/api/journal?status=TAKEN&symbol=BTCUSDT&page=3&per_page=10", auth=True)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == [{"id": 9, "journal": {"status": "TAKEN"}}]
    assert {k: body["meta"][k] for k in ("total_data", "total_pages", "current_page", "per_page")} == {
        "total_data": 31,
        "total_pages": 4,
        "current_page": 3,
        "per_page": 10,
    }
    assert journal_store["query"] == ("TAKEN", "BTCUSDT", 10, 20)
    assert (await _request("GET", "/api/journal?status=OPEN", auth=True)).status_code == 422
    assert (await _request("GET", "/api/journal?page=0", auth=True)).status_code == 422
    assert (await _request("GET", "/api/journal?per_page=201", auth=True)).status_code == 422


@pytest.mark.asyncio
async def test_journal_summary_endpoint(journal_store):
    unauth = await _request("GET", "/api/journal/summary", auth=False)
    assert unauth.status_code == 401

    resp = await _request("GET", "/api/journal/summary", auth=True)
    assert resp.status_code == 200
    assert resp.json()["data"] == db._empty_journal_summary()


def _sig(side="LONG", entry_price=100.0, stop_loss=99.0, r_multiple=None):
    return SimpleNamespace(side=side, entry_price=entry_price, stop_loss=stop_loss, r_multiple=r_multiple)


def _entry(status="TAKEN", entry_fill=None, exit_fill=None):
    return db.JournalModel(status=status, entry_fill=entry_fill, exit_fill=exit_fill, notes="")


def test_journal_table_is_new_and_tied_to_signals():
    assert db.JournalModel.__tablename__ == "signal_journal"
    assert {fk.target_fullname for fk in db.JournalModel.__table__.c.signal_id.foreign_keys} == {"signals.id"}


def test_journal_view_computes_result_from_fills():
    view = db.journal_view(_sig(), _entry(entry_fill=100.0, exit_fill=102.0))
    assert view["pnl_percent"] == pytest.approx(2.0 - db.ROUND_TRIP_FEE_PERCENT, abs=0.01)
    # R against the planned 1% risk of the signal, not the user's own fill
    assert view["r_multiple"] == pytest.approx(2.0 - db.ROUND_TRIP_FEE_PERCENT, abs=0.01)
    short = db.journal_view(_sig("SHORT", 3000.0, 3030.0), _entry(entry_fill=3010.0, exit_fill=2940.0))
    assert short["r_multiple"] == pytest.approx(70 / 3010 * 100 - db.ROUND_TRIP_FEE_PERCENT, abs=0.01)


def test_journal_view_without_exit_has_no_result():
    view = db.journal_view(_sig(), _entry(entry_fill=100.0))
    assert view["pnl_percent"] is None and view["r_multiple"] is None
    assert db.journal_view(_sig(), None) is None
    assert db.journal_view(_sig(), _entry(status="SKIPPED"))["r_multiple"] is None


def test_journal_summary_compares_with_the_signal_r():
    summary = db.journal_summary(
        [
            (_sig(r_multiple=1.9), _entry(entry_fill=100.0, exit_fill=102.0)),
            (_sig(r_multiple=None), _entry(entry_fill=100.0)),  # still open
            (_sig(r_multiple=-1.1), _entry(status="SKIPPED")),
        ]
    )
    assert summary["taken"] == 2 and summary["skipped"] == 1
    assert summary["closed_taken"] == 1
    assert summary["signal_r_taken"] == 1.9
    assert summary["signal_r_skipped"] == -1.1
    assert summary["your_total_r"] == pytest.approx(2.0 - db.ROUND_TRIP_FEE_PERCENT, abs=0.01)
