"""Public ledger endpoints and the persisted chart window, with the database layer faked."""

import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.services import ledger

pytestmark = pytest.mark.unit

H2 = 2 * 60 * 60 * 1000
CROSS_MS = 1_760_140_800_000


async def _get(path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        return await client.get(path, **kwargs)


@pytest.fixture
def signal(monkeypatch):
    state = {"signal": {"id": 7, "symbol": "BTCUSDT", "timeframe": "2h", "cross_candle_ms": CROSS_MS,
                        "win_status": "SL_HIT", "exit_time": "2025-10-11T10:00:00Z"},
             "window": None, "fetches": []}

    async def fake_signal(signal_id):
        return state["signal"] if signal_id == 7 else None

    async def fake_window(signal_id):
        return state["window"]

    async def fake_cache_get(key):
        return None

    async def fake_cache_set(key, data, ttl=30):
        return None

    def fake_fetch(client, symbol, timeframe, start_ms, end_ms):
        state["fetches"].append((start_ms, end_ms))
        return [{"time": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}]

    monkeypatch.setattr(main, "get_signal_by_id", fake_signal)
    monkeypatch.setattr(main, "get_signal_candle_window", fake_window)
    monkeypatch.setattr(main, "get_cached_json", fake_cache_get)
    monkeypatch.setattr(main, "set_cached_json", fake_cache_set)
    monkeypatch.setattr(main, "fetch_candles_range", fake_fetch)
    monkeypatch.setattr(main, "_get_client", lambda: object())
    monkeypatch.setattr(main, "_klines_budget", {"window": 0, "count": 0})
    monkeypatch.setattr(main, "_klines_inflight", {})
    return state


async def test_closed_signal_is_charted_from_its_persisted_window(signal):
    signal["window"] = {"symbol": "BTCUSDT", "timeframe": "2h", "candles": [{"time": 1}], "persisted": True,
                        "sha256": "a" * 64, "resolved_at_ms": CROSS_MS + 3 * H2}
    resp = await _get("/api/signals/7/candles")
    body = resp.json()
    assert (body["persisted"], body["final"], body["sha256"]) == (True, True, "a" * 64)
    assert signal["fetches"] == []
    assert resp.headers["cache-control"] == f"public, max-age={main.SIGNAL_CANDLES_FINAL_TTL}"


async def test_a_window_starting_at_the_cross_gets_chart_context_apart_from_the_hashed_candles(signal, monkeypatch):
    window = [{"time": (CROSS_MS + i * H2) // 1000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0} for i in range(5)]
    signal["window"] = {"symbol": "BTCUSDT", "timeframe": "2h", "candles": window, "persisted": True,
                        "sha256": "a" * 64, "resolved_at_ms": CROSS_MS + 6 * H2}

    def fake_fetch(client, symbol, timeframe, start_ms, end_ms):
        signal["fetches"].append((start_ms, end_ms))
        return [{"time": (CROSS_MS + i * H2) // 1000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
                for i in range(-30, 1)]

    monkeypatch.setattr(main, "fetch_candles_range", fake_fetch)
    body = (await _get("/api/signals/7/candles")).json()
    assert body["candles"] == window
    assert [c["time"] * 1000 for c in body["context"]] == [CROSS_MS + i * H2 for i in range(-30, 0)]
    assert signal["fetches"] == [(CROSS_MS - 30 * H2, CROSS_MS)]


async def test_closed_signal_without_a_persisted_window_falls_back_to_the_exchange(signal):
    resp = await _get("/api/signals/7/candles")
    assert resp.status_code == 200 and "persisted" not in resp.json()
    assert len(signal["fetches"]) == 1


async def test_open_signal_never_reads_a_persisted_window(signal, monkeypatch):
    signal["signal"] = {**signal["signal"], "win_status": "PENDING", "exit_time": None}
    signal["window"] = {"candles": [{"time": 1}], "persisted": True}
    resp = await _get("/api/signals/7/candles")
    assert resp.json()["final"] is False and len(signal["fetches"]) == 1


async def test_signal_events_are_public_and_404_for_unknown_signals(monkeypatch):
    events = [{"id": 1, "signal_id": 7, "event_type": "detected", "payload": {}, "hash": "h"}]

    async def fake_events(signal_id):
        return events if signal_id == 7 else None

    async def connected():
        return True

    monkeypatch.setattr(main, "get_signal_events", fake_events)
    monkeypatch.setattr(main, "is_db_connected", connected)
    assert (await _get("/api/signals/7/events")).json() == {"signal_id": 7, "events": events}
    assert (await _get("/api/signals/8/events")).status_code == 404


async def test_ledger_endpoints_answer_503_without_a_database(monkeypatch):
    monkeypatch.setattr(ledger.db, "_session_maker", None)

    async def disconnected():
        return False

    monkeypatch.setattr(main, "is_db_connected", disconnected)
    for path in ("/api/ledger/head", "/api/ledger/verify", "/api/ledger.csv", "/api/ledger.jsonl",
                 "/api/signals/7/events"):
        assert (await _get(path)).status_code == 503, path


async def test_ledger_csv_lists_every_column_with_flags(monkeypatch):
    row = {col: None for col in ledger.LEDGER_COLUMNS}
    row.update(signal_id=7, symbol="BTCUSDT", is_eligible=False, exclusion_reason="legacy", counted=False,
               net_r=-1.0734, last_event_hash="h" * 64)

    async def fake_rows():
        return [row]

    monkeypatch.setattr(main, "export_rows", fake_rows)
    resp = await _get("/api/ledger.csv")
    assert resp.headers["content-type"].startswith("text/csv")
    header, line = resp.text.strip().split("\n")
    assert header.split(",") == list(ledger.LEDGER_COLUMNS)
    values = dict(zip(ledger.LEDGER_COLUMNS, line.split(","), strict=True))
    assert (values["is_eligible"], values["exclusion_reason"], values["net_r"]) == ("false", "legacy", "-1.0734")
    assert values["exit_price"] == ""


async def test_manual_trades_land_in_adjustments(monkeypatch):
    seen = {}

    async def fake_adjustments(limit=50, offset=0):
        seen["args"] = (limit, offset)
        return [{"id": 1, "booked_by": "manual", "adjustment": True}], 1

    monkeypatch.setattr(main, "get_profitloss_adjustments", fake_adjustments)
    main.app.dependency_overrides[main.verify_api_key] = lambda: True
    try:
        resp = await _get("/api/profitloss-history/adjustments?limit=10")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.json()["adjustments"][0]["adjustment"] is True and seen["args"] == (10, 0)
