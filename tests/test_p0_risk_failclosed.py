"""P0-04 regression tests: the risk plan fails closed.

Invariants (CODE_MAPPING P0-04, SYSTEM_SPEC §1.5/X3/X18/§12/A3):

* a plan with any blocker carries zero size and an empty target ladder;
* a plan with no structural anchor refuses instead of fabricating a ``1xATR``
  stop, and its refusal is distinguishable from the other refusal reasons;
* a valid plan is still sized with its target ladder.
"""
from __future__ import annotations

import pytest

from ict_trader.realtime.analysis.risk import (
    STOP_ANCHOR_UNRESOLVED,
    StopAnchorUnresolved,
    build_risk_plan,
    structural_stop,
)

# --------------------------------------------------------------------------- #
# blocked plans: diagnostics survive, size and ladder do not
# --------------------------------------------------------------------------- #
def test_blocked_plan_has_zero_size_and_an_empty_target_ladder():
    # A real anchor stop (2.5 ATR away) with a target only 0.7 away: the plan is
    # blocked on net RR, so neither size nor the ladder may be emitted.
    plan = build_risk_plan("long", entry=100.0, atr=2.0,
                           sweep={"level": 98.0, "extreme": 98.0},
                           structure=None, pools=[100.9])
    assert plan.blockers == ("net_rr_below_2",)
    assert plan.quantity == 0.0
    assert plan.targets == ()
    assert plan.stop == pytest.approx(97.5)  # the anchor stop is still reported


def test_out_of_bounds_stop_is_blocked_and_sized_zero():
    # The anchor sits 3.5 ATR away, outside the repo's 1-3 ATR band.
    plan = build_risk_plan("long", entry=100.0, atr=2.0,
                           sweep={"level": 93.5, "extreme": 93.5},
                           structure=None, pools=[120.0])
    assert plan.blockers == ("sl_atr_out_of_bounds",)
    assert plan.quantity == 0.0
    assert plan.targets == ()


# --------------------------------------------------------------------------- #
# no anchor: refuse, never synthesise entry -/+ 1xATR
# --------------------------------------------------------------------------- #
def test_no_anchor_plan_refuses_distinctly_without_a_fabricated_stop():
    plan = build_risk_plan("long", entry=100.0, atr=2.0, sweep=None,
                           structure=None, pools=[108.0])
    assert plan.blockers == (STOP_ANCHOR_UNRESOLVED,)
    assert plan.blockers == ("NT_STOP_ANCHOR_UNRESOLVED",)
    # not the fabricated fallback (entry - 1xATR = 98.0), and not a price at all
    assert plan.stop is None
    assert plan.quantity == 0.0
    assert plan.targets == ()
    # the refusal names the missing anchor, not a target/liquidity reason
    assert plan.blockers != ("no_liquidity_target",)
    assert plan.blockers != ("atr_invalid",)


def test_structural_stop_refuses_when_no_anchor_exists():
    with pytest.raises(StopAnchorUnresolved) as excinfo:
        structural_stop("short", entry=100.0, atr=2.0, sweep=None, structure=None)
    assert excinfo.value.code == STOP_ANCHOR_UNRESOLVED


def test_invalid_atr_refusal_authors_no_stop_price():
    plan = build_risk_plan("long", entry=100.0, atr=0.0,
                           sweep={"level": 98.0}, structure=None, pools=[108.0])
    assert plan.blockers == ("atr_invalid",)
    assert plan.stop is None
    assert plan.quantity == 0.0
    assert plan.targets == ()


# --------------------------------------------------------------------------- #
# valid plan still emits size and the target ladder
# --------------------------------------------------------------------------- #
def test_valid_plan_is_sized_and_carries_its_ladder():
    plan = build_risk_plan("long", entry=100.0, atr=2.0,
                           sweep={"level": 98.0, "extreme": 98.0},
                           structure={"price": 98.0}, pools=[108.0, 112.0])
    assert plan.blockers == ()
    assert plan.stop == pytest.approx(97.5)
    assert plan.targets == (108.0, 112.0)
    assert plan.quantity == pytest.approx(200.0)  # equity * risk_pct / risk
    assert plan.gross_rr > plan.net_rr >= 2.0
