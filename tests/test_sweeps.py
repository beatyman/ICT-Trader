import numpy as np
import pandas as pd

from ict_trader.sweeps import KeyLevel, detect_key_levels, detect_sweeps, nearest_key_target


def bars(highs, lows, closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01 09:30", periods=n, freq="15min"),
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n,
    })


def test_bsl_sweep_and_reclaim():
    df = bars([100, 101, 100.5], [99, 98, 99.5], [99.5, 99, 100.0])
    levels = [KeyLevel("BSL", 100.0, "H", known_at=0)]
    sweeps = detect_sweeps(df, levels)
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s.direction == "bear" and s.sweep_bar == 1 and s.reclaim_close == 99.0


def test_break_is_not_sweep():
    df = bars([100, 101, 102], [99, 99, 100.5], [99.5, 101.5, 101.8])
    levels = [KeyLevel("BSL", 100.0, "H", known_at=0)]
    sweeps = detect_sweeps(df, levels)
    assert sweeps == []
    assert levels[0].broken


def test_ssl_sweep_and_nearest_target():
    df = bars([101, 100, 101], [100, 98, 100.5], [100.5, 99.5, 101.0])
    levels = [KeyLevel("SSL", 99.0, "L", known_at=0)]
    sweeps = detect_sweeps(df, levels)
    assert len(sweeps) == 1 and sweeps[0].direction == "bull"
    assert nearest_key_target([KeyLevel("BSL", 105, "H", 0)], 101, True) == 105

