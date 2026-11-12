"""Tests derived from the distilled ICT corpus (one doc id per assertion group).

Every test names the corpus document and line that justifies it, so a behaviour
change can be traced back to a source quote.  Corpus files live in
``output/ict/corpus_distilled/docs/``.

Covered:
  doc 601  dealing range CE / quartiles / OTE band      (liquiditysweeps.com)
  doc 631  equilibrium as a partial-exit price          (liquiditysweeps.com)
  doc 556  four-condition order-block validation        (innercircletrader.net)
  doc 502  IFVG three contexts + CE entry               (innercircletrader.net)
  doc 570  hidden order block boundaries                (innercircletrader.net)
  doc 69   stop beyond the sweep extreme                (ictradingconcepts.com)
  doc 491  silver bullet / macro windows                (innercircletrader.net)
  doc 546  09:50 NY macro window                        (innercircletrader.net)
"""
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.cli import main
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.context import DealingRange as CtxDealingRange
from ict_trader.context import in_session
from ict_trader.realtime.analysis.dealing_range import DealingRange, ob_grab, valid_ob
from ict_trader.realtime.analysis.evidence import extract_sweep
from ict_trader.realtime.analysis.hidden_ob import detect_hidden_ob, nearest_hidden_ob
from ict_trader.realtime.analysis.pda_confluence import (
    ifvg_ce, ifvg_stop, valid_ifvg_context,
)
from ict_trader.realtime.analysis.risk import (
    StopAnchorUnresolved,
    structural_stop,
)
from ict_trader.strategy import StrategyConfig, simulate_cisd


# --------------------------------------------------------------------------- #
# doc 601 - the CE is (high + low) / 2 and the quartiles are 25 / 50 / 75
# --------------------------------------------------------------------------- #
def test_doc601_ce_matches_the_worked_example():
    """Doc 601 L70: "CE = (1.0900 + 1.0800) / 2 = 1.0850"."""
    r = DealingRange(high=1.0900, low=1.0800)
    assert r.ce == pytest.approx(1.0850)
    assert r.q1 == pytest.approx(1.0825)
    assert r.q3 == pytest.approx(1.0875)
    assert r.zone(1.0860) == "premium"
    assert r.zone(1.0840) == "discount"


def test_doc601_ote_band_lands_in_the_21_38_percent_slice():
    """Doc 601 L134/L136: 0.62-0.79 retracement ~ 21-38% of the range."""
    r = DealingRange(high=1.0900, low=1.0800)
    lo, hi = r.ote("long")
    span = r.high - r.low
    assert (lo - r.low) / span == pytest.approx(0.21, abs=1e-9)
    assert (hi - r.low) / span == pytest.approx(0.38, abs=1e-9)
    # mirror for shorts
    slo, shi = r.ote("short")
    assert (slo - r.low) / span == pytest.approx(0.62, abs=1e-9)
    assert (shi - r.low) / span == pytest.approx(0.79, abs=1e-9)


def test_doc601_context_dealing_range_uses_structural_swings():
    """Doc 601 L56: boundaries come from swings, "not ... fixed candle counts".

    The lookback argument is a staleness cap on the swing, not a bar window: a
    fresh pair is accepted however far back the window started.
    """
    class Swing:
        def __init__(self, index, price, kind, confirmed_at):
            self.index, self.price = index, price
            self.kind, self.confirmed_at = kind, confirmed_at

    swings = [Swing(10, 90.0, "L", 12), Swing(40, 110.0, "H", 42)]
    dr = CtxDealingRange  # alias keeps the import meaningful for type readers
    from ict_trader.context import dealing_range as dr_fn
    out = dr_fn(swings, idx=45, lookback_bars=120)
    assert out is not None
    assert out.high == pytest.approx(110.0)
    assert out.low == pytest.approx(90.0)
    assert out.position(100.0) == pytest.approx(0.5)
    assert dr is CtxDealingRange


# --------------------------------------------------------------------------- #
# doc 556 - four conditions, including the liquidity grab
# --------------------------------------------------------------------------- #
def test_doc556_grab_is_the_first_condition():
    """Doc 556 L78: the impulse candle must grab the OB candle's low/high."""
    assert ob_grab("long", c1_high=101, c1_low=99, c2_high=103, c2_low=98.5) is True
    assert ob_grab("long", c1_high=101, c1_low=99, c2_high=103, c2_low=99.5) is False
    assert ob_grab("short", c1_high=101, c1_low=99, c2_high=101.5, c2_low=97) is True
    assert ob_grab("short", c1_high=101, c1_low=99, c2_high=100.5, c2_low=97) is False


def test_doc556_valid_ob_requires_all_four_conditions():
    assert valid_ob("long", grab=True, engulf=True, fvg=True, mss=True)[0] is True
    ok, reasons = valid_ob("long", grab=False, engulf=True, fvg=True, mss=True)
    assert ok is False and "no_grab" in reasons
    ok, reasons = valid_ob("short", grab=True, engulf=True, fvg=False, mss=True)
    assert ok is False and "no_fvg" in reasons


# --------------------------------------------------------------------------- #
# doc 69 - the stop anchors on the sweep extreme, never on the swept level
# --------------------------------------------------------------------------- #
def test_doc69_stop_goes_beyond_the_sweep_extreme_not_the_level():
    """Doc 69 L181: "The stop belongs beyond the sweep's extreme, not at the swept level"."""
    stop = structural_stop("long", entry=100.0, atr=1.0,
                           sweep={"level": 98.0, "extreme": 97.2}, structure=None)
    assert stop < 97.2          # beyond the wick that took the stops
    assert stop < 98.0          # and therefore beyond the level as well
    assert stop == pytest.approx(97.2 - 0.25, abs=1e-9)


def test_doc69_short_stop_mirrors_and_legacy_payload_refuses():
    """P0-06: a level-only payload has no wick to anchor on and must refuse."""
    stop = structural_stop("short", entry=100.0, atr=1.0,
                           sweep={"level": 102.0, "extreme": 102.8}, structure=None)
    assert stop > 102.8
    assert stop == pytest.approx(102.8 + 0.25, abs=1e-9)
    with pytest.raises(StopAnchorUnresolved):
        structural_stop("long", entry=100.0, atr=1.0,
                        sweep={"level": 98.0}, structure=None)


def test_doc69_declared_policy_selects_one_anchor_not_the_outermost_union():
    """P0-06: the declared policy uses the sweep extreme first, not a union.

    Doc 556 L116 also allows the OB extreme, but the policy's declared precedence
    means it only becomes the anchor when no sweep/structure anchor resolves.
    """
    stop = structural_stop("long", entry=100.0, atr=1.0,
                           sweep={"level": 98.0, "extreme": 97.8},
                           structure=None, ob={"bottom": 96.5, "top": 99.0})
    assert stop == pytest.approx(97.8 - 0.25, abs=1e-9)
    assert stop > 96.5          # not the outermost-of-all-anchors union
    ob_only = structural_stop("long", entry=100.0, atr=1.0,
                              sweep=None, structure=None,
                              ob={"bottom": 96.5, "top": 99.0})
    assert ob_only == pytest.approx(96.5 - 0.25, abs=1e-9)


def test_doc69_extract_sweep_exposes_the_extreme():
    sweeps = [{
        "sweep_type": "SSL", "direction": "bullish", "time": 1000,
        "level": 98.0, "sweep_price": 97.2, "reclaim_strength": 0.6,
        "expires_at": 5000,
    }]
    ev = extract_sweep(sweeps, direction="long", source_kline_open_time=2000)
    assert ev is not None
    assert ev["level"] == pytest.approx(98.0)
    assert ev["extreme"] == pytest.approx(97.2)


# --------------------------------------------------------------------------- #
# doc 502 - IFVG: three contexts, CE entry, stop beyond the extreme
# --------------------------------------------------------------------------- #
def test_doc502_ifvg_only_trades_in_the_three_contexts():
    assert valid_ifvg_context("bullish", "premium")[0] is True
    assert valid_ifvg_context("bearish", "discount")[0] is True
    ok, reasons = valid_ifvg_context("bullish", "discount")
    assert ok is False and reasons == ("bullish_ifvg_outside_premium",)
    ok, reasons = valid_ifvg_context("bearish", "premium")
    assert ok is False and reasons == ("bearish_ifvg_outside_discount",)
    # a failed MSS rescues either direction (doc 502 context III)
    assert valid_ifvg_context("bullish", "discount", failed_mss=True)[0] is True


def test_doc502_ifvg_entry_is_the_ce_and_the_stop_is_beyond_the_extreme():
    zone = {"top": 110.0, "bottom": 106.0}
    assert ifvg_ce(zone) == pytest.approx(108.0)
    # with no buffer the stop sits exactly on the extreme; doc 502 L121/L178 asks
    # for "a small buffer", which the caller must supply - never a guessed number.
    assert ifvg_stop("bullish", zone) == pytest.approx(106.0)
    assert ifvg_stop("bullish", zone, buffer=0.5) == pytest.approx(105.5)
    assert ifvg_stop("bearish", zone, buffer=0.5) == pytest.approx(110.5)


# --------------------------------------------------------------------------- #
# doc 570 - hidden order block boundaries
# --------------------------------------------------------------------------- #
def _two_candle_frame(*candles):
    rows = list(candles)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows), freq="1D"),
        "open": [c[0] for c in rows],
        "high": [c[1] for c in rows],
        "low": [c[2] for c in rows],
        "close": [c[3] for c in rows],
        "volume": [1000] * len(rows),
    })


def test_doc570_bullish_hidden_ob_zone_is_low_c2_to_high_c1():
    """Doc 570 L98: "from the low of the second candle's wick to the high of the first candle's wick"."""
    df = _two_candle_frame(
        (100.0, 103.0, 99.8, 102.0),    # bullish, upper wick to 103
        (102.5, 103.5, 101.5, 103.0),   # bullish, lower wick down to 101.5
        (105.0, 106.5, 104.8, 106.0),   # unrelated, must not overlap
    )
    zones = detect_hidden_ob(df)
    assert len(zones) == 1
    z = zones[0]
    assert z.direction == "bullish"
    assert z.top == pytest.approx(103.0)
    assert z.bottom == pytest.approx(101.5)
    assert z.ce == pytest.approx(102.25)
    assert z.proximal == pytest.approx(103.0)   # approached from above
    assert z.distal == pytest.approx(101.5)


def test_doc570_bearish_hidden_ob_zone_is_low_c1_to_high_c2():
    """Doc 570 L110: "from the high of the second candle's wick to the low of the first candle's wick"."""
    df = _two_candle_frame(
        (104.0, 104.5, 101.0, 102.0),   # bearish, lower wick to 101
        (102.5, 103.5, 101.8, 102.0),   # bearish, upper wick up to 103.5
        (99.0, 99.5, 97.0, 98.0),       # unrelated
    )
    zones = [z for z in detect_hidden_ob(df) if z.direction == "bearish"]
    assert zones
    z = zones[0]
    assert z.top == pytest.approx(103.5)
    assert z.bottom == pytest.approx(101.0)
    assert z.proximal == pytest.approx(101.0)   # approached from below
    assert z.distal == pytest.approx(103.5)


def test_doc570_nearest_hidden_ob_prefers_the_containing_zone():
    df = _two_candle_frame(
        (100.0, 103.0, 99.8, 102.0),
        (102.5, 103.5, 101.5, 103.0),
        (105.0, 106.5, 104.8, 106.0),
    )
    zones = detect_hidden_ob(df)
    hit = nearest_hidden_ob(zones, price=102.0, direction="bullish")
    assert hit is not None and hit.bottom <= 102.0 <= hit.top


# --------------------------------------------------------------------------- #
# doc 491 / 546 / 69 - session and macro windows
# --------------------------------------------------------------------------- #
def _bars(*ny_times):
    stamps = [pd.Timestamp(t, tz="America/New_York") for t in ny_times]
    return pd.DataFrame({
        "date": stamps,
        "open": [100.0] * len(stamps), "high": [101.0] * len(stamps),
        "low": [99.0] * len(stamps), "close": [100.0] * len(stamps),
        "volume": [1] * len(stamps),
    })


def test_doc491_silver_bullet_window_is_1000_to_1100_new_york():
    df = _bars("2026-07-15 10:30", "2026-07-15 11:15", "2026-07-15 09:55")
    assert in_session(df, 0, "crypto", "silver_bullet") is True
    assert in_session(df, 1, "crypto", "silver_bullet") is False
    assert in_session(df, 2, "crypto", "silver_bullet") is False


def test_doc546_macro_window_starts_0950_and_lasts_twenty_minutes():
    """Doc 546 L154: "the 09:50 NY-AM macro window ... that 20-minute window"."""
    df = _bars("2026-07-15 09:50", "2026-07-15 10:05", "2026-07-15 10:15", "2026-07-15 09:45")
    assert in_session(df, 0, "crypto", "macro_0950") is True
    assert in_session(df, 1, "crypto", "macro_0950") is True
    assert in_session(df, 2, "crypto", "macro_0950") is False
    assert in_session(df, 3, "crypto", "macro_0950") is False


def test_doc69_venom_window_covers_the_0800_0930_new_york_range():
    """Doc 69 L128: "a raid of the 08:00-09:30 New York range after the 09:30 open"."""
    df = _bars("2026-07-15 08:00", "2026-07-15 09:30", "2026-07-15 09:45", "2026-07-15 07:45")
    assert in_session(df, 0, "crypto", "venom") is True
    assert in_session(df, 1, "crypto", "venom") is False  # half-open [08:00, 09:30)
    assert in_session(df, 2, "crypto", "venom") is False
    assert in_session(df, 3, "crypto", "venom") is False


def test_doc601_asian_range_option_is_0000_to_0200_est():
    """Doc 601 L98: "The Asian range (00:00-02:00 EST)"."""
    df = _bars("2026-07-15 00:00", "2026-07-15 01:30", "2026-07-15 02:30", "2026-07-15 20:00")
    assert in_session(df, 0, "crypto", "asian_dr") is True
    assert in_session(df, 1, "crypto", "asian_dr") is True
    assert in_session(df, 2, "crypto", "asian_dr") is False
    # the legacy window is untouched and still covers the 20:00 NY session
    assert in_session(df, 3, "crypto", "asian") is True


# --------------------------------------------------------------------------- #
# doc 631 - partial exit at the dealing-range EQ
# --------------------------------------------------------------------------- #
def _exit_frame(prices, wick=1.0):
    n = len(prices)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": prices,
        "high": [p + wick for p in prices],
        "low": [p - wick for p in prices],
        "close": prices,
        "volume": [1000] * n,
    })


def _run_exit(df, cfg, *, dr_eq=None):
    parts = {"dr_eq": float(dr_eq)} if dr_eq is not None else {}
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=120, grade=4, checks={"sweep": True},
             quality_parts=parts)
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    base = dict(min_grade=0, min_confluence=0, entry_delay=1, max_bars=6,
                min_rr=0.0, commission_bps=0.0, slippage_bps=0.0)
    return simulate_cisd(df, res, StrategyConfig(**{**base, **cfg}), "test")


def test_doc631_partial_exit_fires_at_the_equilibrium_price():
    """Doc 631 L76: "For longs from discount, take 30-50% at equilibrium"."""
    df = _exit_frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    t = _run_exit(df, {"t1_at_eq": True, "t1_fraction": 0.5}, dr_eq=109.0).trades[0]
    assert t.t1_filled is True
    assert t.t1_price == pytest.approx(109.0, abs=1e-9)


def test_doc631_no_equilibrium_means_no_partial_exit():
    df = _exit_frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    t = _run_exit(df, {"t1_at_eq": True, "t1_fraction": 0.5}, dr_eq=None).trades[0]
    assert t.t1_filled is False and t.t1_price is None
    # and the flag is inert when switched off
    t2 = _run_exit(df, {"t1_at_eq": False}, dr_eq=109.0).trades[0]
    assert t2.t1_filled is False


# --------------------------------------------------------------------------- #
# CLI regression: the backtest help used to raise TypeError (% formatting)
# --------------------------------------------------------------------------- #
def test_backtest_help_renders_without_percent_formatting_error():
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "--help"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- #
# Reproducibility: the CLI backtest is not the library default config
# --------------------------------------------------------------------------- #
def test_cli_vs_library_defaults_are_documented():
    """`cli backtest` and `SetupConfig()` disagree on two HTF scores.

    Library defaults are min_1d_score=2 / min_15m_score=1; the CLI parser uses
    3 / 0.  That changes the trade count materially (ETH 15m: 65 vs 21 trades),
    so it is pinned here instead of being discovered again later.  Changing
    either side is a deliberate product decision, not a refactor.
    """
    import argparse

    import ict_trader.cli as cli_module
    from ict_trader.setup import SetupConfig

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    cli_module._add_setup_args(sub.add_parser("backtest"))
    args = ap.parse_args(["backtest", "--csv", "x.csv"])
    cfg = cli_module._setup_config(args)
    lib = SetupConfig()
    assert (lib.min_1d_score, lib.min_15m_score) == (2, 1)
    assert (cfg.min_1d_score, cfg.min_15m_score) == (3, 0)
