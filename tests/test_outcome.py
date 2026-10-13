import pytest

from src.strategy.outcome import resolve_outcome, trade_return

pytestmark = pytest.mark.unit

H2 = 2 * 60 * 60 * 1000
T0 = 1_700_000_000_000


def _candles(*ranges):
    """Build klines from (high, low, close) tuples, one 2h candle apart."""
    return [[T0 + i * H2, "100", str(h), str(lo), str(c)] for i, (h, lo, c) in enumerate(ranges)]


def test_long_hits_target():
    out = resolve_outcome("LONG", 95.0, 107.5, _candles((101, 99, 100), (108, 100, 107)), 48, T0 + 10 * H2, H2)
    assert (out.result, out.win_status, out.exit_price, out.exit_time_ms) == ("PROFIT", "TP1_HIT", 107.5, T0 + H2)


def test_short_hits_stop():
    out = resolve_outcome("SHORT", 105.0, 92.5, _candles((106, 99, 104)), 48, T0 + 10 * H2, H2)
    assert (out.result, out.exit_price) == ("LOSS", 105.0)


def test_candle_touching_both_levels_counts_as_stop():
    out = resolve_outcome("LONG", 95.0, 107.5, _candles((110, 90, 100)), 48, T0 + 10 * H2, H2)
    assert out.result == "LOSS"


def test_no_hit_within_expiry_closes_at_last_close():
    candles = _candles((101, 99, 100), (102, 98, 101), (103, 97, 102.5), (120, 80, 100))
    out = resolve_outcome("LONG", 95.0, 107.5, candles, 3, T0 + 10 * H2, H2)
    assert (out.result, out.win_status, out.exit_price, out.exit_time_ms) == ("EXPIRED", "EXPIRED", 102.5, T0 + 3 * H2)


def test_expiry_waits_for_the_last_candle_to_close():
    candles = _candles((101, 99, 100), (102, 98, 101), (103, 97, 102.5))
    assert resolve_outcome("LONG", 95.0, 107.5, candles, 3, T0 + 3 * H2 - 1, H2) is None


def test_pending_while_window_incomplete():
    assert resolve_outcome("LONG", 95.0, 107.5, _candles((101, 99, 100)), 48, T0 + 10 * H2, H2) is None


def test_trade_return_subtracts_fees_and_scales_by_risk():
    pnl, r = trade_return("LONG", 100.0, 95.0, 107.5, 0.1)
    assert pnl == pytest.approx(7.4)
    assert r == pytest.approx(7.4 / 5.0)
    pnl, r = trade_return("SHORT", 100.0, 105.0, 105.0, 0.1)
    assert pnl == pytest.approx(-5.1)
    assert r == pytest.approx(-1.02)


def test_trade_return_without_risk_has_no_r():
    assert trade_return("LONG", 100.0, 100.0, 101.0, 0.0) == (pytest.approx(1.0), None)
    assert trade_return("LONG", 0.0, 95.0, 101.0, 0.1) == (0.0, None)
