from pathlib import Path

import numpy as np
import pandas as pd

from ict_trader.concepts import FVG, ICTConfig, analyze, load_csv, plot, signals_frame


def sample_df(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.0, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.0, n)
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": 1000}
    )


def test_fvg_ce_and_first_presented():
    f = FVG("bull", 110.0, 100.0, 1, 3, 3, first_presented=True)
    assert f.ce == 105.0
    assert f.ce == (f.top + f.bottom) / 2


def test_analyze_and_signals():
    df = sample_df()
    cfg = ICTConfig(swing_len=3, plot_bars=60)
    result = analyze(df, cfg)
    assert len(result.bars) == len(df)
    assert result.trend is not None
    assert result.config is cfg

    sig = signals_frame(result)
    assert list(sig.columns) == ["date", "type", "direction", "price", "index",
                                 "sweep_index", "reclaim_bars"]
    assert len(sig) > 0
    assert set(sig["direction"]).issubset({"bull", "bear"})


def test_plot_writes_png(tmp_path: Path):
    df = sample_df()
    result = analyze(df, ICTConfig(swing_len=3, plot_bars=60))
    out = tmp_path / "chart.png"
    path = plot(result, out, title="test", bars=60)
    assert path.exists()
    assert path.stat().st_size > 1000


def test_load_csv(tmp_path: Path):
    df = sample_df(20)
    p = tmp_path / "bars.csv"
    df.to_csv(p, index=False)
    loaded = load_csv(p)
    assert len(loaded) == 20
    assert str(loaded["date"].dtype).startswith("datetime64")

