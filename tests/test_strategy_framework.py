"""Strategy interface, registry, hashes, the frozen-version rule, the as-of market context and the
macd_cross@2.2.0 port."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pydantic
import pytest

from src.backtest.data import load_klines_csv, resample_klines
from src.backtest.replay import replay_strategy, replay_symbol
from src.config.settings import CRON_LOOKBACK_CANDLES, MACD_CANDLES, MACD_FAST, MACD_SIGNAL, MACD_SLOW
from src.services.cron_scheduler import detect_strategy_signals, fetch_plan
from src.strategy import base, registry
from src.strategy.__main__ import main as strategy_main
from src.strategy.base import ResolveContext, SignalIntent, Strategy, StrategyParams
from src.strategy.context import DAY_MS, MarketContext, normalize_klines
from src.strategy.macd import compute_macd, detect_macd_cross
from src.strategy.macd_cross import MacdCross, MacdCrossParams
from src.strategy.outcome import TradeOutcome, resolve_outcome

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
H2 = 2 * 60 * 60 * 1000
MACD = registry.get("macd_cross@2.2.0")
SHADOW = registry.get("macd_cross_ema50d_oppexit@1.0.0")


@pytest.fixture(scope="module")
def btc():
    return load_klines_csv(FIXTURES / "klines" / "BTCUSDT_2h.csv")


def _klines(n: int, start_ms: int = 1_735_689_600_000, step: int = H2) -> pd.DataFrame:
    """n synthetic candles from 2025-01-01 00:00 UTC; candle i opens at 100 + i."""
    open_ms = start_ms + step * np.arange(n, dtype=np.int64)
    price = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "open_time_ms": open_ms,
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price + 0.25,
            "volume": np.ones(n),
        }
    )


class ProbeParams(StrategyParams):
    hold_bars: int = 4
    stop_pct: float = 1.0


class DailyProbe(Strategy):
    """Test strategy reading the closed 1d candle: a LONG at every UTC midnight close."""

    key = "test_daily_probe"
    version = "0.0.1"
    name = "Daily probe"
    status = "shadow"
    context_timeframes = ("1d",)
    context_bars = 5
    params_model = ProbeParams
    seen: list

    def __init__(self, params=None):
        super().__init__(params)
        self.seen = []

    warmup_bars = property(lambda self: 0)
    history_bars = property(lambda self: 50)
    max_hold_bars = property(lambda self: self.params.hold_bars)

    def detect(self, ctx):
        day = ctx.asof("1d")
        self.seen.append((ctx.decision_ms, None if day is None else int(day["open_time_ms"])))
        bar = ctx.asof()
        if day is None or ctx.next_open is None or ctx.decision_ms % DAY_MS:
            return []
        entry = ctx.next_open
        stop = entry * (1 - self.params.stop_pct / 100)
        sig = SignalIntent(
            symbol=ctx.symbol, side="LONG", reason="probe", cross_candle_ms=int(bar["open_time_ms"]),
            cross_time="", cross_price=entry, entry_price=entry, stop_loss=stop, tp1=entry + (entry - stop),
            risk_reward_ratio=1.0, macd=0.0, signal=0.0, histogram=0.0, close=float(bar["close"]), bars_ago=0,
            strategy_key=self.key, strategy_version=self.version, features={"day_close": float(day["close"])},
        )
        return [sig]

    def resolve(self, signal, candles, ctx):
        return resolve_outcome(
            signal.side, signal.stop_loss, signal.tp1, candles, self.params.hold_bars, ctx.now_ms, ctx.interval_ms
        )


# --- protocol conformance -------------------------------------------------------------------


@pytest.mark.parametrize("strategy", registry.all_strategies(), ids=lambda s: s.ref)
def test_every_registered_strategy_honours_the_protocol(strategy, btc):
    assert isinstance(strategy, Strategy)
    assert strategy.status in base.STRATEGY_STATUSES
    assert strategy.timeframe in ("1h", "2h", "4h", "12h", "1d")
    assert all(tf in ("1h", "2h", "4h", "12h", "1d") for tf in strategy.context_timeframes)
    assert strategy.warmup_bars >= 0 and strategy.history_bars > 0 and strategy.max_hold_bars > 0
    assert isinstance(strategy.params, StrategyParams)
    with pytest.raises(pydantic.ValidationError):
        setattr(strategy.params, next(iter(strategy.params_dict)), 0)
    with pytest.raises(pydantic.ValidationError):
        type(strategy.params)(unknown_field=1)

    # detect() returns this strategy's intents for the newest candle only, from closed candles
    klines = normalize_klines(btc.iloc[:600])
    context = {tf: resample_klines(klines, strategy.timeframe, tf) for tf in strategy.context_timeframes}
    for k in range(560, 600):
        ctx = MarketContext("BTCUSDT", int(klines["open_time_ms"].iat[k]) + H2, strategy.timeframe,
                            {strategy.timeframe: klines, **context}, next_open=float(klines["open"].iat[k + 1])
                            if k + 1 < len(klines) else None)
        for sig in strategy.detect(ctx):
            assert isinstance(sig, SignalIntent)
            assert (sig.strategy_key, sig.strategy_version) == (strategy.key, strategy.version)
            assert sig.cross_candle_ms == int(klines["open_time_ms"].iat[k])


def test_strategy_without_detect_or_resolve_cannot_be_built():
    class Incomplete(Strategy):
        key, version, name, status = "x", "1", "x", "research"
        params_model = StrategyParams

    with pytest.raises(TypeError):
        Incomplete()


def test_strategy_rejects_parameters_of_another_model():
    with pytest.raises(TypeError):
        MacdCross(ProbeParams())


# --- registry -------------------------------------------------------------------------------


def test_registry_lookup_and_refs():
    assert registry.get("macd_cross@2.2.0") is MACD
    assert registry.lookup("macd_cross", "2.2.0") is MACD
    assert registry.lookup("macd_cross", "9.9.9") is None
    assert registry.parse_ref(" macd_cross@2.2.0 ") == ("macd_cross", "2.2.0")
    for bad in ("macd_cross", "@2.2.0", "macd_cross@", ""):
        with pytest.raises(ValueError):
            registry.parse_ref(bad)
    with pytest.raises(KeyError, match=r"registered: .*macd_cross@2\.2\.0"):
        registry.get("nope@1.0.0")


def test_registry_refuses_duplicates_and_unknown_statuses():
    with pytest.raises(ValueError, match="already registered"):
        registry.register(MacdCross())

    class Bad(DailyProbe):
        status = "paper"

    with pytest.raises(ValueError, match="status"):
        registry.register(Bad())


def test_runnable_lists_live_before_shadow_and_skips_research():
    probe = registry.register(DailyProbe())

    class Research(DailyProbe):
        key, status = "test_research_probe", "research"

    research = registry.register(Research())
    try:
        refs = [s.ref for s in registry.runnable()]
        assert refs.index("macd_cross@2.2.0") < refs.index(probe.ref)
        assert research.ref not in refs
    finally:
        registry.unregister(probe.key, probe.version)
        registry.unregister(research.key, research.version)


def test_unregistered_versions_are_settled_by_the_production_rules():
    assert registry.resolver_for("macd_cross", "legacy") is MACD
    assert registry.resolver_for(None, None) is MACD
    assert registry.resolver_for("macd_cross", "2.2.0") is MACD


# --- hashes ---------------------------------------------------------------------------------


def test_params_hash_is_sha256_of_canonical_json():
    import hashlib

    canonical = json.dumps(MACD.params_dict, sort_keys=True, separators=(",", ":"))
    assert MACD.params_hash == hashlib.sha256(canonical.encode()).hexdigest()
    # Field order and object identity do not matter; values do
    assert MacdCross(MacdCrossParams(**dict(reversed(MACD.params_dict.items())))).params_hash == MACD.params_hash
    assert MacdCross(MacdCrossParams(risk_reward_ratio=2.0)).params_hash != MACD.params_hash
    assert MacdCross(MacdCrossParams(risk_reward_ratio=1.5)).params_hash == MACD.params_hash


def test_code_hash_covers_the_strategy_module_and_its_math(monkeypatch):
    original = base.module_source
    assert MACD.code_hash == base.source_hash(("src.strategy.macd_cross", "src.strategy.macd", "src.strategy.outcome"))
    assert MACD.code_hash == MacdCross().code_hash

    for module in ("src.strategy.macd_cross", "src.strategy.macd", "src.strategy.outcome"):
        monkeypatch.setattr(
            base, "module_source", lambda name, m=module: original(name) + (b"# edit\n" if name == m else b"")
        )
        assert MACD.code_hash != registry.lock_record(MACD)["code_hash"], module
    monkeypatch.setattr(base, "module_source", original)


def test_code_hash_ignores_line_endings(tmp_path, monkeypatch):
    pkg = tmp_path / "crlf_probe.py"
    pkg.write_bytes(b"X = 1\r\nY = 2\r\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    lf = tmp_path / "lf_probe.py"
    lf.write_bytes(b"X = 1\nY = 2\n")
    assert base.module_source("crlf_probe") == base.module_source("lf_probe")


# --- the lock and the frozen rule -----------------------------------------------------------


def test_every_live_and_shadow_strategy_is_frozen_and_matches_the_lock():
    """The CI guard: editing a frozen strategy's code or parameters fails here, before it ships and
    the engine starts refusing it. Give the change a new version, or rehash a proven no-op refactor."""
    lock = registry.load_lock()
    for strategy in registry.runnable():
        entry = lock.get(strategy.ref)
        assert entry is not None, f"{strategy.ref} is not frozen in {registry.LOCK_PATH.name}"
        assert entry["params_hash"] == strategy.params_hash, f"{strategy.ref}: parameters drifted"
        assert entry["code_hash"] == strategy.code_hash, f"{strategy.ref}: code drifted"
        assert entry["params"] == strategy.params_dict
    assert lock["macd_cross@2.2.0"]["params"] == {
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "macd_candles": 500,
        "risk_reward_ratio": 1.5, "swing_lookback_candles": 5, "signal_expiry_candles": 48,
    }
    assert strategy_main(["verify"]) == 0


def _record(strategy, **overrides):
    return {"frozen": True, "params_hash": strategy.params_hash, "code_hash": strategy.code_hash, **overrides}


def test_engine_refuses_unfrozen_or_drifted_versions():
    assert registry.emission_refusal(MACD, _record(MACD)) is None
    assert registry.emission_refusal(MACD, None) == "no strategy_versions row"
    assert registry.emission_refusal(MACD, _record(MACD, frozen=False)) == "version is not frozen"
    assert "params_hash" in registry.emission_refusal(MACD, _record(MACD, params_hash="0" * 64))
    assert "code_hash" in registry.emission_refusal(MACD, _record(MACD, code_hash="0" * 64))

    class Research(DailyProbe):
        status = "research"

    assert registry.emission_refusal(Research(), _record(Research())) == "status is research"


def test_emitting_strategies_logs_refusals_and_falls_back_to_the_lock(caplog):
    allowed, refused = registry.emitting_strategies({"macd_cross@2.2.0": _record(MACD, frozen=False)})
    assert allowed == []
    assert refused == {"macd_cross@2.2.0": "version is not frozen", SHADOW.ref: "no strategy_versions row"}
    assert "macd_cross@2.2.0 refused to emit signals: version is not frozen" in caplog.text

    # No database: the committed lock is the frozen record
    assert registry.emitting_strategies(None)[0] == [MACD, SHADOW]
    allowed, refused = registry.emitting_strategies(None, lock={})
    assert allowed == []
    assert refused == {"macd_cross@2.2.0": "no strategy_versions row", SHADOW.ref: "no strategy_versions row"}
    tampered = {"macd_cross@2.2.0": {"params_hash": MACD.params_hash, "code_hash": "f" * 64}}
    assert "code_hash" in registry.emitting_strategies(None, lock=tampered)[1]["macd_cross@2.2.0"]


def test_freeze_never_overwrites_and_rehash_keeps_history(tmp_path, monkeypatch):
    lock_path = tmp_path / "versions.lock.json"
    probe = registry.register(DailyProbe())
    try:
        assert strategy_main(["--lock", str(lock_path), "freeze", probe.ref, "--at", "2026-10-15T00:00:00Z"]) == 0
        entry = json.loads(lock_path.read_text())[probe.ref]
        assert entry["params_hash"] == probe.params_hash and entry["frozen_at"] == "2026-10-15T00:00:00Z"
        assert strategy_main(["--lock", str(lock_path), "freeze", probe.ref]) == 0  # same hashes: no-op

        original = base.module_source
        monkeypatch.setattr(base, "module_source", lambda name: original(name) + b"# refactor\n")
        assert strategy_main(["--lock", str(lock_path), "freeze", probe.ref]) == 1
        assert strategy_main(["--lock", str(lock_path), "verify"]) == 1
        assert strategy_main(["--lock", str(lock_path), "rehash", probe.ref, "--reason", "rename"]) == 0
        rehashed = json.loads(lock_path.read_text())[probe.ref]
        assert rehashed["code_hash"] == probe.code_hash
        assert rehashed["code_hash_history"][0]["code_hash"] == entry["code_hash"]
        assert rehashed["params_hash"] == entry["params_hash"]
    finally:
        registry.unregister(probe.key, probe.version)


def test_rehash_cannot_change_parameters(tmp_path):
    lock_path = tmp_path / "versions.lock.json"
    lock_path.write_text(json.dumps({"macd_cross@2.2.0": {"params_hash": "0" * 64, "code_hash": "0" * 64}}))
    assert strategy_main(["--lock", str(lock_path), "rehash", "macd_cross@2.2.0", "--reason", "x"]) == 1
    assert strategy_main(["--lock", str(lock_path), "freeze", "nope@1"]) == 2


# --- market context: closed candles only ----------------------------------------------------


def test_context_never_returns_a_candle_closing_after_the_decision():
    klines = normalize_klines(_klines(10))
    t0 = int(klines["open_time_ms"].iat[0])
    # Decision exactly at the close of candle 3: candle 3 is closed, candle 4 (closing 2h later) is not
    ctx = MarketContext("X", t0 + 4 * H2, "2h", {"2h": klines})
    assert len(ctx.closed()) == 4
    assert int(ctx.asof()["open_time_ms"]) == t0 + 3 * H2
    # One millisecond before that close, candle 3 is still forming
    ctx = MarketContext("X", t0 + 4 * H2 - 1, "2h", {"2h": klines})
    assert int(ctx.asof()["open_time_ms"]) == t0 + 2 * H2
    # Asking for a later instant cannot see past the decision
    assert int(ctx.asof(at_ms=t0 + 9 * H2)["open_time_ms"]) == t0 + 2 * H2
    assert len(ctx.closed(at_ms=t0 + 2 * H2)) == 2
    # Before the first close there is nothing; at it, the first candle
    assert MarketContext("X", t0 + H2 - 1, "2h", {"2h": klines}).asof() is None
    assert int(MarketContext("X", t0 + H2, "2h", {"2h": klines}).asof()["open_time_ms"]) == t0
    assert ctx.closed().equals(ctx.closed().loc[ctx.closed()["open_time_ms"] + H2 <= ctx.decision_ms])


def test_daily_candle_is_visible_only_from_the_utc_midnight_that_closes_it():
    two_h = normalize_klines(_klines(36))  # 2025-01-01 00:00 to 2025-01-03 22:00
    daily = resample_klines(two_h, "2h", "1d")
    day1 = int(two_h["open_time_ms"].iat[0])
    frames = {"2h": two_h, "1d": daily}
    # At 22:00 on day 1 (the 20:00 candle just closed) day 1 has not closed
    assert MarketContext("X", day1 + DAY_MS - H2, "2h", frames).asof("1d") is None
    # At midnight it has, with the full day's OHLC
    day = MarketContext("X", day1 + DAY_MS, "2h", frames).asof("1d")
    assert int(day["open_time_ms"]) == day1
    assert (day["open"], day["close"]) == (100.0, 111.25)
    assert (day["high"], day["low"], day["bars"]) == (111.5, 99.5, 12)
    # Two hours into day 2, still day 1; day 3 (complete in the data) only from its own midnight
    assert int(MarketContext("X", day1 + DAY_MS + H2, "2h", frames).asof("1d")["open_time_ms"]) == day1
    assert int(MarketContext("X", day1 + 3 * DAY_MS, "2h", frames).asof("1d")["open_time_ms"]) == day1 + 2 * DAY_MS


def test_partial_last_day_is_never_returned():
    two_h = normalize_klines(_klines(18))  # day 2 has only 6 of its 12 candles
    daily = resample_klines(two_h, "2h", "1d")
    assert list(daily["bars"]) == [12, 6]
    last_close = int(two_h["open_time_ms"].iat[-1]) + H2
    ctx = MarketContext("X", last_close, "2h", {"2h": two_h, "1d": daily})
    assert len(ctx.closed("1d")) == 1


@pytest.mark.parametrize("unit", ["ns", "us", "ms", "s"])
def test_context_reads_open_times_in_milliseconds_whatever_the_datetime_resolution(unit):
    """pandas 3 parses epoch ms to datetime64[ms]; pandas 2 gave [ns]. Casting either to int64 and
    assuming nanoseconds misplaces every candle by a factor of 10^6, which can let future candles
    through an as-of filter. normalize_klines must give the same open_time_ms for every unit."""
    raw = _klines(5)
    stamped = raw.drop(columns="open_time_ms")
    stamped["timestamp"] = pd.to_datetime(raw["open_time_ms"], unit="ms").astype(f"datetime64[{unit}]")
    assert normalize_klines(stamped)["open_time_ms"].tolist() == raw["open_time_ms"].tolist()
    # The trap itself: the raw int64 of a [ms] column is not nanoseconds
    as_ms = pd.to_datetime(raw["open_time_ms"], unit="ms").astype("datetime64[ms]")
    assert int(as_ms.astype("int64").iat[0]) == int(raw["open_time_ms"].iat[0])


def test_context_accepts_timezone_aware_timestamps():
    raw = _klines(3)
    aware = raw.drop(columns="open_time_ms")
    aware["timestamp"] = pd.to_datetime(raw["open_time_ms"], unit="ms", utc=True).dt.tz_convert("Asia/Jakarta")
    assert normalize_klines(aware)["open_time_ms"].tolist() == raw["open_time_ms"].tolist()


def test_context_rejects_frames_it_cannot_order():
    with pytest.raises(ValueError, match="not normalized"):
        MarketContext("X", 0, "2h", {"2h": pd.DataFrame({"open": [1.0]})})
    with pytest.raises(ValueError, match="no 2h klines"):
        MarketContext("X", 0, "2h", {"1d": normalize_klines(_klines(2))})
    with pytest.raises(ValueError, match="unsupported timeframe"):
        MarketContext("X", 0, "2h", {"2h": normalize_klines(_klines(2)), "3m": normalize_klines(_klines(2))})


def test_live_context_decides_at_the_last_boundary_and_sees_only_the_forming_open():
    raw = _klines(6)
    forming_open = int(raw["open_time_ms"].iat[-1])
    live = raw.drop(columns="open_time_ms")
    live["timestamp"] = pd.to_datetime(raw["open_time_ms"], unit="ms")
    ctx = MarketContext.from_klines("X", forming_open + 15_000, "2h", {"2h": live}, catchup_bars=3)
    assert ctx.decision_ms == forming_open
    assert ctx.next_open == 105.0 and ctx.catchup_bars == 3
    assert len(ctx.closed()) == 5
    frame = ctx.bars_with_entry()
    assert len(frame) == 6
    # Only the open of the forming candle is real; its high, low and close are not leaked
    last = frame.iloc[-1]
    assert (last["open"], last["high"], last["low"], last["close"], last["volume"]) == (105.0, 105.0, 105.0, 105.0, 0.0)
    assert last["timestamp"] == pd.Timestamp(forming_open, unit="ms")
    # Without the forming candle, the closed candles come alone
    ctx = MarketContext.from_klines("X", forming_open + 15_000, "2h", {"2h": live.iloc[:5]})
    assert ctx.next_open is None and len(ctx.bars_with_entry()) == 5


def test_resample_matches_exchange_daily_candles_when_present():
    """1d resampled from 2h equals Binance's own 1d klines: first open, max high, min low, last close,
    summed volume over the 12 two-hour candles of the UTC day (fixture from data.binance.vision)."""
    path = FIXTURES / "klines" / "BTCUSDT_1d.csv"
    two_h = load_klines_csv(FIXTURES / "klines" / "BTCUSDT_2h.csv")
    exchange = normalize_klines(load_klines_csv(path))
    resampled = resample_klines(two_h, "2h", "1d")
    full = resampled[resampled["bars"] == 12]
    merged = full.merge(exchange, on="open_time_ms", suffixes=("", "_x"))
    assert len(merged) >= 160
    for col in ("open", "high", "low", "close"):
        assert (merged[col] == merged[f"{col}_x"]).all(), col
    assert np.allclose(merged["volume"], merged["volume_x"], rtol=1e-9)


def test_resample_refuses_buckets_that_do_not_tile_a_day():
    with pytest.raises(ValueError):
        resample_klines(_klines(3), "2h", "1h")
    with pytest.raises(ValueError):
        resample_klines(_klines(3), "2h", "1w")
    assert list(resample_klines(_klines(12), "2h", "12h")["bars"]) == [6, 6]


# --- macd_cross@2.2.0 port parity -----------------------------------------------------------


def test_port_replays_exactly_the_phase0_trades(btc):
    for warmup in (None, 0):
        phase0 = replay_symbol(btc, "BTCUSDT", **({} if warmup is None else {"warmup": warmup}))
        ported = replay_strategy(MACD, btc, "BTCUSDT", warmup=warmup)
        assert ported.trades == phase0.trades
        assert ported.unresolved == phase0.unresolved
    assert len(ported.trades) == 144


def test_candidate_shortcut_skips_no_signal(btc):
    """Running detect() after every close finds exactly what the candidate shortcut finds."""
    klines = btc.iloc[:700]
    full = replay_strategy(MACD, klines, "BTCUSDT", warmup=0, candidates=False)
    fast = replay_strategy(MACD, klines, "BTCUSDT", warmup=0)
    assert len(full.trades) > 20
    assert full == fast


def test_port_detects_what_the_old_scheduled_scan_did_bar_by_bar(btc):
    """The cron path (fetch window, MarketContext as of the boundary, MacdCross.detect) gives the same
    signal, field for field, as the pre-registry scan (compute_macd + detect_macd_cross on the
    fetched window) after every close, the forming candle included in the fetch."""
    klines = btc.iloc[: MACD_CANDLES + 150].reset_index(drop=True)
    compared = 0
    for forming in range(MACD_CANDLES - 1, len(klines)):
        window = klines.iloc[forming + 1 - MACD_CANDLES : forming + 1].reset_index(drop=True)
        df = compute_macd(window, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        old = detect_macd_cross(df, "BTCUSDT", lookback_candles=CRON_LOOKBACK_CANDLES, include_current=False)
        now_ms = int(window["open_time_ms"].iat[-1]) + 15_000
        live = window.drop(columns="open_time_ms")
        new = detect_strategy_signals(MACD, "BTCUSDT", {"2h": live}, now_ms)
        if old is None:
            assert new == []
            continue
        [intent] = new
        assert {k: v for k, v in intent.to_dict().items() if k in old.to_dict()} == old.to_dict()
        assert (intent.strategy_key, intent.strategy_version) == ("macd_cross", "2.2.0")
        compared += 1
    assert compared > 20


def test_port_resolves_like_resolve_outcome():
    candles = [[i * H2, "100", "101", "99", "100"] for i in range(60)]
    sig = SignalIntent(
        symbol="X", side="LONG", reason="r", cross_candle_ms=0, cross_time="", cross_price=100.0,
        entry_price=100.0, stop_loss=90.0, tp1=115.0, risk_reward_ratio=1.5, macd=0, signal=0,
        histogram=0, close=100.0, bars_ago=0,
    )
    ctx = ResolveContext(now_ms=60 * H2, interval_ms=H2)
    assert MACD.resolve(sig, candles, ctx) == resolve_outcome("LONG", 90.0, 115.0, candles, 48, 60 * H2, H2)
    assert MACD.resolve(sig, candles, ctx) == TradeOutcome("EXPIRED", "EXPIRED", 100.0, 48 * H2)
    assert MACD.max_hold_bars == 48 and MACD.warmup_bars == 498 and MACD.history_bars == 500


def test_port_ignores_environment_overrides(monkeypatch):
    """The frozen version keeps its parameters even if the environment's settings differ."""
    import src.strategy.macd as macd_module

    monkeypatch.setattr(macd_module, "RISK_REWARD_RATIO", 3.0)
    klines = normalize_klines(_klines(80))
    klines.loc[60:, "close"] = klines.loc[60:, "close"] - np.arange(20) * 3  # force a death cross
    ctx = MarketContext("X", int(klines["open_time_ms"].iat[-1]) + H2, "2h", {"2h": klines}, next_open=50.0,
                        catchup_bars=20)
    [sig] = MACD.detect(ctx)
    assert sig.risk_reward_ratio == 1.5


# --- live fetch plan ------------------------------------------------------------------------


def test_fetch_plan_fetches_each_timeframe_once_with_the_largest_window():
    probe = DailyProbe()
    assert fetch_plan([MACD]) == {"2h": 500}
    assert fetch_plan([MACD, probe]) == {"2h": 500, "1d": 35}


def test_each_strategy_sees_only_its_own_window(btc):
    probe = DailyProbe()
    two_h = btc.iloc[:600]
    frames = {"2h": two_h, "1d": resample_klines(two_h, "2h", "1d")}
    now_ms = int(two_h["open_time_ms"].iat[-1]) + 60_000
    detect_strategy_signals(probe, "BTCUSDT", frames, now_ms)
    decision, day = probe.seen[-1]
    assert day is not None and day + DAY_MS <= decision
    # A strategy whose context timeframe failed to fetch is skipped, not run on partial data
    assert detect_strategy_signals(probe, "BTCUSDT", {"2h": two_h, "1d": None}, now_ms) == []


def test_cron_path_on_binance_shaped_klines(btc):
    """fetch_klines frames (string columns, datetime64 timestamps, no open_time_ms) go through the same
    path and give the old scan's signal."""
    from types import SimpleNamespace

    from src.data_feed.ohlcv import fetch_klines

    window = btc.iloc[: MACD_CANDLES + 40]
    rows = [
        [int(r.open_time_ms), f"{r.open}", f"{r.high}", f"{r.low}", f"{r.close}", "1.0",
         int(r.open_time_ms) + H2 - 1, "100.0", 10, "0.5", "50.0", "0"]
        for r in window.itertuples()
    ]
    found = 0
    for end in range(MACD_CANDLES, len(rows) + 1):
        client = SimpleNamespace(klines=lambda symbol, interval, limit, end=end: rows[end - limit : end])
        frame = fetch_klines(client, "BTCUSDT", "2h", MACD_CANDLES)
        old_df = compute_macd(frame, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        old = detect_macd_cross(old_df, "BTCUSDT", lookback_candles=CRON_LOOKBACK_CANDLES)
        new = detect_strategy_signals(MACD, "BTCUSDT", {"2h": frame}, rows[end - 1][0] + 15_000)
        assert [s.to_dict() for s in new] == (
            [{**old.to_dict(), "strategy_key": "macd_cross", "strategy_version": "2.2.0", "features": {}}] if old else []
        )
        found += bool(old)
    assert found > 5
