"""P1-10 -- window tables, the macro set, the dead zone, hard close, Judas.

``CODE_MAPPING`` P1-10 asks for five things:

* the **8-row macro set as data** (SYSTEM_SPEC §10.2 / LT-M01), in one table
  with every other clock object -- no second macro table, no merged killzone
  schedule;
* the ``05:00-07:00`` **dead zone** with the ``NT_DEAD_ZONE`` refusal, plus the
  named non-windows ``06:30`` and ``12:00`` (LT-K11/LT-K12);
* **hard-close instants** ``05:00`` (London) and ``11:00`` (NY AM) under the
  declared half-open convention (LT-K13);
* **instrument-scoped windows**: index futures ``08:30-11:00`` versus forex
  ``07:00-10:00`` (GT-08);
* a **Judas window object** carrying its window, its 15-30 minute inner
  blackout and its hard close (J-C5).

The corpus does not resolve several forks (the macro 7-vs-8 row set, the NY AM
boundary, the eleven Judas clocks).  The tests therefore pin the *declared*
choice and assert that the competing values are kept separate rather than
silently averaged or merged.
"""
from __future__ import annotations

from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    DEAD_ZONE,
    HARD_CLOSES,
    INSTRUMENT_SCOPES,
    JUDAS_BLACKOUT_MINUTES,
    JUDAS_WINDOW_VARIANTS,
    MACRO_GATE_NAMES,
    MACRO_WINDOWS,
    NAMED_NON_WINDOWS,
    NY,
    WINDOW_BOUNDARY,
    WINDOW_TABLE,
    JudasWindow,
    SessionGate,
    WindowKind,
    active_windows,
    dead_zone_at,
    hard_close_at,
    in_dead_zone,
    in_named_non_window,
    in_session_at,
    named_non_window_at,
    session_windows,
    window_for,
    window_spec,
    window_status,
)
from ict_trader.realtime.analysis.evidence import (
    judas_window_evidence,
    window_evidence,
)
from ict_trader.setup import SetupConfig, build_setup
from ict_trader.variants import RefusalCode
from typing import get_args

#: The eight rows, in the §10.2 table order.
EXPECTED_MACROS = (
    (time(2, 33), time(3, 0)),
    (time(4, 3), time(4, 30)),
    (time(8, 50), time(9, 10)),
    (time(9, 50), time(10, 10)),
    (time(10, 50), time(11, 10)),
    (time(11, 50), time(12, 10)),
    (time(13, 10), time(13, 40)),
    (time(15, 15), time(15, 45)),
)


def _ny(stamp: str) -> pd.Timestamp:
    return pd.Timestamp(stamp, tz=NY)


def _clock(row: time, day: date = date(2026, 7, 15)) -> pd.Timestamp:
    return pd.Timestamp(f"{day.isoformat()} {row.strftime('%H:%M')}", tz=NY)


# --------------------------------------------------------------------------- #
# table completeness / one table
# --------------------------------------------------------------------------- #
def test_one_window_table_declares_every_kind_and_gate():
    names = [row.name for row in WINDOW_TABLE]
    assert len(names) == len(set(names)), "window names must be unique"
    assert {row.kind for row in WINDOW_TABLE} == set(WindowKind)
    assert WINDOW_BOUNDARY == "half-open"

    gates = {row.gate for row in WINDOW_TABLE
             if row.gate is not None and row.kind is not WindowKind.DEAD_ZONE}
    declared_gates = set(get_args(SessionGate)) - {"none", "auto"}
    assert gates == declared_gates, (
        "every declared SessionGate must be a row in the one table")

    for row in WINDOW_TABLE:
        assert row.source, f"{row.name} must carry its provenance"
        assert row.duration_minutes() > 0, f"{row.name} is zero-length"
        assert row.zone in {NY, "Asia/Shanghai", "Asia/Hong_Kong"}


def test_macro_set_is_exactly_the_eight_declared_rows():
    assert len(MACRO_WINDOWS) == 8
    assert [(row.start, row.end) for row in MACRO_WINDOWS] == list(EXPECTED_MACROS)
    assert all(row.kind is WindowKind.MACRO for row in MACRO_WINDOWS)
    assert all(row.zone == NY for row in MACRO_WINDOWS)
    assert all(20 <= row.duration_minutes() <= 30 for row in MACRO_WINDOWS)
    assert MACRO_GATE_NAMES == tuple(row.gate for row in MACRO_WINDOWS)


def test_macro_rows_are_a_view_of_the_one_table_not_a_second_clock():
    # The seven-row variant's last window is a different corpus row; it is not
    # merged into the eight-row set (§10.2 "variants kept separate").
    assert (time(14, 50), time(15, 10)) not in {
        (row.start, row.end) for row in WINDOW_TABLE}

    for row in MACRO_WINDOWS:
        assert window_spec(row.name) == row
        assert row in WINDOW_TABLE
        zone, spans = session_windows(row.gate)
        assert zone == NY
        assert spans == ((row.start, row.end),)

        start = _clock(row.start)
        assert in_session_at(start, row.gate) is True
        assert in_session_at(start + pd.Timedelta(minutes=1), row.gate) is True
        assert in_session_at(_clock(row.end), row.gate) is False


def test_every_window_row_is_reachable_through_the_gate_view():
    for row in WINDOW_TABLE:
        if row.gate is None or row.instruments:
            continue
        if row.kind is WindowKind.DEAD_ZONE:
            continue
        zone, spans = session_windows(row.gate)
        assert zone == row.zone
        assert (row.start, row.end) in spans


def test_macro_membership_is_half_open_and_boundary_is_declared():
    row = window_spec("macro_0950")
    assert row.contains(time(9, 49)) is False
    assert row.contains(time(9, 50)) is True
    assert row.contains(time(10, 9)) is True
    assert row.contains(time(10, 10)) is False
    assert in_session_at(_ny("2026-07-15 10:10"), "macro_0950") is False
    assert in_session_at(_ny("2026-07-15 09:50"), "macro_0950") is True


def test_active_windows_returns_the_corpus_nesting_at_0950():
    names = {row.name for row in active_windows(_ny("2026-07-15 09:55"))}
    assert "ny_am" in names
    assert "macro_0950" in names
    # A range (asian_dr/venom) is never a permission window.
    assert "asian_dr" not in names
    assert "venom" not in names


# --------------------------------------------------------------------------- #
# dead zone and named non-windows
# --------------------------------------------------------------------------- #
def test_dead_zone_is_the_0500_to_0700_no_entry_window():
    assert (DEAD_ZONE.start, DEAD_ZONE.end) == (time(5, 0), time(7, 0))
    assert DEAD_ZONE.kind is WindowKind.DEAD_ZONE
    assert DEAD_ZONE in WINDOW_TABLE
    assert sum(row.kind is WindowKind.DEAD_ZONE for row in WINDOW_TABLE) == 1

    assert dead_zone_at(_ny("2026-07-15 04:59")) is False
    for clock in ("05:00", "06:30", "06:59"):
        stamp = _ny(f"2026-07-15 {clock}")
        assert dead_zone_at(stamp) is True
        status = window_status(stamp)
        assert status.dead_zone is True
        assert status.windows == ()
        assert status.refusal is not None
        assert status.refusal.code is RefusalCode.NT_DEAD_ZONE
    assert dead_zone_at(_ny("2026-07-15 07:00")) is False


def test_dead_zone_frame_twin_agrees_with_the_instant_predicate():
    frame = pd.DataFrame({"date": [
        _ny("2026-07-15 05:30"), _ny("2026-07-15 07:30")]})
    assert in_dead_zone(frame, 0) is True
    assert in_dead_zone(frame, 1) is False


def test_named_non_windows_suppress_a_macro_at_1200():
    assert [(row.instant, row.zone) for row in NAMED_NON_WINDOWS] == [
        (time(6, 30), NY), (time(12, 0), NY)]
    assert named_non_window_at(_ny("2026-07-15 06:30")) == (
        "between London close and NY AM open")
    assert named_non_window_at(_ny("2026-07-15 12:00")) == (
        "between NY AM and NY PM")
    assert named_non_window_at(_ny("2026-07-15 11:59")) is None

    # 12:00 is inside the 11:50-12:10 macro; the named non-window wins, and the
    # suppressed macro is reported instead of being silently open.
    noon = window_status(_ny("2026-07-15 12:00"))
    assert noon.windows == ()
    assert noon.non_window is not None
    assert "macro_1150" in noon.suppressed
    assert noon.refusal is not None
    assert noon.refusal.code is RefusalCode.NT_OUT_OF_WINDOW

    # One minute earlier the macro is an ordinary open window.
    before = window_status(_ny("2026-07-15 11:59"))
    assert "macro_1150" in before.windows
    assert before.non_window is None
    assert before.refusal is None


def test_named_non_window_frame_twin_agrees_with_the_instant_predicate():
    frame = pd.DataFrame({"date": [
        _ny("2026-07-15 06:30"), _ny("2026-07-15 12:00"),
        _ny("2026-07-15 11:59")]})
    assert in_named_non_window(frame, 0) is not None
    assert in_named_non_window(frame, 1) is not None
    assert in_named_non_window(frame, 2) is None


# --------------------------------------------------------------------------- #
# hard close
# --------------------------------------------------------------------------- #
def test_hard_close_instants_are_0500_and_1100_and_exclusive():
    assert [(row.instant, row.window) for row in HARD_CLOSES] == [
        (time(5, 0), "london"), (time(11, 0), "ny_am")]

    assert hard_close_at(_ny("2026-07-15 05:00")) is HARD_CLOSES[0]
    assert hard_close_at(_ny("2026-07-15 11:00")) is HARD_CLOSES[1]
    assert hard_close_at(_ny("2026-07-15 04:59")) is None
    assert hard_close_at(_ny("2026-07-15 05:01")) is None
    assert hard_close_at(_ny("2026-07-15 11:01")) is None

    # Half-open: the end instant is outside the window, which is what makes it
    # usable as a hard close rather than a last-entry minute.
    london = window_spec("london")
    assert london.hard_close == time(5, 0)
    assert london.contains(time(5, 0)) is False
    assert london.contains(time(4, 59)) is True

    # The modal NY AM row closes at 10:00; LT-K13's 11:00 hard close belongs to
    # the 07:00-11:00 / 08:00-11:00 variants and is recorded, not overwritten.
    assert window_spec("ny_am").hard_close == time(10, 0)
    assert "07:00-11:00" in HARD_CLOSES[1].variant


def test_hard_close_minute_is_also_a_dead_zone_start_at_0500():
    status = window_status(_ny("2026-07-15 05:00"))
    assert status.hard_close is HARD_CLOSES[0]
    assert status.dead_zone is True
    assert status.refusal.code is RefusalCode.NT_DEAD_ZONE


# --------------------------------------------------------------------------- #
# instrument scoping (GT-08)
# --------------------------------------------------------------------------- #
def test_instrument_scoped_ny_am_resolves_without_merging():
    forex = window_for("ny_am", instrument="forex")
    futures = window_for("ny_am", instrument="index_futures")
    assert (forex.start, forex.end) == (time(7, 0), time(10, 0))
    assert (futures.start, futures.end) == (time(8, 30), time(11, 0))
    assert forex.name != futures.name
    assert futures.instruments == ("index_futures",)
    assert forex.instruments == ()

    # No declared scope keeps the legacy modal row.
    assert window_for("ny_am") == forex
    assert set(INSTRUMENT_SCOPES) == {"forex", "index_futures", "crypto"}

    assert in_session_at(_ny("2026-07-15 07:30"), "ny_am",
                         instrument="forex") is True
    assert in_session_at(_ny("2026-07-15 07:30"), "ny_am",
                         instrument="index_futures") is False
    assert in_session_at(_ny("2026-07-15 10:30"), "ny_am",
                         instrument="index_futures") is True
    assert in_session_at(_ny("2026-07-15 10:30"), "ny_am",
                         instrument="forex") is False


def test_unknown_instrument_scope_is_refused_not_treated_as_forex():
    with pytest.raises(ValueError, match="instrument scope"):
        window_for("ny_am", instrument="equities")


# --------------------------------------------------------------------------- #
# Judas window object (J-C5 / LT-J03)
# --------------------------------------------------------------------------- #
def test_judas_variant_table_is_the_eleven_attested_clocks():
    assert len(JUDAS_WINDOW_VARIANTS) == 11
    assert len({row.name for row in JUDAS_WINDOW_VARIANTS}) == 11
    assert all(row.zone == NY for row in JUDAS_WINDOW_VARIANTS)
    assert all(row.source for row in JUDAS_WINDOW_VARIANTS)
    assert JUDAS_BLACKOUT_MINUTES == (15, 30)


def test_judas_window_objects_carries_window_blackout_and_hard_close():
    judas = JudasWindow.from_variant("london_0200_0233", blackout_minutes=30)
    assert (judas.start, judas.end) == (time(2, 0), time(2, 33))
    assert judas.blackout_end == time(2, 30)
    assert judas.hard_close == time(2, 33)

    assert judas.phase(_ny("2026-07-15 01:59")) == "outside"
    assert judas.phase(_ny("2026-07-15 02:10")) == "blackout"
    assert judas.allows_entry(_ny("2026-07-15 02:10")) is False
    assert judas.phase(_ny("2026-07-15 02:30")) == "open"
    assert judas.allows_entry(_ny("2026-07-15 02:30")) is True
    assert judas.phase(_ny("2026-07-15 02:33")) == "outside"
    assert judas.allows_entry(_ny("2026-07-15 02:33")) is False

    record = judas_window_evidence(judas, _ny("2026-07-15 02:10"))
    assert record["variant"] == "london_0200_0233"
    assert record["phase"] == "blackout"
    assert record["allows_entry"] is False
    assert record["hard_close"] == "02:33"


def test_judas_object_refuses_an_undeclared_variant_or_blackout():
    with pytest.raises(KeyError):
        JudasWindow.from_variant("london_0200_0233x", blackout_minutes=30)
    for bad in (0, 14, 31, 60):
        with pytest.raises(ValueError, match="blackout"):
            JudasWindow.from_variant("london_0200_0233",
                                     blackout_minutes=bad)


def test_judas_entry_stays_closed_inside_the_dead_zone():
    judas = JudasWindow.from_variant("late_london_0500_0700",
                                     blackout_minutes=15)
    # 05:30 is past the inner blackout but still inside LT-K11's dead zone.
    assert judas.phase(_ny("2026-07-15 05:30")) == "open"
    assert judas.allows_entry(_ny("2026-07-15 05:30")) is False
    assert dead_zone_at(_ny("2026-07-15 05:30")) is True
    assert judas_window_evidence(
        judas, _ny("2026-07-15 05:30"))["allows_entry"] is False


# --------------------------------------------------------------------------- #
# realtime evidence boundary
# --------------------------------------------------------------------------- #
def test_window_evidence_carries_the_typed_refusal():
    dead = window_evidence(_ny("2026-07-15 06:30"))
    assert dead["dead_zone"] is True
    assert dead["refusal"]["code"] == "NT_DEAD_ZONE"

    noon = window_evidence(_ny("2026-07-15 12:00"))
    assert noon["non_window"] is not None
    assert noon["refusal"]["code"] == "NT_OUT_OF_WINDOW"

    inside = window_evidence(_ny("2026-07-15 09:55"),
                             instrument="index_futures")
    assert "macro_0950" in inside["windows"]
    assert "ny_am_index_futures" in inside["windows"]
    assert inside["refusal"] is None


# --------------------------------------------------------------------------- #
# setup gate (P1-10): the dead zone refuses the signal bar and a next-open fill
# --------------------------------------------------------------------------- #
def _setup_frame(n: int = 360) -> pd.DataFrame:
    rows = []
    t0 = pd.Timestamp("2026-01-02 00:00", tz=NY)
    for i in range(n):
        close = 200.0 - 0.05 * i + 2.0 * np.sin(i / 7.0)
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
    t0 = pd.Timestamp("2025-09-01 16:00", tz=NY)
    for i in range(200):
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


def _setup_result(df: pd.DataFrame, cisds: list[CISD]):
    from types import SimpleNamespace

    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)),
        cisds=cisds, fvgs=[], order_blocks=[], pd_arrays=[], sweeps=[],
        key_levels=[],
    )


def _cisd(direction: str, level: float, t: int) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
    )


def _setup_cfg(**overrides) -> SetupConfig:
    values = dict(
        market="cn", stack="1d_15m", allow_short=True,
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
        session_gate="none", dead_zone_gate=True, quality_profile="standard",
    )
    values.update(overrides)
    return SetupConfig(**values)


def _bar_index(df: pd.DataFrame, stamp: str) -> int:
    return int(df.index[df["date"] == _ny(stamp)][0])


def test_setup_dead_zone_gate_refuses_the_signal_bar():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 05:15")
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [c]), _setup_cfg(),
                          daily=daily, refusals=refusals)

    assert signals == []
    assert len(refusals) == 1
    assert refusals[0]["code"] == RefusalCode.NT_DEAD_ZONE.value
    assert refusals[0]["gate"] == "dead_zone"
    assert refusals[0]["signal_bar"] == t


def test_setup_dead_zone_gate_refuses_a_next_open_fill_inside_the_dead_zone():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 04:45")  # next_open fills at 05:00 NY
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [c]), _setup_cfg(),
                          daily=daily, refusals=refusals)

    assert signals == []
    assert len(refusals) == 1
    assert refusals[0]["code"] == RefusalCode.NT_DEAD_ZONE.value
    assert refusals[0]["signal_bar"] == t
    assert refusals[0]["fill_bar"] == t + 1


def test_setup_without_the_dead_zone_gate_keeps_the_legacy_path():
    df, daily = _setup_frame(), _daily_frame()
    t = _bar_index(df, "2026-01-05 05:15")
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    refusals: list[dict] = []

    signals = build_setup(df, _setup_result(df, [c]),
                          _setup_cfg(dead_zone_gate=False),
                          daily=daily, refusals=refusals)

    assert len(signals) == 1
    assert refusals == []
