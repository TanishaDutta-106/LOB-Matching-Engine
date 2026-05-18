"""
Pydantic schemas for request validation and response serialization.

Keeping these separate from domain models (app/core/models.py) ensures
the API contract is independent of the internal representation.
"""

from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.models import OrderSide, OrderStatus, OrderType


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class SubmitOrderRequest(BaseModel):
    """
    POST /orders

    For LIMIT orders: price is required.
    For MARKET orders: price is ignored (set to 0 internally).
    """
    symbol: str = Field(..., min_length=1, max_length=20, examples=["AAPL", "BTC-USD"])
    side: OrderSide
    order_type: OrderType
    quantity: Decimal = Field(..., gt=0, description="Order quantity (must be positive)")
    price: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Limit price. Required for LIMIT orders, ignored for MARKET orders.",
    )
    client_order_id: str = Field("", max_length=64, description="Optional client-supplied ID")

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.upper().strip()

    @model_validator(mode="after")
    def validate_limit_price(self) -> "SubmitOrderRequest":
        if self.order_type == OrderType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("LIMIT orders require a positive price")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": "100",
                    "price": "182.50",
                    "client_order_id": "my-order-001",
                }
            ]
        }
    }


class CancelOrderRequest(BaseModel):
    """DELETE /orders/{order_id}"""
    symbol: str = Field(..., min_length=1, max_length=20)

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.upper().strip()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class TradeResponse(BaseModel):
    trade_id: str
    symbol: str
    price: str
    quantity: str
    notional_value: str
    buy_order_id: str
    sell_order_id: str
    aggressor_side: str
    timestamp: str


class OrderResponse(BaseModel):
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    price: str
    quantity: str
    remaining_quantity: str
    filled_quantity: str
    status: str
    timestamp: str
    trades: list[TradeResponse] = Field(default_factory=list)


class BookLevelResponse(BaseModel):
    """A single price level: [price, total_quantity]"""
    price: str
    quantity: str


class BookSnapshotResponse(BaseModel):
    symbol: str
    bids: list[list[str]]  # [[price, qty], ...]
    asks: list[list[str]]
    best_bid: Optional[str]
    best_ask: Optional[str]
    spread: Optional[str]


class TradeHistoryResponse(BaseModel):
    symbol: str
    trades: list[TradeResponse]
    count: int


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
