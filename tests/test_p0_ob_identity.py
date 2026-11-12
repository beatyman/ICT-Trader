"""P0-15: order-block identity is recency and its mean is the body midpoint.

Pins the corpus rules named by ``CODE_MAPPING.md`` P0-15:

* AR-15 -- the OB candidate is
  ``t* = max{t < e : sign(close[t] - open[t]) = -leg_sign}``: the **last
  opposite-close candle** before the displacement leg, not the swing-extreme
  bar.  Recency, not size.
* AR-17 -- the mean threshold is ``(body_high + body_low) / 2``; wicks are
  excluded, so it is *not* the zone's CE whenever the zone reaches a wick.
"""
from __future__ import annotations

import pandas as pd
import pytest

from ict_trader.concepts import (
    ICTConfig,
    StructureEvent,
    Swing,
    analyze,
    detect_order_blocks,
)

def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(rows), freq="15min"),
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1000.0] * len(rows),
        }
    )


def _ob(df: pd.DataFrame, event: StructureEvent,
        swings: list[Swing] | None = None):
    return detect_order_blocks(df, [event], swings or [], ICTConfig())


# --------------------------------------------------------------------------- #
# AR-15: the candidate is the last opposite-close candle, not the extreme bar
# --------------------------------------------------------------------------- #
def test_bull_ob_uses_last_down_close_not_the_swing_low():
    rows = [
        (100.0, 101.0, 99.0, 100.5),   # 0 up
        (100.5, 101.0, 90.0, 91.0),    # 1 down + swing low extreme at 90
        (91.0, 95.5, 90.8, 95.0),      # 2 up
        (96.0, 96.5, 94.5, 94.8),      # 3 down: last opposite-close, low 94.5
        (94.9, 101.0, 94.7, 100.5),    # 4 up: displacement break
        (100.5, 102.0, 100.0, 101.5),  # 5 up
    ]
    df = _frame(rows)
    swings = [Swing(1, 90.0, "L", 2)]
    obs = _ob(df, StructureEvent(4, 101.0, "MSS", "bull"), swings)

    assert len(obs) == 1
    ob = obs[0]
    assert ob.direction == "bull"
    # Recency: bar 3 is the last down close, even though bar 1 is the real low.
    assert ob.left == 3, "the candidate must be the last opposite-close candle"
    assert ob.left != 1, "the swing-extreme bar is a different object"
    assert df["close"].iloc[3] < df["open"].iloc[3]
    assert (ob.created, ob.right) == (4, len(df) - 1)
    # Hybrid zone (AR-16): body high on the near side, wick low on the far side.
    assert (ob.top, ob.bottom) == (96.0, 94.5)
    assert (ob.body_high, ob.body_low) == (96.0, 94.8)
    assert ob.mean == pytest.approx((96.0 + 94.8) / 2.0)
    assert ob.mean == pytest.approx((df["open"].iloc[3] + df["close"].iloc[3]) / 2.0)
    assert ob.mean != pytest.approx((ob.top + ob.bottom) / 2.0), (
        "AR-17: the mean must not be the zone's CE when the zone includes a wick"
    )


def test_bear_ob_uses_last_up_close_not_the_swing_high():
    rows = [
        (100.0, 101.0, 99.0, 99.5),    # 0 down
        (99.5, 110.0, 99.0, 109.0),    # 1 up + swing high extreme at 110
        (109.0, 109.5, 104.0, 105.0),  # 2 down
        (105.0, 106.0, 104.5, 105.8),  # 3 up: last opposite-close, high 106
        (105.8, 106.0, 98.5, 99.0),    # 4 down: displacement break
        (99.0, 99.5, 97.5, 98.0),      # 5 down
    ]
    df = _frame(rows)
    swings = [Swing(1, 110.0, "H", 2)]
    obs = _ob(df, StructureEvent(4, 99.0, "MSS", "bear"), swings)

    assert len(obs) == 1
    ob = obs[0]
    assert ob.direction == "bear"
    assert ob.left == 3, "the candidate must be the last opposite-close candle"
    assert ob.left != 1, "the swing-extreme bar is a different object"
    assert df["close"].iloc[3] > df["open"].iloc[3]
    assert (ob.top, ob.bottom) == (106.0, 105.0)
    assert (ob.body_high, ob.body_low) == (105.8, 105.0)
    assert ob.mean == pytest.approx((105.8 + 105.0) / 2.0)
    assert ob.mean != pytest.approx((ob.top + ob.bottom) / 2.0)


# --------------------------------------------------------------------------- #
# AR-17: the mean is the body midpoint; wicks never enter it
# --------------------------------------------------------------------------- #
def test_ob_mean_excludes_both_wicks_of_the_selected_candle():
    rows = [
        (100.0, 101.0, 99.0, 100.5),  # 0
        (100.5, 106.0, 100.0, 105.5), # 1
        (105.0, 130.0, 70.0, 90.0),   # 2 down: huge wicks, body [90, 105]
        (90.0, 111.0, 89.0, 110.0),   # 3 up: displacement break
        (110.0, 112.0, 109.0, 111.0), # 4
    ]
    df = _frame(rows)
    obs = _ob(df, StructureEvent(3, 101.0, "MSS", "bull"),
              [Swing(0, 99.0, "L", 1)])

    assert len(obs) == 1
    ob = obs[0]
    assert ob.left == 2
    assert (ob.top, ob.bottom) == (105.0, 70.0), "zone keeps the far wick"
    assert ob.mean == pytest.approx((105.0 + 90.0) / 2.0)
    assert ob.mean != pytest.approx((ob.top + ob.bottom) / 2.0), "not the zone CE"
    assert ob.mean != pytest.approx((130.0 + 70.0) / 2.0), "not the full range"


def test_a_doji_is_not_an_opposite_close_candle():
    rows = [
        (100.0, 101.0, 99.0, 99.5),    # 0 down: the real candidate
        (100.0, 101.0, 99.0, 100.0),   # 1 doji: sign(close-open) == 0
        (100.0, 104.0, 99.5, 103.0),   # 2 up: displacement break
        (103.0, 105.0, 102.0, 104.0),  # 3 up
    ]
    df = _frame(rows)
    obs = _ob(df, StructureEvent(2, 101.0, "MSS", "bull"))

    assert len(obs) == 1
    assert obs[0].left == 0, "a doji has no sign and cannot be the candidate"
    assert obs[0].mean == pytest.approx((100.0 + 99.5) / 2.0)


def test_no_opposite_close_candle_means_no_ob():
    rows = [
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 103.0, 100.0, 102.5),
        (102.5, 105.0, 102.0, 104.5),
        (104.5, 107.0, 104.0, 106.5),
    ]
    df = _frame(rows)
    obs = _ob(df, StructureEvent(3, 105.0, "MSS", "bull"))

    assert obs == [], "a missing candidate is refused, not replaced by the extreme"


# --------------------------------------------------------------------------- #
# End-to-end: every emitted OB satisfies both rules
# --------------------------------------------------------------------------- #
def test_analyze_emits_only_opposite_close_body_means():
    rows = [
        (100.0, 101.0, 99.0, 99.5),
        (99.5, 100.5, 95.0, 96.0),
        (96.0, 97.0, 95.5, 96.5),
        (96.5, 103.0, 96.0, 102.0),
        (102.0, 103.0, 98.0, 99.0),
        (99.0, 100.0, 94.0, 95.0),
        (95.0, 96.0, 94.5, 95.5),
        (95.5, 102.0, 95.0, 101.0),
        (101.0, 102.0, 97.0, 98.0),
        (98.0, 99.0, 93.0, 94.0),
        (94.0, 95.0, 93.5, 94.5),
        (94.5, 101.0, 94.0, 100.0),
        (100.0, 101.0, 96.0, 97.0),
        (97.0, 98.0, 92.0, 93.0),
        (93.0, 94.0, 92.5, 93.5),
        (93.5, 100.0, 93.0, 99.0),
        (99.0, 104.0, 98.5, 103.0),
        (103.0, 108.0, 102.5, 107.0),
    ]
    df = _frame(rows)
    cfg = ICTConfig(swing_len=2, mode="historical")
    result = analyze(df, cfg)

    assert result.order_blocks, "the synthetic zigzag must produce order blocks"
    for ob in result.order_blocks:
        bar = df.iloc[ob.left]
        body_high = max(float(bar["open"]), float(bar["close"]))
        body_low = min(float(bar["open"]), float(bar["close"]))
        if ob.direction == "bull":
            assert bar["close"] < bar["open"], "leg sign is +1, so close < open"
        else:
            assert bar["close"] > bar["open"], "leg sign is -1, so close > open"
        assert (ob.body_high, ob.body_low) == (body_high, body_low)
        assert ob.mean == pytest.approx((body_high + body_low) / 2.0)
