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
        "take_profit": 61_260.0,
        "stop_loss": 59_460.0,
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
    assert alerts.format_signals([_row()]).split("\n") == [
        "<b>Simple Trade · 1h</b>",
        "",
        "🟢 <b>LONG BTCUSDT</b>",
        "Entry <code>60000.0</code>",
        "TP <code>61260.0</code> +2%",
        "SL <code>59460.0</code> -1%",
        "⏳ s/d 19:00 WIB 21 Oct",
    ]


def test_short_signal():
    row = _row(side="SHORT", entry_price=3000.25, take_profit=2937.24475, stop_loss=3027.25225)
    text = alerts.format_signal(row)
    assert text.startswith("🔴 <b>SHORT BTCUSDT</b>\n")
    assert "TP <code>2937.24</code> +2%\nSL <code>3027.25</code> -1%" in text


@pytest.mark.parametrize(
    "entry, level, copied",
    [(84673.4, 82895.26148, "82895.3"), (2402.67, 2453.12607, "2453.13"), (0.00001234, 1.259914e-05, "0.00001260")],
)
def test_levels_are_copied_with_the_entry_tick_decimals(entry, level, copied):
    text = alerts.format_signal(_row(entry_price=entry, take_profit=level, stop_loss=entry * 0.991))
    assert f"TP <code>{copied}</code>" in text


def test_late_signal_reports_its_current_state():
    opened = _row(status="OPEN", fill_price=59_990.0, fill_ms=S + 2 * HOUR)
    assert alerts.format_signal(opened).endswith("\n✅ Filled at <code>59990.0</code>, trade open")

    text = alerts.format_signal(_closed("TP_HIT", 61_260.0, 2.0, 2.0))
    assert text.endswith("\n🎯 +2R (+2.00%)")
    assert "s/d" not in text

    assert alerts.format_signal(_row(status="NOT_FILLED")).endswith("\n⚪ Not filled, no trade")


def test_transitions_list_each_trade():
    text = alerts.format_transitions(
        [
            _closed("TP_HIT", 61_260.0, 2.0, 2.0),
            _closed("SL_HIT", 59_460.0, -1.0, -1.0),
            _closed("EXPIRED", 60_300.0, 0.4, None),
            _row(id=2, symbol="ETH<b>", side="SHORT", status="NOT_FILLED", entry_price=3000.0,
                 take_profit=2937.0, stop_loss=3027.0),
        ]
    )
    assert text.split("\n") == [
        "<b>Simple Trade</b>",
        "",
        "🎯 BTCUSDT LONG +2R (+2.00%)",
        "🛑 BTCUSDT LONG -1R (-1.00%)",
        "⏱ BTCUSDT LONG +0.40%",
        "⚪ ETH&lt;b&gt; SHORT not filled, cancel the limit at <code>3000.0</code>",
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

    assert await alerts.notify_run(new, transitions) is True

    assert len(sent) == 2
    assert "<b>LONG SOLUSDT</b>" in sent[0] and "🎯 +1.9R" in sent[0]
    assert "<b>LONG BTCUSDT</b>" in sent[0] and "⏳ s/d" in sent[0]
    assert "🛑 XRPUSDT LONG -1.1R" in sent[1]
    assert "SOLUSDT" not in sent[1] and "ADAUSDT" not in sent[1]


async def test_notify_run_without_anything_new_sends_nothing(sent):
    opened = _row(id=4, status="OPEN", fill_price=60_000.0, fill_ms=S + 2 * HOUR)
    assert await alerts.notify_run([], [opened]) is True
    assert sent == []


async def test_notify_run_respects_outcome_setting(sent, monkeypatch):
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", False)
    await alerts.notify_run([_row(id=1)], [_closed("SL_HIT", 59_400.0, -1.1, -1.1, id=3)])
    assert len(sent) == 1 and "<b>Simple Trade · 1h</b>" in sent[0]


async def test_notify_run_survives_send_errors(monkeypatch):
    async def broken_send(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(alerts, "send_alert", broken_send)
    monkeypatch.setattr(alerts, "TELEGRAM_OUTCOME_ALERTS", True)
    delivered = await alerts.notify_run([_row(id=1)], [_closed("SL_HIT", 59_400.0, -1.1, -1.1, id=3)])
    assert delivered is False


async def test_notify_run_reports_an_undelivered_signal_message(monkeypatch):
    async def refused_send(text):
        return False

    monkeypatch.setattr(alerts, "send_alert", refused_send)
    assert await alerts.notify_run([_row(id=1)], []) is False
