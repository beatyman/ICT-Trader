"""P0-13 - the declared window must gate the fill bar, not only the signal bar.

``entry_mode="next_open"`` fills at the next bar's open, so a CISD that passes
the window gate on its own bar can still fill after the window has closed.  A
setup whose fill bar is outside the window is refused with the named §12 code
``NT_FILL_OUTSIDE_WINDOW`` and opens no position (EN-04/§8.2, CODE_MAPPING
P0-13).

The cash session used here is the declared ``cn`` table
``[09:30, 11:30) · [13:00, 15:00) Asia/Shanghai``: the 11:30 bar is the
exclusive end of the morning window, so a signal at 11:15 is inside while its
``next_open`` fill at 11:30 is outside.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.setup import NT_FILL_OUTSIDE_WINDOW, SetupConfig, build_setup
from ict_trader.strategy import NT_FILL_OUTSIDE_WINDOW as STRATEGY_CODE
from ict_trader.strategy import StrategyConfig, simulate_cisd

PIVOT_LEFT = 5

#: Bar times (naive Asia/Shanghai, ``market="cn"``) used by the fixtures.
INSIDE_EARLY = 235    # 2025-12-22 10:45 -> signal inside; next_open 11:00 inside
INSIDE_LATE = 237     # 2025-12-22 11:15 -> signal inside; next_open 11:30 outside
OUTSIDE = 238         # 2025-12-22 11:30 -> signal outside (exclusive window end)


def _frame(n: int = 240) -> pd.DataFrame:
    rows = []
    t0 = pd.Timestamp("2025-12-20 00:00")
    for i in range(n):
        close = 200.0 - 0.08 * i + 3.0 * np.sin(i / 7.0)
        rows.append({
            "date": t0 + pd.Timedelta(minutes=15 * i),
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _daily_frame() -> pd.DataFrame:
    rows = []
    t0 = pd.Timestamp("2025-09-01")
    for i in range(120):
        close = 100.0 + 0.4 * i + 8.0 * np.sin(i / 6.0)
        rows.append({
            "date": t0 + pd.Timedelta(days=i),
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _result(df: pd.DataFrame, cisds: list[CISD]) -> SimpleNamespace:
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, PIVOT_LEFT, 1)),
        cisds=cisds,
        fvgs=[],
        order_blocks=[],
        pd_arrays=[],
        sweeps=[],
        key_levels=[],
    )


def _cisd(direction: str, level: float, t: int, target: float | None = None) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=target, grade=3,
        checks={"sweep": False, "pda": False},
    )


def _cfg(**overrides) -> SetupConfig:
    """A permissive config whose only live gate is the declared session window."""
    values = dict(
        market="cn",
        stack="1d_15m",
        allow_short=True,
        min_grade=0,
        min_confluence=0,
        require_1d_cloud=False,
        require_1d_macd=False,
        require_1d_tk=False,
        require_4h_cloud=False,
        require_4h_macd=False,
        require_4h_tk=False,
        require_15m_cloud=False,
        require_15m_macd=False,
        require_15m_hist_rising=False,
        require_sweep=False,
        origin_filter="any",
        htf_fvg=False,
        target_mode="fixed",
        rr=2.0,
        min_target_rr=0.0,
        max_inducements=-1,
        regime_features=False,
        daily_bias_filter="none",
        htf_structure_gate="none",
        entry_mode="next_open",
        min_disp_body_ratio=0.0,
        min_disp_atr=0.0,
        min_disp_multiple=0.0,
        min_stop_atr=0.0,
        min_setup_score=0.0,
        require_pda_array=False,
        require_first_fvg=False,
        require_fresh_fvg=False,
        pd_gate=False,
        ote_gate=False,
        session_gate="cn",
        quality_profile="standard",
    )
    values.update(overrides)
    return SetupConfig(**values)


# ---------------------------------------------------------------------------
# build_setup: signal bar and fill bar are both gated
# ---------------------------------------------------------------------------

def test_signal_inside_but_next_open_fill_outside_is_refused():
    df, daily = _frame(), _daily_frame()
    t = INSIDE_LATE
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _result(df, [c]), _cfg(), daily=daily,
                          refusals=refusals)

    assert signals == []
    assert len(refusals) == 1
    assert refusals[0]["code"] == NT_FILL_OUTSIDE_WINDOW
    assert refusals[0]["signal_bar"] == INSIDE_LATE
    assert refusals[0]["fill_bar"] == OUTSIDE


def test_signal_and_next_open_fill_inside_the_window_are_allowed():
    df, daily = _frame(), _daily_frame()
    t = INSIDE_EARLY
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _result(df, [c]), _cfg(), daily=daily,
                          refusals=refusals)

    assert len(signals) == 1
    assert signals[0].end == t
    assert refusals == []
    # The optional sink is additive: the plain call still returns the setup.
    assert len(build_setup(df, _result(df, [c]), _cfg(), daily=daily)) == 1


def test_signal_bar_outside_the_window_is_refused():
    df, daily = _frame(), _daily_frame()
    t = OUTSIDE
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _result(df, [c]), _cfg(), daily=daily,
                          refusals=refusals)

    assert signals == []
    # The signal bar itself is the failing gate: this is not a late fill.
    assert [r["code"] for r in refusals] == []


def test_fill_gate_only_applies_to_next_open_entries():
    # A retest-family entry fills on a later bar found by the simulator, so
    # build_setup cannot judge its fill bar and must not refuse it here; the
    # simulator gates the actual fill bar instead (test below).
    df, daily = _frame(), _daily_frame()
    t = INSIDE_LATE
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _result(df, [c]), _cfg(entry_mode="retest_key"),
                          daily=daily, refusals=refusals)

    assert len(signals) == 1
    assert refusals == []


def test_the_refusal_code_has_one_spelling_across_setup_and_strategy():
    assert NT_FILL_OUTSIDE_WINDOW == "NT_FILL_OUTSIDE_WINDOW"
    assert NT_FILL_OUTSIDE_WINDOW is STRATEGY_CODE


# ---------------------------------------------------------------------------
# simulate_cisd: the actual fill bar is gated too (all entry modes)
# ---------------------------------------------------------------------------

def _sim(c: CISD, **overrides):
    df = _frame()
    base = dict(
        min_grade=0, min_confluence=0, entry_mode="next_open",
        entry_delay=1, min_rr=0.0, commission_bps=0.0, slippage_bps=0.0,
        allow_short=True, session_gate="cn", market="cn",
    )
    base.update(overrides)
    result = SimpleNamespace(cisds=[c], htf_cisds=[])
    return simulate_cisd(df, result, StrategyConfig(**base), "test")


def test_simulator_refuses_a_next_open_fill_outside_the_window():
    df = _frame()
    c = _cisd("bull", float(df["close"].iloc[INSIDE_LATE]), INSIDE_LATE,
              target=float(df["close"].iloc[INSIDE_LATE]) + 3.0)

    r = _sim(c)

    assert r.trades == []
    assert r.metrics["refused_fill_outside_window"] == 1
    assert r.metrics["signals"] == 1


def test_simulator_allows_a_fill_inside_the_window():
    df = _frame()
    c = _cisd("bull", float(df["close"].iloc[INSIDE_EARLY]), INSIDE_EARLY,
              target=float(df["close"].iloc[INSIDE_EARLY]) + 3.0)

    r = _sim(c)

    assert len(r.trades) == 1
    assert r.metrics["refused_fill_outside_window"] == 0
    assert r.trades[0].entry_time == df["date"].iloc[INSIDE_EARLY + 1]
