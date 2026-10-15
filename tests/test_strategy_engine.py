"""The registry wired into the app: the scheduled scan runs live and shadow strategies, refuses
unfrozen or drifted versions, keeps shadow signals off Telegram; the evaluator settles each signal by
its own strategy; and the backtest harness runs any registered strategy and logs every trial."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.backtest.__main__ import main as backtest_main
from src.backtest.run import run_backtest
from src.backtest.trials import dataset_description, research_trial_row, trial_entry
from src.services import cron_scheduler as cron_module
from src.services import db, redis_client
from src.strategy import registry
from src.strategy.base import SignalIntent, Strategy, StrategyParams
from src.strategy.context import DAY_MS
from src.strategy.outcome import TradeOutcome, resolve_outcome

pytestmark = pytest.mark.unit

KLINES_DIR = Path(__file__).parent / "fixtures" / "klines"
H2 = 2 * 60 * 60 * 1000
MACD = registry.get("macd_cross@2.2.0")


class ShadowParams(StrategyParams):
    hold_bars: int = 4


class ShadowDaily(Strategy):
    """A shadow strategy reading closed daily candles: LONG at each UTC midnight, 1% stop, 1R target."""

    key = "test_shadow_daily"
    version = "0.1.0"
    name = "Shadow daily probe"
    status = "shadow"
    context_timeframes = ("1d",)
    context_bars = 3
    params_model = ShadowParams
    resolve_calls: list

    def __init__(self, params=None):
        super().__init__(params)
        self.violations = 0
        self.resolve_calls = []

    warmup_bars = property(lambda self: 0)
    history_bars = property(lambda self: 60)
    max_hold_bars = property(lambda self: self.params.hold_bars)

    def detect(self, ctx):
        day = ctx.asof("1d")
        if day is not None and int(day["open_time_ms"]) + DAY_MS > ctx.decision_ms:
            self.violations += 1
        bar = ctx.asof()
        if day is None or ctx.next_open is None or ctx.decision_ms % DAY_MS:
            return []
        entry = float(ctx.next_open)
        return [_intent(self, ctx.symbol, int(bar["open_time_ms"]), entry, entry * 0.99, entry * 1.01)]

    def resolve(self, signal, candles, ctx):
        self.resolve_calls.append(len(candles))
        return resolve_outcome(
            signal.side, signal.stop_loss, signal.tp1, candles, self.params.hold_bars, ctx.now_ms, ctx.interval_ms
        )


def _intent(strategy, symbol, bar_ms, entry, stop, tp1, side="LONG"):
    return SignalIntent(
        symbol=symbol, side=side, reason="test", cross_candle_ms=bar_ms, cross_time="2025-01-01T00:00:00Z",
        cross_price=entry, entry_price=entry, stop_loss=stop, tp1=tp1, risk_reward_ratio=1.0, macd=0.0,
        signal=0.0, histogram=0.0, close=entry, bars_ago=0, strategy_key=strategy.key,
        strategy_version=strategy.version,
    )


@pytest.fixture
def shadow():
    """The probe as the only shadow strategy: the pre-registered one is set aside meanwhile, so these
    tests see the engine with the live strategy and the probe (tests/test_shadow_strategy.py covers it)."""
    preregistered = [s for s in registry.all_strategies() if s.status == "shadow"]
    for s in preregistered:
        registry.unregister(s.key, s.version)
    strategy = registry.register(ShadowDaily())
    yield strategy
    registry.unregister(strategy.key, strategy.version)
    for s in preregistered:
        registry.register(s)


def _frozen(*strategies, **overrides):
    return {
        s.ref: {"frozen": True, "params_hash": s.params_hash, "code_hash": s.code_hash, **overrides.get(s.ref, {})}
        for s in strategies
    }


# --- scheduled scan -------------------------------------------------------------------------


@pytest.fixture
def scan(monkeypatch):
    state = {"fetches": [], "saved": [], "claims": [], "alerts": [], "detect_calls": [], "stored": None}

    async def fake_record(**kwargs):
        return True

    async def fake_evaluate(client):
        return {"evaluated_count": 0}

    async def fake_notify(closed):
        return False

    async def fake_watchlist(active_only=False):
        return [{"symbol": "BTCUSDT"}]

    async def fake_versions():
        return state["stored"]

    async def fake_claim(side, symbol, candle_ms, timeframe="2h", strategy=None):
        state["claims"].append((symbol, candle_ms, strategy))
        return True

    async def fake_save(sig, timeframe="2h", source="manual", detected_at=None):
        state["saved"].append((sig.strategy_key, sig.strategy_version, source))
        return {"id": len(state["saved"]), "inserted": True}

    def fake_fetch(client, sym, tf, limit):
        state["fetches"].append((sym, tf, limit))
        return f"{tf}-frame"

    def fake_detect(strategy, sym, frames, now_ms):
        state["detect_calls"].append(strategy.ref)
        assert set(frames) == {"2h", "1d"} or set(frames) == {"2h"}
        return [_intent(strategy, sym, 1_735_689_600_000, 100.0, 99.0, 101.0)]

    def fake_send(body):
        state["alerts"].append(body)

        async def done():
            return True

        return done()

    def fake_spawn(coro, name=None):
        coro.close()

    for name, value in {
        "record_cron_run": fake_record,
        "evaluate_pending_signals": fake_evaluate,
        "notify_closed_trades": fake_notify,
        "get_watchlist": fake_watchlist,
        "get_strategy_versions": fake_versions,
        "claim_cross": fake_claim,
        "save_macd_signal": fake_save,
        "fetch_klines": fake_fetch,
        "detect_strategy_signals": fake_detect,
        "send_alert": fake_send,
        "run_in_background": fake_spawn,
        "is_late_signal": lambda *a: False,
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "SHADOW_ALERTS": False,
    }.items():
        monkeypatch.setattr(cron_module, name, value)
    monkeypatch.setattr(redis_client, "_redis", None)
    return state


def _scheduler():
    sched = cron_module.MacdCronScheduler()
    sched._client_getter = lambda: object()
    return sched


async def test_cron_runs_live_and_shadow_but_alerts_only_live(scan, shadow):
    scan["stored"] = _frozen(MACD, shadow)
    sched = _scheduler()
    res = await sched._execute_job(is_manual=False)

    assert res["new_signals_count"] == 2
    assert res["strategies"] == ["macd_cross@2.2.0", shadow.ref]
    # Each timeframe fetched once per symbol, at the largest window any strategy needs
    assert sorted(scan["fetches"]) == [("BTCUSDT", "1d", 35), ("BTCUSDT", "2h", 500)]
    assert scan["saved"] == [("macd_cross", "2.2.0", "cron"), (shadow.key, shadow.version, "cron")]
    # The live strategy keeps the original claim keys; the shadow one claims its own
    assert [c[2] for c in scan["claims"]] == [None, shadow.ref]
    assert len(scan["alerts"]) == 1 and "scheduled 2H scan" in scan["alerts"][0]
    assert sched.get_status()["strategies"] == {"emitting": ["macd_cross@2.2.0", shadow.ref], "refused": {}}


async def test_shadow_alerts_are_opt_in_and_labelled(scan, shadow, monkeypatch):
    monkeypatch.setattr(cron_module, "SHADOW_ALERTS", True)
    scan["stored"] = _frozen(MACD, shadow)
    await _scheduler()._execute_job(is_manual=False)
    assert len(scan["alerts"]) == 2
    assert f"shadow {shadow.ref}" in scan["alerts"][1] and "paper only" in scan["alerts"][1]


async def test_cron_refuses_unfrozen_and_drifted_versions(scan, shadow, caplog):
    scan["stored"] = _frozen(MACD, shadow, **{
        "macd_cross@2.2.0": {"frozen": False},
        shadow.ref: {"code_hash": "0" * 64},
    })
    sched = _scheduler()
    with caplog.at_level(logging.ERROR):
        res = await sched._execute_job(is_manual=False)

    assert res["new_signals_count"] == 0 and res["strategies"] == []
    assert scan["detect_calls"] == [] and scan["fetches"] == [] and scan["saved"] == []
    refused = sched.get_status()["strategies"]["refused"]
    assert refused["macd_cross@2.2.0"] == "version is not frozen"
    assert refused[shadow.ref].startswith("code_hash")
    assert "refused to emit signals" in caplog.text


async def test_cron_refuses_a_version_missing_from_the_database(scan, shadow):
    scan["stored"] = _frozen(MACD)
    res = await _scheduler()._execute_job(is_manual=False)
    assert res["strategies"] == ["macd_cross@2.2.0"]
    assert res["refused_strategies"] == {shadow.ref: "no strategy_versions row"}
    assert scan["detect_calls"] == ["macd_cross@2.2.0"]


async def test_cron_without_a_database_trusts_only_the_committed_lock(scan, shadow):
    # The shadow probe is registered in this test but not frozen in versions.lock.json
    scan["stored"] = None
    res = await _scheduler()._execute_job(is_manual=False)
    assert res["strategies"] == ["macd_cross@2.2.0"]
    assert res["refused_strategies"] == {shadow.ref: "no strategy_versions row"}


async def test_a_failing_strategy_does_not_stop_the_others(scan, shadow, monkeypatch):
    scan["stored"] = _frozen(MACD, shadow)

    def detect(strategy, sym, frames, now_ms):
        if strategy is shadow:
            raise RuntimeError("boom")
        return [_intent(strategy, sym, 1, 100.0, 99.0, 101.0)]

    monkeypatch.setattr(cron_module, "detect_strategy_signals", detect)
    res = await _scheduler()._execute_job(is_manual=False)
    assert res["new_signals_count"] == 1


async def test_redis_claims_are_scoped_per_strategy(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.store = {}

        async def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "_redis", fake)
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1)
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1, strategy="other@1.0.0")
    assert not await redis_client.claim_cross("LONG", "BTCUSDT", 1, strategy="other@1.0.0")
    assert set(fake.store) == {"macd:2h:long:BTCUSDT:1", "sig:other@1.0.0:2h:long:BTCUSDT:1"}


# --- storing ----------------------------------------------------------------------------------


def test_intents_are_stored_under_their_own_strategy_and_ad_hoc_signals_under_the_headline(shadow):
    intent = _intent(shadow, "BTCUSDT", 1, 100.0, 99.0, 101.0)
    params = db.build_signal_insert(intent, "2h", source="cron").compile(dialect=postgresql.dialect()).params
    assert (params["strategy_key"], params["strategy_version"]) == (shadow.key, shadow.version)
    plain = SimpleNamespace(**{k: v for k, v in intent.to_dict().items() if k not in ("strategy_key", "strategy_version")})
    assert db.signal_strategy(plain) == (db.STRATEGY_KEY, db.STRATEGY_VERSION)


def test_shadow_signals_never_count_in_the_headline_track_record():
    sql = str(db.counted_signal().compile(dialect=postgresql.dialect()))
    assert "macd_signals.strategy_key = " in sql and "macd_signals.strategy_version = " in sql


# --- evaluator dispatch ---------------------------------------------------------------------


class _Result:
    def __init__(self, value=None, rows=None):
        self._value, self._rows = value, rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _Session:
    def __init__(self, results):
        self.results, self.commits = list(results), 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return self.results.pop(0) if self.results else _Result()

    async def commit(self):
        self.commits += 1


def _pending(**overrides):
    rec = dict(
        id=7, symbol="BTCUSDT", side="LONG", timeframe="2h", win_status="PENDING",
        cross_candle_ms=1_700_000_000_000, entry_price=100.0, cross_price=99.5, stop_loss=99.0, tp1=101.0,
        risk_reward_ratio=1.0, cross_time="2023-11-14T22:00:00Z", is_confirmed=True, entry_basis="next_open",
        source="cron", strategy_key="macd_cross", strategy_version="2.2.0",
    )
    rec.update(overrides)
    return SimpleNamespace(**rec)


async def _evaluate(monkeypatch, rec, klines):
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return klines

    monkeypatch.setattr(db, "_session_maker", lambda: _Session([_Result(rows=[rec]), _Result(value=1)]))
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())
    result = await db.evaluate_pending_signals(SimpleNamespace(klines=fetch))
    return result, calls


async def test_evaluator_settles_each_signal_with_its_own_strategy(monkeypatch, shadow):
    klines = [[1_700_000_000_000 + i * H2, "100", "100.5", "99.5", "100"] for i in range(6)]
    klines[2] = [klines[2][0], "100", "101.5", "99.5", "101"]  # target on the second candle
    rec = _pending(strategy_key=shadow.key, strategy_version=shadow.version)
    result, calls = await _evaluate(monkeypatch, rec, klines)

    assert calls == [{"symbol": "BTCUSDT", "interval": "2h", "startTime": rec.cross_candle_ms, "limit": 5}]
    assert shadow.resolve_calls == [5]
    assert rec.win_status == "TP1_HIT" and result["profit_count"] == 1
    # Shadow trades close silently
    assert result["closed"] == []


async def test_shadow_outcome_alerts_are_opt_in(monkeypatch, shadow):
    monkeypatch.setattr(db, "SHADOW_ALERTS", True)
    klines = [[1_700_000_000_000 + i * H2, "100", "100.5", "98", "99"] for i in range(6)]
    result, _ = await _evaluate(monkeypatch, _pending(strategy_key=shadow.key, strategy_version=shadow.version), klines)
    [trade] = result["closed"]
    assert trade["shadow_strategy"] == shadow.ref and trade["result"] == "LOSS"


async def test_evaluator_keeps_the_production_request_for_macd_cross(monkeypatch):
    klines = [[1_700_000_000_000 + i * H2, "100", "100.5", "98", "99"] for i in range(3)]
    for rec in (_pending(), _pending(strategy_version="legacy"), _pending(strategy_key=None, strategy_version=None)):
        result, calls = await _evaluate(monkeypatch, rec, klines)
        assert calls == [{"symbol": "BTCUSDT", "interval": "2h", "startTime": rec.cross_candle_ms, "limit": 49}]
        assert rec.win_status == "SL_HIT" and len(result["closed"]) == 1


async def test_evaluator_gives_history_to_strategies_that_ask_for_it(monkeypatch, shadow):
    seen = {}

    class WithHistory(ShadowDaily):
        version = "0.2.0"
        resolve_history_bars = 3

        def resolve(self, signal, candles, ctx):
            seen["candles"] = [c[0] for c in candles]
            seen["history"] = ctx.history.closed()["open_time_ms"].tolist()
            seen["decision"] = ctx.history.decision_ms
            return TradeOutcome("EXPIRED", "EXPIRED", 100.0, candles[-1][0] + H2)

    strategy = registry.register(WithHistory())
    try:
        cross = 1_700_000_000_000
        klines = [[cross + i * H2, "100", "100.5", "99.5", "100", "1"] for i in range(-3, 5)]
        rec = _pending(strategy_key=strategy.key, strategy_version=strategy.version, cross_candle_ms=cross)
        _, calls = await _evaluate(monkeypatch, rec, klines)
        assert calls[0]["startTime"] == cross - 3 * H2 and calls[0]["limit"] == 3 + 4 + 1
        assert seen["history"] == [cross + i * H2 for i in range(-3, 1)]
        assert seen["candles"] == [cross + i * H2 for i in range(1, 5)]
        assert seen["decision"] == cross + H2
    finally:
        registry.unregister(strategy.key, strategy.version)


def _exchange(rows):
    """A klines endpoint over `rows`: startTime+limit returns the first `limit` rows from startTime,
    endTime+limit the newest `limit` rows up to endTime (as Binance does)."""
    calls = []

    def klines(**kwargs):
        calls.append(kwargs)
        if "endTime" in kwargs:
            return [r for r in rows if r[0] <= kwargs["endTime"]][-kwargs["limit"]:]
        return [r for r in rows if r[0] >= kwargs["startTime"]][: kwargs["limit"]]

    return SimpleNamespace(klines=klines), calls


async def test_a_gap_in_the_klines_still_gives_the_strategy_its_history_by_count(monkeypatch, shadow):
    """Live settlement reads the same history the replay does (the newest history+1 candles up to the
    cross, by position), so a hole in the exchange's klines cannot leave the trade open for ever."""

    class WithHistory(ShadowDaily):
        version = "0.3.0"
        resolve_history_bars = 4

    strategy = registry.register(WithHistory())
    try:
        cross = 1_700_000_000_000
        # Two candles missing inside the time-anchored history window
        rows = [[cross + i * H2, "100", "100.5", "99.5", "100", "1"] for i in range(-8, 6) if i not in (-3, -2)]
        rec = _pending(strategy_key=strategy.key, strategy_version=strategy.version, cross_candle_ms=cross)
        client, calls = _exchange(rows)
        got = db.fetch_settlement_klines(client, rec, strategy)
        assert calls[0] == {"symbol": "BTCUSDT", "interval": "2h", "startTime": cross - 4 * H2, "limit": 4 + strategy.max_hold_bars + 1}
        assert calls[1] == {"symbol": "BTCUSDT", "interval": "2h", "endTime": cross, "limit": 5}
        history = [r[0] for r in got if r[0] <= cross]
        assert history == [r[0] for r in rows if r[0] <= cross][-5:]
        assert [r[0] for r in got if r[0] > cross] == [cross + i * H2 for i in range(1, 6)]
        assert len(db.history_context(rec, got, 4).closed()) == 5

        # Without a gap the request is the single one it always was
        full = [[cross + i * H2, "100", "100.5", "99.5", "100", "1"] for i in range(-8, 6)]
        client, calls = _exchange(full)
        db.fetch_settlement_klines(client, rec, strategy)
        assert len(calls) == 1
    finally:
        registry.unregister(strategy.key, strategy.version)


async def test_an_unregistered_strategy_is_never_settled_by_another_ones_rules(monkeypatch, caplog):
    klines = [[1_700_000_000_000 + i * H2, "100", "100.5", "98", "99"] for i in range(3)]
    rec = _pending(strategy_key="retired_thing", strategy_version="0.1.0")
    result, calls = await _evaluate(monkeypatch, rec, klines)
    assert calls == [] and rec.win_status == "PENDING" and result["evaluated_count"] == 0
    assert "not registered" in caplog.text
    assert registry.resolver_for("retired_thing", "0.1.0") is None
    assert registry.resolver_for("macd_cross", "2.1.0").ref == registry.DEFAULT_REF


async def test_outcome_alerts_are_for_the_live_version_only(monkeypatch, shadow):
    class Research(ShadowDaily):
        version = "0.4.0"
        status = "research"

    strategy = registry.register(Research())
    try:
        klines = [[1_700_000_000_000 + i * H2, "100", "100.5", "98", "99"] for i in range(6)]
        rec = _pending(strategy_key=strategy.key, strategy_version=strategy.version)
        result, _ = await _evaluate(monkeypatch, rec, klines)
        assert rec.win_status != "PENDING" and result["closed"] == []
    finally:
        registry.unregister(strategy.key, strategy.version)


# --- backtest harness -----------------------------------------------------------------------


def test_harness_runs_a_context_strategy_without_look_ahead(shadow):
    report, trades = run_backtest(KLINES_DIR, ["BTCUSDT"], oos_start="2025-02-01", strategy=shadow)
    assert shadow.violations == 0
    assert report["strategy"]["key"] == shadow.key and report["strategy"]["context_source"] == "resample"
    assert report["params"] == {"timeframe": "2h", "hold_bars": 4}
    # One signal per UTC midnight close, all settled by the strategy's own 4-candle exit
    assert len(trades) > 150 and all(t.cross_candle_ms % DAY_MS == DAY_MS - H2 for t in trades)
    assert all(t.exit_time_ms <= t.entry_time_ms + 4 * H2 for t in trades)


def test_harness_reads_context_klines_from_csv(shadow):
    resampled, _ = run_backtest(KLINES_DIR, ["BTCUSDT"], strategy=shadow)
    from_csv, _ = run_backtest(KLINES_DIR, ["BTCUSDT"], strategy=shadow, context_source="csv")
    assert from_csv["dataset"]["context_files"]["BTCUSDT"]["1d"]["file"] == "BTCUSDT_1d.csv"
    # The exchange's daily candles and the resampled ones give the same trades
    assert from_csv["scenarios"] == resampled["scenarios"]


def test_cli_appends_one_trial_per_run(tmp_path, capsys):
    log = tmp_path / "trials.jsonl"
    args = ["--data", str(KLINES_DIR), "--symbols", "BTCUSDT", "--oos-start", "2025-02-01", "--trials-log", str(log)]
    assert backtest_main([*args, "--trial-ts", "2026-10-15T00:00:00Z", "--purpose", "gate"]) == 0
    assert backtest_main(args) == 0
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 2
    first = lines[0]
    assert first["ts"] == "2026-10-15T00:00:00Z" and first["purpose"] == "gate"
    assert (first["strategy_key"], first["version"]) == ("macd_cross", "2.2.0")
    assert first["params_hash"] == MACD.params_hash and first["code_hash"] == MACD.code_hash
    assert first["symbols"] == ["BTCUSDT"] and first["date_from"] == "2024-10-01"
    assert first["metrics"]["prod"]["n"] == 114 and "out_of_sample" in first["metrics"]["prod"]
    assert lines[1]["purpose"] == "research" and lines[1]["ts"] != first["ts"]


def test_cli_logs_to_the_environment_log_by_default(tmp_path, monkeypatch):
    log = tmp_path / "env-trials.jsonl"
    monkeypatch.setenv("RESEARCH_TRIALS_LOG", str(log))
    assert backtest_main(["--data", str(KLINES_DIR), "--symbols", "BTCUSDT"]) == 0
    assert len(log.read_text().splitlines()) == 1


def test_cli_rejects_unknown_strategies_and_trial_recording_without_a_database(monkeypatch, capsys):
    assert backtest_main(["--data", str(KLINES_DIR), "--strategy", "nope@1.0.0"]) == 2
    assert "not registered" in capsys.readouterr().err
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert backtest_main(["--data", str(KLINES_DIR), "--record-trial"]) == 2


def test_cli_requires_context_csv_when_asked(shadow, tmp_path):
    (tmp_path / "BTCUSDT_2h.csv").write_text((KLINES_DIR / "BTCUSDT_2h.csv").read_text())
    args = ["--data", str(tmp_path), "--strategy", shadow.ref, "--context-source", "csv"]
    assert backtest_main(args) == 2


def test_research_trial_row_describes_the_dataset():
    report, _ = run_backtest(KLINES_DIR, ["BTCUSDT"], start="2025-01-01", end="2025-02-01")
    entry = trial_entry(report, purpose="research", ts="2026-10-15T00:00:00Z")
    row = research_trial_row(entry)
    assert (row["strategy_key"], row["version"], row["purpose"]) == ("macd_cross", "2.2.0", "research")
    assert (row["date_from"], row["date_to"]) == ("2025-01-01", "2025-02-01")
    assert "BTCUSDT" in row["dataset_desc"] and "cm1" in dataset_description(entry)
    assert row["metrics"]["scenarios"]["prod"]["n"] == report["scenarios"]["prod"]["n"]
    with pytest.raises(ValueError):
        trial_entry(report, purpose="p-hacking")


# --- configuration --------------------------------------------------------------------------


async def test_info_lists_registered_strategies():
    from httpx import ASGITransport, AsyncClient

    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        data = (await client.get("/api/info")).json()
    [macd] = [s for s in data["strategies"] if s["ref"] == "macd_cross@2.2.0"]
    assert macd["status"] == "live" and macd["params_hash"] == MACD.params_hash
    assert macd["params"]["signal_expiry_candles"] == 48


def test_settings_drift_is_reported(monkeypatch):
    assert cron_module.settings_drift() == []
    monkeypatch.setattr(cron_module, "RISK_REWARD_RATIO", 2.0)
    [warning] = cron_module.settings_drift()
    assert "risk_reward_ratio" in warning and "frozen" in warning
    monkeypatch.setattr(cron_module, "STRATEGY_VERSION", "9.9.9")
    assert "not a registered live strategy" in cron_module.settings_drift()[0]
