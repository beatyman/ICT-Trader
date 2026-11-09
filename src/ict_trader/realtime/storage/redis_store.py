"""Redis hot cache for the realtime K-line pipeline.

Every mutation is pipelined: one RTT for the latest hash + closed index +
closed detail + TTLs.  Redis is explicitly a cache, so noeviction is configured
and only cache keys carry TTLs.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from ..models import Kline

log = logging.getLogger(__name__)


def _latest_key(symbol: str, interval: str) -> str:
    return f"kline:latest:{symbol}:{interval}"


def _closed_zset_key(symbol: str, interval: str) -> str:
    return f"kline:closed:zset:{symbol}:{interval}"


def _closed_hash_key(symbol: str, interval: str, open_time: int) -> str:
    return f"kline:closed:hash:{symbol}:{interval}:{open_time}"


class RedisStore:
    def __init__(self, url: str, *, socket_timeout: float = 5.0,
                 latest_ttl: int = 172800, closed_ttl: int = 604800,
                 closed_keep: int = 5000) -> None:
        self.url = url
        self.latest_ttl = latest_ttl
        self.closed_ttl = closed_ttl
        self.closed_keep = closed_keep
        self._client: Redis | None = None
        self._timeout = socket_timeout

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("RedisStore.start() was not called")
        return self._client

    async def start(self) -> None:
        self._client = Redis.from_url(
            self.url,
            decode_responses=True,
            socket_timeout=self._timeout,
            socket_connect_timeout=self._timeout,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        await self._client.ping()
        log.info("Redis connected: %s", self.url)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> tuple[bool, float]:
        start = time.perf_counter()
        try:
            ok = bool(await self.client.ping())
            return ok, (time.perf_counter() - start) * 1000.0
        except Exception:
            return False, (time.perf_counter() - start) * 1000.0

    # ------------------------------------------------------------------
    async def update_latest(self, bar: Kline) -> float:
        start = time.perf_counter()
        async with self.client.pipeline(transaction=False) as pipe:
            pipe.hset(_latest_key(bar.symbol, bar.interval), mapping=bar.to_redis())
            pipe.expire(_latest_key(bar.symbol, bar.interval), self.latest_ttl)
            pipe.set(f"kline:price:{bar.symbol}", repr(bar.close), ex=300)
            await pipe.execute()
        return (time.perf_counter() - start) * 1000.0

    async def store_closed(self, bar: Kline, *, recovered: bool = False) -> float:
        """Write the ZSet index + detail hash atomically.

        The open_time is both member and score.  The hash has a TTL because
        ClickHouse is the durable copy; the index is trimmed to the configured
        hot window to keep Redis memory bounded.
        """
        start = time.perf_counter()
        zkey = _closed_zset_key(bar.symbol, bar.interval)
        hkey = _closed_hash_key(bar.symbol, bar.interval, bar.open_time)
        async with self.client.pipeline(transaction=False) as pipe:
            pipe.hset(_latest_key(bar.symbol, bar.interval), mapping=bar.to_redis())
            pipe.expire(_latest_key(bar.symbol, bar.interval), self.latest_ttl)
            pipe.hset(hkey, mapping=bar.to_redis())
            pipe.expire(hkey, self.closed_ttl)
            pipe.zadd(zkey, {str(bar.open_time): bar.open_time})
            pipe.zremrangebyrank(zkey, 0, -(self.closed_keep + 1))
            pipe.expire(zkey, self.closed_ttl)
            pipe.set("kline:last_ingest_ms", str(bar.ingest_time))
            await pipe.execute()
        return (time.perf_counter() - start) * 1000.0

    async def seed_closed(self, bars: list[Kline], *, keep: int = 800) -> int:
        """Seed the hot closed-bar window with one pipeline per symbol/stream.

        Bootstrap can carry thousands of bars per symbol.  Redis only needs the
        recent window; ClickHouse receives the full history through the batched
        writer.
        """
        if not bars:
            return 0
        grouped: dict[tuple[str, str], list[Kline]] = {}
        for bar in bars:
            grouped.setdefault((bar.symbol, bar.interval), []).append(bar)
        written = 0
        for (symbol, interval), items in grouped.items():
            items = sorted(items, key=lambda b: b.open_time)[-keep:]
            zkey = _closed_zset_key(symbol, interval)
            async with self.client.pipeline(transaction=False) as pipe:
                for bar in items:
                    pipe.hset(
                        _closed_hash_key(symbol, interval, bar.open_time),
                        mapping=bar.to_redis(),
                    )
                    pipe.expire(_closed_hash_key(symbol, interval, bar.open_time), self.closed_ttl)
                pipe.zadd(zkey, {str(b.open_time): b.open_time for b in items})
                pipe.zremrangebyrank(zkey, 0, -(self.closed_keep + 1))
                pipe.expire(zkey, self.closed_ttl)
                if items:
                    pipe.hset(_latest_key(symbol, interval), mapping=items[-1].to_redis())
                    pipe.expire(_latest_key(symbol, interval), self.latest_ttl)
                await pipe.execute()
            written += len(items)
        return written

    async def get_latest(self, symbol: str, interval: str) -> Kline | None:
        data = await self.client.hgetall(_latest_key(symbol.upper(), interval))
        return Kline.from_redis(data) if data else None

    async def get_closed(self, symbol: str, interval: str, limit: int = 200) -> list[Kline]:
        symbol, interval = symbol.upper(), interval
        zkey = _closed_zset_key(symbol, interval)
        members = await self.client.zrange(zkey, -limit, -1)
        if not members:
            return []
        async with self.client.pipeline(transaction=False) as pipe:
            for member in members:
                pipe.hgetall(_closed_hash_key(symbol, interval, int(member)))
            rows = await pipe.execute()
        return [Kline.from_redis(row) for row in rows if row]

    async def get_closed_range(self, symbol: str, interval: str,
                               start_ms: int, end_ms: int) -> list[Kline]:
        zkey = _closed_zset_key(symbol.upper(), interval)
        members = await self.client.zrangebyscore(zkey, start_ms, end_ms)
        if not members:
            return []
        async with self.client.pipeline(transaction=False) as pipe:
            for member in members:
                pipe.hgetall(_closed_hash_key(symbol.upper(), interval, int(member)))
            rows = await pipe.execute()
        return [Kline.from_redis(row) for row in rows if row]

    async def info(self) -> dict[str, Any]:
        try:
            memory = await self.client.info("memory")
            stats = await self.client.info("stats")
            return {
                "used_memory_human": memory.get("used_memory_human"),
                "used_memory": int(memory.get("used_memory", 0)),
                "maxmemory": int(memory.get("maxmemory", 0)),
                "evicted_keys": int(stats.get("evicted_keys", 0)),
                "keyspace_hits": int(stats.get("keyspace_hits", 0)),
                "keyspace_misses": int(stats.get("keyspace_misses", 0)),
            }
        except RedisError as exc:
            return {"error": str(exc)}
