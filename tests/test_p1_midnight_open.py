"""P1-04 -- midnight open as a **price** object, V05 polarity, DB-03.

The audit (``CODE_MAPPING.md`` P1-04, ``time_bias.md`` Missing/T14) found that
the repo had no opening-price series at all: ``grep -i midnight src/`` returned
comments, so the corpus's day anchor ("the 00:00 New York opening price -- ICT's
anchor for the 'true day'", ``daily_bias`` R5) was unimplementable.  Two
families share the name and produce opposite trade sides on the same bar:

* DB-01/DB-02 compare **price with the midnight open** and disagree on the sign
  (``daily_bias`` C2); §14.3 makes V05 a ``BLOCK`` fork, so an undeclared V05
  must refuse rather than pick the majority reading;
* DB-03 compares **the midnight open with the daily CE** -- two prices -- and is
  a ``MODE``, never the ``price > ce`` boolean (C5).

This file locks the P1-04 contract:

* the object carries the opening **price** plus its anchor instant/day, and is
  not a timestamp;
* the price is the open of the first bar of the declared V04 day, and a frame
  whose first bar does not start at the boundary reports ``NT_DATA_MISSING``;
* ``majority`` and ``inverted`` each read both sides, and the two are opposite
  booleans on the same price;
* an undeclared V05 refuses (the read path raises, the gate refuses the run,
  and the evidence reports ``NT_VARIANT_UNDECLARED`` instead of a reading);
* DB-03 compares the open to the CE, keeps its own identity, refuses equality,
  and carries no invented directional mirror on the discount branch;
* ``build_setup`` emits the object, the declared polarity and the DB-03 record
  in ``quality_parts``, and gates the wrong side with ``NT_BIAS_GATE``.

The tests are red on the pre-P1-04 checkout: ``ict_trader.context`` has no
``MidnightOpen``/``daily_open_polarity``/``open_vs_daily_ce``.
"""
from __future__ import annotations

import json
from datetime import time as dtime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    DAILY_OPEN_VS_CE_SOURCE,
    DAILY_POLARITY_OPTIONS,
    DAILY_POLARITY_SOURCES,
    BiasDirection,
    MidnightOpen,
    OpenVsDailyCE,
    daily_open_polarity,
    daily_polarity_mode,
    declare_day_boundary,
    open_vs_daily_ce,
    opening_price,
    resolve_daily_polarity,
)
from ict_trader.realtime.analysis.evidence import midnight_open_evidence
from ict_trader.setup import SetupConfig, build_setup
from ict_trader.variants import (
    RefusalCode,
    VariantConfig,
    VariantUndeclared,
    read_variant_spec,
)

NY = "America/New_York"
#: The first NY midnight in an aligned frame that starts at 00:00 UTC:
#: 2026-11-09 05:00 UTC == 2026-11-09 00:00 America/New_York (EST, UTC-5).
FIRST_ANCHOR = 20


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _crypto_frame(start: str = "2026-11-09 00:00", days: int = 2,
                  minutes: int = 15) -> pd.DataFrame:
    """An aligned 15m UTC frame that rallies into the NY midnight then declines.

    The rally makes the midnight open (bar 20) a distinct price level; the
    decline keeps every later bar *below* that day's open, so the DB-01
    majority reading at those bars is ``bullish``.
    """
    n = int(days * 24 * 60 / minutes)
    i = np.arange(n, dtype=float)
    price = 100.0 + 0.05 * np.minimum(i, FIRST_ANCHOR) - 0.01 * np.maximum(
        i - FIRST_ANCHOR, 0.0)
    return pd.DataFrame({
        "date": pd.Timestamp(start) + pd.Timedelta(minutes=minutes) * i,
        "open": price,
        "high": price + 0.05,
        "low": price - 0.05,
        "close": price + 0.005,
        "volume": 1.0,
    })


def _opening(price: float = 100.0, *, bar: int = FIRST_ANCHOR,
             day: str = "2026-11-09T00:00:00-05:00",
             kind: str = "NY midnight") -> MidnightOpen:
    """A directly constructed price object for the pure comparison tests."""
    anchor = dtime(0, 0) if kind == "NY midnight" else dtime(18, 0)
    return MidnightOpen(
        price=price,
        at=pd.Timestamp("2026-11-09 00:00", tz=NY),
        day=day,
        bar=bar,
        kind=kind,
        zone=NY,
        anchor_time=anchor,
        source="daily_bias R5 (00:00 New York); SYSTEM_SPEC GW-03",
        variant_ids=("V04",),
    )


def _result(df: pd.DataFrame, cisds: list[CISD]) -> SimpleNamespace:
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)),
        cisds=cisds,
        fvgs=[],
        order_blocks=[],
        pd_arrays=[],
        sweeps=[],
        key_levels=[],
    )


def _permissive_cfg(**overrides) -> SetupConfig:
    """A config whose only live directional gate is declared ``none``."""
    values = dict(
        market="crypto", stack="1d_15m", allow_short=True, min_grade=0,
        min_confluence=0, require_sweep=False, origin_filter="any",
        htf_fvg=False, target_mode="fixed", rr=2.0, min_target_rr=0.0,
        max_inducements=-1, regime_features=False, min_disp_body_ratio=0.0,
        min_disp_atr=0.0, min_disp_multiple=0.0, min_setup_score=0.0,
        require_pda_array=False, require_first_fvg=False,
        require_fresh_fvg=False, pd_gate=False, ote_gate=False,
        session_gate="none", quality_profile="standard",
        bias_primitive="none",
    )
    values.update(overrides)
    return SetupConfig(**values)


def _cisd(direction: str, level: float, t: int) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
    )


# --------------------------------------------------------------------------- #
# 1. the open is a price object, not a timestamp
# --------------------------------------------------------------------------- #
def test_midnight_open_is_a_price_object_with_its_anchor_instant():
    boundary = declare_day_boundary("NY midnight")
    frame = _crypto_frame()
    opening, refusal = opening_price(boundary, frame, 30, market="crypto")

    assert refusal is None
    assert isinstance(opening, MidnightOpen)
    anchor = int(boundary.anchor_bars(frame, market="crypto")[30])
    assert anchor == FIRST_ANCHOR
    # The price is the first bar's open, carried as a number on the object.
    assert opening.price == float(frame["open"].iloc[anchor]) == 101.0
    assert opening.bar == anchor
    # ... alongside the instant, in the boundary's zone and with its day key.
    assert opening.at == pd.Timestamp("2026-11-09 05:00", tz="UTC")
    assert opening.at.tz_convert(NY).strftime("%H:%M") == "00:00"
    assert opening.day == boundary.day_key(
        frame["date"].iloc[anchor], market="crypto")
    assert opening.kind == "NY midnight"
    assert opening.is_midnight_open is True
    assert opening.variant_ids == ("V04",)
    # The opening print is knowable from its bar onward, never before it.
    assert opening.is_knowable(anchor) is True
    assert opening.is_knowable(anchor - 1) is False

    public = opening.to_public()
    assert public["price"] == 101.0
    assert public["at"] == "2026-11-09T00:00:00-05:00"
    assert public["variant_id"] == "V04"
    assert json.dumps(public)  # JSON-safe: the price object crosses the boundary
    # A timestamp is not a price: the polarity rule refuses the substitution.
    with pytest.raises(TypeError):
        daily_open_polarity(opening.at, 100.5, polarity="majority")


def test_an_unaligned_first_bar_is_not_reported_as_the_opening_price():
    boundary = declare_day_boundary("NY midnight")
    # Bars start at :03, so no bar opens at 00:00 NY; the first bar of the day
    # opens at 00:03 and its open is not the boundary's price.
    frame = _crypto_frame(start="2026-11-09 00:03")
    opening, refusal = opening_price(boundary, frame, 30, market="crypto")

    assert opening is None
    assert refusal is not None
    assert refusal.code is RefusalCode.NT_DATA_MISSING
    assert "opening price" in refusal.detail


def test_a_non_midnight_v04_open_is_labelled_rather_than_renamed():
    # Under a different V04 option the object is that day's opening price; it
    # must not claim to be the corpus's midnight open.
    opening = _opening(price=100.0, kind="futures open")
    assert opening.is_midnight_open is False
    assert opening.to_public()["kind"] == "futures open"


# --------------------------------------------------------------------------- #
# 2-3. V05 polarity, both readings, both sides
# --------------------------------------------------------------------------- #
def test_v05_is_the_register_fork_and_names_its_sources():
    assert DAILY_POLARITY_OPTIONS == read_variant_spec("V05").options
    assert set(DAILY_POLARITY_SOURCES) == set(DAILY_POLARITY_OPTIONS)
    assert "DB-01" in DAILY_POLARITY_SOURCES["majority"]
    assert "DB-02" in DAILY_POLARITY_SOURCES["inverted"]
    assert read_variant_spec("V05").default_policy.value == "BLOCK"


def test_majority_polarity_reads_both_sides_and_has_no_equality_branch():
    opening = _opening(price=100.0)

    below, refusal = daily_open_polarity(opening, 99.5, polarity="majority")
    assert refusal is None
    assert below is BiasDirection.BULL
    above, refusal = daily_open_polarity(opening, 100.5, polarity="majority")
    assert refusal is None
    assert above is BiasDirection.BEAR
    # DB-01 declares only < and >; equality is a coded refusal, not a side.
    equal, refusal = daily_open_polarity(opening, 100.0, polarity="majority")
    assert equal is None
    assert refusal.code is RefusalCode.NT_NO_BIAS


def test_inverted_polarity_is_the_opposite_boolean_on_the_same_bar():
    opening = _opening(price=100.0)
    for price in (99.0, 101.0):
        majority, _ = daily_open_polarity(opening, price, polarity="majority")
        inverted, _ = daily_open_polarity(opening, price, polarity="inverted")
        assert {majority, inverted} == {BiasDirection.BULL, BiasDirection.BEAR}

    below, refusal = daily_open_polarity(opening, 99.0, polarity="inverted")
    assert refusal is None
    assert below is BiasDirection.BEAR
    above, refusal = daily_open_polarity(opening, 101.0, polarity="inverted")
    assert refusal is None
    assert above is BiasDirection.BULL


def test_an_unknown_polarity_option_refuses():
    opening = _opening()
    with pytest.raises(ValueError, match="V05"):
        daily_open_polarity(opening, 99.0, polarity="bullish")


# --------------------------------------------------------------------------- #
# 4. an undeclared V05 refuses -- no majority fallback
# --------------------------------------------------------------------------- #
def test_unset_v05_refuses_to_fabricate_a_polarity():
    with pytest.raises(VariantUndeclared) as raised:
        resolve_daily_polarity(VariantConfig())
    assert raised.value.variant_id == "V05"
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED

    mode, refusal = daily_polarity_mode(VariantConfig())
    assert mode is None
    assert refusal.variant_id == "V05"
    assert refusal.code is RefusalCode.NT_VARIANT_UNDECLARED


def test_midnight_open_gate_refuses_an_undeclared_v05_or_v04():
    frame = _crypto_frame()
    t = 40
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])

    # V04 declared, V05 unset: the gate needs the reading and refuses it.
    cfg = _permissive_cfg(
        variant_config=VariantConfig({"V04": "NY midnight"}),
        midnight_open_gate=True)
    with pytest.raises(VariantUndeclared) as raised:
        build_setup(frame, result, cfg)
    assert raised.value.variant_id == "V05"

    # V05 declared, V04 unset: there is no clock, so there is no open to read.
    cfg = _permissive_cfg(
        variant_config=VariantConfig({"V05": "majority"}),
        midnight_open_gate=True)
    with pytest.raises(VariantUndeclared) as raised:
        build_setup(frame, result, cfg)
    assert raised.value.variant_id == "V04"

    # Both declared: the same call proceeds (the fixture admits the long).
    cfg = _permissive_cfg(
        variant_config=VariantConfig(
            {"V04": "NY midnight", "V05": "majority"}),
        midnight_open_gate=True)
    assert len(build_setup(frame, result, cfg)) == 1


def test_evidence_reports_the_v05_refusal_instead_of_a_reading():
    evidence = midnight_open_evidence(
        _opening(), price=99.0, variant_config=VariantConfig())

    assert evidence["polarity"] is None
    assert evidence["polarity_refusal"]["code"] == "NT_VARIANT_UNDECLARED"
    assert evidence["polarity_refusal"]["variant_id"] == "V05"
    assert "polarity" in evidence["missing_fields"]
    # Nothing on the record claims a direction.
    assert "bullish" not in json.dumps(evidence)


def test_a_declared_v04_without_v05_reports_no_polarity_and_no_default():
    # The open object is emitted under V04, but the V05 reading is simply not
    # declared: no majority reading may be slipped in on the run's behalf.
    frame = _crypto_frame()
    t = 40
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    signals = build_setup(
        frame, result,
        _permissive_cfg(variant_config=VariantConfig({"V04": "NY midnight"})))

    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["midnight_open"] is not None
    assert parts["midnight_open_polarity"] == ""
    assert parts["midnight_open_polarity_mode"] is None
    assert parts["midnight_open_polarity_source"] is None


def test_evidence_reports_a_missing_daily_ce_instead_of_a_comparison():
    evidence = midnight_open_evidence(
        _opening(), price=99.0,
        variant_config=VariantConfig({"V05": "majority"}))

    assert evidence["daily_ce"] is None
    assert evidence["open_vs_daily_ce"] is None
    assert evidence["open_vs_daily_ce_refusal"] is None
    assert "daily_ce" in evidence["missing_fields"]


# --------------------------------------------------------------------------- #
# 5. DB-03: two prices, its own identity, no equality branch
# --------------------------------------------------------------------------- #
def test_db03_compares_the_open_to_the_ce_not_the_current_price():
    opening = _opening(price=101.0)
    # The current price (99.0) is below the CE (100.0); the open (101.0) is
    # above it.  DB-03 must follow the open, never the current price.
    comparison, refusal = open_vs_daily_ce(opening, 100.0)
    assert refusal is None
    assert isinstance(comparison, OpenVsDailyCE)
    assert comparison.location == "premium"
    assert comparison.judas_lean is BiasDirection.BEAR
    assert comparison.judas_lean_source == DAILY_OPEN_VS_CE_SOURCE
    assert comparison.compares == ("midnight_open.price", "daily_ce")
    public = comparison.to_public()
    assert public["rule"] == "DB-03"
    assert public["location"] == "premium"
    assert public["judas_lean"] == "bearish"
    # A price-vs-CE boolean would call 99.0 "discount"; DB-03 never does.
    assert daily_open_polarity(
        opening, 99.0, polarity="majority")[0] is BiasDirection.BULL

    # The first operand is a named price object, not any float.
    with pytest.raises(TypeError):
        open_vs_daily_ce(99.0, 100.0)

    # Equality has no branch: the open at the CE refuses.
    at_ce, refusal = open_vs_daily_ce(opening, 101.0)
    assert at_ce is None
    assert refusal.code is RefusalCode.NT_AT_EQ


def test_db03_discount_branch_carries_no_invented_mirror():
    comparison, refusal = open_vs_daily_ce(_opening(price=99.0), 100.0)
    assert refusal is None
    assert comparison.location == "discount"
    assert comparison.judas_lean is None
    assert comparison.judas_lean_source is None
    assert comparison.missing_fields == ("judas_lean",)
    assert comparison.to_public()["judas_lean"] == ""


# --------------------------------------------------------------------------- #
# 6. evidence output and the wired gate
# --------------------------------------------------------------------------- #
def test_midnight_open_evidence_carries_the_object_polarity_and_db03():
    opening = _opening(price=101.0)
    evidence = midnight_open_evidence(
        opening, price=100.5,
        variant_config=VariantConfig({"V05": "majority"}),
        daily_ce=100.0)

    assert json.dumps(evidence)  # JSON-safe
    assert evidence["midnight_open"]["price"] == 101.0
    assert evidence["midnight_open"]["bar"] == FIRST_ANCHOR
    assert evidence["midnight_open"]["at"] == "2026-11-09T00:00:00-05:00"
    assert evidence["price"] == 100.5
    assert evidence["polarity"]["variant_id"] == "V05"
    assert evidence["polarity"]["mode"] == "majority"
    assert evidence["polarity"]["reading"] == "bullish"
    assert evidence["polarity_refusal"] is None
    assert evidence["daily_ce"] == 100.0
    assert evidence["open_vs_daily_ce"]["rule"] == "DB-03"
    assert evidence["open_vs_daily_ce"]["location"] == "premium"
    assert evidence["open_vs_daily_ce"]["judas_lean"] == "bearish"
    assert evidence["missing_fields"] == []

    # The two booleans really are different on this bar: DB-01 reads the price
    # below the open as bullish while DB-03 reads the open above the CE as a
    # bearish Judas.  Collapsing them into one `price > ce` would be C5.
    assert evidence["polarity"]["reading"] == "bullish"
    assert evidence["open_vs_daily_ce"]["judas_lean"] == "bearish"


def test_build_setup_emits_the_midnight_open_polarity_and_db03():
    boundary = declare_day_boundary("NY midnight")
    frame = _crypto_frame()
    t = 40
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    cfg = _permissive_cfg(
        variant_config=VariantConfig(
            {"V04": "NY midnight", "V05": "majority"}),
        midnight_open_gate=True)

    signals = build_setup(frame, result, cfg, daily_ce=100.5)

    assert len(signals) == 1
    parts = signals[0].quality_parts
    anchor = int(boundary.anchor_bars(frame, market="crypto")[t])
    assert parts["day_boundary"] == "NY midnight"
    assert parts["day_anchor_bar"] == anchor
    assert parts["midnight_open_declared"] is True
    assert parts["midnight_open_gate"] is True
    assert parts["midnight_open"]["price"] == float(
        frame["open"].iloc[anchor])
    assert parts["midnight_open"]["bar"] == anchor
    assert parts["midnight_open"]["day"] == boundary.day_key(
        frame["date"].iloc[anchor], market="crypto")
    assert parts["midnight_open"]["at"].endswith("-05:00")
    assert parts["midnight_open_source"].strip()
    assert parts["midnight_open_polarity_mode"] == "majority"
    assert parts["midnight_open_polarity"] == "bullish"  # close < open
    assert parts["midnight_open_polarity_source"] == (
        DAILY_POLARITY_SOURCES["majority"])
    assert parts["midnight_open_polarity_refusal"] is None
    assert parts["daily_ce"] == 100.5
    assert parts["daily_open_vs_ce"]["location"] == "premium"  # 101.0 > 100.5
    assert parts["daily_open_vs_ce"]["judas_lean"] == "bearish"


def test_build_setup_gates_the_wrong_side_with_a_code():
    frame = _crypto_frame()
    t = 40
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    # The long is below the midnight open, so the majority reading is bullish;
    # declaring the inverted reading (DB-02) makes the same bar bearish.
    cfg = _permissive_cfg(
        variant_config=VariantConfig(
            {"V04": "NY midnight", "V05": "inverted"}),
        midnight_open_gate=True)
    refusals: list[dict] = []

    signals = build_setup(frame, result, cfg, refusals=refusals)

    assert signals == []
    assert any(
        refusal["code"] == RefusalCode.NT_BIAS_GATE.value
        and refusal["gate"] == "midnight_open"
        and refusal["midnight_open_polarity"] == "bearish"
        for refusal in refusals), refusals


def test_build_setup_refuses_a_boundary_no_bar_opens_at():
    # No bar starts at 00:00 NY, so the day has no opening price: the gate
    # records NT_DATA_MISSING rather than gating on an arbitrary bar's open.
    frame = _crypto_frame(start="2026-11-09 00:03")
    t = 40
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    cfg = _permissive_cfg(
        variant_config=VariantConfig(
            {"V04": "NY midnight", "V05": "majority"}),
        midnight_open_gate=True)
    refusals: list[dict] = []

    assert build_setup(frame, result, cfg, refusals=refusals) == []
    assert any(
        refusal["code"] == RefusalCode.NT_DATA_MISSING.value
        and refusal["gate"] == "midnight_open"
        for refusal in refusals), refusals


def test_an_undeclared_run_carries_no_midnight_open_and_no_fabricated_reading():
    frame = _crypto_frame()
    t = 40
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    signals = build_setup(frame, result, _permissive_cfg())

    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["midnight_open"] is None
    assert parts["midnight_open_declared"] is False
    assert parts["midnight_open_gate"] is False
    assert parts["midnight_open_polarity"] == ""
    assert parts["midnight_open_polarity_mode"] is None
    assert parts["midnight_open_polarity_source"] is None
    assert parts["daily_open_vs_ce"] is None
    assert parts["daily_ce"] is None
