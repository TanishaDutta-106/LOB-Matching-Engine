"""
REST API Routes

POST   /orders              - Submit a new order (limit, market)
DELETE /orders/{order_id}   - Cancel a resting order
GET    /orders/{order_id}   - Get order status
GET    /book/{symbol}       - Current order book snapshot
GET    /trades/{symbol}     - Recent trade history
GET    /symbols             - List active symbols
"""

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Order, OrderSide, OrderStatus, OrderType
from app.db.database import (
    get_order_by_id,
    get_recent_trades,
    get_session,
    get_open_orders,
)
from app.schemas.orders import (
    BookSnapshotResponse,
    CancelOrderRequest,
    ErrorResponse,
    OrderResponse,
    SubmitOrderRequest,
    TradeHistoryResponse,
    TradeResponse,
)
from app.services.matching_engine import engine as matching_engine

logger = logging.getLogger(__name__)
router = APIRouter()


def _order_to_response(order: Order, trades=None) -> OrderResponse:
    trade_resps = []
    for t in (trades or []):
        trade_resps.append(TradeResponse(
            trade_id=t.trade_id,
            symbol=t.symbol,
            price=str(t.price),
            quantity=str(t.quantity),
            notional_value=str(t.price * t.quantity),
            buy_order_id=t.buy_order_id,
            sell_order_id=t.sell_order_id,
            aggressor_side=t.aggressor_side.value,
            timestamp=t.timestamp.isoformat(),
        ))
    return OrderResponse(
        order_id=order.order_id,
        client_order_id=order.client_order_id or "",
        symbol=order.symbol,
        side=order.side.value,
        order_type=order.order_type.value,
        price=str(order.price),
        quantity=str(order.quantity),
        remaining_quantity=str(order.remaining_quantity),
        filled_quantity=str(order.filled_quantity),
        status=order.status.value,
        timestamp=order.timestamp.isoformat(),
        trades=trade_resps,
    )


@router.post(
    "/orders",
    response_model=OrderResponse,
    summary="Submit a new order",
    description=(
        "Submit a limit or market order. "
        "Limit orders rest in the book if not immediately matched. "
        "Market orders consume available liquidity at best prices."
    ),
)
async def submit_order(request: SubmitOrderRequest):
    price = request.price if request.price is not None else Decimal("0")

    order = Order(
        symbol=request.symbol,
        side=request.side,
        order_type=request.order_type,
        quantity=request.quantity,
        price=price,
        client_order_id=request.client_order_id,
    )

    try:
        updated_order, trades = await matching_engine.submit_order(order)
    except Exception as e:
        logger.exception(f"Error submitting order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return _order_to_response(updated_order, trades)


@router.delete(
    "/orders/{order_id}",
    response_model=OrderResponse,
    summary="Cancel a resting order",
    responses={404: {"model": ErrorResponse}},
)
async def cancel_order(order_id: str, symbol: str = Query(..., description="Symbol the order belongs to")):
    cancelled = await matching_engine.cancel_order(order_id, symbol.upper())
    if cancelled is None:
        raise HTTPException(
            status_code=404,
            detail=f"Order {order_id} not found or not cancellable",
        )
    return _order_to_response(cancelled)


@router.get(
    "/orders/{order_id}",
    response_model=OrderResponse,
    summary="Get order status",
    responses={404: {"model": ErrorResponse}},
)
async def get_order(
    order_id: str,
    symbol: str = Query(..., description="Symbol the order belongs to"),
):
    # Check live book first (fastest)
    order = matching_engine.get_order(symbol.upper(), order_id)
    if order:
        return _order_to_response(order)

    # Fall back to DB (for filled/cancelled orders)
    record = await get_order_by_id(order_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")

    # Reconstruct a response from DB record
    return OrderResponse(
        order_id=record.order_id,
        client_order_id=record.client_order_id or "",
        symbol=record.symbol,
        side=record.side.value if hasattr(record.side, "value") else record.side,
        order_type=record.order_type.value if hasattr(record.order_type, "value") else record.order_type,
        price=str(record.price),
        quantity=str(record.quantity),
        remaining_quantity=str(record.remaining_quantity),
        filled_quantity=str(record.filled_quantity),
        status=record.status.value if hasattr(record.status, "value") else record.status,
        timestamp=record.created_at.isoformat(),
        trades=[],
    )


@router.get(
    "/book/{symbol}",
    response_model=BookSnapshotResponse,
    summary="Get order book snapshot",
    description="Returns the current bid/ask levels up to the requested depth.",
)
async def get_book(
    symbol: str,
    depth: int = Query(10, ge=1, le=50, description="Number of price levels to return"),
):
    snapshot = matching_engine.get_book_snapshot(symbol.upper(), depth)
    return BookSnapshotResponse(**snapshot)


@router.get(
    "/trades/{symbol}",
    response_model=TradeHistoryResponse,
    summary="Get recent trade history",
)
async def get_trades(
    symbol: str,
    limit: int = Query(50, ge=1, le=500),
):
    records = await get_recent_trades(symbol.upper(), limit)
    trades = [
        TradeResponse(
            trade_id=r.trade_id,
            symbol=r.symbol,
            price=str(r.price),
            quantity=str(r.quantity),
            notional_value=str(r.notional_value),
            buy_order_id=r.buy_order_id,
            sell_order_id=r.sell_order_id,
            aggressor_side=r.aggressor_side.value if hasattr(r.aggressor_side, "value") else r.aggressor_side,
            timestamp=r.executed_at.isoformat(),
        )
        for r in records
    ]
    return TradeHistoryResponse(symbol=symbol.upper(), trades=trades, count=len(trades))


@router.get("/symbols", summary="List active symbols in the matching engine")
async def list_symbols():
    return {"symbols": matching_engine.list_symbols()}


@router.get("/health", summary="Health check")
async def health():
    return {"status": "ok", "engine": "lob-matching-engine"}
