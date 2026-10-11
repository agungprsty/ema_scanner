import asyncio
import logging
from datetime import datetime

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


class Base(DeclarativeBase):
    pass


class MacdSignalModel(Base):
    __tablename__ = "macd_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    cross_candle_ms: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    cross_time: Mapped[str] = mapped_column(String(40), nullable=False)
    cross_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
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
            "macd": round(self.macd, 6),
            "signal": round(self.signal, 6),
            "histogram": round(self.histogram, 6),
            "bars_ago": self.bars_ago,
            "is_confirmed": self.is_confirmed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


async def init_db(max_retries: int = 5, retry_interval: float = 2.0) -> bool:
    global _engine, _session_maker
    if _engine is not None:
        return True

    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set — database persistence disabled")
        return False

    logger.info("Initializing PostgreSQL database...")
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
            logger.info("Database initialized and schema verified")
            return True
        except Exception as e:
            logger.warning(
                "Database connection attempt %d/%d failed: %s",
                attempt,
                max_retries,
                e,
            )
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


async def save_macd_signal(signal: MacdSignal, timeframe: str = "4h") -> dict | None:
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
        logger.error("Failed to save MACD signal to database: %s", e, exc_info=True)
        return None


async def get_macd_signals(
    symbol: str | None = None,
    side: str | None = None,
    timeframe: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    if _session_maker is None:
        return [], 0

    try:
        async with _session_maker() as session:
            query = select(MacdSignalModel)
            count_query = select(func.count(MacdSignalModel.id))

            if symbol:
                query = query.where(MacdSignalModel.symbol == symbol.upper().strip())
                count_query = count_query.where(
                    MacdSignalModel.symbol == symbol.upper().strip()
                )
            if side:
                query = query.where(MacdSignalModel.side == side.upper().strip())
                count_query = count_query.where(
                    MacdSignalModel.side == side.upper().strip()
                )
            if timeframe:
                query = query.where(MacdSignalModel.timeframe == timeframe.strip())
                count_query = count_query.where(
                    MacdSignalModel.timeframe == timeframe.strip()
                )

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
        logger.error("Failed to query MACD signals: %s", e, exc_info=True)
        return [], 0


async def get_macd_signals_summary() -> dict:
    if _session_maker is None:
        return {
            "total_signals": 0,
            "long_signals": 0,
            "short_signals": 0,
            "symbols_count": 0,
            "latest_signal_time": None,
        }

    try:
        async with _session_maker() as session:
            total = (
                await session.execute(select(func.count(MacdSignalModel.id)))
            ).scalar_one() or 0
            long_cnt = (
                await session.execute(
                    select(func.count(MacdSignalModel.id)).where(
                        MacdSignalModel.side == "LONG"
                    )
                )
            ).scalar_one() or 0
            short_cnt = (
                await session.execute(
                    select(func.count(MacdSignalModel.id)).where(
                        MacdSignalModel.side == "SHORT"
                    )
                )
            ).scalar_one() or 0
            symbols_cnt = (
                await session.execute(
                    select(func.count(func.distinct(MacdSignalModel.symbol)))
                )
            ).scalar_one() or 0
            latest = (
                await session.execute(
                    select(MacdSignalModel.cross_time).order_by(
                        desc(MacdSignalModel.id)
                    ).limit(1)
                )
            ).scalar_one_or_none()

            return {
                "total_signals": total,
                "long_signals": long_cnt,
                "short_signals": short_cnt,
                "symbols_count": symbols_cnt,
                "latest_signal_time": latest,
            }
    except Exception as e:
        logger.error("Failed to calculate signals summary: %s", e, exc_info=True)
        return {
            "total_signals": 0,
            "long_signals": 0,
            "short_signals": 0,
            "symbols_count": 0,
            "latest_signal_time": None,
        }
