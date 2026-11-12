"""P0-03 - dealing-range knowledge time and half-open session windows.

Two rules are locked here (CB-07, DR-06, LT-K13, SYSTEM_SPEC §10.1):

* a dealing range is knowable only once **both** structural anchors have
  confirmed, so its ``formed_at`` is the later anchor's confirmation bar, not
  the earlier anchor's bar; and
* every session window is one half-open interval ``[start, end)`` on the
  session wall clock, with the ``asian`` window declared as ``19:00-00:00``
  (the 00:00 end instant is exclusive, not hidden behind ``23:59:59``).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import in_session
from ict_trader.realtime.analysis.arrays import build_ict_arrays

SWING_HIGH_BAR = 20
SWING_LOW_BAR = 45


def _ms(frame: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(x).timestamp() * 1000) for x in frame["date"]]


def _range_frame() -> pd.DataFrame:
    """A clean high at bar 20 then a clean low at bar 45."""
    n = 60
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    price = np.concatenate([
        np.linspace(100.0, 120.0, SWING_HIGH_BAR + 1),
        np.linspace(120.0, 95.0, 25),
        np.linspace(95.0, 100.0, 14),
    ])
    df = pd.DataFrame({
        "date": dates, "open": price, "high": price + 0.4,
        "low": price - 0.4, "close": price,
    })
    df.loc[SWING_HIGH_BAR, "high"] = 121.0
    df.loc[SWING_LOW_BAR, "low"] = 94.0
    return df


def _anchors(df: pd.DataFrame):
    swings = build_zigzag(find_pivots(df, 5, 1))
    high = [s for s in swings if s.kind == "H"][-1]
    low = [s for s in swings if s.kind == "L"][-1]
    return high, low


def _dealing_range(df: pd.DataFrame):
    return next(a for a in build_ict_arrays(df, "TESTUSDT", "15m")
                if a.array_type == "DEALING_RANGE")


def test_dealing_range_formed_at_is_the_later_anchor_confirmation():
    df = _range_frame()
    times = _ms(df)
    high, low = _anchors(df)
    dr = _dealing_range(df)

    assert (int(high.index), int(high.confirmed_at)) == (SWING_HIGH_BAR, SWING_HIGH_BAR + 1)
    assert (int(low.index), int(low.confirmed_at)) == (SWING_LOW_BAR, SWING_LOW_BAR + 1)

    later_confirmed = max(int(high.confirmed_at), int(low.confirmed_at))
    assert dr.formed_at == times[later_confirmed]
    assert dr.formed_at != times[min(int(high.index), int(low.index))]


def test_dealing_range_is_not_knowable_before_either_anchor_confirms():
    df = _range_frame()
    times = _ms(df)
    high, low = _anchors(df)
    dr = _dealing_range(df)

    assert dr.formed_at >= times[int(high.confirmed_at)]
    assert dr.formed_at >= times[int(low.confirmed_at)]
    assert dr.formed_at >= times[int(high.index)]
    assert dr.formed_at >= times[int(low.index)]


def _ny_bars(*ny_times: str) -> pd.DataFrame:
    dates = [pd.Timestamp(t, tz="America/New_York") for t in ny_times]
    return pd.DataFrame({
        "date": dates,
        "open": [100.0] * len(dates), "high": [101.0] * len(dates),
        "low": [99.0] * len(dates), "close": [100.0] * len(dates),
    })


def test_asian_window_ends_at_midnight():
    df = _ny_bars(
        "2026-07-15 18:59", "2026-07-15 19:00",
        "2026-07-15 23:59", "2026-07-16 00:00",
    )
    assert in_session(df, 0, "crypto", "asian") is False   # before the open
    assert in_session(df, 1, "crypto", "asian") is True    # start is inclusive
    assert in_session(df, 2, "crypto", "asian") is True    # last minute of the range
    assert in_session(df, 3, "crypto", "asian") is False   # 00:00 is the exclusive end


def test_window_end_is_exclusive():
    df = _ny_bars("2026-07-15 10:00", "2026-07-15 10:59", "2026-07-15 11:00")
    assert in_session(df, 0, "crypto", "silver_bullet") is True   # start inclusive
    assert in_session(df, 1, "crypto", "silver_bullet") is True
    assert in_session(df, 2, "crypto", "silver_bullet") is False  # 11:00 hard close
