from __future__ import annotations

import pandas as pd

from ict_trader.realtime.analysis.structure import pivot_bos_bias


def test_pivot_bos_bias_uses_recent_confirmed_break():
    rows = []
    for i in range(40):
        close = 100 + i * 0.5
        rows.append({"date": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15 * i),
                     "open": close - 0.2, "high": close + 0.4, "low": close - 0.4, "close": close})
    out = pivot_bos_bias(pd.DataFrame(rows))
    assert out["bias"] == "bullish"
    assert out["event"] in {"BOS_UP", "TREND_UP", ""}
