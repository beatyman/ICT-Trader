"""P1-06 -- the daily-bias families B-G (DB-04..DB-07).

Each family is a **separately declared choice**: a run selects exactly one, and
the corpus explicitly forbids averaging the families into a composite
(``daily_bias`` C4).  These tests pin each family's rule, the outside-day
caution downgrade, the 1D MSS lock's hold semantics, the two-declaration
refusal, and the ``build_setup`` gate wiring.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    DAILY_BIAS_FAMILIES,
    DAILY_BIAS_FAMILY_ORDER,
    DAILY_BIAS_FAMILY_SPECS,
    DAILY_BIAS_FAMILY_STATUS,
    DailyBiasFamily,
    DailyBiasReading,
    DailyMSSEvent,
    DealingRange,
    LastDailyMSSLock,
    last_1d_mss_bias,
    location_in_range,
    outside_day_bias,
    parse_daily_bias_family,
    prior_close_bias,
    reconcile_daily_bias,
    structure_location_bias,
    structure_location_from_range,
)
from ict_trader.setup import SetupConfig, build_setup
from ict_trader.variants import (
    RefusalCode,
    UnknownVariant,
    VariantConfig,
    VariantUndeclared,
)

NY = "America/New_York"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _frame(path: tuple[tuple[float, float, float], ...]) -> pd.DataFrame:
    """One base bar per day, starting exactly at 00:00 America/New_York.

    Each tuple is ``(high, low, close)``; the open is the close so the daily
    bucket's OHLC is fully determined.  One bar per day keeps every daily bucket
    a real high/low range (unlike a close-only frame) while still mapping to
    exactly one daily bar.
    """
    rows = []
    for index, (high, low, close) in enumerate(path):
        stamp = pd.Timestamp(
            f"2026-01-{index + 1:02d} 00:00", tz=NY).tz_convert("UTC")
        rows.append((stamp, close, high, low, close, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                       "volume"])


def _three_days(prior_high: float = 100.0, prior_low: float = 90.0,
                prior_close: float = 91.0,
                current_high: float = 96.0, current_low: float = 90.0,
                current_close: float = 95.0) -> pd.DataFrame:
    """Three days: day 0 filler, day 1 the prior day, day 2 the current day."""
    return _frame(((100.0, 90.0, 95.0),
                   (prior_high, prior_low, prior_close),
                   (current_high, current_low, current_close)))


def _zigzag_frame() -> pd.DataFrame:
    """A 20-day path with one real 1D bear MSS before the last two days.

    Days 0-4 fall to a day-5 low, days 6-10 rally to a day-10 high, days 11-14
    break the old low (the bear MSS prints at day 14), and days 15-19 retrace
    into premium without printing a bull MSS (the retracement stays below the
    day-10 high).  At base bar 19 the last completed day is 18, so the day-14
    MSS is known, the 1D dealing range is [84, 108] (EQ 96), and the close 101
    is premium.
    """
    path: list[tuple[float, float, float]] = []
    for index in range(5):
        path.append((100 - 2 * index, 96 - 2 * index, 97 - 2 * index))
    path.append((90.0, 88.0, 89.0))                       # day 5 low pivot
    for index in range(5):
        path.append((92 + 4 * index, 88 + 4 * index, 91 + 4 * index))
    for index in range(4):
        path.append((106 - 6 * index, 102 - 6 * index, 103 - 6 * index))
    for index in range(5):
        path.append((90 + 3 * index, 86 + 3 * index, 89 + 3 * index))
    return _frame(tuple(path))


def _result(df: pd.DataFrame, t: int, direction: str = "bull") -> SimpleNamespace:
    level = float(df["close"].iloc[t])
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)),
        cisds=[CISD(
            direction=direction, level=level, extreme=level,
            body_extreme=level, start=max(0, t - 5), end=t, sl=sl, target=None,
            grade=3, checks={"sweep": False, "pda": False})],
        fvgs=[], order_blocks=[], pd_arrays=[], sweeps=[], key_levels=[],
    )


def _permissive_cfg(**overrides) -> SetupConfig:
    values = dict(
        market="other", stack="1d_15m", allow_short=True, min_grade=0,
        min_confluence=0, require_sweep=False, origin_filter="any",
        htf_fvg=False, target_mode="fixed", rr=2.0, min_target_rr=0.0,
        max_inducements=-1, regime_features=False, min_disp_body_ratio=0.0,
        min_disp_atr=0.0, min_disp_multiple=0.0, min_setup_score=0.0,
        require_pda_array=False, require_first_fvg=False,
        require_fresh_fvg=False, pd_gate=False, ote_gate=False,
        session_gate="none", quality_profile="standard", bias_primitive="none",
        variant_config=VariantConfig({"V04": "NY midnight"}),
    )
    values.update(overrides)
    return SetupConfig(**values)


# --------------------------------------------------------------------------- #
# 1. the declaration: four implemented choices, B-G accounted for
# --------------------------------------------------------------------------- #
def test_the_declared_family_choices_are_the_four_implemented_ones():
    assert DAILY_BIAS_FAMILIES == (
        "prior_close", "outside_day", "structure_location", "last_1d_mss")
    assert tuple(family.value for family in DailyBiasFamily) == DAILY_BIAS_FAMILIES
    # Every implemented choice names its concept family letter and its DB rule.
    assert DAILY_BIAS_FAMILY_SPECS["prior_close"]["family"] == "B"
    assert DAILY_BIAS_FAMILY_SPECS["prior_close"]["rule"] == "DB-04"
    assert DAILY_BIAS_FAMILY_SPECS["outside_day"]["family"] == "B"
    assert DAILY_BIAS_FAMILY_SPECS["outside_day"]["rule"] == "DB-05"
    assert DAILY_BIAS_FAMILY_SPECS["structure_location"]["family"] == "C"
    assert DAILY_BIAS_FAMILY_SPECS["structure_location"]["rule"] == "DB-06"
    assert DAILY_BIAS_FAMILY_SPECS["last_1d_mss"]["family"] == "G"
    assert DAILY_BIAS_FAMILY_SPECS["last_1d_mss"]["rule"] == "DB-07"

    # The B-G range is accounted for: B/C/G implemented, D/E/F blocked with the
    # object each one lacks -- not silently omitted and not approximated.
    assert DAILY_BIAS_FAMILY_ORDER == ("B", "C", "D", "E", "F", "G")
    assert set(DAILY_BIAS_FAMILY_STATUS) == set(DAILY_BIAS_FAMILY_ORDER)
    for letter in ("B", "C", "G"):
        assert DAILY_BIAS_FAMILY_STATUS[letter]["status"] == "implemented"
    for letter in ("D", "E", "F"):
        record = DAILY_BIAS_FAMILY_STATUS[letter]
        assert record["status"] == "blocked"
        assert record["source"].strip()
        assert record["family"].strip()


def test_the_family_parser_accepts_one_choice_and_names_blocked_families():
    for family in DAILY_BIAS_FAMILIES:
        assert parse_daily_bias_family(family) == family
        assert parse_daily_bias_family(DailyBiasFamily(family)) == family
    assert parse_daily_bias_family("Prior Close") == "prior_close"
    assert parse_daily_bias_family("structure-location") == "structure_location"
    assert parse_daily_bias_family(" last 1d mss ") == "last_1d_mss"

    with pytest.raises(ValueError, match="unknown daily_bias_family"):
        parse_daily_bias_family("everything")
    # D/E/F exist in the concept but cannot be computed here: the refusal names
    # the blocking object rather than calling the family unknown.
    for letter in ("D", "E", "F"):
        with pytest.raises(ValueError, match="blocked"):
            parse_daily_bias_family(letter)


# --------------------------------------------------------------------------- #
# 2. family B -- prior close (DB-04)
# --------------------------------------------------------------------------- #
def test_prior_close_is_the_prior_range_midpoint_test():
    # Above the midpoint => bullish signal for today; below => bearish.
    above, refusal = prior_close_bias(96.0, 100.0, 90.0, bar=7)
    assert refusal is None
    assert above.bias is not None and above.bias.value == "bullish"
    assert above.rule == "DB-04"
    assert dict(above.inputs)["prior_range_midpoint"] == 95.0

    below, refusal = prior_close_bias(94.0, 100.0, 90.0, bar=7)
    assert refusal is None and below.bias.value == "bearish"

    # The corpus states "above"/"below" only: equality has no branch and the
    # reading is explicitly missing a direction rather than a fabricated side.
    at_mid, refusal = prior_close_bias(95.0, 100.0, 90.0, bar=7)
    assert refusal is not None
    assert refusal.code is RefusalCode.NT_NO_BIAS
    assert at_mid.bias is None
    assert at_mid.missing_fields == ("bias",)
    assert "no tolerance" in refusal.detail

    with pytest.raises(ValueError, match="degenerate"):
        prior_close_bias(95.0, 90.0, 90.0, bar=7)
    with pytest.raises(ValueError, match="finite"):
        prior_close_bias(float("nan"), 100.0, 90.0, bar=7)


def test_a_reading_carries_no_score_and_validates_its_family_and_ids():
    reading, _ = prior_close_bias(96.0, 100.0, 90.0, bar=4, variant_ids=("V04",))
    payload = reading.to_public()
    assert payload["bias"] == "bullish"
    assert payload["concept_family"] == "B"
    assert payload["variant_ids"] == ["V04"]
    assert payload["missing_fields"] == []
    for forbidden in ("score", "probability", "confidence", "weight"):
        assert forbidden not in payload

    # A reading may not be relabelled as another family's rule.
    with pytest.raises(ValueError, match="not 'DB-06'"):
        DailyBiasReading("prior_close", "DB-06", None, 4)
    # Neutral is not a daily-bias direction (BI-06).
    with pytest.raises(ValueError, match="neutral is not a direction"):
        DailyBiasReading("prior_close", "DB-04", "neutral", 4)
    with pytest.raises(ValueError):
        DailyBiasReading("prior_close", "DB-04", None, 4, timeframe="4h")
    with pytest.raises(UnknownVariant):
        prior_close_bias(96.0, 100.0, 90.0, bar=4, variant_ids=("V99",))


# --------------------------------------------------------------------------- #
# 3. family B -- outside day (DB-05)
# --------------------------------------------------------------------------- #
def test_outside_day_is_the_both_extremes_caution_downgrade():
    outside, refusal = outside_day_bias(100.0, 90.0, 101.0, 89.0, bar=3)
    assert outside.caution is True
    assert outside.bias is None
    assert outside.missing_fields == ("bias",)
    assert refusal is not None and refusal.code is RefusalCode.NT_OUTSIDE_DAY
    assert dict(outside.inputs)["pdh_violated"] is True
    assert dict(outside.inputs)["pdl_violated"] is True

    # Strict through: equality does not violate either extreme, so the
    # downgrade does not fire (no wick/close tolerance is invented).
    at_pdh, refusal = outside_day_bias(100.0, 90.0, 100.0, 89.0, bar=3)
    assert at_pdh.caution is False
    assert refusal is not None and refusal.code is RefusalCode.NT_NO_BIAS
    at_pdl, refusal = outside_day_bias(100.0, 90.0, 101.0, 90.0, bar=3)
    assert at_pdl.caution is False and refusal.code is RefusalCode.NT_NO_BIAS

    # One extreme only is not DB-05's downgrade, and DB-05 supplies no
    # direction of its own.
    one_side, refusal = outside_day_bias(100.0, 90.0, 101.0, 92.0, bar=3)
    assert one_side.caution is False and one_side.bias is None
    assert refusal is not None and refusal.code is RefusalCode.NT_NO_BIAS

    with pytest.raises(ValueError, match="degenerate"):
        outside_day_bias(90.0, 90.0, 101.0, 89.0, bar=3)


# --------------------------------------------------------------------------- #
# 4. family C -- structure x location (DB-06)
# --------------------------------------------------------------------------- #
def test_structure_location_is_the_four_cell_matrix():
    bear, refusal = structure_location_bias("bear", "premium", bar=5)
    assert refusal is None and bear.bias.value == "bearish"
    assert bear.rule == "DB-06"

    bull, refusal = structure_location_bias("bull", "discount", bar=5)
    assert refusal is None and bull.bias.value == "bullish"

    # The two remaining cells are "no bias, wait" -- not a neutral direction.
    for shift, location in (("bull", "premium"), ("bear", "discount")):
        wait, refusal = structure_location_bias(shift, location, bar=5)
        assert wait.bias is None and refusal is not None
        assert refusal.code is RefusalCode.NT_NO_BIAS
        assert "no bias, wait" in refusal.detail

    # No confirmed shift yet: no bias, wait (the detector is P1-07's object).
    no_shift, refusal = structure_location_bias(None, "premium", bar=5)
    assert no_shift.bias is None and refusal.code is RefusalCode.NT_NO_BIAS

    with pytest.raises(ValueError, match="premium or discount"):
        structure_location_bias("bear", "equilibrium", bar=5)
    with pytest.raises(ValueError, match="up or down"):
        structure_location_bias("neutral", "premium", bar=5)


def test_structure_location_refuses_the_at_eq_cell_and_reads_a_range():
    dr = DealingRange(high=110.0, low=100.0, eq=105.0, high_bar=2, low_bar=5)
    assert location_in_range(dr, 108.0)[0] == "premium"
    assert location_in_range(dr, 102.0)[0] == "discount"
    location, refusal = location_in_range(dr, 105.0)
    assert location is None
    assert refusal is not None and refusal.code is RefusalCode.NT_AT_EQ

    reading, refusal = structure_location_from_range(
        "bear", dr, 108.0, bar=9)
    assert refusal is None and reading.bias.value == "bearish"
    assert dict(reading.inputs)["dealing_range_eq"] == 105.0
    assert dict(reading.inputs)["location"] == "premium"

    reading, refusal = structure_location_from_range("bull", dr, 105.0, bar=9)
    assert reading is None and refusal.code is RefusalCode.NT_AT_EQ
    with pytest.raises(TypeError):
        location_in_range({"high": 1}, 100.0)


# --------------------------------------------------------------------------- #
# 5. family G -- the last 1D MSS lock (DB-07)
# --------------------------------------------------------------------------- #
def _mss(direction: str, bar: int, **overrides) -> DailyMSSEvent:
    values = dict(direction=direction, bar=bar)
    values.update(overrides)
    return DailyMSSEvent(**values)


def test_last_1d_mss_lock_holds_until_an_opposite_print():
    # No event yet: no reading, never a fabricated direction.
    empty, refusal = LastDailyMSSLock().read(10)
    assert empty is None and refusal.code is RefusalCode.NT_NO_BIAS

    lock = LastDailyMSSLock([_mss("bear", 5)])
    at_five, refusal = lock.read(5, as_of=9)
    assert refusal is None
    assert at_five.bias.value == "bearish"
    assert at_five.as_of == 9          # the base bar the reading is known at
    assert at_five.rule == "DB-07"

    # Locked: a later bar does not change the held direction.
    later, refusal = lock.read(12)
    assert refusal is None and later.bias.value == "bearish"
    # Look-ahead: the MSS does not exist before its bar.
    early, refusal = lock.read(4)
    assert early is None and refusal.code is RefusalCode.NT_NO_BIAS

    # A same-direction newer MSS keeps the lock; an opposite one flips it.
    same = lock.advance(_mss("bear", 8))
    assert same.direction.value == "bearish"
    flipped = same.advance(_mss("bull", 11))
    assert flipped.direction.value == "bullish"
    assert flipped.read(11)[0].bias.value == "bullish"
    # ``advance`` never mutates the original lock.
    assert lock.direction.value == "bearish"

    # The lock moves forward only, and one bar cannot hold two opposite MSS.
    with pytest.raises(ValueError, match="moves? forward|forward only"):
        lock.advance(_mss("bull", 4))
    with pytest.raises(ValueError, match="opposite"):
        LastDailyMSSLock([_mss("bear", 5), _mss("bull", 5)])
    # A same-direction duplicate at one bar is idempotent.
    assert len(LastDailyMSSLock([_mss("bear", 5), _mss("bear", 5)]).events) == 1


def test_the_lock_only_accepts_a_1d_mss():
    with pytest.raises(ValueError, match="not 'BOS'"):
        _mss("bear", 5, kind="BOS")
    with pytest.raises(ValueError, match="not 'CHoCH'"):
        _mss("bear", 5, kind="CHoCH")
    with pytest.raises(ValueError, match="1D"):
        _mss("bear", 5, timeframe="15m")
    with pytest.raises(ValueError, match="bullish or bearish"):
        _mss("neutral", 5)

    lock = LastDailyMSSLock([_mss("bear", 5, source="ST-06 bear MSS")])
    public = lock.to_public()
    assert public["rule"] == "DB-07"
    assert public["direction"] == "bearish"
    assert public["events"][0]["kind"] == "MSS"
    assert public["events"][0]["timeframe"] == "1D"

    reading, refusal = last_1d_mss_bias([_mss("bull", 3)], 7, as_of=7)
    assert refusal is None and reading.bias.value == "bullish"

# --------------------------------------------------------------------------- #
# 6. conflicting families are refused, never averaged
# --------------------------------------------------------------------------- #
def _bear_prior_close() -> DailyBiasReading:
    reading, refusal = prior_close_bias(91.0, 100.0, 90.0, bar=3)
    assert refusal is None and reading.bias.value == "bearish"
    return reading


def _bull_mss() -> DailyBiasReading:
    reading, refusal = last_1d_mss_bias([_mss("bull", 2)], 4, as_of=4)
    assert refusal is None and reading.bias.value == "bullish"
    return reading


def test_two_declared_families_are_refused_and_never_averaged():
    bear = _bear_prior_close()
    bull = _bull_mss()
    # One family is the only accepted result; it passes through unchanged.
    single, refusal = reconcile_daily_bias([bear])
    assert refusal is None and single is bear

    # Two families that disagree are a NT_BIAS_CONFLICT, and the detail names
    # both families rather than silently picking one.
    conflict, refusal = reconcile_daily_bias([bear, bull])
    assert conflict is None
    assert refusal is not None and refusal.code is RefusalCode.NT_BIAS_CONFLICT
    assert "prior_close" in refusal.detail
    assert "last_1d_mss" in refusal.detail
    assert "disagree" in refusal.detail

    # Two families that agree are refused too: a run declares exactly one, and
    # returning the agreed value would hide the second declaration / average.
    second_bull, refusal = prior_close_bias(96.0, 100.0, 90.0, bar=3)
    agree, refusal = reconcile_daily_bias([second_bull, bull])
    assert agree is None
    assert refusal.code is RefusalCode.NT_BIAS_CONFLICT
    assert "averaging" in refusal.detail

    # No reading at all, and duplicate readings of one family, are separate.
    none, refusal = reconcile_daily_bias([])
    assert none is None and refusal.code is RefusalCode.NT_NO_BIAS
    with pytest.raises(ValueError, match="duplicate daily-bias family"):
        reconcile_daily_bias([bear, _bear_prior_close()])
    with pytest.raises(TypeError):
        reconcile_daily_bias([{"family": "prior_close"}])


# --------------------------------------------------------------------------- #
# 7. the setup config declares one family and refutes a second daily rule
# --------------------------------------------------------------------------- #
def test_setup_config_declares_one_family_and_refuses_a_second_rule():
    for family in DAILY_BIAS_FAMILIES:
        cfg = _permissive_cfg(daily_bias_family=family)
        assert cfg.daily_bias_family == family
        assert cfg.daily_bias_gate is False

    with pytest.raises(ValueError, match="unknown daily_bias_family"):
        _permissive_cfg(daily_bias_family="everything")
    with pytest.raises(ValueError, match="blocked"):
        _permissive_cfg(daily_bias_family="D")
    with pytest.raises(ValueError, match="requires a declared daily_bias_family"):
        _permissive_cfg(daily_bias_gate=True)
    # The repo Candle-2/3 filter is a second daily-bias rule; the families may
    # not be combined or averaged (daily_bias C4).
    with pytest.raises(ValueError, match="may not be combined or averaged"):
        _permissive_cfg(daily_bias_family="prior_close", daily_bias_filter="c2")

    base = _permissive_cfg()
    family = _permissive_cfg(daily_bias_family="prior_close")
    gated = _permissive_cfg(daily_bias_family="prior_close", daily_bias_gate=True)
    assert base.config_hash != family.config_hash
    assert family.config_hash != gated.config_hash


def test_the_family_gate_requires_the_declared_day_boundary():
    df = _three_days()
    result = _result(df, t=2)
    cfg = _permissive_cfg(
        daily_bias_family="prior_close", daily_bias_gate=True,
        variant_config=VariantConfig())
    with pytest.raises(VariantUndeclared) as raised:
        build_setup(df, result, cfg)
    assert raised.value.variant_id == "V04"


# --------------------------------------------------------------------------- #
# 8. build_setup wiring: prior close
# --------------------------------------------------------------------------- #
def test_build_setup_prior_close_gate_refuses_the_opposite_direction():
    # Prior day closes at 91, below the 100/90 midpoint => bearish for today.
    df = _three_days(prior_high=100.0, prior_low=90.0, prior_close=91.0,
                     current_high=96.0, current_low=90.0, current_close=95.0)
    t = 2
    cfg = _permissive_cfg(daily_bias_family="prior_close", daily_bias_gate=True)

    long_result = _result(df, t, "bull")
    refusals: list[dict] = []
    assert build_setup(df, long_result, cfg, refusals=refusals) == []
    assert any(
        refusal["code"] == RefusalCode.NT_BIAS_GATE.value
        and refusal["gate"] == "daily_bias_family"
        and refusal["daily_bias_family"] == "prior_close"
        and refusal["daily_bias_family_rule"] == "DB-04"
        and refusal["daily_bias_family_bias"] == "bearish"
        for refusal in refusals), refusals

    # The matching short direction is admitted and carries the reading.
    short_signals = build_setup(df, _result(df, t, "bear"), cfg)
    assert len(short_signals) == 1
    parts = short_signals[0].quality_parts
    assert parts["daily_bias_family"] == "prior_close"
    assert parts["daily_bias_family_declared"] is True
    assert parts["daily_bias_family_gate_enabled"] is True
    assert parts["daily_bias_family_rule"] == "DB-04"
    assert parts["daily_bias_family_reading"]["bias"] == "bearish"
    assert parts["daily_bias_family_reading"]["inputs"]["prior_range_midpoint"] == 95.0

    # With the gate off the same reading is evidence only.
    evidence = build_setup(
        df, _result(df, t, "bull"),
        _permissive_cfg(daily_bias_family="prior_close"))
    assert len(evidence) == 1
    assert evidence[0].quality_parts["daily_bias_family_gate_enabled"] is False
    assert evidence[0].quality_parts["daily_bias_family_reading"]["bias"] == "bearish"


# --------------------------------------------------------------------------- #
# 9. build_setup wiring: outside day
# --------------------------------------------------------------------------- #
def test_build_setup_outside_day_refuses_with_nt_outside_day():
    # Day 1 is the prior day (100/90); day 2's range 101/89 violates both.
    df = _three_days(prior_high=100.0, prior_low=90.0, prior_close=95.0,
                     current_high=101.0, current_low=89.0, current_close=95.0)
    t = 2
    cfg = _permissive_cfg(daily_bias_family="outside_day", daily_bias_gate=True)
    refusals: list[dict] = []
    assert build_setup(df, _result(df, t, "bull"), cfg, refusals=refusals) == []
    assert any(
        refusal["code"] == RefusalCode.NT_OUTSIDE_DAY.value
        and refusal["gate"] == "daily_bias_family"
        for refusal in refusals), refusals

    # Evidence-only mode records the caution reading without vetoing.
    evidence = build_setup(
        df, _result(df, t, "bull"),
        _permissive_cfg(daily_bias_family="outside_day"))
    assert len(evidence) == 1
    parts = evidence[0].quality_parts
    assert parts["daily_bias_family_rule"] == "DB-05"
    assert parts["daily_bias_family_reading"]["caution"] is True
    assert parts["daily_bias_family_refusal"]["code"] == "NT_OUTSIDE_DAY"


def test_a_family_with_no_direction_is_a_no_bias_refusal_not_agreement():
    # Not an outside day: DB-05 has no direction, so the gate refuses NO_BIAS
    # rather than treating "no downgrade" as permission.
    df = _three_days(prior_high=100.0, prior_low=90.0, prior_close=95.0,
                     current_high=100.0, current_low=92.0, current_close=98.0)
    cfg = _permissive_cfg(daily_bias_family="outside_day", daily_bias_gate=True)
    refusals: list[dict] = []
    assert build_setup(df, _result(df, 2, "bull"), cfg, refusals=refusals) == []
    assert any(
        refusal["code"] == RefusalCode.NT_NO_BIAS.value
        and refusal["gate"] == "daily_bias_family"
        for refusal in refusals), refusals


# --------------------------------------------------------------------------- #
# 10. build_setup wiring: structure x location and the 1D MSS lock
# --------------------------------------------------------------------------- #
def test_build_setup_reads_structure_location_and_the_1d_mss_lock():
    df = _zigzag_frame()
    t = 19
    # At base bar 19 the last completed day is 18: the 1D bear MSS printed at
    # day 14, the 1D dealing range is [84, 108] and close 101 is premium.
    for family, expected_rule in (("structure_location", "DB-06"),
                                  ("last_1d_mss", "DB-07")):
        cfg = _permissive_cfg(daily_bias_family=family, daily_bias_gate=True)
        refusals: list[dict] = []
        assert build_setup(df, _result(df, t, "bull"), cfg,
                           refusals=refusals) == []
        assert any(
            refusal["code"] == RefusalCode.NT_BIAS_GATE.value
            and refusal["daily_bias_family"] == family
            and refusal["daily_bias_family_rule"] == expected_rule
            and refusal["daily_bias_family_bias"] == "bearish"
            for refusal in refusals), (family, refusals)

        shorts = build_setup(df, _result(df, t, "bear"), cfg)
        assert len(shorts) == 1
        reading = shorts[0].quality_parts["daily_bias_family_reading"]
        assert reading["family"] == family
        assert reading["rule"] == expected_rule
        assert reading["bias"] == "bearish"

    # DB-06's evidence identifies the two operands it combined and DB-07's the
    # MSS bar it locked on.
    db06 = build_setup(
        df, _result(df, t, "bear"),
        _permissive_cfg(daily_bias_family="structure_location"))
    inputs = db06[0].quality_parts["daily_bias_family_reading"]["inputs"]
    assert inputs["shift"] == "down"
    assert inputs["location"] == "premium"
    assert inputs["dealing_range_eq"] == 96.0

    db07 = build_setup(
        df, _result(df, t, "bear"),
        _permissive_cfg(daily_bias_family="last_1d_mss"))
    assert db07[0].quality_parts["daily_bias_family_reading"]["inputs"]["mss_bar"] == 14.0


def test_an_undeclared_family_leaves_the_evidence_absent_and_says_so():
    df = _three_days()
    signals = build_setup(df, _result(df, 2, "bull"), _permissive_cfg())
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["daily_bias_family"] is None
    assert parts["daily_bias_family_declared"] is False
    assert parts["daily_bias_family_reading"] is None
    assert parts["daily_bias_family_rule"] is None
