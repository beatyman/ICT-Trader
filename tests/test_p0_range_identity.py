"""P0-16 - range and ATR identity.

Pins the four defects named by ``CODE_MAPPING.md`` P0-16:

* a fixed 40-bar high/low fallback is **not** a structural dealing range
  (DR-00/SN-R07): it is never emitted as ``DEALING_RANGE``, and the absence
  carries a diagnostic instead of a bare ``None``;
* the structural dealing range, the IPDA window, the range equilibrium, the
  gap CE and the OB body mean are separate objects/fields (X8, §7.1), and
  ``anchor_price`` is structural only;
* one ATR function and one repo period (14) serve every ATR-relative number
  (§2.3, A7);
* a 120-bar stale range is diagnosed (``NT_RANGE_INVALID``), never silent.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ict_trader.concepts as concepts
import ict_trader.context as context
import ict_trader.sweeps as sweeps
from ict_trader.concepts import (
    GapConsequentEncroachment,
    ICTConfig,
    IPDAWindow,
    OrderBlockBodyMean,
    RangeEquilibrium,
    analyze,
    detect_dealing_range,
    detect_dealing_range_state,
    detect_fvgs,
    detect_ipda_window,
    detect_liquidity,
    find_pivots,
)
from ict_trader.concepts import DealingRange as ConceptDealingRange
from ict_trader.realtime.analysis.arrays import build_ict_arrays
from ict_trader.realtime.analysis.engines.ict import ICTEngine
from ict_trader.setup import SetupConfig

SRC = Path(__file__).resolve().parents[1] / "src" / "ict_trader"


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------
def _walk(n: int = 120, seed: int = 17) -> pd.DataFrame:
    """Deterministic random walk with FVGs, OBs and confirmed swings."""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.6, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


def _tent(n: int = 45) -> pd.DataFrame:
    """One confirmed swing **high**, no confirmed swing low.

    This is exactly the frame that used to take the fixed 40-bar high/low
    fallback branch: a structural pair is impossible, so the old code invented
    a ``DEALING_RANGE`` from the last 40 bars.
    """
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    half = n // 2
    price = np.r_[np.linspace(100.0, 120.0, half),
                  np.linspace(120.0, 100.0, n - half)]
    return pd.DataFrame({
        "date": dates, "open": price, "high": price + 0.4,
        "low": price - 0.4, "close": price, "volume": 1000.0,
    })


def _stale_frame(tail: int = 200) -> pd.DataFrame:
    """Structural anchors early, then a convergent tail with no new pivots.

    The last confirmed high/low are ~213 bars before the final bar, i.e. past
    the repo 120-bar staleness cap, so the range is absent *for a reason*.
    """
    price = np.concatenate([
        np.linspace(100.0, 120.0, 21),
        np.linspace(120.0, 95.0, 25),
        np.linspace(95.0, 100.0, 14),
    ])
    high = price + 0.4
    low = price - 0.4
    high[20] = 121.0
    low[45] = 94.0
    # Converging tail: decreasing highs, increasing lows -> no pivot at all.
    high = np.r_[high, np.linspace(high[-1] - 0.001, high[-1] - 0.2, tail)]
    low = np.r_[low, np.linspace(low[-1] + 0.001, low[-1] + 0.2, tail)]
    close = np.r_[price, (high[-tail:] + low[-tail:]) / 2.0]
    open_ = np.r_[price, (high[-tail:] + low[-tail:]) / 2.0]
    dates = pd.date_range("2026-01-01", periods=len(close), freq="15min", tz="UTC")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


@dataclass
class _P:
    kind: str
    price: float
    index: int
    confirmed_at: int


def _independent_atr(df: pd.DataFrame, period: int) -> np.ndarray:
    """TR mean over ``period`` bars, computed without pandas (parity oracle)."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    tr = np.empty(len(df), dtype=float)
    for i in range(len(df)):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]),
                        abs(low[i] - close[i - 1]))
    return np.array([tr[max(0, i - period + 1): i + 1].mean()
                     for i in range(len(df))])


# ---------------------------------------------------------------------------
# 1. No fixed-count fallback as DEALING_RANGE
# ---------------------------------------------------------------------------
def test_fixed_count_fallback_never_becomes_a_dealing_range():
    df = _tent()
    result = analyze(df, ICTConfig())

    assert result.dealing_range is None, (
        "a frame with no structural swing pair must not fabricate a dealing "
        "range from a fixed candle count (DR-00/SN-R07)"
    )
    diag = result.dealing_range_diagnostic
    assert diag is not None, "an absent range must carry a reason, not a bare None"
    assert diag.code == context.NT_DATA_MISSING
    assert "fallback" in diag.reason.lower(), (
        "the diagnostic must name the refused fixed-count fallback"
    )

    # The fallback constructor (and its parameter) is gone, not merely unused.
    assert "fallback_bars" not in inspect.signature(detect_dealing_range).parameters
    value, direct_diag = detect_dealing_range_state(df)
    assert value is None
    assert direct_diag is not None and direct_diag.code == context.NT_DATA_MISSING

    arrays = build_ict_arrays(df, "TESTUSDT", "15m")
    assert not [a for a in arrays if a.array_type == "DEALING_RANGE"], (
        "the array lifecycle may not emit the fixed-count object as "
        "DEALING_RANGE (P0-16, DR-00)"
    )


# ---------------------------------------------------------------------------
# 2. Separate types/fields: range / IPDA / CE / OB mean
# ---------------------------------------------------------------------------
def test_range_and_ipda_window_are_separate_types():
    dr = ConceptDealingRange(top=120.0, bottom=100.0, known_at=10,
                             left=2, right=11)
    ipda = IPDAWindow(window=40, high=200.0, low=100.0, known_at=59, right=59)

    assert isinstance(dr, ConceptDealingRange)
    assert isinstance(ipda, IPDAWindow)
    assert not isinstance(ipda, ConceptDealingRange)
    assert type(dr) is not type(ipda)
    # Both expose a 50%, but each is the 50% of its own object class.
    assert dr.ce == 110.0       # structural equilibrium (DR-03)
    assert ipda.ce == 150.0     # IPDA EQ_N (IP-01)
    assert dr.fallback is False

    # IPDA is the only object a fixed count may anchor.
    daily = pd.DataFrame({
        "high": np.arange(100.0, 120.0),
        "low": np.arange(90.0, 110.0),
    })
    window = detect_ipda_window(daily, 20, last_closed=19)
    assert isinstance(window, IPDAWindow)
    assert window.window == 20 and window.timeframe == "1d"
    with pytest.raises(ValueError):
        detect_ipda_window(daily, 30)   # outside the declared {20,40,60} set

    # The structural constructor returns the structural type, never an IPDA one.
    value, diag = detect_dealing_range_state(_walk())
    assert value is not None and diag is None
    assert isinstance(value, ConceptDealingRange)
    assert not isinstance(value, IPDAWindow)


def test_three_midpoints_are_three_types_not_one_field():
    eq = RangeEquilibrium(110.0)
    gap = GapConsequentEncroachment(101.0)
    mean = OrderBlockBodyMean(99.0)
    assert {eq.kind, gap.kind, mean.kind} == {
        "range_equilibrium", "gap_ce", "ob_body_mean",
    }
    assert len({type(eq), type(gap), type(mean)}) == 3
    assert not (isinstance(eq, GapConsequentEncroachment)
                or isinstance(gap, RangeEquilibrium)
                or isinstance(mean, RangeEquilibrium))


def test_anchor_price_is_structural_and_each_50_is_typed():
    arrays = build_ict_arrays(_walk(), "TESTUSDT", "15m")
    by_kind: dict[str, list] = {}
    for array in arrays:
        by_kind.setdefault(array.array_type, []).append(array)

    assert "DEALING_RANGE" in by_kind and "FVG" in by_kind and "OB" in by_kind

    dr = by_kind["DEALING_RANGE"][0]
    assert dr.anchor_price is None, "anchor_price may not carry the range CE (X8)"
    assert dr.range_equilibrium is not None
    assert dr.range_equilibrium.kind == "range_equilibrium"
    assert dr.range_equilibrium.value == pytest.approx(
        (dr.price_upper + dr.price_lower) / 2.0)
    assert dr.gap_ce is None and dr.ob_body_mean is None

    for array in by_kind["FVG"]:
        assert array.anchor_price is None
        assert array.gap_ce is not None and array.gap_ce.kind == "gap_ce"
        assert array.gap_ce.value == pytest.approx(
            (array.price_upper + array.price_lower) / 2.0)
        assert array.range_equilibrium is None and array.ob_body_mean is None

    for array in by_kind["OB"]:
        assert array.ob_body_mean is not None
        assert array.ob_body_mean.kind == "ob_body_mean"
        assert array.range_equilibrium is None and array.gap_ce is None

    for array in by_kind.get("CISD", []):
        # A CISD is a line: it has a level anchor and no derived 50%.
        assert array.anchor_price is not None
        assert (array.range_equilibrium is None and array.gap_ce is None
                and array.ob_body_mean is None)

    # At most one midpoint type per array, and none is a bare untyped "ce".
    for array in arrays:
        present = [x for x in (array.range_equilibrium, array.gap_ce,
                               array.ob_body_mean) if x is not None]
        assert len(present) <= 1
        assert not hasattr(array, "ce")

    public = dr.to_public()
    assert public["anchor_price"] is None
    assert public["range_equilibrium"]["kind"] == "range_equilibrium"
    assert public["gap_ce"] is None and public["ob_body_mean"] is None


# ---------------------------------------------------------------------------
# 3. One ATR function and one period
# ---------------------------------------------------------------------------
def test_one_atr_function_one_period():
    assert context.ATR_PERIOD == concepts.ATR_PERIOD == 14
    assert concepts.atr is context.atr, "concepts.py must re-use context.atr"
    assert sweeps.atr is context.atr, "sweeps.py must re-use context.atr"

    # Only context.py defines an ATR; the cited duplicates are gone.
    assert "def atr(" in (SRC / "context.py").read_text(encoding="utf-8")
    for rel in ("concepts.py", "sweeps.py",
                "realtime/analysis/arrays.py",
                "realtime/analysis/sweeps.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "def atr(" not in text, f"{rel} defines a second ATR"
        assert "def _atr(" not in text, f"{rel} defines a private ATR copy"

    # A caller cannot select a second period (the old 10-bar ATR is gone).
    with pytest.raises(ValueError):
        context.atr(_walk(30), 10)

    df = _walk(80)
    got = context.atr(df).to_numpy(float)
    expected = _independent_atr(df, context.ATR_PERIOD)
    np.testing.assert_allclose(got, expected, rtol=0, atol=1e-12)


def test_fvg_and_liquidity_read_the_one_atr(monkeypatch):
    df = _walk(80)
    calls: list[int] = []
    real = context.atr

    def spy(frame, period=context.ATR_PERIOD):
        calls.append(int(period))
        return real(frame, period)

    monkeypatch.setattr(concepts, "atr", spy)
    detect_fvgs(df, ICTConfig())
    detect_liquidity(df, find_pivots(df, 5, 1), ICTConfig())
    assert calls, "detect_fvgs/detect_liquidity must call the ATR function"
    assert set(calls) == {context.ATR_PERIOD}, (
        "every ATR read uses the single declared period (repo 14)"
    )


# ---------------------------------------------------------------------------
# 4. Staleness carries a reason
# ---------------------------------------------------------------------------
def test_stale_120_bar_range_is_not_a_silent_none():
    swings = [_P("L", 100.0, 2, 3), _P("H", 200.0, 5, 6)]

    value, diag = context.dealing_range_state(swings, 200)
    assert value is None
    assert diag is not None
    assert diag.code == context.NT_RANGE_INVALID
    assert diag.age_bars == 197
    assert diag.lookback_bars == context.RANGE_STALENESS_BARS == 120
    assert "stale" in diag.reason.lower()
    assert diag.to_public()["code"] == context.NT_RANGE_INVALID

    # The value-only twin still returns None -- but the reason is available.
    assert context.dealing_range(swings, 200) is None
    index = context.SwingIndex(swings)
    _, index_diag = index.dealing_range_state(200)
    assert index_diag == diag, "the O(1) read must give the same reason"


def test_staleness_survives_an_empty_scan():
    df = _stale_frame()
    engine = ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True))
    engine.scan_history(df)

    # No signal is emitted on this frame, so an evidence-only diagnostic would
    # vanish; the engine records the reason for the scan itself.
    assert engine.range_diagnostic is not None
    assert engine.range_diagnostic["code"] == context.NT_RANGE_INVALID
    assert engine.range_diagnostic["age_bars"] > context.RANGE_STALENESS_BARS
    assert engine.range_diagnostic["reason"]

    result = analyze(df, ICTConfig())
    assert result.dealing_range is None
    assert result.dealing_range_diagnostic is not None
    assert result.dealing_range_diagnostic.code == context.NT_RANGE_INVALID


def test_healthy_range_has_no_diagnostic():
    df = _walk()
    result = analyze(df, ICTConfig())
    assert result.dealing_range is not None
    assert result.dealing_range_diagnostic is None

    value, diag = detect_dealing_range_state(df)
    assert value is not None and diag is None
