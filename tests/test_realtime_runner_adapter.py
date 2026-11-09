from __future__ import annotations

from ict_trader.realtime.analysis.contracts import EngineSignal, PriceZone
from ict_trader.realtime.analysis.engines.ict import ICTEngine
from ict_trader.realtime.config import Settings
from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.models import Kline
from ict_trader.realtime.strategy_runner import ICTStrategyRunner

BASE = 1_700_000_000_000 // 900_000 * 900_000


def bars(n=1500) -> list[Kline]:
    return [
        Kline(
            symbol="BTCUSDT", interval="15m", open_time=BASE + i * 900_000,
            close_time=BASE + (i + 1) * 900_000 - 1, open=100.0, high=101.0,
            low=99.0, close=100.0, volume=10.0, is_closed=True,
        )
        for i in range(n)
    ]


def test_runner_maps_engine_signal_to_trade_signal(tmp_path, monkeypatch):
    last = bars()[-1]
    engine_signal = EngineSignal(
        engine="ict", symbol="BTCUSDT", interval="15m", direction="long",
        signal_type="ict_cisd", trigger_price_zone=PriceZone(100.0, 101.0),
        invalidation_price=98.0, confidence=0.7,
        source_kline_open_time=last.open_time, engine_version="ict_trader-0.1.0",
        evidence={
            "entry": 100.5, "stop": 98.0, "target": 106.0, "rr": 3.0,
            "grade": 4, "quality": 0.7, "setup_level": 100.0,
            "checks": {"sweep": True}, "quality_parts": {"disp_body_atr": 1.2},
            "thesis": "CISD bull",
        },
    )
    monkeypatch.setattr(ICTEngine, "analyze", lambda self, frame, context: [engine_signal])
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                   data_dir=tmp_path)
    runner = ICTStrategyRunner(cfg, Metrics())
    out = runner._compute("BTCUSDT", bars(), True)
    assert len(out) == 1
    signal = out[0]
    assert signal.id == engine_signal.signal_id
    assert signal.direction == "long"
    assert signal.entry == 100.5
    assert signal.stop == 98.0
    assert signal.target == 106.0
    assert signal.grade == 4
    assert signal.checks["sweep"] is True
    assert signal.bar_time == last.open_time
