from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ict_trader.concepts import ICTConfig, analyze
from ict_trader.realtime.analysis.feature_snapshot import build_feature_snapshot


def frame(n=4000):
    t = pd.date_range("2026-09-01", periods=n, freq="15min")
    px = 100 + np.sin(np.arange(n) / 40.0) * 3 + np.arange(n) * 0.005
    return pd.DataFrame({
        "date": t, "open": px, "high": px + 0.2, "low": px - 0.2,
        "close": px, "volume": 10 + np.sin(np.arange(n) / 17.0),
    })


def test_snapshot_has_all_timeframes():
    df = frame()
    s = build_feature_snapshot("ETHUSDT", df, analyze(df, ICTConfig()))
    assert s.schema == "ict-feature-v2"
    assert set(s.views) == {"1D", "4H", "1H", "15m"}
    assert s.as_of.tzinfo is not None
    assert s.price > 0


def test_snapshot_is_json_safe_and_records_fvg():
    df = frame()
    s = build_feature_snapshot("ETHUSDT", df, analyze(df, ICTConfig()))
    public = s.to_public()
    assert public["symbol"] == "ETHUSDT"
    assert isinstance(public["fvg"], list)
    assert public["schema"] == "ict-feature-v2"
