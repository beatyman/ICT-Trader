"""P0-19 - declaration hygiene: every default that decides a trade is named.

The audit (``CODE_MAPPING.md`` P0-19) found nine defaults that were operative
but undeclared, plus one docstring that claimed a test which did not exist:

* the Ichimoku/Tenkan/MACD stack is the operative directional default while
  both corpus-shaped gates default to ``none``;
* ``daily_bias_filter`` names a *daily* object but read ``spec.bias_tf``;
* the 1H bias/confirm tier is a TF-02 fork and was not recorded;
* the sweep side is hardcoded (V15 has three attested readings);
* neutral is handled three different ways (BI-06/BI-07);
* the first partial is off by a bare ``0.0`` sentinel (MG-01/V31);
* a float encodes a sixth break-even trigger (MG-08/V31);
* the default entry is ``next_open``, which is not an attested menu price
  (RE:EX-03);
* ``context.py`` asserted that ``tests/test_context.py`` checks every
  ``SwingIndex`` bar, and it does not.

These tests assert the declarations exist, that their provenance is labelled,
and that the names match the behaviour.  They do not promote a ``repo`` choice
to corpus doctrine (§14.1-6).
"""
from __future__ import annotations

import argparse
import inspect
from types import SimpleNamespace

import pandas as pd
import pytest

import ict_trader.cli as cli_module
import ict_trader.setup as setup_api
from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (DEFAULT_NEUTRAL_HANDLING, NEUTRAL_HANDLINGS,
                                SwingIndex, neutral_blocks, neutral_weight,
                                validate_neutral_handling)
from ict_trader.realtime.analysis.evidence import (
    DEFAULT_SWEEP_DIRECTION_MODE,
    SWEEP_DIRECTION_MODES,
    SWEEP_DIRECTION_SOURCES,
    extract_sweep,
    htf_bias_evidence,
)
from ict_trader.realtime.analysis.feature_snapshot import (FeatureSnapshot,
                                                           TFView)
from ict_trader.realtime.analysis.mtf import aggregate_mtf, score_timeframe
from ict_trader.setup import (BIAS_GATE_DEFAULTS, BIAS_PRIMITIVE_SOURCES,
                              BIAS_PRIMITIVES, BIAS_TF_FORKS,
                              DAILY_BIAS_FILTERS, DAILY_BIAS_FILTER_LABEL,
                              DAILY_BIAS_FILTER_TF, DEFAULT_BIAS_PRIMITIVE,
                              STACKS, SetupConfig, _daily_bias_series,
                              _stack_spec, build_setup)
from ict_trader.strategy import (BE_MODES, DEFAULT_BE_MODE,
                                 DEFAULT_ENTRY_MODE, DEFAULT_LADDER_MODE,
                                 ENTRY_MODE_SOURCES, ENTRY_MODES,
                                 LADDER_MODES, StrategyConfig)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _setup_args():
    return setup_api._parser().parse_args(["--csv", "x.csv"])


def _cli_args():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    cli_module._add_setup_args(sub.add_parser("backtest"))
    return ap.parse_args(["backtest", "--csv", "x.csv"])


def _sweep(*, sweep_type: str = "SSL", direction: str = "bullish",
           time: int = 100, expires_at: int = 200) -> dict:
    return {
        "sweep_type": sweep_type,
        "direction": direction,
        "time": time,
        "level": 99.0,
        "sweep_price": 98.5,
        "reclaim_strength": 0.4,
        "expires_at": expires_at,
    }


def _snap(bias: str) -> FeatureSnapshot:
    views = {tf: TFView(tf, bias, "BOS", 100, 1, 1.0, "discount")
             for tf in ("1D", "4H", "1H", "15m")}
    return FeatureSnapshot(
        "S", "ETHUSDT", pd.Timestamp("2026-01-01", tz="UTC"), 100,
        views, None, (), (), (), (), ())


def _declining_frame(n: int = 240) -> pd.DataFrame:
    t0 = pd.Timestamp("2025-12-01")
    rows = []
    for i in range(n):
        close = 200.0 - 0.2 * i
        rows.append({
            "date": t0 + pd.Timedelta(minutes=15 * i),
            "open": close + 0.1, "high": close + 0.3,
            "low": close - 0.3, "close": close, "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _one_cisd_result(df: pd.DataFrame) -> SimpleNamespace:
    t = len(df) - 1
    level = float(df["close"].iloc[t])
    c = CISD(direction="bull", level=level, extreme=level, body_extreme=level,
             start=max(0, t - 5), end=t, sl=level - 1.0, target=None,
             grade=3, checks={"sweep": False, "pda": False})
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)), cisds=[c], fvgs=[],
        order_blocks=[], pd_arrays=[], sweeps=[], key_levels=[])


def _permissive_cfg(**overrides) -> SetupConfig:
    """A config whose only live directional gate is the declared primitive."""
    values = dict(
        market="other", stack="1d_15m", allow_short=True, min_grade=0,
        min_confluence=0, require_sweep=False, origin_filter="any",
        htf_fvg=False, target_mode="fixed", rr=2.0, min_target_rr=0.0,
        max_inducements=-1, regime_features=False, min_disp_body_ratio=0.0,
        min_disp_atr=0.0, min_disp_multiple=0.0, min_setup_score=0.0,
        require_pda_array=False, require_first_fvg=False,
        require_fresh_fvg=False, pd_gate=False, ote_gate=False,
        session_gate="none", quality_profile="standard",
    )
    values.update(overrides)
    return SetupConfig(**values)


# --------------------------------------------------------------------------- #
# 1. the operative default directional gate is declared and labelled repo
# --------------------------------------------------------------------------- #
def test_default_directional_gate_is_the_labelled_repo_indicator_stack():
    cfg = SetupConfig()
    assert DEFAULT_BIAS_PRIMITIVE == "indicator_stack"
    assert cfg.bias_primitive == DEFAULT_BIAS_PRIMITIVE
    assert cfg.bias_primitive == BIAS_GATE_DEFAULTS["bias_primitive"]
    # The stack is not corpus; it must say so wherever it is read.
    assert cfg.bias_primitive_label() == "repo"
    assert BIAS_PRIMITIVE_SOURCES["indicator_stack"]["label"] == "repo"
    assert "indicator_stack" in BIAS_PRIMITIVES


def test_corpus_shaped_gates_default_per_declaration():
    cfg = SetupConfig()
    assert cfg.htf_structure_gate == BIAS_GATE_DEFAULTS["htf_structure_gate"]
    assert cfg.daily_bias_filter == BIAS_GATE_DEFAULTS["daily_bias_filter"]
    assert BIAS_GATE_DEFAULTS["htf_structure_gate"] == "none"
    assert BIAS_GATE_DEFAULTS["daily_bias_filter"] == "none"
    assert BIAS_PRIMITIVE_SOURCES["structure_read"]["label"] == "corpus"


def test_a_declared_corpus_primitive_must_be_the_operative_gate():
    # Declaring the structure read without switching its gate on is exactly the
    # undeclared-default defect, so construction refuses.
    with pytest.raises(ValueError):
        SetupConfig(bias_primitive="structure_read")
    cfg = SetupConfig(bias_primitive="structure_read",
                      htf_structure_gate="aligned")
    assert cfg.bias_primitive_label() == "corpus"

    with pytest.raises(ValueError):
        SetupConfig(bias_primitive="daily_candle")
    cfg = SetupConfig(bias_primitive="daily_candle",
                      daily_bias_filter="c2")
    assert cfg.daily_bias_filter == "c2"


def test_declared_bias_primitive_selects_the_operative_gate():
    df = _declining_frame()
    result = _one_cisd_result(df)
    # Default: the indicator stack is the operative gate.  This short history
    # cannot form a 1D/4H Ichimoku row, so the long cannot pass that gate.
    assert build_setup(df, result, _permissive_cfg()) == []
    # Declared ``none``: no directional primitive is operative, so the same long
    # is admitted.  The declaration now matches the behaviour instead of merely
    # naming it.
    signals = build_setup(df, result, _permissive_cfg(bias_primitive="none"))
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["bias_primitive"] == "none"
    assert parts["bias_primitive_label"] == "repo"
    assert parts["neutral_handling"] == DEFAULT_NEUTRAL_HANDLING


@pytest.mark.parametrize("field,value", [
    ("bias_primitive", "ichimoku"),
    ("htf_structure_gate", "strict"),
    ("daily_bias_filter", "c4"),
    ("neutral_handling", "ignore"),
    ("entry_mode", "market"),
    ("stack", "2h_15m"),
])
def test_undeclared_enums_refuse_at_construction(field, value):
    with pytest.raises(ValueError):
        SetupConfig(**{field: value})


# --------------------------------------------------------------------------- #
# 2. daily_bias_filter implements the daily name (and is labelled repo)
# --------------------------------------------------------------------------- #
def test_daily_bias_filter_reads_the_1d_frame(monkeypatch):
    calls: list[str] = []
    daily_frame = pd.DataFrame([
        # prior bar, C2 (sweeps the prior low and closes back inside), C3
        # (engulfs C2 downward) -> the filter reads "bear".
        {"date": pd.Timestamp("2026-01-01"), "open": 95.0, "high": 100.0,
         "low": 90.0, "close": 95.0, "volume": 1.0},
        {"date": pd.Timestamp("2026-01-02"), "open": 91.0, "high": 95.0,
         "low": 85.0, "close": 92.0, "volume": 1.0},
        {"date": pd.Timestamp("2026-01-03"), "open": 90.0, "high": 93.0,
         "low": 87.0, "close": 88.0, "volume": 1.0},
    ])

    def fake_raw_htf(df, daily, tf, market):
        calls.append(tf)
        return daily_frame

    monkeypatch.setattr(setup_api, "_raw_htf", fake_raw_htf)
    # A 4H-bias stack: the old implementation would have asked for "4h".
    cfg = SetupConfig(stack="4h_15m", daily_bias_filter="c3")
    base = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-03 12:00", "2026-01-03 12:15"]),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
    })
    out = _daily_bias_series(base, None, cfg)
    assert calls == ["1D"]
    assert out[-1] == "bear"
    assert len(out) == len(base)


def test_daily_bias_filter_is_declared_repo_on_the_1d_frame():
    assert DAILY_BIAS_FILTER_TF == "1D"
    assert DAILY_BIAS_FILTER_LABEL == "repo"
    assert DAILY_BIAS_FILTERS == ("none", "c2", "c3")
    # "daily" must not mean "whatever the stack's bias tier happens to be".
    assert SetupConfig(stack="4h_15m").daily_bias_filter == "none"


# --------------------------------------------------------------------------- #
# 3. the 1H bias/confirm tier is a declared TF-02 fork
# --------------------------------------------------------------------------- #
def test_sub4h_confirm_tier_is_declared_as_a_repo_fork():
    assert BIAS_TF_FORKS == ("corpus_4h_or_higher", "repo_sub4h_confirm")
    default = _stack_spec(SetupConfig())
    assert default.bias_tf_fork == "corpus_4h_or_higher"

    for stack in ("1d_1h_15m", "4h_1h_15m"):
        spec = _stack_spec(SetupConfig(stack=stack))
        assert spec.confirm_tf == "1h"
        assert spec.bias_tf_fork == "repo_sub4h_confirm"
        # The 1H tier is a confirm tier; it is never the bias tier.
        assert spec.bias_tf in {"1D", "4h"}


def test_an_unknown_stack_name_refuses_instead_of_silently_defaulting():
    with pytest.raises(ValueError):
        SetupConfig(stack="30m_15m")
    with pytest.raises(ValueError):
        _stack_spec(SimpleNamespace(stack="30m_15m"))
    assert SetupConfig().stack in STACKS


def test_an_emitted_setup_records_the_declared_sub4h_fork():
    df = _declining_frame()
    result = _one_cisd_result(df)
    signals = build_setup(
        df, result,
        _permissive_cfg(bias_primitive="none", stack="1d_1h_15m"))
    assert len(signals) == 1
    assert signals[0].quality_parts["bias_tf_fork"] == "repo_sub4h_confirm"


# --------------------------------------------------------------------------- #
# 4. sweep side is a declared V15 reading, not a hardcoded constant
# --------------------------------------------------------------------------- #
def test_sweep_direction_default_is_declared_and_labelled():
    assert DEFAULT_SWEEP_DIRECTION_MODE == "sweep_sets_direction"
    assert set(SWEEP_DIRECTION_MODES) == {
        "sweep_sets_direction", "oppose_bias", "match_daily_bias"}
    for mode in SWEEP_DIRECTION_MODES:
        assert SWEEP_DIRECTION_SOURCES[mode]["label"] == "repo"
    ev = extract_sweep([_sweep()], direction="long",
                       source_kline_open_time=150)
    assert ev is not None
    assert ev["sweep_direction_mode"] == DEFAULT_SWEEP_DIRECTION_MODE


def test_each_sweep_reading_applies_its_own_bias_rule():
    sweeps = [_sweep()]
    args = dict(direction="long", source_kline_open_time=150)

    assert extract_sweep(
        sweeps, **args, sweep_direction_mode="oppose_bias",
        bias="bearish") is not None
    assert extract_sweep(
        sweeps, **args, sweep_direction_mode="oppose_bias",
        bias="bullish") is None
    assert extract_sweep(
        sweeps, **args, sweep_direction_mode="match_daily_bias",
        daily_bias="bullish") is not None
    assert extract_sweep(
        sweeps, **args, sweep_direction_mode="match_daily_bias",
        daily_bias="bearish") is None
    # A reading that needs bias refuses without it; neutral neither matches nor
    # opposes, so it never satisfies "must oppose".
    with pytest.raises(ValueError):
        extract_sweep(sweeps, **args, sweep_direction_mode="oppose_bias")
    with pytest.raises(ValueError):
        extract_sweep(sweeps, **args, sweep_direction_mode="match_daily_bias")
    assert extract_sweep(
        sweeps, **args, sweep_direction_mode="oppose_bias",
        bias="neutral") is None
    with pytest.raises(ValueError):
        extract_sweep(sweeps, **args, sweep_direction_mode="sideways")


# --------------------------------------------------------------------------- #
# 5. neutral is handled by one declared policy in every consumer
# --------------------------------------------------------------------------- #
def test_neutral_handling_is_one_declared_enum():
    assert DEFAULT_NEUTRAL_HANDLING == "no_trade"
    assert NEUTRAL_HANDLINGS == ("no_trade", "score_half", "narrative_only")
    assert neutral_blocks("no_trade")
    assert not neutral_blocks("score_half")
    assert neutral_weight("score_half") == 0.5
    assert neutral_weight("narrative_only") == 1.0
    with pytest.raises(ValueError):
        validate_neutral_handling("skip")


def test_mtf_reads_the_declared_neutral_handling():
    neutral = _snap("neutral")
    score, code, _ = aggregate_mtf(neutral, "long")
    assert code == "neutral_1d_no_trade"
    assert score == 0.0

    score, code, _ = aggregate_mtf(neutral, "long",
                                   neutral_handling="score_half")
    assert code == "ok" and score > 0.0
    view = score_timeframe(neutral, "1D", "long",
                           neutral_handling="score_half")
    assert view.reasons[0] == "neutral_score_half"
    assert view.score == pytest.approx(0.55 * 0.5 + 0.30 + 0.15)


def test_htf_bias_evidence_reads_the_declared_neutral_handling():
    frame = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=25, freq="15min"),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 1.0,
    })
    assert htf_bias_evidence(frame, len(frame) - 1) is not None
    with pytest.raises(ValueError):
        htf_bias_evidence(frame, len(frame) - 1, neutral_handling="ignore")


# --------------------------------------------------------------------------- #
# 6. the first partial is a declared coordinate system (MG-01/V31)
# --------------------------------------------------------------------------- #
def test_first_partial_default_is_a_declared_mode_not_a_float_sentinel():
    assert DEFAULT_LADDER_MODE == "off"
    assert LADDER_MODES == ("off", "r_anchored", "level_anchored")
    assert StrategyConfig().declared_ladder_mode() == DEFAULT_LADDER_MODE
    assert SetupConfig().ladder_mode_of() == DEFAULT_LADDER_MODE
    assert StrategyConfig(t1_at_r=1.0).declared_ladder_mode() == "r_anchored"
    assert StrategyConfig(t1_at_eq=True).declared_ladder_mode() == "level_anchored"


def test_ladder_declarations_that_contradict_behaviour_refuse():
    with pytest.raises(ValueError):
        StrategyConfig(t1_at_r=1.0, ladder_mode="off")
    with pytest.raises(ValueError):
        StrategyConfig(t1_at_r=1.0, t1_at_eq=True)
    with pytest.raises(ValueError):
        StrategyConfig(ladder_mode="pyramid")
    # The explicit declaration is honoured when it agrees with the fields.
    cfg = StrategyConfig(t1_at_r=1.0, ladder_mode="r_anchored")
    assert cfg.declared_ladder_mode() == "r_anchored"


# --------------------------------------------------------------------------- #
# 7. break-even is an enum, not a float with an implicit sixth trigger
# --------------------------------------------------------------------------- #
def test_break_even_trigger_is_a_declared_enum():
    assert DEFAULT_BE_MODE == "never"
    assert BE_MODES == ("never", "at_r", "after_first_partial")
    assert StrategyConfig().declared_be_mode() == DEFAULT_BE_MODE
    assert StrategyConfig(t1_at_r=1.0,
                          t1_move_be=True).declared_be_mode() == "after_first_partial"
    assert StrategyConfig(breakeven_at_r=1.0,
                          t1_move_be=False).declared_be_mode() == "at_r"
    assert SetupConfig().be_mode_of() == DEFAULT_BE_MODE


def test_break_even_declarations_that_contradict_behaviour_refuse():
    # Two mutually exclusive triggers in one config (MG-08) is a declaration
    # error, not a silent second BE rule.
    with pytest.raises(ValueError):
        StrategyConfig(breakeven_at_r=1.0, t1_at_r=1.0, t1_move_be=True)
    with pytest.raises(ValueError):
        StrategyConfig(breakeven_at_r=1.0, be_mode="never")
    with pytest.raises(ValueError):
        StrategyConfig(be_mode="at_target")
    cfg = StrategyConfig(breakeven_at_r=1.0, be_mode="at_r")
    assert cfg.declared_be_mode() == "at_r"


# --------------------------------------------------------------------------- #
# 8. the default entry is declared, labelled repo, and shared by both parsers
# --------------------------------------------------------------------------- #
def test_entry_mode_default_is_declared_and_labelled_repo():
    assert DEFAULT_ENTRY_MODE == "next_open"
    assert ENTRY_MODES == ("next_open", "retest_key", "fvg_ce", "ob_me", "ote")
    assert set(ENTRY_MODE_SOURCES) == set(ENTRY_MODES)
    assert ENTRY_MODE_SOURCES[DEFAULT_ENTRY_MODE]["label"] == "repo"
    assert StrategyConfig().entry_mode == DEFAULT_ENTRY_MODE
    assert SetupConfig().entry_mode == DEFAULT_ENTRY_MODE
    # Every declared member is constructible; an undeclared one refuses.
    for mode in ENTRY_MODES:
        assert SetupConfig(entry_mode=mode).entry_mode == mode
    with pytest.raises(ValueError):
        StrategyConfig(entry_mode="market")


def test_declared_defaults_agree_across_config_and_both_parsers():
    setup_args, cli_args, cfg = _setup_args(), _cli_args(), SetupConfig()
    for name, declared_value in (
            ("bias_primitive", DEFAULT_BIAS_PRIMITIVE),
            ("neutral_handling", DEFAULT_NEUTRAL_HANDLING),
            ("entry_mode", DEFAULT_ENTRY_MODE),
            ("daily_bias_filter", "none"),
            ("htf_structure_gate", "none")):
        assert getattr(cfg, name) == declared_value
        assert getattr(setup_args, name) == declared_value
        assert getattr(cli_args, name) == declared_value
    # Remaining declarations are optional and resolve from behaviour.
    assert setup_args.be_mode is None and cli_args.be_mode is None
    assert setup_args.ladder_mode is None and cli_args.ladder_mode is None


# --------------------------------------------------------------------------- #
# 9. the false SwingIndex test claim is gone
# --------------------------------------------------------------------------- #
def test_context_no_longer_claims_a_swingindex_equivalence_test():
    source = inspect.getsource(SwingIndex)
    assert "tests/test_context.py" not in source
    assert "checks every" not in source
    # tests/test_context.py really does not test SwingIndex (the old claim).
    import pathlib
    context_test = pathlib.Path(__file__).with_name("test_context.py").read_text(
        encoding="utf-8")
    assert "SwingIndex" not in context_test

