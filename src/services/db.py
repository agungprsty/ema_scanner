import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    desc,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config.settings import DATABASE_URL
from src.strategy.macd import MacdSignal

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), default="2h", index=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    tp1: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    result: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    pnl_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
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
            "risk_reward_ratio": round(self.risk_reward_ratio, 2),
            "cross_time": self.cross_time,
            "exit_time": self.exit_time,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


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
        stmt = select(MacdSignalModel).where(MacdSignalModel.win_status.in_(["TP1_HIT", "SL_HIT"]))
        resolved = (await session.execute(stmt)).scalars().all()
        for rec in resolved:
            pl_check = await session.execute(
                select(ProfitLossHistoryModel).where(ProfitLossHistoryModel.signal_id == rec.id)
            )
            if not pl_check.scalar_one_or_none():
                is_win = rec.win_status == "TP1_HIT"
                exit_price = rec.tp1 if is_win else rec.stop_loss
                entry_val = rec.entry_price or rec.cross_price or exit_price
                if rec.side == "LONG":
                    pnl = ((exit_price - entry_val) / entry_val) * 100 if entry_val > 0 else 0.0
                else:
                    pnl = ((entry_val - exit_price) / entry_val) * 100 if entry_val > 0 else 0.0

                pl_entry = ProfitLossHistoryModel(
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
                    risk_reward_ratio=rec.risk_reward_ratio,
                    cross_time=rec.cross_time,
                    exit_time=rec.cross_time,
                )
                session.add(pl_entry)
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
            stmt = select(MacdSignalModel).where(
                MacdSignalModel.symbol == signal.symbol,
                MacdSignalModel.side == signal.side,
                MacdSignalModel.timeframe == timeframe,
                MacdSignalModel.cross_candle_ms == signal.cross_candle_ms,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                return existing.to_dict()

            record = MacdSignalModel(
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
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.to_dict()
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


async def evaluate_pending_signals(client) -> dict:
    if _session_maker is None or client is None:
        return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0}

    evaluated_count = 0
    profit_count = 0
    loss_count = 0

    try:
        async with _session_maker() as session:
            stmt = select(MacdSignalModel).where(MacdSignalModel.win_status == "PENDING")
            pending_records = (await session.execute(stmt)).scalars().all()

            for rec in pending_records:
                try:
                    raw_klines = await asyncio.to_thread(
                        client.klines,
                        symbol=rec.symbol,
                        interval=rec.timeframe,
                        startTime=rec.cross_candle_ms,
                        limit=100,
                    )
                    if isinstance(raw_klines, dict) and "data" in raw_klines:
                        raw_klines = raw_klines["data"]

                    if not raw_klines or len(raw_klines) < 2:
                        continue

                    resolved_result = None
                    exit_price = 0.0
                    exit_time_str = ""

                    for candle in raw_klines[1:]:
                        high = float(candle[2])
                        low = float(candle[3])
                        candle_ms = int(candle[0])
                        c_dt = datetime.fromtimestamp(candle_ms / 1000, tz=UTC)
                        c_time = c_dt.strftime("%Y-%m-%d %H:%M:%S")

                        if rec.side == "LONG":
                            if rec.tp1 > 0 and high >= rec.tp1:
                                rec.win_status = "TP1_HIT"
                                resolved_result = "PROFIT"
                                exit_price = rec.tp1
                                exit_time_str = c_time
                                break
                            elif rec.stop_loss > 0 and low <= rec.stop_loss:
                                rec.win_status = "SL_HIT"
                                resolved_result = "LOSS"
                                exit_price = rec.stop_loss
                                exit_time_str = c_time
                                break
                        elif rec.side == "SHORT":
                            if rec.tp1 > 0 and low <= rec.tp1:
                                rec.win_status = "TP1_HIT"
                                resolved_result = "PROFIT"
                                exit_price = rec.tp1
                                exit_time_str = c_time
                                break
                            elif rec.stop_loss > 0 and high >= rec.stop_loss:
                                rec.win_status = "SL_HIT"
                                resolved_result = "LOSS"
                                exit_price = rec.stop_loss
                                exit_time_str = c_time
                                break

                    if resolved_result:
                        entry_val = rec.entry_price or rec.cross_price or exit_price
                        if rec.side == "LONG":
                            pnl = ((exit_price - entry_val) / entry_val) * 100 if entry_val > 0 else 0.0
                        else:
                            pnl = ((entry_val - exit_price) / entry_val) * 100 if entry_val > 0 else 0.0

                        pl_check = await session.execute(
                            select(ProfitLossHistoryModel).where(ProfitLossHistoryModel.signal_id == rec.id)
                        )
                        if not pl_check.scalar_one_or_none():
                            pl_entry = ProfitLossHistoryModel(
                                signal_id=rec.id,
                                symbol=rec.symbol,
                                timeframe=rec.timeframe,
                                direction=rec.side,
                                entry_price=entry_val,
                                stop_loss=rec.stop_loss,
                                tp1=rec.tp1,
                                exit_price=exit_price,
                                result=resolved_result,
                                pnl_percent=pnl,
                                risk_reward_ratio=rec.risk_reward_ratio,
                                cross_time=rec.cross_time,
                                exit_time=exit_time_str,
                            )
                            session.add(pl_entry)

                        evaluated_count += 1
                        if resolved_result == "PROFIT":
                            profit_count += 1
                        else:
                            loss_count += 1

                except Exception as ex:
                    logger.debug("Evaluation error for %s: %s", rec.symbol, ex)

            await session.commit()
            return {
                "evaluated_count": evaluated_count,
                "profit_count": profit_count,
                "loss_count": loss_count,
            }
    except Exception as e:
        logger.error("evaluate_pending_signals error: %s", e)
        return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0}


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
        return [], 0, {"total_trades": 0, "profit_count": 0, "loss_count": 0, "winrate": 0.0, "total_pnl_percent": 0.0}

    try:
        async with _session_maker() as session:
            query = select(ProfitLossHistoryModel)
            count_query = select(func.count(ProfitLossHistoryModel.id))

            if symbol:
                clean_sym = symbol.strip().upper()
                query = query.where(ProfitLossHistoryModel.symbol == clean_sym)
                count_query = count_query.where(ProfitLossHistoryModel.symbol == clean_sym)

            if direction:
                clean_dir = direction.strip().upper()
                query = query.where(ProfitLossHistoryModel.direction == clean_dir)
                count_query = count_query.where(ProfitLossHistoryModel.direction == clean_dir)

            if timeframe:
                clean_tf = timeframe.strip().lower()
                query = query.where(ProfitLossHistoryModel.timeframe == clean_tf)
                count_query = count_query.where(ProfitLossHistoryModel.timeframe == clean_tf)

            if result:
                clean_res = result.strip().upper()
                query = query.where(ProfitLossHistoryModel.result == clean_res)
                count_query = count_query.where(ProfitLossHistoryModel.result == clean_res)

            if date:
                clean_date = date.strip()
                query = query.where(
                    (ProfitLossHistoryModel.cross_time.like(f"{clean_date}%"))
                    | (ProfitLossHistoryModel.exit_time.like(f"{clean_date}%"))
                )
                count_query = count_query.where(
                    (ProfitLossHistoryModel.cross_time.like(f"{clean_date}%"))
                    | (ProfitLossHistoryModel.exit_time.like(f"{clean_date}%"))
                )

            if start_date:
                clean_s = start_date.strip()
                query = query.where(ProfitLossHistoryModel.cross_time >= clean_s)
                count_query = count_query.where(ProfitLossHistoryModel.cross_time >= clean_s)

            if end_date:
                clean_e = f"{end_date.strip()} 23:59:59"
                query = query.where(ProfitLossHistoryModel.cross_time <= clean_e)
                count_query = count_query.where(ProfitLossHistoryModel.cross_time <= clean_e)

            total_res = await session.execute(count_query)
            total = total_res.scalar_one() or 0

            records_query = query.order_by(desc(ProfitLossHistoryModel.id)).limit(limit).offset(offset)
            res = await session.execute(records_query)
            items = [r.to_dict() for r in res.scalars().all()]

            profit_q = count_query.where(ProfitLossHistoryModel.result == "PROFIT")
            loss_q = count_query.where(ProfitLossHistoryModel.result == "LOSS")
            pnl_sum_q = select(func.coalesce(func.sum(ProfitLossHistoryModel.pnl_percent), 0.0))

            if symbol:
                pnl_sum_q = pnl_sum_q.where(ProfitLossHistoryModel.symbol == symbol.strip().upper())
            if direction:
                pnl_sum_q = pnl_sum_q.where(ProfitLossHistoryModel.direction == direction.strip().upper())
            if timeframe:
                pnl_sum_q = pnl_sum_q.where(ProfitLossHistoryModel.timeframe == timeframe.strip().lower())
            if result:
                pnl_sum_q = pnl_sum_q.where(ProfitLossHistoryModel.result == result.strip().upper())
            if date:
                clean_date = date.strip()
                pnl_sum_q = pnl_sum_q.where(
                    (ProfitLossHistoryModel.cross_time.like(f"{clean_date}%"))
                    | (ProfitLossHistoryModel.exit_time.like(f"{clean_date}%"))
                )

            profit_cnt = (await session.execute(profit_q)).scalar_one() or 0
            loss_cnt = (await session.execute(loss_q)).scalar_one() or 0
            total_resolved = profit_cnt + loss_cnt
            winrate = round((profit_cnt / total_resolved) * 100, 1) if total_resolved > 0 else 0.0
            total_pnl = round(float((await session.execute(pnl_sum_q)).scalar_one() or 0.0), 2)

            summary = {
                "total_trades": total,
                "profit_count": profit_cnt,
                "loss_count": loss_cnt,
                "winrate": winrate,
                "total_pnl_percent": total_pnl,
            }

            return items, total, summary
    except Exception as e:
        logger.exception("Failed to query profitloss history: %s", e)
        return [], 0, {"total_trades": 0, "profit_count": 0, "loss_count": 0, "winrate": 0.0, "total_pnl_percent": 0.0}


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
            "winrate": 0.0,
            "avg_rr": 1.5,
            "watchlist_count": len(DEFAULT_WATCHLIST),
            "latest_signal_time": None,
            "total_net_pnl": 0.0,
            "closed_trades": 0,
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
            )
            row = (await session.execute(stmt)).one()
            total = row.total or 0
            long_cnt = row.long_cnt or 0
            short_cnt = row.short_cnt or 0
            symbols_cnt = row.symbols_cnt or 0
            tp1_hits = row.tp1_hits or 0
            sl_hits = row.sl_hits or 0
            pending = row.pending or 0

            resolved = tp1_hits + sl_hits
            winrate = round((tp1_hits / resolved) * 100, 1) if resolved > 0 else 0.0

            pl_stmt = select(
                func.coalesce(func.sum(ProfitLossHistoryModel.pnl_percent), 0.0).label("total_pnl"),
                func.count(ProfitLossHistoryModel.id).label("closed_trades"),
            )
            pl_row = (await session.execute(pl_stmt)).one()
            total_net_pnl = round(float(pl_row.total_pnl or 0.0), 2)
            closed_trades = pl_row.closed_trades or resolved

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
                "winrate": winrate,
                "avg_rr": 1.5,
                "watchlist_count": wl_count,
                "latest_signal_time": latest,
                "total_net_pnl": total_net_pnl,
                "closed_trades": closed_trades,
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
            "winrate": 0.0,
            "avg_rr": 1.5,
            "watchlist_count": 0,
            "latest_signal_time": None,
            "total_net_pnl": 0.0,
            "closed_trades": 0,
        }
