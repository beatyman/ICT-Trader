from __future__ import annotations

import asyncio
import time

import pytest

from ict_trader.realtime.analysis import (
    AnalysisContext, AnalysisEngine, EngineRegistry, EngineSignal, PriceZone,
)
from ict_trader.realtime.models import utc_ms

BASE = 1_700_000_000_000 // 900_000 * 900_000


def ctx() -> AnalysisContext:
    return AnalysisContext("btcusdt", "15m", BASE, params={"sweep": 3})


def signal(**overrides) -> EngineSignal:
    kwargs = dict(
        engine="ict", symbol="BTCUSDT", interval="15m", direction="long",
        signal_type="cisd", trigger_price_zone=PriceZone(100.0, 101.0),
        invalidation_price=98.0, confidence=0.7,
        source_kline_open_time=BASE, engine_version="1.0.0",
        evidence={"checks": {"sweep": True}, "level": 100.5},
    )
    kwargs.update(overrides)
    return EngineSignal(**kwargs)


def test_signal_is_immutable_and_idempotent():
    a = signal()
    b = signal()
    assert a.signal_id == b.signal_id
    assert a.direction.value == "long"
    with pytest.raises(Exception):
        a.confidence = 0.9  # type: ignore[misc]
    with pytest.raises(TypeError):
        a.evidence["x"] = 1  # type: ignore[index]
    assert a.evidence["checks"]["sweep"] is True


def test_signal_validation():
    with pytest.raises(ValueError):
        signal(confidence=1.1)
    with pytest.raises(ValueError):
        signal(direction="up")
    with pytest.raises(ValueError):
        signal(source_kline_open_time=BASE + 1)
    with pytest.raises(ValueError):
        signal(engine_version="")
    with pytest.raises(ValueError):
        PriceZone(2.0, 1.0)
    with pytest.raises(ValueError):
        PriceZone(0.0, 1.0)


def test_zone_and_public_shape():
    s = signal()
    out = s.to_public()
    assert out["signal_id"].startswith("SIG-")
    assert out["trigger_price_zone"] == {"low": 100.0, "high": 101.0}
    assert out["evidence"]["checks"]["sweep"] is True
    assert s.trigger_price == 100.5
    assert PriceZone(1, 3).contains(2)


def test_signal_row_mapping():
    from datetime import datetime
    from ict_trader.realtime.storage.row_mappers import engine_signal_row

    row = engine_signal_row(signal())
    assert row[0].startswith("SIG-")
    assert row[1:3] == ("ict", "1.0.0")
    assert row[6] == "cisd"
    assert isinstance(row[12], datetime)
    assert isinstance(row[13], datetime)
    assert isinstance(row[14], datetime)


class GoodEngine:
    engine_id = "good"
    version = "1.0.0"
    warmup_bars = 100

    def analyze(self, window, context):
        assert context.symbol == "BTCUSDT"
        return [signal()]


class BadEngine:
    engine_id = "bad"
    version = "1.0.0"
    warmup_bars = 0

    def analyze(self, window, context):
        raise RuntimeError("boom")


class SlowEngine:
    engine_id = "slow"
    version = "1.0.0"
    warmup_bars = 0

    def analyze(self, window, context):
        time.sleep(0.8)
        return []


def test_registry_and_failure_isolation():
    registry = EngineRegistry()
    registry.register(GoodEngine())
    registry.register(BadEngine())
    assert registry.names() == ("bad", "good")
    good = registry.run("good", None, ctx())
    bad = registry.run("bad", None, ctx())
    assert good.ok and len(good.signals) == 1
    assert not bad.ok and "RuntimeError" in bad.error


def test_registry_rejects_duplicates_and_bad_returns():
    registry = EngineRegistry()
    registry.register(GoodEngine())
    with pytest.raises(ValueError):
        registry.register(GoodEngine())
    with pytest.raises(KeyError):
        registry.get("missing")

    class Wrong:
        engine_id = "wrong"
        version = "1"
        warmup_bars = 0

        def analyze(self, window, context):
            return ["not-a-signal"]

    registry.register(Wrong())
    result = registry.run("wrong", None, ctx())
    assert not result.ok


def test_analyze_all_timeout_and_concurrency():
    registry = EngineRegistry()
    registry.register(GoodEngine())
    registry.register(SlowEngine())
    runs = asyncio.run(registry.analyze_all(None, ctx(), timeout_seconds=0.2))
    by_id = {r.engine: r for r in runs}
    assert by_id["good"].ok
    assert "timeout" in by_id["slow"].error
