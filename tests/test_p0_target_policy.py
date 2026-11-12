"""P0-05 regressions: target authorship, ceiling, ladder.

Invariants (CODE_MAPPING P0-05; TG-01/TG-02/TG-07/TG-11, SYSTEM_SPEC §12):

* a plan with no target is refused; the gate never manufactures one from the
  RR floor and configured ``rr`` cannot rescue it;
* a target nearer than the declared floor is refused with
  ``NT_TARGET_TOO_NEAR``, and a farther target may not be used to pass the gate
  on that nearer setup (TG-07);
* ``target_mode`` is a declared family whose default is the corpus liquidity
  selector, not ``fixed`` + 2R;
* the invented ``max_target_rr`` ceiling no longer drops a valid far target;
* ``RiskPlan`` carries the full target ladder instead of the first three rungs.
"""
from __future__ import annotations

import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.realtime.analysis.risk import build_risk_plan
from ict_trader.setup import (
    DEFAULT_TARGET_MODE,
    TARGET_MODES,
    SetupConfig,
    select_target,
)
from ict_trader.strategy import (
    TARGET_MISSING_CODE,
    TARGET_TOO_NEAR_CODE,
    StrategyConfig,
    simulate_cisd,
)
from ict_trader.sweeps import KeyLevel


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _frame(prices) -> pd.DataFrame:
    n = len(prices)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": prices,
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
        "close": prices,
        "volume": [1000] * n,
    })


def _simulate(target, *, min_rr: float = 1.2, rr: float = 2.0):
    df = _frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=target, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    return simulate_cisd(
        df, res,
        StrategyConfig(min_grade=0, min_confluence=0, entry_delay=1,
                       max_bars=5, min_rr=min_rr, rr=rr),
        "test",
    )


# --------------------------------------------------------------------------- #
# 1. no target: refuse, never synthesise from the RR floor
# --------------------------------------------------------------------------- #
def test_missing_target_is_refused_not_synthesised_from_rr():
    assert TARGET_MISSING_CODE == "NT_DATA_MISSING"
    assert TARGET_TOO_NEAR_CODE == "NT_TARGET_TOO_NEAR"
    r = _simulate(None, min_rr=1.0, rr=2.0)
    assert r.trades == []
    assert r.metrics["signals"] == 1
    assert r.metrics["refused_target_missing"] == 1
    # The (unused) fixed-R fallback would have produced a trade around 2R.
    assert r.metrics["refused_target_too_near"] == 0


# --------------------------------------------------------------------------- #
# 2. a target below the floor is refused, not replaced
# --------------------------------------------------------------------------- #
def test_target_below_the_floor_is_refused_not_replaced():
    # target 103 is ~0.4R from the fill while the declared floor is 2R; the old
    # gate manufactured entry + 2R instead of refusing.
    r = _simulate(103.0, min_rr=2.0)
    assert r.trades == []
    assert r.metrics["refused_target_too_near"] == 1


def test_target_on_the_wrong_side_of_the_fill_is_refused():
    r = _simulate(98.0, min_rr=1.0)
    assert r.trades == []
    assert r.metrics["refused_target_wrong_side"] == 1


# --------------------------------------------------------------------------- #
# 3. target_mode is a declaration, not an implicit fixed+2R default
# --------------------------------------------------------------------------- #
def test_target_mode_is_declared_and_defaults_to_liquidity():
    assert DEFAULT_TARGET_MODE == "liquidity"
    assert SetupConfig().target_mode == DEFAULT_TARGET_MODE
    assert set(TARGET_MODES) == {"fixed", "liquidity", "hybrid", "dol", "htf_dol"}
    with pytest.raises(ValueError):
        SetupConfig(target_mode="2r")


def test_fixed_mode_authors_its_target_in_setup_not_in_the_gate():
    cfg = SetupConfig(target_mode="fixed", rr=2.5)
    assert select_target(cfg, "bull", 100.0, 95.0) == pytest.approx(112.5)
    assert select_target(cfg, "bear", 100.0, 105.0) == pytest.approx(87.5)


def test_hybrid_falls_back_to_fixed_while_liquidity_refuses():
    liq = SetupConfig(target_mode="liquidity")
    hyb = SetupConfig(target_mode="hybrid", rr=2.0)
    assert select_target(liq, "bull", 100.0, 95.0, [], at=1) is None
    assert select_target(hyb, "bull", 100.0, 95.0, [], at=1) == pytest.approx(110.0)


def test_htf_dol_prefers_time_anchored_levels_then_falls_back():
    cfg = SetupConfig(target_mode="htf_dol")
    swing = KeyLevel("BSL", 110.0, "H", known_at=0, source="swing")
    session = KeyLevel("PDH", 120.0, "H", known_at=0, source="session")
    assert select_target(cfg, "bull", 100.0, 95.0, [swing, session], at=1) == 120.0
    # No anchored level: the ordinary draw inventory is still admissible.
    assert select_target(cfg, "bull", 100.0, 95.0, [swing], at=1) == 110.0


def test_target_inventory_respects_look_ahead_and_lifecycle():
    cfg = SetupConfig(target_mode="liquidity")
    future = KeyLevel("EQH", 130.0, "H", known_at=5)
    swept = KeyLevel("EQH", 108.0, "H", known_at=0, sweep_bar=1)
    live = KeyLevel("EQH", 112.0, "H", known_at=0)
    # Swept only *after* the signal bar: still admissible at bar 2 (TG-03).
    future_sweep = KeyLevel("EQH", 111.0, "H", known_at=0, swept=True, sweep_bar=5)
    assert select_target(cfg, "bull", 100.0, 95.0,
                         [future, swept, live, future_sweep], at=2) == 111.0


# --------------------------------------------------------------------------- #
# 4. no invented upper RR ceiling; far targets survive
# --------------------------------------------------------------------------- #
def test_far_target_above_the_old_six_r_ceiling_is_kept():
    # The deleted ceiling was max_target_rr = 6.0; 150 on a 5-point risk is 10R.
    cfg = SetupConfig(target_mode="liquidity", max_target_rr=6.0)
    far = KeyLevel("EQH", 150.0, "H", known_at=0, source="swing")
    assert select_target(cfg, "bull", 100.0, 95.0, [far], at=1) == 150.0


def test_nearest_structural_target_wins_even_when_it_is_below_the_floor():
    # TG-07: a far target may not be used to pass the RR gate on a nearer
    # setup.  Selection returns the nearest pool; the gate then refuses it.
    cfg = SetupConfig(target_mode="liquidity")
    near = KeyLevel("EQH", 101.0, "H", known_at=0)
    far = KeyLevel("EQH", 150.0, "H", known_at=0)
    assert select_target(cfg, "bull", 100.0, 95.0, [near, far], at=1) == 101.0


# --------------------------------------------------------------------------- #
# 5. the ladder is complete, not sliced to three
# --------------------------------------------------------------------------- #
def test_risk_plan_returns_the_full_target_ladder():
    pools = [108.0, 109.0, 110.0, 111.0, 112.0, 113.0]
    plan = build_risk_plan("long", entry=100.0, atr=2.0,
                           sweep={"level": 98.0, "extreme": 98.0},
                           structure=None, pools=pools)
    assert plan.blockers == ()
    assert plan.targets == tuple(pools)
    assert len(plan.targets) == len(pools) > 3


def test_short_ladder_is_full_and_nearest_first():
    pools = [92.0, 90.0, 88.0, 86.0, 84.0]
    plan = build_risk_plan("short", entry=100.0, atr=2.0,
                           sweep={"level": 102.0, "extreme": 102.0},
                           structure=None, pools=pools)
    assert plan.blockers == ()
    assert plan.targets == tuple(pools)
    assert len(plan.targets) == len(pools) > 3
