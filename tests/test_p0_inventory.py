"""P0-14 - the selection inventory is complete and the IFVG side is declared.

The three defects in ``CODE_MAPPING.md`` P0-14 are pinned here:

* the repo 500-bar "present" cut is a **display** window; it may not remove an
  eligible array or sweep from the inventory that selection reads (A1, §7.3);
* per-kind caps are a presentation rule; the ninth active FVG must still be in
  ``build_ict_arrays`` so §7.3 can choose it (AS-06, §7.3);
* IFVG context carries the declared ``side_mode`` (AS-04), so a reader can tell
  the by-position label from the corpus's by-type/by-role alternatives.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ict_trader.concepts import ICTConfig, analyze, presentation_view
from ict_trader.realtime.analysis.arrays import (
    PRESENTATION_CAPS,
    build_ict_arrays,
    present_ict_arrays,
)
from ict_trader.realtime.analysis.dealing_range import DealingRange
from ict_trader.realtime.analysis.evidence import (
    IFVG_SIDE_MODE,
    IFVG_SIDE_MODES,
    ifvg_evidence,
    ifvg_side,
)
from ict_trader.setup import _BoxIndex


def _times(frame: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(x).timestamp() * 1000) for x in frame["date"]]


def _inventory_frame(n: int = 900) -> pd.DataFrame:
    """An old bullish FVG at bar 12 and an old SSL sweep at bar 25."""
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    price = np.full(n, 115.0)
    price[:13] = np.linspace(99.2, 104.0, 13)
    high, low = price + 0.3, price - 0.3
    open_, close = price - 0.05, price + 0.05

    # C1 at 10, displacement C2 at 11, C3 at 12: bull FVG [100.0, 102.5].
    high[10], low[10], open_[10], close[10] = 100.0, 99.0, 99.5, 99.6
    open_[11], high[11], low[11], close[11] = 99.6, 104.2, 99.5, 104.0
    open_[12], high[12], low[12], close[12] = 104.0, 104.5, 102.5, 104.0

    # A clean swing low at 20 (level 100) and a wick/reclaim at 25.
    for j in range(13, 20):
        open_[j], high[j], low[j], close[j] = 104.8, 105.2, 104.5, 105.0
    open_[20], high[20], low[20], close[20] = 100.6, 100.8, 100.0, 100.5
    for j in range(21, 25):
        open_[j], high[j], low[j], close[j] = 100.5, 100.9, 100.2, 100.6
    open_[25], high[25], low[25], close[25] = 100.6, 100.9, 99.8, 100.7
    for j, wick in enumerate((99.7, 99.6, 99.5, 99.4), start=26):
        open_[j], high[j], low[j], close[j] = 100.5, 100.9, wick, 100.7

    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


def _many_gaps_frame(n: int = 900,
                     gaps: tuple[int, ...] = (20, 120, 220, 320, 420,
                                              520, 620, 720, 820)
                     ) -> pd.DataFrame:
    """Nine active bullish FVGs, each in a higher price shelf."""
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    level = np.zeros(n)
    for j, start in enumerate(gaps):
        end = gaps[j + 1] if j + 1 < len(gaps) else n
        level[start:end] = 100.0 + 4.0 * j

    open_, high = level.copy(), level + 0.2
    low, close = level - 0.2, level.copy()
    for j, start in enumerate(gaps):
        base = 100.0 + 4.0 * j
        # C1 high = base, C2 displacement, C3 low = base + 2.8.
        open_[start], high[start] = base - 0.1, base
        low[start], close[start] = base - 0.5, base - 0.2
        open_[start + 1], high[start + 1] = base - 0.1, base + 3.2
        low[start + 1], close[start + 1] = base - 0.2, base + 3.0
        open_[start + 2], high[start + 2] = base + 3.0, base + 3.3
        low[start + 2], close[start + 2] = base + 2.8, base + 3.1

    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


def test_present_cut_keeps_the_full_array_and_sweep_inventory():
    df = _inventory_frame()
    cutoff = len(df) - 500
    present = analyze(df, ICTConfig(mode="present"))
    history = analyze(df, ICTConfig(mode="historical"))

    # Present mode must not be a different object set: only the presentation
    # view is windowed (P0-14).
    assert ([(f.created, f.direction, f.top, f.bottom) for f in present.fvgs]
            == [(f.created, f.direction, f.top, f.bottom) for f in history.fvgs])
    assert ([(s.reclaim_bar, s.name, s.level) for s in present.sweeps]
            == [(s.reclaim_bar, s.name, s.level) for s in history.sweeps])

    old_fvg = next(f for f in present.fvgs if f.created == 12)
    old_sweep = next(s for s in present.sweeps if s.reclaim_bar == 25)
    assert old_fvg.created < cutoff
    assert old_sweep.reclaim_bar < cutoff

    view = presentation_view(present)
    assert all(f.created >= cutoff for f in view.fvgs)
    assert all(s.reclaim_bar >= cutoff for s in view.sweeps)
    assert old_fvg.created not in {f.created for f in view.fvgs}
    assert old_sweep.reclaim_bar not in {s.reclaim_bar for s in view.sweeps}


def test_older_eligible_array_is_still_selected():
    df = _many_gaps_frame()
    n = len(df)
    cutoff = n - 500
    result = analyze(df, ICTConfig(mode="present"))
    bullish = [f for f in result.fvgs
               if f.label == "FVG" and f.direction == "bull"]
    oldest = min(bullish, key=lambda f: f.created)

    assert oldest.created == 22, "fixture must create the gap at bar 22"
    assert oldest.created < cutoff, "fixture gap must be outside the 500-bar view"

    # The setup layer's array picker reads the inventory.  At the old gap's CE
    # the nearest live bullish FVG is the old gap, not a newer one.
    box = _BoxIndex(
        bullish, edge_of=lambda f: f.ce,
        fresh_of=lambda f: f.fresh, first_of=lambda f: f.first_presented,
    )
    _, chosen = box.nearest(oldest.ce, n - 1)
    assert chosen is not None
    assert chosen.created == oldest.created

    # The lifecycle inventory sees the same old array instead of dropping it.
    arrays = build_ict_arrays(df, "TESTUSDT", "15m")
    active_fvg = [a for a in arrays if a.array_type == "FVG" and a.is_active]
    assert _times(df)[22] in {a.formed_at for a in active_fvg}


def test_per_kind_caps_are_presentation_only():
    df = _many_gaps_frame()
    times = _times(df)
    arrays = build_ict_arrays(df, "TESTUSDT", "15m")
    active_fvg = [a for a in arrays if a.array_type == "FVG" and a.is_active]

    assert len(active_fvg) == 9, (
        "the inventory must retain every active FVG, including the ninth"
    )
    assert times[22] in {a.formed_at for a in active_fvg}

    presented = present_ict_arrays(arrays)
    presented_fvg = [a for a in presented if a.array_type == "FVG" and a.is_active]
    assert len(presented_fvg) == PRESENTATION_CAPS["FVG"]
    assert times[22] not in {a.formed_at for a in presented_fvg}
    for kind, limit in PRESENTATION_CAPS.items():
        shown = [a for a in presented if a.array_type == kind and a.is_active]
        assert len(shown) <= limit


def test_ifvg_side_mode_is_declared_and_consistent():
    assert IFVG_SIDE_MODE == "by_position"
    assert IFVG_SIDE_MODE in IFVG_SIDE_MODES

    dealing_range = DealingRange(120.0, 100.0)  # EQ 110, q1 105, q3 115
    premium = ifvg_evidence(
        [{"direction": "bullish", "top": 114.0, "bottom": 112.0}],
        dealing_range,
    )
    assert premium is not None
    assert premium["pd_zone"] == "premium"
    assert premium["side"] == "premium"
    assert premium["side_mode"] == IFVG_SIDE_MODE
    assert premium["tradable"] is True

    # Under the declared by-position mode a bullish IFVG in discount is not
    # tradable.  A bearish IFVG in the same discount half is.
    discount_bull = ifvg_evidence(
        [{"direction": "bullish", "top": 108.0, "bottom": 106.0}],
        dealing_range,
    )
    assert discount_bull is not None
    assert discount_bull["pd_zone"] == "discount"
    assert discount_bull["side"] == "discount"
    assert discount_bull["tradable"] is False
    assert "bullish_ifvg_outside_premium" in discount_bull["reasons"]

    discount_bear = ifvg_evidence(
        [{"direction": "bearish", "top": 108.0, "bottom": 106.0}],
        dealing_range,
    )
    assert discount_bear is not None
    assert discount_bear["side"] == "discount"
    assert discount_bear["side_mode"] == IFVG_SIDE_MODE
    assert discount_bear["tradable"] is True

    assert ifvg_side("bullish", "discount") == "discount"
    with pytest.raises(ValueError):
        # The mode is read: an undeclared AS-04 alternative is refused rather
        # than silently relabelled as a by-position tag.
        ifvg_side("bullish", "discount", side_mode="by_type")
