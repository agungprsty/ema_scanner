import pytest

from src.strategy.outcome import cost_adjusted_return, resolve_outcome, trade_return

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


def test_cost_model_long_target_fills_at_limit():
    # Entry 100 slips 10 bps to 100.1, the target fills at its limit 107.5, fee 0.1%
    c = cost_adjusted_return("LONG", 100.0, 95.0, 107.5, "PROFIT", 0.1, 10, 1.5)
    assert c.gross_r == pytest.approx(1.5)
    assert c.pnl_percent_gross == pytest.approx(7.5)
    assert c.pnl_percent_net == pytest.approx(7.3)
    assert c.cost_r == pytest.approx(0.04)
    assert c.net_r == pytest.approx(1.46)


def test_cost_model_long_stop_slips_more_than_entry():
    # Stop 95 fills 15 bps worse at 94.8575; net -5.2425% - 0.1% fee = -5.3425% on 5% risk
    c = cost_adjusted_return("LONG", 100.0, 95.0, 95.0, "LOSS", 0.1, 10, 1.5)
    assert c.gross_r == pytest.approx(-1.0)
    assert c.pnl_percent_net == pytest.approx(-5.3425)
    assert c.cost_r == pytest.approx(0.0685)
    assert c.net_r == pytest.approx(-1.0685)


def test_cost_model_short_expiry_slips_once():
    # Short entry 100 sells at 99.9, expiry close 98 buys back at 98.098; risk 5%
    c = cost_adjusted_return("SHORT", 100.0, 105.0, 98.0, "EXPIRED", 0.1, 10, 1.5)
    assert c.gross_r == pytest.approx(0.4)
    assert c.pnl_percent_net == pytest.approx(1.702)
    assert c.cost_r == pytest.approx(0.0596)
    assert c.net_r == pytest.approx(0.3404)


def test_cost_model_short_stop_and_target():
    # Short stop 105 buys back 15 bps higher at 105.1575: -5.1575% - 0.1% (entry slip 0.1%)
    loss = cost_adjusted_return("SHORT", 100.0, 105.0, 105.0, "LOSS", 0.1, 10, 1.5)
    assert loss.gross_r == pytest.approx(-1.0)
    assert loss.pnl_percent_net == pytest.approx(-5.3575)
    assert loss.net_r == pytest.approx(-1.0715)
    # Short target 92.5 at its limit, 2 bps entry slip: 99.98 - 92.5 = 7.48%, less the fee
    win = cost_adjusted_return("SHORT", 100.0, 105.0, 92.5, "PROFIT", 0.1, 2, 1.5)
    assert win.gross_r == pytest.approx(1.5)
    assert win.pnl_percent_net == pytest.approx(7.38)
    assert win.cost_r == pytest.approx(0.024)
    assert win.net_r == pytest.approx(win.gross_r - win.cost_r)


def test_cost_model_without_costs_matches_gross():
    c = cost_adjusted_return("LONG", 100.0, 95.0, 95.0, "LOSS", 0.0, 0, 1.5)
    assert c.cost_r == pytest.approx(0.0)
    assert c.net_r == c.gross_r == pytest.approx(-1.0)


def test_cost_model_without_risk_or_entry_has_no_r():
    flat = cost_adjusted_return("LONG", 100.0, 100.0, 101.0, "PROFIT", 0.1, 10, 1.5)
    assert (flat.gross_r, flat.cost_r, flat.net_r) == (None, None, None)
    assert flat.pnl_percent_gross == pytest.approx(1.0)
    assert cost_adjusted_return("LONG", 0.0, 95.0, 101.0, "PROFIT", 0.1, 10, 1.5).net_r is None


def test_slippage_map_defaults_and_env_override(monkeypatch):
    from src.config import settings

    monkeypatch.delenv("SLIPPAGE_BPS_MAP", raising=False)
    assert settings._load_slippage_map() == settings.CM1_SLIPPAGE_BPS_MAP
    assert settings._load_slippage_map() == {
        "BTCUSDT": 2.0, "ETHUSDT": 2.0, "SOLUSDT": 8.0, "XRPUSDT": 10.0, "ZECUSDT": 20.0, "NEARUSDT": 25.0,
    }
    monkeypatch.setenv("SLIPPAGE_BPS_MAP", '{"btcusdt": 3, "NEARUSDT": 25}')
    assert settings._load_slippage_map() == {"BTCUSDT": 3.0, "NEARUSDT": 25.0}
    # A broken map falls back to the conservative default for every symbol
    monkeypatch.setenv("SLIPPAGE_BPS_MAP", "[1, 2]")
    assert settings._load_slippage_map() == {}


def test_slippage_for_symbol_falls_back_to_default(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "SLIPPAGE_BPS_BY_SYMBOL", {"BTCUSDT": 2.0})
    monkeypatch.setattr(settings, "SLIPPAGE_BPS_DEFAULT", 10.0)
    assert settings.slippage_bps_for("btcusdt") == 2.0
    assert settings.slippage_bps_for("ZECUSDT") == 10.0


def test_cost_model_version_names_the_parameters():
    from src.config import settings

    cm1 = (
        settings.CM1_FEE_PERCENT,
        settings.CM1_SLIPPAGE_BPS_DEFAULT,
        dict(settings.CM1_SLIPPAGE_BPS_MAP),
        settings.CM1_STOP_SLIPPAGE_MULTIPLIER,
    )
    assert settings.cost_model_version(*cm1) == "cm1"
    # Map order and int vs float do not matter
    reordered = {sym: int(bps) for sym, bps in reversed(list(settings.CM1_SLIPPAGE_BPS_MAP.items()))}
    assert settings.cost_model_version(0.1, 10, reordered, 1.5) == "cm1"

    # Any changed parameter gets its own stable label that fits the 10-character column
    variants = {
        settings.cost_model_version(0.08, *cm1[1:]),
        settings.cost_model_version(cm1[0], 20.0, *cm1[2:]),
        settings.cost_model_version(*cm1[:2], {"BTCUSDT": 2.0}, cm1[3]),
        settings.cost_model_version(*cm1[:3], 1.0),
    }
    assert len(variants) == 4
    for label in variants:
        assert label.startswith("cmx-") and len(label) <= 10
    assert settings.cost_model_version(*cm1[:3], 1.0) == settings.cost_model_version(*cm1[:3], 1.0)
    # The defaults this suite runs with are cm1
    assert settings.COST_MODEL_VERSION == "cm1"
