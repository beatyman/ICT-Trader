"""P0-01 - a forming HTF bar must not leak into the signal's HTF bias.

``htf_bias_evidence`` resamples the base frame and used to hand the still
forming 1D/4H bucket to ``structure_bias``.  That bucket's partial OHLC (in
particular its close) could set or flip the bias attached to a signal, even
though the bucket is knowable only at its close time (CB-01, CB-07, CB-08).
"""
from __future__ import annotations

import pandas as pd

from ict_trader.realtime.analysis.evidence import htf_bias_evidence
from ict_trader.realtime.analysis.structure import structure_bias

START = pd.Timestamp("2026-01-01", tz="UTC")
DAY = pd.Timedelta(days=1)


def _bar(ts: pd.Timestamp, close: float) -> dict:
    return {
        "date": ts,
        "open": close - 0.10,
        "high": close + 0.20,
        "low": close - 0.20,
        "close": close,
        "volume": 1.0,
    }


def _uptrend_frame() -> pd.DataFrame:
    rows: list[dict] = []
    # 21 complete days of a clean uptrend: the last closed 1D bar is bullish.
    for i in range(21 * 96):
        day, bar = divmod(i, 96)
        rows.append(_bar(START + pd.Timedelta(minutes=15 * i),
                         100.0 + day + bar / 96.0))
    # Day 22 starts with the last complete 4H bucket (00:00-03:59), still rising.
    for bar in range(16):
        rows.append(_bar(START + DAY * 21 + pd.Timedelta(minutes=15 * bar),
                         121.0 + bar / 96.0))
    # The forming 4H bucket (04:00-07:59) and the forming 1D bar (day 22) crash.
    # Neither bucket is knowable at the base frame's last bar (05:45).
    for bar in range(16, 24):
        rows.append(_bar(START + DAY * 21 + pd.Timedelta(minutes=15 * bar),
                         80.0 + (bar - 16) * 0.1))
    return pd.DataFrame(rows)


def _expected_bias(frame: pd.DataFrame, rule: str, cutoff: pd.Timestamp) -> dict:
    """Bias from only the buckets that closed before ``cutoff``."""
    complete = frame[frame["date"] < cutoff]
    x = (complete.set_index("date").resample(rule)
         .agg({"open": "first", "high": "max", "low": "min",
               "close": "last", "volume": "sum"})
         .dropna().reset_index())
    b = structure_bias(x)
    return {"bias": b["bias"], "event": b["event"],
            "event_time": b["event_time"]}


def test_forming_htf_buckets_are_not_visible_to_the_signal():
    frame = _uptrend_frame()
    out = htf_bias_evidence(frame, len(frame) - 1)
    assert out is not None
    day22 = START + DAY * 21
    assert out["1D"] == _expected_bias(frame, "1D", day22)
    assert out["4h"] == _expected_bias(
        frame, "4h", day22 + pd.Timedelta(hours=4))
    # The synthetic crash exists only in the forming buckets; seeing it would
    # change both reads (1D collapses to neutral, 4H flips to bearish).
    assert out["1D"]["bias"] == "bullish"
    assert out["4h"]["bias"] == "bullish"
    assert out["1D"]["event"] != "BOS_DOWN"


def test_a_dropped_forming_bucket_is_not_replaced_by_an_incomplete_bar():
    rows: list[dict] = []
    # 19 closed 1D bars; adding the forming one would fake a 20-bar history.
    for i in range(19 * 96):
        day, bar = divmod(i, 96)
        rows.append(_bar(START + pd.Timedelta(minutes=15 * i),
                         100.0 + day + bar / 96.0))
    for bar in range(24):
        rows.append(_bar(START + DAY * 19 + pd.Timedelta(minutes=15 * bar),
                         80.0 + bar * 0.1))
    frame = pd.DataFrame(rows)

    out = htf_bias_evidence(frame, len(frame) - 1)
    assert out is not None
    assert out["1D"] is None
    assert out["4h"] is not None
