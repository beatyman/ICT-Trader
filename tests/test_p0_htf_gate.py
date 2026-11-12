"""P0-09 - the HTF structure gate reads the declared bias timeframe.

TF-01/TF-02: the bias tier is declared separately from the base/entry tier, and
sub-4H frames never set bias.  The gate named ``htf_structure_gate`` therefore
had to stop reading the base frame's swings and read ``spec.bias_tf`` instead.

The corpus primitive is HH/HL vs LH/LL (BI-01).  It is the *only* read named
``structure_bias``; the realtime pivot/BOS read was renamed ``pivot_bos_bias``
so the two algorithms cannot be confused (P0-09).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (BiasDirection as BiasState, parse_bias,
                                serialize_bias, structure_bias)
from ict_trader.realtime.analysis import structure as structure_module
from ict_trader.realtime.analysis.evidence import htf_bias_evidence
from ict_trader.realtime.analysis.structure import pivot_bos_bias
from ict_trader.setup import SetupConfig, _SetupPrecompute, _stack_spec, build_setup

PIVOT_LEFT = 5


def _frame(n: int, start: str, minutes: int, base: float, slope: float,
           amp: float, period: float) -> pd.DataFrame:
    """Oscillating series so the repo pivot scan produces an alternating zigzag."""
    rows = []
    t0 = pd.Timestamp(start)
    for i in range(n):
        close = base + slope * i + amp * np.sin(i / period)
        rows.append({
            "date": t0 + pd.Timedelta(minutes=minutes * i),
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _base_bearish_frame() -> pd.DataFrame:
    # 15m base/entry frame: lower highs AND lower lows at the last bar.
    return _frame(240, "2025-12-20", 15, 200.0, -0.08, 3.0, 7.0)


def _daily_bullish_frame() -> pd.DataFrame:
    # 1D bias frame: higher highs AND higher lows at the last bar.
    return _frame(120, "2025-09-01", 1440, 100.0, 0.4, 8.0, 6.0)


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


def _cisd(direction: str, level: float, t: int) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
    )


def _gate_cfg(**overrides) -> SetupConfig:
    """A permissive config whose only live directional gate is the structure gate."""
    cfg = dict(
        market="other",
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
        use_score=False,
        require_sweep=False,
        origin_filter="any",
        htf_fvg=False,
        target_mode="fixed",
        rr=2.0,
        min_target_rr=0.0,
        max_inducements=-1,
        regime_features=False,
        htf_structure_gate="aligned",
        daily_bias_filter="none",
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
        session_gate="none",
        quality_profile="standard",
    )
    cfg.update(overrides)
    return SetupConfig(**cfg)


# ---------------------------------------------------------------------------
# the gate uses spec.bias_tf, not the base/entry frame
# ---------------------------------------------------------------------------

def test_gate_reads_bias_tf_even_when_base_tf_structure_is_opposite():
    df, daily = _base_bearish_frame(), _daily_bullish_frame()
    cfg = _gate_cfg()
    spec = _stack_spec(cfg)
    assert spec.bias_tf == "1D"

    t = len(df) - 1
    pre = _SetupPrecompute(df, _result(df, []), cfg, daily, spec)

    # The base/entry frame is bearish; the declared bias tier is bullish.
    assert pre.swings.structure_bias(t) == BiasState.BEAR.value
    assert pre.bias_structure(t) == BiasState.BULL.value


def test_base_tf_bearish_pattern_cannot_satisfy_a_1d_bullish_gate():
    df, daily = _base_bearish_frame(), _daily_bullish_frame()
    cfg = _gate_cfg()
    t = len(df) - 1
    level = float(df["close"].iloc[t])

    # Old behaviour: a bearish base-TF pattern passed the "aligned" gate for a
    # short.  The 1D bias is bullish, so the short must now be refused.
    short = _cisd("bear", level, t)
    assert build_setup(df, _result(df, [short]), cfg, daily=daily) == []

    # The same 1D bullish state permits the long, and records the tf it read.
    long = _cisd("bull", level, t)
    signals = build_setup(df, _result(df, [long]), cfg, daily=daily)
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["htf_structure"] == BiasState.BULL.value
    assert parts["htf_structure_tf"] == "1D"
    assert parts["htf_structure_tf"] == _stack_spec(cfg).bias_tf
    assert parts["bias_tf"] == "1D"


# ---------------------------------------------------------------------------
# one enum + one serializer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["bull", "bullish", "long", "BULL", " bullish "])
def test_parse_bias_maps_every_bull_spelling(value):
    assert parse_bias(value) is BiasState.BULL


@pytest.mark.parametrize("value", ["bear", "bearish", "short", "BEARISH"])
def test_parse_bias_maps_every_bear_spelling(value):
    assert parse_bias(value) is BiasState.BEAR


@pytest.mark.parametrize("value", ["flat", "neutral", "FLAT", " Neutral "])
def test_flat_and_neutral_are_one_state(value):
    assert parse_bias(value) is BiasState.NEUTRAL


def test_serializer_is_the_only_wire_form():
    assert serialize_bias("bull") == "bullish"
    assert serialize_bias("flat") == "neutral"
    assert serialize_bias(BiasState.BEAR) == "bearish"
    # "no reading" is not neutral and must not become a direction.
    assert serialize_bias(None) == ""
    with pytest.raises(ValueError):
        serialize_bias("sideways")


def test_structure_bias_returns_only_declared_enum_values():
    bull = build_zigzag(find_pivots(_daily_bullish_frame(), PIVOT_LEFT, 1))
    bear = build_zigzag(find_pivots(_base_bearish_frame(), PIVOT_LEFT, 1))
    allowed = {state.value for state in BiasState}
    assert structure_bias(bull, len(bull) + 100) == "bullish"
    assert structure_bias(bear, len(bear) + 100) == "bearish"
    assert structure_bias([], 0) == "neutral"
    assert {structure_bias(bull, 0), structure_bias(bear, 0)} <= allowed


def test_the_pivot_bos_read_no_longer_shares_the_structure_bias_name():
    assert not hasattr(structure_module, "structure_bias")
    out = pivot_bos_bias(_daily_bullish_frame())
    assert out["bias"] in {state.value for state in BiasState}


# ---------------------------------------------------------------------------
# htf_bias_evidence uses the HH/HL rule, not the pivot/BOS read
# ---------------------------------------------------------------------------

def test_htf_bias_evidence_uses_hh_hl_not_pivot_bos():
    # A monotonic 1D series has no confirmed zigzag pivots, so the HH/HL
    # primitive is neutral while the pivot read's 20-bar TREND_UP fallback is
    # bullish.  The evidence must report the corpus rule.
    rows = []
    t0 = pd.Timestamp("2026-01-01")
    for i in range(25 * 96):
        close = 100.0 + i / 96.0
        rows.append({
            "date": t0 + pd.Timedelta(minutes=15 * i),
            "open": close - 0.10, "high": close + 0.20,
            "low": close - 0.20, "close": close, "volume": 1.0,
        })
    frame = pd.DataFrame(rows)
    out = htf_bias_evidence(frame, len(frame) - 1)
    assert out is not None
    assert out["1D"] == {"bias": "neutral"}

    daily = (frame.set_index("date").resample("1D")
             .agg({"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"})
             .dropna().reset_index())
    assert pivot_bos_bias(daily)["bias"] == "bullish"
