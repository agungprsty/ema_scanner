import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    desc,
    exists,
    func,
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
    DATABASE_URL,
    RISK_REWARD_RATIO,
    ROUND_TRIP_FEE_PERCENT,
    SIGNAL_EXPIRY_CANDLES,
)
from src.strategy.macd import MacdSignal
from src.strategy.outcome import CANDLE_INTERVAL_MS, resolve_outcome, trade_return

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None
_evaluation_lock = asyncio.Lock()

DEFAULT_WATCHLIST = [
    "BTCUSDT",
    "ETHUSDT"
]


class Base(DeclarativeBase):
    pass


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
        }


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

UTC_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime(UTC_ISO)


def build_signal_insert(signal: MacdSignal, timeframe: str):
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


async def seed_default_watchlist(session: AsyncSession) -> None:
    res = await session.execute(select(func.count(WatchlistModel.id)))
    count = res.scalar_one() or 0
    if count == 0:
        for sym in DEFAULT_WATCHLIST:
            session.add(WatchlistModel(symbol=sym, is_active=True, notes="Default Pair"))
        await session.commit()
        logger.info("Default watchlist seeded with %d symbols", len(DEFAULT_WATCHLIST))


async def backfill_resolved_signals_to_profitloss_history(session: AsyncSession) -> None:
    try:
        stmt = select(MacdSignalModel).where(
            MacdSignalModel.win_status.in_(["TP1_HIT", "SL_HIT"]),
            ~exists().where(ProfitLossHistoryModel.signal_id == MacdSignalModel.id),
        )
        resolved = (await session.execute(stmt)).scalars().all()
        for rec in resolved:
            is_win = rec.win_status == "TP1_HIT"
            exit_price = rec.tp1 if is_win else rec.stop_loss
            entry_val = rec.entry_price or rec.cross_price or exit_price
            pnl, r_multiple = trade_return(rec.side, entry_val, rec.stop_loss, exit_price, 0.0)

            await session.execute(
                build_profitloss_insert(
                    signal_id=rec.id,
                    symbol=rec.symbol,
                    timeframe=rec.timeframe,
                    direction=rec.side,
                    entry_price=entry_val,
                    stop_loss=rec.stop_loss,
                    tp1=rec.tp1,
                    exit_price=exit_price,
                    result="PROFIT" if is_win else "LOSS",
                    pnl_percent=pnl,
                    r_multiple=r_multiple,
                    risk_reward_ratio=rec.risk_reward_ratio,
                    cross_time=rec.cross_time,
                    exit_time=rec.cross_time,
                )
            )
        await session.commit()
    except Exception as e:
        logger.debug("Backfill profitloss_history error: %s", e)


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
                for migration in (*UNIQUE_INDEX_MIGRATIONS, *PROFITLOSS_R_MIGRATIONS):
                    await conn.execute(text(migration))

            async with _session_maker() as session:
                await seed_default_watchlist(session)
                await backfill_resolved_signals_to_profitloss_history(session)

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
async def save_macd_signal(signal: MacdSignal, timeframe: str = "2h") -> dict | None:
    if _session_maker is None:
        return None

    try:
        async with _session_maker() as session:
            inserted_id = (await session.execute(build_signal_insert(signal, timeframe))).scalar_one_or_none()
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
) -> tuple[list[dict], int]:
    if _session_maker is None:
        return [], 0

    target_side = (direction or side or "").strip().upper() or None
    target_status = None
    if result:
        res_upper = result.strip().upper()
        if res_upper == "PROFIT":
            target_status = "TP1_HIT"
        elif res_upper == "LOSS":
            target_status = "SL_HIT"
        elif res_upper == "PENDING":
            target_status = "PENDING"
        else:
            target_status = res_upper
    elif win_status:
        target_status = win_status.strip().upper()

    try:
        async with _session_maker() as session:
            query = select(MacdSignalModel)
            count_query = select(func.count(MacdSignalModel.id))

            if symbol:
                clean_sym = symbol.upper().strip()
                query = query.where(MacdSignalModel.symbol == clean_sym)
                count_query = count_query.where(MacdSignalModel.symbol == clean_sym)
            if target_side:
                query = query.where(MacdSignalModel.side == target_side)
                count_query = count_query.where(MacdSignalModel.side == target_side)
            if timeframe:
                clean_tf = timeframe.strip().lower()
                query = query.where(MacdSignalModel.timeframe == clean_tf)
                count_query = count_query.where(MacdSignalModel.timeframe == clean_tf)
            if target_status:
                query = query.where(MacdSignalModel.win_status == target_status)
                count_query = count_query.where(MacdSignalModel.win_status == target_status)

            if date:
                clean_date = date.strip()
                query = query.where(MacdSignalModel.cross_time.like(f"{clean_date}%"))
                count_query = count_query.where(MacdSignalModel.cross_time.like(f"{clean_date}%"))

            if start_date:
                clean_start = start_date.strip()
                query = query.where(MacdSignalModel.cross_time >= clean_start)
                count_query = count_query.where(MacdSignalModel.cross_time >= clean_start)

            if end_date:
                clean_end = f"{end_date.strip()} 23:59:59"
                query = query.where(MacdSignalModel.cross_time <= clean_end)
                count_query = count_query.where(MacdSignalModel.cross_time <= clean_end)

            total_res = await session.execute(count_query)
            total = total_res.scalar_one() or 0

            query = (
                query.order_by(desc(MacdSignalModel.id))
                .limit(limit)
                .offset(offset)
            )
            res = await session.execute(query)
            records = res.scalars().all()
            return [r.to_dict() for r in records], total
    except Exception as e:
        logger.exception("Failed to query MACD signals: %s", e)
        return [], 0


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
            return sig.to_dict() if sig else None
    except Exception as e:
        logger.error("Failed to get signal %d: %s", signal_id, e)
        return None


def _empty_evaluation() -> dict:
    return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0, "expired_count": 0, "closed": []}


async def _evaluate_pending_signals_unlocked(client) -> dict:
    """Close pending signals that reached their target, their stop, or their expiry.

    Returns counts plus `closed`, the trades this run recorded, for outcome alerts.
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

                    entry_val = rec.entry_price or rec.cross_price or outcome.exit_price
                    pnl, r_multiple = trade_return(
                        rec.side, entry_val, rec.stop_loss, outcome.exit_price, ROUND_TRIP_FEE_PERCENT
                    )
                    values = {
                        "signal_id": rec.id,
                        "symbol": rec.symbol,
                        "timeframe": rec.timeframe,
                        "direction": rec.side,
                        "entry_price": entry_val,
                        "stop_loss": rec.stop_loss,
                        "tp1": rec.tp1,
                        "exit_price": outcome.exit_price,
                        "result": outcome.result,
                        "pnl_percent": pnl,
                        "r_multiple": r_multiple,
                        "fee_percent": ROUND_TRIP_FEE_PERCENT,
                        "risk_reward_ratio": rec.risk_reward_ratio,
                        "cross_time": rec.cross_time,
                        "exit_time": _ms_to_iso(outcome.exit_time_ms),
                    }
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
    if _session_maker is None:
        return None

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
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.to_dict()
    except Exception as e:
        logger.error("Failed to record profit/loss history: %s", e)
        return None


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
    }


def _max_drawdown(values: list[float]) -> float:
    """Largest peak-to-trough fall of the running total, as a non-negative number."""
    peak = total = worst = 0.0
    for v in values:
        total += v
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


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

    pl = ProfitLossHistoryModel
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
    if start_date:
        conditions.append(pl.cross_time >= start_date.strip())
    if end_date:
        conditions.append(pl.cross_time <= f"{end_date.strip()} 23:59:59")

    try:
        async with _session_maker() as session:
            records_query = select(pl).where(*conditions).order_by(desc(pl.id)).limit(limit).offset(offset)
            items = [r.to_dict() for r in (await session.execute(records_query)).scalars().all()]

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
                    ).where(*conditions)
                )
            ).one()

            total = agg.total or 0
            summary = {
                "total_trades": total,
                "profit_count": agg.profit or 0,
                "loss_count": agg.loss or 0,
                "expired_count": agg.expired or 0,
                "winrate": round((agg.profit or 0) / total * 100, 1) if total else 0.0,
                "total_pnl_percent": round(float(agg.pnl or 0.0), 2),
                "total_r": round(float(agg.total_r or 0.0), 2),
                "avg_r": round(float(agg.avg_r or 0.0), 3),
            }
            return items, total, summary
    except Exception as e:
        logger.exception("Failed to query profitloss history: %s", e)
        return [], 0, _empty_profitloss_summary()


async def get_macd_signals_summary() -> dict:
    if _session_maker is None:
        return {
            "total_signals": 0,
            "long_signals": 0,
            "short_signals": 0,
            "symbols_count": 0,
            "tp1_hit_count": 0,
            "sl_hit_count": 0,
            "pending_count": 0,
            "expired_count": 0,
            "winrate": 0.0,
            "avg_rr": 1.5,
            "watchlist_count": len(DEFAULT_WATCHLIST),
            "latest_signal_time": None,
            "total_net_pnl": 0.0,
            "closed_trades": 0,
            "total_r": 0.0,
            "avg_r": 0.0,
            "max_drawdown_r": 0.0,
        }

    try:
        async with _session_maker() as session:
            stmt = select(
                func.count(MacdSignalModel.id).label("total"),
                func.count().filter(MacdSignalModel.side == "LONG").label("long_cnt"),
                func.count().filter(MacdSignalModel.side == "SHORT").label("short_cnt"),
                func.count(func.distinct(MacdSignalModel.symbol)).label("symbols_cnt"),
                func.count().filter(MacdSignalModel.win_status == "TP1_HIT").label("tp1_hits"),
                func.count().filter(MacdSignalModel.win_status == "SL_HIT").label("sl_hits"),
                func.count().filter(MacdSignalModel.win_status == "PENDING").label("pending"),
                func.count().filter(MacdSignalModel.win_status == "EXPIRED").label("expired"),
            )
            row = (await session.execute(stmt)).one()
            total = row.total or 0
            long_cnt = row.long_cnt or 0
            short_cnt = row.short_cnt or 0
            symbols_cnt = row.symbols_cnt or 0
            tp1_hits = row.tp1_hits or 0
            sl_hits = row.sl_hits or 0
            pending = row.pending or 0
            expired = row.expired or 0

            resolved = tp1_hits + sl_hits + expired
            winrate = round((tp1_hits / resolved) * 100, 1) if resolved > 0 else 0.0

            pl_stmt = select(
                func.coalesce(func.sum(ProfitLossHistoryModel.pnl_percent), 0.0).label("total_pnl"),
                func.count(ProfitLossHistoryModel.id).label("closed_trades"),
            )
            pl_row = (await session.execute(pl_stmt)).one()
            total_net_pnl = round(float(pl_row.total_pnl or 0.0), 2)
            closed_trades = pl_row.closed_trades or resolved

            r_values = [
                float(v)
                for v in (
                    await session.execute(
                        select(ProfitLossHistoryModel.r_multiple)
                        .where(ProfitLossHistoryModel.r_multiple.is_not(None))
                        .order_by(ProfitLossHistoryModel.exit_time, ProfitLossHistoryModel.id)
                    )
                ).scalars().all()
            ]
            total_r = sum(r_values)

            wl_count = (
                await session.execute(
                    select(func.count(WatchlistModel.id)).where(WatchlistModel.is_active.is_(True))
                )
            ).scalar_one() or 0

            latest = (
                await session.execute(
                    select(MacdSignalModel.cross_time).order_by(desc(MacdSignalModel.id)).limit(1)
                )
            ).scalar_one_or_none()

            return {
                "total_signals": total,
                "long_signals": long_cnt,
                "short_signals": short_cnt,
                "symbols_count": symbols_cnt,
                "tp1_hit_count": tp1_hits,
                "sl_hit_count": sl_hits,
                "pending_count": pending,
                "expired_count": expired,
                "winrate": winrate,
                "avg_rr": RISK_REWARD_RATIO,
                "watchlist_count": wl_count,
                "latest_signal_time": latest,
                "total_net_pnl": total_net_pnl,
                "closed_trades": closed_trades,
                "total_r": round(total_r, 2),
                "avg_r": round(total_r / len(r_values), 3) if r_values else 0.0,
                "max_drawdown_r": round(_max_drawdown(r_values), 2),
            }
    except Exception as e:
        logger.exception("Failed to calculate signals summary: %s", e)
        return {
            "total_signals": 0,
            "long_signals": 0,
            "short_signals": 0,
            "symbols_count": 0,
            "tp1_hit_count": 0,
            "sl_hit_count": 0,
            "pending_count": 0,
            "expired_count": 0,
            "winrate": 0.0,
            "avg_rr": 1.5,
            "watchlist_count": 0,
            "latest_signal_time": None,
            "total_net_pnl": 0.0,
            "closed_trades": 0,
            "total_r": 0.0,
            "avg_r": 0.0,
            "max_drawdown_r": 0.0,
        }
