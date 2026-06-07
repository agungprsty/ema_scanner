from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


@dataclass
class OrderSpec:
    symbol: str
    side: str
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    quantity: Decimal


def calculate_position(
    symbol: str,
    side: str,
    entry_price: float,
    atr: float,
    balance: float,
    risk_pct: float = 0.01,
    tick_size: float = 0.01,
    step_size: float = 0.001,
) -> OrderSpec:
    entry = Decimal(str(entry_price))
    atr_dec = Decimal(str(atr))

    if side == "LONG":
        sl = entry - atr_dec * Decimal("1.5")
        tp = entry + (entry - sl) * Decimal("2.0")
    else:
        sl = entry + atr_dec * Decimal("1.5")
        tp = entry - (sl - entry) * Decimal("2.0")

    risk_amount = Decimal(str(balance)) * Decimal(str(risk_pct))
    price_distance = abs(entry - sl)
    raw_qty = risk_amount / price_distance if price_distance > 0 else Decimal("0")

    # Round quantity down to step_size
    step_dec = Decimal(str(step_size))
    qty = (raw_qty / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec

    # Round prices to tick_size
    tick_dec = Decimal(str(tick_size))
    entry_rounded = (entry / tick_dec).to_integral_value() * tick_dec
    sl_rounded = (sl / tick_dec).to_integral_value() * tick_dec
    tp_rounded = (tp / tick_dec).to_integral_value() * tick_dec

    return OrderSpec(
        symbol=symbol,
        side=side,
        entry=entry_rounded,
        stop_loss=sl_rounded,
        take_profit=tp_rounded,
        quantity=qty,
    )
