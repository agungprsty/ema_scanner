"""The signal ledger's hash chain, verifier, CLI and the records the app writes into it (no database)."""

import copy
import json
from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace

import pandas as pd
import pytest

from src.ledger import __main__ as cli
from src.ledger.candles import chart_candles, context_fingerprint, kline_rows
from src.ledger.chain import (
    GENESIS_HASH,
    candles_sha256,
    canonical_json,
    event_core,
    event_hash,
    iso_ms,
    normalize,
    truncate_ms,
    verify_chain,
)
from src.ledger.verify import verify_jsonl
from src.services import db
from src.strategy import registry
from src.strategy.base import SignalIntent
from src.strategy.context import MarketContext
from src.strategy.macd import MacdSignal
from src.strategy.outcome import TradeOutcome

pytestmark = pytest.mark.unit

H2 = 2 * 60 * 60 * 1000
T0 = datetime(2026, 10, 15, 6, 0, 15, 123456, tzinfo=UTC)


def build_chain(drafts: list[tuple[int | None, str, dict]]) -> list[dict]:
    """Events as append_signal_events writes them, ids from 1."""
    events, prev = [], GENESIS_HASH
    for i, (signal_id, event_type, payload) in enumerate(drafts, start=1):
        core = event_core(i, signal_id, event_type, truncate_ms(T0), payload)
        digest = event_hash(prev, core)
        events.append({**core, "prev_hash": prev, "hash": digest})
        prev = digest
    return events


CHAIN = build_chain([
    (1, "detected", {"signal": {"symbol": "BTCUSDT", "entry_price": 83829.2, "win_status": "PENDING"}}),
    (2, "restated", {"genesis": True, "signal": {"symbol": "ETHUSDT", "win_status": "SL_HIT"}}),
    (2, "excluded", {"genesis": True, "reason": "legacy"}),
    (1, "resolved", {"net_r": -1.0734, "gross_r": -0.98, "candles": {"sha256": "ab" * 32}}),
    (1, "alert_sent", {"channel": "telegram", "kind": "outcome"}),
])


# --- canonical JSON -------------------------------------------------------------------------------


def test_canonical_json_is_sorted_compact_and_matches_jsonb_numbers():
    assert canonical_json({"b": 1, "a": [1.0, 0.5, -0.0, None, True]}) == '{"a":[1,0.5,0,null,true],"b":1}'
    # An integral float hashes like the integer JSONB gives back for it, whatever its size
    assert canonical_json(1e20) == canonical_json(100000000000000000000)
    # Small and long floats keep their shortest round-trip form
    assert canonical_json(1e-07) == "1e-07" and json.loads(canonical_json(0.1 + 0.2)) == 0.1 + 0.2
    assert canonical_json({"t": T0}) == '{"t":"2026-10-15T06:00:15.123Z"}'
    with pytest.raises(ValueError):
        canonical_json(float("nan"))
    with pytest.raises(TypeError):
        canonical_json(object())


def test_normalize_survives_a_json_round_trip():
    payload = {"r": -1.0734, "n": 3, "f": 2.0, "nested": [{"x": 1.5e-9}], "when": T0}
    once = normalize(payload)
    assert normalize(json.loads(json.dumps(once))) == once


def test_iso_ms_and_truncation_agree():
    assert iso_ms(T0) == "2026-10-15T06:00:15.123Z"
    assert truncate_ms(T0).microsecond == 123000
    assert iso_ms(datetime(2026, 1, 1)) == "2026-01-01T00:00:00.000Z"


# --- chain verification and tamper detection -----------------------------------------------------


def test_a_well_formed_chain_verifies():
    report = verify_chain(CHAIN)
    assert report.ok and report.first_bad_event is None
    assert (report.events_checked, report.head_event_id, report.head_hash) == (5, 5, CHAIN[-1]["hash"])
    assert CHAIN[0]["prev_hash"] == GENESIS_HASH
    assert all(b["prev_hash"] == a["hash"] for a, b in pairwise(CHAIN))


def test_an_empty_chain_verifies_to_the_genesis_hash():
    report = verify_chain([])
    assert report.ok and report.head_hash == GENESIS_HASH and report.head_event_id is None


@pytest.mark.parametrize(
    ("event_index", "mutate", "reason"),
    [
        (3, lambda e: e["payload"].update(net_r=1.5), "hash does not match"),
        (1, lambda e: e.update(signal_id=99), "hash does not match"),
        (2, lambda e: e.update(event_type="resolved"), "hash does not match"),
        (0, lambda e: e.update(occurred_at="2026-10-15T06:00:15.124Z"), "hash does not match"),
        (4, lambda e: e.update(event_type="deleted"), "unknown event_type"),
    ],
)
def test_tampering_with_an_event_fails_verification_at_that_event(event_index, mutate, reason):
    events = copy.deepcopy(CHAIN)
    mutate(events[event_index])
    report = verify_chain(events)
    assert not report.ok
    assert report.first_bad_event == events[event_index]["id"]
    assert reason in report.reason
    assert report.events_checked == event_index


def test_rehashing_a_tampered_event_breaks_the_next_link():
    events = copy.deepcopy(CHAIN)
    events[1]["payload"]["signal"]["win_status"] = "TP1_HIT"
    core = event_core(2, 2, "restated", events[1]["occurred_at"], events[1]["payload"])
    events[1]["hash"] = event_hash(events[1]["prev_hash"], core)
    report = verify_chain(events)
    assert report.first_bad_event == 3 and "prev_hash" in report.reason


def test_deleted_or_reordered_events_are_caught():
    assert verify_chain(CHAIN[:2] + CHAIN[3:]).first_bad_event == 4
    swapped = [CHAIN[0], CHAIN[2], CHAIN[1], *CHAIN[3:]]
    assert verify_chain(swapped).first_bad_event == 3
    assert verify_chain(CHAIN[1:]).first_bad_event == 2


# --- JSONL export verification and the CLI --------------------------------------------------------


def _export(events=CHAIN, heads=(), signals=()) -> list[str]:
    lines = [json.dumps({"type": "header", "format": "crossline-ledger/1"})]
    lines += [canonical_json({"type": "signal", **s}) for s in signals]
    lines += [canonical_json({"type": "event", **e}) for e in events]
    lines += [json.dumps({"type": "head", **h}) for h in heads]
    return lines


def test_jsonl_export_verifies_with_heads_and_rows():
    heads = [{"date": "2026-10-15", "last_event_id": 3, "head_hash": CHAIN[2]["hash"]}]
    signals = [
        {"signal_id": 1, "symbol": "BTCUSDT", "entry_price": 83829.2, "win_status": "PENDING", "booked_by": None},
        {"signal_id": 2, "symbol": "ETHUSDT", "win_status": "SL_HIT", "booked_by": None},
    ]
    report = verify_jsonl(_export(heads=heads, signals=signals), expected_heads=[CHAIN[1]["hash"]])
    assert report["ok"], report
    assert report["heads"]["checked"] == 1 and report["rows"]["checked"] == 2
    assert report["expected_heads"]["heads"] == [{"hash": CHAIN[1]["hash"], "event_id": 2, "found": True}]


def test_jsonl_verification_flags_a_wrong_head_drifted_rows_and_an_unknown_head():
    heads = [{"date": "2026-10-15", "last_event_id": 3, "head_hash": "f" * 64}]
    signals = [{"signal_id": 2, "symbol": "ETHUSDT", "win_status": "TP1_HIT"}, {"signal_id": 3, "symbol": "X"}]
    report = verify_jsonl(_export(heads=heads, signals=signals), expected_heads=["e" * 64])
    assert report["chain"]["ok"] and not report["ok"]
    assert report["heads"]["mismatched"][0]["reason"] == "head_hash differs from the chain"
    assert report["rows"]["drifted"] == [{"signal_id": 2, "latest_event_id": 2, "fields": ["win_status"]}]
    assert report["rows"]["without_events"] == [3]
    assert report["expected_heads"]["ok"] is False


def test_cli_verifies_a_jsonl_file_and_exits_by_the_result(tmp_path, capsys):
    good = tmp_path / "ledger.jsonl"
    good.write_text("\n".join(_export(signals=ROWS)) + "\n")
    assert cli.main(["verify", "--jsonl", str(good), "--head", CHAIN[-1]["hash"]]) == 0
    assert json.loads(capsys.readouterr().out)["head_event_id"] == 5

    events = copy.deepcopy(CHAIN)
    events[3]["payload"]["net_r"] = 0.5
    bad = tmp_path / "tampered.jsonl"
    bad.write_text("\n".join(_export(events)) + "\n")
    assert cli.main(["verify", "--jsonl", str(bad)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["first_bad_event"]) == (False, 4)


ROWS = [
    {"signal_id": 1, "symbol": "BTCUSDT", "entry_price": 83829.2, "win_status": "PENDING", "booked_by": None},
    {"signal_id": 2, "symbol": "ETHUSDT", "win_status": "SL_HIT", "booked_by": None},
]


def test_a_deleted_signal_row_fails_verification():
    """A signal the events record but whose row was deleted outside the ledger is reported missing."""
    report = verify_jsonl(_export(signals=ROWS[:1]))
    assert report["chain"]["ok"] and not report["ok"]
    assert report["rows"]["missing"] == [{"signal_id": 2, "latest_event_id": 2}]
    assert report["rows"]["missing_count"] == 1


def test_a_deleted_trade_row_fails_verification():
    """The latest snapshot carries a booked trade, but the signal's row no longer has one."""
    trade = {"result": "LOSS", "exit_price": 1.0, "exit_time": "2026-10-15T00:00:00Z", "gross_r": -1.0,
             "cost_r": 0.1, "net_r": -1.1, "cost_model_version": "cm1", "booked_by": "evaluator"}
    events = build_chain([(7, "resolved", {"signal": {"symbol": "SOLUSDT", "win_status": "SL_HIT"}, "trade": trade})])
    with_trade = {"signal_id": 7, "symbol": "SOLUSDT", "win_status": "SL_HIT", **trade}
    assert verify_jsonl(_export(events, signals=[with_trade]))["ok"]
    without = {"signal_id": 7, "symbol": "SOLUSDT", "win_status": "SL_HIT", "booked_by": None}
    report = verify_jsonl(_export(events, signals=[without]))
    assert not report["ok"]
    assert report["rows"]["drifted"] == [{"signal_id": 7, "latest_event_id": 1, "fields": ["trade.missing"]}]


def test_a_broken_chain_skips_the_checks_built_on_it():
    """Past a break, rows and later heads are not compared with unverified snapshots (no false
    without_events or "event missing" lists); the chain section names the break."""
    events = copy.deepcopy(CHAIN)
    events[1]["payload"]["win_status"] = "TP1_HIT"
    heads = [
        {"date": "2026-10-14", "last_event_id": 1, "head_hash": CHAIN[0]["hash"]},
        {"date": "2026-10-15", "last_event_id": 5, "head_hash": CHAIN[4]["hash"]},
    ]
    report = verify_jsonl(_export(events, heads=heads, signals=ROWS))
    assert (report["ok"], report["first_bad_event"]) == (False, 2)
    assert report["rows"] == {"ok": None, "skipped": "chain broken at event 2"}
    assert report["heads"]["checked"] == 1 and report["heads"]["ok"] and report["heads"]["unverifiable"] == 1


def test_cli_without_a_source_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli.main(["verify"]) == 2


# --- candle fingerprints --------------------------------------------------------------------------


def _frame(n: int, start: int = 1_760_140_800_000) -> pd.DataFrame:
    rows = [[start + i * H2, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0] for i in range(n)]
    return pd.DataFrame(rows, columns=["open_time_ms", "open", "high", "low", "close", "volume"])


def test_context_fingerprint_covers_closed_candles_only():
    frame = _frame(40)
    now_ms = int(frame["open_time_ms"].iloc[-1]) + 60_000  # the last candle is forming
    ctx = MarketContext.from_klines("BTCUSDT", now_ms, "2h", {"2h": frame})
    sha, summary = context_fingerprint(ctx)
    assert summary["timeframes"]["2h"]["count"] == 39
    assert summary["timeframes"]["2h"]["last_open_ms"] == int(frame["open_time_ms"].iloc[-2])
    # Changing the forming candle does not change the fingerprint, changing a closed one does
    forming = frame.copy()
    forming.loc[39, "close"] = 1.0
    assert context_fingerprint(MarketContext.from_klines("BTCUSDT", now_ms, "2h", {"2h": forming}))[0] == sha
    closed = frame.copy()
    closed.loc[10, "high"] = 500.0
    assert context_fingerprint(MarketContext.from_klines("BTCUSDT", now_ms, "2h", {"2h": closed}))[0] != sha


def test_persisted_kline_rows_keep_exchange_text_and_chart_marks_partial_candles():
    raw = [[1000 * H2, "1.10", "1.20", "1.00", "1.15", "5", 0, "x"], [1001 * H2, "1.15", "1.3", "1.1", "1.2", "6"]]
    rows = kline_rows(raw)
    assert rows == [[1000 * H2, "1.10", "1.20", "1.00", "1.15", "5"], [1001 * H2, "1.15", "1.3", "1.1", "1.2", "6"]]
    assert candles_sha256(rows) == candles_sha256(json.loads(json.dumps(rows)))
    candles = chart_candles(rows, H2, as_of_ms=1001 * H2 + 5)
    assert candles[0] == {"time": 1000 * H2 // 1000, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 5.0}
    assert candles[1]["partial"] is True and "partial" not in candles[0]


# --- what the app writes into the chain -----------------------------------------------------------


def _signal(**overrides) -> MacdSignal:
    base = dict(
        symbol="BTCUSDT", side="LONG", reason="MACD cross up", cross_candle_ms=1_760_140_800_000,
        cross_time="2025-10-11T00:00:00Z", cross_price=100.0, entry_price=100.5, stop_loss=99.0, tp1=102.75,
        risk_reward_ratio=1.5, macd=1.0, signal=0.5, histogram=0.5, close=100.4, bars_ago=0,
    )
    base.update(overrides)
    return MacdSignal(**base)


def _row(sig: MacdSignal, **overrides):
    rec = dict(
        id=11, symbol=sig.symbol, side=sig.side, timeframe="2h", cross_candle_ms=sig.cross_candle_ms,
        entry_price=sig.entry_price, stop_loss=sig.stop_loss, tp1=sig.tp1, win_status="PENDING",
        strategy_key="macd_cross", strategy_version="2.2.0", source="cron", entry_basis="next_open",
        reason=sig.reason, cross_time=sig.cross_time, cross_price=sig.cross_price, close_price=sig.close,
        risk_reward_ratio=sig.risk_reward_ratio, macd=sig.macd, signal=sig.signal, histogram=sig.histogram,
        bars_ago=0, is_confirmed=True, detected_at=T0,
    )
    rec.update(overrides)
    return SimpleNamespace(**rec)


def test_detected_payload_of_a_registered_strategy_carries_its_frozen_hashes():
    strategy = registry.get("macd_cross@2.2.0")
    intent = SignalIntent.from_signal(_signal(), strategy)
    intent.input_candles_sha256 = "c" * 64
    intent.input_candles = {"decision_ms": 1, "timeframes": {"2h": {"count": 498}}}
    payload = normalize(db.detected_payload(_row(intent), intent))
    assert payload["params_source"] == "frozen"
    assert (payload["params_hash"], payload["code_hash"]) == (strategy.params_hash, strategy.code_hash)
    assert payload["input_candles_sha256"] == "c" * 64
    assert payload["signal"]["entry_price"] == 100.5 and payload["signal"]["win_status"] == "PENDING"
    assert payload["detected_at"] == "2026-10-15T06:00:15.123Z"


def test_detected_payload_of_an_ad_hoc_scan_names_the_settings_it_ran_with():
    payload = db.detected_payload(_row(_signal(), source="manual"), _signal())
    assert payload["params_source"] == "settings" and payload["params"] == db.settings_params()
    # With no override in the environment the settings hash like the frozen version
    assert payload["params_hash"] == registry.get("macd_cross@2.2.0").params_hash
    assert payload["input_candles_sha256"] is None


@pytest.mark.parametrize(
    ("source", "basis", "reason"),
    [("cron", "next_open", None), ("legacy", "cross_interp", "legacy"), ("catchup", "next_open", "catchup"),
     ("manual", "forming_close", "manual"), ("cron", "forming_close", "other")],
)
def test_exclusion_reason(source, basis, reason):
    assert db.exclusion_reason(source, basis) == reason


def test_resolved_records_hash_the_settling_window_into_the_event():
    sig = _signal()
    rec = _row(sig, win_status="SL_HIT")
    raw = [[sig.cross_candle_ms + i * H2, "100", "100.5", "98", "99", "1"] for i in range(3)]
    values = db.booked_trade_values(rec, 100.5, "next_open", 99.0, "LOSS", "2025-10-11T02:00:00Z")
    res = db.Resolution(
        rec, registry.get("macd_cross@2.2.0"), TradeOutcome("LOSS", "SL_HIT", 99.0, raw[1][0]), values, True,
        "cron", raw, H2,
    )
    window, drafts = db.resolved_records(res, now_ms=raw[-1][0] + 60_000)
    assert window["sha256"] == candles_sha256(kline_rows(raw)) and window["candle_count"] == 3
    assert (window["start_ms"], window["end_ms"]) == (raw[0][0], raw[-1][0])
    [resolved] = drafts
    assert resolved.event_type == "resolved"
    assert resolved.payload["candles"]["sha256"] == window["sha256"]
    assert resolved.payload["trade"]["net_r"] == values["net_r"] == resolved.payload["net_r"]
    assert resolved.payload["cost_model_version"] == "cm1"
    assert resolved.payload["signal"]["win_status"] == "SL_HIT"


def test_a_booking_that_moves_the_signal_to_legacy_is_recorded_as_excluded():
    sig = _signal()
    rec = _row(sig, source="legacy", entry_basis="cross_interp", strategy_version="legacy", win_status="SL_HIT")
    raw = [[sig.cross_candle_ms + i * H2, "100", "100.5", "98", "99"] for i in range(2)]
    values = db.booked_trade_values(rec, 100.0, "cross_interp", 99.0, "LOSS", "t")
    res = db.Resolution(
        rec, registry.get("macd_cross@2.2.0"), TradeOutcome("LOSS", "SL_HIT", 99.0, raw[1][0]), values, False,
        "cron", raw, H2,
    )
    _, drafts = db.resolved_records(res, now_ms=0)
    assert [d.event_type for d in drafts] == ["excluded", "resolved"]
    assert drafts[0].payload["reason"] == "legacy" and drafts[0].payload["source_before"] == "cron"
    # The trade row was already there, so the event does not claim the values it would have booked
    assert drafts[1].payload["trade"] is None


def test_genesis_drafts_restate_every_row_and_exclude_ineligible_ones():
    sig = _row(_signal(), id=4, source="legacy", entry_basis="cross_interp", strategy_version="legacy",
               created_at=T0, is_eligible=False, input_candles_sha256=None)
    trade = SimpleNamespace(
        result="LOSS", exit_price=99.0, exit_time="t", gross_r=-1.0, cost_r=0.1, net_r=-1.1, cost_model_version="cm1",
        booked_by="legacy", r_multiple=-1.0, pnl_percent=-1.5, fee_percent=0.0, slippage_bps=2.0,
        entry_basis="cross_interp",
    )
    restated, excluded = db.genesis_drafts(sig, trade)
    assert restated.event_type == "restated" and restated.payload["genesis"] is True
    assert restated.payload["trade"]["net_r"] == -1.1 and restated.payload["restatement"]["booked_r_multiple"] == -1.0
    assert excluded.event_type == "excluded" and excluded.payload["reason"] == "legacy"
    eligible = _row(_signal(), created_at=T0, is_eligible=True, input_candles_sha256=None)
    assert [d.event_type for d in db.genesis_drafts(eligible, None)] == ["restated"]
