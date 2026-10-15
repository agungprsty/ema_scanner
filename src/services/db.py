import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
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
    func,
    or_,
    select,
    text,
    tuple_,
)
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
    LATE_SIGNAL_GRACE_SECONDS,
    RISK_REWARD_RATIO,
    ROUND_TRIP_FEE_PERCENT,
    SIGNAL_EXPIRY_CANDLES,
    STOP_SLIPPAGE_MULTIPLIER,
    STRATEGY_KEY,
    STRATEGY_VERSION,
    slippage_bps_for,
)
from src.services.performance import DEFINITIONS, ClosedTrade, summarize_trades
from src.strategy.macd import MacdSignal
from src.strategy.outcome import (
    CANDLE_INTERVAL_MS,
    TradeCosts,
    cost_adjusted_return,
    resolve_outcome,
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
COUNTED_RESULTS = ("PROFIT", "LOSS", "EXPIRED")


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
            "uq_macd_signals_symbol_side_tf_candle",
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


SIGNAL_UNIQUE_COLUMNS = ("symbol", "side", "timeframe", "cross_candle_ms")

SIGNAL_SURVIVOR_RANK = """
    SELECT id,
           ROW_NUMBER() OVER w AS rn,
           FIRST_VALUE(id) OVER w AS keep_id
    FROM macd_signals
    WINDOW w AS (
        PARTITION BY symbol, side, timeframe, cross_candle_ms
        ORDER BY (COALESCE(win_status, 'PENDING') <> 'PENDING') DESC, id ASC
    )
"""

UNIQUE_INDEX_MIGRATIONS = (
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
    CREATE UNIQUE INDEX IF NOT EXISTS uq_macd_signals_symbol_side_tf_candle
    ON macd_signals (symbol, side, timeframe, cross_candle_ms);
    """,
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


def build_signal_insert(
    signal: MacdSignal, timeframe: str, source: str = "manual", detected_at: datetime | None = None
):
    if source not in SIGNAL_SOURCES:
        raise ValueError(f"unknown signal source {source!r}")
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
            strategy_key=STRATEGY_KEY,
            strategy_version=STRATEGY_VERSION,
            source=source,
            entry_basis=signal.entry_basis,
            detected_at=detected_at or datetime.now(UTC),
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


def counted_signal(version: str = STRATEGY_VERSION):
    """A signal that counts in the track record of `version`: on time, entered at the next open."""
    sig = MacdSignalModel
    return and_(sig.is_eligible.is_(True), sig.strategy_version == version)


def not_counted_signal(version: str = STRATEGY_VERSION):
    sig = MacdSignalModel
    return or_(sig.is_eligible.is_not(True), sig.strategy_version.is_distinct_from(version))


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
        side, entry, stop_loss, exit_price, result, ROUND_TRIP_FEE_PERCENT, slippage, STOP_SLIPPAGE_MULTIPLIER
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
    try:
        stmt = select(MacdSignalModel).where(
            MacdSignalModel.win_status.in_(["TP1_HIT", "SL_HIT"]),
            ~exists().where(ProfitLossHistoryModel.signal_id == MacdSignalModel.id),
        )
        resolved = (await session.execute(stmt)).scalars().all()
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
            await session.execute(build_profitloss_insert(**values))
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
                ):
                    await conn.execute(text(migration))

            async with _session_maker() as session:
                await seed_default_watchlist(session)
                await backfill_resolved_signals_to_profitloss_history(session)
                await backfill_profitloss_costs(session)

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

    try:
        async with _session_maker() as session:
            stmt = build_signal_insert(signal, timeframe, source=source, detected_at=detected_at)
            inserted_id = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()

            if inserted_id is not None:
                stmt = select(MacdSignalModel).where(MacdSignalModel.id == inserted_id)
            else:
                stmt = select(MacdSignalModel).where(
                    MacdSignalModel.symbol == signal.symbol,
                    MacdSignalModel.side == signal.side,
                    MacdSignalModel.timeframe == timeframe,
                    MacdSignalModel.cross_candle_ms == signal.cross_candle_ms,
                )
            record = (await session.execute(stmt)).scalars().first()
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
    try:
        async with _session_maker() as session:
            agg = (
                await session.execute(
                    select(
                        func.count(sig.id).label("total"),
                        func.count().filter(sig.win_status == "PENDING").label("open"),
                        func.count().filter(sig.win_status == "TP1_HIT").label("profit"),
                        func.count().filter(sig.win_status == "SL_HIT").label("loss"),
                        func.count().filter(sig.win_status == "EXPIRED").label("expired"),
                        func.count(pl.id).label("closed"),
                        func.coalesce(func.sum(pl.r_multiple), 0.0).label("total_r"),
                        func.coalesce(func.sum(pl.pnl_percent), 0.0).label("pnl"),
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
            stmt = select(MacdSignalModel).where(
                MacdSignalModel.timeframe == timeframe,
                tuple_(
                    MacdSignalModel.symbol,
                    MacdSignalModel.side,
                    MacdSignalModel.cross_candle_ms,
                ).in_(list(set(keys))),
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


def _empty_evaluation() -> dict:
    return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0, "expired_count": 0, "closed": []}


async def _evaluate_pending_signals_unlocked(client) -> dict:
    """Close pending signals that reached their target, their stop, or their expiry.

    The trade is booked under the cost model: pnl_percent and r_multiple are net of fees and
    modelled slippage, with gross_r, cost_r and net_r stored alongside. Returns counts plus
    `closed`, the trades this run recorded, for outcome alerts.
    """
    if _session_maker is None or client is None:
        return _empty_evaluation()

    summary = _empty_evaluation()
    counters = {"PROFIT": "profit_count", "LOSS": "loss_count", "EXPIRED": "expired_count"}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    try:
        async with _session_maker() as session:
            stmt = select(MacdSignalModel).where(MacdSignalModel.win_status == "PENDING")
            pending_records = (await session.execute(stmt)).scalars().all()

            for rec in pending_records:
                try:
                    interval_ms = CANDLE_INTERVAL_MS.get(rec.timeframe, CANDLE_INTERVAL_MS["2h"])
                    raw_klines = await asyncio.to_thread(
                        client.klines,
                        symbol=rec.symbol,
                        interval=rec.timeframe,
                        startTime=rec.cross_candle_ms,
                        limit=min(SIGNAL_EXPIRY_CANDLES + 1, 1500),
                    )
                    if isinstance(raw_klines, dict) and "data" in raw_klines:
                        raw_klines = raw_klines["data"]
                    if not raw_klines or len(raw_klines) < 2:
                        continue

                    # The first kline is the cross candle itself; the trade starts on the next one
                    outcome = resolve_outcome(
                        rec.side,
                        rec.stop_loss,
                        rec.tp1,
                        raw_klines[1:],
                        SIGNAL_EXPIRY_CANDLES,
                        now_ms,
                        interval_ms,
                    )
                    if outcome is None:
                        continue

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

                    summary["evaluated_count"] += 1
                    summary[counters[outcome.result]] += 1
                    if inserted is not None:
                        summary["closed"].append(values)

                except Exception as ex:
                    logger.warning("Evaluation error for %s: %s", rec.symbol, ex)

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
    """Store a hand-entered trade. It is labelled booked_by="manual", never restated under the cost
    model and never counted in the track record. Raises SignalTradeConflict when `signal_id` names a
    stored signal: the evaluator books that signal's outcome, and a manual row would take its slot."""
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
            record = ProfitLossHistoryModel(
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
                booked_by="manual",
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.to_dict()
    except Exception as e:
        logger.error("Failed to record profit/loss history: %s", e)
        return None


PROFITLOSS_SUMMARY_NOTE = (
    "winrate, total_r, avg_r and total_pnl_percent are as booked over every listed row, including legacy "
    "fee-free rows and hand-entered ones, and are deprecated. The eligible_* fields count only trades the "
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
            agg = (
                await session.execute(
                    select(
                        func.count(pl.id).label("total"),
                        func.count().filter(pl.result == "PROFIT").label("profit"),
                        func.count().filter(pl.result == "LOSS").label("loss"),
                        func.count().filter(pl.result == "EXPIRED").label("expired"),
                        func.coalesce(func.sum(pl.pnl_percent), 0.0).label("pnl"),
                        func.coalesce(func.sum(pl.r_multiple), 0.0).label("total_r"),
                        func.avg(pl.r_multiple).label("avg_r"),
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
            eligible_closed = agg.eligible_closed or 0
            eligible_wins = agg.eligible_wins or 0
            summary = {
                "total_trades": total,
                "profit_count": agg.profit or 0,
                "loss_count": agg.loss or 0,
                "expired_count": agg.expired or 0,
                "winrate": round((agg.profit or 0) / total * 100, 1) if total else 0.0,
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


# Why a signal is kept out of a version's statistics: its source, a different strategy version, or
# (a catch-all that should stay at zero) a scheduled signal not entered at the next open
EXCLUSION_REASONS = ("legacy", "catchup", "manual", "other_version", "other")


def _performance_payload(
    version: str,
    symbol: str | None,
    side: str | None,
    trades: list[ClosedTrade],
    open_count: int = 0,
    excluded: dict | None = None,
) -> dict:
    return {
        "strategy_key": STRATEGY_KEY,
        "strategy_version": version,
        "cost_model_version": COST_MODEL_VERSION,
        "filters": {"symbol": symbol, "side": side},
        **summarize_trades(trades, RISK_REWARD_RATIO),
        "open_count": open_count,
        "excluded": {reason: (excluded or {}).get(reason, 0) for reason in EXCLUSION_REASONS},
        "definitions": DEFINITIONS,
    }


async def _performance_report(
    session: AsyncSession, version: str, symbol: str | None = None, side: str | None = None
) -> dict:
    """KPIs of the eligible closed trades of one strategy version, plus what was left out and why.

    A trade counts only when its signal is eligible and of that version and the evaluator booked it
    (counted_trade). Excluded counts cover every other signal, open or closed, in the symbol/side
    scope, each under one reason, so counted plus excluded is every stored signal.
    """
    sig, pl = MacdSignalModel, ProfitLossHistoryModel
    scope = []
    if symbol:
        scope.append(sig.symbol == symbol)
    if side:
        scope.append(sig.side == side)
    eligible = [counted_signal(version), *scope]

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
        (sig.source == "legacy", "legacy"),
        (sig.strategy_version.is_distinct_from(version), "other_version"),
        (sig.source.in_(("catchup", "manual")), sig.source),
        else_="other",
    )
    excluded = await session.execute(
        select(reason, func.count(sig.id)).where(not_counted_signal(version), *scope).group_by(reason)
    )
    return _performance_payload(version, symbol, side, trades, open_count, dict(excluded.all()))


async def get_performance(
    strategy_version: str | None = None, symbol: str | None = None, side: str | None = None
) -> dict:
    version = (strategy_version or "").strip() or STRATEGY_VERSION
    symbol = (symbol or "").strip().upper() or None
    side = (side or "").strip().upper() or None
    if _session_maker is None:
        return _performance_payload(version, symbol, side, [])

    try:
        async with _session_maker() as session:
            return await _performance_report(session, version, symbol, side)
    except Exception:
        logger.exception("Failed to calculate performance")
        return _performance_payload(version, symbol, side, [])


SUMMARY_NOTE = (
    "Outcome fields (target, stop and expired counts, win rate, closed trades, R and drawdown) count only "
    "eligible trades of strategy_version, the same as /api/performance. Signal counts cover every recorded signal."
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
                )
            ).one()

            wl_count = (
                await session.execute(
                    select(func.count(WatchlistModel.id)).where(WatchlistModel.is_active.is_(True))
                )
            ).scalar_one() or 0

            latest = (
                await session.execute(
                    select(sig.cross_time).order_by(desc(sig.cross_candle_ms), desc(sig.id)).limit(1)
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
