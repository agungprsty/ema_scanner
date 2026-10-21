from datetime import UTC, datetime

import pytest

from src.services import alerts, db

pytestmark = pytest.mark.unit

HOUR = 3_600_000
# 06:00 WIB 21 Oct, the signal candle of the 1h example
S = int(datetime(2026, 10, 20, 23, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture(autouse=True)
def strategy_settings(monkeypatch):
    monkeypatch.setattr(db, "FILL_WINDOW_HOURS", 12.0)
    monkeypatch.setattr(db, "MAX_HOLD_HOURS", 24.0)
    monkeypatch.setattr(alerts, "RSI_OVERSOLD", 28.0)
    monkeypatch.setattr(alerts, "RSI_OVERBOUGHT", 72.0)


def _row(**overrides) -> dict:
    values = {
        "id": 1,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "signal_candle_ms": S,
        "rsi": 27.4,
        "prev_rsi": 30.1,
        "candle_close": 60_300.0,
        "entry_price": 60_000.0,
        "take_profit": 61_200.0,
        "stop_loss": 59_400.0,
        "status": "PENDING",
    }
    values.update(overrides)
    return db.SignalModel(**values).to_dict()


def _closed(status, exit_price, pnl, r, **overrides) -> dict:
    return _row(
        status=status,
        fill_price=60_000.0,
        fill_ms=S + 2 * HOUR,
        exit_price=exit_price,
        closed_ms=S + 5 * HOUR,
        pnl_percent=pnl,
        r_multiple=r,
        **overrides,
    )


def test_pending_signal_asks_for_the_limit_order():
    text = alerts.format_signals([_row()], now_ms=S + HOUR + 15_000)
    assert text.startswith("<b>Crossline: RSI 1h signals</b>\n\n")
    assert "<b>LONG</b> #$BTCUSDT (1h) RSI 27.4, crossed below 28\n" in text
    assert "Limit entry: 60,000.00 (signal candle low)" in text
    assert "TP 61,200.00 (+2%) · SL 59,400.00 (-1%) · R:R 1:2" in text
    assert "Fill by 19:00 WIB 21 Oct (12h window), else cancelled" in text
    assert "before the latest close" not in text


def test_short_signal_uses_the_candle_high():
    row = _row(side="SHORT", rsi=73.2, entry_price=3000.0, take_profit=2940.0, stop_loss=3030.0)
    text = alerts.format_signal(row, now_ms=S + 2 * HOUR)
    assert "RSI 73.2, crossed above 72" in text
    assert "Limit entry: 3,000.00 (signal candle high)" in text
    assert "TP 2,940.00 (+2%) · SL 3,030.00 (-1%) · R:R 1:2" in text


@pytest.mark.parametrize(
    "timeframe, interval, candles_later, age",
    [("1h", HOUR, 3, "2h"), ("15m", 900_000, 4, "45m"), ("1d", 86_400_000, 2, "1d")],
)
def test_signal_age_follows_the_timeframe(timeframe, interval, candles_later, age):
    row = _row(timeframe=timeframe)
    text = alerts.format_signal(row, now_ms=S + candles_later * interval + 15_000)
    assert f"({timeframe}) RSI 27.4, crossed below 28 · signal {age} before the latest close" in text


@pytest.mark.parametrize("timeframe, window", [("15m", "12h"), ("8h", "16h"), ("1d", "1d")])
def test_fill_window_label_is_the_rounded_up_candle_window(timeframe, window):
    assert f"({window} window), else cancelled" in alerts.format_signal(_row(timeframe=timeframe), S)


def test_late_signal_reports_its_current_state():
    opened = _row(status="OPEN", fill_price=59_990.0, fill_ms=S + 2 * HOUR)
    assert "Already filled at 59,990.00 (08:00 WIB 21 Oct), trade open" in alerts.format_signal(opened, S)

    closed = _closed("TP_HIT", 61_200.0, 1.9, 1.9)
    text = alerts.format_signal(closed, S)
    assert "Already closed: target hit 60,000.00 → 61,200.00, +1.90R, +1.90% after fees" in text
    assert "Fill by" not in text

    assert "Not filled within the window" in alerts.format_signal(_row(status="NOT_FILLED"), S)


def test_transitions_list_each_trade():
    text = alerts.format_transitions(
        [
            _closed("TP_HIT", 61_200.0, 1.9, 1.9),
            _closed("SL_HIT", 59_400.0, -1.1, -1.1),
            _closed("EXPIRED", 60_300.0, 0.4, 0.4),
            _row(id=2, symbol="ETH<b>", side="SHORT", status="NOT_FILLED", entry_price=3000.0,
                 take_profit=2940.0, stop_loss=3030.0),
        ]
    )
    assert text.split("\n") == [
        "<b>Crossline: trade updates</b>",
        "BTCUSDT Long target hit 60,000.00 → 61,200.00, +1.90R, +1.90% after fees",
        "BTCUSDT Long stopped out 60,000.00 → 59,400.00, -1.10R, -1.10% after fees",
        "BTCUSDT Long max hold reached 60,000.00 → 60,300.00, +0.40R, +0.40% after fees",
        "ETH&lt;b&gt; Short not filled, cancel the limit at 3,000.00",
    ]


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def fake_send(text):
        messages.append(text)
        return True

    monkeypatch.setattr(alerts, "send_alert", fake_send)
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", True)
    return messages


async def test_notify_run_sends_signals_then_updates(sent):
    new = [_row(id=1), _row(id=2, symbol="SOLUSDT")]
    transitions = [
        _closed("TP_HIT", 61_200.0, 1.9, 1.9, id=2, symbol="SOLUSDT"),  # new and already closed
        _closed("SL_HIT", 59_400.0, -1.1, -1.1, id=3, symbol="XRPUSDT"),
        _row(id=4, symbol="ADAUSDT", status="OPEN", fill_price=60_000.0, fill_ms=S + 2 * HOUR),
    ]

    assert await alerts.notify_run(new, transitions, now_ms=S + 2 * HOUR) is True

    assert len(sent) == 2
    assert "#$SOLUSDT (1h)" in sent[0] and "Already closed: target hit" in sent[0]
    assert "#$BTCUSDT (1h)" in sent[0] and "Fill by" in sent[0]
    assert "XRPUSDT Long stopped out" in sent[1]
    assert "SOLUSDT" not in sent[1] and "ADAUSDT" not in sent[1]


async def test_notify_run_without_anything_new_sends_nothing(sent):
    opened = _row(id=4, status="OPEN", fill_price=60_000.0, fill_ms=S + 2 * HOUR)
    assert await alerts.notify_run([], [opened], now_ms=S) is True
    assert sent == []


async def test_notify_run_respects_outcome_setting(sent, monkeypatch):
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", False)
    await alerts.notify_run([_row(id=1)], [_closed("SL_HIT", 59_400.0, -1.1, -1.1, id=3)], now_ms=S)
    assert len(sent) == 1 and "Crossline: RSI 1h signals" in sent[0]


async def test_notify_run_survives_send_errors(monkeypatch):
    async def broken_send(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(alerts, "send_alert", broken_send)
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", True)
    delivered = await alerts.notify_run([_row(id=1)], [_closed("SL_HIT", 59_400.0, -1.1, -1.1, id=3)], now_ms=S)
    assert delivered is False


async def test_notify_run_reports_an_undelivered_signal_message(monkeypatch):
    async def refused_send(text):
        return False

    monkeypatch.setattr(alerts, "send_alert", refused_send)
    assert await alerts.notify_run([_row(id=1)], [], now_ms=S) is False
