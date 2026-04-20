from __future__ import annotations

from pathlib import Path

from aiohttp import web


STATIC_DIR = Path(__file__).resolve().parent / "static"


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_static("/", STATIC_DIR, show_index=True)
    return app


def run_server(host: str = "127.0.0.1", port: int = 3000) -> None:
    app = create_app()
    print(f"Matching engine frontend running at http://{host}:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
