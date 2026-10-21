import pytest

from src.strategy.outcome import Trade, resolve, trade_return

pytestmark = pytest.mark.unit

I = 60 * 60 * 1000  # noqa: E741
S = 1_700_000_000_000  # open time of the signal candle
F, H = 12, 24
FLAT = (100.6, 101.0, 100.5, 100.6)  # never reaches the LONG entry 100, target 102 or stop 99


def _mirror(side, p):
    """LONG prices around entry 100 mirror to SHORT ones: target 102 -> 98, stop 99 -> 101."""
    return p if side == "LONG" else 200 - p


def _candles(side, *ohlc):
    """Klines from (open, high, low, close) written for LONG, starting the candle after the signal."""
    out = []
    for i, (o, h, lo, c) in enumerate(ohlc):
        if side == "SHORT":
            o, h, lo, c = 200 - o, 200 - lo, 200 - h, 200 - c
        out.append([S + (i + 1) * I, str(o), str(h), str(lo), str(c)])
    return out


def _resolve(side, candles, closed):
    """Resolve with entry 100 once `closed` candles after the signal have closed."""
    tp, sl = _mirror(side, 102.0), _mirror(side, 99.0)
    return resolve(side, 100.0, tp, sl, candles, F, H, I, S + (closed + 1) * I)


def _assert_trade(side, trade, status, fill=None, fill_ms=None, exit_price=None, closed_ms=None):
    assert trade.status == status
    assert trade.fill_price == (None if fill is None else pytest.approx(_mirror(side, fill)))
    assert trade.fill_ms == fill_ms
    assert trade.exit_price == (None if exit_price is None else pytest.approx(_mirror(side, exit_price)))
    assert trade.closed_ms == closed_ms


sides = pytest.mark.parametrize("side", ["LONG", "SHORT"])


@sides
def test_no_touch_in_fill_window_is_not_filled(side):
    trade = _resolve(side, _candles(side, *[FLAT] * F), closed=F)
    _assert_trade(side, trade, "NOT_FILLED", closed_ms=S + 13 * I)


@sides
def test_pending_while_last_fill_candle_is_forming(side):
    candles = _candles(side, *[FLAT] * F)
    tp, sl = _mirror(side, 102.0), _mirror(side, 99.0)
    trade = resolve(side, 100.0, tp, sl, candles, F, H, I, S + 13 * I - 1)
    assert trade == Trade("PENDING")


@sides
def test_touch_after_fill_window_is_not_filled(side):
    candles = _candles(side, *[FLAT] * F, (100.5, 100.8, 99.5, 100.2))
    _assert_trade(side, _resolve(side, candles, closed=F + 1), "NOT_FILLED", closed_ms=S + 13 * I)


@sides
def test_signal_candle_is_never_the_fill(side):
    # Candles start after the signal candle, so an untouched first candle leaves the limit waiting
    assert _resolve(side, _candles(side, FLAT), closed=1).status == "PENDING"


@sides
def test_gap_through_entry_fills_at_open(side):
    candles = _candles(side, *[FLAT] * 3, (99.6, 100.2, 99.4, 99.9))
    _assert_trade(side, _resolve(side, candles, closed=4), "OPEN", fill=99.6, fill_ms=S + 4 * I)


@sides
def test_fill_candle_that_hits_stop(side):
    candles = _candles(side, (100.5, 100.8, 98.8, 99.2))
    _assert_trade(side, _resolve(side, candles, closed=1), "SL_HIT", 100.0, S + I, 99.0, S + I)


@sides
def test_fill_candle_target_ignored_when_open_was_before_entry(side):
    candles = _candles(side, (100.5, 102.5, 99.8, 101.0))
    _assert_trade(side, _resolve(side, candles, closed=1), "OPEN", fill=100.0, fill_ms=S + I)

    candles += _candles(side, (100.5, 102.5, 99.8, 101.0), (101.0, 102.2, 100.8, 102.0))[1:]
    _assert_trade(side, _resolve(side, candles, closed=2), "TP_HIT", 100.0, S + I, 102.0, S + 2 * I)


@sides
def test_fill_candle_target_counts_when_open_at_entry(side):
    candles = _candles(side, (100.0, 102.3, 99.5, 102.0))
    _assert_trade(side, _resolve(side, candles, closed=1), "TP_HIT", 100.0, S + I, 102.0, S + I)


@sides
def test_open_below_stop_fills_and_exits_at_open(side):
    candles = _candles(side, (98.5, 99.2, 98.2, 98.9))
    _assert_trade(side, _resolve(side, candles, closed=1), "SL_HIT", 98.5, S + I, 98.5, S + I)


@sides
def test_candle_touching_both_levels_counts_as_stop(side):
    candles = _candles(side, (100.5, 100.8, 99.8, 100.3), (100.3, 102.5, 98.5, 101.0))
    _assert_trade(side, _resolve(side, candles, closed=2), "SL_HIT", 100.0, S + I, 99.0, S + 2 * I)


@sides
def test_gap_through_target_exits_at_open(side):
    fill = (100.5, 100.8, 99.8, 100.3)
    candles = _candles(side, fill, (102.6, 103.0, 102.4, 102.8))
    _assert_trade(side, _resolve(side, candles, closed=2), "TP_HIT", 100.0, S + I, 102.6, S + 2 * I)

    # The open already took the target, so a later drop through the stop in that candle does not matter
    candles = _candles(side, fill, (102.6, 103.0, 98.0, 99.0))
    _assert_trade(side, _resolve(side, candles, closed=2), "TP_HIT", 100.0, S + I, 102.6, S + 2 * I)


@sides
def test_no_hit_within_max_hold_expires_at_close(side):
    hold = (100.6, 101.0, 99.5, 100.7)
    candles = _candles(side, (100.5, 100.8, 99.8, 100.3), *[hold] * (H - 2), (100.6, 101.0, 99.5, 100.9), FLAT)
    _assert_trade(side, _resolve(side, candles, closed=H + 1), "EXPIRED", 100.0, S + I, 100.9, S + (H + 1) * I)


@sides
def test_open_until_max_hold_has_passed(side):
    candles = _candles(side, (100.5, 100.8, 99.8, 100.3), *[(100.6, 101.0, 99.5, 100.7)] * (H - 1))
    _assert_trade(side, _resolve(side, candles, closed=H - 1), "OPEN", fill=100.0, fill_ms=S + I)


@sides
def test_replay_is_idempotent(side):
    candles = _candles(side, *[FLAT] * 2, (100.5, 100.8, 99.8, 100.3), (100.3, 102.5, 100.1, 101.0))
    first = _resolve(side, candles, closed=4)
    assert first == _resolve(side, candles, closed=4)
    # Later candles and a still-forming one never change a closed trade
    later = candles + _candles(side, *[FLAT] * 4, (98.0, 99.0, 97.0, 98.0))[4:]
    assert _resolve(side, later, closed=4) == first == _resolve(side, later, closed=5)
    assert first.status == "TP_HIT"


def test_trade_return_against_planned_risk():
    assert trade_return("LONG", 100.0, 102.0, 1.0, 0.1) == (pytest.approx(1.9), pytest.approx(1.9))
    assert trade_return("LONG", 100.0, 99.0, 1.0, 0.1) == (pytest.approx(-1.1), pytest.approx(-1.1))
    assert trade_return("SHORT", 100.0, 98.0, 1.0, 0.1) == (pytest.approx(1.9), pytest.approx(1.9))
    # Gapped below the stop: filled and stopped at the same open, only fees are lost
    assert trade_return("LONG", 98.5, 98.5, 1.0, 0.1) == (pytest.approx(-0.1), pytest.approx(-0.1))


def test_trade_return_without_risk_has_no_r():
    assert trade_return("LONG", 100.0, 101.0, 0.0, 0.0) == (pytest.approx(1.0), None)
    assert trade_return("LONG", 0.0, 101.0, 1.0, 0.1) == (0.0, None)
