import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    and_,
    case,
    cast,
    desc,
    exists,
    false,
    func,
    or_,
    select,
    text,
    true,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config.settings import (
    COST_MODEL_VERSION,
    DATABASE_URL,
    GIT_SHA,
    LATE_SIGNAL_GRACE_SECONDS,
    MACD_CANDLES,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    RISK_REWARD_RATIO,
    ROUND_TRIP_FEE_PERCENT,
    SHADOW_ALERTS,
    SIGNAL_EXPIRY_CANDLES,
    STOP_SLIPPAGE_MULTIPLIER,
    STRATEGY_KEY,
    STRATEGY_VERSION,
    SWING_LOOKBACK_CANDLES,
    slippage_bps_for,
)
from src.ledger.candles import kline_rows
from src.ledger.chain import (
    EVENT_TYPES,
    GENESIS_HASH,
    candles_sha256,
    canonical_json,
    event_core,
    event_hash,
    normalize,
    truncate_ms,
)
from src.services.performance import (
    DEFINITIONS,
    HISTOGRAM_BIN_R,
    MIN_BREAKDOWN_SAMPLE,
    ClosedTrade,
    breakdown,
    equity_curve,
    histogram,
    parse_utc,
    summarize_trades,
)
from src.strategy import registry
from src.strategy.base import ResolveContext, SignalIntent
from src.strategy.context import MarketContext, normalize_klines
from src.strategy.exits import EXIT_RESULTS, STOPPED, TIME_EXIT, cost_result
from src.strategy.macd import MacdSignal
from src.strategy.outcome import (
    CANDLE_INTERVAL_MS,
    TradeCosts,
    TradeOutcome,
    cost_adjusted_return,
    trade_return,
)

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None
_evaluation_lock = asyncio.Lock()

DEFAULT_WATCHLIST = [
    "BTCUSDT",
    "ETHUSDT"
]


# Where a stored signal came from. Only "cron" signals entered at the next open count as live
# results: "catchup" was detected late, "manual" came from an ad-hoc scan, and "legacy" rows were
# booked by earlier rules (interpolated entry or no fees)
SIGNAL_SOURCES = ("cron", "catchup", "manual", "legacy")
LEGACY_STRATEGY_VERSION = "legacy"
# The rules that produced every non-legacy row stored before signals carried their own version
BACKFILL_STRATEGY_VERSION = "2.2.0"
IS_ELIGIBLE_SQL = "COALESCE(source = 'cron' AND entry_basis = 'next_open', FALSE)"

# Who wrote a profitloss_history row. Only "evaluator" rows (the outcome the evaluator booked from
# candles) can count; "manual" rows came in through POST /api/profitloss-history, and "legacy" rows
# were booked without fees by the pre-v2.2 evaluator
TRADE_BOOKERS = ("evaluator", "manual", "legacy")
COUNTED_RESULTS = ("PROFIT", "LOSS", "EXPIRED", *EXIT_RESULTS)


class Base(DeclarativeBase):
    pass


def _round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


class WatchlistModel(Base):
    __tablename__ = "watchlist_symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "is_active": self.is_active,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class MacdSignalModel(Base):
    __tablename__ = "macd_signals"
    __table_args__ = (
        Index(
            "uq_macd_signals_strategy_candle",
            "strategy_key",
            "strategy_version",
            "symbol",
            "side",
            "timeframe",
            "cross_candle_ms",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), default="2h", index=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    cross_candle_ms: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    cross_time: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    cross_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tp1: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    risk_reward_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.5)
    win_status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    macd: Mapped[float] = mapped_column(Float, nullable=False)
    signal: Mapped[float] = mapped_column(Float, nullable=False)
    histogram: Mapped[float] = mapped_column(Float, nullable=False)
    bars_ago: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    # Provenance; nullable only until the boot backfill classifies rows stored before it existed
    strategy_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    entry_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # When the app saw the signal, from the application clock (created_at is the transaction start)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_eligible: Mapped[bool | None] = mapped_column(Boolean, Computed(IS_ELIGIBLE_SQL, persisted=True))
    # SHA-256 of the closed candles the strategy read when it detected the signal (src.ledger.candles);
    # NULL for rows stored before the ledger and for ad-hoc scans
    input_candles_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "timeframe": self.timeframe,
            "reason": self.reason,
            "cross_candle_ms": self.cross_candle_ms,
            "cross_time": self.cross_time,
            "cross_price": round(self.cross_price, 6),
            "close_price": round(self.close_price, 6),
            "entry_price": round(self.entry_price, 6),
            "stop_loss": round(self.stop_loss, 6),
            "tp1": round(self.tp1, 6),
            "risk_reward_ratio": round(self.risk_reward_ratio, 2),
            "win_status": self.win_status,
            "macd": round(self.macd, 6),
            "signal": round(self.signal, 6),
            "histogram": round(self.histogram, 6),
            "bars_ago": self.bars_ago,
            "is_confirmed": self.is_confirmed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "strategy_key": self.strategy_key,
            "strategy_version": self.strategy_version,
            "source": self.source,
            "entry_basis": self.entry_basis,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "is_eligible": bool(self.is_eligible),
            "input_candles_sha256": self.input_candles_sha256,
        }


class ProfitLossHistoryModel(Base):
    __tablename__ = "profitloss_history"
    __table_args__ = (
        Index("uq_profitloss_history_signal_id", "signal_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), default="2h", index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    tp1: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    result: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    risk_reward_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.5)
    cross_time: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    exit_time: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    # The trade restated under a versioned cost model; pnl_percent and r_multiple keep what was
    # booked at the time (net of the same model for trades the evaluator closes from cm1 on)
    gross_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_model_version: Mapped[str | None] = mapped_column(String(10), nullable=True)
    slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    booked_by: Mapped[str | None] = mapped_column(String(10), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 6),
            "stop_loss": round(self.stop_loss, 6),
            "tp1": round(self.tp1, 6),
            "exit_price": round(self.exit_price, 6),
            "result": self.result,
            "pnl_percent": round(self.pnl_percent, 2),
            "r_multiple": round(self.r_multiple, 2) if self.r_multiple is not None else None,
            "fee_percent": round(self.fee_percent or 0.0, 3),
            "risk_reward_ratio": round(self.risk_reward_ratio, 2),
            "cross_time": self.cross_time,
            "exit_time": self.exit_time,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "gross_r": _round_or_none(self.gross_r, 3),
            "cost_r": _round_or_none(self.cost_r, 3),
            "net_r": _round_or_none(self.net_r, 3),
            "cost_model_version": self.cost_model_version,
            "slippage_bps": self.slippage_bps,
            "entry_basis": self.entry_basis,
            "booked_by": self.booked_by,
        }


class TradeJournalModel(Base):
    """What the user actually did with a signal: taken or skipped, and at which fills."""

    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    entry_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "entry_fill": self.entry_fill,
            "exit_fill": self.exit_fill,
            "notes": self.notes,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CronRunModel(Base):
    """One scheduled scan: when it ran, for which 2H slot, and how it ended."""

    __tablename__ = "cron_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Open time (UTC ms) of the 2H candle the run was due for, i.e. the candle boundary it followed
    slot_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    signals_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evaluated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_cron_runs_status_finished", "status", "finished_at"),)


class StrategyModel(Base):
    """A strategy family; its versions, each with frozen rules, are in strategy_versions."""

    __tablename__ = "strategies"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategyVersionModel(Base):
    """One version of a strategy as registered in code. Once frozen, its hashes never change, and the
    scheduled scan refuses to emit for it unless the running code hashes the same."""

    __tablename__ = "strategy_versions"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    version: Mapped[str] = mapped_column(String(20), primary_key=True)
    params_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "version": self.version,
            "params": self.params_json,
            "params_hash": self.params_hash,
            "code_hash": self.code_hash,
            "status": self.status,
            "frozen": self.frozen,
            "frozen_at": self.frozen_at.isoformat() if self.frozen_at else None,
            "notes": self.notes,
        }


class ResearchTrialModel(Base):
    """One backtest run of one configuration: the multiple-testing log, so every configuration tried is
    counted when judging the one that is kept."""

    __tablename__ = "research_trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_key: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_desc: Mapped[str] = mapped_column(Text, nullable=False)
    date_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    metrics_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purpose: Mapped[str] = mapped_column(String(20), nullable=False, default="research", server_default="research")

    __table_args__ = (Index("ix_research_trials_strategy", "strategy_key", "version"),)


# Same shape as the models, for databases created before the tables existed
STRATEGY_REGISTRY_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS strategies (
        key VARCHAR(40) PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_versions (
        key VARCHAR(40) NOT NULL,
        version VARCHAR(20) NOT NULL,
        params_json JSONB NOT NULL,
        params_hash VARCHAR(64) NOT NULL,
        code_hash VARCHAR(64) NOT NULL,
        status VARCHAR(10) NOT NULL,
        frozen BOOLEAN NOT NULL DEFAULT FALSE,
        frozen_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        notes TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (key, version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS research_trials (
        id SERIAL PRIMARY KEY,
        strategy_key VARCHAR(40) NOT NULL,
        version VARCHAR(20) NOT NULL,
        params_hash VARCHAR(64) NOT NULL,
        dataset_desc TEXT NOT NULL,
        date_from DATE,
        date_to DATE,
        metrics_json JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        purpose VARCHAR(20) NOT NULL DEFAULT 'research'
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_research_trials_strategy ON research_trials (strategy_key, version);",
    # Tables create_all made before the models carried these server defaults get the DDL's defaults
    "ALTER TABLE strategies ALTER COLUMN description SET DEFAULT '';",
    "ALTER TABLE strategy_versions ALTER COLUMN frozen SET DEFAULT FALSE;",
    "ALTER TABLE strategy_versions ALTER COLUMN notes SET DEFAULT '';",
    "ALTER TABLE research_trials ALTER COLUMN purpose SET DEFAULT 'research';",
)


# --- Append-only signal ledger --------------------------------------------------------------------
# signal_events is the history of every signal, one hash-chained event per fact (src/ledger/chain.py).
# macd_signals and profitloss_history keep the current state for queries; the events say how it got
# there, and the verifier flags any row whose state differs from its latest event.


class SignalEventModel(Base):
    """One event of the global hash chain. Rows are never updated or deleted (a trigger refuses it)."""

    __tablename__ = "signal_events"
    __table_args__ = (
        Index("ix_signal_events_signal", "signal_id", "id"),
        # A linear chain: no two events may follow the same one, and no hash may repeat
        Index("uq_signal_events_prev_hash", "prev_hash", unique=True),
        Index("uq_signal_events_hash", "hash", unique=True),
        CheckConstraint(
            "event_type IN (" + ", ".join(f"'{t}'" for t in EVENT_TYPES) + ")", name="ck_signal_events_type"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Application clock at the event, to the millisecond; recorded_at is the database's insert time
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {
            **event_core(self.id, self.signal_id, self.event_type, self.occurred_at, self.payload),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


class LedgerHeadModel(Base):
    """The chain head published once per UTC day (the first scheduled run of the day writes it)."""

    __tablename__ = "ledger_heads"

    day: Mapped[date] = mapped_column("date", Date, primary_key=True)
    last_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "last_event_id": self.last_event_id,
            "head_hash": self.head_hash,
            "event_count": self.event_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SignalCandlesModel(Base):
    """The klines the evaluator settled a signal on, stored at resolution so its chart never changes.
    `candles` are [open_time_ms, open, high, low, close, volume] rows as Binance sent them, and
    `sha256` their canonical hash, which the signal's resolved event also carries."""

    __tablename__ = "signal_candles"

    # The signal's id (a natural key), not a sequence of its own
    signal_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    interval_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candle_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # The evaluation instant; a candle not closed by then is shown as partial
    resolved_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candles: Mapped[list] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProfitLossAdjustmentModel(Base):
    """A hand-entered trade (POST /api/profitloss-history). Kept apart from profitloss_history so it can
    never reach a statistic, an export of the ledger or the evaluator's slot for a signal."""

    __tablename__ = "profitloss_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False, default="2h", server_default="2h")
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    tp1: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0.0")
    risk_reward_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.5, server_default="1.5")
    cross_time: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    exit_time: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 6),
            "stop_loss": round(self.stop_loss, 6),
            "tp1": round(self.tp1, 6),
            "exit_price": round(self.exit_price, 6),
            "result": self.result,
            "pnl_percent": round(self.pnl_percent, 2),
            "risk_reward_ratio": round(self.risk_reward_ratio, 2),
            "cross_time": self.cross_time,
            "exit_time": self.exit_time,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # Same shape as a profitloss_history row: never restated, never counted
            "r_multiple": None,
            "gross_r": None,
            "cost_r": None,
            "net_r": None,
            "cost_model_version": None,
            "booked_by": "manual",
            "adjustment": True,
        }


APPEND_ONLY_TABLES = ("signal_events", "ledger_heads", "signal_candles")

# Same shape as the models, for databases created before the tables existed, plus what create_all
# cannot express: the append-only triggers. Every statement is idempotent
LEDGER_MIGRATIONS = (
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS input_candles_sha256 VARCHAR(64);",
    f"""
    CREATE TABLE IF NOT EXISTS signal_events (
        id BIGSERIAL PRIMARY KEY,
        signal_id INTEGER,
        event_type VARCHAR(20) NOT NULL,
        payload JSONB NOT NULL,
        occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
        prev_hash VARCHAR(64) NOT NULL,
        hash VARCHAR(64) NOT NULL,
        recorded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
        CONSTRAINT ck_signal_events_type CHECK (event_type IN ({", ".join(f"'{t}'" for t in EVENT_TYPES)}))
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_signal_events_signal ON signal_events (signal_id, id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_events_prev_hash ON signal_events (prev_hash);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_events_hash ON signal_events (hash);",
    """
    CREATE TABLE IF NOT EXISTS ledger_heads (
        date DATE PRIMARY KEY,
        last_event_id BIGINT NOT NULL,
        head_hash VARCHAR(64) NOT NULL,
        event_count BIGINT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_candles (
        signal_id INTEGER PRIMARY KEY,
        symbol VARCHAR(20) NOT NULL,
        timeframe VARCHAR(10) NOT NULL,
        interval_ms BIGINT NOT NULL,
        start_ms BIGINT NOT NULL,
        end_ms BIGINT NOT NULL,
        candle_count INTEGER NOT NULL,
        resolved_at_ms BIGINT NOT NULL,
        candles JSONB NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS profitloss_adjustments (
        id SERIAL PRIMARY KEY,
        signal_id INTEGER,
        symbol VARCHAR(20) NOT NULL,
        timeframe VARCHAR(10) NOT NULL DEFAULT '2h',
        direction VARCHAR(10) NOT NULL,
        entry_price DOUBLE PRECISION NOT NULL,
        stop_loss DOUBLE PRECISION NOT NULL,
        tp1 DOUBLE PRECISION NOT NULL,
        exit_price DOUBLE PRECISION NOT NULL,
        result VARCHAR(20) NOT NULL,
        pnl_percent DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        risk_reward_ratio DOUBLE PRECISION NOT NULL DEFAULT 1.5,
        cross_time VARCHAR(40) NOT NULL DEFAULT '',
        exit_time VARCHAR(40) NOT NULL DEFAULT '',
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    """,
    # Databases whose tables create_all made before the models carried these defaults: give them the
    # same defaults as the DDL above, and drop the sequence create_all once gave signal_candles.signal_id
    "ALTER TABLE profitloss_adjustments ALTER COLUMN timeframe SET DEFAULT '2h';",
    "ALTER TABLE profitloss_adjustments ALTER COLUMN pnl_percent SET DEFAULT 0.0;",
    "ALTER TABLE profitloss_adjustments ALTER COLUMN risk_reward_ratio SET DEFAULT 1.5;",
    "ALTER TABLE profitloss_adjustments ALTER COLUMN cross_time SET DEFAULT '';",
    "ALTER TABLE profitloss_adjustments ALTER COLUMN exit_time SET DEFAULT '';",
    "ALTER TABLE signal_candles ALTER COLUMN signal_id DROP DEFAULT;",
    "DROP SEQUENCE IF EXISTS signal_candles_signal_id_seq;",
    # Refuses every UPDATE, DELETE and TRUNCATE on the ledger tables through normal statements. It is
    # not tamper-proof: the tables' owner (the app's own role, which recreates these triggers on every
    # boot) or a superuser can disable a trigger. A changed or removed event in the middle of the chain
    # breaks it; removing the newest events, or rewriting the chain together with ledger_heads, is only
    # caught against a head copied outside this database (python -m src.ledger verify --head HASH)
    """
    CREATE OR REPLACE FUNCTION crossline_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION '% on % is not allowed: the table is append-only', TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql;
    """,
    *(
        statement
        for table in APPEND_ONLY_TABLES
        for statement in (
            f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};",
            f"""
            CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION crossline_append_only();
            """,
            f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table};",
            f"""
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION crossline_append_only();
            """,
        )
    ),
)

# pg_advisory_xact_lock key serializing ledger writes, so every append sees the latest head
LEDGER_LOCK_KEY = 0x63726F73
LEDGER_LOCK_SQL = f"SELECT pg_advisory_xact_lock({LEDGER_LOCK_KEY})"
NEXT_EVENT_ID_SQL = "SELECT nextval(pg_get_serial_sequence('signal_events', 'id'))"

# What an event records of a signal and of its booked trade; the verifier compares the live rows to
# the latest event that carries them
SIGNAL_SNAPSHOT_FIELDS = (
    "symbol",
    "side",
    "timeframe",
    "cross_candle_ms",
    "entry_price",
    "stop_loss",
    "tp1",
    "win_status",
    "strategy_key",
    "strategy_version",
    "source",
    "entry_basis",
)
TRADE_SNAPSHOT_FIELDS = (
    "result",
    "exit_price",
    "exit_time",
    "gross_r",
    "cost_r",
    "net_r",
    "cost_model_version",
    "booked_by",
)


@dataclass
class EventDraft:
    """An event to append: the chain assigns its id, prev_hash and hash."""

    signal_id: int | None
    event_type: str
    payload: dict
    occurred_at: datetime | None = None


def _field(obj, name: str):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def signal_snapshot(rec) -> dict:
    return {name: _field(rec, name) for name in SIGNAL_SNAPSHOT_FIELDS}


def trade_snapshot(trade) -> dict | None:
    if trade is None:
        return None
    return {name: _field(trade, name) for name in TRADE_SNAPSHOT_FIELDS}


def exclusion_reason(source: str | None, entry_basis: str | None) -> str | None:
    """Why a signal is kept out of its own strategy version's track record; None when it counts."""
    if source == "cron" and entry_basis == "next_open":
        return None
    if source == "legacy":
        return "legacy"
    if source in ("catchup", "manual"):
        return source
    return "other"


def _json_safe(value):
    """`value` normalized for hashing, or its text when it cannot be (a strategy's free-form features)."""
    try:
        return normalize(value)
    except (TypeError, ValueError):
        return repr(value)


def settings_params() -> dict:
    """The parameters an ad-hoc scan runs with: the settings, laid out like macd_cross's frozen ones."""
    return {
        "macd_fast": MACD_FAST,
        "macd_slow": MACD_SLOW,
        "macd_signal": MACD_SIGNAL,
        "macd_candles": MACD_CANDLES,
        "risk_reward_ratio": RISK_REWARD_RATIO,
        "swing_lookback_candles": SWING_LOOKBACK_CANDLES,
        "signal_expiry_candles": SIGNAL_EXPIRY_CANDLES,
    }


def signal_provenance(signal) -> dict:
    """Which rules produced a signal: the frozen parameters and code of a registered strategy, or the
    settings an ad-hoc scan used (whose hash then equals the frozen one only if nothing was overridden)."""
    key, version = signal_strategy(signal)
    strategy = registry.lookup(key, version)
    if isinstance(signal, SignalIntent) and strategy is not None:
        return {
            "strategy_ref": strategy.ref,
            "params_source": "frozen",
            "params_hash": strategy.params_hash,
            "code_hash": strategy.code_hash,
        }
    params = settings_params()
    return {
        "strategy_ref": f"{key}@{version}",
        "params_source": "settings",
        "params": params,
        "params_hash": hashlib.sha256(canonical_json(params).encode()).hexdigest(),
        "code_hash": strategy.code_hash if strategy is not None else None,
    }


def detected_payload(rec, signal) -> dict:
    """The detected event of a newly stored signal: its levels and provenance, as stored."""
    return {
        "signal": signal_snapshot(rec),
        "reason": _field(rec, "reason"),
        "cross_time": _field(rec, "cross_time"),
        "cross_price": _field(rec, "cross_price"),
        "close_price": _field(rec, "close_price"),
        "risk_reward_ratio": _field(rec, "risk_reward_ratio"),
        "macd": _field(rec, "macd"),
        "signal_line": _field(rec, "signal"),
        "histogram": _field(rec, "histogram"),
        "bars_ago": _field(rec, "bars_ago"),
        "is_confirmed": _field(rec, "is_confirmed"),
        "detected_at": _field(rec, "detected_at"),
        **signal_provenance(signal),
        "git_sha": GIT_SHA,
        "input_candles_sha256": getattr(signal, "input_candles_sha256", None),
        "input_candles": _json_safe(getattr(signal, "input_candles", None)),
        "features": _json_safe(getattr(signal, "features", None) or {}),
    }


async def append_signal_events(session: AsyncSession, drafts) -> list[dict]:
    """Append events to the chain inside the caller's transaction, which must commit them.

    The advisory lock is held until that transaction ends, so concurrent writers line up and every
    event links to the one committed before it. Returns the events as hashed.
    """
    drafts = list(drafts)
    if not drafts:
        return []
    await session.execute(text(LEDGER_LOCK_SQL))
    head = (
        await session.execute(select(SignalEventModel.hash).order_by(SignalEventModel.id.desc()).limit(1))
    ).scalar_one_or_none()
    prev_hash = head or GENESIS_HASH
    written = []
    for draft in drafts:
        if draft.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {draft.event_type!r}")
        event_id = (await session.execute(text(NEXT_EVENT_ID_SQL))).scalar_one_or_none()
        occurred_at = truncate_ms(draft.occurred_at or datetime.now(UTC))
        core = event_core(event_id, draft.signal_id, draft.event_type, occurred_at, draft.payload)
        digest = event_hash(prev_hash, core)
        await session.execute(
            pg_insert(SignalEventModel).values(
                id=event_id,
                signal_id=draft.signal_id,
                event_type=draft.event_type,
                payload=core["payload"],
                occurred_at=occurred_at,
                prev_hash=prev_hash,
                hash=digest,
            )
        )
        written.append({**core, "prev_hash": prev_hash, "hash": digest})
        prev_hash = digest
    return written


def genesis_drafts(sig, trade) -> list[EventDraft]:
    """Events opening the history of a signal stored before the ledger (or, should one ever lack
    them, outside it): a restated snapshot of the row as Phase 0 classified and restated it, plus
    an excluded event when it does not count."""
    restatement = None
    if trade is not None:
        restatement = {
            "cost_model_version": trade.cost_model_version,
            "booked_r_multiple": trade.r_multiple,
            "booked_pnl_percent": trade.pnl_percent,
            "fee_percent": trade.fee_percent,
            "slippage_bps": trade.slippage_bps,
            "entry_basis": trade.entry_basis,
        }
    payload = {
        "genesis": True,
        "reason": "ledger_genesis",
        "signal": signal_snapshot(sig),
        "trade": trade_snapshot(trade),
        "restatement": restatement,
        "cross_time": sig.cross_time,
        "cross_price": sig.cross_price,
        "bars_ago": sig.bars_ago,
        "is_confirmed": sig.is_confirmed,
        "detected_at": sig.detected_at,
        "created_at": sig.created_at,
        "is_eligible": bool(sig.is_eligible),
        "input_candles_sha256": sig.input_candles_sha256,
    }
    drafts = [EventDraft(sig.id, "restated", payload)]
    reason = exclusion_reason(sig.source, sig.entry_basis)
    if reason is not None:
        drafts.append(
            EventDraft(
                sig.id,
                "excluded",
                {
                    "genesis": True,
                    "reason": reason,
                    "source": sig.source,
                    "entry_basis": sig.entry_basis,
                    "strategy_version": sig.strategy_version,
                },
            )
        )
    return drafts


async def emit_genesis_events(session: AsyncSession) -> int:
    """Give every signal stored before the ledger its genesis events (see genesis_drafts). Called on
    every boot, but writes only once: on the boot that installs the ledger, while signal_events is
    still empty. From then on every signal gets its detected event in the transaction that stores
    it, so a signal without events was put in the table outside the app. It is never given a genesis
    afterwards: verification lists it (rows.without_events) and the statistics leave it out
    (outside_ledger). Returns the number of events written."""
    sig, pl, ev = MacdSignalModel, ProfitLossHistoryModel, SignalEventModel
    try:
        # Locked before the lookup, so two processes booting at once cannot both write a genesis
        await session.execute(text(LEDGER_LOCK_SQL))
        if (await session.execute(select(ev.id).limit(1))).first() is not None:
            outside = (
                await session.execute(select(func.count(sig.id)).where(~exists().where(ev.signal_id == sig.id)))
            ).scalar_one() or 0
            await session.commit()
            if outside:
                logger.error(
                    "%d signals have no ledger events: they were stored outside the ledger, so they are not "
                    "counted and /api/ledger/verify reports them (rows.without_events)",
                    outside,
                )
            return 0
        rows = (
            await session.execute(
                select(sig, pl)
                .outerjoin(pl, pl.signal_id == sig.id)
                .where(~exists().where(ev.signal_id == sig.id))
                .order_by(sig.id)
            )
        ).all()
        drafts = [draft for s, trade in rows for draft in genesis_drafts(s, trade)]
        written = await append_signal_events(session, drafts)
        await session.commit()
        if written:
            logger.info("Ledger genesis: %d events for %d signals stored before the ledger", len(written), len(rows))
        return len(written)
    except Exception:
        logger.exception("Writing the ledger genesis events failed")
        await session.rollback()
        return 0


async def record_alert_events(signal_ids, kind: str, message: str) -> bool:
    """Append an alert_sent event per signal a delivered Telegram message announced. Never raises:
    the message is out either way."""
    ids = [int(i) for i in dict.fromkeys(signal_ids or []) if i is not None]
    if _session_maker is None or not ids:
        return False
    sent_at = datetime.now(UTC)
    payload = {
        "channel": "telegram",
        "kind": kind,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "signals_in_message": len(ids),
    }
    try:
        async with _session_maker() as session:
            await append_signal_events(session, [EventDraft(i, "alert_sent", payload, sent_at) for i in ids])
            await session.commit()
        return True
    except Exception:
        logger.exception("Failed to record alert_sent events for signals %s", ids)
        return False


async def record_daily_ledger_head(now: datetime | None = None) -> dict | None:
    """Publish today's (UTC) chain head once: the first call of the day writes it, later ones keep it.
    Returns the row written, or None (already written, empty chain, or no database)."""
    if _session_maker is None:
        return None
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    try:
        async with _session_maker() as session:
            await session.execute(text(LEDGER_LOCK_SQL))
            if await session.get(LedgerHeadModel, today) is not None:
                return None
            head = (
                await session.execute(
                    select(SignalEventModel.id, SignalEventModel.hash).order_by(SignalEventModel.id.desc()).limit(1)
                )
            ).first()
            if head is None:
                return None
            count = (await session.execute(select(func.count(SignalEventModel.id)))).scalar_one()
            row = {"day": today, "last_event_id": head.id, "head_hash": head.hash, "event_count": count}
            await session.execute(pg_insert(LedgerHeadModel).values(**row).on_conflict_do_nothing(index_elements=["date"]))
            await session.commit()
            logger.info("Published ledger head for %s: event %s, %s", today, head.id, head.hash)
            return {**row, "date": today.isoformat()}
    except Exception:
        logger.exception("Failed to publish the ledger head for %s", today)
        return None


CRON_RUN_STATUSES = ("ok", "error", "skipped")
CRON_RUN_ERROR_MAX_LENGTH = 1000

# Same shape as the model, for databases created before the table existed; create_all already
# covers fresh ones, so both statements are no-ops after the first boot
CRON_RUNS_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS cron_runs (
        id SERIAL PRIMARY KEY,
        slot_ms BIGINT NOT NULL,
        started_at TIMESTAMP WITH TIME ZONE NOT NULL,
        finished_at TIMESTAMP WITH TIME ZONE,
        status VARCHAR(10) NOT NULL,
        signals_count INTEGER NOT NULL DEFAULT 0,
        evaluated_count INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_cron_runs_status_finished ON cron_runs (status, finished_at);",
)


# One row per strategy version per cross: several strategies (a live one and its shadows) can each
# record a signal on the same candle
SIGNAL_UNIQUE_COLUMNS = ("strategy_key", "strategy_version", "symbol", "side", "timeframe", "cross_candle_ms")
LEGACY_SIGNAL_UNIQUE_INDEX = "uq_macd_signals_symbol_side_tf_candle"

# Rows stored before provenance existed have no strategy yet; they were all macd_cross, which the
# provenance backfill writes later, and until then they compare as one strategy
SIGNAL_SURVIVOR_RANK = """
    SELECT id,
           ROW_NUMBER() OVER w AS rn,
           FIRST_VALUE(id) OVER w AS keep_id
    FROM macd_signals
    WINDOW w AS (
        PARTITION BY COALESCE(strategy_key, 'macd_cross'), COALESCE(strategy_version, ''),
                     symbol, side, timeframe, cross_candle_ms
        ORDER BY (COALESCE(win_status, 'PENDING') <> 'PENDING') DESC, id ASC
    )
"""

UNIQUE_INDEX_MIGRATIONS = (
    # The dedup below partitions by strategy, so the columns must exist before the provenance
    # migration adds the rest
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS strategy_key VARCHAR(40);",
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS strategy_version VARCHAR(20);",
    f"""
    UPDATE profitloss_history AS p
    SET signal_id = r.keep_id
    FROM ({SIGNAL_SURVIVOR_RANK}) AS r
    WHERE p.signal_id = r.id
      AND r.rn > 1;
    """,
    f"""
    DELETE FROM macd_signals AS m
    USING ({SIGNAL_SURVIVOR_RANK}) AS r
    WHERE m.id = r.id
      AND r.rn > 1;
    """,
    """
    DELETE FROM profitloss_history AS p
    USING (
        SELECT pl.id, ROW_NUMBER() OVER (
            PARTITION BY pl.signal_id
            ORDER BY (
                (s.win_status = 'TP1_HIT' AND pl.result = 'PROFIT')
                OR (s.win_status = 'SL_HIT' AND pl.result = 'LOSS')
            ) IS TRUE DESC, pl.id ASC
        ) AS rn
        FROM profitloss_history pl
        LEFT JOIN macd_signals s ON s.id = pl.signal_id
        WHERE pl.signal_id IS NOT NULL
    ) AS r
    WHERE p.id = r.id
      AND r.rn > 1;
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_macd_signals_strategy_candle
    ON macd_signals (strategy_key, strategy_version, symbol, side, timeframe, cross_candle_ms);
    """,
    # The index before multi-strategy, one row per cross whatever the strategy; the new index is
    # created first, so a cross is never left without a uniqueness guard
    f"DROP INDEX IF EXISTS {LEGACY_SIGNAL_UNIQUE_INDEX};",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_profitloss_history_signal_id
    ON profitloss_history (signal_id);
    """,
    "DROP INDEX IF EXISTS ix_profitloss_history_signal_id;",
)

PROFITLOSS_R_MIGRATIONS = (
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS r_multiple DOUBLE PRECISION;",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS fee_percent DOUBLE PRECISION NOT NULL DEFAULT 0.0;",
    # Rows written before R tracking: derive R from the stored PnL and the planned risk
    """
    UPDATE profitloss_history
    SET r_multiple = pnl_percent / (ABS(entry_price - stop_loss) / entry_price * 100)
    WHERE r_multiple IS NULL AND entry_price > 0 AND stop_loss <> entry_price;
    """,
)

# An entry within this relative distance of the cross price was the interpolated cross itself,
# not a fill anyone could get
CROSS_PRICE_TOLERANCE = 1e-9

# Candle length in ms for a stored row's timeframe, as SQL
_INTERVAL_MS_SQL = (
    "CASE m.timeframe "
    + " ".join(f"WHEN '{tf}' THEN {ms}" for tf, ms in CANDLE_INTERVAL_MS.items())
    + f" ELSE {CANDLE_INTERVAL_MS['2h']} END"
)

PROVENANCE_MIGRATIONS = (
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS strategy_key VARCHAR(40);",
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS strategy_version VARCHAR(20);",
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS source VARCHAR(10);",
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS entry_basis VARCHAR(20);",
    "ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS detected_at TIMESTAMP WITH TIME ZONE;",
    f"""
    ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS is_eligible BOOLEAN
    GENERATED ALWAYS AS ({IS_ELIGIBLE_SQL}) STORED;
    """,
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS gross_r DOUBLE PRECISION;",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS cost_r DOUBLE PRECISION;",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS net_r DOUBLE PRECISION;",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS cost_model_version VARCHAR(10);",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS slippage_bps DOUBLE PRECISION;",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS entry_basis VARCHAR(20);",
    "ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS booked_by VARCHAR(10);",
    # Before this column only the evaluator charged fees, and POST /api/profitloss-history never
    # did, so a row with fees is the evaluator's; a fee-free row is a legacy booking
    """
    UPDATE profitloss_history
    SET booked_by = CASE WHEN fee_percent > 0 THEN 'evaluator' ELSE 'legacy' END
    WHERE booked_by IS NULL;
    """,
    # Classify signals stored before provenance existed. Every statement only fills NULLs, so
    # running them on each boot never rewrites a row twice. An entry equal to the cross price
    # (or missing) was the interpolated cross, which pre-v2.2 code booked as the entry
    f"""
    UPDATE macd_signals
    SET entry_basis = CASE
        WHEN entry_price IS NULL OR entry_price <= 0
             OR ABS(entry_price - cross_price) <= {CROSS_PRICE_TOLERANCE} * ABS(cross_price) THEN 'cross_interp'
        WHEN is_confirmed IS FALSE THEN 'forming_close'
        ELSE 'next_open'
    END
    WHERE entry_basis IS NULL;
    """,
    # Legacy: interpolated entry, or a result booked without fees by the pre-v2.2 evaluator.
    # Catch-up: found after newer candles had already closed, or stored more than the grace period
    # after its entry candle opened (the runtime rule in cron_scheduler.is_late_signal). Manual:
    # entered at the forming candle's close, which only an ad-hoc scan of the forming candle does
    f"""
    UPDATE macd_signals AS m
    SET source = CASE
        WHEN m.entry_basis = 'cross_interp' OR EXISTS (
            SELECT 1 FROM profitloss_history p
            WHERE p.signal_id = m.id AND COALESCE(p.fee_percent, 0) = 0
        ) THEN 'legacy'
        WHEN m.bars_ago > 0 THEN 'catchup'
        WHEN m.created_at > to_timestamp(
            (m.cross_candle_ms + {_INTERVAL_MS_SQL}) / 1000.0
        ) + interval '{LATE_SIGNAL_GRACE_SECONDS} seconds' THEN 'catchup'
        WHEN m.entry_basis = 'forming_close' THEN 'manual'
        ELSE 'cron'
    END
    WHERE m.source IS NULL;
    """,
    "UPDATE macd_signals SET strategy_key = 'macd_cross' WHERE strategy_key IS NULL;",
    f"""
    UPDATE macd_signals
    SET strategy_version = CASE
        WHEN source = 'legacy' THEN '{LEGACY_STRATEGY_VERSION}' ELSE '{BACKFILL_STRATEGY_VERSION}'
    END
    WHERE strategy_version IS NULL;
    """,
    "UPDATE macd_signals SET detected_at = created_at WHERE detected_at IS NULL;",
)

UTC_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime(UTC_ISO)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def utc_day_bounds(
    start_date: str | None, end_date: str | None, tz_offset_minutes: int = 0
) -> tuple[datetime | None, datetime | None]:
    """Half-open UTC range for YYYY-MM-DD filters: from start_date 00:00 up to, not including, the
    day after end_date, so end_date covers its whole day. The days are local days at
    `tz_offset_minutes` east of UTC (420 for WIB), returned as UTC. Raises ValueError for any other format."""
    offset = timedelta(minutes=tz_offset_minutes)

    def day_start(value: str, name: str) -> datetime:
        try:
            return datetime.combine(date.fromisoformat(value.strip()), time.min, tzinfo=UTC) - offset
        except ValueError:
            raise ValueError(f"{name} must be a date in YYYY-MM-DD format") from None

    start = day_start(start_date, "start_date") if start_date else None
    end = day_start(end_date, "end_date") + timedelta(days=1) if end_date else None
    return start, end


# Stored UTC time text, "2026-10-13T00:00:00Z" or the older "2026-10-13 00:00:00"
_UTC_TEXT_PATTERN = r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?$"


def _utc_text_as_timestamp(column):
    """A text time column as a UTC timestamp, so both stored formats compare by time rather than
    as strings ("T" sorts after " "); NULL for text that is not a timestamp."""
    normalized = func.replace(func.replace(column, "T", " "), "Z", "")
    return case((column.regexp_match(_UTC_TEXT_PATTERN), cast(normalized, DateTime)), else_=None)


def classify_entry_basis(entry_price: float | None, cross_price: float, is_confirmed: bool = True) -> str:
    """Python twin of the entry_basis backfill, for rows that reach the evaluator unclassified."""
    if (
        entry_price is None
        or entry_price <= 0
        or abs(entry_price - cross_price) <= CROSS_PRICE_TOLERANCE * abs(cross_price)
    ):
        return "cross_interp"
    return "forming_close" if is_confirmed is False else "next_open"


def signal_strategy(signal: MacdSignal) -> tuple[str, str]:
    """(strategy_key, strategy_version) a signal is stored under: its own when a registered strategy
    produced it (a SignalIntent), else the configured headline version (ad-hoc scans)."""
    key = getattr(signal, "strategy_key", None) or STRATEGY_KEY
    version = getattr(signal, "strategy_version", None) or STRATEGY_VERSION
    return key, version


def build_signal_insert(
    signal: MacdSignal, timeframe: str, source: str = "manual", detected_at: datetime | None = None
):
    if source not in SIGNAL_SOURCES:
        raise ValueError(f"unknown signal source {source!r}")
    strategy_key, strategy_version = signal_strategy(signal)
    return (
        pg_insert(MacdSignalModel)
        .values(
            symbol=signal.symbol,
            side=signal.side,
            timeframe=timeframe,
            reason=signal.reason,
            cross_candle_ms=signal.cross_candle_ms,
            cross_time=signal.cross_time,
            cross_price=signal.cross_price,
            close_price=signal.close,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            risk_reward_ratio=signal.risk_reward_ratio,
            win_status=signal.win_status,
            macd=signal.macd,
            signal=signal.signal,
            histogram=signal.histogram,
            bars_ago=signal.bars_ago,
            is_confirmed=signal.is_confirmed,
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            source=source,
            entry_basis=signal.entry_basis,
            detected_at=detected_at or datetime.now(UTC),
            input_candles_sha256=getattr(signal, "input_candles_sha256", None),
        )
        .on_conflict_do_nothing(index_elements=list(SIGNAL_UNIQUE_COLUMNS))
        .returning(MacdSignalModel.id)
    )


def build_profitloss_insert(**values):
    return (
        pg_insert(ProfitLossHistoryModel)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["signal_id"])
        .returning(ProfitLossHistoryModel.id)
    )


def in_ledger():
    """The signal has at least one ledger event. Every signal the app stores gets its detected event
    in the same transaction (and those stored before the ledger got a genesis), so one without events
    was put in the table outside the app and never counts."""
    return exists().where(SignalEventModel.signal_id == MacdSignalModel.id)


def counted_signal(version: str = STRATEGY_VERSION, key: str = STRATEGY_KEY):
    """A signal that counts in the track record of `key`@`version`: on time, entered at the next open,
    recorded in the ledger. Other strategies (shadow ones included) never mix into it, even under the
    same version number."""
    sig = MacdSignalModel
    return and_(sig.is_eligible.is_(True), sig.strategy_key == key, sig.strategy_version == version, in_ledger())


def not_counted_signal(version: str = STRATEGY_VERSION, key: str = STRATEGY_KEY):
    sig = MacdSignalModel
    return or_(
        sig.is_eligible.is_not(True),
        sig.strategy_key.is_distinct_from(key),
        sig.strategy_version.is_distinct_from(version),
        ~in_ledger(),
    )


def headline_family():
    """Signals of the headline strategy key, every version (legacy rows included). The deprecated
    aggregates of the listing endpoints are kept to these, so a shadow strategy's paper trades never
    read as part of the live record there."""
    sig = MacdSignalModel
    return or_(sig.strategy_key == STRATEGY_KEY, sig.strategy_key.is_(None))


def counted_trade():
    """A closed trade row that can count: booked by the evaluator from candles, with a known result and
    a net R. Rows typed in through POST /api/profitloss-history never count."""
    pl = ProfitLossHistoryModel
    return and_(
        pl.booked_by == "evaluator",
        pl.result.in_(COUNTED_RESULTS),
        pl.net_r.is_not(None),
        MacdSignalModel.win_status != "PENDING",
    )


async def seed_default_watchlist(session: AsyncSession) -> None:
    res = await session.execute(select(func.count(WatchlistModel.id)))
    count = res.scalar_one() or 0
    if count == 0:
        for sym in DEFAULT_WATCHLIST:
            session.add(WatchlistModel(symbol=sym, is_active=True, notes="Default Pair"))
        await session.commit()
        logger.info("Default watchlist seeded with %d symbols", len(DEFAULT_WATCHLIST))


def booking_entry(rec) -> tuple[float, str]:
    """Entry price a closing signal is booked at, and how that price was obtained.

    A signal stored without a real entry, or with the interpolated cross as its entry, is still
    booked so it leaves the open list, but it is labelled cross_interp and moved to the legacy
    source, so it never counts as a live result.
    """
    if rec.entry_price is not None and rec.entry_price > 0:
        entry = rec.entry_price
        basis = rec.entry_basis or classify_entry_basis(rec.entry_price, rec.cross_price, rec.is_confirmed)
    else:
        entry, basis = rec.cross_price, "cross_interp"
    if basis == "cross_interp":
        rec.entry_basis = basis
        rec.source = "legacy"
        rec.strategy_version = LEGACY_STRATEGY_VERSION
    return entry, basis


def trade_costs(
    symbol: str, side: str, entry: float, stop_loss: float, exit_price: float, result: str
) -> tuple[TradeCosts, dict]:
    """The trade under the current cost model, plus the profitloss_history columns that record it."""
    slippage = slippage_bps_for(symbol)
    costs = cost_adjusted_return(
        side, entry, stop_loss, exit_price, cost_result(result), ROUND_TRIP_FEE_PERCENT, slippage,
        STOP_SLIPPAGE_MULTIPLIER,
    )
    columns = {
        "gross_r": costs.gross_r,
        "cost_r": costs.cost_r,
        "net_r": costs.net_r,
        "cost_model_version": COST_MODEL_VERSION,
        "slippage_bps": slippage,
    }
    return costs, columns


def booked_trade_values(rec, entry: float, entry_basis: str, exit_price: float, result: str, exit_time: str) -> dict:
    """profitloss_history row for a signal closing now: pnl_percent and r_multiple are net of
    the cost model (fees and modelled slippage), gross_r keeps the result at the planned levels."""
    costs, columns = trade_costs(rec.symbol, rec.side, entry, rec.stop_loss, exit_price, result)
    return {
        "signal_id": rec.id,
        "symbol": rec.symbol,
        "timeframe": rec.timeframe,
        "direction": rec.side,
        "entry_price": entry,
        "stop_loss": rec.stop_loss,
        "tp1": rec.tp1,
        "exit_price": exit_price,
        "result": result,
        "pnl_percent": costs.pnl_percent_net,
        "r_multiple": costs.net_r,
        "fee_percent": ROUND_TRIP_FEE_PERCENT,
        "risk_reward_ratio": rec.risk_reward_ratio,
        "cross_time": rec.cross_time,
        "exit_time": exit_time,
        "entry_basis": entry_basis,
        "booked_by": "evaluator",
        **columns,
    }


async def backfill_resolved_signals_to_profitloss_history(session: AsyncSession) -> None:
    """Book a trade for resolved signals that have none (rows from before profitloss_history).

    Before the ledger's genesis this is part of what the genesis snapshot records. A signal that
    already has events gets a restated event for the booking instead, so its row never drifts from
    its latest event.
    """
    try:
        stmt = select(MacdSignalModel).where(
            MacdSignalModel.win_status.in_(["TP1_HIT", "SL_HIT"]),
            ~exists().where(ProfitLossHistoryModel.signal_id == MacdSignalModel.id),
        )
        resolved = (await session.execute(stmt)).scalars().all()
        booked = []
        for rec in resolved:
            is_win = rec.win_status == "TP1_HIT"
            entry, basis = booking_entry(rec)
            values = booked_trade_values(
                rec,
                entry,
                basis,
                exit_price=rec.tp1 if is_win else rec.stop_loss,
                result="PROFIT" if is_win else "LOSS",
                # The real exit time is unknown for these rows
                exit_time=rec.cross_time,
            )
            inserted = (await session.execute(build_profitloss_insert(**values))).scalar_one_or_none()
            if inserted is not None:
                booked.append((rec, values))
        if booked:
            with_events = set(
                (
                    await session.execute(
                        select(SignalEventModel.signal_id)
                        .where(SignalEventModel.signal_id.in_([rec.id for rec, _ in booked]))
                        .distinct()
                    )
                ).scalars().all()
            )
            await append_signal_events(
                session,
                [
                    EventDraft(
                        rec.id,
                        "restated",
                        {
                            "genesis": False,
                            "reason": "resolved_signal_backfill",
                            "detail": "booked at its planned level; the real exit time is unknown",
                            "signal": signal_snapshot(rec),
                            "trade": trade_snapshot(values),
                        },
                    )
                    for rec, values in booked
                    if rec.id in with_events
                ],
            )
        await session.commit()
    except Exception as e:
        logger.warning("Backfill profitloss_history error: %s", e)


async def backfill_profitloss_costs(session: AsyncSession) -> None:
    """Restate closed trades booked before the cost model with gross, cost and net R.

    Runs on every boot but only touches rows that have no cost model version yet. The trade is
    re-priced from its signal's levels when the signal has a real entry, else from the levels on
    the row. Legacy rows get the same treatment, so their net R carries fees and slippage they were
    originally booked without. Hand-entered rows are left alone: their exit is whatever was typed in.
    pnl_percent and r_multiple are left as they were recorded.
    """
    pl, sig = ProfitLossHistoryModel, MacdSignalModel
    try:
        rows = (
            await session.execute(
                select(pl, sig)
                .outerjoin(sig, sig.id == pl.signal_id)
                .where(pl.cost_model_version.is_(None), pl.booked_by.is_distinct_from("manual"))
            )
        ).all()
        for trade, signal in rows:
            if signal is not None and signal.entry_price is not None and signal.entry_price > 0:
                side, entry, stop_loss = signal.side, signal.entry_price, signal.stop_loss
            else:
                side, entry, stop_loss = trade.direction, trade.entry_price, trade.stop_loss
            _, columns = trade_costs(trade.symbol, side, entry, stop_loss, trade.exit_price, trade.result)
            for name, value in columns.items():
                setattr(trade, name, value)
            trade.entry_basis = signal.entry_basis if signal is not None else None
        await session.commit()
        if rows:
            logger.info("Restated %d closed trades under cost model %s", len(rows), COST_MODEL_VERSION)
    except Exception:
        logger.exception("Backfill of profitloss_history costs failed")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _note(row: StrategyVersionModel, text_: str) -> None:
    stamp = datetime.now(UTC).strftime(UTC_ISO)
    row.notes = f"{row.notes}\n{stamp} {text_}".strip()


async def sync_strategy_registry(session: AsyncSession, lock: dict[str, dict] | None = None) -> None:
    """Mirror the registered strategies into strategies / strategy_versions.

    New versions are inserted, frozen with the lock file's hashes when the lock has them. Existing
    rows follow the code's status. A frozen row never changes its hashes, except a code_hash that the
    lock lists under code_hash_history (a refactor proven not to change results), which moves to the
    lock's current code_hash with a note. Unfrozen rows track the code, and freeze once the lock
    has them. Errors are logged; the scheduled scan then refuses the versions it cannot verify.
    """
    lock = registry.load_lock() if lock is None else lock
    try:
        for strategy in registry.all_strategies():
            await session.execute(
                pg_insert(StrategyModel)
                .values(key=strategy.key, name=strategy.name, description=strategy.description)
                .on_conflict_do_update(
                    index_elements=["key"], set_={"name": strategy.name, "description": strategy.description}
                )
            )
            entry = lock.get(strategy.ref)
            row = await session.get(StrategyVersionModel, (strategy.key, strategy.version))
            if row is None:
                session.add(
                    StrategyVersionModel(
                        key=strategy.key,
                        version=strategy.version,
                        params_json=strategy.params_dict,
                        params_hash=entry["params_hash"] if entry else strategy.params_hash,
                        code_hash=entry["code_hash"] if entry else strategy.code_hash,
                        status=strategy.status,
                        frozen=entry is not None,
                        frozen_at=_parse_utc(entry.get("frozen_at")) if entry else None,
                        notes=(entry or {}).get("notes", ""),
                    )
                )
                continue
            if row.status != strategy.status:
                _note(row, f"status {row.status} -> {strategy.status}")
                row.status = strategy.status
            if not row.frozen:
                row.params_json = strategy.params_dict
                row.params_hash = entry["params_hash"] if entry else strategy.params_hash
                row.code_hash = entry["code_hash"] if entry else strategy.code_hash
                if entry is not None:
                    row.frozen = True
                    row.frozen_at = _parse_utc(entry.get("frozen_at")) or datetime.now(UTC)
                    _note(row, "frozen")
            elif entry is None:
                logger.warning("%s is frozen in the database but missing from the lock file", strategy.ref)
            elif entry["code_hash"] != row.code_hash:
                superseded = {h["code_hash"]: h for h in entry.get("code_hash_history", [])}
                if row.code_hash in superseded and entry["params_hash"] == row.params_hash:
                    _note(row, f"code_hash {row.code_hash[:12]} -> {entry['code_hash'][:12]}: "
                               f"{superseded[row.code_hash].get('reason', '')}")
                    row.code_hash = entry["code_hash"]
                else:
                    logger.error(
                        "%s: the lock's code_hash differs from the frozen one and is not a recorded rehash",
                        strategy.ref,
                    )
        await session.commit()
    except Exception:
        logger.exception("Syncing the strategy registry failed")
        await session.rollback()


async def get_strategy_versions() -> dict[str, dict] | None:
    """Stored strategy_versions rows by "key@version"; None without a database or when it cannot be
    read, in which case the caller falls back to the lock file."""
    if _session_maker is None:
        return None
    try:
        async with _session_maker() as session:
            rows = (await session.execute(select(StrategyVersionModel))).scalars().all()
            return {f"{r.key}@{r.version}": r.to_dict() for r in rows}
    except Exception as e:
        logger.error("Failed to read strategy_versions: %s", e)
        return None


async def insert_research_trial(trial: dict, database_url: str) -> int:
    """Store one backtest run in research_trials (creating the table if needed) with a short-lived
    connection, for the harness CLI. Returns the new row id."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            for migration in STRATEGY_REGISTRY_MIGRATIONS:
                await conn.execute(text(migration))
            row_id = (
                await conn.execute(
                    pg_insert(ResearchTrialModel)
                    .values(
                        strategy_key=trial["strategy_key"],
                        version=trial["version"],
                        params_hash=trial["params_hash"],
                        dataset_desc=trial["dataset_desc"],
                        date_from=date.fromisoformat(trial["date_from"][:10]) if trial.get("date_from") else None,
                        date_to=date.fromisoformat(trial["date_to"][:10]) if trial.get("date_to") else None,
                        metrics_json=trial["metrics"],
                        purpose=trial.get("purpose") or "research",
                    )
                    .returning(ResearchTrialModel.id)
                )
            ).scalar_one()
        return int(row_id)
    finally:
        await engine.dispose()


async def init_db(max_retries: int = 5, retry_interval: float = 2.0) -> bool:
    global _engine, _session_maker
    if _engine is not None:
        return True

    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set — database persistence disabled")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            _engine = create_async_engine(
                DATABASE_URL,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
            )
            _session_maker = async_sessionmaker(
                bind=_engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # Ensure columns and tables exist in existing databases
                await conn.execute(text("ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS entry_price DOUBLE PRECISION DEFAULT 0.0;"))
                await conn.execute(text("ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS stop_loss DOUBLE PRECISION DEFAULT 0.0;"))
                await conn.execute(text("ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS tp1 DOUBLE PRECISION DEFAULT 0.0;"))
                await conn.execute(text("ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS risk_reward_ratio DOUBLE PRECISION DEFAULT 1.5;"))
                await conn.execute(text("ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS win_status VARCHAR(20) DEFAULT 'PENDING';"))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS profitloss_history (
                        id SERIAL PRIMARY KEY,
                        signal_id INTEGER,
                        symbol VARCHAR(20) NOT NULL,
                        timeframe VARCHAR(10) NOT NULL DEFAULT '2h',
                        direction VARCHAR(10) NOT NULL,
                        entry_price DOUBLE PRECISION NOT NULL,
                        stop_loss DOUBLE PRECISION NOT NULL,
                        tp1 DOUBLE PRECISION NOT NULL,
                        exit_price DOUBLE PRECISION NOT NULL,
                        result VARCHAR(20) NOT NULL,
                        pnl_percent DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        risk_reward_ratio DOUBLE PRECISION NOT NULL DEFAULT 1.5,
                        cross_time VARCHAR(40) NOT NULL,
                        exit_time VARCHAR(40) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """))
                for migration in (
                    *UNIQUE_INDEX_MIGRATIONS,
                    *PROFITLOSS_R_MIGRATIONS,
                    *PROVENANCE_MIGRATIONS,
                    *CRON_RUNS_MIGRATIONS,
                    *STRATEGY_REGISTRY_MIGRATIONS,
                    *LEDGER_MIGRATIONS,
                ):
                    await conn.execute(text(migration))

            async with _session_maker() as session:
                await seed_default_watchlist(session)
                await backfill_resolved_signals_to_profitloss_history(session)
                await backfill_profitloss_costs(session)
                await sync_strategy_registry(session)
                # Last, so the genesis snapshots carry every restatement above
                await emit_genesis_events(session)

            logger.info("Database initialized successfully")
            return True
        except Exception as e:
            logger.warning("Database connection attempt %d/%d failed: %s", attempt, max_retries, e)
            if _engine is not None:
                await _engine.dispose()
                _engine = None
                _session_maker = None
            if attempt < max_retries:
                await asyncio.sleep(retry_interval)

    logger.error("Could not connect to PostgreSQL after %d attempts", max_retries)
    return False


async def close_db() -> None:
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_maker = None
        logger.info("Database connection closed")


async def is_db_connected() -> bool:
    if _session_maker is None:
        return False
    try:
        async with _session_maker() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# Cron run log
async def record_cron_run(
    slot_ms: int,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    signals_count: int = 0,
    evaluated_count: int = 0,
    error: str | None = None,
) -> bool:
    """Store the outcome of one scheduled run. Never raises: losing this row must not fail the run."""
    if status not in CRON_RUN_STATUSES:
        raise ValueError(f"status must be one of {CRON_RUN_STATUSES}")
    if _session_maker is None:
        return False
    try:
        async with _session_maker() as session:
            session.add(
                CronRunModel(
                    slot_ms=int(slot_ms),
                    started_at=started_at,
                    finished_at=finished_at,
                    status=status,
                    signals_count=int(signals_count),
                    evaluated_count=int(evaluated_count),
                    error=error[:CRON_RUN_ERROR_MAX_LENGTH] if error else None,
                )
            )
            await session.commit()
        return True
    except Exception as e:
        logger.error("Failed to record cron run for slot %s: %s", slot_ms, e)
        return False


async def get_last_cron_success() -> datetime | None:
    """Finish time of the newest successful scheduled run, or None (no run yet, or no database)."""
    if _session_maker is None:
        return None
    try:
        async with _session_maker() as session:
            res = await session.execute(
                select(func.max(CronRunModel.finished_at)).where(CronRunModel.status == "ok")
            )
            return res.scalar_one_or_none()
    except Exception as e:
        logger.error("Failed to read the last cron run: %s", e)
        return None


# A scheduled run counts as on schedule when it succeeded within this long after its 2H boundary
CRON_ON_SCHEDULE_SECONDS = 15 * 60
# Window the on-schedule ratio is measured over
CRON_SCHEDULE_WINDOW_DAYS = 7


def cron_schedule_stats(
    runs: list[dict], now: datetime, window_days: int = CRON_SCHEDULE_WINDOW_DAYS, first_slot_ms: int | None = None
) -> dict:
    """On-schedule ratio of the scheduled scan: of the 2H slots due in the window (from the first
    recorded slot when that is later), how many have an "ok" run that started within
    CRON_ON_SCHEDULE_SECONDS of the slot opening. `runs` are cron_runs rows as dicts."""
    interval = CANDLE_INTERVAL_MS["2h"]
    now_ms = _ms(now)
    window_start = now_ms - window_days * 86_400_000
    window_start -= window_start % interval
    if first_slot_ms is not None:
        window_start = max(window_start, first_slot_ms)
    # A slot is due once its grace period has passed
    last_due = now_ms - CRON_ON_SCHEDULE_SECONDS * 1000
    last_due -= last_due % interval
    expected = max(0, (last_due - window_start) // interval + 1) if last_due >= window_start else 0
    on_time = set()
    for run in runs:
        slot = run.get("slot_ms")
        started = run.get("started_at_ms")
        if run.get("status") != "ok" or slot is None or started is None:
            continue
        if window_start <= slot <= last_due and started - slot <= CRON_ON_SCHEDULE_SECONDS * 1000:
            on_time.add(slot)
    return {
        "window_days": window_days,
        "window_start": _ms_to_iso(window_start) if expected else None,
        "expected_slots": int(expected),
        "on_schedule_slots": len(on_time),
        "on_schedule_ratio": round(len(on_time) / expected, 4) if expected else None,
        "on_schedule_seconds": CRON_ON_SCHEDULE_SECONDS,
    }


def _cron_run_view(row: CronRunModel, include_error: bool) -> dict:
    out = {
        "id": row.id,
        "slot_ms": row.slot_ms,
        "slot": _ms_to_iso(row.slot_ms),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "started_at_ms": _ms(row.started_at) if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_seconds": (
            round((row.finished_at - row.started_at).total_seconds(), 1) if row.finished_at and row.started_at else None
        ),
        "status": row.status,
        "signals_count": row.signals_count,
        "evaluated_count": row.evaluated_count,
        "has_error": bool(row.error),
    }
    # Error text can carry internals, so only the signed-in owner sees it
    if include_error:
        out["error"] = row.error
    return out


async def get_cron_runs(limit: int = 50, include_error: bool = False, now: datetime | None = None) -> dict | None:
    """The newest scheduled runs, the last success and the on-schedule ratio; None without a database."""
    if _session_maker is None:
        return None
    now = now or datetime.now(UTC)
    since = now - timedelta(days=CRON_SCHEDULE_WINDOW_DAYS + 1)
    async with _session_maker() as session:
        recent = (
            (
                await session.execute(
                    select(CronRunModel).order_by(desc(CronRunModel.started_at), desc(CronRunModel.id)).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        window = (
            (await session.execute(select(CronRunModel).where(CronRunModel.started_at >= since))).scalars().all()
        )
        first_slot, last_success, counts = (
            await session.execute(
                select(
                    func.min(CronRunModel.slot_ms),
                    func.max(CronRunModel.finished_at).filter(CronRunModel.status == "ok"),
                    func.count(CronRunModel.id),
                )
            )
        ).one()
    runs = [_cron_run_view(r, include_error) for r in recent]
    stats = cron_schedule_stats([_cron_run_view(r, False) for r in window], now, first_slot_ms=first_slot)
    return {
        "runs": runs,
        "total_runs": counts or 0,
        "last_success": last_success.isoformat() if last_success else None,
        "schedule": stats,
    }


# Watchlist Operations
async def get_watchlist(active_only: bool = False) -> list[dict]:
    if _session_maker is None:
        return [{"symbol": s, "is_active": True, "notes": "Memory fallback"} for s in DEFAULT_WATCHLIST]

    try:
        async with _session_maker() as session:
            stmt = select(WatchlistModel)
            if active_only:
                stmt = stmt.where(WatchlistModel.is_active.is_(True))
            stmt = stmt.order_by(WatchlistModel.symbol.asc())
            res = await session.execute(stmt)
            return [row.to_dict() for row in res.scalars().all()]
    except Exception as e:
        logger.error("Failed to fetch watchlist: %s", e)
        return []


async def add_watchlist_symbol(symbol: str, notes: str = "") -> dict | None:
    if _session_maker is None:
        return None

    clean_sym = symbol.strip().upper()
    if not clean_sym.endswith("USDT"):
        clean_sym += "USDT"

    try:
        async with _session_maker() as session:
            stmt = select(WatchlistModel).where(WatchlistModel.symbol == clean_sym)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                existing.is_active = True
                if notes:
                    existing.notes = notes
                await session.commit()
                return existing.to_dict()

            item = WatchlistModel(symbol=clean_sym, is_active=True, notes=notes)
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return item.to_dict()
    except Exception as e:
        logger.error("Failed to add watchlist symbol %s: %s", clean_sym, e)
        return None


async def delete_watchlist_symbol(symbol: str) -> bool:
    if _session_maker is None:
        return False

    clean_sym = symbol.strip().upper()
    try:
        async with _session_maker() as session:
            stmt = select(WatchlistModel).where(WatchlistModel.symbol == clean_sym)
            item = (await session.execute(stmt)).scalar_one_or_none()
            if item:
                await session.delete(item)
                await session.commit()
                return True
            return False
    except Exception as e:
        logger.error("Failed to delete watchlist symbol %s: %s", clean_sym, e)
        return False


async def toggle_watchlist_symbol(symbol: str, is_active: bool) -> bool:
    if _session_maker is None:
        return False

    clean_sym = symbol.strip().upper()
    try:
        async with _session_maker() as session:
            stmt = select(WatchlistModel).where(WatchlistModel.symbol == clean_sym)
            item = (await session.execute(stmt)).scalar_one_or_none()
            if item:
                item.is_active = is_active
                await session.commit()
                return True
            return False
    except Exception as e:
        logger.error("Failed to toggle watchlist symbol %s: %s", clean_sym, e)
        return False


# Signal Operations
async def save_macd_signal(
    signal: MacdSignal, timeframe: str = "2h", source: str = "manual", detected_at: datetime | None = None
) -> dict | None:
    """Store a signal unless it is already stored. `source` defaults to "manual", which never
    counts in the track record; the scheduled scan passes "cron" or "catchup"."""
    if _session_maker is None:
        return None

    # The detected event carries the same instant as the row, to the millisecond
    detected_at = truncate_ms(detected_at or datetime.now(UTC))
    try:
        async with _session_maker() as session:
            stmt = build_signal_insert(signal, timeframe, source=source, detected_at=detected_at)
            inserted_id = (await session.execute(stmt)).scalar_one_or_none()

            if inserted_id is not None:
                stmt = select(MacdSignalModel).where(MacdSignalModel.id == inserted_id)
            else:
                strategy_key, strategy_version = signal_strategy(signal)
                stmt = select(MacdSignalModel).where(
                    MacdSignalModel.strategy_key == strategy_key,
                    MacdSignalModel.strategy_version == strategy_version,
                    MacdSignalModel.symbol == signal.symbol,
                    MacdSignalModel.side == signal.side,
                    MacdSignalModel.timeframe == timeframe,
                    MacdSignalModel.cross_candle_ms == signal.cross_candle_ms,
                )
            record = (await session.execute(stmt)).scalars().first()
            # The row and its detected event commit together, or neither does
            if inserted_id is not None and record is not None:
                await append_signal_events(
                    session, [EventDraft(inserted_id, "detected", detected_payload(record, signal), detected_at)]
                )
            await session.commit()
            if record is None:
                return None
            return {**record.to_dict(), "inserted": inserted_id is not None}
    except Exception as e:
        logger.exception("Failed to save MACD signal to database: %s", e)
        return None


def _empty_signals_summary() -> dict:
    return {
        "open_count": 0,
        "profit_count": 0,
        "loss_count": 0,
        "expired_count": 0,
        "closed_count": 0,
        "total_r": 0.0,
        "total_pnl_percent": 0.0,
        "strategy_version": STRATEGY_VERSION,
        "eligible_closed_count": 0,
        "eligible_net_r": 0.0,
        "excluded_count": 0,
    }


def _trade_result(pl) -> dict:
    """Result fields shown with a signal; all None while the signal is still open."""
    if pl is None:
        return {
            "exit_price": None,
            "exit_time": None,
            "r_multiple": None,
            "pnl_percent": None,
            "gross_r": None,
            "cost_r": None,
            "net_r": None,
        }
    return {
        "exit_price": round(pl.exit_price, 6),
        "exit_time": pl.exit_time,
        "r_multiple": round(pl.r_multiple, 2) if pl.r_multiple is not None else None,
        "pnl_percent": round(pl.pnl_percent, 2),
        "gross_r": _round_or_none(pl.gross_r, 3),
        "cost_r": _round_or_none(pl.cost_r, 3),
        "net_r": _round_or_none(pl.net_r, 3),
    }


async def _trade_results_by_signal(session: AsyncSession, signal_ids: list[int]) -> dict[int, dict]:
    if not signal_ids:
        return {}
    rows = await session.execute(
        select(ProfitLossHistoryModel).where(ProfitLossHistoryModel.signal_id.in_(signal_ids))
    )
    return {pl.signal_id: _trade_result(pl) for pl in rows.scalars().all()}


async def get_macd_signals(
    symbol: str | None = None,
    side: str | None = None,
    direction: str | None = None,
    timeframe: str | None = "2h",
    win_status: str | None = None,
    result: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
    tz_offset_minutes: int = 0,
    strategy_key: str | None = None,
    strategy_version: str | None = None,
    source: str | None = None,
) -> tuple[list[dict], int, dict]:
    """Signals, newest cross first, each with its journal status and trade result, plus a summary of
    every signal matching the filters. Date filters are days at `tz_offset_minutes` east of UTC."""
    if _session_maker is None:
        return [], 0, _empty_signals_summary()

    sig, pl = MacdSignalModel, ProfitLossHistoryModel
    target_side = (direction or side or "").strip().upper() or None
    target_status = None
    if result:
        res_upper = result.strip().upper()
        target_status = {"PROFIT": "TP1_HIT", "LOSS": "SL_HIT"}.get(res_upper, res_upper)
    elif win_status:
        target_status = win_status.strip().upper()

    conditions = []
    if symbol:
        conditions.append(sig.symbol == symbol.upper().strip())
    if target_side:
        conditions.append(sig.side == target_side)
    if timeframe:
        conditions.append(sig.timeframe == timeframe.strip().lower())
    if target_status:
        conditions.append(sig.win_status == target_status)
    if strategy_key:
        conditions.append(sig.strategy_key == strategy_key.strip())
    if strategy_version:
        conditions.append(sig.strategy_version == strategy_version.strip())
    if source:
        conditions.append(sig.source == source.strip().lower())
    # Dates select by the cross candle's open time, half-open so end_date keeps its last day
    day_ranges = [utc_day_bounds(start_date, end_date, tz_offset_minutes)]
    if date:
        day_ranges.append(utc_day_bounds(date, date, tz_offset_minutes))
    for start, end in day_ranges:
        if start:
            conditions.append(sig.cross_candle_ms >= _ms(start))
        if end:
            conditions.append(sig.cross_candle_ms < _ms(end))

    # Only eligible trades of the current rules make up the headline; the rest is listed, not counted
    counted = and_(counted_signal(), counted_trade())
    # The deprecated as-booked counts cover the headline strategy (every version) unless a strategy is
    # asked for, so a shadow strategy's paper trades never read as part of the live record
    family = true() if strategy_key else headline_family()
    try:
        async with _session_maker() as session:
            agg = (
                await session.execute(
                    select(
                        func.count(sig.id).label("total"),
                        func.count().filter(family, sig.win_status == "PENDING").label("open"),
                        func.count().filter(family, sig.win_status == "TP1_HIT").label("profit"),
                        func.count().filter(family, sig.win_status == "SL_HIT").label("loss"),
                        func.count().filter(family, sig.win_status == "EXPIRED").label("expired"),
                        func.count(pl.id).filter(family).label("closed"),
                        func.coalesce(func.sum(pl.r_multiple).filter(family), 0.0).label("total_r"),
                        func.coalesce(func.sum(pl.pnl_percent).filter(family), 0.0).label("pnl"),
                        func.count(pl.id).filter(counted).label("eligible_closed"),
                        func.coalesce(func.sum(pl.net_r).filter(counted), 0.0).label("eligible_net_r"),
                        # Every signal outside the running version's stats, whatever the reason
                        func.count().filter(not_counted_signal()).label("excluded"),
                    )
                    .select_from(sig)
                    .outerjoin(pl, pl.signal_id == sig.id)
                    .where(*conditions)
                )
            ).one()

            query = (
                select(sig)
                .where(*conditions)
                .order_by(desc(sig.cross_candle_ms), desc(sig.id))
                .limit(limit)
                .offset(offset)
            )
            records = (await session.execute(query)).scalars().all()
            ids = [r.id for r in records]
            journal = await _journal_status_by_signal(session, ids)
            results = await _trade_results_by_signal(session, ids)
            items = [
                {**r.to_dict(), **results.get(r.id, _trade_result(None)), "journal_status": journal.get(r.id)}
                for r in records
            ]
            summary = {
                "open_count": agg.open or 0,
                "profit_count": agg.profit or 0,
                "loss_count": agg.loss or 0,
                "expired_count": agg.expired or 0,
                "closed_count": agg.closed or 0,
                # Deprecated: as booked, every row included (legacy fee-free rows too); use eligible_net_r
                "total_r": round(float(agg.total_r or 0.0), 2),
                "total_pnl_percent": round(float(agg.pnl or 0.0), 2),
                "strategy_version": STRATEGY_VERSION,
                "eligible_closed_count": agg.eligible_closed or 0,
                "eligible_net_r": round(float(agg.eligible_net_r or 0.0), 2),
                "excluded_count": agg.excluded or 0,
            }
            return items, agg.total or 0, summary
    except Exception as e:
        logger.exception("Failed to query MACD signals: %s", e)
        return [], 0, _empty_signals_summary()


async def get_signals_by_keys(
    keys: list[tuple[str, str, int]], timeframe: str = "2h"
) -> dict[tuple[str, str, int], dict]:
    if _session_maker is None or not keys:
        return {}

    try:
        async with _session_maker() as session:
            # An ad-hoc scan runs the headline strategy's rules, so only its rows can match; when two
            # of its versions recorded the cross, the headline version wins (it is ordered last)
            stmt = (
                select(MacdSignalModel)
                .where(
                    MacdSignalModel.timeframe == timeframe,
                    MacdSignalModel.strategy_key == STRATEGY_KEY,
                    tuple_(
                        MacdSignalModel.symbol,
                        MacdSignalModel.side,
                        MacdSignalModel.cross_candle_ms,
                    ).in_(list(set(keys))),
                )
                .order_by((MacdSignalModel.strategy_version == STRATEGY_VERSION).asc(), MacdSignalModel.id.asc())
            )
            records = (await session.execute(stmt)).scalars().all()
            return {(r.symbol, r.side, r.cross_candle_ms): r.to_dict() for r in records}
    except Exception as e:
        logger.error("Failed to look up signals by key: %s", e)
        return {}


async def get_signal_by_id(signal_id: int) -> dict | None:
    if _session_maker is None:
        return None

    try:
        async with _session_maker() as session:
            stmt = select(MacdSignalModel).where(MacdSignalModel.id == signal_id)
            res = await session.execute(stmt)
            sig = res.scalar_one_or_none()
            if sig is None:
                return None
            entry = (
                await session.execute(select(TradeJournalModel).where(TradeJournalModel.signal_id == signal_id))
            ).scalar_one_or_none()
            results = await _trade_results_by_signal(session, [signal_id])
            return {
                **sig.to_dict(),
                **results.get(signal_id, _trade_result(None)),
                "journal": journal_view(sig, entry),
            }
    except Exception as e:
        logger.error("Failed to get signal %d: %s", signal_id, e)
        return None


def history_context(rec, raw_klines: list, history: int) -> MarketContext | None:
    """MarketContext of the klines up to and including the cross candle (at most `history` of them
    before it), as of the cross candle's close, for strategies whose exit needs indicator history; None
    for those that need none. Rows opening after the cross candle are never included."""
    if history <= 0:
        return None
    rows = [k[:6] for k in raw_klines if int(k[0]) <= int(rec.cross_candle_ms)][-(history + 1) :]
    frame = pd.DataFrame(rows, columns=["open_time_ms", "open", "high", "low", "close", "volume"])
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    interval_ms = CANDLE_INTERVAL_MS.get(rec.timeframe, CANDLE_INTERVAL_MS["2h"])
    decision_ms = int(rec.cross_candle_ms) + interval_ms
    return MarketContext(rec.symbol, decision_ms, rec.timeframe, {rec.timeframe: normalize_klines(frame)})


def settle_signal(strategy, rec, raw_klines: list, now_ms: int) -> TradeOutcome | None:
    """How a stored signal ended, by its strategy's resolve(), from the klines the evaluator fetched
    (resolve_history_bars before the cross candle, the cross candle, then the trade's candles, the
    forming one included); None while it is still open. Klines are split at the cross candle by open
    time, so a symbol with less history than asked for cannot shift the trade's candles."""
    interval_ms = CANDLE_INTERVAL_MS.get(rec.timeframe, CANDLE_INTERVAL_MS["2h"])
    candles = [k for k in raw_klines if int(k[0]) > int(rec.cross_candle_ms)]
    if not candles:
        return None
    history = history_context(rec, raw_klines, strategy.resolve_history_bars)
    return strategy.resolve(rec, candles, ResolveContext(now_ms, interval_ms, history))


def _kline_rows(raw) -> list:
    if isinstance(raw, dict) and "data" in raw:
        raw = raw["data"]
    return list(raw or [])


def fetch_settlement_klines(client, rec, strategy) -> list:
    """The klines a stored signal is settled on: resolve_history_bars before the cross candle, the cross
    candle, then up to max_hold_bars trade candles, the forming one included.

    One request anchored in time, unchanged from before the ledger. When the exchange's klines have a
    gap inside the history window, that request holds fewer history candles than the strategy needs,
    so the history is fetched again by count (the newest history+1 candles up to the cross candle),
    the way the backtest replay slices it, instead of leaving the trade open for ever."""
    interval_ms = CANDLE_INTERVAL_MS.get(rec.timeframe, CANDLE_INTERVAL_MS["2h"])
    history = strategy.resolve_history_bars
    cross = int(rec.cross_candle_ms)
    raw = _kline_rows(
        client.klines(
            symbol=rec.symbol,
            interval=rec.timeframe,
            startTime=cross - history * interval_ms,
            limit=min(history + strategy.max_hold_bars + 1, 1500),
        )
    )
    if history <= 0 or not raw or sum(1 for k in raw if int(k[0]) <= cross) >= history + 1:
        return raw
    older = _kline_rows(client.klines(symbol=rec.symbol, interval=rec.timeframe, endTime=cross, limit=history + 1))
    merged = {int(k[0]): k for k in older if int(k[0]) <= cross}
    merged.update((int(k[0]), k) for k in raw)
    return [merged[t] for t in sorted(merged)]


def _empty_evaluation() -> dict:
    return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0, "expired_count": 0, "closed": []}


@dataclass
class Resolution:
    """A signal the evaluator closed in this run, for its ledger records. `raw_klines` is exactly what
    it was settled on (the request is unchanged from before the ledger: resolve_history_bars before
    the cross candle, the cross candle, then up to max_hold_bars trade candles, the forming one
    included), which is the window persisted in signal_candles."""

    rec: object
    strategy: object
    outcome: TradeOutcome
    values: dict
    inserted: bool
    source_before: str | None
    raw_klines: list
    interval_ms: int


def resolved_records(res: Resolution, now_ms: int) -> tuple[dict, list[EventDraft]]:
    """The signal_candles row and the events (excluded when the booking moved the signal to legacy,
    then resolved) for one closed signal."""
    rec, values = res.rec, res.values
    rows = kline_rows(res.raw_klines)
    window = {
        "signal_id": rec.id,
        "symbol": rec.symbol,
        "timeframe": rec.timeframe,
        "interval_ms": res.interval_ms,
        "start_ms": rows[0][0],
        "end_ms": rows[-1][0],
        "candle_count": len(rows),
        "resolved_at_ms": now_ms,
        "candles": normalize(rows),
        "sha256": candles_sha256(rows),
    }
    drafts = []
    if rec.source == "legacy" and res.source_before != "legacy":
        drafts.append(
            EventDraft(
                rec.id,
                "excluded",
                {
                    "genesis": False,
                    "reason": "legacy",
                    "detail": "booked with the interpolated cross as its entry (cross_interp)",
                    "source_before": res.source_before,
                    "source": rec.source,
                    "entry_basis": rec.entry_basis,
                    "strategy_version": rec.strategy_version,
                },
            )
        )
    drafts.append(
        EventDraft(
            rec.id,
            "resolved",
            {
                "signal": signal_snapshot(rec),
                # None when a trade row already held the signal's slot, which then stays as it was
                "trade": trade_snapshot(values) if res.inserted else None,
                "strategy_ref": res.strategy.ref,
                "result": res.outcome.result,
                "win_status": res.outcome.win_status,
                "exit_price": res.outcome.exit_price,
                "exit_time_ms": res.outcome.exit_time_ms,
                "entry_price": values["entry_price"],
                "entry_basis": values["entry_basis"],
                "gross_r": values["gross_r"],
                "cost_r": values["cost_r"],
                "net_r": values["net_r"],
                "pnl_percent": values["pnl_percent"],
                "fee_percent": values["fee_percent"],
                "slippage_bps": values["slippage_bps"],
                "cost_model_version": values["cost_model_version"],
                "evaluated_at_ms": now_ms,
                "candles": {
                    "sha256": window["sha256"],
                    "count": window["candle_count"],
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                },
            },
        )
    )
    return window, drafts


async def record_resolutions(session: AsyncSession, resolutions: list[Resolution], now_ms: int) -> None:
    """Persist each closed signal's candle window and append its events, in the evaluation's
    transaction: a trade is never booked without its ledger records, nor the other way round."""
    drafts = []
    for res in resolutions:
        window, events = resolved_records(res, now_ms)
        await session.execute(
            pg_insert(SignalCandlesModel).values(**window).on_conflict_do_nothing(index_elements=["signal_id"])
        )
        drafts.extend(events)
    await append_signal_events(session, drafts)


async def _evaluate_pending_signals_unlocked(client) -> dict:
    """Close pending signals that reached their target, their stop, or their expiry.

    The trade is booked under the cost model: pnl_percent and r_multiple are net of fees and
    modelled slippage, with gross_r, cost_r and net_r stored alongside. Each closed signal also gets
    its candle window (signal_candles) and its resolved event, in the same transaction. Returns
    counts plus `closed`, the trades this run recorded, for outcome alerts.
    """
    if _session_maker is None or client is None:
        return _empty_evaluation()

    summary = _empty_evaluation()
    counters = {
        "PROFIT": "profit_count",
        "LOSS": "loss_count",
        "EXPIRED": "expired_count",
        STOPPED: "loss_count",
        TIME_EXIT: "expired_count",
    }
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    try:
        async with _session_maker() as session:
            stmt = select(MacdSignalModel).where(MacdSignalModel.win_status == "PENDING")
            pending_records = (await session.execute(stmt)).scalars().all()
            resolutions: list[Resolution] = []

            for rec in pending_records:
                try:
                    # Each signal is settled by the exit rules of the strategy version that emitted it
                    key, version = getattr(rec, "strategy_key", None), getattr(rec, "strategy_version", None)
                    strategy = registry.resolver_for(key, version)
                    if strategy is None:
                        logger.error(
                            "Signal %s is of %s@%s, which is not registered: left open rather than settled "
                            "by another strategy's rules", rec.id, key, version,
                        )
                        continue
                    interval_ms = CANDLE_INTERVAL_MS.get(rec.timeframe, CANDLE_INTERVAL_MS["2h"])
                    raw_klines = await asyncio.to_thread(fetch_settlement_klines, client, rec, strategy)
                    if not raw_klines:
                        continue

                    # Klines up to the cross candle are history; the trade starts on the next one
                    outcome = settle_signal(strategy, rec, raw_klines, now_ms)
                    if outcome is None:
                        continue

                    source_before = getattr(rec, "source", None)
                    entry, entry_basis = booking_entry(rec)
                    values = booked_trade_values(
                        rec,
                        entry,
                        entry_basis,
                        exit_price=outcome.exit_price,
                        result=outcome.result,
                        exit_time=_ms_to_iso(outcome.exit_time_ms),
                    )
                    inserted = (await session.execute(build_profitloss_insert(**values))).scalar_one_or_none()
                    rec.win_status = outcome.win_status
                    resolutions.append(
                        Resolution(
                            rec, strategy, outcome, values, inserted is not None, source_before, raw_klines, interval_ms
                        )
                    )

                    summary["evaluated_count"] += 1
                    # Exit labels of strategies without a target count under their own key
                    counter = counters.get(outcome.result, f"{outcome.result.lower()}_count")
                    summary[counter] = summary.get(counter, 0) + 1
                    # Only the live version's outcomes are announced; shadow (and research or retired)
                    # trades close silently unless SHADOW_ALERTS is on
                    if inserted is not None and strategy.status == "live":
                        summary["closed"].append(values)
                    elif inserted is not None and SHADOW_ALERTS:
                        summary["closed"].append({**values, "shadow_strategy": strategy.ref})

                except Exception as ex:
                    logger.warning("Evaluation error for %s: %s", rec.symbol, ex)

            # Written last, so the ledger lock is held only for the appends and the commit
            await record_resolutions(session, resolutions, now_ms)
            await session.commit()
            return summary
    except Exception as e:
        logger.error("evaluate_pending_signals error: %s", e)
        return _empty_evaluation()


async def evaluate_pending_signals(client) -> dict:
    if _session_maker is None or client is None:
        return _empty_evaluation()

    async with _evaluation_lock:
        return await _evaluate_pending_signals_unlocked(client)


# Profit/Loss History Operations
class SignalTradeConflict(Exception):
    """A hand-entered trade pointed at a stored signal, whose outcome only the evaluator books."""


async def record_profit_loss(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    exit_price: float,
    result: str,
    timeframe: str = "2h",
    signal_id: int | None = None,
    pnl_percent: float = 0.0,
    risk_reward_ratio: float = 1.5,
    cross_time: str = "",
    exit_time: str = "",
) -> dict | None:
    """Store a hand-entered trade in profitloss_adjustments, a table no statistic, ledger export or
    evaluator reads, so it can never change the track record. Raises SignalTradeConflict when
    `signal_id` names a stored signal: the evaluator books that signal's outcome, and what was really
    filled on a signal belongs in its journal entry."""
    if _session_maker is None:
        return None

    if signal_id is not None:
        async with _session_maker() as session:
            stored = (
                await session.execute(select(MacdSignalModel.id).where(MacdSignalModel.id == signal_id))
            ).scalar_one_or_none()
        if stored is not None:
            raise SignalTradeConflict(f"signal {signal_id} is a stored signal; its outcome is booked by the evaluator")

    try:
        async with _session_maker() as session:
            record = ProfitLossAdjustmentModel(
                signal_id=signal_id,
                symbol=symbol.upper().strip(),
                timeframe=timeframe,
                direction=direction.upper().strip(),
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                exit_price=exit_price,
                result=result.upper().strip(),
                pnl_percent=pnl_percent,
                risk_reward_ratio=risk_reward_ratio,
                cross_time=cross_time,
                exit_time=exit_time or cross_time,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.to_dict()
    except Exception as e:
        logger.error("Failed to record profit/loss history: %s", e)
        return None


async def get_profitloss_adjustments(limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Hand-entered trades, newest first."""
    if _session_maker is None:
        return [], 0
    adj = ProfitLossAdjustmentModel
    try:
        async with _session_maker() as session:
            total = (await session.execute(select(func.count(adj.id)))).scalar_one() or 0
            rows = (await session.execute(select(adj).order_by(desc(adj.id)).limit(limit).offset(offset))).scalars()
            return [r.to_dict() for r in rows.all()], total
    except Exception:
        logger.exception("Failed to list profit/loss adjustments")
        return [], 0


PROFITLOSS_SUMMARY_NOTE = (
    "total_trades, winrate, total_r, avg_r and total_pnl_percent are as booked over the listed rows of the "
    f"headline strategy ({STRATEGY_KEY}, every version, legacy fee-free rows included; shadow strategies "
    "left out), and are deprecated. The eligible_* fields count only trades the "
    "evaluator booked for eligible signals of strategy_version, in net R, the same as /api/performance."
)


def _empty_profitloss_summary() -> dict:
    return {
        "total_trades": 0,
        "profit_count": 0,
        "loss_count": 0,
        "expired_count": 0,
        "winrate": 0.0,
        "total_pnl_percent": 0.0,
        "total_r": 0.0,
        "avg_r": 0.0,
        "strategy_version": STRATEGY_VERSION,
        "eligible_closed_count": 0,
        "eligible_win_count": 0,
        "eligible_win_rate": None,
        "eligible_net_r": 0.0,
        "note": PROFITLOSS_SUMMARY_NOTE,
    }


async def get_profitloss_history(
    symbol: str | None = None,
    direction: str | None = None,
    timeframe: str | None = None,
    result: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, dict]:
    if _session_maker is None:
        return [], 0, _empty_profitloss_summary()

    pl, sig = ProfitLossHistoryModel, MacdSignalModel
    conditions = []
    if symbol:
        conditions.append(pl.symbol == symbol.strip().upper())
    if direction:
        conditions.append(pl.direction == direction.strip().upper())
    if timeframe:
        conditions.append(pl.timeframe == timeframe.strip().lower())
    if result:
        conditions.append(pl.result == result.strip().upper())
    if date:
        clean_date = date.strip()
        conditions.append(pl.cross_time.like(f"{clean_date}%") | pl.exit_time.like(f"{clean_date}%"))
    # Half-open UTC range on the cross time read as a timestamp, so end_date keeps its last day
    start, end = utc_day_bounds(start_date, end_date)
    if start:
        conditions.append(_utc_text_as_timestamp(pl.cross_time) >= start.replace(tzinfo=None))
    if end:
        conditions.append(_utc_text_as_timestamp(pl.cross_time) < end.replace(tzinfo=None))

    try:
        async with _session_maker() as session:
            records_query = select(pl).where(*conditions).order_by(desc(pl.id)).limit(limit).offset(offset)
            items = [r.to_dict() for r in (await session.execute(records_query)).scalars().all()]

            counted = and_(counted_signal(), counted_trade())
            # Deprecated as-booked figures: the headline strategy's rows (and old rows with no signal)
            family = or_(sig.id.is_(None), headline_family())
            agg = (
                await session.execute(
                    select(
                        func.count(pl.id).label("total"),
                        func.count(pl.id).filter(family).label("family_total"),
                        func.count().filter(family, pl.result == "PROFIT").label("profit"),
                        func.count().filter(family, pl.result == "LOSS").label("loss"),
                        func.count().filter(family, pl.result == "EXPIRED").label("expired"),
                        func.coalesce(func.sum(pl.pnl_percent).filter(family), 0.0).label("pnl"),
                        func.coalesce(func.sum(pl.r_multiple).filter(family), 0.0).label("total_r"),
                        func.avg(pl.r_multiple).filter(family).label("avg_r"),
                        func.count(pl.id).filter(counted).label("eligible_closed"),
                        func.count(pl.id).filter(counted, pl.result == "PROFIT").label("eligible_wins"),
                        func.coalesce(func.sum(pl.net_r).filter(counted), 0.0).label("eligible_net_r"),
                    )
                    .select_from(pl)
                    .outerjoin(sig, sig.id == pl.signal_id)
                    .where(*conditions)
                )
            ).one()

            total = agg.total or 0
            family_total = agg.family_total or 0
            eligible_closed = agg.eligible_closed or 0
            eligible_wins = agg.eligible_wins or 0
            summary = {
                "total_trades": family_total,
                "profit_count": agg.profit or 0,
                "loss_count": agg.loss or 0,
                "expired_count": agg.expired or 0,
                "winrate": round((agg.profit or 0) / family_total * 100, 1) if family_total else 0.0,
                "total_pnl_percent": round(float(agg.pnl or 0.0), 2),
                "total_r": round(float(agg.total_r or 0.0), 2),
                "avg_r": round(float(agg.avg_r or 0.0), 3),
                "strategy_version": STRATEGY_VERSION,
                "eligible_closed_count": eligible_closed,
                "eligible_win_count": eligible_wins,
                "eligible_win_rate": round(eligible_wins / eligible_closed * 100, 1) if eligible_closed else None,
                "eligible_net_r": round(float(agg.eligible_net_r or 0.0), 2),
                "note": PROFITLOSS_SUMMARY_NOTE,
            }
            return items, total, summary
    except Exception as e:
        logger.exception("Failed to query profitloss history: %s", e)
        return [], 0, _empty_profitloss_summary()


# Why a signal is kept out of a version's statistics: its source, a different strategy version, no
# ledger events (stored outside the app), or (a catch-all that should stay at zero) a scheduled signal
# not entered at the next open
EXCLUSION_REASONS = ("legacy", "catchup", "manual", "other_version", "outside_ledger", "other")


def strategy_target_rr(key: str, version: str) -> float | None:
    """Planned reward-to-risk of a strategy version's fixed target; None for a registered strategy without
    a target (its trades end on other exits). Unregistered versions (older or legacy rows) used the
    configured ratio."""
    strategy = registry.lookup(key, version)
    if strategy is None:
        return RISK_REWARD_RATIO
    return getattr(strategy.params, "risk_reward_ratio", None)


def _performance_payload(
    version: str,
    symbol: str | None,
    side: str | None,
    trades: list[ClosedTrade],
    open_count: int = 0,
    excluded: dict | None = None,
    key: str = STRATEGY_KEY,
) -> dict:
    return {
        "strategy_key": key,
        "strategy_version": version,
        "cost_model_version": COST_MODEL_VERSION,
        "filters": {"symbol": symbol, "side": side},
        **summarize_trades(trades, strategy_target_rr(key, version)),
        "open_count": open_count,
        "excluded": {reason: (excluded or {}).get(reason, 0) for reason in EXCLUSION_REASONS},
        "definitions": DEFINITIONS,
    }


def _scope(symbol: str | None, side: str | None) -> list:
    sig = MacdSignalModel
    scope = []
    if symbol:
        scope.append(sig.symbol == symbol)
    if side:
        scope.append(sig.side == side)
    return scope


async def _performance_report(
    session: AsyncSession,
    version: str,
    symbol: str | None = None,
    side: str | None = None,
    key: str = STRATEGY_KEY,
) -> dict:
    """KPIs of the eligible closed trades of one strategy version, plus what was left out and why.

    A trade counts only when its signal is eligible and of that version and the evaluator booked it
    (counted_trade). Excluded counts cover every other signal, open or closed, in the symbol/side
    scope, each under one reason, so counted plus excluded is every stored signal.
    """
    sig, pl = MacdSignalModel, ProfitLossHistoryModel
    scope = _scope(symbol, side)
    eligible = [counted_signal(version, key), *scope]

    rows = await session.execute(
        select(pl.id, pl.result, pl.net_r, pl.gross_r, pl.cost_r, pl.exit_time)
        .join(sig, sig.id == pl.signal_id)
        .where(*eligible, counted_trade())
    )
    trades = [ClosedTrade(*row) for row in rows.all()]
    open_count = (
        await session.execute(select(func.count(sig.id)).where(*eligible, sig.win_status == "PENDING"))
    ).scalar_one() or 0
    reason = case(
        # Another strategy (a shadow one, say) is another version of the rules as far as this one goes;
        # checked first, so another strategy's legacy or late rows are not listed as this one's
        (sig.strategy_key.is_distinct_from(key), "other_version"),
        (sig.source == "legacy", "legacy"),
        (sig.strategy_version.is_distinct_from(version), "other_version"),
        (sig.source.in_(("catchup", "manual")), sig.source),
        (~in_ledger(), "outside_ledger"),
        else_="other",
    )
    excluded = await session.execute(
        select(reason, func.count(sig.id)).where(not_counted_signal(version, key), *scope).group_by(reason)
    )
    return _performance_payload(version, symbol, side, trades, open_count, dict(excluded.all()), key)


async def get_performance(
    strategy_version: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    strategy_key: str | None = None,
) -> dict:
    key = (strategy_key or "").strip() or STRATEGY_KEY
    version = (strategy_version or "").strip() or default_version(key)
    symbol = (symbol or "").strip().upper() or None
    side = (side or "").strip().upper() or None
    if _session_maker is None:
        return _performance_payload(version, symbol, side, [], key=key)

    try:
        async with _session_maker() as session:
            return await _performance_report(session, version, symbol, side, key)
    except Exception:
        logger.exception("Failed to calculate performance")
        return _performance_payload(version, symbol, side, [], key=key)


def default_version(key: str) -> str:
    """The version a strategy's statistics default to: the running headline version, else the newest
    registered version of `key`."""
    if key == STRATEGY_KEY:
        return STRATEGY_VERSION
    versions = [s.version for s in registry.all_strategies() if s.key == key]
    return versions[-1] if versions else STRATEGY_VERSION


# A counted trade with what the track-record page groups it by
TRACK_RECORD_COLUMNS = ("signal_id", "symbol", "side", "result", "net_r", "gross_r", "cost_r", "exit_time")


async def get_track_record(strategy_key: str | None = None, strategy_version: str | None = None) -> dict:
    """The track record of one strategy version: the KPIs of /api/performance, the equity curve and
    drawdown in R, the net R histogram and breakdowns per side, symbol and exit month, all over the
    same eligible closed trades."""
    perf = await get_performance(strategy_version=strategy_version, strategy_key=strategy_key)
    key, version = perf["strategy_key"], perf["strategy_version"]
    records: list[dict] = []
    if _session_maker is not None:
        sig, pl = MacdSignalModel, ProfitLossHistoryModel
        try:
            async with _session_maker() as session:
                rows = await session.execute(
                    select(sig.id, sig.symbol, sig.side, pl.result, pl.net_r, pl.gross_r, pl.cost_r, pl.exit_time)
                    .join(sig, sig.id == pl.signal_id)
                    .where(counted_signal(version, key), counted_trade())
                )
                records = [dict(zip(TRACK_RECORD_COLUMNS, row, strict=True)) for row in rows.all()]
        except Exception:
            logger.exception("Failed to load the track record of %s@%s", key, version)
            records = []
    return track_record_payload(perf, records)


def track_record_payload(perf: dict, records: list[dict]) -> dict:
    target_rr = strategy_target_rr(perf["strategy_key"], perf["strategy_version"])
    trades = [
        (r, ClosedTrade(r["signal_id"], r["result"], r["net_r"], r["gross_r"], r["cost_r"], r["exit_time"]))
        for r in records
    ]

    def month(r: dict) -> str:
        dt = parse_utc(r["exit_time"])
        return dt.strftime("%Y-%m") if dt else "unknown"

    return {
        "performance": perf,
        "equity": equity_curve(t for _, t in trades),
        "histogram": histogram(t.net_r for _, t in trades),
        "histogram_bin_r": HISTOGRAM_BIN_R,
        "breakdowns": {
            "side": breakdown(((r["side"], t) for r, t in trades), target_rr),
            "symbol": breakdown(((r["symbol"], t) for r, t in trades), target_rr),
            "month": sorted(breakdown(((month(r), t) for r, t in trades), target_rr), key=lambda g: g["key"]),
        },
        "min_breakdown_sample": MIN_BREAKDOWN_SAMPLE,
    }


async def get_signal_counts() -> list[dict]:
    """Stored signals per strategy version and source, with how many are still open."""
    if _session_maker is None:
        return []
    sig = MacdSignalModel
    try:
        async with _session_maker() as session:
            rows = await session.execute(
                select(
                    sig.strategy_key,
                    sig.strategy_version,
                    sig.source,
                    func.count(sig.id),
                    func.count().filter(sig.win_status == "PENDING"),
                ).group_by(sig.strategy_key, sig.strategy_version, sig.source)
            )
            return [
                {"strategy_key": k, "strategy_version": v, "source": src, "count": n, "open": o}
                for k, v, src, n, o in rows.all()
            ]
    except Exception:
        logger.exception("Failed to count signals per strategy version")
        return []


SUMMARY_NOTE = (
    "Outcome fields (target, stop and expired counts, win rate, closed trades, R and drawdown) count only "
    "eligible trades of strategy_version, the same as /api/performance. Signal counts cover every recorded "
    f"signal of the headline strategy ({STRATEGY_KEY}, every version); shadow strategies are reported on "
    "their own (/api/performance?strategy_key=...)."
)


def _signals_summary(
    perf: dict, counts: dict | None = None, watchlist_count: int = 0, latest: str | None = None
) -> dict:
    counts = counts or {}
    return {
        "total_signals": counts.get("total") or 0,
        "long_signals": counts.get("long_cnt") or 0,
        "short_signals": counts.get("short_cnt") or 0,
        "symbols_count": counts.get("symbols_cnt") or 0,
        "pending_count": counts.get("pending") or 0,
        "tp1_hit_count": perf["wins"],
        "sl_hit_count": perf["losses"],
        "expired_count": perf["expired"],
        "winrate": perf["win_rate"] or 0.0,
        "target_rr": RISK_REWARD_RATIO,
        "watchlist_count": watchlist_count,
        "latest_signal_time": latest,
        "total_net_pnl": round(float(counts.get("eligible_pnl") or 0.0), 2),
        "closed_trades": perf["n_closed"],
        "total_r": round(perf["total_net_r"], 2),
        "avg_r": perf["mean_net_r"] or 0.0,
        "max_drawdown_r": round(perf["max_drawdown_r"], 2),
        "eligible_only": True,
        "strategy_version": perf["strategy_version"],
        "note": SUMMARY_NOTE,
    }


async def get_macd_signals_summary() -> dict:
    empty = _performance_payload(STRATEGY_VERSION, None, None, [])
    if _session_maker is None:
        return _signals_summary(empty, watchlist_count=len(DEFAULT_WATCHLIST))

    sig, pl = MacdSignalModel, ProfitLossHistoryModel
    try:
        async with _session_maker() as session:
            perf = await _performance_report(session, STRATEGY_VERSION)
            counted = and_(counted_signal(), counted_trade())
            counts = (
                await session.execute(
                    select(
                        func.count(sig.id).label("total"),
                        func.count().filter(sig.side == "LONG").label("long_cnt"),
                        func.count().filter(sig.side == "SHORT").label("short_cnt"),
                        func.count(func.distinct(sig.symbol)).label("symbols_cnt"),
                        func.count().filter(sig.win_status == "PENDING").label("pending"),
                        # Deprecated summed percent, kept for old clients; R is the comparable unit
                        func.coalesce(func.sum(pl.pnl_percent).filter(counted), 0.0).label("eligible_pnl"),
                    )
                    .select_from(sig)
                    .outerjoin(pl, pl.signal_id == sig.id)
                    .where(headline_family())
                )
            ).one()

            wl_count = (
                await session.execute(
                    select(func.count(WatchlistModel.id)).where(WatchlistModel.is_active.is_(True))
                )
            ).scalar_one() or 0

            latest = (
                await session.execute(
                    select(sig.cross_time)
                    .where(headline_family())
                    .order_by(desc(sig.cross_candle_ms), desc(sig.id))
                    .limit(1)
                )
            ).scalar_one_or_none()

            return _signals_summary(perf, dict(counts._mapping), wl_count, latest)
    except Exception as e:
        logger.exception("Failed to calculate signals summary: %s", e)
        return _signals_summary(empty)


# Trade Journal Operations
JOURNAL_STATUSES = ("TAKEN", "SKIPPED")


def journal_view(sig, entry) -> dict | None:
    """Journal entry for a signal, with the result of the user's own fills when both are known."""
    if entry is None:
        return None
    view = entry.to_dict()
    pnl, r_multiple = None, None
    if entry.status == "TAKEN" and entry.entry_fill and entry.exit_fill:
        pnl, r_multiple = trade_return(
            sig.side, entry.entry_fill, sig.stop_loss, entry.exit_fill, ROUND_TRIP_FEE_PERCENT
        )
    view["pnl_percent"] = round(pnl, 2) if pnl is not None else None
    view["r_multiple"] = round(r_multiple, 2) if r_multiple is not None else None
    return view


async def _journal_status_by_signal(session: AsyncSession, signal_ids: list[int]) -> dict[int, str]:
    if not signal_ids:
        return {}
    rows = await session.execute(
        select(TradeJournalModel.signal_id, TradeJournalModel.status).where(
            TradeJournalModel.signal_id.in_(signal_ids)
        )
    )
    return {signal_id: status for signal_id, status in rows.all()}


async def upsert_journal_entry(
    signal_id: int,
    status: str,
    entry_fill: float | None = None,
    exit_fill: float | None = None,
    notes: str = "",
) -> dict | None:
    """Create or replace the journal entry for a signal. Returns None when the signal does not exist."""
    if _session_maker is None:
        return None
    if status == "SKIPPED":
        entry_fill = exit_fill = None
    values = {"status": status, "entry_fill": entry_fill, "exit_fill": exit_fill, "notes": notes}

    try:
        async with _session_maker() as session:
            sig = (
                await session.execute(select(MacdSignalModel).where(MacdSignalModel.id == signal_id))
            ).scalar_one_or_none()
            if sig is None:
                return None
            await session.execute(
                pg_insert(TradeJournalModel)
                .values(signal_id=signal_id, **values)
                .on_conflict_do_update(index_elements=["signal_id"], set_={**values, "updated_at": func.now()})
            )
            await session.commit()
            entry = (
                await session.execute(select(TradeJournalModel).where(TradeJournalModel.signal_id == signal_id))
            ).scalar_one()
            return {**sig.to_dict(), "journal": journal_view(sig, entry)}
    except Exception as e:
        logger.exception("Failed to save journal entry for signal %d: %s", signal_id, e)
        raise


async def delete_journal_entry(signal_id: int) -> bool:
    if _session_maker is None:
        return False
    async with _session_maker() as session:
        entry = (
            await session.execute(select(TradeJournalModel).where(TradeJournalModel.signal_id == signal_id))
        ).scalar_one_or_none()
        if entry is None:
            return False
        await session.delete(entry)
        await session.commit()
        return True


def _empty_journal_summary() -> dict:
    return {
        "taken": 0,
        "skipped": 0,
        "closed_taken": 0,
        "your_total_r": 0.0,
        "your_total_pnl_percent": 0.0,
        "signal_r_taken": 0.0,
        "signal_r_skipped": 0.0,
        "signal_closed_taken": 0,
        "signal_closed_skipped": 0,
    }


async def get_journal(
    status: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, dict]:
    """Journal entries joined with their signals, newest signal first, plus a summary of all entries."""
    if _session_maker is None:
        return [], 0, _empty_journal_summary()

    j, sig_m, pl = TradeJournalModel, MacdSignalModel, ProfitLossHistoryModel
    conditions = []
    if status:
        conditions.append(j.status == status.strip().upper())
    if symbol:
        conditions.append(sig_m.symbol == symbol.strip().upper())

    try:
        async with _session_maker() as session:
            base = select(sig_m, j).join(j, j.signal_id == sig_m.id).where(*conditions)
            total = (
                await session.execute(
                    select(func.count(j.id)).join(sig_m, j.signal_id == sig_m.id).where(*conditions)
                )
            ).scalar_one() or 0
            rows = (await session.execute(base.order_by(desc(sig_m.id)).limit(limit).offset(offset))).all()
            items = [{**s.to_dict(), "journal": journal_view(s, e)} for s, e in rows]

            # Summary across every entry, unfiltered, so the comparison is stable while browsing. The
            # signals' own results are net R under the cost model, as on the Signals tab (booked R for
            # rows never restated), and only closed signals add to them
            all_rows = (await session.execute(select(sig_m, j).join(j, j.signal_id == sig_m.id))).all()
            signal_r = dict(
                (
                    await session.execute(
                        select(pl.signal_id, func.coalesce(pl.net_r, pl.r_multiple)).where(
                            pl.signal_id.in_([s.id for s, _ in all_rows]),
                            func.coalesce(pl.net_r, pl.r_multiple).is_not(None),
                        )
                    )
                ).all()
            ) if all_rows else {}

            summary = _empty_journal_summary()
            for s, e in all_rows:
                taken = e.status == "TAKEN"
                summary["taken" if taken else "skipped"] += 1
                if s.id in signal_r:
                    summary["signal_r_taken" if taken else "signal_r_skipped"] += signal_r[s.id]
                    summary["signal_closed_taken" if taken else "signal_closed_skipped"] += 1
                view = journal_view(s, e)
                if taken and view["r_multiple"] is not None:
                    summary["closed_taken"] += 1
                    summary["your_total_r"] += view["r_multiple"]
                    summary["your_total_pnl_percent"] += view["pnl_percent"]
            for key in ("your_total_r", "your_total_pnl_percent", "signal_r_taken", "signal_r_skipped"):
                summary[key] = round(summary[key], 2)
            return items, total, summary
    except Exception as e:
        logger.exception("Failed to query trade journal: %s", e)
        return [], 0, _empty_journal_summary()
