import numpy as np
import pandas as pd

from ict_trader.regime import market_regime


def test_market_regime_on_rising_series_is_up():
    close = np.linspace(100, 150, 120)
    df = pd.DataFrame({
        "close": close,
        "high": close + 0.5,
        "low": close - 0.5,
    })
    r = market_regime(df, 100)
    assert r.trend == "up"
    assert r.trend_strength > 0
    assert 0.0 <= r.vol_rank <= 1.0


def test_market_regime_on_flat_series_is_flat():
    close = np.full(120, 100.0)
    df = pd.DataFrame({
        "close": close,
        "high": close + 0.1,
        "low": close - 0.1,
    })
    r = market_regime(df, 100)
    assert r.trend == "flat"
