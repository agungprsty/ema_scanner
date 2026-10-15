"""The contract every strategy implements, so live scans, the evaluator and the backtest harness can
run any registered strategy through the same code.

A strategy is identified by (key, version). Its parameters are a frozen pydantic model; its
`params_hash` is the SHA-256 of their canonical JSON and its `code_hash` the SHA-256 of the source of
its module plus the modules it declares in `code_modules` (the functions holding its math). A version
that is frozen may change neither: the engine refuses to emit signals when either hash differs from
what was frozen (see registry.emission_refusal).
"""

import hashlib
import importlib.util
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Literal, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from src.strategy.context import MarketContext
from src.strategy.macd import MacdSignal
from src.strategy.outcome import TradeOutcome

StrategyStatus = Literal["live", "shadow", "research"]
STRATEGY_STATUSES: tuple[str, ...] = ("live", "shadow", "research")
# Statuses the scheduled scan runs: live signals are alerted, shadow ones only stored and evaluated
RUNNABLE_STATUSES: tuple[str, ...] = ("live", "shadow")


class StrategyParams(BaseModel):
    """Base for a strategy's parameters: immutable, and unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def params_hash(params: StrategyParams) -> str:
    return hashlib.sha256(canonical_json(params.model_dump(mode="json")).encode()).hexdigest()


def module_source(module_name: str) -> bytes:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise ValueError(f"cannot find the source of module {module_name}")
    with open(spec.origin, "rb") as fh:
        # Line endings are normalized so a checkout on another OS hashes the same
        return fh.read().replace(b"\r\n", b"\n")


def source_hash(module_names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in module_names:
        digest.update(name.encode() + b"\0")
        digest.update(module_source(name) + b"\0")
    return digest.hexdigest()


@dataclass
class SignalIntent(MacdSignal):
    """A signal a strategy wants recorded: the columns of a macd_signals row plus who produced it.

    `cross_candle_ms` is the candle the signal fired on (the decision candle), and `tp1` is 0 for a
    strategy without a fixed target. `features` holds strategy-specific inputs worth keeping.
    """

    strategy_key: str = ""
    strategy_version: str = ""
    features: dict = field(default_factory=dict)

    @classmethod
    def from_signal(cls, sig: MacdSignal, strategy: "Strategy", **features) -> "SignalIntent":
        return cls(
            **sig.to_dict(),
            strategy_key=strategy.key,
            strategy_version=strategy.version,
            features=features,
        )


class TradeLevels(Protocol):
    """What `resolve` reads from a signal: a SignalIntent in a replay, a stored row in the evaluator."""

    side: str
    entry_price: float
    stop_loss: float
    tp1: float
    cross_candle_ms: int


@dataclass(frozen=True)
class ResolveContext:
    """`now_ms` is the evaluation instant (only candles closed by then may settle a trade), and
    `history` the context up to now for strategies whose exit needs indicators (None otherwise)."""

    now_ms: int
    interval_ms: int
    history: MarketContext | None = None


class Strategy(ABC):
    """One version of one strategy.

    detect() decides on closed candles only and must be pure: the same context always gives the same
    signals, and it may not read clocks, files or the network. resolve() settles a signal from the
    candles that follow its decision candle (the entry candle first), so exit rules belong to the
    strategy and can differ between strategies.
    """

    key: ClassVar[str]
    version: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    status: ClassVar[StrategyStatus]
    timeframe: ClassVar[str] = "2h"
    # Higher timeframes detect() reads, closed candles only, e.g. ("1d",)
    context_timeframes: ClassVar[tuple[str, ...]] = ()
    # Closed candles of each context timeframe the live scan fetches
    context_bars: ClassVar[int] = 0
    # Modules besides the strategy's own whose source is part of its code_hash
    code_modules: ClassVar[tuple[str, ...]] = ()
    params_model: ClassVar[type[StrategyParams]]

    def __init__(self, params: StrategyParams | None = None):
        params = params if params is not None else self.params_model()
        if not isinstance(params, self.params_model):
            raise TypeError(f"{self.ref} takes {self.params_model.__name__}, not {type(params).__name__}")
        self.params = params

    @property
    def ref(self) -> str:
        return f"{self.key}@{self.version}"

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Closed base candles a decision needs before it (a replay skips earlier ones)."""

    @property
    @abstractmethod
    def history_bars(self) -> int:
        """Base candles the live scan fetches per symbol, the forming one included."""

    @property
    @abstractmethod
    def max_hold_bars(self) -> int:
        """Most candles after the decision candle that resolve() can need to settle a trade."""

    # Base candles up to and including the decision candle that resolve() needs as history
    resolve_history_bars: ClassVar[int] = 0

    @abstractmethod
    def detect(self, ctx: MarketContext) -> list[SignalIntent]:
        """Signals on the newest `ctx.catchup_bars` closed candles, oldest first or newest only, as the
        strategy defines. Closed candles only; `ctx.next_open` is the earliest possible entry."""

    @abstractmethod
    def resolve(self, signal: TradeLevels, candles: list, ctx: ResolveContext) -> TradeOutcome | None:
        """How the trade ended, or None while it is still open. `candles` are kline rows
        [open_ms, open, high, low, close, ...] from the entry candle on, oldest first."""

    def candidate_bars(self, klines: pd.DataFrame) -> np.ndarray | None:
        """Optional replay shortcut: positions in normalized `klines` whose candle can produce a signal.

        A replay then calls detect() only after those candles close instead of after every one. It
        must be a superset of where detect() fires; None (the default) means every candle.
        """
        return None

    @property
    def params_dict(self) -> dict:
        return self.params.model_dump(mode="json")

    @property
    def params_hash(self) -> str:
        return params_hash(self.params)

    @property
    def code_hash(self) -> str:
        return source_hash((type(self).__module__, *self.code_modules))

    def report_params(self) -> dict:
        """Parameters as a backtest report shows them."""
        return {"timeframe": self.timeframe, **self.params_dict}

    def describe(self) -> dict:
        return {
            "key": self.key,
            "version": self.version,
            "ref": self.ref,
            "name": self.name,
            "status": self.status,
            "timeframe": self.timeframe,
            "context_timeframes": list(self.context_timeframes),
            "params": self.params_dict,
            "params_hash": self.params_hash,
            "code_hash": self.code_hash,
        }
