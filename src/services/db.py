import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    desc,
    func,
    select,
    text,
    update,
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
    FILL_WINDOW_HOURS,
    INTERVAL_MS,
    MAX_HOLD_HOURS,
    ROUND_TRIP_FEE_PERCENT,
    TIMEFRAME,
)
from src.strategy.outcome import resolve, trade_return
from src.strategy.rsi import Signal

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None
_evaluation_lock = asyncio.Lock()

DEFAULT_WATCHLIST = [
    "BTCUSDT",
    "ETHUSDT"
]

STATUSES = ("PENDING", "OPEN", "TP_HIT", "SL_HIT", "EXPIRED", "NOT_FILLED")
ACTIVE_STATUSES = ("PENDING", "OPEN")
# Forward moves only, so a short or empty klines reply can never reopen or unfill a trade
NEXT_STATUSES = {
    "PENDING": {"OPEN", "TP_HIT", "SL_HIT", "EXPIRED", "NOT_FILLED"},
    "OPEN": {"TP_HIT", "SL_HIT", "EXPIRED"},
}
KLINES_PAGE_LIMIT = 1500
UTC_ISO = "%Y-%m-%dT%H:%M:%SZ"
WIB = timezone(timedelta(hours=7))
DAY_MS = 86_400_000


def _ms_to_iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime(UTC_ISO) if ms is not None else None


def window_candles(interval_ms: int) -> tuple[int, int]:
    """(fill window, max hold) in candles of `interval_ms`, from the hour settings.

    Same rounding as settings.FILL_WINDOW_CANDLES, but per row, so rows of an earlier
    TIMEFRAME keep their own windows.
    """
    def count(hours: float) -> int:
        return max(1, math.ceil(hours * 3_600_000 / interval_ms))

    return count(FILL_WINDOW_HOURS), count(MAX_HOLD_HOURS)


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


class SignalModel(Base):
    """An RSI signal and the limit-entry trade replayed from it."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("symbol", "side", "timeframe", "signal_candle_ms", name="uq_signals_key"),
        CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_signals_side"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")", name="ck_signals_status"
        ),
        Index(
            "ix_signals_active",
            "status",
            postgresql_where=text("status IN ('PENDING', 'OPEN')"),
        ),
        Index("ix_signals_candle", "signal_candle_ms"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    signal_candle_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)  # open time of the closed signal candle
    rsi: Mapped[float] = mapped_column(Float, nullable=False)
    prev_rsi: Mapped[float] = mapped_column(Float, nullable=False)
    candle_close: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)  # limit: low (LONG) / high (SHORT)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="PENDING", server_default="PENDING"
    )
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pnl_percent: Mapped[float | None] = mapped_column(Float, nullable=True)  # net of fees, closed trades only
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self) -> dict:
        interval_ms = INTERVAL_MS.get(self.timeframe)
        fill_deadline = hold_deadline = None
        if interval_ms:
            fill_window, max_hold = window_candles(interval_ms)
            fill_deadline = self.signal_candle_ms + (fill_window + 1) * interval_ms
            if self.fill_ms is not None:
                hold_deadline = self.fill_ms + max_hold * interval_ms
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "timeframe": self.timeframe,
            "signal_candle_ms": self.signal_candle_ms,
            "signal_time": _ms_to_iso(self.signal_candle_ms),
            "rsi": round(self.rsi, 2),
            "prev_rsi": round(self.prev_rsi, 2),
            "candle_close": self.candle_close,
            "entry_price": self.entry_price,
            "take_profit": self.take_profit,
            "stop_loss": self.stop_loss,
            "status": self.status,
            "fill_price": self.fill_price,
            "fill_ms": self.fill_ms,
            "fill_time": _ms_to_iso(self.fill_ms),
            "exit_price": self.exit_price,
            "closed_ms": self.closed_ms,
            "exit_time": _ms_to_iso(self.closed_ms),
            "pnl_percent": round(self.pnl_percent, 2) if self.pnl_percent is not None else None,
            "r_multiple": round(self.r_multiple, 2) if self.r_multiple is not None else None,
            "fill_deadline": _ms_to_iso(fill_deadline),
            "hold_deadline": _ms_to_iso(hold_deadline),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class JournalModel(Base):
    """What the user actually did with a signal: taken or skipped, and at which fills."""

    __tablename__ = "signal_journal"
    __table_args__ = (CheckConstraint("status IN ('TAKEN', 'SKIPPED')", name="ck_signal_journal_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
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


def build_signals_insert(signals: list[Signal], timeframe: str = TIMEFRAME):
    """One INSERT for a run's hits; signals already stored are skipped and not returned."""
    return (
        pg_insert(SignalModel)
        .values([
            {
                "symbol": s.symbol,
                "side": s.side,
                "timeframe": timeframe,
                "signal_candle_ms": s.signal_candle_ms,
                "rsi": s.rsi,
                "prev_rsi": s.prev_rsi,
                "candle_close": s.candle_close,
                "entry_price": s.entry_price,
                "take_profit": s.take_profit,
                "stop_loss": s.stop_loss,
            }
            for s in signals
        ])
        .on_conflict_do_nothing(constraint="uq_signals_key")
        .returning(SignalModel)
    )


def build_transition_update(signal_id: int, old_status: str, values: dict):
    """UPDATE that only wins while the row still has `old_status`, so of two workers replaying
    the same trade only one sees the transition and alerts it."""
    return (
        update(SignalModel)
        .where(SignalModel.id == signal_id, SignalModel.status == old_status)
        .values(**values)
        .returning(SignalModel.id)
    )


def build_alert_claim(since_ms: int):
    """UPDATE that marks recent never-alerted signals as alerted and returns them, so of two
    runs only one sends each. Older rows are past the scan lookback and are left alone."""
    return (
        update(SignalModel)
        .where(SignalModel.alerted_at.is_(None), SignalModel.signal_candle_ms >= since_ms)
        .values(alerted_at=func.now())
        .returning(SignalModel)
        .execution_options(synchronize_session=False)
    )


async def seed_default_watchlist(session: AsyncSession) -> None:
    res = await session.execute(select(func.count(WatchlistModel.id)))
    count = res.scalar_one() or 0
    if count == 0:
        for sym in DEFAULT_WATCHLIST:
            session.add(WatchlistModel(symbol=sym, is_active=True, notes="Default Pair"))
        await session.commit()
        logger.info("Default watchlist seeded with %d symbols", len(DEFAULT_WATCHLIST))


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

            async with _session_maker() as session:
                await seed_default_watchlist(session)

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
async def insert_signals(signals: list[Signal], timeframe: str = TIMEFRAME) -> list[dict]:
    """Store a run's hits and return only the rows that were new. Raises when the database is
    unavailable, so a run never reports zero signals because nothing could be stored."""
    if not signals:
        return []
    if _session_maker is None:
        raise RuntimeError("Database not initialized")

    async with _session_maker() as session:
        rows = (await session.execute(build_signals_insert(signals, timeframe))).scalars().all()
        await session.commit()
        return [r.to_dict() for r in rows]


async def claim_unalerted_signals(since_ms: int) -> list[dict]:
    """Signals from `since_ms` on that no run has alerted yet, oldest first, now marked alerted.

    Alerting from here instead of from insert_signals' new rows means a run that dies after
    storing its signals leaves them for the next run. Raises when the database is unavailable.
    """
    if _session_maker is None:
        raise RuntimeError("Database not initialized")

    async with _session_maker() as session:
        rows = (await session.execute(build_alert_claim(since_ms))).scalars().all()
        await session.commit()
        return [r.to_dict() for r in sorted(rows, key=lambda r: r.id)]


async def release_signal_alerts(signal_ids: list[int]) -> None:
    """Undo a claim whose alert was not delivered, so the next run sends it again."""
    if not signal_ids or _session_maker is None:
        return
    async with _session_maker() as session:
        await session.execute(
            update(SignalModel).where(SignalModel.id.in_(signal_ids)).values(alerted_at=None)
        )
        await session.commit()


def _wib_day_ms(day: str) -> int:
    """Midnight WIB at the start of a YYYY-MM-DD day, in UTC ms. Raises ValueError on bad input."""
    return int(datetime.strptime(day.strip(), "%Y-%m-%d").replace(tzinfo=WIB).timestamp() * 1000)


def signal_conditions(
    status: str | None = None,
    side: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list:
    """Filters on signals. `status` may list several, comma separated; dates are WIB days."""
    sig = SignalModel
    conditions = []
    statuses = [s.strip().upper() for s in (status or "").split(",") if s.strip()]
    if statuses:
        conditions.append(sig.status.in_(statuses))
    if side:
        conditions.append(sig.side == side.strip().upper())
    if symbol:
        conditions.append(sig.symbol == symbol.strip().upper())
    if timeframe:
        conditions.append(sig.timeframe == timeframe.strip())
    if date:
        day = _wib_day_ms(date)
        conditions += [sig.signal_candle_ms >= day, sig.signal_candle_ms < day + DAY_MS]
    if start_date:
        conditions.append(sig.signal_candle_ms >= _wib_day_ms(start_date))
    if end_date:
        conditions.append(sig.signal_candle_ms < _wib_day_ms(end_date) + DAY_MS)
    return conditions


def _max_drawdown(values: list[float]) -> float:
    """Largest peak-to-trough fall of the running total, as a non-negative number."""
    peak = total = worst = 0.0
    for v in values:
        total += v
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def build_stats(
    counts: dict,
    total_pnl: float = 0.0,
    r_values: list[float] | None = None,
    watchlist_count: int = 0,
    latest_signal_ms: int | None = None,
) -> dict:
    """Summary from per-status counts (`counts` also holds total/long/short/symbols).

    Not filled signals never traded, so they count against the fill rate but not the win rate.
    """
    r_values = r_values or []
    tp, sl, expired = counts.get("TP_HIT", 0), counts.get("SL_HIT", 0), counts.get("EXPIRED", 0)
    not_filled = counts.get("NOT_FILLED", 0)
    closed = tp + sl + expired
    filled = counts.get("OPEN", 0) + closed
    total_r = sum(r_values)
    return {
        "total_signals": counts.get("total", 0),
        "long_signals": counts.get("long", 0),
        "short_signals": counts.get("short", 0),
        "symbols_count": counts.get("symbols", 0),
        "pending_count": counts.get("PENDING", 0),
        "open_count": counts.get("OPEN", 0),
        "tp_hit_count": tp,
        "sl_hit_count": sl,
        "expired_count": expired,
        "not_filled_count": not_filled,
        "filled_count": filled,
        "fill_rate": round(filled / (filled + not_filled) * 100, 1) if filled + not_filled else 0.0,
        "closed_trades": closed,
        "winrate": round(tp / closed * 100, 1) if closed else 0.0,
        "total_net_pnl": round(float(total_pnl or 0.0), 2),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / closed, 3) if closed else 0.0,
        "max_drawdown_r": round(_max_drawdown(r_values), 2),
        "watchlist_count": watchlist_count,
        "latest_signal_time": _ms_to_iso(latest_signal_ms),
    }


async def signal_stats(conditions: list | tuple = ()) -> dict:
    """Summary of every signal matching `conditions` (from signal_conditions); all signals by default."""
    if _session_maker is None:
        return build_stats({}, watchlist_count=len(DEFAULT_WATCHLIST))

    sig = SignalModel
    try:
        async with _session_maker() as session:
            row = (
                await session.execute(
                    select(
                        func.count(sig.id).label("total"),
                        func.count().filter(sig.side == "LONG").label("long"),
                        func.count().filter(sig.side == "SHORT").label("short"),
                        func.count(func.distinct(sig.symbol)).label("symbols"),
                        *(func.count().filter(sig.status == s).label(s) for s in STATUSES),
                        func.coalesce(func.sum(sig.pnl_percent), 0.0).label("pnl"),
                        func.max(sig.signal_candle_ms).label("latest"),
                    ).where(*conditions)
                )
            ).one()
            r_values = [
                float(v)
                for v in (
                    await session.execute(
                        select(sig.r_multiple)
                        .where(*conditions, sig.r_multiple.is_not(None))
                        .order_by(sig.closed_ms, sig.id)
                    )
                ).scalars().all()
            ]
            wl_count = (
                await session.execute(
                    select(func.count(WatchlistModel.id)).where(WatchlistModel.is_active.is_(True))
                )
            ).scalar_one() or 0
            counts = {k: getattr(row, k) or 0 for k in ("total", "long", "short", "symbols", *STATUSES)}
            return build_stats(counts, row.pnl, r_values, wl_count, row.latest)
    except Exception as e:
        logger.exception("Failed to calculate signal stats: %s", e)
        return build_stats({})


async def list_signals(
    status: str | None = None,
    side: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, dict]:
    """Signals newest first, each with its journal status, plus the total and summary of every
    signal matching the filters. Raises ValueError on a malformed date."""
    conditions = signal_conditions(status, side, symbol, timeframe, date, start_date, end_date)
    summary = await signal_stats(conditions)
    if _session_maker is None:
        return [], 0, summary

    sig = SignalModel
    try:
        async with _session_maker() as session:
            query = (
                select(sig)
                .where(*conditions)
                .order_by(desc(sig.signal_candle_ms), desc(sig.id))
                .limit(limit)
                .offset(offset)
            )
            records = (await session.execute(query)).scalars().all()
            journal = await _journal_status_by_signal(session, [r.id for r in records])
            items = [{**r.to_dict(), "journal_status": journal.get(r.id)} for r in records]
            return items, summary["total_signals"], summary
    except Exception as e:
        logger.exception("Failed to query signals: %s", e)
        return [], 0, summary


async def get_signal(signal_id: int) -> dict | None:
    if _session_maker is None:
        return None

    try:
        async with _session_maker() as session:
            sig = (
                await session.execute(select(SignalModel).where(SignalModel.id == signal_id))
            ).scalar_one_or_none()
            if sig is None:
                return None
            entry = (
                await session.execute(select(JournalModel).where(JournalModel.signal_id == signal_id))
            ).scalar_one_or_none()
            return {**sig.to_dict(), "journal": journal_view(sig, entry)}
    except Exception as e:
        logger.error("Failed to get signal %d: %s", signal_id, e)
        return None


def fetch_trade_klines(client, symbol: str, timeframe: str, start_ms: int, count: int, interval_ms: int) -> list:
    """Up to `count` raw klines opening from `start_ms`, paged since one request returns at most
    KLINES_PAGE_LIMIT. Stops early at the first short page, which means the latest candle."""
    candles: list = []
    while len(candles) < count:
        limit = min(count - len(candles), KLINES_PAGE_LIMIT)
        page = client.klines(symbol=symbol, interval=timeframe, startTime=start_ms, limit=limit)
        if isinstance(page, dict) and "data" in page:
            page = page["data"]
        if not page:
            break
        candles.extend(page)
        if len(page) < limit:
            break
        start_ms = int(page[-1][0]) + interval_ms
    return candles


def trade_values(rec, trade) -> dict:
    """Columns to store for a replayed trade; P/L only once it has an exit."""
    pnl = r_multiple = None
    if trade.exit_price is not None:
        # Planned risk from the stored levels, so later changes to SL_PERCENT keep old R intact
        risk_percent = abs(rec.entry_price - rec.stop_loss) / rec.entry_price * 100
        pnl, r_multiple = trade_return(
            rec.side, trade.fill_price, trade.exit_price, risk_percent, ROUND_TRIP_FEE_PERCENT
        )
    return {
        "status": trade.status,
        "fill_price": trade.fill_price,
        "fill_ms": trade.fill_ms,
        "exit_price": trade.exit_price,
        "closed_ms": trade.closed_ms,
        "pnl_percent": pnl,
        "r_multiple": r_multiple,
    }


def _empty_evaluation() -> dict:
    return {
        "evaluated": 0,
        "filled": 0,
        "tp": 0,
        "sl": 0,
        "expired": 0,
        "not_filled": 0,
        "failed": 0,
        "transitions": [],
    }


async def _evaluate_active_unlocked(client) -> dict:
    summary = _empty_evaluation()
    counters = {"TP_HIT": "tp", "SL_HIT": "sl", "EXPIRED": "expired", "NOT_FILLED": "not_filled"}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    async with _session_maker() as session:
        records = (
            await session.execute(select(SignalModel).where(SignalModel.status.in_(ACTIVE_STATUSES)))
        ).scalars().all()

    for rec in records:
        summary["evaluated"] += 1
        interval_ms = INTERVAL_MS.get(rec.timeframe)
        if interval_ms is None:
            logger.error("Signal %d has unsupported timeframe %r; not evaluated", rec.id, rec.timeframe)
            summary["failed"] += 1
            continue
        try:
            fill_window, max_hold = window_candles(interval_ms)
            # The trade starts on the candle after the signal candle
            candles = await asyncio.to_thread(
                fetch_trade_klines,
                client,
                rec.symbol,
                rec.timeframe,
                rec.signal_candle_ms + interval_ms,
                fill_window + max_hold,
                interval_ms,
            )
            trade = resolve(
                rec.side,
                rec.entry_price,
                rec.take_profit,
                rec.stop_loss,
                candles,
                fill_window,
                max_hold,
                interval_ms,
                now_ms,
            )
            if trade.status not in NEXT_STATUSES[rec.status]:
                continue
            values = trade_values(rec, trade)
            async with _session_maker() as session:
                won = (
                    await session.execute(build_transition_update(rec.id, rec.status, values))
                ).scalar_one_or_none()
                await session.commit()
        except Exception as ex:
            logger.warning("Evaluation error for signal %d %s: %s", rec.id, rec.symbol, ex)
            summary["failed"] += 1
            continue

        if won is None:
            continue  # another run already recorded this move
        previous = rec.status
        for key, value in values.items():
            setattr(rec, key, value)  # rec is detached, so this only feeds the alert
        if previous == "PENDING" and trade.fill_price is not None:
            summary["filled"] += 1
        if trade.status in counters:
            summary[counters[trade.status]] += 1
        summary["transitions"].append({**rec.to_dict(), "previous_status": previous})

    return summary


async def evaluate_active(client) -> dict:
    """Replay every pending and open trade and store the moves it made since the last run.

    Returns counts plus `transitions`: the rows this run moved, as stored, each with its
    `previous_status`. Raises when the database is unavailable.
    """
    if _session_maker is None:
        raise RuntimeError("Database not initialized")

    async with _evaluation_lock:
        return await _evaluate_active_unlocked(client)


# Trade Journal Operations
JOURNAL_STATUSES = ("TAKEN", "SKIPPED")


def journal_view(sig, entry) -> dict | None:
    """Journal entry for a signal, with the result of the user's own fills when both are known."""
    if entry is None:
        return None
    view = entry.to_dict()
    pnl, r_multiple = None, None
    if entry.status == "TAKEN" and entry.entry_fill and entry.exit_fill:
        risk_percent = abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100
        pnl, r_multiple = trade_return(
            sig.side, entry.entry_fill, entry.exit_fill, risk_percent, ROUND_TRIP_FEE_PERCENT
        )
    view["pnl_percent"] = round(pnl, 2) if pnl is not None else None
    view["r_multiple"] = round(r_multiple, 2) if r_multiple is not None else None
    return view


async def _journal_status_by_signal(session: AsyncSession, signal_ids: list[int]) -> dict[int, str]:
    if not signal_ids:
        return {}
    rows = await session.execute(
        select(JournalModel.signal_id, JournalModel.status).where(JournalModel.signal_id.in_(signal_ids))
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
                await session.execute(select(SignalModel).where(SignalModel.id == signal_id))
            ).scalar_one_or_none()
            if sig is None:
                return None
            await session.execute(
                pg_insert(JournalModel)
                .values(signal_id=signal_id, **values)
                .on_conflict_do_update(index_elements=["signal_id"], set_={**values, "updated_at": func.now()})
            )
            await session.commit()
            entry = (
                await session.execute(select(JournalModel).where(JournalModel.signal_id == signal_id))
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
            await session.execute(select(JournalModel).where(JournalModel.signal_id == signal_id))
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
    }


def journal_summary(rows) -> dict:
    """Taken vs skipped, your own R against the signal's R, over (signal, entry) pairs."""
    summary = _empty_journal_summary()
    for s, e in rows:
        taken = e.status == "TAKEN"
        summary["taken" if taken else "skipped"] += 1
        summary["signal_r_taken" if taken else "signal_r_skipped"] += s.r_multiple or 0.0
        view = journal_view(s, e)
        if taken and view["r_multiple"] is not None:
            summary["closed_taken"] += 1
            summary["your_total_r"] += view["r_multiple"]
            summary["your_total_pnl_percent"] += view["pnl_percent"]
    for key in ("your_total_r", "your_total_pnl_percent", "signal_r_taken", "signal_r_skipped"):
        summary[key] = round(summary[key], 2)
    return summary


async def get_journal(
    status: str | None = None,
    symbol: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Journal entries joined with their signals, newest signal first, and the matching total."""
    if _session_maker is None:
        return [], 0

    j, sig_m = JournalModel, SignalModel
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
            return [{**s.to_dict(), "journal": journal_view(s, e)} for s, e in rows], total
    except Exception as e:
        logger.exception("Failed to query trade journal: %s", e)
        return [], 0


async def get_journal_summary() -> dict:
    """Summary of every journal entry, unfiltered."""
    if _session_maker is None:
        return _empty_journal_summary()
    try:
        async with _session_maker() as session:
            rows = (await session.execute(select(SignalModel, JournalModel).join(JournalModel, JournalModel.signal_id == SignalModel.id))).all()
            return journal_summary(rows)
    except Exception as e:
        logger.exception("Failed to summarise trade journal: %s", e)
        return _empty_journal_summary()
