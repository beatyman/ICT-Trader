"""Structured error and engine-performance recording.

The application reports through this service; it never writes to ClickHouse
directly. Rows go through the analytics batch writer, so an exception path
cannot issue a synchronous single-row INSERT.
"""
from __future__ import annotations

import hashlib
import logging
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson

from ..analysis.contracts import AnalysisContext
from ..analysis.registry import EngineRun
from ..metrics import Metrics
from ..models import ms_to_datetime, utc_ms

log = logging.getLogger(__name__)

ERROR_COLUMNS = [
    "error_id", "category", "subtype", "symbol", "interval", "module",
    "error_message", "stack_trace", "context", "occurred_at", "severity",
    "resolved_at", "version", "ingested_at",
]
PERF_COLUMNS = [
    "sample_id", "engine", "engine_version", "symbol", "interval",
    "source_kline_open_time", "latency_ms", "cpu_ms", "memory_bytes",
    "bar_count", "status", "error_type", "occurred_at", "ingested_at",
]


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    category: str
    subtype: str
    module: str
    message: str
    severity: str = "ERROR"
    symbol: str = ""
    interval: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    stack_trace: str = ""
    occurred_at: int = 0
    version: str = "ict-trader-0.1.0"

    def __post_init__(self) -> None:
        if self.occurred_at <= 0:
            object.__setattr__(self, "occurred_at", utc_ms())
        if self.severity not in {"INFO", "WARN", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid severity: {self.severity}")

    @property
    def error_id(self) -> str:
        raw = f"{self.module}|{self.category}|{self.subtype}|{self.occurred_at}|{self.message[:200]}"
        return "ERR-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    def to_row(self) -> tuple:
        return (
            self.error_id,
            self.category,
            self.subtype,
            self.symbol,
            self.interval,
            self.module,
            self.message[:4000],
            self.stack_trace[:12000],
            orjson.dumps(self.context, default=str).decode()[:20000],
            ms_to_datetime(self.occurred_at),
            self.severity,
            None,
            self.version,
            datetime.now(timezone.utc),
        )


class Observability:
    def __init__(self, analytics, metrics: Metrics, *, version: str = "ict-trader-0.1.0") -> None:
        self.analytics = analytics
        self.metrics = metrics
        self.version = version
        self.errors = 0
        self.engine_samples = 0
        self.last_error = ""
        self._last_loop_alert = 0.0

    async def error(self, category: str, subtype: str, module: str,
                    message: str, *, severity: str = "ERROR",
                    symbol: str = "", interval: str = "",
                    context: dict[str, Any] | None = None,
                    exc: BaseException | None = None) -> ErrorEvent:
        stack = ""
        if exc is not None:
            stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            message = f"{message}: {type(exc).__name__}: {exc}"
        event = ErrorEvent(
            category=category, subtype=subtype, module=module, message=message,
            severity=severity, symbol=symbol, interval=interval,
            context=context or {}, stack_trace=stack, version=self.version,
        )
        self.errors += 1
        self.metrics.inc(f"error_{category}")
        self.metrics.event(severity, module, message, context=context or {})
        try:
            await self.analytics.enqueue("error_events", ERROR_COLUMNS, [event.to_row()])
        except Exception as write_exc:
            self.last_error = str(write_exc)
            log.error("failed to enqueue error_event: %s", write_exc)
        return event

    async def engine_timing(self, engine: str, engine_version: str, symbol: str,
                            interval: str, source_kline_open_time: int,
                            latency_ms: float, *, bar_count: int = 0,
                            status: str = "ok", error: str = "") -> None:
        occurred = utc_ms()
        raw = f"{engine}|{symbol}|{interval}|{source_kline_open_time}|{occurred}"
        sample_id = "PERF-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        self.engine_samples += 1
        self.metrics.observe("engine_latency_ms", latency_ms)
        row = (
            sample_id, engine, engine_version, symbol, interval,
            ms_to_datetime(source_kline_open_time), float(latency_ms),
            0.0, 0, int(bar_count), status, error[:400],
            ms_to_datetime(occurred), datetime.now(timezone.utc),
        )
        try:
            await self.analytics.enqueue("engine_performance", PERF_COLUMNS, [row])
        except Exception as exc:
            self.last_error = str(exc)
            log.error("failed to enqueue engine_performance: %s", exc)

    async def engine_run(self, run: EngineRun, context: AnalysisContext,
                         *, bar_count: int = 0, cpu_ms: float = 0.0,
                         memory_bytes: int = 0) -> None:
        occurred = utc_ms()
        raw = f"{run.engine}|{context.symbol}|{context.interval}|{context.as_of_ms}|{occurred}"
        sample_id = "PERF-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        row = (
            sample_id,
            run.engine,
            "unknown",
            context.symbol,
            context.interval,
            ms_to_datetime(context.as_of_ms),
            float(run.latency_ms),
            float(cpu_ms),
            int(memory_bytes),
            int(bar_count),
            "ok" if run.ok else "error",
            run.error[:400],
            ms_to_datetime(occurred),
            datetime.now(timezone.utc),
        )
        self.engine_samples += 1
        self.metrics.observe("engine_latency_ms", run.latency_ms)
        try:
            await self.analytics.enqueue("engine_performance", PERF_COLUMNS, [row])
        except Exception as write_exc:
            self.last_error = str(write_exc)
            log.error("failed to enqueue engine_performance: %s", write_exc)

    async def loop_lag(self, lag_ms: float, *, threshold_ms: float = 200.0) -> None:
        self.metrics.observe("loop_lag_ms", max(0.0, lag_ms))
        self.metrics.set("loop_lag_ms", round(max(0.0, lag_ms), 2))
        now = time.monotonic()
        if lag_ms >= threshold_ms and now - self._last_loop_alert > 30.0:
            self._last_loop_alert = now
            await self.error(
                "runtime", "loop_lag", "event_loop",
                f"event loop lag {lag_ms:.1f}ms >= {threshold_ms:.1f}ms",
                severity="WARN", context={"lag_ms": round(lag_ms, 2)},
            )

    def status(self) -> dict[str, Any]:
        return {
            "errors": self.errors,
            "engine_samples": self.engine_samples,
            "last_error": self.last_error,
        }
