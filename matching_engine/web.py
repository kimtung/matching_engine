from __future__ import annotations

import asyncio
import os
import time
import uuid

from aiohttp import WSMsgType, web

from matching_engine.service import MatchingEngineService

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

# BUG-05 fix: reset requires ADMIN_TOKEN env var to be set and matched
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


class EngineHub:
    def __init__(self) -> None:
        self.service = MatchingEngineService()
        self.clients: set[web.WebSocketResponse] = set()
        self.lock = asyncio.Lock()

    def state(self) -> dict:
        book = self.service.get_order_book()
        return {
            "book": book,
            "active_orders": self.service.get_active_orders(),
        }

    async def broadcast(self, event: str, trades: list[dict] | None = None, state: dict | None = None) -> None:
        # BUG-08 fix: accept pre-captured state so broadcast uses the same snapshot
        # that was captured inside the lock, preventing state/trade mismatch
        payload = {
            "event": event,
            "state": state if state is not None else self.state(),
            "trades": trades or [],
        }
        stale_clients: list[web.WebSocketResponse] = []
        # BUG-07 fix: snapshot set to prevent RuntimeError if clients mutate during await
        for client in list(self.clients):
            if client.closed:
                stale_clients.append(client)
                continue
            await client.send_json(payload)
        for client in stale_clients:
            self.clients.discard(client)


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["hub"] = EngineHub()
    app.router.add_route("OPTIONS", "/api/{tail:.*}", options_handler)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/health", health)
    app.router.add_post("/api/orders", place_order)
    app.router.add_post("/api/cancel", cancel_order)
    app.router.add_post("/api/reset", reset_book)
    app.router.add_get("/ws", websocket_handler)
    return app


@web.middleware
async def cors_middleware(request: web.Request, handler):
    response = await handler(request)
    response.headers.update(CORS_HEADERS)
    return response


async def options_handler(_: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS_HEADERS)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def get_state(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    # BUG-10 fix: acquire lock so reads cannot observe half-applied mutations
    # once engine operations become async (currently latent, preemptively fixed).
    async with hub.lock:
        state = hub.state()
    return web.json_response(state)


async def place_order(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    # BUG-10 fix: validate required fields before processing
    side = payload.get("side")
    if side not in ("BUY", "SELL"):
        return web.json_response({"ok": False, "error": "side must be BUY or SELL"}, status=400)

    try:
        quantity = int(payload["quantity"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "quantity must be a positive integer"}, status=400)
    if quantity <= 0:
        return web.json_response({"ok": False, "error": "quantity must be positive"}, status=400)

    order_type = payload.get("order_type", "LIMIT")
    # BUG-06 fix: reject unknown order_type instead of silently treating as LIMIT
    valid_types = ("LIMIT", "MARKET", "STOP_MARKET", "STOP_LIMIT")
    if order_type not in valid_types:
        return web.json_response(
            {"ok": False, "error": f"order_type must be one of {valid_types}"},
            status=400,
        )
    # BUG-09 fix: use UUID to avoid millisecond timestamp collision
    order_id = payload.get("order_id") or f"ORD-{uuid.uuid4().hex[:12].upper()}"
    timestamp = int(payload.get("timestamp") or time.time() * 1000)

    price: float | None = None
    if order_type in ("LIMIT", "STOP_LIMIT"):
        try:
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "price must be a positive number"}, status=400)
        if price <= 0:
            return web.json_response({"ok": False, "error": "price must be positive"}, status=400)

    stop_price: float | None = None
    if order_type in ("STOP_MARKET", "STOP_LIMIT"):
        try:
            stop_price = float(payload["stop_price"])
        except (KeyError, TypeError, ValueError):
            return web.json_response({"ok": False, "error": "stop_price must be a positive number"}, status=400)
        if stop_price <= 0:
            return web.json_response({"ok": False, "error": "stop_price must be positive"}, status=400)

    async with hub.lock:
        if order_type == "MARKET":
            trades = hub.service.place_market_order(order_id, side, quantity, timestamp)
        elif order_type == "LIMIT":
            trades = hub.service.place_limit_order(order_id, side, quantity, price, timestamp)
        elif order_type == "STOP_MARKET":
            trades = hub.service.place_stop_order(order_id, side, quantity, stop_price, timestamp)
        else:  # STOP_LIMIT
            trades = hub.service.place_stop_order(
                order_id, side, quantity, stop_price, timestamp, limit_price=price
            )
        state = hub.state()

    # BUG-08 fix: pass captured state into broadcast so clients see consistent snapshot
    await hub.broadcast("order_placed", trades, state)
    return web.json_response({"ok": True, "trades": trades, "state": state}, status=201)


async def cancel_order(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    order_id = payload.get("order_id")
    if not order_id:
        return web.json_response({"ok": False, "error": "order_id is required"}, status=400)

    async with hub.lock:
        cancelled = hub.service.cancel_order(order_id)
        state = hub.state()

    if cancelled:
        # BUG-04 fix: broadcast for ALL cancelled orders, not just "B"-prefixed ones
        await hub.broadcast("order_cancelled", state=state)
        return web.json_response({"ok": True, "state": state})
    return web.json_response({"ok": False, "state": state}, status=404)


async def reset_book(request: web.Request) -> web.Response:
    # BUG-05 fix: require admin token — set ADMIN_TOKEN env var to enable this endpoint
    if not ADMIN_TOKEN:
        return web.json_response({"ok": False, "error": "reset is disabled"}, status=403)
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_TOKEN:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    hub: EngineHub = request.app["hub"]
    async with hub.lock:
        hub.service = MatchingEngineService()
        state = hub.state()
    await hub.broadcast("book_reset", state=state)
    return web.json_response({"ok": True, "state": state})


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    hub: EngineHub = request.app["hub"]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)
    # BUG-08 fix: hold the hub lock across state capture, initial send, and
    # membership add. While the lock is held, no mutation can trigger a
    # broadcast, so "connected" is guaranteed to be the first message this
    # client receives — subsequent broadcasts queue after it on the socket.
    async with hub.lock:
        state = hub.state()
        await socket.send_json({"event": "connected", "state": state, "trades": []})
        hub.clients.add(socket)

    try:
        async for message in socket:
            if message.type == WSMsgType.ERROR:
                break
    finally:
        hub.clients.discard(socket)
    return socket


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    app = create_app()
    print(f"Matching engine backend running at http://{host}:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
