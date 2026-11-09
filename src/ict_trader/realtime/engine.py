"""Realtime orchestration: feed -> per-stream queue -> gap recovery -> stores.

The feed callback only enqueues.  Every ``(symbol, interval)`` has an
independent worker, so a REST backfill for BTC 15m cannot stall ETH 1m.  A
worker processes closed bars in order, detects gaps, pauses its own stream,
backfills, replays and only then resumes normal processing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .ai import DeepSeekReviewer
from .broker import PaperBroker
from .config import Settings
from .demo import DemoFeed, generate_history
from .feed import BinanceKlineFeed
from .gap_detector import GapDetector
from .metrics import Metrics
from .models import Kline, interval_ms, utc_ms, validate_ohlcv
from .rest_backfill import BackfillError, RestBackfiller
from .storage import ClickHouseStore, RedisStore, Wal
from .storage.observability import Observability
from .strategy_runner import ICTStrategyRunner, TradeSignal

log = logging.getLogger(__name__)


class StreamSession:
    def __init__(self, engine: "RealtimeEngine", symbol: str, interval: str) -> None:
        self.engine = engine
        self.symbol = symbol
        self.interval = interval
        self.queue: asyncio.Queue[Kline] = asyncio.Queue()
        self.detector = GapDetector(symbol=symbol, interval=interval)
        self.task: asyncio.Task | None = None
        self.last_update_time = 0
        self.closed_count = 0
        self.live_count = 0
        self.recovering = False

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name=f"stream-{self.symbol}-{self.interval}")

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def push(self, bar: Kline) -> None:
        self.queue.put_nowait(bar)

    async def run(self) -> None:
        while True:
            bar = await self.queue.get()
            try:
                if bar.is_closed:
                    await self._handle_closed(bar)
                else:
                    await self._handle_live(bar)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.engine.observability.error(
                    "runtime", "stream_worker", "engine",
                    f"{self.symbol} {self.interval} worker error",
                    symbol=self.symbol, interval=self.interval, exc=exc,
                )
                log.exception("stream worker error %s %s: %s", self.symbol, self.interval, exc)
                await asyncio.sleep(1.0)
                # Do not drop a closed bar on a transient failure.
                if bar.is_closed:
                    self.queue.put_nowait(bar)
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------
    async def _handle_live(self, bar: Kline) -> None:
        self.live_count += 1
        self.last_update_time = utc_ms()
        start = time.perf_counter()
        try:
            latency = await self.engine.redis.update_latest(bar)
            self.engine.metrics.observe("redis_ms", latency)
            self.engine.metrics.observe("pipeline_ms", (time.perf_counter() - start) * 1000.0)
        except Exception as exc:
            self.engine.metrics.inc("redis_error")
            self.engine.metrics.event("WARN", "redis", "latest update failed", error=str(exc)[:200])
        await self.engine.broker.on_price(bar.symbol, bar.close)

    async def _handle_closed(self, bar: Kline) -> None:
        if bar.symbol != self.symbol or bar.interval != self.interval:
            return
        if not validate_ohlcv(bar):
            self.engine.metrics.inc("invalid_bar")
            return
        decision = self.detector.observe(bar)
        if decision.kind in {"duplicate", "out_of_order"}:
            self.engine.metrics.inc("duplicate_bar")
            return
        if decision.kind == "gap":
            self.engine.metrics.inc("gap")
            self.engine.metrics.event(
                "WARN", "gap",
                f"{bar.symbol} {bar.interval} gap {decision.missing_bars} bars",
                gap_start=decision.gap_start,
                gap_end=decision.gap_end,
            )
            started = time.perf_counter()
            self.recovering = True
            try:
                await self._recover(bar.open_time)
            finally:
                self.recovering = False
            self.engine.metrics.observe("gap_recovery_ms", (time.perf_counter() - started) * 1000.0)
            self.engine.metrics.inc("gap_recovered")

        # Normal or post-recovery path.
        while True:
            try:
                await self.engine._process_closed(bar, recovered=False)
                self.detector.commit(bar)
                self.closed_count += 1
                self.last_update_time = utc_ms()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.engine.metrics.inc("closed_process_retry")
                await self.engine.observability.error(
                    "runtime", "closed_persist_failed", "stream",
                    "closed-bar persistence failed; retaining in queue",
                    symbol=self.symbol, interval=self.interval,
                    context={"open_time": bar.open_time}, exc=exc,
                )
                await asyncio.sleep(2.0)

    async def _recover(self, target_open_time: int) -> None:
        step = interval_ms(self.interval)
        attempts = 0
        while self.detector.last_closed_open_time is not None and \
                self.detector.last_closed_open_time + step < target_open_time:
            start = self.detector.last_closed_open_time + step
            end = target_open_time - step
            attempts += 1
            try:
                bars = await self.engine.rest.fetch_range(self.symbol, self.interval, start, end)
            except BackfillError as exc:
                attempts = 0
                self.engine.metrics.inc("backfill_failure")
                self.engine.wal.dead_letter(
                    "backfill_failure",
                    {"symbol": self.symbol, "interval": self.interval,
                     "start": start, "end": end},
                    str(exc),
                )
                await self.engine.observability.error(
                    "data_quality", "backfill_failed", "gap",
                    f"{self.symbol} {self.interval} backfill failed; stream stays paused",
                    symbol=self.symbol, interval=self.interval,
                    context={"start": start, "end": end}, exc=exc,
                )
                await asyncio.sleep(self.engine.settings.backfill_outer_retry_seconds)
                continue

            expected = start
            progressed = False
            for candidate in bars:
                if candidate.open_time < expected:
                    continue
                if candidate.open_time != expected:
                    break
                await self.engine._process_closed(candidate, recovered=True)
                self.detector.commit(candidate, recovered=True)
                expected += step
                progressed = True
                if expected > end:
                    break
            if progressed:
                self.engine.metrics.inc("backfilled_bars", max(0, (expected - start) // step))
            else:
                self.engine.metrics.inc("backfill_no_progress")
                await asyncio.sleep(self.engine.settings.backfill_outer_retry_seconds)
            if attempts > 50:
                raise BackfillError("too many backfill iterations")
        self.engine.metrics.event(
            "INFO", "gap",
            f"{self.symbol} {self.interval} gap recovered to {target_open_time}",
        )


class RealtimeEngine:
    def __init__(self, settings: Settings, metrics: Metrics | None = None) -> None:
        self.settings = settings
        self.metrics = metrics or Metrics()
        self.wal = Wal(settings.wal_path)
        self.redis = RedisStore(
            settings.redis_url,
            socket_timeout=settings.redis_socket_timeout,
            latest_ttl=settings.redis_latest_ttl,
            closed_ttl=settings.redis_closed_ttl,
            closed_keep=settings.redis_closed_keep,
        )
        self.rest = RestBackfiller(
            base_url=settings.rest_base_url,
            market=settings.market,
            metrics=self.metrics,
            max_retries=settings.backfill_max_retries,
            rate_per_sec=settings.backfill_rate_per_sec,
            max_bars=settings.backfill_max_bars,
        )
        self.ch = ClickHouseStore(
            host=settings.ch_host,
            port=settings.ch_port,
            username=settings.ch_user,
            password=settings.ch_password,
            database=settings.ch_database,
            secure=settings.ch_secure,
            batch_size=settings.ch_batch_size,
            flush_seconds=settings.ch_flush_seconds,
            retry_seconds=settings.ch_retry_seconds,
            retention_days=settings.ch_retention_days,
            wal=self.wal,
            metrics=self.metrics,
        )
        self.runner = ICTStrategyRunner(settings, self.metrics)
        self.broker = PaperBroker(settings, self.metrics)
        self.ai = DeepSeekReviewer(settings, self.metrics)
        self.observability = Observability(self.ch.analytics, self.metrics)
        self.runner.on_signal = self._on_signal
        self.broker.on_trade = self._on_trade
        self.sessions: dict[tuple[str, str], StreamSession] = {}
        self.feed: BinanceKlineFeed | DemoFeed | None = None
        self.phase = "idle"
        self.boot_error = ""
        self.boot_total = len(settings.streams)
        self.boot_done = 0
        self.started_at = ""
        self._health_task: asyncio.Task | None = None
        self._loop_lag_task: asyncio.Task | None = None
        self._health: dict[str, Any] = {
            "redis": {"ok": False, "latency_ms": 0.0},
            "clickhouse": {"ok": False, "latency_ms": 0.0},
            "wal": {"pending": 0, "dead_letters": 0},
        }
        self._ch_rows: int | None = None
        self._redis_info: dict[str, Any] = {}
        self._last_health_at = 0.0

    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self.phase not in {"idle", "error", "stopped"}:
            return
        self.phase = "starting"
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.metrics.event("INFO", "engine", "starting realtime engine")
        try:
            await self._start_redis()
            await self.ch.start()
            await self._bootstrap_all()
            self._start_sessions()
            if self.settings.mode == "demo":
                self.feed = DemoFeed(
                    symbols=self.settings.symbols,
                    intervals=self.settings.intervals,
                    metrics=self.metrics,
                    speed=self.settings.demo_speed,
                    subticks=self.settings.demo_subticks,
                )
            else:
                self.feed = BinanceKlineFeed(
                    ws_base_url=self.settings.ws_base_url,
                    groups=self.settings.stream_groups(),
                    metrics=self.metrics,
                    ping_interval=self.settings.ping_interval,
                    ping_timeout=self.settings.ping_timeout,
                    reconnect_max=self.settings.reconnect_max_seconds,
                )
            await self.feed.start(self.dispatch)
            self._health_task = asyncio.create_task(self._health_loop(), name="health-loop")
            self._loop_lag_task = asyncio.create_task(self._loop_lag_loop(), name="loop-lag")
            self.phase = "ready"
            self.metrics.event(
                "INFO", "engine",
                f"ready · {len(self.settings.symbols)} symbols · "
                f"{len(self.settings.intervals)} intervals · {self.settings.mode.upper()}",
            )
            log.info("realtime engine ready")
        except Exception as exc:
            self.phase = "error"
            self.boot_error = f"{type(exc).__name__}: {exc}"
            self.metrics.event("ERROR", "engine", "startup failed", error=str(exc)[:400])
            log.exception("engine startup failed: %s", exc)
            raise

    async def _start_redis(self, attempts: int = 30) -> None:
        last: Exception | None = None
        for _ in range(attempts):
            try:
                await self.redis.start()
                return
            except Exception as exc:
                last = exc
                await asyncio.sleep(2.0)
        raise RuntimeError(f"Redis unavailable: {last}")

    async def stop(self) -> None:
        self.phase = "stopping"
        if self.feed is not None:
            await self.feed.stop()
            self.feed = None
        tasks = [s.task for s in self.sessions.values() if s.task is not None]
        for session in self.sessions.values():
            session.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.clear()
        if self._health_task is not None:
            self._health_task.cancel()
            self._health_task = None
        if self._loop_lag_task is not None:
            self._loop_lag_task.cancel()
            self._loop_lag_task = None
        await self.ai.close()
        await self.ch.stop()
        await self.rest.close()
        await self.redis.stop()
        self.wal.close()
        self.phase = "stopped"
        self.metrics.event("INFO", "engine", "stopped cleanly")

    # ------------------------------------------------------------------
    async def _bootstrap_all(self) -> None:
        sem = asyncio.Semaphore(max(1, self.settings.bootstrap_concurrency))
        self.metrics.event("INFO", "bootstrap", f"loading {self.boot_total} streams")

        async def one(symbol: str, interval: str) -> None:
            async with sem:
                try:
                    await self._bootstrap_stream(symbol, interval)
                except Exception as exc:
                    self.metrics.event(
                        "ERROR", "bootstrap", f"{symbol} {interval} bootstrap failed",
                        error=str(exc)[:300],
                    )
                    log.exception("bootstrap failed for %s %s", symbol, interval)
                finally:
                    self.boot_done += 1

        await asyncio.gather(*(one(s, i) for s, i in self.settings.streams))

    async def _bootstrap_stream(self, symbol: str, interval: str) -> None:
        limit = self.settings.bootstrap_limit(interval)
        step = interval_ms(interval)
        bars: list[Kline] = []
        source = "none"
        try:
            existing = await self.ch.load_recent(symbol, interval, limit)
        except Exception:
            existing = []
        if existing:
            age = utc_ms() - existing[-1].close_time
            enough = len(existing) >= min(limit, 600 if interval == self.settings.strategy_interval else 100)
            # Reject future-dated cache left by an accelerated demo run. Live
            # bars always have age >= 0; negative age is a stale simulation.
            if 0 <= age <= step * 3 and enough:
                bars = existing
                source = "clickhouse"

        if not bars and self.settings.mode == "live":
            try:
                bars = await self.rest.fetch_last(symbol, interval, limit)
                source = "rest"
            except Exception as exc:
                self.metrics.event(
                    "WARN", "bootstrap",
                    f"{symbol} {interval} REST bootstrap failed; waiting for live bars",
                    error=str(exc)[:240],
                )
                bars = []

        if not bars and self.settings.mode == "demo":
            if self.settings.demo_seed_source == "binance":
                try:
                    bars = await self.rest.fetch_last(symbol, interval, limit)
                    source = "rest-demo"
                except Exception as exc:
                    self.metrics.event(
                        "WARN", "bootstrap",
                        f"{symbol} {interval} demo REST seed failed; using synthetic",
                        error=str(exc)[:200],
                    )
            if not bars:
                bars = generate_history(symbol, interval, limit, source="demo")
                source = "synthetic"

        bars = [b for b in bars if b.is_closed]
        bars.sort(key=lambda b: b.open_time)
        dedup = {b.open_time: b for b in bars}
        bars = [dedup[k] for k in sorted(dedup)]
        if len(bars) > limit:
            bars = bars[-limit:]

        session = StreamSession(self, symbol, interval)
        self.sessions[(symbol, interval)] = session
        if bars:
            session.detector.last_closed_open_time = bars[-1].open_time
            for bar in bars:
                await self.ch.enqueue(bar)
            await self.redis.seed_closed(bars[-max(200, min(800, limit)):])
            if interval == self.settings.strategy_interval:
                await self.runner.bootstrap(symbol, bars)
        self.metrics.event(
            "INFO", "bootstrap",
            f"{symbol} {interval}: {len(bars)} bars from {source}",
        )

    def _start_sessions(self) -> None:
        for session in self.sessions.values():
            session.start()

    async def dispatch(self, bar: Kline) -> None:
        session = self.sessions.get((bar.symbol, bar.interval))
        if session is None:
            self.metrics.inc("orphan_message")
            return
        session.push(bar)

    # ------------------------------------------------------------------
    async def _process_closed(self, bar: Kline, *, recovered: bool) -> None:
        start = time.perf_counter()
        redis_ms = await self.redis.store_closed(bar, recovered=recovered)
        await self.ch.enqueue(bar)
        now = utc_ms()
        close_age = max(0, now - bar.close_time)
        self.metrics.inc("closed_bar")
        self.metrics.inc("redis_update")
        self.metrics.observe("redis_ms", redis_ms)
        # parse/receive -> durable enqueue. This is the low-latency hot path;
        # close_age_ms separately exposes exchange-close-to-processing skew.
        self.metrics.observe("pipeline_ms", (time.perf_counter() - start) * 1000.0)
        self.metrics.observe("close_age_ms", close_age)
        self.metrics.set("last_closed", now)
        if recovered:
            self.runner.append_recovered(bar)
            return

        await self.broker.on_bar_closed(
            bar.symbol, high=bar.high, low=bar.low, close=bar.close,
        )
        closes = self.broker.increment_bar(bar.symbol, bar.open_time, bar.close)
        for symbol in closes:
            await self.broker.close_time_stop(symbol, bar.close)
        await self.runner.on_closed(bar)
        if self.runner.last_eval_ms.get(bar.symbol) == bar.open_time:
            elapsed = self.runner.last_eval_duration_ms.get(bar.symbol)
            if elapsed is not None:
                await self.observability.engine_timing(
                    "ict", "ict_trader-0.1.0", bar.symbol, bar.interval,
                    bar.open_time, elapsed,
                    bar_count=self.runner.eval_window,
                )

    async def _on_signal(self, signal: TradeSignal) -> None:
        result = await self.broker.execute(signal, signal.entry, bar_time=signal.bar_time)
        if result.accepted:
            self.runner.update_signal(
                signal.id, status="EXECUTED", executed=True,
                order_id=result.position_id,
                reason=f"fill {result.fill:.6g}",
            )
        else:
            self.runner.update_signal(signal.id, status="REJECTED", executed=False, reason=result.reason)
            self.metrics.inc("signal_rejected")
        await self._persist_signal(signal)

    async def _on_trade(self, trade) -> None:
        self.runner.update_signal(trade.signal_id, status="CLOSED")
        await self.ch.insert_trade(trade.for_clickhouse())

    async def _persist_signal(self, signal: TradeSignal) -> None:
        data = signal.to_public()
        data.update({
            "bar_time_dt": signal.bar_time_dt,
            "created_at_dt": signal.created_at_dt,
        })
        await self.ch.insert_signal(data)

    # ------------------------------------------------------------------
    async def _loop_lag_loop(self) -> None:
        interval = 0.25
        loop = asyncio.get_running_loop()
        while True:
            try:
                started = loop.time()
                await asyncio.sleep(interval)
                lag_ms = max(0.0, (loop.time() - started - interval) * 1000.0)
                await self.observability.loop_lag(lag_ms)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.metrics.event("WARN", "loop_lag", "loop-lag probe failed", error=str(exc)[:200])
                await asyncio.sleep(1.0)

    async def _health_loop(self) -> None:
        tick = 0
        while True:
            try:
                started = time.perf_counter()
                redis_ok, redis_ms = await self.redis.ping()
                ch_ok, ch_ms = await self.ch.ping()
                wal_stats = await asyncio.to_thread(self.wal.stats)
                if tick % 6 == 0:
                    self._redis_info = await self.redis.info()
                    self._ch_rows = await self.ch.count()
                feed_status = self.feed.status() if self.feed is not None else {}
                self._health = {
                    "redis": {"ok": redis_ok, "latency_ms": round(redis_ms, 2), **self._redis_info},
                    "clickhouse": {
                        "ok": ch_ok, "latency_ms": round(ch_ms, 2),
                        "rows": self._ch_rows, "queue": self.ch.queue_size,
                        "analytics_queue": self.ch.analytics_queue_size,
                        "last_error": self.ch.last_error or self.ch.analytics.last_error,
                    },
                    "wal": wal_stats,
                    "observability": self.observability.status(),
                    "feed": feed_status,
                    "sessions": {
                        f"{s}:{i}": {
                            "last_closed": session.detector.last_closed_open_time,
                            "gaps": session.detector.gaps,
                            "recovered_bars": session.detector.recovered_bars,
                            "duplicates": session.detector.duplicates,
                            "queue": session.queue.qsize(),
                            "closed": session.closed_count,
                            "updates": session.live_count,
                        }
                        for (s, i), session in self.sessions.items()
                    },
                }
                self.metrics.set("health_ok", bool(redis_ok and ch_ok))
                self.metrics.set("redis_memory", self._redis_info.get("used_memory", 0))
                self.metrics.set("ch_rows", self._ch_rows)
                self.metrics.set("wal_pending", wal_stats.get("pending", 0))
                self._last_health_at = time.time()
                tick += 1
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.metrics.event("WARN", "health", "health probe failed", error=str(exc)[:200])
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        broker = self.broker.snapshot()
        metrics = self.metrics.snapshot()
        signals = self.runner.visible_signals()
        last_update = 0
        if self.feed is not None:
            last_update = int(getattr(self.feed, "last_message_ms", 0) or 0)
        return {
            "mode": self.settings.mode,
            "phase": self.phase,
            "boot_error": self.boot_error,
            "boot": {"done": self.boot_done, "total": self.boot_total},
            "started_at": self.started_at,
            "server_time": utc_ms(),
            "venue": "BINANCE SPOT" if self.settings.market == "spot" else "BINANCE USD-M",
            "trading_mode": "PAPER",
            "config": self.settings.public_dict(),
            "health": self._health,
            "last_update": last_update,
            "strategy": self.runner.snapshot(),
            "signals": [s.to_public() for s in signals[:30]],
            "watchlist": self.runner.watchlist(),
            "account": broker["account"],
            "positions": broker["positions"],
            "trades": broker["trades"],
            "equity_curve": broker["equity_curve"],
            "stats": broker["stats"],
            "metrics": metrics,
            "ai": self.ai.status(),
            "selected": self.settings.symbols[0] if self.settings.symbols else "",
        }

    async def flatten_all(self) -> int:
        """Close every paper position at the latest mark (local UI control)."""
        closed = 0
        for position in list(self.broker.positions.values()):
            await self.broker._close(position, position.last_price, "manual")
            closed += 1
        if closed:
            self.metrics.event("WARN", "risk", f"manual flatten closed {closed} paper position(s)")
        return closed

    async def klines(self, symbol: str, interval: str, limit: int = 240,
                     end_time: int | None = None) -> list[dict[str, Any]]:
        limit = max(20, min(int(limit), 2000))
        symbol = symbol.upper()
        if interval == self.settings.strategy_interval:
            rows = self.runner.public_klines(symbol, limit, end_time)
            if rows:
                return rows
        try:
            bars = await self.redis.get_closed(symbol, interval, limit)
            if bars:
                return [b.to_public() for b in bars]
        except Exception:
            pass
        try:
            bars = await self.ch.load_recent(symbol, interval, limit)
            return [b.to_public() for b in bars]
        except Exception:
            return []
