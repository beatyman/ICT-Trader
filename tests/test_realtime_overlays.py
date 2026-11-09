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
