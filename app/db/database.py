"""
Database layer: async SQLAlchemy session factory + repository functions.

We use asyncpg as the async PostgreSQL driver under SQLAlchemy's async engine.
All DB calls are fire-and-forget from the matching engine perspective —
they run as asyncio tasks and don't block the hot path.
"""

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncGenerator, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.models import Order, OrderStatus, Trade
from app.db.models import Base, OrderRecord, TradeRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------

_engine = None
_session_factory = None


def get_database_url() -> str:
    """
    Build the async-compatible PostgreSQL URL.
    Converts postgresql:// -> postgresql+asyncpg://
    """
    url = os.getenv("DATABASE_URL", "postgresql://lob:lob@localhost:5432/lobdb")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


async def init_db() -> None:
    """Create tables and initialize the session factory. Called at app startup."""
    global _engine, _session_factory

    db_url = get_database_url()
    logger.info(f"Connecting to database: {db_url.split('@')[-1]}")  # hide credentials

    _engine = create_async_engine(
        db_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,  # Validate connections before use
        echo=os.getenv("DB_ECHO", "false").lower() == "true",
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Create tables if they don't exist
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialized successfully")


async def close_db() -> None:
    """Dispose the engine. Called at app shutdown."""
    global _engine
    if _engine:
        await _engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a DB session and commits/rolls back."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Repository functions
# ---------------------------------------------------------------------------

async def upsert_order(order: Order) -> None:
    """
    Insert or update an order record.
    Called every time an order's status/quantities change.
    """
    if _session_factory is None:
        return  # DB not available (e.g., tests without DB)

    async with _session_factory() as session:
        try:
            record = await session.get(OrderRecord, order.order_id)

            if record is None:
                # New order — insert
                record = OrderRecord(
                    order_id=order.order_id,
                    client_order_id=order.client_order_id or None,
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    price=order.price,
                    quantity=order.quantity,
                    remaining_quantity=order.remaining_quantity,
                    filled_quantity=order.filled_quantity,
                    status=order.status,
                    created_at=order.timestamp,
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(record)
            else:
                # Existing order — update mutable fields
                record.remaining_quantity = order.remaining_quantity
                record.filled_quantity = order.filled_quantity
                record.status = order.status
                record.updated_at = datetime.now(timezone.utc)

            await session.commit()
        except Exception as e:
            logger.error(f"Failed to upsert order {order.order_id}: {e}")
            await session.rollback()


async def insert_trade(trade: Trade) -> None:
    """Insert a new trade execution record (immutable after creation)."""
    if _session_factory is None:
        return

    async with _session_factory() as session:
        try:
            record = TradeRecord(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                price=trade.price,
                quantity=trade.quantity,
                notional_value=trade.price * trade.quantity,
                buy_order_id=trade.buy_order_id,
                sell_order_id=trade.sell_order_id,
                aggressor_side=trade.aggressor_side,
                executed_at=trade.timestamp,
            )
            session.add(record)
            await session.commit()
        except Exception as e:
            logger.error(f"Failed to insert trade {trade.trade_id}: {e}")
            await session.rollback()


async def get_order_by_id(order_id: str) -> Optional[OrderRecord]:
    """Fetch an order record from DB by ID."""
    if _session_factory is None:
        return None

    async with _session_factory() as session:
        return await session.get(OrderRecord, order_id)


async def get_recent_trades(
    symbol: str,
    limit: int = 50,
) -> list[TradeRecord]:
    """Fetch the most recent trades for a symbol, ordered newest-first."""
    if _session_factory is None:
        return []

    async with _session_factory() as session:
        result = await session.execute(
            select(TradeRecord)
            .where(TradeRecord.symbol == symbol)
            .order_by(TradeRecord.executed_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_open_orders(symbol: str) -> list[OrderRecord]:
    """Fetch all open/partial orders for a symbol."""
    if _session_factory is None:
        return []

    async with _session_factory() as session:
        result = await session.execute(
            select(OrderRecord)
            .where(
                OrderRecord.symbol == symbol,
                OrderRecord.status.in_([OrderStatus.OPEN.value, OrderStatus.PARTIAL.value]),
            )
            .order_by(OrderRecord.created_at.asc())
        )
        return list(result.scalars().all())
