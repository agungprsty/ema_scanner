"""Offline replay of the production MACD strategy over historical klines.

The harness reuses the live code path (compute_macd, detect_macd_cross, resolve_outcome,
cost_adjusted_return) so a backtest number and a live number mean the same thing. See README.md.
"""
