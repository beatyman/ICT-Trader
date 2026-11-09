from __future__ import annotations

import asyncio
from datetime import datetime

from ict_trader.realtime.analysis import (
    AnalysisContext, CircuitBreaker, EngineRegistry, EngineRun,
)
from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.storage.observability import ErrorEvent, Observability


class FakeAnalytics:
    def __init__(self):
        self.items = []

    async def enqueue(self, table, columns, rows):
        self.items.append((table, columns, rows))
        return len(rows)


def ctx() -> AnalysisContext:
    return AnalysisContext("BTCUSDT", "15m", 1_700_000_000_000)


def test_error_event_row_shape():
    event = ErrorEvent("runtime", "worker", "engine", "boom", symbol="BTCUSDT",
                       context={"x": 1})
    row = event.to_row()
    assert row[0].startswith("ERR-")
    assert row[1:3] == ("runtime", "worker")
    assert isinstance(row[9], datetime)
    assert row[12] == "ict-trader-0.1.0"


def test_observability_errors_and_engine_run_go_to_analytics():
    analytics = FakeAnalytics()
    obs = Observability(analytics, Metrics())
    asyncio.run(obs.error("ai", "timeout", "ai", "late", exc=TimeoutError("x")))
    run = EngineRun(engine="ict", signals=[], latency_ms=12.5)
    asyncio.run(obs.engine_run(run, ctx(), bar_count=1800))
    asyncio.run(obs.engine_timing("ict", "1.0.0", "BTCUSDT", "15m",
                                  ctx().as_of_ms, 9.5, bar_count=1800))
    assert [x[0] for x in analytics.items] == [
        "error_events", "engine_performance", "engine_performance",
    ]
    assert obs.errors == 1
    assert obs.engine_samples == 2
    assert "TimeoutError" in analytics.items[0][2][0][6]


def test_loop_lag_records_metric_and_alert():
    analytics = FakeAnalytics()
    obs = Observability(analytics, Metrics())
    asyncio.run(obs.loop_lag(5.0))
    assert obs.metrics.snapshot()["gauges"]["loop_lag_ms"] == 5.0
    assert analytics.items == []
    asyncio.run(obs.loop_lag(250.0))
    assert analytics.items[0][0] == "error_events"


class FailingEngine:
    engine_id = "bad"
    version = "1"
    warmup_bars = 0

    def analyze(self, window, context):
        raise RuntimeError("broken")


class GoodEngine:
    engine_id = "good"
    version = "1"
    warmup_bars = 0

    def analyze(self, window, context):
        return []


def test_circuit_breaker_opens_and_closes_on_success():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10)
    assert breaker.allow("bad")
    breaker.failure("bad", now=1.0)
    assert breaker.allow("bad", now=2.0)
    breaker.failure("bad", now=2.0)
    assert not breaker.allow("bad", now=3.0)
    assert breaker.allow("bad", now=20.0)
    breaker.success("bad")
    assert breaker.allow("bad", now=30.0)
    snap = breaker.snapshot()["bad"]
    assert snap["total_failures"] == 2
    assert snap["total_successes"] == 1


def test_registry_uses_circuit_breaker():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    registry = EngineRegistry(breaker=breaker)
    registry.register(FailingEngine())
    registry.register(GoodEngine())

    async def run():
        results = []
        for _ in range(3):
            results.extend(await registry.analyze_all(None, ctx(), timeout_seconds=1))
        return results

    results = asyncio.run(run())
    bad = [r for r in results if r.engine == "bad"]
    assert len(bad) == 3
    assert "circuit breaker open" in bad[-1].error
    assert all(r.ok for r in results if r.engine == "good")
