"""Deterministic synthetic market data for offline demos and CI.

The series is intentionally regime-switching and fat-tailed enough to exercise
indicators, gaps and chart rendering.  It is NOT market data and must never be
used for performance claims.  The dashboard shows a SIM badge in this mode.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from .metrics import Metrics
from .models import Kline, align_open_time, interval_ms, utc_ms

log = logging.getLogger(__name__)

BASE_PRICES = {
    "BTCUSDT": 74_000.0,
    "ETHUSDT": 2_100.0,
    "SOLUSDT": 87.0,
    "BNBUSDT": 680.0,
    "XRPUSDT": 1.10,
    "DOGEUSDT": 0.069,
    "ADAUSDT": 0.62,
    "AVAXUSDT": 28.0,
    "LINKUSDT": 18.0,
    "TONUSDT": 4.2,
}

VOL_BY_SYMBOL = {
    "BTCUSDT": 0.0018,
    "ETHUSDT": 0.0026,
    "SOLUSDT": 0.0034,
    "BNBUSDT": 0.0025,
    "XRPUSDT": 0.0031,
    "DOGEUSDT": 0.0042,
}


def _seed(symbol: str, interval: str) -> int:
    return zlib.crc32(f"{symbol}:{interval}".encode()) & 0xFFFFFFFF


def _base_price(symbol: str) -> float:
    return BASE_PRICES.get(symbol.upper(), 10.0 + (zlib.crc32(symbol.encode()) % 900) / 10.0)


def generate_history(symbol: str, interval: str, count: int,
                     *, end_open_time: int | None = None,
                     source: str = "demo") -> list[Kline]:
    """Generate ``count`` closed bars ending at the latest closed interval."""
    if count <= 0:
        return []
    step = interval_ms(interval)
    if end_open_time is None:
        end_open_time = align_open_time(utc_ms(), interval) - step
    start_open = end_open_time - (count - 1) * step
    rng = np.random.default_rng(_seed(symbol, interval))
    base = _base_price(symbol)
    vol = VOL_BY_SYMBOL.get(symbol.upper(), 0.003)

    # Walk forward from a plausible but not identical start price.
    price = base * (0.92 + rng.random() * 0.12)
    bars: list[Kline] = []
    for i in range(count):
        macro = 0.00018 * math.sin(i / 97.0 + 0.7) + 0.00010 * math.sin(i / 23.0)
        momentum = 0.04 * (bars[-1].close - bars[-2].close) / bars[-2].close if len(bars) > 1 else 0.0
        shock = 0.0
        if i and i % 211 == 0:
            shock = (1 if rng.random() > 0.5 else -1) * vol * (2.5 + rng.random() * 2.0)
        ret = macro + momentum + rng.normal(0.0, vol) + shock
        open_ = max(1e-9, price)
        close = max(1e-9, open_ * (1.0 + ret))
        wick = abs(rng.normal(0.0, vol * 0.72))
        high = max(open_, close) * (1.0 + wick * (0.7 + rng.random()))
        low = min(open_, close) * (1.0 - wick * (0.7 + rng.random()))
        volume = float(abs(rng.lognormal(mean=9.2, sigma=0.55)) * (1.0 + abs(ret) * 900))
        quote = volume * (high + low) / 2.0
        trades = int(max(20, volume / max(0.05, volume / 5000)))
        open_time = start_open + i * step
        bars.append(Kline(
            symbol=symbol.upper(), interval=interval, open_time=open_time,
            close_time=open_time + step - 1, open=open_, high=high, low=low,
            close=close, volume=volume, quote_volume=quote, trades=trades,
            taker_buy_base=volume * (0.45 + rng.random() * 0.1),
            taker_buy_quote=quote * (0.45 + rng.random() * 0.1),
            is_closed=True, source=source,
        ))
        price = close
    return bars


class DemoFeed:
    """Emit accelerated synthetic bars.

    Each ``(symbol, interval)`` has its own clock: a logical bar closes every
    ``speed`` seconds, with ``subticks`` unclosed updates in between so the
    dashboard has a live last candle and Redis sees the unclosed hot path.
    """

    name = "synthetic-demo"

    def __init__(self, *, symbols: list[str], intervals: list[str], metrics: Metrics,
                 speed: float = 1.0, subticks: int = 4) -> None:
        self.symbols = symbols
        self.intervals = intervals
        self.metrics = metrics
        self.speed = max(0.2, speed)
        self.subticks = max(1, subticks)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.connections = 1
        self.connected = False
        self.last_message_ms = 0

    async def start(self, on_kline: Callable[[Kline], Awaitable[None]]) -> None:
        self._running = True
        self.connected = True
        for symbol in self.symbols:
            for interval in self.intervals:
                self._tasks.append(asyncio.create_task(
                    self._run_stream(symbol, interval, on_kline),
                    name=f"demo-{symbol}-{interval}",
                ))
        self.metrics.event("INFO", "feed", f"demo feed started: {len(self._tasks)} streams")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.connected = False

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "connections": self.connections,
            "streams": len(self._tasks),
            "last_message_ms": self.last_message_ms,
        }

    async def _run_stream(self, symbol: str, interval: str,
                          on_kline: Callable[[Kline], Awaitable[None]]) -> None:
        step = interval_ms(interval)
        rng = np.random.default_rng(_seed(symbol, interval) ^ 0x5A5A5A5A)
        history = generate_history(symbol, interval, 2)
        last_close = history[-1].close
        open_time = align_open_time(utc_ms(), interval)
        vol = VOL_BY_SYMBOL.get(symbol.upper(), 0.003)
        try:
            while self._running:
                open_ = last_close
                close = open_
                high = open_
                low = open_
                volume = 0.0
                quote = 0.0
                trades = 0
                tick_sleep = self.speed / self.subticks
                for tick in range(self.subticks):
                    ret = rng.normal(0.0, vol * 0.38)
                    close = max(1e-9, close * (1.0 + ret))
                    high = max(high, close)
                    low = min(low, close)
                    if tick == self.subticks - 1:
                        # Make the demo series a little more directional.
                        high = max(high, close * (1.0 + abs(rng.normal(0, vol * 0.3))))
                        low = min(low, close * (1.0 - abs(rng.normal(0, vol * 0.3))))
                    fill = float(abs(rng.normal(1.0, 0.25)))
                    volume += fill
                    quote += fill * close
                    trades += int(5 + rng.random() * 20)
                    bar = Kline(
                        symbol=symbol.upper(), interval=interval,
                        open_time=open_time, close_time=open_time + step - 1,
                        open=open_, high=high, low=low, close=close,
                        volume=volume, quote_volume=quote, trades=trades,
                        taker_buy_base=volume * 0.49,
                        taker_buy_quote=quote * 0.49,
                        is_closed=(tick == self.subticks - 1),
                        source="demo",
                    )
                    self.last_message_ms = utc_ms()
                    self.metrics.inc("ws_message")
                    await on_kline(bar)
                    if tick < self.subticks - 1:
                        await asyncio.sleep(tick_sleep)
                last_close = close
                open_time += step
                await asyncio.sleep(max(0.02, self.speed / self.subticks))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("demo stream failed: %s %s", symbol, interval)
            self.metrics.event("ERROR", "feed", f"demo stream failed {symbol} {interval}", error=str(exc))
