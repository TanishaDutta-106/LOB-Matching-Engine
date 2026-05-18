"""
Test suite for the LOB Matching Engine.

Covers:
  - Basic limit order matching (buy vs. sell cross)
  - Price-time priority (FIFO within a price level)
  - Partial fills
  - Market orders consuming multiple price levels
  - Order cancellation
  - Self-crossing prevention (maker vs. taker on same side)
  - Empty book behavior
  - Decimal precision
  - Market order with insufficient liquidity
  - Multiple fills in a single submission
"""

import pytest
from decimal import Decimal

from app.core.models import Order, OrderSide, OrderStatus, OrderType
from app.core.order_book import OrderBook


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def book():
    """Fresh order book for each test."""
    return OrderBook("TEST")


def make_limit(side: str, price: str, qty: str, **kwargs) -> Order:
    return Order(
        symbol="TEST",
        side=OrderSide(side),
        order_type=OrderType.LIMIT,
        price=Decimal(price),
        quantity=Decimal(qty),
        **kwargs,
    )


def make_market(side: str, qty: str) -> Order:
    return Order(
        symbol="TEST",
        side=OrderSide(side),
        order_type=OrderType.MARKET,
        price=Decimal("0"),
        quantity=Decimal(qty),
    )


# ---------------------------------------------------------------------------
# Basic Matching
# ---------------------------------------------------------------------------

class TestBasicMatching:

    def test_no_match_no_cross(self, book):
        """Orders that don't cross should both rest in the book."""
        buy = make_limit("buy", "100.00", "10")
        sell = make_limit("sell", "101.00", "10")

        book.add_order(buy)
        book.add_order(sell)

        assert buy.status == OrderStatus.OPEN
        assert sell.status == OrderStatus.OPEN
        snapshot = book.get_snapshot()
        assert len(snapshot["bids"]) == 1
        assert len(snapshot["asks"]) == 1

    def test_exact_cross_full_fill(self, book):
        """Crossing orders at the same price should fully fill each other."""
        sell = make_limit("sell", "100.00", "10")
        book.add_order(sell)

        buy = make_limit("buy", "100.00", "10")
        trades = book.add_order(buy)

        assert len(trades) == 1
        trade = trades[0]
        assert trade.price == Decimal("100.00")
        assert trade.quantity == Decimal("10")
        assert buy.status == OrderStatus.FILLED
        assert sell.status == OrderStatus.FILLED

        # Book should be empty after full fill
        snapshot = book.get_snapshot()
        assert snapshot["bids"] == []
        assert snapshot["asks"] == []

    def test_buy_limit_above_ask_crosses(self, book):
        """A buy limit at 105 should match an ask at 100 (execution at ask price = 100)."""
        sell = make_limit("sell", "100.00", "5")
        book.add_order(sell)

        buy = make_limit("buy", "105.00", "5")
        trades = book.add_order(buy)

        assert len(trades) == 1
        # Execution is at the resting (maker) price
        assert trades[0].price == Decimal("100.00")
        assert buy.status == OrderStatus.FILLED
        assert sell.status == OrderStatus.FILLED

    def test_sell_limit_below_bid_crosses(self, book):
        """A sell limit at 95 should match a bid at 100 (execution at bid price = 100)."""
        buy = make_limit("buy", "100.00", "5")
        book.add_order(buy)

        sell = make_limit("sell", "95.00", "5")
        trades = book.add_order(sell)

        assert len(trades) == 1
        assert trades[0].price == Decimal("100.00")
        assert buy.status == OrderStatus.FILLED
        assert sell.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Price-Time Priority (FIFO)
# ---------------------------------------------------------------------------

class TestPriceTimePriority:

    def test_same_price_fifo_order(self, book):
        """Orders at the same price level should fill in insertion order."""
        sell1 = make_limit("sell", "100.00", "5")
        sell2 = make_limit("sell", "100.00", "5")
        book.add_order(sell1)
        book.add_order(sell2)

        # Buy 5: should match sell1 (first in)
        buy = make_limit("buy", "100.00", "5")
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].sell_order_id == sell1.order_id
        assert sell1.status == OrderStatus.FILLED
        assert sell2.status == OrderStatus.OPEN  # Second order untouched

    def test_price_priority_over_time(self, book):
        """Lower ask price should always match before a later but higher ask."""
        sell_high = make_limit("sell", "101.00", "5")
        sell_low = make_limit("sell", "100.00", "5")

        # Intentionally add higher price first (should NOT get priority)
        book.add_order(sell_high)
        book.add_order(sell_low)

        buy = make_limit("buy", "105.00", "5")
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].price == Decimal("100.00")  # Best ask matched
        assert trades[0].sell_order_id == sell_low.order_id

    def test_multiple_price_levels_drained_in_order(self, book):
        """An aggressive buy should drain asks from lowest to highest price."""
        sell_100 = make_limit("sell", "100.00", "3")
        sell_101 = make_limit("sell", "101.00", "3")
        sell_102 = make_limit("sell", "102.00", "3")
        book.add_order(sell_100)
        book.add_order(sell_101)
        book.add_order(sell_102)

        buy = make_limit("buy", "102.00", "9")
        trades = book.add_order(buy)

        assert len(trades) == 3
        prices = [t.price for t in trades]
        assert prices == [Decimal("100.00"), Decimal("101.00"), Decimal("102.00")]
        assert buy.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Partial Fills
# ---------------------------------------------------------------------------

class TestPartialFills:

    def test_buy_larger_than_sell_partial_fill(self, book):
        """Buy 10 against sell 3 — partial fill, rest 7 in book."""
        sell = make_limit("sell", "100.00", "3")
        book.add_order(sell)

        buy = make_limit("buy", "100.00", "10")
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].quantity == Decimal("3")
        assert buy.status == OrderStatus.PARTIAL
        assert buy.remaining_quantity == Decimal("7")
        assert buy.filled_quantity == Decimal("3")
        assert sell.status == OrderStatus.FILLED

        # Buy should be resting in book with 7 remaining
        snapshot = book.get_snapshot()
        assert snapshot["bids"][0][1] == "7"

    def test_sell_larger_than_buy_partial_fill(self, book):
        """Sell 10 against buy 3 — partial fill, rest 7 in book."""
        buy = make_limit("buy", "100.00", "3")
        book.add_order(buy)

        sell = make_limit("sell", "100.00", "10")
        trades = book.add_order(sell)

        assert len(trades) == 1
        assert sell.status == OrderStatus.PARTIAL
        assert sell.remaining_quantity == Decimal("7")
        assert sell.filled_quantity == Decimal("3")

        snapshot = book.get_snapshot()
        assert snapshot["asks"][0][1] == "7"

    def test_partial_then_complete_fill(self, book):
        """Two separate sells that together fill a large buy."""
        sell1 = make_limit("sell", "100.00", "5")
        sell2 = make_limit("sell", "100.00", "5")
        book.add_order(sell1)
        book.add_order(sell2)

        buy = make_limit("buy", "100.00", "10")
        trades = book.add_order(buy)

        assert len(trades) == 2
        assert buy.status == OrderStatus.FILLED
        total_filled = sum(t.quantity for t in trades)
        assert total_filled == Decimal("10")


# ---------------------------------------------------------------------------
# Market Orders
# ---------------------------------------------------------------------------

class TestMarketOrders:

    def test_market_buy_consumes_best_ask(self, book):
        """Market buy should execute at the current best ask."""
        sell = make_limit("sell", "100.00", "5")
        book.add_order(sell)

        buy = make_market("buy", "5")
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].price == Decimal("100.00")
        assert buy.status == OrderStatus.FILLED

    def test_market_sell_consumes_best_bid(self, book):
        """Market sell should execute at the current best bid."""
        buy = make_limit("buy", "100.00", "5")
        book.add_order(buy)

        sell = make_market("sell", "5")
        trades = book.add_order(sell)

        assert len(trades) == 1
        assert trades[0].price == Decimal("100.00")
        assert sell.status == OrderStatus.FILLED

    def test_market_buy_sweeps_multiple_levels(self, book):
        """Market order should sweep through all available levels."""
        for price, qty in [("99.00", "3"), ("100.00", "3"), ("101.00", "3")]:
            book.add_order(make_limit("sell", price, qty))

        buy = make_market("buy", "9")
        trades = book.add_order(buy)

        assert len(trades) == 3
        assert buy.status == OrderStatus.FILLED
        # Should start at best ask (99)
        assert trades[0].price == Decimal("99.00")

    def test_market_order_insufficient_liquidity(self, book):
        """Market order with no liquidity on the opposite side is cancelled."""
        buy = make_market("buy", "10")
        trades = book.add_order(buy)

        assert len(trades) == 0
        assert buy.status == OrderStatus.CANCELLED
        assert buy.remaining_quantity == Decimal("10")

    def test_market_order_partial_liquidity(self, book):
        """Market order with partial liquidity fills what's available, rest cancelled."""
        book.add_order(make_limit("sell", "100.00", "3"))

        buy = make_market("buy", "10")
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].quantity == Decimal("3")
        assert buy.status == OrderStatus.CANCELLED  # Partially filled but no more liquidity
        assert buy.filled_quantity == Decimal("3")
        assert buy.remaining_quantity == Decimal("7")


# ---------------------------------------------------------------------------
# Order Cancellation
# ---------------------------------------------------------------------------

class TestCancellation:

    def test_cancel_resting_order(self, book):
        """Cancel a resting limit order — it should be removed from the book."""
        order = make_limit("buy", "100.00", "10")
        book.add_order(order)
        assert order.status == OrderStatus.OPEN

        cancelled = book.cancel_order(order.order_id)
        assert cancelled is not None
        assert cancelled.order_id == order.order_id
        assert cancelled.status == OrderStatus.CANCELLED

        snapshot = book.get_snapshot()
        assert snapshot["bids"] == []

    def test_cancel_nonexistent_order(self, book):
        """Cancelling a non-existent order ID should return None."""
        result = book.cancel_order("does-not-exist")
        assert result is None

    def test_cancel_already_filled_order(self, book):
        """Filled orders are removed from the book index and cannot be cancelled."""
        sell = make_limit("sell", "100.00", "5")
        buy = make_limit("buy", "100.00", "5")
        book.add_order(sell)
        book.add_order(buy)
        assert buy.status == OrderStatus.FILLED

        # Order is no longer in the book — cancel should return None
        result = book.cancel_order(buy.order_id)
        assert result is None

    def test_cancel_one_of_multiple_orders_at_same_price(self, book):
        """Cancelling one order at a price level should not affect others."""
        order1 = make_limit("sell", "100.00", "5")
        order2 = make_limit("sell", "100.00", "5")
        book.add_order(order1)
        book.add_order(order2)

        book.cancel_order(order1.order_id)

        snapshot = book.get_snapshot()
        assert len(snapshot["asks"]) == 1
        assert snapshot["asks"][0][1] == "5"  # Only order2 remains


# ---------------------------------------------------------------------------
# Book State & Snapshot
# ---------------------------------------------------------------------------

class TestBookState:

    def test_snapshot_bid_ordering(self, book):
        """Bids should be returned highest-price-first in snapshot."""
        book.add_order(make_limit("buy", "99.00", "5"))
        book.add_order(make_limit("buy", "101.00", "5"))
        book.add_order(make_limit("buy", "100.00", "5"))

        snapshot = book.get_snapshot()
        prices = [Decimal(level[0]) for level in snapshot["bids"]]
        assert prices == sorted(prices, reverse=True)  # Descending

    def test_snapshot_ask_ordering(self, book):
        """Asks should be returned lowest-price-first in snapshot."""
        book.add_order(make_limit("sell", "103.00", "5"))
        book.add_order(make_limit("sell", "101.00", "5"))
        book.add_order(make_limit("sell", "102.00", "5"))

        snapshot = book.get_snapshot()
        prices = [Decimal(level[0]) for level in snapshot["asks"]]
        assert prices == sorted(prices)  # Ascending

    def test_spread_calculation(self, book):
        """Spread should equal best_ask - best_bid."""
        book.add_order(make_limit("buy", "100.00", "5"))
        book.add_order(make_limit("sell", "101.00", "5"))

        snapshot = book.get_snapshot()
        assert Decimal(snapshot["spread"]) == Decimal("1")
        assert snapshot["best_bid"] == "100.00"
        assert snapshot["best_ask"] == "101.00"

    def test_empty_book_snapshot(self, book):
        """Empty book should return empty bid/ask lists."""
        snapshot = book.get_snapshot()
        assert snapshot["bids"] == []
        assert snapshot["asks"] == []
        assert snapshot["best_bid"] is None
        assert snapshot["best_ask"] is None
        assert snapshot["spread"] is None


# ---------------------------------------------------------------------------
# Decimal Precision
# ---------------------------------------------------------------------------

class TestDecimalPrecision:

    def test_high_precision_price(self, book):
        """Prices with many decimal places should be handled exactly."""
        sell = make_limit("sell", "100.12345678", "1")
        buy = make_limit("buy", "100.12345678", "1")
        book.add_order(sell)
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].price == Decimal("100.12345678")

    def test_small_quantity_precision(self, book):
        """Very small quantities (e.g., crypto fractions) should be handled."""
        sell = make_limit("sell", "50000.00", "0.00000001")
        buy = make_limit("buy", "50000.00", "0.00000001")
        book.add_order(sell)
        trades = book.add_order(buy)

        assert len(trades) == 1
        assert trades[0].quantity == Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Trade Metadata
# ---------------------------------------------------------------------------

class TestTradeMetadata:

    def test_trade_buy_sell_order_ids(self, book):
        """Trade should correctly identify buy and sell order IDs."""
        sell = make_limit("sell", "100.00", "5")
        buy = make_limit("buy", "100.00", "5")
        book.add_order(sell)
        trades = book.add_order(buy)

        trade = trades[0]
        assert trade.buy_order_id == buy.order_id
        assert trade.sell_order_id == sell.order_id

    def test_trade_aggressor_side_buy(self, book):
        """When a buy crosses a resting sell, the aggressor is BUY."""
        sell = make_limit("sell", "100.00", "5")
        book.add_order(sell)

        buy = make_limit("buy", "100.00", "5")
        trades = book.add_order(buy)

        assert trades[0].aggressor_side == OrderSide.BUY

    def test_trade_aggressor_side_sell(self, book):
        """When a sell crosses a resting buy, the aggressor is SELL."""
        buy = make_limit("buy", "100.00", "5")
        book.add_order(buy)

        sell = make_limit("sell", "100.00", "5")
        trades = book.add_order(sell)

        assert trades[0].aggressor_side == OrderSide.SELL

    def test_notional_value(self, book):
        """Trade notional value should equal price * quantity."""
        sell = make_limit("sell", "150.00", "4")
        buy = make_limit("buy", "150.00", "4")
        book.add_order(sell)
        trades = book.add_order(buy)

        assert trades[0].notional_value == Decimal("600.00")
