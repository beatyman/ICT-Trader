from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from ict_trader.realtime.analysis import AnalysisContext, EngineSignal
from ict_trader.realtime.analysis.engines.chan import ChanConfig, ChanEngine
from ict_trader.realtime.analysis.engines.wyckoff import WyckoffConfig, WyckoffEngine
from ict_trader.chanlun import ChanlunResult
from ict_trader.wyckoff import WyckoffSignal


def frame(n=120, last=110.0):
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = np.full(n, 100.0)
    close[-1] = last
    return pd.DataFrame({
        "date": dates, "open": close - 0.3, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": 100.0,
    })


def context():
    return AnalysisContext("BTCUSDT", "15m", 1_700_000_000_000)


def test_wyckoff_spring_builds_signal():
    engine = WyckoffEngine(WyckoffConfig())
    df = frame()
    sig = WyckoffSignal(
        bar=len(df) - 1, direction="long", kind="spring", entry_price=110.0,
        range_top=112.0, range_bottom=98.0, creek_ice=111.0,
        climax_side=99.0, spring_extreme=99.0,
    )
    state = SimpleNamespace(phase="C", stage=3, t1=118.0, t2=124.0, signals=[sig])
    result = engine._build(df, state, sig, context())
    assert isinstance(result, EngineSignal)
    assert result.engine == "wyckoff"
    assert result.direction.value == "long"
    assert result.signal_type == "wyckoff_spring"
    assert result.invalidation_price == 99.0
    assert result.evidence_plain()["t1"] == 118.0


def test_wyckoff_rejects_wrong_side_stop():
    engine = WyckoffEngine(WyckoffConfig())
    df = frame()
    sig = WyckoffSignal(
        bar=len(df) - 1, direction="long", kind="spring", entry_price=110.0,
        range_top=112.0, range_bottom=98.0, creek_ice=111.0,
        climax_side=99.0, spring_extreme=120.0,
    )
    state = SimpleNamespace(phase="C", stage=3, t1=118.0, t2=124.0, signals=[sig])
    assert engine._build(df, state, sig, context()) is None


def test_chan_bsp_builds_signal(monkeypatch):
    import ict_trader.realtime.analysis.engines.chan as mod

    out = ChanlunResult(
        bi_count=8, seg_count=2, zs_count=1, last_bi_direction="up",
        last_zs_zd=105.0, last_zs_zg=109.0, price=110.0, position="above",
        bias="long", bsp_type="3买", bsp_count=1, reasons=["test"],
    )
    monkeypatch.setattr(mod, "analyze_chanlun", lambda df, symbol, freq: out)
    engine = ChanEngine(ChanConfig())
    result = engine.analyze(frame(), context())
    assert len(result) == 1
    sig = result[0]
    assert sig.engine == "chan"
    assert sig.direction.value == "long"
    assert sig.invalidation_price == 105.0
    assert sig.evidence_plain()["bsp_type"] == "3买"


def test_chan_missing_dependency_is_no_signal(monkeypatch):
    import ict_trader.realtime.analysis.engines.chan as mod

    def boom(*args, **kwargs):
        raise ImportError("chan.py unavailable")

    monkeypatch.setattr(mod, "analyze_chanlun", boom)
    assert ChanEngine(ChanConfig()).analyze(frame(), context()) == []
