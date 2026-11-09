"""Binance combined-stream WebSocket consumer.

Connections are grouped by interval and hard-capped at
``max_streams_per_conn`` (default 100).  Each group reconnects independently
with exponential backoff; a dropped connection is repaired by the gap detector
and REST backfill when the next closed bar arrives.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import websockets

from .metrics import Metrics
from .models import Kline, utc_ms

log = logging.getLogger(__name__)


class BinanceKlineFeed:
    name = "binance-combined-ws"

    def __init__(
        self,
        *,
        ws_base_url: str,
        groups: list[list[tuple[str, str]]],
        metrics: Metrics,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        reconnect_max: float = 30.0,
    ) -> None:
        self.ws_base_url = ws_base_url.rstrip("?")
        self.groups = groups
        self.metrics = metrics
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.reconnect_max = reconnect_max
        self._tasks: list[asyncio.Task] = []
        self._sockets: set[Any] = set()
        self._running = False
        self._active_connections = 0
        self.last_message_ms = 0
        self.reconnects = 0

    # ------------------------------------------------------------------
    async def start(self, on_kline: Callable[[Kline], Awaitable[None]]) -> None:
        self._running = True
        for index, group in enumerate(self.groups):
            self._tasks.append(asyncio.create_task(
                self._connection_loop(index, group, on_kline),
                name=f"ws-group-{index}",
            ))
        streams = sum(len(g) for g in self.groups)
        self.metrics.event(
            "INFO", "feed",
            f"Binance WS starting: {streams} streams / {len(self.groups)} connections",
        )

    async def stop(self) -> None:
        self._running = False
        for socket in list(self._sockets):
            try:
                await socket.close()
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._sockets.clear()
        self._active_connections = 0

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self._active_connections > 0,
            "connections": self._active_connections,
            "configured_connections": len(self.groups),
            "streams": sum(len(g) for g in self.groups),
            "reconnects": self.reconnects,
            "last_message_ms": self.last_message_ms,
        }

    # ------------------------------------------------------------------
    def _url(self, group: list[tuple[str, str]]) -> str:
        streams = "/".join(f"{symbol.lower()}@kline_{interval}" for symbol, interval in group)
        return f"{self.ws_base_url}?streams={streams}"

    async def _connection_loop(
        self,
        index: int,
        group: list[tuple[str, str]],
        on_kline: Callable[[Kline], Awaitable[None]],
    ) -> None:
        delay = 0.5
        while self._running:
            url = self._url(group)
            started = time.monotonic()
            try:
                async with websockets.connect(
                    url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    close_timeout=5,
                    max_queue=2048,
                    open_timeout=15,
                ) as socket:
                    self._sockets.add(socket)
                    self._active_connections += 1
                    self.metrics.event(
                        "INFO", "feed",
                        f"WS group {index} connected ({len(group)} streams)",
                    )
                    delay = 0.5
                    async for raw in socket:
                        if not self._running:
                            break
                        await self._on_raw(raw, group, on_kline)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._running:
                    self.reconnects += 1
                    self.metrics.inc("ws_reconnect")
                    self.metrics.event(
                        "WARN", "feed",
                        f"WS group {index} disconnected; reconnecting",
                        error=str(exc)[:240],
                    )
                    log.warning("WS group %d error: %s", index, exc)
            finally:
                self._active_connections = max(0, self._active_connections - 1)
                for socket in list(self._sockets):
                    if getattr(socket, "state", None) is not None and getattr(socket, "state").name != "OPEN":
                        self._sockets.discard(socket)
                if self._running and time.monotonic() - started < 2:
                    # A tight failure loop should back off; a healthy multi-hour
                    # connection can reconnect immediately.
                    await asyncio.sleep(delay + random.random() * 0.3)
                    delay = min(self.reconnect_max, delay * 2)
                elif self._running:
                    await asyncio.sleep(0.2)

    async def _on_raw(self, raw: bytes | str, group: list[tuple[str, str]],
                      on_kline: Callable[[Kline], Awaitable[None]]) -> None:
        start = time.perf_counter()
        try:
            payload = orjson.loads(raw)
            data = payload.get("data")
            if not isinstance(data, dict) or data.get("e") != "kline":
                return
            kline = Kline.from_binance(data, source="ws")
            if not kline.symbol or not kline.interval:
                return
            self.last_message_ms = utc_ms()
            self.metrics.inc("ws_message")
            self.metrics.observe("parse_ms", (time.perf_counter() - start) * 1000.0)
            await on_kline(kline)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.metrics.inc("parse_error")
            self.metrics.event("WARN", "feed", "failed to parse WS message", error=str(exc)[:200])
