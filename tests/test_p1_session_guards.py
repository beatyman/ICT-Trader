"""P1-16 -- session guards: one attempt per window and the killzone-close fill.

``CODE_MAPPING.md`` P1-16 asks for the two session rules the checkout lacked
(``EN-03``/``EN-04``, ``GT-06``; ``RE-T11`` extension):

* **one attempt per window** -- ``EN-04``/``LT-B09``: one attempt per window and
  a stop-out ends the window, with the softer variant allowing 1-2.  A second
  attempt in the same window occurrence is refused with ``NT_ONE_ATTEMPT_USED``.
* **fill deadline at the kill-zone close** -- ``GT-06``/``R39``/``TM-43``: a
  resting limit is cancelled at the enclosing killzone's close.  The deadline is
  a clock, never the pre-P1-16 ``retest_max_bars`` bar count.

Both read the one P1-10 window table through
:func:`ict_trader.context.killzone_occurrence`; no window is invented, and a
signal outside every declared killzone has no deadline to be given one, so the
guard refuses it (``EN-05``/``LT-B02``).  Every number asserted below is quoted
from the spec's §10.2/§13.7 rows or from the cited rule; the only declared
choice the corpus leaves open (1 or 1-2 attempts) is an explicit config field.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    enclosing_killzone,
    killzone_occurrence,
    window_close_reached,
    window_occurrence,
    window_spec,
)
from ict_trader.setup import SetupConfig, build_setup
from ict_trader.strategy import (
    ATTEMPTS_PER_WINDOW,
    NT_FILL_TIMEOUT,
    NT_ONE_ATTEMPT_USED,
    NT_OUT_OF_WINDOW,
    StrategyConfig,
    WindowAttemptBudget,
    simulate_cisd,
)
from ict_trader.variants import RefusalCode

NY = "America/New_York"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
# -- context ---------------------------------------------------------------- #
def _clock(stamp: str) -> pd.Timestamp:
    return pd.Timestamp(stamp, tz=NY)


def _sim_frame(*, n: int = 24, start: str = "2026-07-15 07:00") -> pd.DataFrame:
    """A flat 15-minute NY frame; individual bars are overridden per test."""
    t0 = pd.Timestamp(start, tz=NY)
    return pd.DataFrame({
        "date": [t0 + pd.Timedelta(minutes=15 * i) for i in range(n)],
        "open": [100.0] * n,
        "high": [100.3] * n,
        "low": [99.7] * n,
        "close": [100.0] * n,
        "volume": [1.0] * n,
    })


def _signal(direction: str = "bull", *, level: float = 100.0,
            sl: float = 99.0, target: float = 101.0, t: int = 0) -> CISD:
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=target, grade=3,
        checks={"sweep": False, "pda": False},
    )


def _simulate(cisds: list[CISD], frame: pd.DataFrame, **overrides):
    base = dict(
        min_grade=0, min_confluence=0, entry_delay=1, max_bars=20,
        min_rr=0.0, commission_bps=0.0, slippage_bps=0.0,
        allow_short=True, market="other", session_guard=True,
        max_attempts_per_window=1,
    )
    base.update(overrides)
    result = SimpleNamespace(cisds=cisds, htf_cisds=[])
    return simulate_cisd(frame, result, StrategyConfig(**base), "test")


def _first_trade_frame(*, n: int = 24) -> pd.DataFrame:
    """07:00 frame where a 07:00 signal enters at 07:15 and hits 101 at once."""
    frame = _sim_frame(n=n)
    frame.loc[1, ["high", "close"]] = [101.6, 101.2]
    frame.loc[13, ["high", "close"]] = [101.6, 101.2]
    return frame


# -- setup surface ---------------------------------------------------------- #
def _setup_frame(n: int = 360) -> pd.DataFrame:
    """P1-10's fixture: 15m NY bars from 2026-01-02 00:00.

    The guard tests read 2026-01-05's NY AM occurrence (07:00-10:00), which the
    frame covers: 07:45 is index 319, 08:00 is 320 and 09:45 is 327.
    """
    rows = []
    t0 = pd.Timestamp("2026-01-02 00:00", tz=NY)
    for i in range(n):
        close = 200.0 - 0.05 * i + 2.0 * np.sin(i / 7.0)
        rows.append({
            "date": t0 + pd.Timedelta(minutes=15 * i),
            "open": close - 0.1, "high": close + 0.3,
            "low": close - 0.3, "close": close, "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _daily_frame() -> pd.DataFrame:
    rows = []
    t0 = pd.Timestamp("2025-09-01 16:00", tz=NY)
    for i in range(200):
        close = 100.0 + 0.4 * i + 8.0 * np.sin(i / 6.0)
        rows.append({
            "date": t0 + pd.Timedelta(days=i),
            "open": close - 0.1, "high": close + 0.3,
            "low": close - 0.3, "close": close, "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _setup_result(df: pd.DataFrame, cisds: list[CISD]):
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)),
        cisds=cisds, fvgs=[], order_blocks=[], pd_arrays=[], sweeps=[],
        key_levels=[],
    )


def _setup_signal(direction: str, level: float, t: int) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
    )


def _setup_cfg(**overrides) -> SetupConfig:
    values = dict(
        market="other", stack="1d_15m", allow_short=True,
        min_grade=0, min_confluence=0,
        require_1d_cloud=False, require_1d_macd=False, require_1d_tk=False,
        require_4h_cloud=False, require_4h_macd=False, require_4h_tk=False,
        require_15m_cloud=False, require_15m_macd=False,
        require_15m_hist_rising=False, require_sweep=False,
        origin_filter="any", htf_fvg=False, target_mode="fixed", rr=2.0,
        min_target_rr=0.0, max_inducements=-1, regime_features=False,
        daily_bias_filter="none", htf_structure_gate="none",
        entry_mode="next_open", min_disp_body_ratio=0.0, min_disp_atr=0.0,
        min_disp_multiple=0.0, min_stop_atr=0.0, min_setup_score=0.0,
        require_pda_array=False, require_first_fvg=False,
        require_fresh_fvg=False, pd_gate=False, ote_gate=False,
        session_gate="none", dead_zone_gate=False, quality_profile="standard",
        session_guard=True, max_attempts_per_window=1,
    )
    values.update(overrides)
    return SetupConfig(**values)


def _bar_index(df: pd.DataFrame, stamp: str) -> int:
    return int(df.index[df["date"] == _clock(stamp)][0])


# --------------------------------------------------------------------------- #
# 1. The enclosing killzone occurrence and its close instant
# --------------------------------------------------------------------------- #
def test_enclosing_killzone_is_the_one_declared_row():
    assert enclosing_killzone(_clock("2026-07-15 03:00")).name == "london"
    assert enclosing_killzone(_clock("2026-07-15 08:00")).name == "ny_am"
    assert enclosing_killzone(_clock("2026-07-15 23:45")).name == "asian"
    # 05:30 is the declared dead zone and is not a killzone row.
    assert enclosing_killzone(_clock("2026-07-15 05:30")) is None
    assert enclosing_killzone(_clock("2026-07-15 12:30")) is None


def test_killzone_close_is_the_half_open_end():
    ny_am = killzone_occurrence(_clock("2026-07-15 08:00"))
    assert ny_am.window == "ny_am"
    assert ny_am.start == _clock("2026-07-15 07:00")
    assert ny_am.close == _clock("2026-07-15 10:00")
    assert ny_am.key == "ny_am@2026-07-15"

    london = killzone_occurrence(_clock("2026-07-15 03:00"))
    assert london.close == _clock("2026-07-15 05:00")

    # The modal NY AM row ends at 10:00; the corpus's 11:00 hard close belongs
    # to the 07:00-11:00 variants and is a different table row (LT-K13).  The
    # window table is not merged to make them agree.
    assert window_spec("ny_am").end.strftime("%H:%M") == "10:00"


def test_cross_midnight_occurrence_closes_on_the_next_day():
    asian = killzone_occurrence(_clock("2026-07-15 23:45"))
    assert asian.window == "asian"
    assert asian.start == _clock("2026-07-15 19:00")
    assert asian.close == _clock("2026-07-16 00:00")
    # The reset key is the occurrence's start date, so the budget is shared by
    # every bar of one Asian window and renewed on the next evening.
    assert asian.key == "asian@2026-07-15"
    assert killzone_occurrence(_clock("2026-07-16 00:15")) is None


def test_instrument_scope_selects_the_declared_override():
    forex = killzone_occurrence(_clock("2026-07-15 09:55"))
    futures = killzone_occurrence(_clock("2026-07-15 09:55"),
                                  instrument="index_futures")
    assert (forex.window, forex.close.strftime("%H:%M")) == ("ny_am", "10:00")
    assert (futures.window, futures.close.strftime("%H:%M")) == (
        "ny_am_index_futures", "11:00")


def test_window_close_reached_is_half_open_and_dst_safe():
    occurrence = killzone_occurrence(_clock("2026-07-15 08:00"))
    assert window_close_reached(_clock("2026-07-15 09:45"),
                                occurrence.close) is False
    # 10:00 is the first instant outside the half-open window (V13).
    assert window_close_reached(_clock("2026-07-15 10:00"),
                                occurrence.close) is True
    # An absolute stamp is judged on the instant, not the spelling: 14:00 UTC
    # is 10:00 EDT.
    assert window_close_reached(pd.Timestamp("2026-07-15 14:00", tz="UTC"),
                                occurrence.close) is True

    winter = killzone_occurrence(_clock("2026-01-15 08:00"))
    assert winter.close == _clock("2026-01-15 10:00")
    assert winter.close.utcoffset() != occurrence.close.utcoffset()
    assert window_close_reached(pd.Timestamp("2026-01-15 15:00", tz="UTC"),
                                winter.close) is True


def test_window_occurrence_refuses_a_window_the_stamp_is_not_inside():
    row = window_spec("ny_am")
    assert window_occurrence(_clock("2026-07-15 12:00"), row) is None
    with pytest.raises(TypeError):
        window_occurrence(_clock("2026-07-15 08:00"), "ny_am")


# --------------------------------------------------------------------------- #
# 2. One attempt per window (EN-04/LT-B09)
# --------------------------------------------------------------------------- #
def test_attempt_budget_counts_and_resets_by_occurrence():
    budget = WindowAttemptBudget(1)
    key = "ny_am@2026-07-15"
    assert budget.exhausted(key) is False
    assert budget.use(key) == 1
    assert budget.exhausted(key) is True
    assert budget.remaining(key) == 0
    with pytest.raises(ValueError, match="already used"):
        budget.use(key)
    # A new occurrence is a new bucket: the once-per-window reset.
    assert budget.exhausted("ny_am@2026-07-16") is False
    budget.reset(key)
    assert budget.exhausted(key) is False


def test_the_guard_refusal_codes_are_the_typed_vocabulary():
    assert NT_ONE_ATTEMPT_USED == RefusalCode.NT_ONE_ATTEMPT_USED.value
    assert NT_FILL_TIMEOUT == RefusalCode.NT_FILL_TIMEOUT.value
    assert NT_OUT_OF_WINDOW == RefusalCode.NT_OUT_OF_WINDOW.value


def test_attempt_budget_admits_the_softer_one_to_two_variant():
    budget = WindowAttemptBudget(2)
    key = "ny_am@2026-07-15"
    assert budget.use(key) == 1
    assert budget.exhausted(key) is False
    assert budget.use(key) == 2
    assert budget.exhausted(key) is True


def test_only_the_attested_attempt_budgets_are_admissible():
    assert ATTEMPTS_PER_WINDOW == (1, 2)
    with pytest.raises(ValueError, match="not attested"):
        WindowAttemptBudget(3)
    with pytest.raises(ValueError, match="not attested"):
        StrategyConfig(session_guard=True, max_attempts_per_window=3)


def test_an_active_guard_requires_a_declared_attempt_budget():
    with pytest.raises(ValueError, match="requires a declared"):
        StrategyConfig(session_guard=True)
    with pytest.raises(ValueError, match="requires a declared"):
        SetupConfig(session_guard=True)
    # Off is the legacy path and needs no budget; the declaration is kept.
    assert StrategyConfig(session_guard=False).max_attempts_per_window is None
    assert StrategyConfig(
        session_guard=True, max_attempts_per_window=1
    ).max_attempts_per_window == 1


def test_the_guard_and_budget_are_part_of_the_config_hash():
    base = StrategyConfig()
    guard = StrategyConfig(session_guard=True, max_attempts_per_window=1)
    softer = StrategyConfig(session_guard=True, max_attempts_per_window=2)
    assert guard.config_hash != base.config_hash
    assert softer.config_hash != guard.config_hash
    assert (SetupConfig(session_guard=True, max_attempts_per_window=1).config_hash
            != SetupConfig().config_hash)


def test_second_attempt_in_the_same_window_is_refused():
    frame = _first_trade_frame()
    first = _signal(t=0)
    second = _signal(t=3)  # still inside the 07:00-10:00 NY AM occurrence

    result = _simulate([first, second], frame)

    assert len(result.trades) == 1
    assert result.trades[0].signal_time == frame["date"].iloc[0]
    assert result.metrics["refused_one_attempt_used"] == 1
    assert result.metrics["signals"] == 2


def test_attempt_budget_resets_at_the_next_window_occurrence():
    frame = _first_trade_frame()
    first = _signal(t=0)     # NY AM 07:00-10:00
    later = _signal(t=12)    # London Close 10:00-12:00, a new occurrence

    result = _simulate([first, later], frame)

    assert len(result.trades) == 2
    assert result.metrics["refused_one_attempt_used"] == 0
    assert [trade.entry_time for trade in result.trades] == [
        frame["date"].iloc[1], frame["date"].iloc[13]]


def test_the_softer_two_attempt_variant_admits_one_reentry():
    frame = _sim_frame()
    frame.loc[1, ["low", "close"]] = [98.5, 99.5]      # first entry stops out
    frame.loc[4, ["high", "close"]] = [105.6, 105.2]   # re-entry reaches target
    first = _signal(sl=99.0, target=105.0, t=0)
    second = _signal(sl=99.0, target=105.0, t=3)

    strict = _simulate([first, second], frame)
    softer = _simulate([first, second], frame, max_attempts_per_window=2)

    assert len(strict.trades) == 1
    assert strict.metrics["refused_one_attempt_used"] == 1
    assert len(softer.trades) == 2
    assert softer.metrics["refused_one_attempt_used"] == 0


def test_a_signal_outside_every_killzone_is_refused_under_the_guard():
    # 06:00 is outside every declared killzone; the guard invents no window.
    frame = _sim_frame(start="2026-07-15 06:00")
    result = _simulate([_signal(t=0)], frame)

    assert result.trades == []
    assert result.metrics["refused_out_of_window"] == 1


def test_a_spent_window_is_refused_before_the_overlap_guard():
    # The first limit fills on the 07:15 bar and its target is never reached, so
    # the position is still open when the second signal arrives.  The overlap
    # counter must not mask the corpus's per-window refusal.
    frame = _sim_frame()
    first = _signal(level=100.0, sl=98.0, target=120.0, t=0)
    second = _signal(level=100.0, sl=98.0, target=120.0, t=3)

    result = _simulate([first, second], frame, entry_mode="retest_key",
                       max_bars=30)

    assert len(result.trades) == 1
    assert result.metrics["refused_one_attempt_used"] == 1
    assert result.metrics["skipped_busy"] == 0


# --------------------------------------------------------------------------- #
# 3. The fill deadline is the killzone close, not a bar count (EN-03/GT-06)
# --------------------------------------------------------------------------- #
def test_next_open_fill_stamped_at_the_close_is_refused():
    frame = _sim_frame(n=24)
    signal = _signal(t=11)  # 09:45; its next_open fill is the 10:00 bar

    result = _simulate([signal], frame)

    assert result.trades == []
    assert result.metrics["refused_fill_timeout"] == 1
    assert result.metrics["refused_fill_outside_window"] == 0


def test_retest_fill_deadline_is_the_window_close_not_a_bar_count():
    frame = _sim_frame(n=24)
    # The limit at 99 is only touched by the 10:00 bar (the NY AM close).  The
    # legacy 20-bar count would have allowed that fill; the clock does not.
    frame.loc[12, ["low", "high", "close"]] = [98.5, 100.5, 100.0]
    signal = _signal(level=99.0, sl=98.0, target=101.0, t=8)  # 09:00

    guarded = _simulate([signal], frame, entry_mode="retest_key",
                        retest_max_bars=200)

    assert guarded.trades == []
    assert guarded.metrics["refused_fill_timeout"] == 1
    assert guarded.metrics["not_triggered"] == 0


def test_legacy_bar_count_can_fill_on_the_close_bar():
    # The pre-P1-16 path is untouched when the guard is off: the same fixture
    # fills on the 10:00 bar, which is exactly what P1-16 replaces.
    frame = _sim_frame(n=24)
    frame.loc[12, ["low", "high", "close"]] = [98.5, 100.5, 100.0]
    signal = _signal(level=99.0, sl=98.0, target=101.0, t=8)

    legacy = _simulate([signal], frame, entry_mode="retest_key",
                       session_guard=False, max_attempts_per_window=None)

    assert len(legacy.trades) == 1
    assert legacy.trades[0].entry_time == frame["date"].iloc[12]


def test_a_trade_carries_the_occurrence_and_the_attempt_it_used():
    frame = _first_trade_frame()
    result = _simulate([_signal(t=0)], frame)

    features = result.trades[0].features
    assert features["session_guard"] is True
    assert features["session_window"] == "ny_am"
    assert features["session_window_key"] == "ny_am@2026-07-15"
    assert features["session_window_close"] == _clock(
        "2026-07-15 10:00").isoformat()
    assert features["attempt_limit"] == 1
    assert features["attempt_number"] == 1


# --------------------------------------------------------------------------- #
# 4. The setup surface: a plan is an attempt
# --------------------------------------------------------------------------- #
def test_setup_refuses_a_second_plan_in_the_same_window():
    df, daily = _setup_frame(), _daily_frame()
    first = _setup_signal("bull", float(df["close"].iloc[_bar_index(
        df, "2026-01-05 07:45")]), _bar_index(df, "2026-01-05 07:45"))
    second = _setup_signal("bull", float(df["close"].iloc[_bar_index(
        df, "2026-01-05 08:00")]), _bar_index(df, "2026-01-05 08:00"))
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [first, second]), _setup_cfg(),
                          daily=daily, refusals=refusals)

    assert len(signals) == 1
    assert [r["code"] for r in refusals] == [
        RefusalCode.NT_ONE_ATTEMPT_USED.value]
    assert refusals[0]["gate"] == "session_guard"
    assert refusals[0]["session_window"] == "ny_am"


def test_setup_refuses_a_next_open_fill_at_the_close():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 09:45")  # next_open fills on the 10:00 bar
    signal = _setup_signal("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [signal]), _setup_cfg(),
                          daily=daily, refusals=refusals)

    assert signals == []
    assert [r["code"] for r in refusals] == [RefusalCode.NT_FILL_TIMEOUT.value]
    assert refusals[0]["gate"] == "session_guard"
    assert refusals[0]["fill_bar"] == t + 1


def test_setup_leaves_a_retest_fill_to_the_simulator():
    # build_setup cannot see a retest fill bar; it must not time the order out
    # on a bar count it does not own.  The simulator enforces the close.
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 09:45")
    signal = _setup_signal("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [signal]),
                          _setup_cfg(entry_mode="retest_key"),
                          daily=daily, refusals=refusals)

    assert len(signals) == 1
    assert refusals == []


def test_setup_guard_evidence_carries_the_occurrence_and_budget():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 07:45")
    signal = _setup_signal("bull", float(df["close"].iloc[t]), t)

    signals = build_setup(df, _setup_result(df, [signal]), _setup_cfg(),
                          daily=daily)

    parts = signals[0].quality_parts
    assert parts["session_window"] == "ny_am"
    assert parts["session_window_key"] == "ny_am@2026-01-05"
    assert parts["session_window_close"] == _clock(
        "2026-01-05 10:00").isoformat()
    assert (parts["attempt_limit"], parts["attempt_number"]) == (1, 1)
    assert "LT-B09" in parts["attempt_limit_source"]
    assert "GT-06" in parts["fill_deadline_source"]


def test_setup_guard_refuses_a_signal_outside_every_killzone():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 06:00")  # between two killzones
    signal = _setup_signal("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [signal]), _setup_cfg(),
                          daily=daily, refusals=refusals)

    assert signals == []
    assert [r["code"] for r in refusals] == [
        RefusalCode.NT_OUT_OF_WINDOW.value]
    assert refusals[0]["gate"] == "session_guard"


def test_setup_without_the_guard_keeps_the_legacy_path():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 09:45")
    signal = _setup_signal("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [signal]),
                          _setup_cfg(session_guard=False,
                                     max_attempts_per_window=None),
                          daily=daily, refusals=refusals)

    # No guard, no deadline: the legacy path still emits the plan (the P0-13
    # window gate is a separate declaration and is off here).
    assert len(signals) == 1
    assert refusals == []
