from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ict_trader.realtime.analysis import AnalysisContext, EngineSignal
from ict_trader.realtime.analysis.engines.ichimoku import IchimokuConfig, IchimokuEngine

BASE = 1_700_000_000_000 // 900_000 * 900_000


def frame(n=120, *, last_close=110.0) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = np.full(n, 100.0)
    close[-1] = last_close
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 100.0,
    })


def indicator(long=True) -> pd.DataFrame:
    # Penultimate row has no cross; the final row crosses and is above cloud.
    if long:
        rows = [
            (99.0, 100.0, 98.0, 99.0, 99.0, 98.0, False, True),
            (101.0, 100.0, 102.0, 101.0, 102.0, 101.0, True, False),
        ]
    else:
        rows = [
            (101.0, 100.0, 102.0, 101.0, 102.0, 101.0, True, False),
            (99.0, 100.0, 98.0, 99.0, 99.0, 98.0, False, True),
        ]
    cols = ["tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom",
            "chikou_ok", "chikou_ok_short"]
    return pd.DataFrame(rows, columns=cols)


def context() -> AnalysisContext:
    return AnalysisContext("BTCUSDT", "15m", BASE)


def test_long_cross_creates_immutable_signal():
    engine = IchimokuEngine(IchimokuConfig(require_htf=False), version="ichimoku-test")
    df = frame()
    sig = engine._evaluate(df, indicator(True), context())
    assert isinstance(sig, EngineSignal)
    assert sig.engine == "ichimoku"
    assert sig.direction.value == "long"
    assert sig.signal_type == "ichimoku_tk_cross"
    assert sig.source_kline_open_time == int(df["date"].iloc[-1].timestamp() * 1000)
    assert sig.evidence_plain()["entry"] == 110.0
    with pytest.raises(Exception):
        sig.confidence = 1.0  # type: ignore[misc]


def test_short_cross_creates_short_signal():
    engine = IchimokuEngine(IchimokuConfig(require_htf=False), version="ichimoku-test")
    signal = engine._evaluate(frame(last_close=90.0), indicator(False), context())
    assert signal is not None
    assert signal.direction.value == "short"


def test_no_cross_yields_no_signal():
    engine = IchimokuEngine(IchimokuConfig(require_htf=False), version="ichimoku-test")
    ind = indicator(True)
    ind.loc[0, "tenkan"] = 111.0
    ind.loc[1, "tenkan"] = 112.0
    assert engine._evaluate(frame(), ind, context()) is None


def test_require_htf_without_enough_data_yields_none():
    engine = IchimokuEngine(IchimokuConfig(require_htf=True), version="ichimoku-test")
    # 120 base bars cannot provide 78 completed 4H bars.
    assert engine._evaluate(frame(120), indicator(True), context()) is None


def test_engine_analyze_short_window_returns_empty():
    engine = IchimokuEngine(IchimokuConfig(require_htf=False))
    assert engine.analyze(frame(50), context()) == []


def test_htf_known_at_has_no_forming_bucket():
    engine = IchimokuEngine(IchimokuConfig(require_htf=True))
    df = frame(1500)
    htf = engine._htf_frame(df)
    assert htf is not None
    assert pd.to_datetime(htf["date"].iloc[-1], utc=True) <= pd.Timestamp(df["date"].iloc[-1])


def test_engine_analyze_real_window_returns_engine_signals_only():
    engine = IchimokuEngine(IchimokuConfig(require_htf=False))
    result = engine.analyze(frame(1200), context())
    assert isinstance(result, list)
    assert all(isinstance(x, EngineSignal) for x in result)
