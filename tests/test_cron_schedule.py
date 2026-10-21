import importlib
from datetime import datetime

import pytest

from src.config import settings
from src.config.settings import INTERVAL_MS
from src.services.cron_scheduler import WIB_TZ, next_run_ms

pytestmark = pytest.mark.unit

DELAY = 15_000


def _wib_ms(*args) -> int:
    return int(datetime(*args, tzinfo=WIB_TZ).timestamp() * 1000)


@pytest.mark.parametrize(
    ("tf", "now", "expected"),
    [
        ("15m", (2026, 10, 21, 10, 7), (2026, 10, 21, 10, 15, 15)),
        ("1h", (2026, 10, 21, 10, 7), (2026, 10, 21, 11, 0, 15)),
        ("4h", (2026, 10, 21, 0, 30), (2026, 10, 21, 3, 0, 15)),
        ("1d", (2026, 10, 21, 8, 0), (2026, 10, 22, 7, 0, 15)),
    ],
)
def test_next_run_is_the_next_candle_close_plus_delay(tf, now, expected):
    assert next_run_ms(_wib_ms(*now), INTERVAL_MS[tf], DELAY) == _wib_ms(*expected)


def test_default_30m_timeframe_runs_on_the_hour_and_half_hour_wib():
    # The default schedule: 01:00, 01:30, 02:00, ... WIB, each plus the safety delay
    half_hour = INTERVAL_MS["30m"]
    runs, last = [], _wib_ms(2026, 10, 21, 0, 50)
    for _ in range(4):
        last = next_run_ms(last, half_hour, DELAY)
        runs.append(datetime.fromtimestamp(last / 1000, WIB_TZ).strftime("%H:%M:%S"))
    assert runs == ["01:00:15", "01:30:15", "02:00:15", "02:30:15"]


def test_start_inside_the_delay_still_runs_that_boundary():
    boundary = _wib_ms(2026, 10, 21, 11, 0)
    hour = INTERVAL_MS["1h"]
    assert next_run_ms(boundary, hour, DELAY) == boundary + DELAY
    assert next_run_ms(boundary + DELAY - 1, hour, DELAY) == boundary + DELAY


def test_early_wake_does_not_rerun_the_boundary_just_run():
    boundary = _wib_ms(2026, 10, 21, 11, 0)
    hour = INTERVAL_MS["1h"]
    last = boundary + DELAY
    early_now = last - 5
    assert next_run_ms(max(early_now, last), hour, DELAY) == boundary + hour + DELAY


def test_overrun_jumps_to_the_next_boundary_ahead():
    boundary = _wib_ms(2026, 10, 21, 11, 0)
    minute = INTERVAL_MS["1m"]
    last = boundary + DELAY
    now = boundary + 3 * minute + 30_000
    assert next_run_ms(max(now, last), minute, DELAY) == boundary + 4 * minute + DELAY


def test_unsupported_timeframe_is_rejected_at_import(monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("TIMEFRAME", "1w")
        with pytest.raises(ValueError, match="1w"):
            importlib.reload(settings)
    importlib.reload(settings)
    assert settings.TIMEFRAME in INTERVAL_MS
