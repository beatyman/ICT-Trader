"""Engine registry and execution helper.

The registry itself never performs I/O.  The async helper runs engines in
worker threads and enforces per-engine timeouts, so a slow Chanlun/Wyckoff
implementation cannot block the feed loop or another engine.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .circuit import CircuitBreaker
from .contracts import AnalysisContext, EngineSignal

log = logging.getLogger(__name__)


@runtime_checkable
class AnalysisEngine(Protocol):
    engine_id: str
    version: str
    warmup_bars: int

    def analyze(self, window: Any, context: AnalysisContext) -> list[EngineSignal]:
        """Analyze a closed-bar window and return immutable signals."""


@dataclass(slots=True)
class EngineRun:
    engine: str
    signals: list[EngineSignal]
    latency_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class EngineRegistry:
    def __init__(self, breaker: CircuitBreaker | None = None) -> None:
        self._engines: dict[str, AnalysisEngine] = {}
        self.breaker = breaker

    def register(self, engine: AnalysisEngine) -> None:
        if not isinstance(engine, AnalysisEngine):
            raise TypeError("engine does not implement AnalysisEngine")
        engine_id = engine.engine_id.strip().lower()
        if not engine_id:
            raise ValueError("engine_id is required")
        if engine_id in self._engines:
            raise ValueError(f"engine already registered: {engine_id}")
        if engine.warmup_bars < 0:
            raise ValueError("warmup_bars must be >= 0")
        self._engines[engine_id] = engine

    def get(self, engine_id: str) -> AnalysisEngine:
        key = engine_id.strip().lower()
        try:
            return self._engines[key]
        except KeyError as exc:
            raise KeyError(f"unknown engine: {engine_id}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._engines))

    def __len__(self) -> int:
        return len(self._engines)

    def run(self, engine_id: str, window: Any,
            context: AnalysisContext) -> EngineRun:
        engine = self.get(engine_id)
        started = time.perf_counter()
        try:
            signals = engine.analyze(window, context)
            if not isinstance(signals, list) or not all(
                isinstance(s, EngineSignal) for s in signals
            ):
                raise TypeError(f"{engine_id} returned a non-EngineSignal result")
            return EngineRun(
                engine=engine_id, signals=signals,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:
            log.exception("engine %s failed", engine_id)
            return EngineRun(
                engine=engine_id, signals=[], error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

    async def analyze_all(self, window: Any, context: AnalysisContext, *,
                          timeout_seconds: float = 8.0,
                          engine_ids: list[str] | None = None) -> list[EngineRun]:
        """Run engines concurrently in threads with independent timeouts."""
        ids = [i.lower() for i in (engine_ids or self.names())]

        async def one(engine_id: str) -> EngineRun:
            started = time.perf_counter()
            if self.breaker is not None and not self.breaker.allow(engine_id):
                return EngineRun(
                    engine=engine_id, signals=[], latency_ms=0.0,
                    error="circuit breaker open",
                )
            try:
                run = await asyncio.wait_for(
                    asyncio.to_thread(self.run, engine_id, window, context),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                run = EngineRun(
                    engine=engine_id, signals=[],
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    error=f"timeout after {timeout_seconds:.1f}s",
                )
            except Exception as exc:
                run = EngineRun(
                    engine=engine_id, signals=[],
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if self.breaker is not None:
                if run.ok:
                    self.breaker.success(engine_id)
                else:
                    self.breaker.failure(engine_id)
            return run

        return await asyncio.gather(*(one(i) for i in ids))
