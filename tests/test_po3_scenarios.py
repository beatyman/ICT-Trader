"""PO3 state-machine expiry and the A>B>C>D arbitration on real BTC findings."""
import numpy as np
import pandas as pd
import pytest

from ict_trader.concepts import ICTConfig, detect_fvgs
from ict_trader.po3 import ACCUMULATING, DISTRIBUTING, INVALID, MANIPULATED, detect_po3
from ict_trader.scenarios import _dol, evaluate_scenarios
from ict_trader.sweeps import KeyLevel


def frame(spike_high=101.0, spike_close=104.0, distribute=False, n=60) -> pd.DataFrame:
    """10 flat bars 100-110, one sweep bar below 100, then calm bars."""
    rows = []
    for i in range(10):
        rows.append({"open": 105.0, "high": 110.0, "low": 100.0, "close": 105.0})
    rows.append({"open": 104.0, "high": spike_high, "low": 98.0, "close": spike_close})
    for i in range(n - 11):
        if distribute and i == 0:
            rows.append({"open": 106.0, "high": 114.0, "low": 106.0, "close": 113.0})
        else:
            rows.append({"open": 104.0, "high": 108.0, "low": 101.0, "close": 104.0})
    df = pd.DataFrame(rows)
    df.insert(0, "date", pd.date_range("2026-01-01", periods=len(df), freq="15min"))
    df["volume"] = 100.0
    return df


def test_manipulated_state_expires_without_distribution():
    df = frame()
    st, _ = detect_po3(df, accumulate_bars=10)
    assert st.state == MANIPULATED
    st2, phases = detect_po3(df, accumulate_bars=10, max_manipulation_bars=5)
    assert st2.state == INVALID
    assert "expired" in (st2.invalid_reason or "")
    assert phases[-1] == INVALID


def test_distributing_requires_mss_inside_window():
    df = frame(distribute=True, n=60)
    st, _ = detect_po3(df, accumulate_bars=10, mss_window=20)
    assert st.state == INVALID  # no MSS feed -> expired
    st2, _ = detect_po3(df, accumulate_bars=10, mss_window=20, mss_events=[(14, "bull")])
    assert st2.state == DISTRIBUTING
    st3, _ = detect_po3(df, accumulate_bars=10, mss_window=20, mss_events=[(14, "bear")])
    assert st3.state == INVALID


def test_shallow_spike_reclaim_is_not_a_manipulation():
    # closes back inside the range by only ~18% of its own range
    df = frame(spike_high=110.0, spike_close=100.2)
    st, _ = detect_po3(df, accumulate_bars=10)
    assert st.state == MANIPULATED
    st2, _ = detect_po3(df, accumulate_bars=10, min_reclaim_ratio=0.5)
    assert st2.state == ACCUMULATING


class _Res:
    def __init__(self, levels=(), sweeps=(), cisds=()):
        self.key_levels = list(levels)
        self.sweeps = list(sweeps)
        self.cisds = list(cisds)
        self.fvgs = []
        self.bars = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=10, freq="15min")})


class _Sweep:
    def __init__(self, level, extreme, reclaim_bar=8, direction="bear"):
        self.level = level
        self.sweep_price = extreme
        self.reclaim_bar = reclaim_bar
        self.direction = direction


class _PO3:
    def __init__(self, state, sweep_direction="bull", cisd_confirmed=False):
        self.state = state
        self.sweep_direction = sweep_direction
        self.cisd_confirmed = cisd_confirmed
        self.distribution_bar = 1
        self.range_high = 110.0
        self.range_low = 100.0
        self.manipulation_extreme = 98.0


def _audit() -> dict:
    return {"rules": [{"rule_id": "L5-03", "status": "FAIL", "value": {}},
                      {"rule_id": "L1-04", "status": "PASS", "value": {}},
                      {"rule_id": "L2-04", "status": "PASS", "value": {}},
                      {"rule_id": "L2-08", "status": "PASS", "value": {}},
                      {"rule_id": "L3-01", "status": "PASS", "value": {}}]}


def test_rejected_plan_does_not_suppress_a_live_lower_priority(monkeypatch):
    import ict_trader.scenarios as sc

    monkeypatch.setattr(sc, "_plan_a", lambda po3, atr, min_rr: {
        "scenario": "A", "direction": "long", "entry": 110.0, "sl": 109.0, "tp": 112.0,
        "status": "REJECTED_LOW_RR", "gross_rr": 2.0, "net_rr": 1.1, "reason": "low rr"})
    monkeypatch.setattr(sc, "_plan_b", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_c", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_d", lambda *a, **k: {
        "scenario": "D", "direction": "short", "entry": 100.0, "sl": 103.0, "tp": 92.0,
        "status": "CONDITIONAL", "gross_rr": 2.7, "net_rr": 2.4, "reason": "fakeout"})

    sid, plan = evaluate_scenarios(_audit(), _PO3(DISTRIBUTING), _Res(), atr=1.0, price=101.0)
    assert sid == "D"
    assert plan.status == "CONDITIONAL"
    assert plan.suppressed == ["A"]


def test_priority_order_kept_when_both_are_live(monkeypatch):
    import ict_trader.scenarios as sc

    monkeypatch.setattr(sc, "_plan_a", lambda po3, atr, min_rr: {
        "scenario": "A", "direction": "long", "entry": 110.0, "sl": 108.0, "tp": 114.0,
        "status": "CONDITIONAL", "gross_rr": 2.0, "net_rr": 2.2, "reason": "po3"})
    monkeypatch.setattr(sc, "_plan_b", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_c", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_d", lambda *a, **k: {
        "scenario": "D", "direction": "short", "entry": 100.0, "sl": 103.0, "tp": 92.0,
        "status": "CONDITIONAL", "gross_rr": 2.7, "net_rr": 2.4, "reason": "fakeout"})

    sid, plan = evaluate_scenarios(_audit(), _PO3(DISTRIBUTING), _Res(), atr=1.0, price=101.0)
    assert sid == "A"
    assert plan.suppressed == ["D"]


def test_po3_invalid_vetoes_the_po3_scenarios_only(monkeypatch):
    import ict_trader.scenarios as sc

    monkeypatch.setattr(sc, "_plan_a", lambda po3, atr, min_rr: {
        "scenario": "A", "direction": "long", "entry": 110.0, "sl": 108.0, "tp": 114.0,
        "status": "CONDITIONAL", "gross_rr": 2.0, "net_rr": 2.2, "reason": "po3"})
    monkeypatch.setattr(sc, "_plan_b", lambda *a, **k: {
        "scenario": "B", "direction": "short", "entry": 100.0, "sl": 102.0, "tp": 95.0,
        "status": "CONDITIONAL", "gross_rr": 2.5, "net_rr": 2.3, "reason": "poi"})
    monkeypatch.setattr(sc, "_plan_c", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_d", lambda *a, **k: {
        "scenario": "D", "direction": "short", "entry": 100.0, "sl": 103.0, "tp": 92.0,
        "status": "CONDITIONAL", "gross_rr": 2.7, "net_rr": 2.4, "reason": "fakeout"})

    # INVALID PO3 drops A and B (PO3-derived) but D is a non-PO3 range model.
    sid, plan = evaluate_scenarios(_audit(), _PO3(INVALID), _Res(), atr=1.0, price=101.0)
    assert sid == "D"
    assert plan.suppressed == []

    # HARD veto: nothing may trade when the caller wants a strict PO3 gate.
    sid2, plan2 = evaluate_scenarios(_audit(), _PO3(INVALID), _Res(), atr=1.0, price=101.0,
                                     po3_veto_scenarios=("A", "B", "C", "D"))
    assert sid2 is None and plan2 is None


def test_tight_stop_is_entry_invalid(monkeypatch):
    import ict_trader.scenarios as sc

    monkeypatch.setattr(sc, "_plan_a", lambda po3, atr, min_rr: None)
    monkeypatch.setattr(sc, "_plan_b", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_c", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_plan_d", lambda *a, **k: {
        "scenario": "D", "direction": "short", "entry": 100.0, "sl": 100.2, "tp": 90.0,
        "status": "CONDITIONAL", "gross_rr": 50.0, "net_rr": 40.0, "reason": "fakeout"})

    _, plan = evaluate_scenarios(_audit(), _PO3(MANIPULATED), _Res(), atr=1.0, price=100.0)
    assert plan.status == "REJECTED_ENTRY_INVALID"


def test_dol_ignores_levels_confirmed_after_the_decision_bar():
    levels = [KeyLevel("BSL", 120.0, "H", known_at=50),
              KeyLevel("BSL", 130.0, "H", known_at=500)]
    res = _Res(levels=levels)
    assert _dol(res, 100.0, "long", at=100) == 120.0
    assert _dol(res, 100.0, "long", at=40) is None


def test_fvg_width_filter():
    n = 12
    dates = pd.date_range("2026-01-01", periods=n, freq="15min")
    df = pd.DataFrame({
        "date": dates,
        "open": [100.0] * n, "high": [100.4] * n, "low": [99.6] * n,
        "close": [100.1] * n, "volume": [100.0] * n,
    })
    # bar 2 low 100.2 > bar 0 high 100.4? no: build an explicit 0.2-wide gap
    df.loc[0, ["high", "open", "close", "low"]] = [100.0, 99.8, 99.9, 99.6]
    df.loc[1, ["high", "open", "close", "low"]] = [100.6, 99.9, 100.5, 99.9]
    df.loc[2, ["high", "open", "close", "low"]] = [101.0, 100.5, 100.8, 100.2]
    base = detect_fvgs(df, ICTConfig(fvg_min_width_atr=0.0, displacement_filter=False))
    assert base, "expected the narrow gap to be detected"
    filtered = detect_fvgs(df, ICTConfig(fvg_min_width_atr=10.0, displacement_filter=False))
    assert not filtered
