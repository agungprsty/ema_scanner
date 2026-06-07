from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from src.config.settings import MAX_TOTAL_RISK_PCT, MAX_DAILY_LOSS_PCT, TP1_RISK_MULTIPLIER, TP1_EXIT_PCT


@dataclass
class OrderSpec:
    symbol: str
    side: str
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    take_profit2: Decimal | None = None
    quantity: Decimal = Decimal("0")
    quantity_tp1: Decimal = Decimal("0")


class PortfolioRisk:
    def __init__(self, max_total_risk: float = MAX_TOTAL_RISK_PCT):
        self.max_total_risk = max_total_risk
        self.active_risks: dict[str, float] = {}  # symbol -> risk_pct used
        self.daily_pnl: float = 0.0
        self.last_reset_day: int = datetime.now(timezone.utc).timetuple().tm_yday

    def _reset_daily_if_needed(self):
        today = datetime.now(timezone.utc).timetuple().tm_yday
        if today != self.last_reset_day:
            self.daily_pnl = 0.0
            self.last_reset_day = today

    def check_daily_loss_limit(self) -> bool:
        self._reset_daily_if_needed()
        return self.daily_pnl >= -MAX_DAILY_LOSS_PCT / 100.0

    def record_pnl(self, pnl_pct: float):
        self._reset_daily_if_needed()
        self.daily_pnl += pnl_pct

    def adjusted_risk_pct(self, symbol: str, side: str, base_risk: float = 0.01) -> float:
        total_active = sum(self.active_risks.values())
        if total_active >= self.max_total_risk:
            return 0.0
        available = self.max_total_risk - total_active
        return min(base_risk, available)

    def register_trade(self, symbol: str, risk_pct_used: float):
        self.active_risks[symbol] = risk_pct_used

    def remove_trade(self, symbol: str):
        self.active_risks.pop(symbol, None)


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    if avg_loss == 0 or win_rate <= 0 or win_rate >= 1:
        return 0.01
    r = avg_win / avg_loss
    f = win_rate - (1 - win_rate) / r
    return max(0.005, min(0.02, f * 0.25))


def calculate_position(
    symbol: str,
    side: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    balance: float,
    risk_pct: float = 0.01,
    tick_size: float = 0.01,
    step_size: float = 0.001,
    leverage: int = 10,
    portfolio_risk: PortfolioRisk | None = None,
    win_rate: float = 0.0,
    avg_win: float = 0.0,
    avg_loss: float = 0.0,
) -> OrderSpec | None:
    entry = Decimal(str(entry_price))

    if side == "LONG":
        risk = entry - Decimal(str(sl_price))
        if risk <= Decimal("0"):
            return None
        tp1 = entry + risk * Decimal(str(TP1_RISK_MULTIPLIER))
        tp = Decimal(str(tp_price))
    else:
        risk = Decimal(str(sl_price)) - entry
        if risk <= Decimal("0"):
            return None
        tp1 = entry - risk * Decimal(str(TP1_RISK_MULTIPLIER))
        tp = Decimal(str(tp_price))

    adjusted_risk = portfolio_risk.adjusted_risk_pct(symbol, side, risk_pct) if portfolio_risk else risk_pct
    if adjusted_risk <= 0:
        return None

    if win_rate > 0 and avg_win > 0 and avg_loss > 0:
        kelly_risk = kelly_fraction(win_rate, avg_win, avg_loss)
        adjusted_risk = min(adjusted_risk, kelly_risk)

    risk_amount = Decimal(str(balance)) * Decimal(str(adjusted_risk))

    if risk > 0:
        ideal_qty = risk_amount / risk
    else:
        ideal_qty = Decimal("0")

    max_qty = (Decimal(str(balance)) * Decimal(str(leverage))) / entry
    raw_qty = min(ideal_qty, max_qty)

    # Round quantity down to step_size
    step_dec = Decimal(str(step_size))
    qty = (raw_qty / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec

    # TP1_EXIT_PCT at TP1, remaining at TP2
    qty_tp1 = (qty * Decimal(str(TP1_EXIT_PCT))).to_integral_value(rounding=ROUND_DOWN) * step_dec

    # Round prices to tick_size
    tick_dec = Decimal(str(tick_size))
    entry_rounded = (entry / tick_dec).to_integral_value() * tick_dec
    sl_rounded = (Decimal(str(sl_price)) / tick_dec).to_integral_value() * tick_dec
    tp_rounded = (tp / tick_dec).to_integral_value() * tick_dec
    tp1_rounded = (tp1 / tick_dec).to_integral_value() * tick_dec

    if portfolio_risk:
        portfolio_risk.register_trade(symbol, float(adjusted_risk))

    return OrderSpec(
        symbol=symbol,
        side=side,
        entry=entry_rounded,
        stop_loss=sl_rounded,
        take_profit=tp1_rounded,
        take_profit2=tp_rounded,
        quantity=qty,
        quantity_tp1=qty_tp1,
    )
