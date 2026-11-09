"""Rate-limited Binance REST backfill for missing closed bars.

Weight control:
  * one global async lock serialises requests (weight 2 each for klines);
  * at most ``rate_per_sec`` requests/second;
  * HTTP 429 honours Retry-After, 418 backs off hard;
  * transient failures retry with exponential backoff + jitter.

A failed recovery is surfaced to the engine, which keeps the stream paused and
its queue buffered.  A dead-letter row is written only after the configured
retry budget is exhausted.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx
import orjson

from .metrics import Metrics
from .models import Kline, interval_ms

log = logging.getLogger(__name__)


class BackfillError(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, rate_per_sec: float) -> None:
        self.min_interval = 1.0 / max(rate_per_sec, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class RestBackfiller:
    def __init__(
        self,
        *,
        base_url: str,
        market: str,
        metrics: Metrics,
        max_retries: int = 6,
        rate_per_sec: float = 4.0,
        timeout: float = 20.0,
        max_bars: int = 20000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.market = market
        self.metrics = metrics
        self.max_retries = max(1, max_retries)
        self.max_bars = max_bars
        self.limiter = RateLimiter(rate_per_sec)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": "ict-trader-realtime/0.1"},
        )

    @property
    def klines_path(self) -> str:
        return "/fapi/v1/klines" if self.market == "futures" else "/api/v3/klines"

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    async def _request(self, params: dict[str, Any]) -> list[list[Any]]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            await self.limiter.wait()
            try:
                response = await self._client.get(self.klines_path, params=params)
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "2"))
                    self.metrics.inc("rest_429")
                    await asyncio.sleep(min(60.0, max(1.0, retry_after)))
                    raise BackfillError("HTTP 429 rate limited")
                if response.status_code == 418:
                    self.metrics.inc("rest_418")
                    await asyncio.sleep(30.0 * (attempt + 1))
                    raise BackfillError("HTTP 418 IP banned")
                response.raise_for_status()
                payload = orjson.loads(response.content)
                if not isinstance(payload, list):
                    raise BackfillError(f"unexpected REST payload: {type(payload).__name__}")
                self.metrics.inc("rest_requests")
                return payload
            except Exception as exc:
                last_error = exc
                delay = min(20.0, 0.5 * (2 ** attempt)) + random.random() * 0.25
                self.metrics.inc("rest_retry")
                log.warning("REST backfill attempt %d/%d failed: %s (sleep %.1fs)",
                            attempt + 1, self.max_retries, exc, delay)
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(delay)
        raise BackfillError(f"REST backfill failed after {self.max_retries} attempts: {last_error}")

    async def fetch_range(self, symbol: str, interval: str,
                          start_ms: int, end_ms: int) -> list[Kline]:
        step = interval_ms(interval)
        if end_ms < start_ms:
            return []
        expected = (end_ms - start_ms) // step + 1
        if expected > self.max_bars:
            raise BackfillError(
                f"refusing huge backfill {expected} bars for {symbol} {interval}"
            )
        out: list[Kline] = []
        cursor = start_ms
        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + step * 1000 - 1)
            payload = await self._request({
                "symbol": symbol.upper(),
                "interval": interval,
                "startTime": cursor,
                "endTime": chunk_end,
                "limit": 1000,
            })
            if not payload:
                cursor = chunk_end + 1
                continue
            for row in payload:
                bar = Kline.from_rest(symbol, interval, row)
                if bar.open_time <= end_ms:
                    out.append(bar)
            newest = max(int(row[0]) for row in payload)
            if newest < cursor:
                break
            cursor = newest + step
        dedup = {b.open_time: b for b in out}
        return [dedup[k] for k in sorted(dedup)]

    async def fetch_last(self, symbol: str, interval: str, limit: int) -> list[Kline]:
        """Fetch the most recent ``limit`` closed bars, newest page first."""
        step = interval_ms(interval)
        now_ms = int(time.time() * 1000)
        end = now_ms
        collected: dict[int, Kline] = {}
        while len(collected) < limit:
            want = min(1000, limit - len(collected))
            payload = await self._request({
                "symbol": symbol.upper(),
                "interval": interval,
                "endTime": end,
                "limit": want,
            })
            if not payload:
                break
            for row in payload:
                bar = Kline.from_rest(symbol, interval, row)
                if bar.close_time <= now_ms:
                    collected[bar.open_time] = bar
            earliest = min(int(row[0]) for row in payload)
            end = earliest - 1
            if len(payload) < want:
                break
        bars = [collected[k] for k in sorted(collected)]
        return bars[-limit:]
