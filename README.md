# LOB Matching Engine

A production-quality **Limit Order Book (LOB)** matching engine built in Python.
Implements price-time priority (FIFO) matching with a REST API, WebSocket feed,
and PostgreSQL persistence — the architecture you'd find at the core of any
real exchange or algorithmic trading system.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Layer                             │
│   REST Clients (curl, httpx)      WebSocket Subscribers         │
│         │                                  │                    │
└─────────┼──────────────────────────────────┼────────────────────┘
          │ HTTP                             │ WS
          ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FastAPI Application                          │
│                                                                  │
│  ┌─────────────────────┐    ┌──────────────────────────────┐    │
│  │   REST Routes        │    │   WebSocket Manager          │    │
│  │  POST /orders        │    │  Subscriptions per symbol    │    │
│  │  DELETE /orders/:id  │    │  Broadcasts on book change   │    │
│  │  GET  /book/:symbol  │    └──────────────────────────────┘    │
│  │  GET  /trades/:sym   │              │                         │
│  └──────────┬───────────┘              │                         │
│             │                          │                         │
│             ▼                          │                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │               Matching Engine Service                     │   │
│  │  - Per-symbol asyncio.Lock (serializes submissions)       │   │
│  │  - Manages OrderBook instances (one per symbol)           │   │
│  │  - Fires async callbacks: persist order, persist trade,   │   │
│  │    broadcast book snapshot                                │   │
│  └──────────────────────┬───────────────────────────────────┘   │
│                          │                                       │
│             ┌────────────┴────────────┐                          │
│             ▼                         ▼                          │
│  ┌────────────────────┐   ┌─────────────────────────────────┐   │
│  │   Core OrderBook   │   │        DB Repository            │   │
│  │                    │   │  upsert_order() (async)          │   │
│  │  Bids: SortedDict  │   │  insert_trade() (async)         │   │
│  │  (negated key →    │   │  get_recent_trades()            │   │
│  │   highest bid 1st) │   └──────────────┬──────────────────┘   │
│  │                    │                  │                       │
│  │  Asks: SortedDict  │                  ▼                       │
│  │  (ascending price) │   ┌─────────────────────────────────┐   │
│  │                    │   │     PostgreSQL (asyncpg)         │   │
│  │  Price Level:      │   │  orders table (lifecycle)        │   │
│  │  deque[Order]      │   │  trades table (immutable)        │   │
│  │  (FIFO matching)   │   └─────────────────────────────────┘   │
│  └────────────────────┘                                          │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

**Price-time priority (FIFO):**
Within a price level, orders are stored in a `deque` and consumed oldest-first.
This is standard exchange behavior (NASDAQ, CME, NYSE).

**SortedDict for O(log n) operations:**
- Asks: ascending key = lowest ask is always `peekitem(0)`
- Bids: negated price key = highest bid is always `peekitem(0)`
- `sortedcontainers.SortedDict` uses a list-of-lists structure, giving O(log n) insert/delete and O(1) min/max.

**Per-symbol asyncio.Lock:**
Prevents race conditions when multiple concurrent HTTP requests target the same symbol.
Matching itself is synchronous (pure Python, no I/O) so the lock hold time is minimal.

**Fire-and-forget DB writes:**
Persistence (`upsert_order`, `insert_trade`) runs as `asyncio.create_task()` calls —
they don't block the matching hot path. Trade confirmations reach the client immediately.

---

## Project Structure

```
lob-engine/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, callback wiring
│   ├── core/
│   │   ├── models.py            # Domain models: Order, Trade, enums
│   │   └── order_book.py        # OrderBook + PriceLevel — pure matching logic
│   ├── services/
│   │   ├── matching_engine.py   # Orchestrator: locks, callbacks, symbol mgmt
│   │   └── websocket_manager.py # WebSocket broadcast manager
│   ├── api/
│   │   ├── routes.py            # REST endpoints
│   │   └── websocket.py         # WebSocket endpoint
│   ├── db/
│   │   ├── database.py          # Async SQLAlchemy engine + repository fns
│   │   └── models.py            # ORM models (OrderRecord, TradeRecord)
│   └── schemas/
│       └── orders.py            # Pydantic request/response schemas
├── tests/
│   └── test_matching_engine.py  # 29 test cases covering all edge cases
├── scripts/
│   └── benchmark.py             # Throughput benchmark
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml
└── .env.example
```

---

## Quickstart

### Prerequisites

- Docker + Docker Compose
- Python 3.12+ (for local dev without Docker)

### 1. Clone and configure

```bash
git clone https://github.com/TanishaDutta-106/LOB-Matching-Engine.git
cd lob-engine
cp .env.example .env
# Edit .env if you want custom credentials
```

### 2. Run with Docker Compose

```bash
docker compose up --build
```

This starts:
- **PostgreSQL** on port 5432 (creates tables automatically on first run)
- **Redis** on port 6379
- **LOB API** on port 8000

API docs available at: http://localhost:8000/docs

### 3. Local development (without Docker)

```bash
# Install dependencies
pip install -r requirements.txt

# Set up PostgreSQL locally (or use Docker just for PG)
docker compose up postgres -d

# Set env var
export DATABASE_URL=postgresql://lob:lob@localhost:5432/lobdb

# Run the app
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---
## Live Dashboard

A branded trading dashboard is included at `lob-dashboard.html`.

Open it directly in Chrome while Docker is running — no install needed.

**Features:**
- Live order book with depth visualization (WebSocket)
- Real-time trade feed with notional volume tracking
- Order submission form (limit + market, buy + sell)
- Order history table with status badges
- Connects automatically to `localhost:8000`

## API Reference

### Submit an Order

```bash
# Limit buy order
curl -X POST http://localhost:8000/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "AAPL",
    "side": "buy",
    "order_type": "limit",
    "quantity": "100",
    "price": "182.50"
  }'

# Market sell order
curl -X POST http://localhost:8000/api/v1/orders \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "AAPL",
    "side": "sell",
    "order_type": "market",
    "quantity": "50"
  }'
```

### Cancel an Order

```bash
curl -X DELETE "http://localhost:8000/api/v1/orders/{order_id}?symbol=AAPL"
```

### Query the Book

```bash
# Order book snapshot (top 10 levels)
curl "http://localhost:8000/api/v1/book/AAPL?depth=10"

# Recent trades
curl "http://localhost:8000/api/v1/trades/AAPL?limit=20"
```

### WebSocket Feed

```javascript
// Connect to live book updates
const ws = new WebSocket("ws://localhost:8000/ws/AAPL");

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  // msg.type: "book_snapshot" | "book_update" | "trade"
  console.log(msg);
};

// Send ping
ws.send(JSON.stringify({ type: "ping" }));
```

**Message types:**

| Type | When | Payload |
|------|------|---------|
| `book_snapshot` | On connect | Full book snapshot |
| `book_update` | After any order event | Updated book snapshot |
| `trade` | After each execution | Trade details |

---

## Running Tests

```bash
# All 29 test cases
pytest tests/ -v

# With coverage
pip install pytest-cov
pytest tests/ --cov=app --cov-report=term-missing
```

**Test coverage areas:**

| Category | Tests |
|----------|-------|
| Basic matching (cross/no-cross) | 4 |
| Price-time priority (FIFO) | 3 |
| Partial fills | 3 |
| Market orders | 5 |
| Order cancellation | 4 |
| Book state & snapshots | 4 |
| Decimal precision | 2 |
| Trade metadata | 4 |

---

## Benchmark Results

Measured on a single-threaded Python 3.12 process (no async overhead —
benchmark runs the core `OrderBook` synchronously).

```
============================================================
  LOB Matching Engine — Throughput Benchmark
  Python + sortedcontainers (SortedDict)
============================================================

  1. Pure Insertion (no matches)
     Orders:      100,000
     Avg:         448,314 orders/sec
     Latency:         2.23 µs/order

  2. Pure Matching (every order fills)
     Orders:      100,000
     Avg:          56,458 orders/sec
     Latency:        17.71 µs/order

  3. Mixed Workload (60% passive / 20% aggressive / 20% market)
     Orders:      100,000
     Avg:         115,570 orders/sec
     Latency:         8.65 µs/order

  4. Cancel Throughput
     Orders:       50,000
     Avg:          45,148 orders/sec
     Latency:        22.15 µs/order
```

**Reproduce:**
```bash
PYTHONPATH=. python scripts/benchmark.py
```

**Performance notes:**
- Insertion is fast because SortedDict insert is O(log n)
- Matching latency includes both price level lookup (O(log n)) + FIFO dequeue (O(1))
- Cancel is O(n) within a price level in the worst case (linear scan of the level's deque) — acceptable for typical level depths; can be improved with an order-indexed deque if needed
- In production Python, you'd gain 5–10× by moving the hot path to Rust/C++ (common in high-frequency setups); this codebase is intentionally readable and correct-first

---

## Order Lifecycle

```
PENDING → (matching) → OPEN (resting) → PARTIAL (partially filled) → FILLED
                                      → CANCELLED (by user or market order no-fill)
                    → FILLED (immediately, if fully matched on entry)
```

---

## Environment Variables

See `.env.example` for all options.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://lob:lob@localhost:5432/lobdb` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection (optional) |
| `API_PORT` | `8000` | Port the API listens on |
| `LOG_LEVEL` | `info` | Logging verbosity |
| `DB_ECHO` | `false` | Log all SQL queries (debug) |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Web framework | FastAPI 0.115 |
| Async runtime | uvicorn + asyncio |
| Order book data structure | `sortedcontainers.SortedDict` |
| Database | PostgreSQL 16 + asyncpg |
| ORM | SQLAlchemy 2.0 (async) |
| Pub/sub | Redis 7 (optional) |
| Containerization | Docker + Docker Compose |
| Testing | pytest |
| Python | 3.12 |

---

## Further Reading

- *Designing Data-Intensive Applications* — Kleppmann (systems design foundations)
- [How a Limit Order Book Works](https://www.investopedia.com/terms/l/limitorderbook.asp)
- [CME Group: How Markets Work](https://www.cmegroup.com/education/courses/introduction-to-futures/how-does-trading-work.html)
- [`sortedcontainers` performance analysis](http://www.grantjenks.com/docs/sortedcontainers/performance.html)
