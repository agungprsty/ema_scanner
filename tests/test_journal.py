from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.services import db

pytestmark = pytest.mark.unit

API_KEY = "journal_test_key_123"


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
        return [], 0, db._empty_journal_summary()

    monkeypatch.setattr(main, "upsert_journal_entry", fake_upsert)
    monkeypatch.setattr(main, "delete_journal_entry", fake_delete)
    monkeypatch.setattr(main, "get_journal", fake_get_journal)
    monkeypatch.setattr(main, "API_SECRET_KEY", API_KEY)
    return store


async def _request(method, url, json=None, auth=True):
    headers = {"X-API-KEY": API_KEY} if auth else {}
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, json=json, headers=headers)


@pytest.mark.asyncio
async def test_save_taken_entry(journal_store):
    resp = await _request(
        "PUT", "/api/signals/7/journal",
        {"status": "TAKEN", "entry_fill": 100.5, "exit_fill": 104, "notes": "  late fill  "},
    )
    assert resp.status_code == 200
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
    assert (await _request("PUT", "/api/signals/404/journal", {"status": "SKIPPED"})).status_code == 404
    assert (await _request("DELETE", "/api/signals/404/journal")).status_code == 404


@pytest.mark.asyncio
async def test_list_journal_passes_filters(journal_store):
    resp = await _request("GET", "/api/journal?status=TAKEN&symbol=BTCUSDT&limit=10")
    assert resp.status_code == 200
    assert resp.json()["summary"]["taken"] == 0
    assert journal_store["query"] == ("TAKEN", "BTCUSDT", 10, 0)
    assert (await _request("GET", "/api/journal?status=OPEN")).status_code == 422


@pytest.mark.asyncio
async def test_list_journal_is_private(journal_store):
    resp = await _request("GET", "/api/journal?status=TAKEN", auth=False)
    assert resp.status_code == 401
    assert "query" not in journal_store


def _sig(side="LONG", stop_loss=95.0):
    return SimpleNamespace(side=side, stop_loss=stop_loss)


def _entry(status="TAKEN", entry_fill=None, exit_fill=None):
    return db.TradeJournalModel(status=status, entry_fill=entry_fill, exit_fill=exit_fill, notes="")


def test_journal_view_computes_result_from_fills():
    view = db.journal_view(_sig(), _entry(entry_fill=100.0, exit_fill=107.5))
    assert view["pnl_percent"] == pytest.approx(7.5 - db.ROUND_TRIP_FEE_PERCENT, abs=0.01)
    assert view["r_multiple"] == pytest.approx((7.5 - db.ROUND_TRIP_FEE_PERCENT) / 5.0, abs=0.01)


def test_journal_view_without_exit_has_no_result():
    view = db.journal_view(_sig(), _entry(entry_fill=100.0))
    assert view["pnl_percent"] is None and view["r_multiple"] is None
    assert db.journal_view(_sig(), None) is None
    assert db.journal_view(_sig(), _entry(status="SKIPPED"))["r_multiple"] is None
