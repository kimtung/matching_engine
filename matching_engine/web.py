from __future__ import annotations

import asyncio
import time

from aiohttp import WSMsgType, web

from matching_engine.service import MatchingEngineService

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


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

    async def broadcast(self, event: str, trades: list[dict] | None = None) -> None:
        payload = {
            "event": event,
            "state": self.state(),
            "trades": trades or [],
        }
        stale_clients: list[web.WebSocketResponse] = []
        for client in self.clients:
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
    return web.json_response(hub.state())


async def place_order(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    payload = await request.json()
    order_type = payload.get("order_type", "LIMIT")
    order_id = payload.get("order_id") or f"ORD-{int(time.time() * 1000)}"
    side = payload["side"]
    quantity = int(payload["quantity"])
    timestamp = int(payload.get("timestamp") or time.time() * 1000)

    async with hub.lock:
        if order_type == "MARKET":
            trades = hub.service.place_market_order(order_id, side, quantity, timestamp)
        else:
            price = float(payload["price"])
            trades = hub.service.place_limit_order(order_id, side, quantity, price, timestamp)
        state = hub.state()

    await hub.broadcast("order_placed", trades)
    return web.json_response({"ok": True, "trades": trades, "state": state}, status=201)


async def cancel_order(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    payload = await request.json()
    order_id = payload["order_id"]

    async with hub.lock:
        cancelled = hub.service.cancel_order(order_id)
        state = hub.state()

    if cancelled:
        if order_id.startswith("B"):
            await hub.broadcast("order_cancelled")
        return web.json_response({"ok": True, "state": state})
    return web.json_response({"ok": False, "state": state}, status=404)


async def reset_book(request: web.Request) -> web.Response:
    hub: EngineHub = request.app["hub"]
    async with hub.lock:
        hub.service = MatchingEngineService()
        state = hub.state()
    await hub.broadcast("book_reset")
    return web.json_response({"ok": True, "state": state})


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    hub: EngineHub = request.app["hub"]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)
    hub.clients.add(socket)
    await socket.send_json({"event": "connected", "state": hub.state(), "trades": []})

    async for message in socket:
        if message.type == WSMsgType.ERROR:
            break

    hub.clients.discard(socket)
    return socket


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    app = create_app()
    print(f"Matching engine backend running at http://{host}:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
