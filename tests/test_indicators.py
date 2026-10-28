import numpy as np
import pandas as pd

from ict_trader.indicators import align_htf, ichimoku, macd, resample_cn_4h, resample_htf


def frame(n=160):
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01 09:30", periods=n, freq="15min"),
        "open": close + rng.normal(0, 0.2, n),
        "high": close + 0.8,
        "low": close - 0.8,
        "close": close,
        "volume": 1000,
    })


def test_ichimoku_columns_and_cloud_order():
    df = frame()
    i = ichimoku(df)
    assert {"tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom"} <= set(i.columns)
    tail = i.dropna()
    assert (tail["cloud_top"] >= tail["cloud_bottom"]).all()


def test_macd_identity():
    df = frame()
    m = macd(df)
    assert np.allclose((m["macd"] - m["signal"]).to_numpy(), m["hist"].to_numpy())


def test_htf_alignment_is_backward():
    df = frame()
    htf = resample_htf(df, "1h")
    aligned = align_htf(df, htf, ["close"])
    assert len(aligned) == len(df)
    assert aligned["date"].is_monotonic_increasing


def test_cn_4h_uses_session_closes():
    dates = []
    for day in ("2026-01-05", "2026-01-06"):
        for hh, mm in ((9, 45), (10, 0), (10, 15), (10, 30), (10, 45), (11, 0), (11, 15), (11, 30),
                       (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45), (15, 0)):
            dates.append(pd.Timestamp(f"{day} {hh:02d}:{mm:02d}"))
    n = len(dates)
    df = pd.DataFrame({
        "date": dates, "open": 100.0, "high": 101.0, "low": 99.0,
        "close": np.arange(n, dtype=float), "volume": 1000,
    })
    h = resample_cn_4h(df)
    assert len(h) == 4
    assert set(h["date"].dt.strftime("%H:%M")) == {"11:30", "15:00"}
    assert h["close"].iloc[0] == df["close"].iloc[7]
    assert h["close"].iloc[1] == df["close"].iloc[15]

