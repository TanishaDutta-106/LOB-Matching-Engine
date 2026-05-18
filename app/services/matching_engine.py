"""
Matching Engine Service

Sits between the API layer and the core OrderBook.
Responsibilities:
  - Manages multiple OrderBook instances (one per symbol)
  - Persists orders and trades to PostgreSQL via async DB calls
  - Broadcasts order book updates via WebSocket manager
  - Thread-safe order submission via asyncio locks (one lock per symbol)
"""

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from app.core.models import Order, OrderSide, OrderStatus, OrderType, Trade
from app.core.order_book import OrderBook

logger = logging.getLogger(__name__)


class MatchingEngine:
    """
    Central coordinator for the limit order book system.

    One MatchingEngine instance is shared across the FastAPI app (singleton).
    Each symbol gets its own OrderBook and asyncio.Lock to prevent races.
    """

    def __init__(self):
        # symbol -> OrderBook
        self._books: dict[str, OrderBook] = {}
        # symbol -> asyncio.Lock (serialize order submission per symbol)
        self._locks: dict[str, asyncio.Lock] = {}
        # Pluggable callbacks for persistence and WebSocket broadcasting
        self._on_order_callback = None
        self._on_trade_callback = None
        self._on_book_update_callback = None

    def register_callbacks(
        self,
        on_order=None,
        on_trade=None,
        on_book_update=None,
    ):
        """Register async callbacks for persistence and broadcasting."""
        self._on_order_callback = on_order
        self._on_trade_callback = on_trade
        self._on_book_update_callback = on_book_update

    def _get_book(self, symbol: str) -> OrderBook:
        """Get or create an OrderBook for the given symbol."""
        if symbol not in self._books:
            self._books[symbol] = OrderBook(symbol)
            self._locks[symbol] = asyncio.Lock()
            logger.info(f"Created new order book for symbol: {symbol}")
        return self._books[symbol]

    async def submit_order(self, order: Order) -> tuple[Order, list[Trade]]:
        """
        Submit an order to the matching engine.

        Steps:
          1. Acquire the per-symbol lock (ensures FIFO order of submission)
          2. Run matching logic (synchronous, fast)
          3. Persist order + trades to DB (async)
          4. Broadcast book snapshot over WebSocket (async)

        Returns the updated order and list of trades generated.
        """
        symbol = order.symbol

        # Ensure book + lock exist before acquiring
        self._get_book(symbol)

        async with self._locks[symbol]:
            book = self._books[symbol]
            trades = book.add_order(order)

            # Fire-and-forget persistence (don't block the matching loop)
            if self._on_order_callback:
                asyncio.create_task(self._on_order_callback(order))
            for trade in trades:
                if self._on_trade_callback:
                    asyncio.create_task(self._on_trade_callback(trade))

            # Broadcast updated book snapshot
            if self._on_book_update_callback:
                snapshot = book.get_snapshot()
                asyncio.create_task(self._on_book_update_callback(symbol, snapshot))

        return order, trades

    async def cancel_order(self, order_id: str, symbol: str) -> Optional[Order]:
        """
        Cancel a resting order.
        Returns the cancelled Order, or None if not found / already filled.
        """
        self._get_book(symbol)

        async with self._locks[symbol]:
            book = self._books[symbol]
            cancelled = book.cancel_order(order_id)

            if cancelled and self._on_order_callback:
                asyncio.create_task(self._on_order_callback(cancelled))

            if self._on_book_update_callback:
                snapshot = book.get_snapshot()
                asyncio.create_task(self._on_book_update_callback(symbol, snapshot))

        return cancelled

    def get_book_snapshot(self, symbol: str, depth: int = 10) -> dict:
        """Return a snapshot of the current order book (no lock needed for reads)."""
        book = self._get_book(symbol)
        return book.get_snapshot(depth)

    def get_order(self, symbol: str, order_id: str) -> Optional[Order]:
        """Look up a resting order by ID."""
        if symbol not in self._books:
            return None
        return self._books[symbol].get_order(order_id)

    def list_symbols(self) -> list[str]:
        return list(self._books.keys())


# ---------------------------------------------------------------------------
# Module-level singleton — imported by FastAPI app and route handlers
# ---------------------------------------------------------------------------
engine = MatchingEngine()
