"""
Limit Order Book (LOB) - Core Matching Engine

Architecture:
  - Bids: Max-heap via SortedDict (descending price)
  - Asks: Min-heap via SortedDict (ascending price)
  - Each price level holds a deque of orders (FIFO / price-time priority)
  - Matching runs in O(log n) per price level lookup, O(1) FIFO dequeue

Price-time priority (FIFO within a level):
  Orders at the same price are matched oldest-first. This is the standard
  behavior on most exchanges (NASDAQ, CME, etc.).
"""

import uuid
from collections import deque
from decimal import Decimal
from typing import Optional
from sortedcontainers import SortedDict

from app.core.models import Order, OrderSide, OrderStatus, OrderType, Trade


class PriceLevel:
    """
    A single price level in the order book.
    Holds a deque of orders at this price, maintaining insertion order (FIFO).
    """

    def __init__(self, price: Decimal):
        self.price = price
        self.orders: deque[Order] = deque()
        self.total_quantity: Decimal = Decimal("0")

    def add_order(self, order: Order) -> None:
        self.orders.append(order)
        self.total_quantity += order.remaining_quantity

    def remove_order(self, order_id: str) -> Optional[Order]:
        """Remove a specific order by ID (used for cancellations)."""
        for i, order in enumerate(self.orders):
            if order.order_id == order_id:
                removed = self.orders[i]
                # Rebuild deque without this order
                self.orders = deque(o for o in self.orders if o.order_id != order_id)
                self.total_quantity -= removed.remaining_quantity
                return removed
        return None

    def is_empty(self) -> bool:
        return len(self.orders) == 0

    def reduce_total(self, qty: Decimal) -> None:
        self.total_quantity -= qty

    def peek(self) -> Optional[Order]:
        return self.orders[0] if self.orders else None

    def pop_front(self) -> Optional[Order]:
        if self.orders:
            order = self.orders.popleft()
            self.total_quantity -= order.remaining_quantity
            return order
        return None


class OrderBook:
    """
    The full Limit Order Book for a single trading instrument.

    Bids (buy orders): stored in descending price order (highest bid first)
    Asks (sell orders): stored in ascending price order (lowest ask first)

    SortedDict is used for O(log n) inserts and O(1) best-price lookups.
    We negate bid prices so SortedDict's ascending sort gives us descending order.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

        # Asks: SortedDict keyed by price ascending (lowest ask = best ask)
        self._asks: SortedDict[Decimal, PriceLevel] = SortedDict()

        # Bids: SortedDict keyed by NEGATED price (so lowest key = highest bid)
        self._bids: SortedDict[Decimal, PriceLevel] = SortedDict()

        # Fast order lookup: order_id -> Order (for O(1) cancel)
        self._order_index: dict[str, Order] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_order(self, order: Order) -> list[Trade]:
        """
        Submit an order to the book.
        Returns a list of Trade executions generated (may be empty).
        """
        if order.order_type == OrderType.MARKET:
            return self._match_market(order)
        elif order.order_type == OrderType.LIMIT:
            trades = self._match_limit(order)
            # If order wasn't fully filled, rest it in the book
            if order.remaining_quantity > 0:
                self._rest_order(order)
            return trades
        return []

    def cancel_order(self, order_id: str) -> Optional[Order]:
        """
        Cancel a resting order. Returns the cancelled Order or None if not found.
        """
        order = self._order_index.get(order_id)
        if not order or order.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
            return None

        price_key = self._price_key(order.side, order.price)
        levels = self._bids if order.side == OrderSide.BUY else self._asks

        level = levels.get(price_key)
        if level:
            level.remove_order(order_id)
            if level.is_empty():
                del levels[price_key]

        order.status = OrderStatus.CANCELLED
        del self._order_index[order_id]
        return order

    def get_snapshot(self, depth: int = 10) -> dict:
        """
        Return a snapshot of the order book up to `depth` levels on each side.
        Format: { bids: [[price, qty], ...], asks: [[price, qty], ...] }
        """
        bids = []
        for neg_price in list(self._bids.keys())[:depth]:
            level = self._bids[neg_price]
            bids.append([str(level.price), str(level.total_quantity)])

        asks = []
        for price in list(self._asks.keys())[:depth]:
            level = self._asks[price]
            asks.append([str(level.price), str(level.total_quantity)])

        return {
            "symbol": self.symbol,
            "bids": bids,
            "asks": asks,
            "best_bid": str(bids[0][0]) if bids else None,
            "best_ask": str(asks[0][0]) if asks else None,
            "spread": str(
                Decimal(asks[0][0]) - Decimal(bids[0][0])
            ) if bids and asks else None,
        }

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._order_index.get(order_id)

    # ------------------------------------------------------------------
    # Internal matching logic
    # ------------------------------------------------------------------

    def _match_limit(self, order: Order) -> list[Trade]:
        """
        Match a limit order against the opposite side.
        A buy limit matches against asks at or below the limit price.
        A sell limit matches against bids at or above the limit price.
        """
        trades = []

        if order.side == OrderSide.BUY:
            # Match against asks (ascending) while ask_price <= limit_price
            while order.remaining_quantity > 0 and self._asks:
                best_ask_price = self._asks.peekitem(0)[0]  # lowest ask
                if best_ask_price > order.price:
                    break  # No cross — rest in book
                trades += self._fill_against_level(order, self._asks, best_ask_price)

        else:  # SELL
            # Match against bids (descending, stored as negated keys) while bid_price >= limit_price
            while order.remaining_quantity > 0 and self._bids:
                best_bid_neg_price = self._bids.peekitem(0)[0]  # most negative = highest bid
                best_bid_price = -best_bid_neg_price
                if best_bid_price < order.price:
                    break  # No cross
                trades += self._fill_against_level(order, self._bids, best_bid_neg_price)

        if order.remaining_quantity == 0:
            order.status = OrderStatus.FILLED
        # Note: if partially filled and resting, _rest_order sets it to OPEN;
        # we update to PARTIAL here so the caller sees accurate state immediately.
        elif trades:
            order.status = OrderStatus.PARTIAL

        return trades

    def _match_market(self, order: Order) -> list[Trade]:
        """
        Match a market order against the best available prices.
        Market orders have no price constraint — they consume liquidity until filled or book is empty.
        """
        trades = []

        if order.side == OrderSide.BUY:
            while order.remaining_quantity > 0 and self._asks:
                best_price_key = self._asks.peekitem(0)[0]
                trades += self._fill_against_level(order, self._asks, best_price_key)

        else:  # SELL
            while order.remaining_quantity > 0 and self._bids:
                best_price_key = self._bids.peekitem(0)[0]
                trades += self._fill_against_level(order, self._bids, best_price_key)

        if order.remaining_quantity == 0:
            order.status = OrderStatus.FILLED
        else:
            # Partial fill on market order — mark remaining as cancelled (no liquidity)
            order.status = OrderStatus.CANCELLED

        return trades

    def _fill_against_level(
        self,
        aggressor: Order,
        levels: SortedDict,
        price_key: Decimal,
    ) -> list[Trade]:
        """
        Fill the aggressor order against orders at a given price level (FIFO).
        Removes fully consumed orders; updates partial quantities.
        Cleans up empty price levels.
        """
        trades = []
        level: PriceLevel = levels[price_key]

        while level.orders and aggressor.remaining_quantity > 0:
            resting = level.peek()

            fill_qty = min(aggressor.remaining_quantity, resting.remaining_quantity)
            fill_price = resting.price  # Resting order's price is the execution price

            # Update quantities
            aggressor.remaining_quantity -= fill_qty
            aggressor.filled_quantity += fill_qty

            resting.remaining_quantity -= fill_qty
            resting.filled_quantity += fill_qty
            level.reduce_total(fill_qty)

            # Determine trade sides properly
            if aggressor.side == OrderSide.BUY:
                buy_order_id, sell_order_id = aggressor.order_id, resting.order_id
            else:
                buy_order_id, sell_order_id = resting.order_id, aggressor.order_id

            trade = Trade(
                trade_id=str(uuid.uuid4()),
                symbol=self.symbol,
                price=fill_price,
                quantity=fill_qty,
                buy_order_id=buy_order_id,
                sell_order_id=sell_order_id,
                aggressor_side=aggressor.side,
            )
            trades.append(trade)

            # Remove resting order if fully filled
            if resting.remaining_quantity == 0:
                resting.status = OrderStatus.FILLED
                level.orders.popleft()
                self._order_index.pop(resting.order_id, None)
            else:
                resting.status = OrderStatus.PARTIAL

        # Clean up empty price levels
        if level.is_empty():
            del levels[price_key]

        return trades

    def _rest_order(self, order: Order) -> None:
        """Place an unfilled (or partially filled) limit order into the book."""
        levels = self._bids if order.side == OrderSide.BUY else self._asks
        price_key = self._price_key(order.side, order.price)

        if price_key not in levels:
            levels[price_key] = PriceLevel(order.price)

        levels[price_key].add_order(order)
        self._order_index[order.order_id] = order
        # Preserve PARTIAL if already set; only mark OPEN for fresh (never-filled) orders
        if order.status != OrderStatus.PARTIAL:
            order.status = OrderStatus.OPEN

    @staticmethod
    def _price_key(side: OrderSide, price: Decimal) -> Decimal:
        """
        Bids are stored with negated price keys so that SortedDict's
        ascending iteration gives us descending price order (highest bid first).
        """
        return -price if side == OrderSide.BUY else price
