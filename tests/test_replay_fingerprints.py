"""Trade-list fingerprints of every locked strategy version on the committed fixture.

A code_hash moves with any edit to a strategy's modules, and `python -m src.strategy rehash` lets a
refactor keep its frozen version. This test is the proof a rehash needs: each locked version must
replay exactly the trades it did when it was frozen (the live version is also held to the CI backtest
gate). The shadow version replays with a 60-day trend window because the fixture covers 167 days;
the code it runs is the frozen code. A change here is a behaviour change: give the strategy a new
version instead of updating the fingerprint.
"""

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from src.backtest.data import load_klines_csv, resample_klines
from src.backtest.replay import replay_strategy
from src.strategy import registry
from src.strategy.macd_cross_ema50d_oppexit import MacdCrossEma50dOppExit, MacdCrossEma50dOppExitParams

FIXTURE = Path(__file__).parent / "fixtures" / "klines" / "BTCUSDT_2h.csv"

# ref -> (trades, sha256 of the trades as canonical JSON)
FINGERPRINTS = {
    "macd_cross@2.2.0": (114, "223f4216541737cc8e31f8bc5836cf7fbc67ea18b0fb227ce09e84b33aafb40b"),
    "macd_cross_ema50d_oppexit@1.0.0": (44, "6e940045e02366aa0a4ac12f9c2a338d3d7b29936bd4774c02a08777371d8baa"),
}


@pytest.fixture(scope="module")
def btc():
    return load_klines_csv(FIXTURE)


def _fingerprint(replay) -> tuple[int, str]:
    rows = [dataclasses.asdict(t) for t in replay.trades]
    return len(rows), hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _replay(ref: str, btc):
    if ref == "macd_cross_ema50d_oppexit@1.0.0":
        strategy = MacdCrossEma50dOppExit(MacdCrossEma50dOppExitParams(trend_ema_bars=60))
        return replay_strategy(strategy, btc, "BTCUSDT", context={"1d": resample_klines(btc, "2h", "1d")})
    return replay_strategy(registry.get(ref), btc, "BTCUSDT")


def test_every_locked_version_has_a_fingerprint():
    assert set(registry.load_lock()) == set(FINGERPRINTS)


@pytest.mark.parametrize("ref", sorted(FINGERPRINTS))
def test_locked_versions_replay_the_trades_they_were_frozen_with(ref, btc):
    assert _fingerprint(_replay(ref, btc)) == FINGERPRINTS[ref], f"{ref} now trades differently"
