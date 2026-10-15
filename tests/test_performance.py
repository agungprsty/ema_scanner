from datetime import UTC, datetime

import pytest

from src.services import performance as perf
from src.services.performance import ClosedTrade

pytestmark = pytest.mark.unit

# The six v2.2 trades closed in production (ids 5-10), as booked: 1 target, 5 stops, -3.88R
V22_R = [-1.06, -1.05, -1.03, -1.11, 1.44, -1.07]


def _trade(id_, result, net_r, exit_time="2026-10-14T12:00:00Z", gross_r=None, cost_r=0.05):
    gross = gross_r if gross_r is not None else net_r + cost_r
    return ClosedTrade(id=id_, result=result, net_r=net_r, gross_r=gross, cost_r=cost_r, exit_time=exit_time)


@pytest.mark.parametrize(
    ("df", "expected"),
    [(1, 12.7062), (2, 4.3027), (5, 2.5706), (10, 2.2281), (29, 2.0452), (100, 1.9840), (1000, 1.9623)],
)
def test_student_t_quantile_matches_tables(df, expected):
    assert perf.student_t_quantile(0.975, df) == pytest.approx(expected, abs=1e-4)


def test_student_t_quantile_is_symmetric_and_rejects_bad_p():
    assert perf.student_t_quantile(0.025, 5) == pytest.approx(-2.5706, abs=1e-4)
    assert perf.student_t_quantile(0.5, 7) == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(ValueError):
        perf.student_t_quantile(1.0, 5)


def test_t_interval_reproduces_the_review_figure_for_v22():
    lo, hi = perf.t_interval(V22_R)
    assert (round(lo, 2), round(hi, 2)) == (-1.72, 0.43)


def test_t_interval_needs_two_values():
    assert perf.t_interval([]) is None
    assert perf.t_interval([1.5]) is None
    assert perf.t_interval([1.0, 1.0]) == (1.0, 1.0)


def test_wilson_interval_known_values():
    lo, hi = perf.wilson_interval(1, 6)
    assert lo == pytest.approx(0.0301, abs=1e-4)
    assert hi == pytest.approx(0.5635, abs=1e-4)
    lo, hi = perf.wilson_interval(4, 9)
    assert lo == pytest.approx(0.1888, abs=1e-4)
    assert hi == pytest.approx(0.7333, abs=1e-4)


def test_wilson_interval_edges():
    assert perf.wilson_interval(0, 0) is None
    lo, hi = perf.wilson_interval(0, 10)
    assert lo == 0.0 and 0 < hi < 0.35
    lo, hi = perf.wilson_interval(10, 10)
    assert hi == pytest.approx(1.0) and 0.65 < lo < 1


def test_profit_factor():
    assert perf.profit_factor([1.5, -1.0, -1.0]) == pytest.approx(0.75)
    assert perf.profit_factor(V22_R) == pytest.approx(1.44 / 5.32)
    # Nothing lost: the ratio is unbounded, so it is not reported
    assert perf.profit_factor([1.5, 0.5]) is None
    assert perf.profit_factor([]) is None


def test_max_drawdown_measures_peak_to_trough():
    assert perf.max_drawdown([]) == 0.0
    assert perf.max_drawdown([1.5, -1.0, -1.0, 1.5, -1.0]) == pytest.approx(2.0)
    assert perf.max_drawdown([-1.0, -1.0]) == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-13T00:00:00Z", datetime(2026, 10, 13, tzinfo=UTC)),
        ("2026-10-13 00:00:00", datetime(2026, 10, 13, tzinfo=UTC)),
        ("2026-10-13T07:00:00+07:00", datetime(2026, 10, 13, tzinfo=UTC)),
        ("t", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_utc_reads_both_stored_formats(value, expected):
    assert perf.parse_utc(value) == expected


def test_chronological_orders_by_real_time_not_text():
    # As text, "2026-10-13 14:00:00" sorts before "2026-10-13T12:00:00Z" because " " < "T"
    late_legacy = _trade(1, "LOSS", -1.0, "2026-10-13 14:00:00")
    early_iso = _trade(2, "PROFIT", 1.5, "2026-10-13T12:00:00Z")
    unreadable = _trade(3, "LOSS", -1.0, "t")
    assert [t.id for t in perf.chronological([unreadable, late_legacy, early_iso])] == [2, 1, 3]


def test_chronological_breaks_ties_by_id():
    trades = [_trade(9, "PROFIT", 1.44, "2026-10-14T14:00:00Z"), _trade(7, "LOSS", -1.06, "2026-10-14T14:00:00Z")]
    assert [t.id for t in perf.chronological(trades)] == [7, 9]


def test_drawdown_uses_exit_order_rather_than_text_order():
    # As text the legacy-format 18:00 loss sorts first; by time the win at 10:00 comes first
    trades = [
        _trade(1, "LOSS", -1.0, "2026-10-13 18:00:00"),
        _trade(2, "PROFIT", 1.5, "2026-10-13T10:00:00Z"),
        _trade(3, "LOSS", -1.0, "2026-10-13T12:00:00Z"),
    ]
    text_order = sorted(trades, key=lambda t: t.exit_time)
    assert perf.max_drawdown(t.net_r for t in text_order) == pytest.approx(1.0)
    assert perf.summarize_trades(trades, 1.5)["max_drawdown_r"] == pytest.approx(2.0)


def test_break_even_win_rate():
    assert perf.break_even_win_rate(1.5, 0.0) == pytest.approx(0.40)
    assert perf.break_even_win_rate(1.5, 0.05) == pytest.approx(0.42)
    assert perf.break_even_win_rate(1.5, None) is None


def test_summarize_v22_production_trades():
    exits = ["2026-10-14T14:00:00Z", "2026-10-14T12:00:00Z", "2026-10-14T14:00:00Z",
             "2026-10-14T10:00:00Z", "2026-10-14T14:00:00Z", "2026-10-14T12:00:00Z"]
    ids = [7, 5, 8, 4, 9, 6]
    trades = [
        _trade(i, "PROFIT" if r > 0 else "LOSS", r, exit_time=e) for i, r, e in zip(ids, V22_R, exits, strict=True)
    ]
    out = perf.summarize_trades(trades, 1.5)
    assert (out["n_closed"], out["wins"], out["losses"], out["expired"]) == (6, 1, 5, 0)
    assert out["win_rate"] == 16.7
    assert out["win_rate_ci95"] == [3.0, 56.4]
    assert out["mean_net_r"] == pytest.approx(-0.647, abs=1e-3)
    assert out["mean_net_r_ci95"] == [pytest.approx(-1.72, abs=1e-3), pytest.approx(0.426, abs=1e-3)]
    assert out["total_net_r"] == pytest.approx(-3.88)
    assert out["mean_cost_r"] == 0.05
    assert out["break_even_win_rate"] == 42.0
    assert out["profit_factor"] == pytest.approx(0.27)
    # By exit time then id: four losses reach -4.29R, the 14:00 loss on id 8 adds -1.03R, then the win
    assert out["max_drawdown_r"] == pytest.approx(5.32)
    assert out["sample_size_warning"] == perf.INSUFFICIENT_SAMPLE
    assert (out["first_exit_time"], out["last_exit_time"]) == ("2026-10-14T10:00:00Z", "2026-10-14T14:00:00Z")


def test_expired_trades_count_against_the_win_rate():
    trades = [_trade(1, "PROFIT", 1.45), _trade(2, "LOSS", -1.05), _trade(3, "EXPIRED", 0.2)]
    out = perf.summarize_trades(trades, 1.5)
    assert out["win_rate"] == 33.3
    assert out["expired"] == 1


def test_summarize_without_trades():
    out = perf.summarize_trades([], 1.5)
    assert out["n_closed"] == 0
    assert out["win_rate"] is None and out["win_rate_ci95"] is None
    assert out["mean_net_r"] is None and out["mean_net_r_ci95"] is None
    assert out["total_net_r"] == 0.0 and out["max_drawdown_r"] == 0.0
    assert out["break_even_win_rate"] is None
    assert out["sample_size_warning"] == perf.INSUFFICIENT_SAMPLE
    assert out["first_exit_time"] is None


def test_large_sample_has_no_warning():
    trades = [_trade(i, "PROFIT" if i % 2 else "LOSS", 1.45 if i % 2 else -1.05) for i in range(perf.MIN_SAMPLE)]
    assert perf.summarize_trades(trades, 1.5)["sample_size_warning"] is None


def test_definitions_state_the_formulas():
    assert "expired" in perf.DEFINITIONS["win_rate"]
    assert "(1 + mean_cost_r) / (target_rr + 1)" in perf.DEFINITIONS["break_even_win_rate"]
    assert "exit time" in perf.DEFINITIONS["max_drawdown_r"]
