import numpy as np
import pandas as pd

from ict_trader.cisd import CISDConfig, detect_cisd
from ict_trader.concepts import ICTConfig, analyze


def sample_df(n=120):
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.4, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.8, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.8, n)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(500, 2000, n),
    })


def test_cisd_sl_side():
    df = sample_df()
    levels = [(float(h), "H") for h in df["high"].iloc[::7]]
    levels += [(float(l), "L") for l in df["low"].iloc[::7]]
    out = detect_cisd(df, CISDConfig(require_origin=False, min_grade=0, max_per_side=3),
                      swing_levels=levels)
    assert out
    for c in out:
        if c.direction == "bull":
            assert c.sl < c.level
        else:
            assert c.sl > c.level


def test_ict_result_contains_cisd():
    df = sample_df()
    res = analyze(df, ICTConfig(swing_len=3, plot_bars=60, cisd_require_origin=False,
                                cisd_min_grade=0, cisd_max=2))
    assert isinstance(res.cisds, list)

