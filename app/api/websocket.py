"""
WebSocket endpoint for live order book updates.

Connect: ws://localhost:8000/ws/{symbol}

On connect: immediately receives the current book snapshot.
On each order submission/cancel: receives a new book snapshot.
On each trade: receives a trade event.

Messages are JSON with a "type" field: "book_update" | "trade" | "error"
"""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.matching_engine import engine as matching_engine
from app.services.websocket_manager import ws_manager

logger = logging.getLogger(__name__)
ws_router = APIRouter()


@ws_router.websocket("/ws/{symbol}")
async def websocket_book_feed(websocket: WebSocket, symbol: str):
    """
    Subscribe to real-time order book updates for a symbol.

    Protocol:
      - Client connects to /ws/AAPL
      - Server sends current book snapshot immediately
      - Server sends updates on every book change
      - Connection stays alive until client disconnects
    """
    symbol = symbol.upper()
    await ws_manager.connect(websocket, symbol)

    try:
        # Send current snapshot immediately so the client isn't waiting
        snapshot = matching_engine.get_book_snapshot(symbol)
        await websocket.send_text(json.dumps({
            "type": "book_snapshot",
            "symbol": symbol,
            "data": snapshot,
        }))

        logger.info(f"Client subscribed to {symbol} feed")

        # Keep connection open; client can send pings if desired
        while True:
            data = await websocket.receive_text()
            # Handle optional client messages (ping/pong, unsubscribe)
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass  # Ignore malformed messages

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from {symbol} feed")
    except Exception as e:
        logger.error(f"WebSocket error for {symbol}: {e}")
    finally:
        await ws_manager.disconnect(websocket, symbol)
