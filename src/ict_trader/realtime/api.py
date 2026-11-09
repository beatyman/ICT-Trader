"""FastAPI dashboard + JSON API + WebSocket snapshot stream."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import orjson
from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .ai import AiReviewError
from .engine import RealtimeEngine

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"


class WSManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.discard(websocket)

    async def send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_text(orjson.dumps(payload, default=str).decode())

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.active:
            return
        text = orjson.dumps(payload, default=str).decode()
        dead = []
        for websocket in list(self.active):
            try:
                await websocket.send_text(text)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.active.discard(websocket)


def create_app(engine: RealtimeEngine) -> FastAPI:
    manager = WSManager()

    async def run_engine() -> None:
        try:
            await engine.start()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("engine startup failed")

    async def broadcast_loop() -> None:
        while True:
            try:
                await asyncio.sleep(engine.settings.broadcast_seconds)
                if manager.active:
                    await manager.broadcast(engine.snapshot())
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("broadcast failed: %s", exc)
                await asyncio.sleep(2.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine_task = asyncio.create_task(run_engine(), name="engine")
        app.state.broadcaster = asyncio.create_task(broadcast_loop(), name="broadcast")
        try:
            yield
        finally:
            app.state.broadcaster.cancel()
            await asyncio.gather(app.state.broadcaster, return_exceptions=True)
            if engine.phase in {"ready", "starting", "error"}:
                await engine.stop()
            task = app.state.engine_task
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(
        title="ICT Realtime Paper Trader",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.ws = manager

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health() -> JSONResponse:
        snap = engine.snapshot()
        ok = engine.phase == "ready" and snap["health"].get("redis", {}).get("ok") \
            and snap["health"].get("clickhouse", {}).get("ok")
        return JSONResponse(
            {
                "ok": bool(ok),
                "phase": engine.phase,
                "boot": snap["boot"],
                "boot_error": engine.boot_error,
                "health": snap["health"],
                "mode": engine.settings.mode,
                "paper_trade": engine.settings.paper_trade,
            },
            status_code=200 if ok else 503,
        )

    @app.get("/api/snapshot")
    async def snapshot() -> JSONResponse:
        return JSONResponse(engine.snapshot())

    @app.get("/api/config")
    async def config() -> JSONResponse:
        return JSONResponse(engine.settings.public_dict())

    @app.get("/api/events")
    async def events(limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
        return JSONResponse(engine.metrics.snapshot()["events"][:limit])

    @app.get("/api/signals")
    async def signals() -> JSONResponse:
        return JSONResponse([s.to_public() for s in engine.runner.visible_signals()[:50]])

    @app.get("/api/ai/decisions")
    async def ai_decisions() -> JSONResponse:
        persisted = await engine.ch.load_ai_decisions(50)
        if persisted:
            return JSONResponse(persisted)
        return JSONResponse(list(engine.ai_decisions)[-50:])

    @app.get("/api/chan/structure")
    async def chan_structure(
        symbol: str,
        interval: str | None = Query(None),
        limit: int = Query(700, ge=120, le=2000),
    ) -> JSONResponse:
        iv = interval or engine.settings.strategy_interval
        data = await engine.chan_structure(symbol, iv, limit)
        return JSONResponse({"symbol": symbol.upper(), "interval": iv, **data})

    @app.get("/api/engines")
    async def engines() -> JSONResponse:
        return JSONResponse({
            "registry": list(engine.registry.names()),
            "breaker": engine.registry.breaker.snapshot() if engine.registry.breaker else {},
            "runs": list(engine.registry_runs),
            "signals": [s.to_public() for s in list(engine.registry_signals)[-30:]],
        })

    @app.get("/api/setups")
    async def setups() -> JSONResponse:
        return JSONResponse([s.to_public() for s in engine.setups.all()[:100]])

    @app.get("/api/signals/{signal_id}/context")
    async def signal_context(signal_id: str) -> JSONResponse:
        signal = engine.runner.get_signal(signal_id)
        if signal is None:
            return JSONResponse({"ok": False, "error": "signal not found"}, status_code=404)
        from .models import interval_ms
        step = interval_ms(signal.interval)
        end = signal.bar_time + 70 * step
        bars = await engine.klines(signal.symbol, signal.interval, 180, end)
        aligned_bar = next(
            (b for b in bars if int(b["open_time"]) == int(signal.bar_time)),
            None,
        )
        return JSONResponse({
            "ok": True,
            "signal": signal.to_public(),
            "aligned": aligned_bar is not None,
            "signal_bar": aligned_bar,
            "bars_before": sum(1 for b in bars if int(b["open_time"]) < signal.bar_time),
            "bars_after": sum(1 for b in bars if int(b["open_time"]) > signal.bar_time),
            "bars": bars,
        })

    @app.get("/api/ai/status")
    async def ai_status() -> JSONResponse:
        return JSONResponse(engine.ai.status())

    @app.post("/api/ai/review")
    async def ai_review(payload: dict | None = Body(default=None)) -> JSONResponse:
        payload = payload or {}
        symbol = str(payload.get("symbol") or engine.settings.symbols[0]).upper()
        if symbol not in engine.settings.symbols:
            return JSONResponse({"ok": False, "error": "unknown symbol"}, status_code=400)
        if not engine.ai.available:
            return JSONResponse({"ok": False, "error": "DeepSeek API key is not configured"}, status_code=503)
        if engine.phase != "ready":
            return JSONResponse({"ok": False, "error": "engine is not ready"}, status_code=409)
        bars = engine.runner.frame(symbol, 80)
        try:
            result = await engine.ai.review(engine.snapshot(), bars, symbol)
        except AiReviewError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, "review": result})

    @app.post("/api/paper/flatten")
    async def flatten() -> JSONResponse:
        count = await engine.flatten_all()
        return JSONResponse({"ok": True, "closed": count})

    @app.get("/api/klines")
    async def klines(
        symbol: str = Query(...),
        interval: str | None = Query(None),
        limit: int = Query(240, ge=20, le=2000),
        end_time: int | None = Query(None, ge=0),
        include_live: bool = Query(False),
    ) -> JSONResponse:
        iv = interval or engine.settings.strategy_interval
        rows = await engine.klines(symbol, iv, limit, end_time, include_live)
        return JSONResponse({
            "symbol": symbol.upper(), "interval": iv,
            "end_time": end_time, "include_live": include_live, "bars": rows,
        })

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            await manager.send(websocket, engine.snapshot())
            while True:
                # We do not require client messages; reading keeps the socket
                # alive and detects a clean close immediately.
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except Exception:
            manager.disconnect(websocket)
            try:
                await websocket.close()
            except Exception:
                pass

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    return app
