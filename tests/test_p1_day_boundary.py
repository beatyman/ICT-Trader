"""P1-03 -- one declared day boundary (V04 / SYSTEM_SPEC GW-01).

The audit (``CODE_MAPPING.md`` P1-03, ``TB-C7``/``TB-M3``) found three
competing day conventions in the repo -- a UTC-midnight ``resample("1D")``, a
supplied daily CSV re-stamped ``+15 h``/``+16 h``, and ISO-week grouping -- none
of them the corpus's New York anchor.  ``daily_bias`` R10 makes the fork
explicit: four legitimate boundaries exist, each can produce a different day,
and the run must "pick one and do not mix them".

This file locks the P1-03 contract:

* every V04 option exists and has one declared clock;
* the four options really are different days at the same instant;
* the ``ny_midnight_crypto`` fixture (00:00 NY != 00:00 UTC) buckets the 1D
  bias tier on the declared convention only;
* an unset V04 refuses with ``NT_VARIANT_UNDECLARED`` instead of defaulting to
  UTC midnight;
* the daily budget key and the daily anchor are read from the same boundary.

The tests are red on the pre-P1-03 checkout: ``ict_trader.context`` has no
``DayBoundary``/``declare_day_boundary``/``resolve_day_boundary`` and the 1D
tier is always UTC-midnight resampled.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    DAY_BOUNDARY_CLOCKS,
    DAY_BOUNDARY_OPTIONS,
    DayBoundary,
    declare_day_boundary,
    resolve_day_boundary,
)
from ict_trader.setup import (
    SetupConfig,
    _bias_structure_frame,
    _raw_htf,
    _tf_frame,
    build_setup,
)
from ict_trader.variants import (
    RefusalCode,
    VariantConfig,
    VariantUndeclared,
    read_variant_spec,
)

NY = "America/New_York"
#: 2026-11-09 15:00 UTC == 2026-11-09 10:00 America/New_York (EST, UTC-5).
#: A single instant at which the four V04 boundaries name four different days.
ONE_INSTANT = pd.Timestamp("2026-11-09 15:00", tz="UTC")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _crypto_frame(start: str = "2026-11-09 00:00", days: int = 3,
                  minutes: int = 15) -> pd.DataFrame:
    """The ``ny_midnight_crypto`` fixture: naive UTC 15m bars.

    Prices ramp monotonically, so the open at 00:00 UTC and the open at 00:00
    New York (05:00 UTC in November) are distinguishable.
    """
    n = int(days * 24 * 60 / minutes)
    i = np.arange(n)
    base = 100.0 + 0.01 * i
    return pd.DataFrame({
        "date": pd.Timestamp(start) + pd.Timedelta(minutes=minutes) * i,
        "open": base,
        "high": base + 0.05,
        "low": base - 0.05,
        "close": base + 0.01,
        "volume": 1.0,
    })


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
# 1. the register is the only vocabulary, and every option has one clock
# --------------------------------------------------------------------------- #
def test_v04_options_and_clocks_are_declared_once():
    spec = read_variant_spec("V04")
    assert spec.name == "day boundary"
    assert spec.blocks == "G0"
    assert DAY_BOUNDARY_OPTIONS == spec.options == (
        "NY midnight", "broker day", "futures open", "cash open")
    assert tuple(DAY_BOUNDARY_CLOCKS) == DAY_BOUNDARY_OPTIONS
    # Three clocks are corpus-attested; broker day deliberately has none.
    assert DAY_BOUNDARY_CLOCKS["NY midnight"][1] is not None
    assert DAY_BOUNDARY_CLOCKS["futures open"][1] is not None
    assert DAY_BOUNDARY_CLOCKS["cash open"][1] is not None
    assert DAY_BOUNDARY_CLOCKS["broker day"][1] is None


# --------------------------------------------------------------------------- #
# 2. each declared boundary anchors at its own clock, and they disagree
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kind,broker_roll,expected",
    [
        ("NY midnight", None,
         pd.Timestamp("2026-11-09 00:00", tz=NY)),
        ("broker day", "17:00",
         pd.Timestamp("2026-11-08 17:00", tz=NY)),
        ("futures open", None,
         pd.Timestamp("2026-11-08 18:00", tz=NY)),
        ("cash open", None,
         pd.Timestamp("2026-11-09 09:30", tz=NY)),
    ],
)
def test_each_declared_boundary_anchors_at_its_clock(kind, broker_roll, expected):
    boundary = declare_day_boundary(kind, broker_roll=broker_roll)
    start = boundary.bucket_start(ONE_INSTANT)
    end = boundary.bucket_end(ONE_INSTANT)

    assert start == expected
    assert expected <= ONE_INSTANT < end
    assert boundary.day_key(ONE_INSTANT) == start.isoformat()
    # The end is the next anchor at the same wall clock (DST-safe localisation).
    assert end.tz_convert(NY).time() == boundary.anchor_time
    assert start < end

    # Every option names one declared clock; the boundary carries its source.
    assert boundary.kind == kind
    assert boundary.zone == NY
    assert boundary.source.strip()
    assert boundary.to_public()["variant_id"] == "V04"
    assert boundary.to_public()["kind"] == kind


def test_the_four_boundaries_really_are_four_different_days():
    starts = {
        declare_day_boundary(
            "broker day", broker_roll="17:00").bucket_start(ONE_INSTANT),
        declare_day_boundary("NY midnight").bucket_start(ONE_INSTANT),
        declare_day_boundary("futures open").bucket_start(ONE_INSTANT),
        declare_day_boundary("cash open").bucket_start(ONE_INSTANT),
    }
    # daily_bias R10: "each can produce a different PO3 shape on the same date".
    assert len(starts) == 4
    # ...and no two keys collide.
    keys = {
        declare_day_boundary(
            "broker day", broker_roll="17:00").day_key(ONE_INSTANT),
        declare_day_boundary("NY midnight").day_key(ONE_INSTANT),
        declare_day_boundary("futures open").day_key(ONE_INSTANT),
        declare_day_boundary("cash open").day_key(ONE_INSTANT),
    }
    assert len(keys) == 4


def test_boundary_clock_is_dst_aware():
    cash = declare_day_boundary("cash open")
    summer = cash.bucket_start(pd.Timestamp("2026-07-01 13:00", tz="UTC"))
    winter = cash.bucket_start(pd.Timestamp("2026-12-01 14:00", tz="UTC"))

    assert summer.tz_convert(NY).strftime("%H:%M") == "09:30"
    assert winter.tz_convert(NY).strftime("%H:%M") == "09:30"
    assert summer.utcoffset() == pd.Timedelta(hours=-4)   # EDT
    assert winter.utcoffset() == pd.Timedelta(hours=-5)   # EST


# --------------------------------------------------------------------------- #
# 3. T12 fixture: 00:00 NY != 00:00 UTC
# --------------------------------------------------------------------------- #
def test_ny_midnight_bucket_differs_from_utc_midnight():
    boundary = declare_day_boundary("NY midnight")
    # 23:30 New York on 2026-11-09 and 00:30 New York on 2026-11-10 are the
    # same UTC date, so a UTC-date budget cannot tell them apart.
    late = pd.Timestamp("2026-11-10 04:30", tz="UTC")
    early = pd.Timestamp("2026-11-10 05:30", tz="UTC")
    assert late.date() == early.date()

    late_start = boundary.bucket_start(late)
    early_start = boundary.bucket_start(early)
    assert late_start == pd.Timestamp("2026-11-09 00:00", tz=NY)
    assert early_start == pd.Timestamp("2026-11-10 00:00", tz=NY)
    assert boundary.day_key(late) != boundary.day_key(early)
    # The two days are adjacent, exactly one boundary apart.
    assert early_start - late_start == pd.Timedelta(days=1)

    # And the derived 1D bar opens at the 00:00 NY price, not 00:00 UTC.
    frame = _crypto_frame("2026-11-09 00:00", days=2)
    daily = boundary.bucket_frame(frame, market="crypto")
    ny_open = float(frame.loc[frame["date"] == pd.Timestamp("2026-11-09 05:00"),
                              "open"].iloc[0])
    utc_open = float(frame.loc[frame["date"] == pd.Timestamp("2026-11-09 00:00"),
                               "open"].iloc[0])
    assert ny_open != utc_open
    # The bucket ending 2026-11-10 05:00 UTC is the 2026-11-09 New York day.
    assert float(daily["open"].iloc[1]) == ny_open
    assert float(daily["open"].iloc[1]) != utc_open


# --------------------------------------------------------------------------- #
# 4. the declared boundary drives the 1D bias tier, not UTC midnight
# --------------------------------------------------------------------------- #
def test_declared_boundary_drives_the_1d_bias_bucket():
    boundary = declare_day_boundary("NY midnight")
    frame = _crypto_frame("2026-11-09 00:00", days=2)

    raw = _raw_htf(frame, None, "1D", "crypto", day_boundary=boundary)
    assert raw is not None
    # Two complete 1D bars; the still-forming third is dropped.
    assert len(raw) == 2
    # Labels are the bucket ends (known-at), never left/UTC-midnight labels.
    assert list(pd.to_datetime(raw["date"])) == [
        pd.Timestamp("2026-11-09 05:00"), pd.Timestamp("2026-11-10 05:00")]
    assert float(raw["open"].iloc[1]) == float(frame["open"].iloc[20])

    structure = _bias_structure_frame(frame, None, "1D", "crypto", boundary)
    assert len(structure) == len(raw)
    assert float(structure["open"].iloc[1]) == float(frame["open"].iloc[20])

    aligned = _tf_frame(frame, None, "1D", "crypto", boundary)
    # Bar 20 closes the first NY day, so the daily row first becomes knowable
    # exactly there; bar 19 (04:45 UTC, still 23:45 NY) must not see it.
    assert np.isnan(aligned["close"].to_numpy(float)[19])
    assert float(aligned["close"].iloc[20]) == float(
        boundary.bucket_frame(frame, market="crypto")["close"].iloc[0])


def test_undeclared_path_is_labelled_instead_of_assuming_a_boundary():
    # The compatibility path no longer *claims* a day boundary: every setup
    # says the run has none.  (P1-03 cannot silently keep UTC midnight.)
    frame = _crypto_frame("2026-11-09 00:00", days=2)
    t = len(frame) - 2
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    signals = build_setup(frame, result, _permissive_cfg())

    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["day_boundary"] is None
    assert parts["day_boundary_declared"] is False
    assert "undeclared" in parts["day_boundary_source"]
    assert parts["day_key"] is None
    assert parts["day_anchor_bar"] is None
    assert parts["day_anchor_price"] is None


# --------------------------------------------------------------------------- #
# 5. an unset V04 refuses -- no default, no UTC midnight
# --------------------------------------------------------------------------- #
def test_unset_day_boundary_refuses_with_variant_undeclared():
    with pytest.raises(VariantUndeclared) as raised:
        resolve_day_boundary(VariantConfig())
    assert raised.value.variant_id == "V04"
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED

    # The config surface refuses through the same read path.
    with pytest.raises(VariantUndeclared):
        SetupConfig().day_boundary
    assert not SetupConfig().variant_config.is_declared("V04")


def test_broker_day_without_a_broker_clock_refuses():
    cfg = SetupConfig(variant_config=VariantConfig({"V04": "broker day"}))
    with pytest.raises(ValueError, match="broker"):
        cfg.day_boundary
    # Supplying the broker clock is the declaration; it is then usable.
    cfg = SetupConfig(variant_config=VariantConfig({"V04": "broker day"}),
                      broker_roll="17:00")
    assert cfg.day_boundary.kind == "broker day"
    assert cfg.day_boundary.anchor_time.strftime("%H:%M") == "17:00"
    assert cfg.day_boundary.broker_supplied_clock is True


def test_a_clock_the_option_does_not_read_refuses():
    with pytest.raises(ValueError, match="broker"):
        declare_day_boundary("cash open", broker_roll="17:00")
    with pytest.raises(ValueError):
        SetupConfig(broker_roll="not-a-clock")
    with pytest.raises(ValueError):
        declare_day_boundary("UTC midnight")
    with pytest.raises(ValueError):
        declare_day_boundary("NY midnight", broker_zone="Mars/Olympus")


def test_naive_stamps_need_a_declared_input_zone():
    boundary = declare_day_boundary("NY midnight")
    with pytest.raises(ValueError, match="naive"):
        boundary.bucket_start(pd.Timestamp("2026-11-09 15:00"))
    assert boundary.day_key(
        pd.Timestamp("2026-11-09 15:00"), market="crypto"
    ) == "2026-11-09T00:00:00-05:00"


def test_required_day_boundary_refuses_an_undeclared_run():
    frame = _crypto_frame("2026-11-09 00:00", days=1)
    result = _result(frame, [])
    with pytest.raises(VariantUndeclared) as raised:
        build_setup(frame, result, _permissive_cfg(),
                    day_boundary_required=True)
    assert raised.value.variant_id == "V04"
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED

    # Declared: the same call proceeds (the test fixture has no candidates).
    cfg = _permissive_cfg(
        variant_config=VariantConfig({"V04": "NY midnight"}))
    assert build_setup(frame, result, cfg, day_boundary_required=True) == []


# --------------------------------------------------------------------------- #
# 6. two declarations may not disagree (GW-01: declare exactly one)
# --------------------------------------------------------------------------- #
def test_a_second_disagreeing_boundary_refuses():
    frame = _crypto_frame("2026-11-09 00:00", days=2)
    result = _result(frame, [])
    declared = VariantConfig({"V04": "NY midnight"})

    # Same boundary: the explicit value is accepted.
    assert build_setup(
        frame, result, _permissive_cfg(variant_config=declared),
        day_boundary=declare_day_boundary("NY midnight")) == []

    with pytest.raises(ValueError, match="disagree"):
        build_setup(
            frame, result, _permissive_cfg(variant_config=declared),
            day_boundary=declare_day_boundary("futures open"))

    # A passed boundary is a cache for the V04 declaration, never a second
    # declaration path (A7).
    with pytest.raises(VariantUndeclared):
        build_setup(
            frame, result, _permissive_cfg(),
            day_boundary=declare_day_boundary("NY midnight"))


# --------------------------------------------------------------------------- #
# 7. the daily budget key and the daily anchor share one boundary
# --------------------------------------------------------------------------- #
def test_daily_budget_key_and_anchor_share_the_declared_boundary():
    boundary = declare_day_boundary("NY midnight")
    frame = _crypto_frame("2026-11-09 00:00", days=3)
    t = len(frame) - 2
    cfg = _permissive_cfg(
        variant_config=VariantConfig({"V04": "NY midnight"}),
        max_trades_per_day=2)
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    signals = build_setup(frame, result, cfg)

    assert len(signals) == 1
    parts = signals[0].quality_parts
    keys = boundary.day_keys(frame, market="crypto")
    anchors = boundary.anchor_bars(frame, market="crypto")
    daily = boundary.bucket_frame(frame, market="crypto")

    assert parts["day_boundary"] == "NY midnight"
    assert parts["day_boundary_declared"] is True
    assert parts["day_key"] == keys[t] == boundary.day_key(
        frame["date"].iloc[t], market="crypto")
    assert parts["day_anchor_bar"] == int(anchors[t])
    anchor = int(anchors[t])
    assert parts["day_anchor_price"] == float(frame["open"].iloc[anchor])

    # The anchor bar really is the first bar of that day, and the day's open in
    # the derived daily frame is the same price (budget key == anchor day).
    assert anchor == 0 or keys[anchor - 1] != keys[anchor]
    bucket = list(dict.fromkeys(keys)).index(keys[t])
    assert float(daily["open"].iloc[bucket]) == float(frame["open"].iloc[anchor])

    # A budget keyed by the boundary resets there, not at UTC midnight: the
    # 23:30 NY and 00:30 NY bars are one UTC date but two budget days.
    late = pd.Timestamp("2026-11-10 04:30", tz="UTC")
    early = pd.Timestamp("2026-11-10 05:30", tz="UTC")
    counts: dict[str, int] = {}
    for stamp in (late, early):
        key = boundary.day_key(stamp)
        counts[key] = counts.get(key, 0) + 1
    assert len(counts) == 2
    assert boundary.day_key(late) == "2026-11-09T00:00:00-05:00"
    assert boundary.day_key(early) == "2026-11-10T00:00:00-05:00"

    # The per-bar budget account is contiguous inside one day and changes only
    # at an anchor bar.
    for i in range(1, len(frame)):
        if keys[i] != keys[i - 1]:
            assert int(anchors[i]) == i


def test_day_boundary_is_carried_on_the_setup_and_in_the_hash():
    cfg = SetupConfig(variant_config=VariantConfig({"V04": "cash open"}))
    assert cfg.day_boundary.kind == "cash open"
    assert cfg.config_hash != SetupConfig().config_hash
    assert cfg.config_hash == SetupConfig(
        variant_config=VariantConfig({"V04": "cash open"})).config_hash

    # The object records provenance; it never promotes a clock to corpus.
    assert isinstance(cfg.day_boundary, DayBoundary)
    public = cfg.day_boundary.to_public()
    assert public["source"].strip()
    assert public["anchor_time"] == "09:30"
    assert public["zone"] == NY
