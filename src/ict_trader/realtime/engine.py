"""Realtime orchestration: feed -> per-stream queue -> gap recovery -> stores.

The feed callback only enqueues.  Every ``(symbol, interval)`` has an
independent worker, so a REST backfill for BTC 15m cannot stall ETH 1m.  A
worker processes closed bars in order, detects gaps, pauses its own stream,
backfills, replays and only then resumes normal processing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import pandas as pd
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .analysis import AnalysisContext, CircuitBreaker, EngineRegistry, EngineSignal
from .analysis.engines import (
    ChanConfig, ChanEngine, ICTEngine, IchimokuConfig, IchimokuEngine,
    WyckoffConfig, WyckoffEngine,
)
from ..concepts import ICTConfig
from ..setup import SetupConfig
from .ai import DeepSeekReviewer
from .broker import PaperBroker
from .config import Settings
from .demo import DemoFeed, generate_history
from .feed import BinanceKlineFeed
from .gap_detector import GapDetector
from .metrics import Metrics
from .models import Kline, interval_ms, ms_to_datetime, utc_ms, validate_ohlcv
from .rest_backfill import BackfillError, RestBackfiller
from .storage import ClickHouseStore, RedisStore, Wal
from .storage.observability import Observability
from .strategy_runner import ICTStrategyRunner, TradeSignal
from .setups import Setup, SetupManager
from .universe import fetch_top_usdt_symbols

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
        # Production mode must fail closed: synthetic data can never enter the
        # live chart, Redis latest cache, or strategy pipeline.
        if self.engine.settings.mode == "live" and bar.source == "demo":
            self.engine.metrics.inc("rejected_demo_bar")
            return
        if not validate_ohlcv(bar):
            self.engine.metrics.inc("invalid_live_bar")
            return
        self.live_count += 1
        self.last_update_time = utc_ms()
        self.engine.latest_bars[(bar.symbol, bar.interval)] = bar
        await self.engine.setups.on_price(
            bar.symbol, bar.close, interval=bar.interval,
            high=bar.high, low=bar.low, now=utc_ms(),
        )
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
        self.registry = EngineRegistry(CircuitBreaker())
        self.registry.register(IchimokuEngine(IchimokuConfig()))
        self.registry.register(WyckoffEngine(WyckoffConfig()))
        self.registry.register(ChanEngine(ChanConfig(freq=settings.strategy_interval)))
        self.registry.register(ICTEngine(
            setup_config=SetupConfig(rr=settings.rr, allow_short=True),
            ict_config=ICTConfig(),
        ))
        self.registry_signals: deque[EngineSignal] = deque(maxlen=200)
        self.auxiliary: dict[str, dict[str, Any]] = {}
        self.registry_runs: deque[dict[str, Any]] = deque(maxlen=100)
        self._registry_sem = asyncio.Semaphore(1)
        self._registry_tasks: set[asyncio.Task] = set()
        self._registry_keys: set[tuple[str, str, int]] = set()
        self.broker = PaperBroker(settings, self.metrics)
        self.ai = DeepSeekReviewer(settings, self.metrics)
        self.observability = Observability(self.ch.analytics, self.metrics)
        self.ai_decisions: deque[dict[str, Any]] = deque(maxlen=50)
        self._ai_sem = asyncio.Semaphore(1)
        self._ai_last: dict[str, float] = {}
        self._ai_tasks: set[asyncio.Task] = set()
        self.setups = SetupManager(
            self.ch.insert_setup, self.ch.insert_tp_sl_event,
            on_created=self._on_setup_created,
            on_entry=self._on_setup_entry,
            settings=settings,
        )
        self.runner.on_signal = self._on_signal
        self.broker.on_trade = self._on_trade
        self.sessions: dict[tuple[str, str], StreamSession] = {}
        self.latest_bars: dict[tuple[str, str], Kline] = {}
        self.feed: BinanceKlineFeed | DemoFeed | None = None
        self.phase = "idle"
        self.boot_error = ""
        self.boot_total = len(settings.streams)
        self.boot_done = 0
        self.universe: list[dict[str, Any]] = []
        self.started_at = ""
        self._health_task: asyncio.Task | None = None
        self._loop_lag_task: asyncio.Task | None = None
        self._ai_scan_task: asyncio.Task | None = None
        self._analysis_scan_task: asyncio.Task | None = None
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
            await self._select_universe()
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
            self._analysis_scan_task = asyncio.create_task(
                self._analysis_scan_loop(), name="analysis-scan")
            if self.settings.ai_auto and self.ai.available:
                self._ai_scan_task = asyncio.create_task(self._ai_scan_loop(), name="ai-scan")
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
        if self._ai_scan_task is not None:
            self._ai_scan_task.cancel()
            self._ai_scan_task = None
        if self._analysis_scan_task is not None:
            self._analysis_scan_task.cancel()
            self._analysis_scan_task = None
        await self.ai.close()
        await self.ch.stop()
        await self.rest.close()
        await self.redis.stop()
        self.wal.close()
        self.phase = "stopped"
        self.metrics.event("INFO", "engine", "stopped cleanly")

    # ------------------------------------------------------------------
    async def _select_universe(self) -> None:
        if self.settings.symbol_mode != "top10":
            return
        try:
            rows = await fetch_top_usdt_symbols(
                self.settings.rest_base_url, self.settings.top_symbols_count,
                timeout=30.0,
            )
            if len(rows) < self.settings.top_symbols_count:
                raise RuntimeError(f"only {len(rows)} eligible USDT symbols")
            self.universe = rows
            self.settings.symbols = [r["symbol"] for r in rows]
            # The runner allocates per-symbol histories/locks in __init__;
            # rebuild it after the volume universe changes the symbol set.
            self.runner = ICTStrategyRunner(self.settings, self.metrics)
            self.runner.on_signal = self._on_signal
            self.boot_total = len(self.settings.streams)
            self.metrics.event(
                "INFO", "universe",
                f"top {len(rows)} USDT by 24h quote volume selected",
            )
            log.info("volume universe: %s", self.settings.symbols)
        except Exception as exc:
            self.metrics.event(
                "WARN", "universe",
                "top10 volume ranking failed; keeping static SYMBOLS",
                error=str(exc)[:300],
            )
            log.warning("volume universe failed, keeping static symbols: %s", exc)

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
            self.latest_bars[(symbol, interval)] = bars[-1]
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
    def _schedule_registry(self, bar: Kline) -> None:
        if bar.interval != self.settings.strategy_interval:
            return
        key = (bar.symbol, bar.interval, bar.open_time)
        if key in self._registry_keys:
            return
        self._registry_keys.add(key)

        async def runner() -> None:
            try:
                async with self._registry_sem:
                    await self._run_registry(bar)
            finally:
                self._registry_keys.discard(key)
                self._registry_tasks.discard(task)

        task = asyncio.create_task(runner(), name=f"registry-{bar.symbol}-{bar.open_time}")
        self._registry_tasks.add(task)

    async def _analysis_scan_loop(self) -> None:
        """Recompute engines every N seconds on the latest CLOSED bars."""
        while True:
            try:
                await asyncio.sleep(max(60.0, self.settings.analysis_scan_seconds))
                for symbol in list(self.settings.symbols):
                    history = self.runner.frame(symbol, 1)
                    if not history:
                        continue
                    bar = history[-1]
                    if not bar.is_closed:
                        continue
                    async with self._registry_sem:
                        await self._run_registry(bar)
                    self.metrics.event(
                        "INFO", "analysis_scan",
                        f"{symbol} {bar.interval} recomputed on closed bar",
                        symbol=symbol,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("analysis scan loop failed: %s", exc)
                await self.observability.error(
                    "engine", "analysis_scan", "engine",
                    "analysis scan loop failed", exc=exc,
                )

    async def _ai_scan_loop(self) -> None:
        while True:
            try:
                interval = self.settings.ai_scan_seconds
                if self.settings.ai_market_scan:
                    interval = min(interval, self.settings.ai_market_scan_seconds)
                await asyncio.sleep(max(30.0, interval))
                for setup in self.setups.active():
                    if not setup.ai_decision_id:
                        await self._on_setup_created(setup)
                if self.settings.ai_market_scan:
                    await self._ai_market_scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("AI scan loop failed: %s", exc)
                await self.observability.error(
                    "ai", "scan_loop", "engine", "AI scan loop failed", exc=exc,
                )

    async def _on_setup_entry(self, setup) -> bool | None:
        """Send an accepted registry setup to the paper broker.

        None means: a same-symbol paper position exists; keep the Setup pending
        instead of invalidating it. The SetupManager will retry after that
        position closes.
        """
        positions = self.broker.snapshot().get("positions", [])
        if any((p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None))
               == setup.symbol for p in positions):
            return None
        target = (setup.tp_levels[0] if setup.tp_levels else setup.target)
        signal = TradeSignal(
            id=setup.setup_id, symbol=setup.symbol, interval=setup.interval,
            bar_time=int(setup.engine_signals[0].get("source_kline_open_time")
                         if setup.engine_signals else setup.created_at),
            created_at=setup.created_at, direction=setup.direction,
            entry=setup.entry, stop=setup.stop, target=target,
            rr=setup.rr_for(target), grade=5, quality=setup.confidence,
            status="ARMED", actionable=True, executed=False,
            reason=f"{setup.engine} setup · resonance {setup.resonance_score}/4",
        )
        result = await self.broker.execute(signal, setup.entry, bar_time=signal.bar_time)
        if result.accepted:
            setup.fill_price = float(result.fill)
            await self.setups.persist_setup(setup.setup_row(datetime.now(timezone.utc)))
            self.metrics.event(
                "INFO", "execution",
                f"paper {setup.direction} {setup.symbol} @ {result.fill:.6g}",
                symbol=setup.symbol,
            )
        else:
            self.metrics.event(
                "WARN", "execution",
                f"paper entry rejected {setup.symbol}: {result.reason}",
                symbol=setup.symbol,
            )
        return bool(result.accepted)

    async def _ai_market_scan_once(self) -> None:
        symbols = list(self.settings.symbols)[:max(1, self.settings.ai_market_scan_max_symbols)]
        tasks = []
        for symbol in symbols:
            positions = self.broker.snapshot().get("positions", [])
            if any((p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)) == symbol
                   for p in positions):
                continue
            tasks.append(asyncio.create_task(self._ai_market_review(symbol),
                                             name=f"ai-market-{symbol}"))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ai_market_review(self, symbol: str) -> None:
        if not self.ai.available:
            return
        async with self._ai_sem:
            started = time.perf_counter()
            now = utc_ms()
            try:
                context = await self.ai_context(symbol, market_scan=True)
                bars = self.runner.frame(symbol, 80)
                review = await self.ai.review(
                    self.snapshot(), bars, symbol, context=context,
                )
                status, error = 1, ""
            except Exception as exc:
                review = {"verdict": "reject", "confidence": 0,
                          "headline": "AI market scan unavailable",
                          "summary": str(exc)[:400], "raw_output": ""}
                status, error = 4, f"{type(exc).__name__}: {exc}"
            decision_id = "AI-" + hashlib.sha1(
                f"market|{symbol}|{now}".encode()
            ).hexdigest()[:22]
            plan = review.get("trade_plan") or {}
            direction = str(plan.get("direction") or "long")
            targets = [float(x) for x in (plan.get("targets") or []) if float(x) > 0]
            setup = Setup(
                setup_id="AISET-" + decision_id[4:],
                symbol=symbol, interval=self.settings.strategy_interval,
                direction=direction, status="DETECTED",
                created_at=now, updated_at=now,
                entry_low=float(plan.get("entry_low") or 0),
                entry_high=float(plan.get("entry_high") or plan.get("entry_low") or 0),
                stop=float(plan.get("stop") or 0),
                target=targets[0] if targets else 0.0,
                targets=targets, confidence=float(review.get("confidence", 0) or 0) / 100.0,
                engine="ai_market", engine_version="deepseek-market-v1",
                signal_ids=[], engine_signals=[],
            )
            usage = review.get("usage") or {}
            latency_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))
            input_snapshot = json.dumps({
                "mode": "market_scan", "symbol": symbol, "as_of": now,
                "context": context,
            }, ensure_ascii=False, default=str)
            await self.ch.insert_ai_decision((
                decision_id, setup.setup_id, symbol, setup.interval,
                "deepseek", self.settings.deepseek_model, "market-scan-v1",
                input_snapshot, str(review.get("raw_output", ""))[:20000],
                json.dumps(review, ensure_ascii=False, default=str), status,
                latency_ms, int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
                int(usage.get("total_tokens", 0) or 0), 0.0,
                datetime.fromtimestamp(now / 1000.0, tz=timezone.utc),
                datetime.now(timezone.utc),
            ))
            execution = {"accepted": False, "reason": "AI execution disabled"}
            if (self.settings.ai_execution_enabled
                    and str(review.get("verdict")) == "support" and plan.get("open")):
                execution = await self._execute_ai_plan(setup, review)
                review["execution"] = execution
            self.ai_decisions.append({
                "decision_id": decision_id, "setup_id": setup.setup_id,
                "symbol": symbol, "verdict": review.get("verdict"),
                "confidence": review.get("confidence"),
                "headline": review.get("headline"),
                "summary": review.get("summary"), "status": status,
                "error": error, "latency_ms": latency_ms,
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "created_at": now, "parsed": review, "execution": execution,
                "mode": "market_scan",
            })
            self.metrics.inc("ai_market_review")

    async def _on_setup_created(self, setup) -> None:
        if setup.ai_decision_id:
            return
        if not self.settings.ai_auto or not self.ai.available:
            return
        if setup.confidence < self.settings.ai_auto_min_confidence:
            return
        now = time.monotonic()
        if now - self._ai_last.get(setup.symbol, 0.0) < self.settings.ai_auto_cooldown_seconds:
            return
        self._ai_last[setup.symbol] = now
        task = asyncio.create_task(self._auto_review(setup), name=f"ai-{setup.setup_id}")
        self._ai_tasks.add(task)
        task.add_done_callback(self._ai_tasks.discard)

    async def _execute_ai_plan(self, setup, review: dict[str, Any]) -> dict[str, Any]:
        """Execute a validated support plan as a paper order.

        The AI never touches the exchange; PaperBroker remains the executor.
        """
        plan = review.get("trade_plan") or {}
        if str(review.get("verdict")) != "support" or not plan.get("open"):
            return {"accepted": False, "reason": "no support plan"}
        direction = str(plan.get("direction") or setup.direction)
        stop = float(plan.get("stop") or setup.stop)
        targets = [float(x) for x in (plan.get("targets") or []) if float(x) > 0]
        if not targets:
            targets = [float(setup.target)]
        entry = float(plan.get("entry_low") or setup.entry)
        entry = (entry + float(plan.get("entry_high") or entry)) / 2.0
        live = self.latest_bars.get((setup.symbol, setup.interval))
        if live is not None and live.close > 0:
            entry = float(live.close)  # direct paper open at current mark
        risk = abs(entry - stop)
        if risk <= 0:
            return {"accepted": False, "reason": "zero stop distance"}
        target = targets[0]
        if direction == "long" and not (stop < entry < target):
            return {"accepted": False, "reason": "invalid long price order"}
        if direction == "short" and not (target < entry < stop):
            return {"accepted": False, "reason": "invalid short price order"}
        rr = abs(target - entry) / risk
        if rr < 2.0:
            return {"accepted": False, "reason": f"RR {rr:.2f} below 2.0"}
        signal = TradeSignal(
            id=setup.setup_id, symbol=setup.symbol, interval=setup.interval,
            bar_time=int(setup.engine_signals[0].get("source_kline_open_time")
                         if setup.engine_signals else setup.created_at),
            created_at=utc_ms(), direction=direction,
            entry=entry, stop=stop, target=target, rr=rr,
            grade=5, quality=float(review.get("confidence", 0) or 0) / 100.0,
            status="ARMED", actionable=True, executed=False,
            reason="AI support plan",
        )
        result = await self.broker.execute(signal, entry, bar_time=signal.bar_time)
        if result.accepted:
            setup.fill_price = float(result.fill)
            setup.status = "OPEN"
            setup.opened_at = utc_ms()
            setup.updated_at = setup.opened_at
            await self.setups.persist_setup(setup.setup_row(datetime.now(timezone.utc)))
            self.metrics.event(
                "INFO", "ai_execution",
                f"AI paper {direction} {setup.symbol} @ {result.fill:.6g}",
                symbol=setup.symbol,
            )
        return {"accepted": bool(result.accepted), "reason": result.reason,
                "fill": float(result.fill) if result.accepted else None, "rr": round(rr, 3)}

    async def _auto_review(self, setup) -> None:
        """Async, non-blocking review of a newly created future setup."""
        async with self._ai_sem:
            bars = self.runner.frame(setup.symbol, self.runner.eval_window)
            snapshot = self.snapshot()
            started = time.perf_counter()
            try:
                rich = await self.ai_context(setup.symbol)
                review = await self.ai.review(snapshot, bars, setup.symbol, context=rich)
                status = 1
                error = ""
            except Exception as exc:
                review = {"verdict": "reject", "confidence": 0, "headline": "AI unavailable",
                          "summary": str(exc)[:500], "raw_output": ""}
                status = 4
                error = f"{type(exc).__name__}: {exc}"
            now = utc_ms()
            parsed = json.dumps(review, ensure_ascii=False, default=str)
            input_snapshot = json.dumps({
                "setup": setup.to_public(),
                "mode": self.settings.mode,
                "as_of": now,
                "closed_bars": len(bars),
            }, ensure_ascii=False, default=str)
            decision_id = "AI-" + hashlib.sha1(
                f"{setup.setup_id}|{now}".encode()
            ).hexdigest()[:22]
            usage = review.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0)
            latency_ms = int(max(0.0, (time.perf_counter() - started) * 1000.0))
            await self.ch.insert_ai_decision((
                decision_id, setup.setup_id, setup.symbol, setup.interval,
                "deepseek", self.settings.deepseek_model, "auto-setup-v1",
                input_snapshot, str(review.get("raw_output", ""))[:20000],
                parsed, status, latency_ms, prompt_tokens, completion_tokens,
                total_tokens, 0.0, datetime.fromtimestamp(now / 1000.0, tz=timezone.utc), datetime.now(timezone.utc),
            ))
            setup.ai_decision_id = decision_id
            setup.ai_confidence = float(review.get("confidence", 0) or 0) / 100.0
            setup.ai_decision = str(review.get("verdict", "reject"))
            await self.setups.persist_setup(setup.setup_row(datetime.now(timezone.utc)))
            execution = {"accepted": False, "reason": "AI execution disabled"}
            if self.settings.ai_execution_enabled:
                execution = await self._execute_ai_plan(setup, review)
            review["execution"] = execution
            self.ai_decisions.append({
                "decision_id": decision_id, "setup_id": setup.setup_id,
                "symbol": setup.symbol, "verdict": review.get("verdict"),
                "confidence": review.get("confidence"), "headline": review.get("headline"),
                "summary": review.get("summary"), "status": status, "error": error,
                "latency_ms": latency_ms, "total_tokens": total_tokens,
                "created_at": now, "parsed": review, "execution": execution,
            })
            self.metrics.inc("ai_auto_review")
            self.metrics.event(
                "INFO", "ai",
                f"auto review {review.get('verdict')} for {setup.symbol} "
                f"({review.get('confidence')}%)",
                symbol=setup.symbol,
            )

    async def _run_registry(self, bar: Kline) -> None:
        """Run non-ICT engines in shadow mode on the just-closed real bar."""
        history = self.runner.frame(bar.symbol, self.runner.eval_window, bar.open_time)
        if len(history) < 120:
            return
        frame = pd.DataFrame([b.to_csv_row() for b in history])
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = frame[col].astype(float)
        from .analysis.arrays import build_ict_arrays
        try:
            await self.ch.insert_ict_arrays(build_ict_arrays(frame, bar.symbol, bar.interval))
        except Exception as exc:
            log.warning("ICT arrays persist failed for %s %s: %s", bar.symbol, bar.interval, exc)
        context = AnalysisContext(bar.symbol, bar.interval, utc_ms())
        runs = await self.registry.analyze_all(
            frame, context, timeout_seconds=8.0,
            engine_ids=[x.strip() for x in self.settings.setup_engine_ids.split(",")
                        if x.strip()],
        )
        from .analysis.auxiliary import build_auxiliary_context
        from .analysis.structure import structure_bias
        structure = structure_bias(frame)
        biases = {"chan": "neutral", "ichimoku": "neutral", "wyckoff": "neutral"}
        for run in runs:
            if run.signals:
                biases[run.engine] = run.signals[-1].direction.value
        ema = float(frame["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        htf_bias = "bullish" if float(bar.close) > ema else "bearish"
        for run in runs:
            version = self.registry.get(run.engine).version
            await self.observability.engine_timing(
                run.engine, version, bar.symbol, bar.interval,
                bar.open_time, run.latency_ms,
                bar_count=len(frame),
                status="ok" if run.ok else "error",
                error=run.error,
            )
            self.registry_runs.append({
                "engine": run.engine, "symbol": bar.symbol,
                "source_kline_open_time": bar.open_time,
                "latency_ms": round(run.latency_ms, 1),
                "signals": len(run.signals), "error": run.error,
            })
            if not run.signals:
                continue
            fresh = []
            known = {s.signal_id for s in self.registry_signals}
            for signal in run.signals:
                if signal.signal_id not in known:
                    known.add(signal.signal_id)
                    fresh.append(signal)
            if fresh:
                await self.ch.insert_engine_signals(fresh)
                for signal in fresh:
                    aux = build_auxiliary_context(
                        signal.direction.value, htf_bias=htf_bias,
                        chan_bias=biases["chan"],
                        ichimoku_bias=biases["ichimoku"],
                        wyckoff_bias=biases["wyckoff"],
                    )
                    self.auxiliary[signal.symbol] = aux.to_public()
                    await self.setups.on_signal(
                        signal, resonance=aux, min_rr=self.settings.rr,
                        min_confidence=self.settings.setup_min_confidence,
                        structure_bias=structure["bias"],
                    )
                self.registry_signals.extend(fresh)
                self.metrics.inc("engine_signal", len(fresh))
                self.metrics.event(
                    "INFO", "registry",
                    f"{bar.symbol} {bar.interval} · {len(fresh)} new engine signal(s)",
                )

    async def _process_closed(self, bar: Kline, *, recovered: bool) -> None:
        if self.settings.mode == "live" and bar.source == "demo":
            self.metrics.inc("rejected_demo_bar")
            return
        if not validate_ohlcv(bar):
            self.metrics.inc("invalid_closed_bar")
            return
        start = time.perf_counter()
        self.latest_bars[(bar.symbol, bar.interval)] = bar
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
        await self.setups.on_bar(bar.symbol, interval=bar.interval,
                                 high=bar.high, low=bar.low,
                                 close=bar.close, now=utc_ms())
        if not recovered:
            self._schedule_registry(bar)
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
        await self.setups.on_ict_signal(
            signal, accepted=result.accepted,
            fill=float(getattr(result, "fill", 0.0) or signal.entry),
        )

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
            "watchlist": self.runner.watchlist(self.latest_bars),
            "live_bars": [bar.to_public() for bar in self.latest_bars.values()],
            "engine_signals": [s.to_public() for s in list(self.registry_signals)[-30:]],
            "auxiliary": list(self.auxiliary.values()),
            "setups": [s.to_public() for s in self.setups.all()[:30]],
            "engines": {
                "registry": list(self.registry.names()),
                "breaker": self.registry.breaker.snapshot() if self.registry.breaker else {},
            },
            "account": broker["account"],
            "positions": broker["positions"],
            "trades": broker["trades"],
            "equity_curve": broker["equity_curve"],
            "stats": broker["stats"],
            "metrics": metrics,
            "ai": {**self.ai.status(), "auto": self.settings.ai_auto,
                   "execution_enabled": self.settings.ai_execution_enabled,
                   "market_scan": self.settings.ai_market_scan},
            "ai_decisions": list(self.ai_decisions)[-20:],
            "universe": list(self.universe),
            "selected": self.settings.symbols[0] if self.settings.symbols else "",
        }

    def review(self, days: int = 7) -> dict[str, Any]:
        from .review import build_review
        cutoff = utc_ms() - max(1, int(days)) * 86_400_000
        trades = [t.to_public() for t in self.broker.trades
                  if int(t.exit_time) >= cutoff]
        return build_review(trades, [s.to_public() for s in self.setups.all()])

    async def flatten_all(self) -> int:
        """Close every paper position at the latest mark (local UI control)."""
        closed = 0
        for position in list(self.broker.positions.values()):
            await self.broker._close(position, position.last_price, "manual")
            closed += 1
        if closed:
            self.metrics.event("WARN", "risk", f"manual flatten closed {closed} paper position(s)")
        return closed

    async def ict_arrays(self, symbol: str, interval: str,
                         limit: int = 700) -> list[dict[str, Any]]:
        rows = await self.klines(symbol, interval, max(40, min(limit, 2000)))
        if not rows:
            return []
        frame = pd.DataFrame([{
            "date": datetime.fromtimestamp(int(r["open_time"]) / 1000.0, tz=timezone.utc),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
        } for r in rows])
        from .analysis.arrays import build_ict_arrays
        arrays = build_ict_arrays(frame, symbol, interval)
        await self.ch.insert_ict_arrays(arrays)
        return [a.to_public() for a in arrays]

    async def chart_overlays(self, symbol: str, interval: str,
                             limit: int = 700) -> dict[str, Any]:
        rows = await self.klines(symbol, interval, max(40, min(limit, 2000)))
        if not rows:
            return {"rectangles": [], "source": "backend"}
        frame = pd.DataFrame([{
            "date": datetime.fromtimestamp(int(r["open_time"]) / 1000.0, tz=timezone.utc),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
        } for r in rows])
        from .overlays import build_ict_overlays, overlays_public
        return overlays_public(build_ict_overlays(frame))

    async def ai_context(self, symbol: str, *,
                         market_scan: bool = False) -> dict[str, Any]:
        """Compact multi-timeframe context for the AI reviewer.

        ICT is primary; Ichimoku/Wyckoff/Chan are context, never execution.
        """
        from .analysis.arrays import build_ict_arrays
        from .overlays import build_ict_overlays, overlays_public

        out: dict[str, Any] = {"symbol": symbol.upper(), "frames": {},
                               "market_scan": market_scan}
        limits = {"15m": 80, "1h": 48, "4h": 32, "1d": 24}
        frames: dict[str, pd.DataFrame] = {}
        for iv, limit in limits.items():
            try:
                rows = await self.klines(symbol, iv, limit, include_live=True)
            except Exception:
                rows = []
            compact = [{
                "t": ms_to_datetime(int(r["open_time"])).isoformat(),
                "o": r["open"], "h": r["high"], "l": r["low"],
                "c": r["close"], "v": r.get("volume", 0),
            } for r in rows]
            out["frames"][iv] = {
                "bar_count": len(compact),
                "bars": compact[-min(40, len(compact)):],
                "last": compact[-1] if compact else None,
            }
            if rows:
                frames[iv] = pd.DataFrame([{
                    "date": datetime.fromtimestamp(int(r["open_time"]) / 1000.0, tz=timezone.utc),
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                } for r in rows])

        try:
            arrays = build_ict_arrays(frames["15m"], symbol, "15m") if "15m" in frames else []
            out["ict"] = {
                "primary": True,
                "arrays": [a.to_public() for a in arrays[:20]],
                "active_count": sum(1 for a in arrays if a.is_active),
            }
            if "15m" in frames:
                out["ict"]["overlays"] = overlays_public(build_ict_overlays(frames["15m"]))["rectangles"][:10]
        except Exception as exc:
            out["ict"] = {"primary": True, "error": str(exc)[:200]}

        try:
            chan = await self.chan_structure(symbol, "15m", 700)
            out["chan"] = {
                "bi_count": chan.get("bi_count", 0),
                "seg_count": chan.get("seg_count", 0),
                "zs_count": chan.get("zs_count", 0),
                "bsp_count": chan.get("bsp_count", 0),
                "bis": chan.get("bis", [])[-12:],
                "segs": chan.get("segs", [])[-6:],
                "zss": chan.get("zss", [])[-4:],
                "bsps": chan.get("bsps", [])[-8:],
                "reasons": chan.get("reasons", [])[-8:],
            }
        except Exception as exc:
            out["chan"] = {"error": str(exc)[:200]}

        biases = {}
        for engine in ("chan", "ichimoku", "wyckoff"):
            latest = [s for s in self.registry_signals
                      if s.symbol == symbol and s.engine == engine]
            biases[engine] = latest[-1].direction.value if latest else "neutral"
        htf_bias = "neutral"
        if "4h" in frames:
            f4 = frames["4h"]
            ema = float(f4["close"].ewm(span=50, adjust=False).mean().iloc[-1])
            htf_bias = "bullish" if float(f4["close"].iloc[-1]) > ema else "bearish"
        out["auxiliary"] = {
            "resonance": self.auxiliary.get(symbol),
            "biases": {"htf": htf_bias, **biases},
            "ichimoku": [s.to_public() for s in self.registry_signals
                         if s.symbol == symbol and s.engine == "ichimoku"][-3:],
            "wyckoff": [s.to_public() for s in self.registry_signals
                        if s.symbol == symbol and s.engine == "wyckoff"][-3:],
            "note": "auxiliary engines are context only; empty list means no current signal, not missing bars",
        }
        out["active_setup"] = next(
            (s.to_public() for s in self.setups.all()
             if s.symbol == symbol and s.status in {"DETECTED", "OPEN"}), None)
        out["positions"] = [p for p in self.broker.snapshot().get("positions", [])
                            if p.get("symbol") == symbol]
        out["rules"] = {
            "ict_primary": True,
            "market_scan": market_scan,
            "allow_trade_plan_without_setup": market_scan,
            "auxiliary": "context only; do not overturn ICT without explicit conflict",
            "execution": "paper only; AI cannot place orders",
        }
        return out

    async def chan_structure(self, symbol: str, interval: str,
                             limit: int = 700) -> dict[str, Any]:
        """Return real chan.py structure for the chart overlay."""
        rows = await self.klines(symbol, interval, max(120, min(limit, 2000)))
        if not rows:
            return {"bi_count": 0, "seg_count": 0, "zs_count": 0,
                    "bis": [], "segs": [], "zss": [], "bsps": [],
                    "divergences": []}
        frame = pd.DataFrame([{
            "date": datetime.fromtimestamp(int(r["open_time"]) / 1000.0, tz=timezone.utc),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r.get("volume", 0.0)),
        } for r in rows])
        from ..chanlun import analyze_chanlun
        result = await asyncio.to_thread(analyze_chanlun, frame, symbol.upper(), interval)
        return result.to_public()

    async def klines(self, symbol: str, interval: str, limit: int = 240,
                     end_time: int | None = None,
                     include_live: bool = False) -> list[dict[str, Any]]:
        limit = max(20, min(int(limit), 2000))
        symbol = symbol.upper()
        if interval == self.settings.strategy_interval:
            rows = self.runner.public_klines(symbol, limit, end_time)
            rows = self._with_live(symbol, interval, rows, limit, end_time, include_live)
            if rows:
                return rows
        try:
            bars = await self.redis.get_closed(symbol, interval, limit)
            if bars:
                rows = [b.to_public() for b in bars]
                return self._with_live(symbol, interval, rows, limit, end_time, include_live)
        except Exception:
            pass
        try:
            bars = await self.ch.load_recent(symbol, interval, limit)
            rows = [b.to_public() for b in bars]
            return self._with_live(symbol, interval, rows, limit, end_time, include_live)
        except Exception:
            return []

    def _with_live(self, symbol: str, interval: str, rows: list[dict[str, Any]],
                   limit: int, end_time: int | None,
                   include_live: bool) -> list[dict[str, Any]]:
        if not include_live or end_time is not None:
            return rows
        live = self.latest_bars.get((symbol, interval))
        if live is None:
            return rows
        if rows and int(live.open_time) == int(rows[-1]["open_time"]):
            rows[-1] = live.to_public()
        elif not rows or int(live.open_time) > int(rows[-1]["open_time"]):
            rows.append(live.to_public())
            rows = rows[-limit:]
        return rows
