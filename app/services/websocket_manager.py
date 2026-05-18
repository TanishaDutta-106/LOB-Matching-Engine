"""
WebSocket Connection Manager

Manages active WebSocket connections and broadcasts order book updates.
Clients subscribe to a specific symbol's feed.

Broadcast format (JSON):
  {
    "type": "book_update",
    "symbol": "AAPL",
    "data": { "bids": [...], "asks": [...], "best_bid": "...", ... }
  }
"""

import asyncio
import json
import logging
from collections import defaultdict
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """
    Manages subscriptions for live order book feeds.

    Each symbol has a set of connected WebSocket clients.
    When the matching engine produces an update, it calls broadcast()
    which sends the new snapshot to all subscribers.
    """

    def __init__(self):
        # symbol -> set of active WebSocket connections
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, symbol: str) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[symbol].add(websocket)
        logger.info(f"WebSocket connected: symbol={symbol}, total={len(self._connections[symbol])}")

    async def disconnect(self, websocket: WebSocket, symbol: str) -> None:
        async with self._lock:
            self._connections[symbol].discard(websocket)
        logger.info(f"WebSocket disconnected: symbol={symbol}")

    async def broadcast(self, symbol: str, snapshot: dict) -> None:
        """Send a book snapshot to all subscribers of this symbol."""
        payload = json.dumps({
            "type": "book_update",
            "symbol": symbol,
            "data": snapshot,
        })

        dead_connections = set()
        async with self._lock:
            subscribers = set(self._connections.get(symbol, set()))

        for ws in subscribers:
            try:
                await ws.send_text(payload)
            except Exception:
                dead_connections.add(ws)

        # Clean up dead connections
        if dead_connections:
            async with self._lock:
                self._connections[symbol] -= dead_connections

    async def send_trade(self, symbol: str, trade_dict: dict) -> None:
        """Send a trade execution event to subscribers."""
        payload = json.dumps({
            "type": "trade",
            "symbol": symbol,
            "data": trade_dict,
        })

        async with self._lock:
            subscribers = set(self._connections.get(symbol, set()))

        for ws in subscribers:
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    def subscriber_count(self, symbol: str) -> int:
        return len(self._connections.get(symbol, set()))


# Singleton
ws_manager = WebSocketManager()
