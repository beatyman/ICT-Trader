"""P0-10: one array stack, a newest-bar FVG, and CISD lines are not zones.

``arrays.py`` is a lifecycle view of ``concepts.py`` -- it may not hold its own
FVG/IFVG/OB/CISD/range formulas (A7, X8, §14.1-2).  These tests pin the three
defects named by ``CODE_MAPPING.md`` P0-10:

* FVG/IFVG/OB/range bounds and lifecycle flags agree with ``concepts.py`` for
  the same frame (``N2``); the assertion message names ``concepts.py`` as the
  authoritative stack on a parity failure.
* the FVG scan includes the newest closed gap (``range(2, n)``, CB-02), so the
  last bar may complete the newest FVG.
* a zero-height CISD is a line/invalid array, not a tradable zone, and both
  directions share one type id.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ict_trader.concepts import (
    ICTConfig,
    analyze,
    detect_dealing_range,
)
from ict_trader.realtime.analysis.arrays import build_ict_arrays


def _times(df: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]


def _random_walk(seed: int, n: int) -> pd.DataFrame:
    """Deterministic synthetic frame with enough gaps/OBs for a parity check."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.6, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


def _key(direction: str, top: float, bottom: float, created: int,
         active: bool) -> tuple:
    return (direction, round(float(top), 9), round(float(bottom), 9),
            int(created), bool(active))


def _array_key(array, at: dict[int, int]) -> tuple:
    direction = "bull" if array.direction == "bullish" else "bear"
    return _key(direction, array.price_upper, array.price_lower,
                at[array.formed_at], array.is_active)


def test_arrays_and_concepts_agree_on_fvg_ob_and_range():
    df = _random_walk(17, 60)
    times = _times(df)
    at = {t: i for i, t in enumerate(times)}
    cfg = ICTConfig()
    result = analyze(df, cfg)
    arrays = build_ict_arrays(df, "TESTUSDT", "15m")

    # FVG / IFVG -- bounds, creation bar and lifecycle flag must be identical.
    canonical_fvg = {
        _key(f.direction, f.top, f.bottom, f.created, f.active)
        for f in result.fvgs if f.label == "FVG"
    }
    array_fvg = {
        _array_key(a, at) for a in arrays if a.array_type == "FVG"
    }
    assert array_fvg == canonical_fvg, (
        "arrays.py must be a lifecycle view of concepts.py; concepts.py is the "
        "authoritative FVG stack"
    )

    canonical_ifvg = {
        _key(f.direction, f.top, f.bottom, f.created, f.active)
        for f in result.fvgs if f.label == "IFVG"
    }
    array_ifvg = {
        _array_key(a, at) for a in arrays if a.array_type == "IFVG"
    }
    assert array_ifvg == canonical_ifvg, (
        "arrays.py must be a lifecycle view of concepts.py; concepts.py is the "
        "authoritative IFVG stack"
    )

    # OB -- the same candidate, zone and active state.
    canonical_ob = {
        _key(o.direction, o.top, o.bottom, o.created, o.active)
        for o in result.order_blocks
    }
    array_ob = {
        _array_key(a, at) for a in arrays if a.array_type == "OB"
    }
    assert array_ob == canonical_ob, (
        "arrays.py must be a lifecycle view of concepts.py; concepts.py is the "
        "authoritative OB stack"
    )

    # Dealing range -- one constructor, one knowledge time.
    canonical_range = detect_dealing_range(df, result.swings,
                                           swing_len=cfg.swing_len)
    assert result.dealing_range is not None
    assert canonical_range is not None
    assert (round(canonical_range.top, 9), round(canonical_range.bottom, 9),
            canonical_range.known_at, canonical_range.fallback) == (
        round(result.dealing_range.top, 9),
        round(result.dealing_range.bottom, 9),
        result.dealing_range.known_at, result.dealing_range.fallback,
    )

    range_arrays = [a for a in arrays if a.array_type == "DEALING_RANGE"]
    assert len(range_arrays) == 1
    dr = range_arrays[0]
    assert round(dr.price_upper, 9) == round(canonical_range.top, 9)
    assert round(dr.price_lower, 9) == round(canonical_range.bottom, 9)
    assert at[dr.formed_at] == canonical_range.known_at
    assert dr.is_active is True


def test_newest_closed_fvg_is_emitted():
    """The final bar is C3 of a bullish gap: ``range(2, n)`` must see it."""
    n = 20
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    rows = [{"date": d, "open": 100.0, "high": 100.5, "low": 99.5,
             "close": 100.0} for d in dates]
    # C1 at bar 17, displacement C2 at bar 18, C3 at bar 19 (the newest bar):
    # C3.low = 102 > C1.high = 100 -> bull FVG [100, 102].
    rows[17].update(open=99.0, high=100.0, low=98.0, close=99.5)
    rows[18].update(open=99.5, high=104.0, low=99.0, close=103.5)
    rows[19].update(open=103.5, high=106.0, low=102.0, close=105.0)
    df = pd.DataFrame(rows)
    times = _times(df)

    canonical = [f for f in analyze(df, ICTConfig()).fvgs if f.label == "FVG"]
    newest = [f for f in canonical if f.created == n - 1]
    assert len(newest) == 1
    assert (newest[0].direction, newest[0].top, newest[0].bottom) == (
        "bull", 102.0, 100.0,
    )

    arrays = build_ict_arrays(df, "TESTUSDT", "15m")
    newest_arrays = [a for a in arrays
                     if a.array_type == "FVG" and a.formed_at == times[-1]]
    assert len(newest_arrays) == 1, "the newest closed FVG must not be dropped"
    gap = newest_arrays[0]
    assert (gap.direction, gap.price_upper, gap.price_lower) == (
        "bullish", 102.0, 100.0,
    )
    assert gap.is_active is True


def test_zero_height_cisd_is_an_invalid_line_not_a_zone():
    """A CISD close-through is a line; it is never an active zero-height zone."""
    df = _random_walk(0, 80)
    result = analyze(df, ICTConfig())
    assert {c.direction for c in result.cisds} == {"bull", "bear"}

    arrays = build_ict_arrays(df, "TESTUSDT", "15m")
    cisd_arrays = [a for a in arrays if a.array_type == "CISD"]
    assert cisd_arrays, "a CISD is represented as an invalid line, not dropped"

    # Both sides share one symmetric type id; direction is the discriminator.
    assert {a.array_type for a in arrays} >= {"CISD"}
    assert "CISD_BEARISH" not in {a.array_type for a in arrays}
    assert {a.direction for a in cisd_arrays} == {"bullish", "bearish"}

    canonical_levels = {round(c.level, 9) for c in result.cisds}
    for array in cisd_arrays:
        assert array.price_upper == array.price_lower, (
            "a CISD has no zone height; it may not pretend to be a zone"
        )
        assert array.is_active is False
        assert array.invalidated_at == array.formed_at
        assert round(array.price_upper, 9) in canonical_levels

    # No array of any type may be an active zero-height zone.
    for array in arrays:
        if array.price_upper == array.price_lower:
            assert array.is_active is False
