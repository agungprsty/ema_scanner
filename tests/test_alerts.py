import pytest

from src.services import alerts
from src.strategy.macd import MacdSignal

pytestmark = pytest.mark.unit

CLOSED = [
    {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "result": "PROFIT",
        "entry_price": 112340.5,
        "exit_price": 114362.63,
        "pnl_percent": 1.70,
        "r_multiple": 1.42,
    },
    {
        "symbol": "SOL<b>",
        "direction": "SHORT",
        "result": "EXPIRED",
        "entry_price": 187.234,
        "exit_price": 186.0,
        "pnl_percent": 0.56,
        "r_multiple": None,
    },
]


def test_outcome_alert_lists_each_trade():
    text = alerts.format_outcome_alert(CLOSED)
    assert "<b>BTCUSDT Long</b> target hit" in text
    assert "Entry 112,340.50 → exit 114,362.63" in text
    assert "+1.42R, +1.70% after fees and modelled slippage" in text
    assert "<b>SOL&lt;b&gt; Short</b> expired" in text
    assert "+0.56% after fees and modelled slippage" in text


def _signal(bars_ago: int) -> MacdSignal:
    return MacdSignal(
        symbol="ETHUSDT", side="LONG", reason="macd_golden_cross", cross_candle_ms=1, cross_time="t",
        cross_price=1.0, entry_price=4120.11, stop_loss=4070.67, tp1=4194.27, risk_reward_ratio=1.5,
        macd=0.1, signal=0.05, histogram=0.05, close=1.0, bars_ago=bars_ago,
    )


def test_signal_alert_flags_catch_up_crosses_as_late():
    text = alerts.format_signal_alert(_signal(2))
    assert text.startswith("<b>LATE signal</b>: crossed 4h before the latest close.")
    assert "do not chase" in text
    assert "Not counted in the track record" in text
    assert "<b>LONG</b> #$ETHUSDT (2h)\nEntry: 4,120.11" in text


def test_signal_alert_on_time_has_no_warning():
    text = alerts.format_signal_alert(_signal(0))
    assert text.startswith("<b>LONG</b> #$ETHUSDT (2h)")
    assert "LATE" not in text
    assert "crossed" not in text


def test_signal_alert_late_by_clock_without_older_cross():
    text = alerts.format_signal_alert(_signal(0), late=True)
    assert text.startswith("<b>LATE signal</b>: found well after its entry candle opened.")
    assert "do not chase" in text
    assert "LATE" not in alerts.format_signal_alert(_signal(0), late=False)


@pytest.mark.asyncio
async def test_notify_sends_once_per_run(monkeypatch):
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(alerts, "send_alert", fake_send)
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", True)

    assert await alerts.notify_closed_trades([]) is False
    assert await alerts.notify_closed_trades(None) is False
    assert await alerts.notify_closed_trades(CLOSED) is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_notify_respects_setting(monkeypatch):
    async def fail_send(text):
        raise AssertionError("should not send")

    monkeypatch.setattr(alerts, "send_alert", fail_send)
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", False)
    assert await alerts.notify_closed_trades(CLOSED) is False
