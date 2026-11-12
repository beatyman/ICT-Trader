"""P0-02 - a sweep is known only when the reclaim prints.

``build_sweep_events`` must timestamp the event at the reclaim bar (its
knowledge time) and count its TTL from that bar, so a signal evaluated on the
penetration bar cannot consume a sweep that has not happened yet
(CB-07, SW-02, SW-11).
"""
from __future__ import annotations

import pandas as pd

from ict_trader.realtime.analysis.evidence import extract_sweep
from ict_trader.realtime.analysis.sweeps import build_sweep_events

PENETRATION_BAR = 30
RECLAIM_BAR = 31
TTL_BARS = 5


def _frame(n: int = 80) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    rows = [{"date": d, "open": 100.0, "high": 100.5, "low": 99.5,
             "close": 100.0} for d in dates]
    df = pd.DataFrame(rows)
    # A confirmed swing high at 101.5 (pivot known one bar after it).
    df.loc[20, ["open", "high", "low", "close"]] = [100.0, 101.5, 100.0, 100.2]
    df.loc[21, ["open", "high", "low", "close"]] = [100.2, 100.6, 99.8, 100.0]
    # Penetration bar: wick through 101.5 but close still above it - no reclaim.
    df.loc[PENETRATION_BAR, ["open", "high", "low", "close"]] = [
        100.0, 102.0, 99.9, 101.8,
    ]
    # Reclaim bar: close back below 101.5 - the sweep becomes known here.
    df.loc[RECLAIM_BAR, ["open", "high", "low", "close"]] = [
        101.8, 101.9, 100.8, 101.0,
    ]
    return df


def _times(df: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]


def _sweep_event(df: pd.DataFrame):
    events = [e for e in build_sweep_events(df, ttl_bars=TTL_BARS)
              if e.sweep_type == "BSL" and e.level == 101.5]
    assert len(events) == 1
    return events[0]


def test_sweep_time_and_expiry_are_measured_from_the_reclaim_bar():
    df = _frame()
    times = _times(df)
    ev = _sweep_event(df)

    assert ev.bar_index == RECLAIM_BAR
    assert ev.time == times[RECLAIM_BAR]
    assert ev.time != times[PENETRATION_BAR]
    assert ev.expires_at == times[RECLAIM_BAR] + TTL_BARS * (times[-1] - times[-2])

    # The timestamp fix must not drop the event's identity/payload.
    assert ev.source_name == "BSL"
    assert ev.level == 101.5
    assert ev.sweep_price == 102.0
    assert ev.direction == "bearish"
    assert ev.close == 101.0


def test_signal_at_the_penetration_bar_cannot_see_the_sweep():
    df = _frame()
    times = _times(df)
    ev = _sweep_event(df)

    sweep = extract_sweep(
        [ev.to_public()],
        direction="short",
        source_kline_open_time=times[PENETRATION_BAR],
    )

    assert sweep is None


def test_signal_at_the_reclaim_bar_sees_the_sweep():
    df = _frame()
    times = _times(df)
    ev = _sweep_event(df)

    sweep = extract_sweep(
        [ev.to_public()],
        direction="short",
        source_kline_open_time=times[RECLAIM_BAR],
    )

    assert sweep is not None
    assert sweep["time"] == times[RECLAIM_BAR]
    assert sweep["level"] == 101.5
    assert sweep["extreme"] == 102.0


def test_sweep_expiry_counts_from_the_reclaim_bar():
    df = _frame()
    times = _times(df)
    public = [_sweep_event(df).to_public()]

    last_visible = extract_sweep(
        public,
        direction="short",
        source_kline_open_time=times[RECLAIM_BAR + TTL_BARS],
    )
    expired = extract_sweep(
        public,
        direction="short",
        source_kline_open_time=times[RECLAIM_BAR + TTL_BARS + 1],
    )

    assert last_visible is not None
    assert expired is None
