from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.realtime.analysis import AnalysisContext, EngineSignal
from ict_trader.realtime.analysis.engines import ICTEngine
from ict_trader.setup import SetupConfig


def frame(n: int = 1500) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = np.linspace(100, 102, n)
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.1,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 100.0,
    })


def cisd(end: int = 1499) -> CISD:
    return CISD(
        direction="bull", level=100.5, extreme=99.0, body_extreme=100.0,
        start=end - 5, end=end, sl=98.0, target=106.0, grade=4,
        checks={"sweep": True, "pda": True, "volume": True},
        quality=0.72, quality_parts={"disp_body_atr": 1.2},
    )


def test_ict_engine_emits_immutable_signal(monkeypatch):
    import ict_trader.realtime.analysis.engines.ict as ict_module
    c = cisd()
    monkeypatch.setattr(
        ict_module, "analyze",
        lambda df, cfg: ICTResult(bars=df, cisds=[c]),
    )
    monkeypatch.setattr(ict_module, "build_setup", lambda df, res, cfg: [c])
    engine = ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True))
    ctx = AnalysisContext("btcusdt", "15m", 1_700_000_000_000)
    signals = engine.analyze(frame(), ctx)
    assert len(signals) == 1
    sig = signals[0]
    assert isinstance(sig, EngineSignal)
    assert sig.engine == "ict"
    assert sig.symbol == "BTCUSDT"
    assert sig.direction.value == "long"
    assert sig.signal_type == "ict_cisd"
    assert sig.trigger_price_zone.low <= sig.trigger_price_zone.high
    assert sig.evidence_plain()["target"] == 106.0
    assert sig.evidence_plain()["checks"]["sweep"] is True
    assert sig.engine_version == "ict_trader-0.1.0"


def test_ict_engine_signal_entry_matches_source_bar_close(monkeypatch):
    import ict_trader.realtime.analysis.engines.ict as ict_module
    df = frame(1500)
    c = cisd(end=1499)
    monkeypatch.setattr(
        ict_module, "analyze",
        lambda data, cfg: ICTResult(bars=data, cisds=[c]),
    )
    monkeypatch.setattr(ict_module, "build_setup", lambda data, res, cfg: [c])
    engine = ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True))
    ctx = AnalysisContext("BTCUSDT", "15m", 1_700_000_000_000)
    sig = engine.analyze(df, ctx)[0]
    assert sig.source_kline_open_time == int(df["date"].iloc[1499].timestamp() * 1000)
    assert sig.evidence_plain()["entry"] == float(df["close"].iloc[1499])
    assert "ict_overlays" in sig.evidence_plain()


def test_ict_engine_ignores_stale_setup_for_actionable(monkeypatch):
    import ict_trader.realtime.analysis.engines.ict as ict_module
    c = cisd(end=1400)
    monkeypatch.setattr(
        ict_module, "analyze",
        lambda df, cfg: ICTResult(bars=df, cisds=[c]),
    )
    monkeypatch.setattr(ict_module, "build_setup", lambda df, res, cfg: [c])
    engine = ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True))
    ctx = AnalysisContext("BTCUSDT", "15m", 1_700_000_000_000)
    assert engine.analyze(frame(), ctx) == []
    history = engine.scan_history(frame(), ctx)
    assert len(history) == 1
    assert history[0].source_kline_open_time > 0


def test_rule_mode_does_not_use_unbound_symbol(monkeypatch):
    import pandas as pd
    from ict_trader.realtime.analysis.engines.ict import ICTEngine
    from ict_trader.realtime.analysis import AnalysisContext
    from ict_trader.concepts import ICTConfig
    from ict_trader.setup import SetupConfig

    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=140, freq="15min"),
        "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0,
        "volume": 1.0,
    })
    engine = ICTEngine(
        setup_config=SetupConfig(rr=2.0, stack="4h_15m"),
        ict_config=ICTConfig(), rule_mode=True,
    )
    out = engine.analyze(df, AnalysisContext("ZECUSDT", "15m", 1_700_000_000_000))
    assert out == []
