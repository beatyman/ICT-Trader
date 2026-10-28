import pandas as pd

from ict_trader.concepts import FVG, detect_ifvg


def test_bull_fvg_flips_to_bear_ifvg():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=5, freq="15min"),
        "open": [100, 100, 100, 100, 100],
        "high": [101, 101, 101, 101, 101],
        "low": [99, 99, 99, 99, 99],
        "close": [100, 100, 100, 100, 98.5],
        "volume": [1000] * 5,
    })
    f = FVG("bull", top=100.5, bottom=99.5, left=1, right=4, created=1)
    out = detect_ifvg([f], df)
    assert len(out) == 1
    assert out[0].direction == "bear"
    assert out[0].label == "IFVG"
    assert f.inverted is True


def test_wick_through_does_not_flip():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=4, freq="15min"),
        "open": [100] * 4, "high": [101] * 4, "low": [99] * 4,
        "close": [100] * 4, "volume": [1000] * 4,
    })
    f = FVG("bull", top=100.5, bottom=99.5, left=1, right=3, created=1)
    out = detect_ifvg([f], df)
    assert out == []

