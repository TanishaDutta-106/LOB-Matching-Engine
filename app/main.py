"""
LOB Matching Engine — FastAPI Application

Startup sequence:
  1. Initialize PostgreSQL connection pool
  2. Register matching engine callbacks (DB persistence + WebSocket broadcast)
  3. Mount REST routes + WebSocket endpoint
  4. Begin serving requests

Shutdown:
  - Dispose DB connection pool cleanly
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.websocket import ws_router
from app.core.models import Order, Trade
from app.db.database import close_db, init_db, insert_trade, upsert_order
from app.services.matching_engine import engine as matching_engine
from app.services.websocket_manager import ws_manager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callbacks wiring: matching engine -> DB + WebSocket
# ---------------------------------------------------------------------------

async def _on_order(order: Order) -> None:
    """Persist order state changes to PostgreSQL."""
    await upsert_order(order)


async def _on_trade(trade: Trade) -> None:
    """Persist trade records and broadcast to WebSocket subscribers."""
    await insert_trade(trade)
    await ws_manager.send_trade(trade.symbol, trade.to_dict())


async def _on_book_update(symbol: str, snapshot: dict) -> None:
    """Broadcast the updated book snapshot to all WebSocket subscribers."""
    await ws_manager.broadcast(symbol, snapshot)


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting LOB Matching Engine...")

    # Initialize DB (creates tables if not exists)
    await init_db()

    # Wire up callbacks
    matching_engine.register_callbacks(
        on_order=_on_order,
        on_trade=_on_trade,
        on_book_update=_on_book_update,
    )

    logger.info("Matching engine ready.")
    yield

    # Cleanup
    logger.info("Shutting down...")
    await close_db()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="LOB Matching Engine",
    description=(
        "A production-quality Limit Order Book matching engine with price-time "
        "priority, REST API, WebSocket feed, and PostgreSQL persistence."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS — restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(router, prefix="/api/v1", tags=["Orders & Book"])
app.include_router(ws_router, tags=["WebSocket"])


@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({
        "service": "LOB Matching Engine",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/health",
    })
