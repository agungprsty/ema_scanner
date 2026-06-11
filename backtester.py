#!/usr/bin/env python3
import argparse
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.data_feed.binance_client import create_futures_client
from src.strategy.indicators import compute_indicators
from src.data_feed.macro_filter import compute_btc_bias
from src.config.settings import (
    GC_ATR_SL_MULTIPLIER,
    GC_ENTRY_FEE_PCT, SIGNAL_COOLDOWN_CANDLES,
    RISK_PER_TRADE_PERCENT, BTC_STRENGTH_MIN, MAX_HOLDING_CANDLES,
)


@dataclass
class TradeRecord:
    trade_id: int
    symbol: str
    side: str
    order_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    bep_price: float = 0.0
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    pnl: float = 0.0
    balance_after: float = 0.0
    status: str = "PENDING"
    position_size: float = 0.0
    tp1_hit: bool = False
    remaining_qty: float = 0.0
    expiry_idx: int = 0
    order_idx: int = 0


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    htf: str
    start_date: str
    end_date: str
    initial_balance: float
    final_balance: float
    total_signals: int
    filled: int
    expired: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    trades: list[dict[str, Any]] = field(default_factory=list)
    leverage: int = 10
    raw_crosses: list[dict[str, Any]] = field(default_factory=list)


def _raw_to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_av", "trades", "tb_base_av",
        "tb_quote_av", "ignore",
    ])
    df = df.iloc[:-1].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _detect_golden_cross(df: pd.DataFrame, idx: int) -> bool:
    if idx < 1:
        return False
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    ema5_curr = curr.get("EMA5", 0)
    ema20_curr = curr.get("EMA20", 0)
    ema5_prev = prev.get("EMA5", 0)
    ema20_prev = prev.get("EMA20", 0)
    if any(pd.isna(v) for v in [ema5_curr, ema20_curr, ema5_prev, ema20_prev]):
        return False
    return ema5_prev <= ema20_prev and ema5_curr > ema20_curr


def _detect_death_cross(df: pd.DataFrame, idx: int) -> bool:
    if idx < 1:
        return False
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    ema5_curr = curr.get("EMA5", 0)
    ema20_curr = curr.get("EMA20", 0)
    ema5_prev = prev.get("EMA5", 0)
    ema20_prev = prev.get("EMA20", 0)
    if any(pd.isna(v) for v in [ema5_curr, ema20_curr, ema5_prev, ema20_prev]):
        return False
    return ema5_prev >= ema20_prev and ema5_curr < ema20_curr


def _calc_cross_price(df: pd.DataFrame, idx: int) -> float:
    if idx < 1:
        return 0.0
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    ema5_curr = curr.get("EMA5", 0)
    ema20_curr = curr.get("EMA20", 0)
    ema5_prev = prev.get("EMA5", 0)
    ema20_prev = prev.get("EMA20", 0)
    if any(pd.isna(v) for v in [ema5_curr, ema20_curr, ema5_prev, ema20_prev]):
        return 0.0
    diff_prev = ema5_prev - ema20_prev
    diff_curr = ema5_curr - ema20_curr
    delta = diff_curr - diff_prev
    if delta == 0:
        return 0.0
    ratio = -diff_prev / delta
    return ema5_prev + ratio * (ema5_curr - ema5_prev)


def _detect_early_long(df: pd.DataFrame, idx: int) -> bool:
    if idx < 1:
        return False
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    ema5_curr = curr.get("EMA5", 0)
    ema20_curr = curr.get("EMA20", 0)
    ema5_prev = prev.get("EMA5", 0)
    ema20_prev = prev.get("EMA20", 0)
    rsi = curr.get("RSI", 0)
    if any(pd.isna(v) for v in [ema5_curr, ema20_curr, ema5_prev, ema20_prev, rsi]):
        return False
    if ema5_curr >= ema20_curr:
        return False
    gap_curr = ema5_curr - ema20_curr
    gap_prev = ema5_prev - ema20_prev
    return gap_curr > gap_prev and rsi > 55


def _detect_early_short(df: pd.DataFrame, idx: int) -> bool:
    if idx < 1:
        return False
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    ema5_curr = curr.get("EMA5", 0)
    ema20_curr = curr.get("EMA20", 0)
    ema5_prev = prev.get("EMA5", 0)
    ema20_prev = prev.get("EMA20", 0)
    rsi = curr.get("RSI", 0)
    if any(pd.isna(v) for v in [ema5_curr, ema20_curr, ema5_prev, ema20_prev, rsi]):
        return False
    if ema5_curr <= ema20_curr:
        return False
    gap_curr = ema5_curr - ema20_curr
    gap_prev = ema5_prev - ema20_prev
    return gap_curr < gap_prev and rsi < 45


def _check_volume_confirm(df: pd.DataFrame, idx: int) -> bool:
    curr = df.iloc[idx]
    volume = curr["volume"]
    ma_vol = curr.get("SMA_VOL20", 0)
    if pd.isna(ma_vol) or ma_vol <= 0:
        return False
    return volume > ma_vol * 0.31


def run_backtest(
    df_entry: pd.DataFrame,
    df_btc: pd.DataFrame | None = None,
    initial_balance: float = 10_000.0,
    risk_pct: float = RISK_PER_TRADE_PERCENT,
    leverage: int = 10,
) -> BacktestResult:
    if df_entry is None or len(df_entry) < 50:
        raise ValueError("Not enough entry data")
    if df_btc is not None:
        df_btc = compute_indicators(df_btc)

    btc_bias_cache: dict[int, str] = {}
    btc_strength_cache: dict[int, float] = {}

    balance = initial_balance
    trade_log: list[TradeRecord] = []
    trade_counter = 0
    peak_balance = initial_balance
    max_drawdown = 0.0
    total_gross_profit = 0.0
    total_gross_loss = 0.0

    active_trade: TradeRecord | None = None
    cooldown_map: dict[str, int] = {}
    raw_crosses: list[dict[str, Any]] = []

    for i in range(len(df_entry)):
        candle = df_entry.iloc[i]

        bias_key = (i // 24) * 24
        if bias_key not in btc_bias_cache:
            if df_btc is not None and len(df_btc) > 100:
                btc_slice = df_btc[df_btc["timestamp"] <= candle["timestamp"]]
                if len(btc_slice) >= 100:
                    b = compute_btc_bias(btc_slice)
                    btc_bias_cache[bias_key] = b.side
                    btc_strength_cache[bias_key] = b.strength
                else:
                    btc_bias_cache[bias_key] = "NEUTRAL"
                    btc_strength_cache[bias_key] = 0.0
            else:
                btc_bias_cache[bias_key] = "NEUTRAL"
                btc_strength_cache[bias_key] = 0.0

        current_bias = btc_bias_cache[bias_key]
        current_strength = btc_strength_cache[bias_key]
        bias_ok = current_bias != "NEUTRAL" and current_strength >= BTC_STRENGTH_MIN

        gc_raw = _detect_golden_cross(df_entry, i)
        dc_raw = _detect_death_cross(df_entry, i)
        early_long = _detect_early_long(df_entry, i)
        early_short = _detect_early_short(df_entry, i)

        # --- Macro 4h filter (rekomendasi) ---
        ema5_4h = candle.get("EMA5_4h", 0)
        close_price = candle["close"]
        if pd.isna(ema5_4h) or ema5_4h <= 0:
            macro_pass = False
            macro_label = "N/A (no EMA5_4h)"
        elif gc_raw or early_long:
            macro_pass = close_price > ema5_4h
            macro_label = f"{'PASS' if macro_pass else 'FAIL'} (Close {'>' if macro_pass else '<='} EMA5_4h)"
        elif dc_raw or early_short:
            macro_pass = close_price < ema5_4h
            macro_label = f"{'PASS' if macro_pass else 'FAIL'} (Close {'<' if macro_pass else '>='} EMA5_4h)"
        else:
            macro_pass = False
            macro_label = "N/A (no signal)"

        # --- Raw Signal Detection (unfiltered, for debug) ---
        signal_detected = gc_raw or dc_raw or early_long or early_short
        if signal_detected:
            close = candle["close"]
            ema20_val = candle.get("EMA20", 0)
            ema5_val = candle.get("EMA5", 0)
            rsi_val = candle.get("RSI", 0)
            cross_price = _calc_cross_price(df_entry, i) if (gc_raw or dc_raw) else 0.0

            vol_ok_raw = _check_volume_confirm(df_entry, i)
            vol_ratio = candle["volume"] / candle.get("SMA_VOL20", 1) if candle.get("SMA_VOL20", 0) > 0 else 0

            if gc_raw:
                signal_type = "GC"
            elif dc_raw:
                signal_type = "DC"
            elif early_long:
                signal_type = "EARLY_LONG"
            else:
                signal_type = "EARLY_SHORT"

            cross_rec: dict[str, Any] = {
                "timestamp": str(candle["timestamp"]),
                "type": signal_type,
                "close": close,
                "cross_price": round(cross_price, 4),
                "ema20": ema20_val,
                "ema5": ema5_val,
                "rsi": rsi_val,
                "volume_check": "PASS" if vol_ok_raw else "FAIL",
                "volume_ratio": round(vol_ratio, 2),
                "volume_raw": candle["volume"],
                "sma_vol20": candle.get("SMA_VOL20", 0),
                "macro_check": macro_label,
                "btc_check": "PASS" if bias_ok else "FAIL",
                "btc_bias": current_bias,
                "btc_strength": current_strength,
            }
            raw_crosses.append(cross_rec)

            if not (bias_ok and macro_pass):
                parts = []
                if not bias_ok:
                    parts.append(f"BTC Bias unfavourable ({current_bias}/{current_strength:.0f})")
                if not macro_pass:
                    parts.append(f"Macro unfavourable ({macro_label})")
                print(f"  [RECOMMENDATION] {signal_type} at {candle['timestamp']} — {', '.join(parts)} (not blocking)")

        # --- Active trade management (LONG / SHORT) ---
        if active_trade:
            is_long = active_trade.side == "LONG"
            tp1_hit = active_trade.tp1_hit
            remaining_qty = active_trade.remaining_qty if tp1_hit else active_trade.position_size
            tp1_qty = active_trade.position_size * 0.5

            exit_price = None
            exit_time = None
            cumulative_pnl = 0.0
            total_qty_exited = 0.0
            closed = False
            j = i

            for j in range(i, len(df_entry)):
                c = df_entry.iloc[j]
                candle_idx = j - i

                if not tp1_hit:
                    if (is_long and c["low"] <= active_trade.sl_price) or \
                       (not is_long and c["high"] >= active_trade.sl_price):
                        if is_long:
                            actual_sl = min(active_trade.sl_price, c["open"])
                            exit_fee = remaining_qty * actual_sl * GC_ENTRY_FEE_PCT / 100
                            cumulative_pnl += (actual_sl - active_trade.entry_price) * remaining_qty - exit_fee
                        else:
                            actual_sl = max(active_trade.sl_price, c["open"])
                            exit_fee = remaining_qty * actual_sl * GC_ENTRY_FEE_PCT / 100
                            cumulative_pnl += (active_trade.entry_price - actual_sl) * remaining_qty - exit_fee
                        total_qty_exited += remaining_qty
                        exit_price = actual_sl
                        exit_time = c["timestamp"]
                        closed = True
                        break

                    if (is_long and c["high"] >= active_trade.tp1_price) or \
                       (not is_long and c["low"] <= active_trade.tp1_price):
                        if is_long:
                            actual_tp1 = max(active_trade.tp1_price, c["open"])
                            exit_fee = tp1_qty * actual_tp1 * GC_ENTRY_FEE_PCT / 100
                            tp1_pnl = (actual_tp1 - active_trade.entry_price) * tp1_qty - exit_fee
                        else:
                            actual_tp1 = min(active_trade.tp1_price, c["open"])
                            exit_fee = tp1_qty * actual_tp1 * GC_ENTRY_FEE_PCT / 100
                            tp1_pnl = (active_trade.entry_price - actual_tp1) * tp1_qty - exit_fee
                        cumulative_pnl += tp1_pnl
                        total_qty_exited += tp1_qty
                        remaining_qty -= tp1_qty
                        tp1_hit = True
                        active_trade.tp1_hit = True
                        active_trade.remaining_qty = remaining_qty
                        active_trade.bep_price = active_trade.entry_price * (1 + GC_ENTRY_FEE_PCT / 100) if is_long \
                            else active_trade.entry_price * (1 - GC_ENTRY_FEE_PCT / 100)
                        print(f"  [TRADE] #{active_trade.trade_id} TP1 HIT at {c['timestamp']} ({actual_tp1:.4f})")
                        continue
                else:
                    if (is_long and c["high"] >= active_trade.tp2_price) or \
                       (not is_long and c["low"] <= active_trade.tp2_price):
                        if is_long:
                            actual_tp2 = max(active_trade.tp2_price, c["open"])
                            exit_fee = remaining_qty * actual_tp2 * GC_ENTRY_FEE_PCT / 100
                            cumulative_pnl += (actual_tp2 - active_trade.entry_price) * remaining_qty - exit_fee
                        else:
                            actual_tp2 = min(active_trade.tp2_price, c["open"])
                            exit_fee = remaining_qty * actual_tp2 * GC_ENTRY_FEE_PCT / 100
                            cumulative_pnl += (active_trade.entry_price - actual_tp2) * remaining_qty - exit_fee
                        total_qty_exited += remaining_qty
                        exit_price = actual_tp2
                        exit_time = c["timestamp"]
                        closed = True
                        print(f"  [TRADE] #{active_trade.trade_id} TP2 HIT at {c['timestamp']} ({actual_tp2:.4f})")
                        break

                    if (is_long and c["low"] <= active_trade.bep_price) or \
                       (not is_long and c["high"] >= active_trade.bep_price):
                        if is_long:
                            actual_bep = min(active_trade.bep_price, c["open"])
                            exit_fee = remaining_qty * actual_bep * GC_ENTRY_FEE_PCT / 100
                            cumulative_pnl += (actual_bep - active_trade.entry_price) * remaining_qty - exit_fee
                        else:
                            actual_bep = max(active_trade.bep_price, c["open"])
                            exit_fee = remaining_qty * actual_bep * GC_ENTRY_FEE_PCT / 100
                            cumulative_pnl += (active_trade.entry_price - actual_bep) * remaining_qty - exit_fee
                        total_qty_exited += remaining_qty
                        exit_price = actual_bep
                        exit_time = c["timestamp"]
                        closed = True
                        print(f"  [TRADE] #{active_trade.trade_id} BEP HIT at {c['timestamp']} ({actual_bep:.4f})")
                        break

                if candle_idx >= MAX_HOLDING_CANDLES and remaining_qty > 0:
                    exit_price = c["close"]
                    exit_fee = remaining_qty * exit_price * GC_ENTRY_FEE_PCT / 100
                    if is_long:
                        cumulative_pnl += (exit_price - active_trade.entry_price) * remaining_qty - exit_fee
                    else:
                        cumulative_pnl += (active_trade.entry_price - exit_price) * remaining_qty - exit_fee
                    total_qty_exited += remaining_qty
                    exit_time = c["timestamp"]
                    closed = True
                    break

            entry_fee_total = active_trade.position_size * active_trade.entry_price * GC_ENTRY_FEE_PCT / 100
            active_trade.pnl = cumulative_pnl - entry_fee_total
            active_trade.status = "WIN" if active_trade.pnl > 0 else "LOSS"

            if not closed and total_qty_exited == 0:
                last_c = df_entry.iloc[-1]
                exit_price = last_c["close"]
                if is_long:
                    active_trade.pnl = (exit_price - active_trade.entry_price) * remaining_qty - entry_fee_total
                else:
                    active_trade.pnl = (active_trade.entry_price - exit_price) * remaining_qty - entry_fee_total
                exit_time = last_c["timestamp"]
                active_trade.status = "LOSS" if active_trade.pnl < 0 else "WIN"

            active_trade.exit_price = exit_price
            active_trade.exit_time = exit_time
            i = j
            balance += active_trade.pnl
            active_trade.balance_after = balance
            if active_trade.pnl > 0:
                total_gross_profit += active_trade.pnl
            elif active_trade.pnl < 0:
                total_gross_loss += abs(active_trade.pnl)
            print(f"  [TRADE] #{active_trade.trade_id} CLOSED — PnL: ${active_trade.pnl:+.2f} | {active_trade.status}")
            trade_log.append(active_trade)
            active_trade = None
            cooldown_map["TEST"] = i
            continue

        # --- Entry signal generation (Market Order) ---
        last_sig = cooldown_map.get("TEST", -SIGNAL_COOLDOWN_CANDLES)
        if i - last_sig < SIGNAL_COOLDOWN_CANDLES:
            continue

        signal_side = None
        signal_type = None
        if gc_raw:
            signal_side = "LONG"
            signal_type = "GC"
        elif dc_raw:
            signal_side = "SHORT"
            signal_type = "DC"
        elif early_long:
            signal_side = "LONG"
            signal_type = "EARLY_LONG"
        elif early_short:
            signal_side = "SHORT"
            signal_type = "EARLY_SHORT"

        if not signal_side:
            continue

        if not _check_volume_confirm(df_entry, i):
            continue

        atr_val = candle.get("ATR", 0)
        if pd.isna(atr_val) or atr_val <= 0:
            continue
        risk_dist = GC_ATR_SL_MULTIPLIER * atr_val
        if risk_dist <= 0:
            continue

        entry_price = candle["close"]
        trade_counter += 1
        risk_amount = balance * risk_pct
        risk_per_unit = risk_dist
        if risk_per_unit > 0:
            ideal_position = risk_amount / risk_per_unit
            max_position = (balance * leverage) / entry_price
            pos_size = min(ideal_position, max_position)
        else:
            pos_size = 0

        if pos_size <= 0:
            continue

        active_trade = TradeRecord(
            trade_id=trade_counter,
            symbol="TEST",
            side=signal_side,
            order_time=candle["timestamp"],
            entry_time=candle["timestamp"],
            entry_price=entry_price,
            position_size=pos_size,
            status="FILLED",
            order_idx=i,
            expiry_idx=i + 24,
        )

        if signal_side == "LONG":
            active_trade.sl_price = entry_price - risk_dist
            active_trade.tp1_price = entry_price + risk_dist
            active_trade.tp2_price = entry_price + 2 * risk_dist
            active_trade.bep_price = entry_price * (1 + GC_ENTRY_FEE_PCT / 100)
        else:
            active_trade.sl_price = entry_price + risk_dist
            active_trade.tp1_price = entry_price - risk_dist
            active_trade.tp2_price = entry_price - 2 * risk_dist
            active_trade.bep_price = entry_price * (1 - GC_ENTRY_FEE_PCT / 100)

        active_trade.remaining_qty = pos_size
        print(f"  [TRADE] #{active_trade.trade_id} MARKET ENTRY at {candle['timestamp']} "
              f"({signal_type}, {signal_side}, entry={entry_price:.4f}, "
              f"SL={active_trade.sl_price:.4f}, TP1={active_trade.tp1_price:.4f}, "
              f"TP2={active_trade.tp2_price:.4f})")

        cooldown_map["TEST"] = i
        continue

    wins = sum(1 for t in trade_log if t.status == "WIN")
    losses = sum(1 for t in trade_log if t.status == "LOSS")
    filled_count = sum(1 for t in trade_log if t.status in ("WIN", "LOSS", "FILLED"))
    expired_count = sum(1 for t in trade_log if t.status == "EXPIRED")
    filled_trades = wins + losses
    win_rate = (wins / filled_trades * 100) if filled_trades > 0 else 0.0
    profit_factor = (total_gross_profit / total_gross_loss) if total_gross_loss > 0 else float("inf")

    equity = initial_balance
    for t in trade_log:
        if t.status in ("WIN", "LOSS"):
            equity += t.pnl
            peak_balance = max(peak_balance, equity)
            dd = (peak_balance - equity) / peak_balance * 100
            max_drawdown = max(max_drawdown, dd)

    trade_dicts = [
        {
            "trade_id": t.trade_id,
            "type": t.side,
            "side": t.side,
            "order_time": str(t.order_time),
            "entry_time": str(t.entry_time),
            "exit_time": str(t.exit_time) if t.exit_time else None,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "sl_price": t.sl_price,
            "tp1_price": t.tp1_price,
            "tp2_price": t.tp2_price,
            "bep_price": t.bep_price,
            "tp1_hit": t.tp1_hit,
            "pnl": t.pnl,
            "balance_after": t.balance_after,
            "status": t.status,
        }
        for t in trade_log
    ]

    return BacktestResult(
        symbol="TEST",
        timeframe="1h",
        htf="4h",
        start_date=str(df_entry.iloc[0]["timestamp"]) if "timestamp" in df_entry.columns else "",
        end_date=str(df_entry.iloc[-1]["timestamp"]) if "timestamp" in df_entry.columns else "",
        initial_balance=initial_balance,
        final_balance=balance,
        total_signals=trade_counter,
        filled=filled_trades,
        expired=expired_count,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        trades=trade_dicts,
        leverage=leverage,
        raw_crosses=raw_crosses,
    )


def print_report(result: BacktestResult) -> None:
    print("=" * 68)
    print("           BACKTEST SETUP — Market Order (EMA5/20 + RSI)")
    print("=" * 68)
    print(f"Symbol               : {result.symbol}")
    print(f"TF Stack             : {result.timeframe} / {result.htf}")
    print("Periode Pengujian    : {result.start_date} s/d {result.end_date}")
    print("Strategi             : Market Order @ Close + 1:2 R:R + TP1/BEP/TP2")
    print("Volume Threshold      : SMA20 x 1.45 (min)")
    print("Early Entry          : EMA gap narrowing + RSI > 55 (LONG) / < 45 (SHORT)")
    print(f"Modal Awal           : ${result.initial_balance:,.2f}")
    print(f"Risiko per Trade     : {RISK_PER_TRADE_PERCENT * 100:.1f}%")
    print(f"Leverage             : {result.leverage}x")

    print()
    print("=" * 68)
    print("         BALANCE COMPARISON (BEFORE vs AFTER)")
    print("=" * 68)
    print(f"[BEFORE] Initial Balance : ${result.initial_balance:,.2f}")
    print(f"[AFTER]  Final Balance   : ${result.final_balance:,.2f}")
    net_pnl = result.final_balance - result.initial_balance
    net_pnl_pct = (net_pnl / result.initial_balance) * 100
    print(f"{'─' * 68}")
    print(f"Net Profit/Loss ($)      : ${net_pnl:+,.2f}")
    print(f"Net PnL %                : {net_pnl_pct:+.2f}%")

    print()
    print("=" * 68)
    print("           STRATEGY PERFORMANCE METRICS")
    print("=" * 68)
    print(f"Pending Signals          : {result.total_signals}")
    print(f"Filled                   : {result.filled}")
    print(f"Expired                  : {result.expired}")
    print()
    print(f"Wins                     : {result.wins}")
    print(f"Losses                   : {result.losses}")
    print(f"Win Rate                 : {result.win_rate:.2f}%")
    print()
    print(f"Profit Factor            : {result.profit_factor:.2f}")
    print(f"Max Drawdown             : {result.max_drawdown:.2f}%")
    print("=" * 68)

    gc_count = sum(1 for xc in result.raw_crosses if xc["type"] == "GC")
    dc_count = sum(1 for xc in result.raw_crosses if xc["type"] == "DC")
    el_count = sum(1 for xc in result.raw_crosses if xc["type"] == "EARLY_LONG")
    es_count = sum(1 for xc in result.raw_crosses if xc["type"] == "EARLY_SHORT")
    print()
    print("=" * 68)
    print("           SIGNAL INFO")
    print("=" * 68)
    print(f"GC (Golden Cross)        : {gc_count}")
    print(f"DC (Death Cross)         : {dc_count}")
    print(f"EARLY_LONG (RSI > 55)    : {el_count}")
    print(f"EARLY_SHORT (RSI < 45)   : {es_count}")
    print("=" * 68)


def generate_trade_chart(df: pd.DataFrame, results: BacktestResult, output_path: str = "trade_chart.html") -> None:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.75, 0.25],
    )

    fig.add_trace(go.Candlestick(
        x=df["timestamp"],
        open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="Price",
        showlegend=False,
    ), row=1, col=1)

    ema20_color = "rgba(255, 165, 0, 0.85)"
    ema5_color = "rgba(100, 149, 237, 0.85)"
    ema20_4h_color = "rgba(255, 99, 132, 0.8)"
    ema5_4h_color = "rgba(255, 255, 0, 0.7)"

    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["EMA20"],
        line=dict(color=ema20_color, width=1.5),
        name="EMA20 (30m)",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["EMA5"],
        line=dict(color=ema5_color, width=1.5),
        name="EMA5 (30m)",
    ), row=1, col=1)

    if "EMA5_4h" in df.columns:
        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=df["EMA5_4h"],
            line=dict(color=ema5_4h_color, width=1.5, dash="dot"),
            name="EMA5 (4h) — Macro",
        ), row=1, col=1)

    if "EMA20_4h" in df.columns:
        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=df["EMA20_4h"],
            line=dict(color=ema20_4h_color, width=1.5, dash="dash"),
            name="EMA20 (4h)",
        ), row=1, col=1)

    # --- Raw Cross Markers ---
    gc_times: list[str] = []
    gc_prices: list[float] = []
    gc_hover: list[str] = []
    xc_times: list[str] = []
    xc_prices: list[float] = []
    xc_hover: list[str] = []
    xc_colors: list[str] = []
    dc_times: list[str] = []
    dc_prices: list[float] = []
    dc_hover: list[str] = []

    for xc in results.raw_crosses:
        ts = xc["timestamp"]
        close = xc["close"]
        xprice = xc.get("cross_price", close)
        stype = xc["type"]

        if stype == "GC":
            label = "GOLDEN CROSS"
            color = "limegreen"
            vcolor = "rgba(50,205,50,0.25)"
        elif stype == "DC":
            label = "DEATH CROSS"
            color = "tomato"
            vcolor = "rgba(255,99,71,0.25)"
        elif stype == "EARLY_LONG":
            label = "EARLY LONG (RSI)"
            color = "cyan"
            vcolor = "rgba(0,255,255,0.15)"
        else:
            label = "EARLY SHORT (RSI)"
            color = "magenta"
            vcolor = "rgba(255,0,255,0.15)"

        hover = (
            f"<b>[{label}]</b><br>"
            f"Time: {ts}<br>"
            f"Close: {close:.4f} | EMA20: {xc['ema20']:.4f} | EMA5: {xc['ema5']:.4f}<br>"
            f"RSI: {xc.get('rsi', 0):.1f}<br>"
            f"Volume Confirm: [{xc['volume_check']}] ({xc['volume_ratio']:.2f}x)<br>"
            f"Macro Filter: {xc['macro_check']}<br>"
            f"BTC Bias Filter: [{xc['btc_check']}] (bias={xc['btc_bias']}, str={xc['btc_strength']:.0f})"
        )

        fig.add_vline(x=ts, line=dict(color=vcolor, width=1, dash="dash"), row=1)

        if stype == "GC":
            gc_times.append(ts)
            gc_prices.append(close)
            gc_hover.append(hover)
        elif stype == "DC":
            dc_times.append(ts)
            dc_prices.append(close)
            dc_hover.append(hover)

        xc_times.append(ts)
        xc_prices.append(xprice)
        xc_hover.append(hover)
        xc_colors.append(color)

    if gc_times:
        fig.add_trace(go.Scatter(
            x=gc_times, y=gc_prices,
            mode="markers",
            marker=dict(size=10, color="lime", symbol="triangle-up", line=dict(width=1, color="darkgreen")),
            name="Raw GC",
            hovertext=gc_hover,
            hoverinfo="text",
            showlegend=True,
        ), row=1, col=1)

    if dc_times:
        fig.add_trace(go.Scatter(
            x=dc_times, y=dc_prices,
            mode="markers",
            marker=dict(size=10, color="tomato", symbol="triangle-down", line=dict(width=1, color="darkred")),
            name="Raw DC",
            hovertext=dc_hover,
            hoverinfo="text",
            showlegend=True,
        ), row=1, col=1)

    if xc_times:
        fig.add_trace(go.Scatter(
            x=xc_times, y=xc_prices,
            mode="markers",
            marker=dict(size=14, color=xc_colors, symbol="circle", line=dict(width=2, color="black")),
            name="Cross Price",
            hovertext=xc_hover,
            hoverinfo="text",
            showlegend=True,
        ), row=1, col=1)

    # Trade markers
    for t in results.trades:
        order_time = t.get("order_time")
        entry_time = t.get("entry_time")
        exit_time = t.get("exit_time")
        entry_price = t.get("entry_price")
        exit_price = t.get("exit_price")
        status = t.get("status", "")
        trade_id = t.get("trade_id", "")
        pnl = t.get("pnl", 0)
        tp1_p = t.get("tp1_price", 0)
        tp2_p = t.get("tp2_price", 0)
        sl_p = t.get("sl_price", 0)
        bep_p = t.get("bep_price", 0)
        tp1_hit_flag = t.get("tp1_hit", False)

        if status == "EXPIRED":
            continue

        if order_time and entry_price:
            trade_side = t.get("side", "LONG")
            entry_symbol = "triangle-down" if trade_side == "SHORT" else "triangle-up"
            entry_color = "tomato" if trade_side == "SHORT" else "lime"
            entry_border = "darkred" if trade_side == "SHORT" else "darkgreen"
            fig.add_trace(go.Scatter(
                x=[entry_time], y=[entry_price],
                mode="markers",
                marker=dict(symbol=entry_symbol, size=12, color=entry_color, line=dict(width=1, color=entry_border)),
                name=f"Entry #{trade_id}",
                hovertext=(f"<b>[MARKET ENTRY] #{trade_id}</b><br>"
                           f"Side: {trade_side}<br>"
                           f"Entry: ${entry_price:,.2f}<br>"
                           f"Time: {entry_time}<br>"
                           f"TP1 Hit: {'Yes' if tp1_hit_flag else 'No'}"),
                hoverinfo="text",
                showlegend=False,
            ), row=1, col=1)



        if exit_time and exit_price is not None and exit_price > 0:
            marker_color = "red" if status == "LOSS" else "blue"
            fig.add_trace(go.Scatter(
                x=[exit_time], y=[exit_price],
                mode="markers",
                marker=dict(symbol="triangle-down", size=12, color=marker_color,
                            line=dict(width=1, color="darkblue" if status == "WIN" else "darkred")),
                name=f"Exit #{trade_id}",
                hovertext=f"<b>[EXIT] #{trade_id}</b><br>Exit: ${exit_price:,.2f}<br>PnL: ${pnl:+.2f}<br>Status: {status}",
                hoverinfo="text",
                showlegend=False,
            ), row=1, col=1)

        if entry_time and exit_time and entry_price and exit_price and exit_price > 0:
            fig.add_trace(go.Scatter(
                x=[entry_time, exit_time],
                y=[entry_price, exit_price],
                mode="lines",
                line=dict(color="rgba(128, 128, 128, 0.4)", width=1, dash="dot"),
                showlegend=False,
                hoverinfo="skip",
            ), row=1, col=1)

    fig.add_trace(go.Bar(
        x=df["timestamp"], y=df["volume"],
        name="Volume",
        marker=dict(color="rgba(150, 150, 150, 0.5)"),
        showlegend=False,
    ), row=2, col=1)

    if "SMA_VOL20" in df.columns:
        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=df["SMA_VOL20"],
            line=dict(color="orange", width=1.5, dash="dash"),
            name="MA Volume 20",
            showlegend=False,
        ), row=2, col=1)

    fig.update_layout(
        title=f"Backtest: {results.symbol} — Market Order (EMA5/20 + RSI) 1:2 R:R<br><sup>{results.start_date} to {results.end_date} | "
               f"Signals: {results.total_signals} | Filled: {results.filled} | Win Rate: {results.win_rate:.1f}% | "
               f"PnL: ${results.final_balance - results.initial_balance:+.2f}</sup>",
        xaxis_title="Time",
        yaxis_title="Price (USDT)",
        hovermode="x unified",
        template="plotly_dark",
        height=900,
        legend=dict(orientation="h", y=1.02, x=0.5, xanchor="center"),
    )

    fig.update_xaxes(rangeslider=dict(visible=False))
    fig.update_yaxes(title_text="Price (USDT)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    fig.write_html(output_path)
    print(f"Chart saved: {output_path}")


def fetch_klines_paginated(client, symbol, interval, total_candles=10000, limit=1500):
    all_candles = []
    end_time = None

    while len(all_candles) < total_candles:
        params = dict(symbol=symbol, interval=interval, limit=limit)
        if end_time is not None:
            params["endTime"] = end_time
        resp = client.klines(**params)
        if not resp:
            break
        all_candles = resp + all_candles
        if len(resp) < limit:
            break
        end_time = resp[0][0] - 1

    return all_candles[-total_candles:]


def main():
    parser = argparse.ArgumentParser(description="Market Order Backtester (EMA5/20 + RSI Early Entry)")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--timeframe", default="30m", help="Entry timeframe (golden cross)")
    parser.add_argument("--htf", default="4h", help="Macro timeframe")
    parser.add_argument("--limit", type=int, default=1000, help="Entry candles to fetch (~83 days on 1h)")
    parser.add_argument("--balance", type=float, default=100.0, help="Initial balance")
    args = parser.parse_args()

    client = create_futures_client()

    macro_limit = max(200, args.limit // 4)

    raw_entry = fetch_klines_paginated(client, args.symbol, args.timeframe, total_candles=args.limit)
    if not raw_entry or len(raw_entry) < 200:
        print("Failed to fetch entry data")
        return
    df_entry = _raw_to_df(raw_entry)
    df_entry = compute_indicators(df_entry)
    if df_entry is None or len(df_entry) < 50:
        print("Not enough entry data after indicators")
        return

    raw_macro = fetch_klines_paginated(client, args.symbol, args.htf, total_candles=macro_limit)
    if raw_macro and len(raw_macro) >= 100:
        df_macro = compute_indicators(_raw_to_df(raw_macro))
    else:
        df_macro = None

    raw_btc = fetch_klines_paginated(client, "BTCUSDT", args.htf, total_candles=macro_limit)
    df_btc = compute_indicators(_raw_to_df(raw_btc)) if raw_btc and len(raw_btc) >= 100 else None

    if df_macro is not None and "EMA5" in df_macro.columns:
        macro_cols = df_macro[["timestamp", "close", "EMA5", "EMA20"]].dropna().rename(
            columns={"close": "close_4h", "EMA5": "EMA5_4h", "EMA20": "EMA20_4h"})
        df_entry = pd.merge_asof(df_entry, macro_cols, on="timestamp", direction="backward")
        df_entry = df_entry.dropna(subset=["EMA5_4h"]).reset_index(drop=True)
    else:
        print("No macro data available for merge")
        return

    if len(df_entry) < 50:
        print("Not enough data after merge")
        return

    result = run_backtest(df_entry, df_btc=df_btc, initial_balance=args.balance)
    print_report(result)
    chart_path = f"backtest_{args.symbol}_{args.timeframe}_{args.htf}.html"
    generate_trade_chart(df_entry, result, output_path=chart_path)


if __name__ == "__main__":
    main()
