from __future__ import annotations

import pandas as pd
import pytest

from ict_trader.realtime.overlays import build_ict_overlays, overlays_public


def frame(n=80):
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    rows = [{"date": d, "open": 100.0, "high": 100.5, "low": 99.5,
             "close": 100.0} for d in dates]
    # bullish 3-candle gap: c1.high=100, c3.low=103
    rows[40].update(high=100.0, low=99.0, close=99.5)
    rows[42].update(open=103.0, high=104.0, low=103.0, close=103.5)
    return pd.DataFrame(rows)


def test_backend_fvg_uses_exact_bounds():
    data = build_ict_overlays(frame())
    fvg = [r for r in data["rectangles"]
           if r["kind"] == "FVG" and r["direction"] == "bullish"
           and r["low"] == pytest.approx(100.0)]
    assert fvg
    assert fvg[-1]["high"] == pytest.approx(103.0)
    assert fvg[-1]["start_time"] > 0
    assert fvg[-1]["end_time"] > fvg[-1]["start_time"]


def test_backend_ifvg_after_close_through():
    df = frame()
    df.loc[43, "close"] = 99.0
    df.loc[43, "low"] = 98.0
    data = build_ict_overlays(df)
    assert any(r["kind"] == "IFVG" and r["direction"] == "bearish"
               for r in data["rectangles"])


def test_backend_overlays_public_converts_to_seconds():
    public = overlays_public(build_ict_overlays(frame()))
    assert public["source"] == "backend"
    for r in public["rectangles"]:
        assert r["start_time"] < 10**11  # seconds, not milliseconds
        assert r["end_time"] >= r["start_time"]
        assert r["low"] <= r["high"]


def test_ict_arrays_lifecycle_and_rows():
    from ict_trader.realtime.analysis.arrays import build_ict_arrays

    df = frame()
    df.loc[50, ["open", "high", "low", "close"]] = [102.0, 102.2, 99.8, 100.0]
    df.loc[51, ["open", "high", "low", "close"]] = [100.0, 104.5, 99.8, 104.0]
    df.loc[60, ["open", "high", "low", "close"]] = [100.0, 100.2, 99.0, 99.2]
    df.loc[61, ["open", "high", "low", "close"]] = [99.2, 99.4, 98.8, 99.0]
    df.loc[62, ["open", "high", "low", "close"]] = [99.2, 101.5, 99.1, 101.0]
    arrays = build_ict_arrays(df, "XRPUSDT", "15m")
    assert arrays
    assert any(a.array_type == "FVG" and a.is_active for a in arrays)
    assert any(a.array_type == "OB" for a in arrays)
    assert any(a.array_type == "CISD" for a in arrays)
    assert any(a.array_type == "DEALING_RANGE" for a in arrays)
    for a in arrays:
        assert a.array_id.startswith("ARR-")
        assert len(a.row()) == 15
        assert a.price_upper >= a.price_lower


def test_sweep_events_have_type_and_expiry():
    from ict_trader.realtime.analysis.sweeps import build_sweep_events

    df = frame(80)
    for i in range(8, 13):
        df.loc[i, ["open", "high", "low", "close"]] = [99.0, 99.0, 98.0, 98.5]
    df.loc[10, "high"] = 100.0
    df.loc[15, ["open", "high", "low", "close"]] = [99.0, 101.0, 98.5, 99.2]
    events = build_sweep_events(df, ttl_bars=5)
    bsl = [e for e in events if e.sweep_type == "BSL"]
    assert bsl
    assert bsl[-1].direction == "bearish"
    assert bsl[-1].expires_at > bsl[-1].time
    assert bsl[-1].reclaim_strength > 0
    # The event is older than its TTL by the end of this frame.
    assert bsl[-1].active is False
