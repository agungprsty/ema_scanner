import importlib

import pytest

from src.config import settings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("SUPERTREND_ATR_PERIOD", "0", "SUPERTREND_ATR_PERIOD"),
        ("EMA_PERIOD", "0", "EMA_PERIOD"),
        ("RSI_PERIOD", "0", "RSI_PERIOD"),
        ("SUPERTREND_FACTOR", "0", "SUPERTREND_FACTOR"),
        ("RSI_LONG_MAX", "40", "RSI levels"),
        ("RSI_LONG_MAX", "101", "RSI levels"),
        ("RSI_SHORT_MIN", "50", "RSI levels"),
        ("RSI_SHORT_MIN", "-1", "RSI levels"),
    ],
)
def test_invalid_strategy_settings_are_rejected_at_import(monkeypatch, name, value, match):
    with monkeypatch.context() as m:
        m.setenv(name, value)
        with pytest.raises(ValueError, match=match):
            importlib.reload(settings)
    importlib.reload(settings)
    assert settings.RSI_SHORT_MIN < settings.RSI_MIDLINE < settings.RSI_LONG_MAX
