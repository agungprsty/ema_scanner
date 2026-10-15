"""Exit labels of strategies that close without a fixed target, and how the cost model fills each.

A TradeOutcome's `result` is what the ledger books and what cost_adjusted_return prices the exit by:
PROFIT is a resting limit order (no slippage), LOSS a stop (slippage times the stop multiplier) and
anything else a market order (plain slippage). The labels below are results in their own right, so
the ledger says how a trade ended; `cost_result` names the fill each one is priced as.
"""

# The swing stop was touched; filled like a stop
STOPPED = "STOPPED"
# Closed at the open of the candle after the first opposite MACD cross; a market fill
OPPOSITE_CROSS = "OPPOSITE_CROSS"
# Closed at the close of the last candle the trade may be held; a market fill
TIME_EXIT = "TIME_EXIT"

EXIT_RESULTS: tuple[str, ...] = (STOPPED, OPPOSITE_CROSS, TIME_EXIT)

# EXPIRED is the market close in cost_adjusted_return's vocabulary
_COST_RESULT = {STOPPED: "LOSS", OPPOSITE_CROSS: "EXPIRED", TIME_EXIT: "EXPIRED"}


def cost_result(result: str) -> str:
    """The result cost_adjusted_return prices `result` as: PROFIT, LOSS and EXPIRED pass through."""
    return _COST_RESULT.get(result, result)
