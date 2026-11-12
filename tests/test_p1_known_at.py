"""P1-08 - the knowledge layer: ``known_at``, LQ-06 state, pre-session map.

Three rules from ``CODE_MAPPING.md`` P1-08 and ``SYSTEM_SPEC.md`` are pinned:

* CB-07/CB-02 -- every array/event carries a ``known_at`` derived from its
  confirmation bar, and no consumer may read it before that bar.  An FVG is
  known at the close of ``C3``, an IFVG at its inversion bar, a BPR at its newer
  constituent, an OB at the structure event that selects it, a breaker at its
  flip bar, a dealing range at the later anchor confirmation, and a sweep at
  its reclaim bar (never the penetration bar, SW-02).
* LQ-06/SW-11 -- the boolean active flags become one explicit
  ``{intact, swept, broken}`` state, with the historical ``is_active`` kept as a
  compatibility view.  A level swept before it was knowable is *unusable*.
* I5/§1.6 -- the pre-session ``LevelMap`` contains only levels observable on a
  closed bar before the session opened, with the state they had at that bar
  (append-only knowledge times, CB-09).

Fixtures are closed bars with explicit ``America/New_York`` timestamps; there
is no wall-clock, network or vendor input.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ict_trader.concepts import (
    ARRAY_STATES,
    FVG,
    ICTConfig,
    ICTResult,
    StructureEvent,
    accepts_knowledge_at,
    analyze,
    build_zigzag,
    detect_bpr,
    detect_dealing_range_state,
    detect_fvgs,
    detect_ifvg,
    detect_order_blocks,
    find_pivots,
    knowledge_state,
    stamp_knowledge_times,
)
from ict_trader.pd_arrays import PDAArray
from ict_trader.realtime.analysis.arrays import build_ict_arrays
from ict_trader.realtime.analysis.evidence import extract_sweep
from ict_trader.realtime.analysis.sweeps import build_sweep_events
from ict_trader.setup import _BoxIndex
from ict_trader.sweeps import KeyLevel, LiquiditySweep

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
# (open, high, low, close) -- C3 completes a bull gap [100, 102] against C1.
BULL_GAP = [
    (99.0, 100.0, 98.0, 99.5),
    (99.5, 103.0, 99.0, 102.5),
    (102.5, 104.0, 102.0, 103.5),
]
# A wick below bottom=100 that closes back inside the gap (AR-10(a) test).
WICK_INTO_GAP = (103.5, 104.0, 99.0, 101.0)
# A body close below bottom=100 (AR-04 break).
BREAK_GAP = (103.5, 104.0, 98.5, 99.0)
# Bars after the event keep the frame long enough for the lifecycle wrapper.
FILLER_UP = [
    (101.0, 103.0, 100.5, 102.0),
    (102.0, 103.0, 101.0, 102.5),
    (102.5, 104.0, 102.0, 103.5),
    (103.5, 105.0, 103.0, 104.5),
    (104.5, 106.0, 104.0, 105.5),
    (105.5, 107.0, 105.0, 106.5),
]


def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows),
                              freq="15min", tz="America/New_York"),
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "volume": [1000.0] * len(rows),
    })


def _cfg() -> ICTConfig:
    # Isolation: only the lifecycle/knowledge rule under test decides, so the
    # displacement gate cannot remove the fixture gap.
    return ICTConfig(displacement_filter=False)


def _walk(seed: int = 7, n: int = 400) -> pd.DataFrame:
    """Deterministic synthetic frame with every array family present."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.6, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)
    dates = pd.date_range("2026-01-01", periods=n, freq="15min",
                          tz="America/New_York")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


def _times(df: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]


# --------------------------------------------------------------------------- #
# known_at is derived from the confirmation bar
# --------------------------------------------------------------------------- #
def test_fvg_known_at_is_the_close_of_c3():
    f = detect_fvgs(_frame(BULL_GAP), _cfg())[0]
    assert (f.created, f.known_at) == (2, 2), "CB-02: the gap exists at C3's close"
    assert accepts_knowledge_at(f, 1) is False
    assert accepts_knowledge_at(f, 2) is True
    assert f.state == "intact" and f.is_active is True


def test_ifvg_known_at_is_the_inversion_bar():
    df = _frame(BULL_GAP + [BREAK_GAP, *FILLER_UP])
    parent = detect_fvgs(df, _cfg())[0]
    ifvg = detect_ifvg([parent], df)[0]
    assert ifvg.created == 3 and ifvg.known_at == 3
    assert ifvg.known_at == ifvg.created
    assert accepts_knowledge_at(ifvg, 2) is False
    assert accepts_knowledge_at(ifvg, 3) is True


def test_bpr_known_at_is_the_newer_constituent():
    bear = FVG("bear", 108.0, 103.0, 1, 6, 1)
    bull = FVG("bull", 105.0, 100.0, 4, 9, 4)
    bpr = detect_bpr([bear, bull])[0]
    assert bpr.label == "BPR"
    assert bpr.known_at == max(bear.known_at, bull.known_at) == 4
    assert accepts_knowledge_at(bpr, 3) is False
    assert accepts_knowledge_at(bpr, 4) is True


def test_order_block_known_at_is_the_structure_event_bar():
    rows = [
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 101.0, 90.0, 91.0),
        (91.0, 95.5, 90.8, 95.0),
        (96.0, 96.5, 94.5, 94.8),
        (94.9, 101.0, 94.7, 100.5),
        *FILLER_UP[:3],
    ]
    ob = detect_order_blocks(
        _frame(rows), [StructureEvent(4, 101.0, "MSS", "bull")], [],
        ICTConfig())[0]
    assert ob.known_at == ob.created == 4
    assert accepts_knowledge_at(ob, 3) is False
    assert accepts_knowledge_at(ob, 4) is True


def test_dealing_range_known_at_is_the_later_anchor_confirmation():
    n = 60
    price = np.concatenate([
        np.linspace(100.0, 120.0, 21),
        np.linspace(120.0, 95.0, 25),
        np.linspace(95.0, 100.0, 14),
    ])
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min",
                              tz="America/New_York"),
        "open": price, "high": price + 0.4, "low": price - 0.4,
        "close": price, "volume": 1000.0,
    })
    df.loc[20, "high"] = 121.0
    df.loc[45, "low"] = 94.0
    swings = build_zigzag(find_pivots(df, 5, 1))
    high = [s for s in swings if s.kind == "H"][-1]
    low = [s for s in swings if s.kind == "L"][-1]
    dr, diagnostic = detect_dealing_range_state(df, swings)

    assert diagnostic is None and dr is not None
    assert dr.known_at == max(int(high.confirmed_at), int(low.confirmed_at))
    assert dr.known_at >= int(high.index) and dr.known_at >= int(low.index)
    assert accepts_knowledge_at(dr, dr.known_at - 1) is False
    assert accepts_knowledge_at(dr, dr.known_at) is True
    assert dr.state_at(dr.known_at - 1) is None
    assert dr.state == "intact" and dr.is_active is True


def test_sweep_event_known_at_is_the_reclaim_bar():
    penetration_bar, reclaim_bar, ttl = 30, 31, 5
    n = 80
    df = pd.DataFrame([
        {"date": d, "open": 100.0, "high": 100.5, "low": 99.5,
         "close": 100.0}
        for d in pd.date_range("2026-01-01", periods=n, freq="15min",
                               tz="America/New_York")
    ])
    df.loc[20, ["open", "high", "low", "close"]] = [100.0, 101.5, 100.0, 100.2]
    df.loc[21, ["open", "high", "low", "close"]] = [100.2, 100.6, 99.8, 100.0]
    df.loc[penetration_bar, ["open", "high", "low", "close"]] = [
        100.0, 102.0, 99.9, 101.8]
    df.loc[reclaim_bar, ["open", "high", "low", "close"]] = [
        101.8, 101.9, 100.8, 101.0]
    times = _times(df)

    ev = next(e for e in build_sweep_events(df, ttl_bars=ttl)
              if e.sweep_type == "BSL" and e.level == 101.5)
    assert ev.known_at == ev.bar_index == reclaim_bar
    assert ev.known_at != penetration_bar
    assert ev.known_at_ms == ev.time == times[reclaim_bar]
    assert ev.to_public()["known_at"] == reclaim_bar
    assert ev.is_active is ev.active

    # The one consumer of the sweep payload gates on the knowledge time.
    assert extract_sweep(
        [ev.to_public()], direction="short",
        source_kline_open_time=times[penetration_bar]) is None
    assert extract_sweep(
        [ev.to_public()], direction="short",
        source_kline_open_time=times[reclaim_bar]) is not None


def test_canonical_sweep_object_carries_known_at_and_swept_state():
    n = 80
    df = pd.DataFrame([
        {"date": d, "open": 100.0, "high": 100.5, "low": 99.5,
         "close": 100.0}
        for d in pd.date_range("2026-01-01", periods=n, freq="15min",
                               tz="America/New_York")
    ])
    df.loc[20, ["open", "high", "low", "close"]] = [100.0, 101.5, 100.0, 100.2]
    df.loc[21, ["open", "high", "low", "close"]] = [100.2, 100.6, 99.8, 100.0]
    df.loc[30, ["open", "high", "low", "close"]] = [100.0, 102.0, 99.9, 101.8]
    df.loc[31, ["open", "high", "low", "close"]] = [101.8, 101.9, 100.8, 101.0]

    sweep = next(s for s in analyze(df, ICTConfig()).sweeps
                 if s.level == 101.5)
    assert sweep.known_at == sweep.reclaim_bar == 31
    assert sweep.known_at != sweep.sweep_bar
    assert sweep.state == "swept"
    assert accepts_knowledge_at(sweep, 30) is False
    assert accepts_knowledge_at(sweep, 31) is True


def test_stamp_knowledge_times_covers_producers_outside_the_owner_set():
    sweep = LiquiditySweep("bear", "BSL", 101.5, "H", 30, 31, 102.0, 101.0)
    array = PDAArray("breaker", "bull", 100.0, 99.0, 10, 12)
    result = stamp_knowledge_times(ICTResult(
        bars=_frame(BULL_GAP), sweeps=[sweep], pd_arrays=[array]))
    assert sweep.known_at == 31 and sweep.state == "swept"
    assert array.known_at == 12 and array.state == "intact"
    # Idempotent: a second stamp cannot move the knowledge time.
    stamp_knowledge_times(result)
    assert (sweep.known_at, array.known_at) == (31, 12)


def test_analyze_emits_known_at_on_every_array_family():
    result = analyze(_walk(), ICTConfig(mode="historical"))
    assert result.fvgs and result.order_blocks and result.pd_arrays
    for gap in result.fvgs:
        assert gap.known_at == gap.created, gap.label
    for ob in result.order_blocks:
        assert ob.known_at == ob.created
    for array in result.pd_arrays:
        assert array.known_at == array.confirmed_at, array.kind
        assert array.state in ARRAY_STATES
    assert {a.kind for a in result.pd_arrays} >= {"breaker"}
    for sweep in result.sweeps:
        assert sweep.known_at == sweep.reclaim_bar
    if result.dealing_range is not None:
        assert result.dealing_range.known_at >= 0


# --------------------------------------------------------------------------- #
# LQ-06 state transitions and the is_active compatibility view
# --------------------------------------------------------------------------- #
def test_fvg_state_transitions_are_intact_swept_broken():
    intact = detect_fvgs(_frame(BULL_GAP), _cfg())[0]
    swept = detect_fvgs(_frame(BULL_GAP + [WICK_INTO_GAP, *FILLER_UP]),
                        _cfg())[0]
    broken = detect_fvgs(_frame(BULL_GAP + [BREAK_GAP, *FILLER_UP]),
                         _cfg())[0]

    assert intact.state == "intact"
    assert [intact.state_at(t) for t in (1, 2)] == [None, "intact"]

    assert swept.state == "swept", "a wick test is not a break (AR-10a)"
    assert swept.is_active is True, "compatibility: a swept gap is not broken"
    assert [swept.state_at(t) for t in (2, 3)] == ["intact", "swept"]

    assert broken.state == "broken"
    assert broken.is_active is False
    assert [broken.state_at(t) for t in (2, 3)] == ["intact", "broken"]


def test_order_block_state_transitions_are_intact_swept_broken():
    base = [
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 101.0, 90.0, 91.0),
        (91.0, 95.5, 90.8, 95.0),
        (96.0, 96.5, 94.5, 94.8),
        (94.9, 101.0, 94.7, 100.5),
    ]
    event = [StructureEvent(4, 101.0, "MSS", "bull")]
    entered = detect_order_blocks(
        _frame(base + [(100.5, 100.7, 95.5, 95.8), *FILLER_UP[:2]]),
        event, [], ICTConfig())[0]
    broken = detect_order_blocks(
        _frame(base + [(100.5, 100.7, 95.5, 95.8),
                       (95.8, 96.2, 94.0, 94.2),
                       (94.2, 94.5, 93.0, 93.5)]),
        event, [], ICTConfig())[0]

    # The block is known at the event bar; the entry happens afterwards.
    assert entered.known_at == 4
    assert entered.state == "swept" and entered.is_active is True
    assert [entered.state_at(t) for t in (3, 4, 5)] == [None, "intact", "swept"]

    assert broken.known_at == 4
    assert broken.state == "broken" and broken.is_active is False
    assert [broken.state_at(t) for t in (4, 5, 6)] == [
        "intact", "swept", "broken"]


def test_ict_array_state_and_is_active_compatibility_view():
    wick = build_ict_arrays(_frame(BULL_GAP + [WICK_INTO_GAP, *FILLER_UP]),
                            "TESTUSDT", "15m")
    times = _times(_frame(BULL_GAP + [WICK_INTO_GAP, *FILLER_UP]))
    gap = next(a for a in wick
               if a.array_type == "FVG" and a.formed_at == times[2])
    assert (gap.state, gap.is_active) == ("swept", True)
    assert gap.state_at(times[3] - 1) == "intact"
    assert gap.state_at(times[3]) == "swept"

    broken_frame = _frame(BULL_GAP + [BREAK_GAP, *FILLER_UP])
    broken = build_ict_arrays(broken_frame, "TESTUSDT", "15m")
    broken_times = _times(broken_frame)
    gap = next(a for a in broken
               if a.array_type == "FVG" and a.formed_at == broken_times[2])
    assert (gap.state, gap.is_active) == ("broken", False)
    assert gap.state_at(broken_times[3] - 1) == "intact"
    assert gap.state_at(broken_times[3]) == "broken"

    for array in wick + broken:
        assert array.state in ARRAY_STATES
        assert array.is_active is (array.state != "broken")
        public = array.to_public()
        assert public["known_at"] == array.known_at
        assert public["state"] == array.state
        assert public["is_active"] is array.is_active


def test_every_ict_array_has_all_three_states_on_a_rich_frame():
    arrays = build_ict_arrays(_walk(), "TESTUSDT", "15m")
    states = {a.state for a in arrays}
    assert states == set(ARRAY_STATES), (
        "the fixture must exercise intact, swept and broken arrays")
    assert any(a.array_type == "BREAKER" for a in arrays)
    for array in arrays:
        assert array.known_at == array.formed_at
        assert array.is_known_at(array.known_at - 1) is False
        assert array.is_known_at(array.known_at) is True


# --------------------------------------------------------------------------- #
# No array/event is consumable before known_at
# --------------------------------------------------------------------------- #
def test_fvg_is_not_consumable_before_known_at():
    gap = detect_fvgs(_frame(BULL_GAP), _cfg())[0]
    box = _BoxIndex([gap], edge_of=lambda f: f.ce)
    _, before = box.nearest(gap.ce, gap.known_at - 1)
    at, chosen = box.nearest(gap.ce, gap.known_at)
    assert before is None, "an FVG cannot be consumed before its C3 close"
    assert at == 0 and chosen is gap


def test_order_block_is_not_consumable_before_known_at():
    rows = [
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 101.0, 90.0, 91.0),
        (91.0, 95.5, 90.8, 95.0),
        (96.0, 96.5, 94.5, 94.8),
        (94.9, 101.0, 94.7, 100.5),
        *FILLER_UP[:3],
    ]
    ob = detect_order_blocks(
        _frame(rows), [StructureEvent(4, 101.0, "MSS", "bull")], [],
        ICTConfig())[0]
    box = _BoxIndex([ob], edge_of=lambda o: o.mean)
    _, before = box.nearest(ob.mean, ob.known_at - 1)
    at, chosen = box.nearest(ob.mean, ob.known_at)
    assert before is None, "an OB cannot be consumed before its event bar closes"
    assert at == 0 and chosen is ob


def test_breaker_array_is_not_consumable_before_known_at():
    arrays = build_ict_arrays(_walk(), "TESTUSDT", "15m")
    breaker = next(a for a in arrays if a.array_type == "BREAKER")
    assert breaker.known_at == breaker.formed_at
    assert breaker.is_known_at(breaker.known_at - 1) is False
    assert breaker.is_known_at(breaker.known_at) is True


# --------------------------------------------------------------------------- #
# I5: the pre-session level inventory
# --------------------------------------------------------------------------- #
def test_pre_session_inventory_is_committed_before_the_open():
    df = _frame(BULL_GAP + [WICK_INTO_GAP, *FILLER_UP])
    result = analyze(df, ICTConfig())
    gap_bar = 2

    before = result.pre_session_inventory(gap_bar)
    assert before.as_of == gap_bar - 1
    assert not [e for e in before if e.level_class == "gap"
                and e.known_at == gap_bar], (
        "a gap confirmed at bar 2 is not pre-session knowledge for a bar-2 open")

    at_open = result.pre_session_inventory(gap_bar + 1)
    gap_entry = next(e for e in at_open if e.level_class == "gap"
                     and e.known_at == gap_bar)
    assert gap_entry.state == "intact", "the wick is on bar 3, not yet known"

    after = result.pre_session_inventory(gap_bar + 2)
    gap_entry = next(e for e in after if e.level_class == "gap"
                     and e.known_at == gap_bar)
    assert gap_entry.state == "swept", "state is evaluated at the snapshot bar"

    for snapshot in (before, at_open, after):
        assert all(e.known_at < snapshot.as_of + 1 for e in snapshot)
        for entry in snapshot:
            assert entry.state in ARRAY_STATES
            assert set(entry.to_public()) >= {
                "class", "price", "known_at", "state", "tf"}
    assert set(at_open.to_public()) >= {"as_of", "tf", "entries"}


def test_pre_session_inventory_refuses_a_level_swept_before_it_was_known():
    # I5/SW-11: the level is first observed at bar 10 but is recorded as swept
    # at bar 5.  Its first observation postdates the sweep => unusable.
    impossible = KeyLevel("BSL", 102.0, "H", known_at=10, swept=True,
                          sweep_bar=5)
    result = ICTResult(bars=_frame(BULL_GAP), key_levels=[impossible])
    inventory = result.pre_session_inventory(11)
    assert not [e for e in inventory if e.level_class == "pool"]
    assert knowledge_state(10, 11, swept_at=5) is None

    # A level known at bar 5 and swept at bar 8 is intact in a bar-6 snapshot
    # and swept in a bar-9 snapshot (it was swept after it became knowable).
    usable = KeyLevel("BSL", 102.0, "H", known_at=5, swept=True, sweep_bar=8)
    result = ICTResult(bars=_frame(BULL_GAP), key_levels=[usable])
    early = result.pre_session_inventory(6)
    late = result.pre_session_inventory(9)
    assert [e.state for e in early if e.level_class == "pool"] == ["intact"]
    assert [e.state for e in late if e.level_class == "pool"] == ["swept"]
