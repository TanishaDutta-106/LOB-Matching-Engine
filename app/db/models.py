"""
SQLAlchemy ORM models for PostgreSQL persistence.

Two tables:
  - orders: Full order lifecycle record
  - trades: Execution records (immutable once created)

We store prices/quantities as NUMERIC(20, 8) to avoid floating-point errors.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Column, DateTime, Enum as SAEnum, Index, Numeric, String, text
)
from sqlalchemy.orm import DeclarativeBase

from app.core.models import OrderSide, OrderStatus, OrderType


class Base(DeclarativeBase):
    pass


class OrderRecord(Base):
    """Persistent record of an order throughout its lifecycle."""
    __tablename__ = "orders"

    order_id = Column(String(36), primary_key=True)
    client_order_id = Column(String(64), nullable=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(SAEnum(OrderSide, values_callable=lambda e: [x.value for x in e]), nullable=False)
    order_type = Column(SAEnum(OrderType, values_callable=lambda e: [x.value for x in e]), nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)
    remaining_quantity = Column(Numeric(20, 8), nullable=False)
    filled_quantity = Column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    status = Column(
        SAEnum(OrderStatus, values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_orders_symbol_status", "symbol", "status"),
        Index("ix_orders_symbol_side", "symbol", "side"),
    )

    def __repr__(self):
        return (
            f"<Order {self.order_id[:8]} {self.symbol} "
            f"{self.side} {self.order_type} {self.price}x{self.quantity} [{self.status}]>"
        )


class TradeRecord(Base):
    """Immutable execution record. Trades are never updated after creation."""
    __tablename__ = "trades"

    trade_id = Column(String(36), primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(20, 8), nullable=False)
    notional_value = Column(Numeric(28, 8), nullable=False)  # price * quantity
    buy_order_id = Column(String(36), nullable=False, index=True)
    sell_order_id = Column(String(36), nullable=False, index=True)
    aggressor_side = Column(
        SAEnum(OrderSide, values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    executed_at = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        Index("ix_trades_symbol_time", "symbol", "executed_at"),
    )

    def __repr__(self):
        return (
            f"<Trade {self.trade_id[:8]} {self.symbol} "
            f"{self.price}x{self.quantity}>"
        )
