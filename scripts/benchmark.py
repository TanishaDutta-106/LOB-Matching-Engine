"""
LOB Matching Engine Benchmark

Measures:
  - Order insertion throughput (no matches): orders/sec
  - Matching throughput (alternating buy/sell): orders/sec
  - Mixed workload (80% limit, 20% market): orders/sec
  - Memory usage under load

Run: python scripts/benchmark.py
"""

import random
import time
from decimal import Decimal
from statistics import mean, stdev

from app.core.models import Order, OrderSide, OrderStatus, OrderType
from app.core.order_book import OrderBook


def make_limit(side: str, price: str, qty: str) -> Order:
    return Order(
        symbol="BENCH",
        side=OrderSide(side),
        order_type=OrderType.LIMIT,
        price=Decimal(price),
        quantity=Decimal(qty),
    )


def make_market(side: str, qty: str) -> Order:
    return Order(
        symbol="BENCH",
        side=OrderSide(side),
        order_type=OrderType.MARKET,
        price=Decimal("0"),
        quantity=Decimal(qty),
    )


def bench_insertion(n: int = 100_000) -> float:
    """
    Benchmark pure insertion throughput (no matches).
    Adds n limit orders spread across price levels — no crosses.
    """
    book = OrderBook("BENCH")

    # Pre-build orders so object creation doesn't skew timing
    orders = []
    for i in range(n // 2):
        price = Decimal(str(90.0 + (i % 100) * 0.01))
        orders.append(make_limit("buy", str(price), "10"))
    for i in range(n // 2):
        price = Decimal(str(110.0 + (i % 100) * 0.01))
        orders.append(make_limit("sell", str(price), "10"))

    start = time.perf_counter()
    for order in orders:
        book.add_order(order)
    elapsed = time.perf_counter() - start

    return n / elapsed


def bench_matching(n: int = 100_000) -> float:
    """
    Benchmark matching throughput.
    Alternating buy/sell at the same price — every order triggers a fill.
    """
    book = OrderBook("BENCH")
    price = "100.00"

    # Pre-load one resting sell to start the chain
    book.add_order(make_limit("sell", price, "999999999"))

    orders = []
    for _ in range(n):
        orders.append(make_limit("buy", price, "1"))

    start = time.perf_counter()
    for order in orders:
        book.add_order(order)
        # Replenish sell side so we always have liquidity
        book.add_order(make_limit("sell", price, "1"))
    elapsed = time.perf_counter() - start

    return n / elapsed


def bench_mixed_workload(n: int = 100_000) -> float:
    """
    Realistic mixed workload:
      - 60% limit orders (random prices near mid)
      - 20% aggressive limit orders (cross the spread)
      - 20% market orders
    """
    book = OrderBook("BENCH")

    # Seed the book with some resting orders
    for i in range(100):
        book.add_order(make_limit("buy", str(99 - i * 0.01), "10"))
        book.add_order(make_limit("sell", str(101 + i * 0.01), "10"))

    orders = []
    rng = random.Random(42)

    for _ in range(n):
        roll = rng.random()
        side = "buy" if rng.random() < 0.5 else "sell"
        qty = str(rng.randint(1, 50))

        if roll < 0.60:
            # Passive limit (no cross)
            if side == "buy":
                price = str(round(rng.uniform(90, 99), 2))
            else:
                price = str(round(rng.uniform(101, 110), 2))
            orders.append(make_limit(side, price, qty))
        elif roll < 0.80:
            # Aggressive limit (crosses spread)
            if side == "buy":
                price = str(round(rng.uniform(101, 105), 2))
            else:
                price = str(round(rng.uniform(95, 99), 2))
            orders.append(make_limit(side, price, qty))
        else:
            # Market order
            orders.append(make_market(side, qty))

    start = time.perf_counter()
    for order in orders:
        book.add_order(order)
    elapsed = time.perf_counter() - start

    return n / elapsed


def bench_cancel_throughput(n: int = 50_000) -> float:
    """
    Benchmark cancel throughput. Inserts n orders then cancels them all.
    """
    book = OrderBook("BENCH")

    orders = []
    for i in range(n // 2):
        o = make_limit("buy", str(90 + (i % 50) * 0.01), "10")
        book.add_order(o)
        orders.append(o)
    for i in range(n // 2):
        o = make_limit("sell", str(110 + (i % 50) * 0.01), "10")
        book.add_order(o)
        orders.append(o)

    start = time.perf_counter()
    for order in orders:
        book.cancel_order(order.order_id)
    elapsed = time.perf_counter() - start

    return n / elapsed


def run_benchmark(name: str, fn, n: int, runs: int = 3) -> dict:
    results = []
    for _ in range(runs):
        results.append(fn(n))

    avg = mean(results)
    sd = stdev(results) if len(results) > 1 else 0

    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    print(f"  Orders:      {n:,}")
    print(f"  Runs:        {runs}")
    print(f"  Avg:         {avg:>12,.0f} orders/sec")
    print(f"  Std Dev:     {sd:>12,.0f} orders/sec")
    print(f"  Latency:     {1_000_000/avg:>10.2f} µs/order")

    return {"name": name, "n": n, "avg_ops_sec": avg, "std_dev": sd}


def main():
    print("\n" + "="*60)
    print("  LOB Matching Engine — Throughput Benchmark")
    print("  Python + sortedcontainers (SortedDict)")
    print("="*60)

    results = []
    results.append(run_benchmark("1. Pure Insertion (no matches)", bench_insertion, 100_000))
    results.append(run_benchmark("2. Pure Matching (every order fills)", bench_matching, 100_000))
    results.append(run_benchmark("3. Mixed Workload (60/20/20)", bench_mixed_workload, 100_000))
    results.append(run_benchmark("4. Cancel Throughput", bench_cancel_throughput, 50_000))

    print("\n" + "="*60)
    print("  Summary")
    print("="*60)
    for r in results:
        print(f"  {r['name'][:40]:<40} {r['avg_ops_sec']:>10,.0f} ops/sec")
    print()


if __name__ == "__main__":
    main()
