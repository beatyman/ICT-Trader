"""P0-06 regressions: one declared stop policy, one anchor, a real buffer.

Invariants (CODE_MAPPING P0-06; RK-01/RK-04/RK-06, X1, ``NT_STOP_ANCHOR_UNRESOLVED``):

* a stop is built from exactly **one** anchor selected by the declared
  ``STOP_POLICY`` order, never from a union of every available anchor;
* the stop sits beyond that anchor by the declared buffer, and a negative pad
  cannot move it inside the anchor;
* no resolvable anchor is a refusal, not a fabricated price;
* the wired ``CISD.sl`` is no longer placed exactly on the pivot.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISDConfig, detect_cisd
from ict_trader.realtime.analysis.risk import (
    STOP_ANCHOR_UNRESOLVED,
    STOP_POLICY,
    StopAnchorUnresolved,
    StopPolicy,
    build_risk_plan,
    structural_stop,
)


# --------------------------------------------------------------------------- #
# declaration + single-anchor buffer
# --------------------------------------------------------------------------- #
def test_stop_policy_is_declared_with_a_non_negative_buffer():
    assert STOP_POLICY.anchors[0] == "sweep_extreme"
    assert STOP_POLICY.buffer_atr >= 0
    with pytest.raises(ValueError):
        StopPolicy(buffer_atr=-0.01)


def test_stop_sits_beyond_the_selected_anchor_by_the_declared_buffer():
    # 97.2 is the sweep extreme (the wick), not the swept level (98.0).
    stop = structural_stop("long", entry=100.0, atr=2.0,
                           sweep={"level": 98.0, "extreme": 97.2},
                           structure=None)
    assert stop == pytest.approx(97.2 - STOP_POLICY.buffer_atr * 2.0)
    assert stop < 97.2

    mirror = structural_stop("short", entry=100.0, atr=2.0,
                             sweep={"level": 102.0, "extreme": 102.8},
                             structure=None)
    assert mirror == pytest.approx(102.8 + STOP_POLICY.buffer_atr * 2.0)
    assert mirror > 102.8


def test_declared_policy_uses_one_anchor_not_the_outermost_union():
    stop = structural_stop("long", entry=100.0, atr=1.0,
                           sweep={"level": 98.0, "extreme": 97.8},
                           structure=None, ob={"bottom": 96.5, "top": 99.0})
    # Sweep extreme wins by declaration; the farther OB bottom is ignored.
    assert stop == pytest.approx(97.8 - STOP_POLICY.buffer_atr)
    assert stop > 96.5


def test_negative_buffer_is_clamped_and_cannot_enter_the_anchor():
    stop = structural_stop("long", entry=100.0, atr=1.0,
                           sweep={"level": 98.0, "extreme": 97.2},
                           structure=None, pad_atr=-5.0, floor_atr=0.0)
    assert stop == pytest.approx(97.2)  # buffer clamped to 0, never inside
    assert stop <= 97.2


# --------------------------------------------------------------------------- #
# no anchor: refuse, never synthesise a price
# --------------------------------------------------------------------------- #
def test_stop_without_a_resolvable_anchor_refuses():
    # A legacy level-only sweep is not an anchor: RK-01 places the stop beyond
    # the wick, never on the swept level.
    with pytest.raises(StopAnchorUnresolved) as excinfo:
        structural_stop("long", entry=100.0, atr=1.0,
                        sweep={"level": 98.0}, structure=None)
    assert excinfo.value.code == STOP_ANCHOR_UNRESOLVED

    plan = build_risk_plan("long", entry=100.0, atr=1.0,
                           sweep={"level": 98.0}, structure=None, pools=[108.0])
    assert plan.blockers == (STOP_ANCHOR_UNRESOLVED,)
    assert plan.stop is None
    assert plan.quantity == 0.0
    assert plan.targets == ()


# --------------------------------------------------------------------------- #
# the wired CISD stop
# --------------------------------------------------------------------------- #
def _cisd_frame() -> pd.DataFrame:
    """A bearish leg into 97.5, then a bullish close back through 100.0."""
    rows = [
        (100.0, 100.5, 99.0, 99.5),
        (99.5, 99.8, 98.0, 98.5),
        (98.5, 98.9, 97.5, 98.0),   # pivot / invalidation extreme = 97.5
        (98.0, 101.0, 97.9, 100.5),  # close > 100.0 confirms the bull CISD
        (100.5, 101.5, 100.2, 101.0),
    ]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["date"] = pd.date_range("2026-01-01", periods=len(df), freq="15min")
    df["volume"] = 1000.0
    return df


def _atr_at(df: pd.DataFrame, idx: int) -> float:
    """The ATR series ``detect_cisd`` uses (recomputed independently here)."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - np.r_[np.nan, close[:-1]]),
        np.abs(low - np.r_[np.nan, close[:-1]]),
    ])
    return float(pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy()[idx])


def test_wired_cisd_stop_is_not_at_the_pivot():
    df = _cisd_frame()
    out = detect_cisd(df, CISDConfig(require_origin=False, min_grade=0,
                                     max_per_side=3))
    assert len(out) == 1
    c = out[0]
    assert c.direction == "bull"
    # ``CISD.extreme`` carries the pivot the old constructor stopped on.
    assert c.extreme == pytest.approx(97.5)
    assert c.sl != c.extreme
    assert c.sl < c.extreme
    assert c.sl == pytest.approx(
        c.extreme - STOP_POLICY.buffer_atr * _atr_at(df, c.end))
