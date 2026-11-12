"""ICT Concepts (LuxAlgo-inspired) in Python.

This is a standalone re-implementation of the core ICT ideas from the
"ICT Concepts [LuxAlgo]" Pine script:

  * swing highs / lows and a zigzag
  * MSS / BOS
  * Fair Value Gaps (FVG), optional BPR
  * order blocks (OB)
  * equal-high / equal-low liquidity
  * New Week Opening Gap (NWOG) / New Day Opening Gap (NDOG)
  * optional killzones for intraday data
  * Fibonacci levels on the last swing leg
  * a matplotlib chart and a signals CSV

It is not a line-by-line port of every Pine object/lifecycle detail; it is a
practical Python engine that can consume a CSV or a Futu OpenD dataframe.

Example:
    python ict_concepts.py --csv output/csi300/csv/SH600519.csv --bars 260

Original Pine indicator: CC BY-NC-SA 4.0, © LuxAlgo.
This Python re-implementation keeps the same non-commercial share-alike spirit.
"""
from __future__ import annotations

import argparse
import math
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle

from .cisd import CISD, CISDConfig, detect_cisd
from .context import (
    ATR_PERIOD,
    NT_DATA_MISSING,
    NT_RANGE_INVALID,
    RANGE_STALENESS_BARS,
    RangeDiagnostic,
    atr,
    in_session_at,
)
from .enhancements import SMT, SessionLevel, detect_htf_cisds, detect_session_levels, detect_smt
from .pd_arrays import (DEFAULT_BREAKER_TIER, PDAArray, detect_breakers,
                        detect_mitigations, detect_rejections)
from .sweeps import KeyLevel, LiquiditySweep, detect_key_levels, detect_sweeps, nearest_key_target, score_key_levels

Direction = Literal["bull", "bear"]


# ---------------------------------------------------------------------------
# Declared constants (P0-20): one name, one value, one provenance.
# No call site may re-type any of these.
# ---------------------------------------------------------------------------
#: ST-03: the pivot width is a declared constant.  The corpus modal is a
#: 3-candle fractal (1 left / 1 right) and it disfavours the 5-candle fractal;
#: the repo default is 5/1.  ``PIVOT_RIGHT`` is also the confirmation lag used
#: by ``Swing.confirmed_at`` (ST-01).  Both values are labelled ``repo``: they
#: are not promoted to corpus doctrine (§14.1-6).
PIVOT_LEFT = 5
PIVOT_RIGHT = 1
PIVOT_WIDTH = (PIVOT_LEFT, PIVOT_RIGHT)
PIVOT_WIDTH_SOURCE = "repo"
PIVOT_WIDTH_NOTE = ("ST-03: corpus modal is 1/1 and disfavours 5/1; 5/1 is the "
                    "labelled repo default")

#: ST-03/LQ-03: pivot ties are **non-strict** (``>=`` high, ``<=`` low), so two
#: equal highs are two pivots (EQH needs that).  ``build_zigzag`` then keeps the
#: *later* equal extreme, i.e. the most recent confirmed swing, which is what
#: the dealing-range and structure reads require.
PIVOT_TIE_RULE = "non_strict"

#: ST-09: the MSS level-selection fork.  The corpus attests four incompatible
#: mains and chooses none of them, so the implementation must declare the one it
#: uses instead of silently taking the latest confirmed swing (P0-20).
STRUCTURE_LEVEL_SELECTIONS = ("most_recent_confirmed", "all_confirmed")
DEFAULT_STRUCTURE_LEVEL_SELECTION = "most_recent_confirmed"
STRUCTURE_LEVEL_SELECTION_SOURCES: dict[str, dict[str, str]] = {
    "most_recent_confirmed": {
        "label": "repo",
        "st09": "most-recent secondary swing",
        "source": ("repo default; ST-09 lists four mains and chooses none of "
                   "them"),
    },
    "all_confirmed": {
        "label": "corpus",
        "st04": "exists swing_high(j), j < t: close[t] > price(j)",
        "source": ("ST-04 existential break over every confirmed swing; the "
                   "event attaches the nearest broken level"),
    },
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ICTConfig:
    mode: Literal["present", "historical"] = "present"
    swing_len: int = PIVOT_LEFT
    displacement_filter: bool = True
    displacement_body_ratio: float = 0.36
    fvg_min_atr: float = 0.0
    fvg_min_body: float = 0.0
    fvg_min_width_atr: float = 0.0
    fvg_max_age: int = 500
    show_fvg: bool = True
    show_bpr: bool = False
    show_ifvg: bool = True
    show_ob: bool = True
    show_liquidity: bool = True
    show_nwog: bool = True
    show_ndog: bool = False
    show_killzones: bool = False
    show_fib: bool = True
    max_fvg: int = 2
    max_ob: int = 1
    max_liq: int = 2
    max_nwog: int = 3
    max_ndog: int = 1
    max_events: int = 4
    liq_margin_atr: float = 2.5  # tolerance = ATR / liq_margin_atr
    plot_bars: int = 260
    show_cisd: bool = True
    cisd_min_grade: int = 3
    cisd_max: int = 2
    cisd_all: bool = False
    cisd_extend: int = 16
    cisd_require_origin: bool = True
    cisd_min_displacement_atr: float = 0.0
    show_htf_cisd: bool = True
    htf_tfs: tuple[str, ...] = ("1h", "4h")
    max_htf: int = 2
    show_sessions: bool = True
    max_sessions: int = 2
    show_sweeps: bool = True
    sweep_window: int = 15
    sweep_reclaim_bars: int = 3
    sweep_min_penetration_atr: float = 0.0
    max_key_levels: int = 10
    show_smt: bool = False
    max_smt: int = 3
    #: ST-09: which confirmed swing the break is tested against (declared, not
    #: implied).  ``detect_structure`` validates the value.
    structure_level_selection: str = DEFAULT_STRUCTURE_LEVEL_SELECTION
    #: PP:C9/R4: which breaker tier is emitted (``minimal`` flips a failed OB;
    #: ``sequenced`` additionally requires a sweep before the flip).
    breaker_tier: str = DEFAULT_BREAKER_TIER


@dataclass
class Swing:
    index: int
    price: float
    kind: Literal["H", "L"]
    confirmed_at: int


@dataclass
class StructureEvent:
    index: int
    price: float
    kind: Literal["MSS", "BOS"]
    direction: Direction
    #: Identity of the broken swing (P0-20).  The de-dupe key is
    #: ``(swing_index, price)``: a re-break of the same price by a *newer*
    #: swing is a new event, breaking the same swing twice is not.
    swing_index: int | None = None
    #: The declared ST-09 selection that produced this event.
    level_selection: str = ""


@dataclass
class FVG:
    direction: Direction
    top: float
    bottom: float
    left: int
    right: int
    created: int
    active: bool = True
    mitigated: bool = False
    label: str = "FVG"
    inverted: bool = False
    invert_bar: int | None = None
    size_atr: float = 0.0
    body_ratio: float = 0.0
    volume_ratio: float = 0.0
    first_presented: bool = False
    touched_at: int | None = None
    signal_id: str = ""

    @property
    def ce(self) -> float:
        """Consequent Encroachment: the 50% midpoint of the gap."""
        return (float(self.top) + float(self.bottom)) / 2.0

    @property
    def fresh(self) -> bool:
        """True only while no later wick has traded inside the gap."""
        return bool(self.active and self.touched_at is None)


@dataclass
class OrderBlock:
    direction: Direction
    top: float
    bottom: float
    left: int
    right: int
    created: int
    active: bool = True
    mitigated: bool = False
    signal_id: str = ""
    #: Body of the selected candle.  AR-17 defines the mean threshold on the
    #: body, not on the zone: the zone may reach a wick (AR-16 hybrid), while
    #: ``mean`` must ignore it.  ``None`` only for hand-built instances, which
    #: have no candle to read and fall back to the zone midpoint.
    body_high: float | None = None
    body_low: float | None = None

    @property
    def mean(self) -> float:
        """Mean threshold: ``(body_high + body_low) / 2`` -- wicks excluded.

        AR-17: this is the body midpoint of the selected candle, explicitly not
        the CE of the zone (``(top + bottom) / 2``), so the two differ whenever
        the zone reaches a wick.
        """
        body_high = float(self.top if self.body_high is None else self.body_high)
        body_low = float(self.bottom if self.body_low is None else self.body_low)
        return (body_high + body_low) / 2.0


@dataclass
class Liquidity:
    direction: Literal["buyside", "sellside"]
    top: float
    bottom: float
    left: int
    right: int
    broken: bool = False
    touches: int = 1

    @property
    def created(self) -> int:
        return self.left

    @property
    def equal(self) -> bool:
        return self.touches >= 2


@dataclass
class Gap:
    kind: Literal["NWOG", "NDOG"]
    top: float
    bottom: float
    left: int
    right: int


@dataclass
class DealingRange:
    """A **structural** dealing range -- one construction, one type (DR-00).

    ``known_at`` is the bar where the range becomes knowable: both structural
    anchors must have confirmed, so it is the later confirmation (CB-07).

    P0-16: a fixed-count high/low object is **not** this type and is never
    emitted as ``DEALING_RANGE`` (DR-00/SN-R07).  The former 40-bar fallback is
    gone; when no confirmed structural pair exists the constructor returns
    ``None`` together with a :class:`~ict_trader.context.RangeDiagnostic`.  An
    IPDA window is the separate :class:`IPDAWindow` type.
    """

    top: float
    bottom: float
    known_at: int
    left: int
    right: int

    @property
    def ce(self) -> float:
        """Equilibrium: the 50% of the range (range CE, not a gap CE) -- DR-03."""
        return (float(self.top) + float(self.bottom)) / 2.0

    @property
    def fallback(self) -> bool:
        """Always ``False`` (kept for callers written before P0-16).

        A structural range can no longer be constructed from a fixed count, so
        there is no fallback branch that could set this flag.
        """
        return False


#: The declared IPDA window classes (IP-01/DR-00).  A fixed count is legal
#: **only** for this object, never for a structural dealing range.
IPDA_WINDOWS: tuple[int, ...] = (20, 40, 60)


@dataclass(frozen=True, slots=True)
class IPDAWindow:
    """A fixed-count IPDA window -- deliberately not a :class:`DealingRange`.

    ``H_N = max(high)`` / ``L_N = min(low)`` over the last ``window`` closed
    candles of the window's own timeframe, with ``CE_N = (H_N + L_N)/2`` (IP-01).
    DR-00: merging this object with the structural range makes one of the two
    anchoring prohibitions unenforceable, so the two are separate types.
    """

    window: int
    high: float
    low: float
    known_at: int
    right: int
    timeframe: str = "1d"

    @property
    def ce(self) -> float:
        """``EQ_N`` -- one midpoint per window (IP-01), not a structural EQ."""
        return (float(self.high) + float(self.low)) / 2.0

    @property
    def span(self) -> float:
        return float(self.high) - float(self.low)


#: §7.1: the "50%" formula is shared but the object is not.  Each producer
#: carries its own type so a downstream reader cannot mistake a gap CE or an OB
#: body mean for the equilibrium of a dealing range (X8).
@dataclass(frozen=True, slots=True)
class RangeEquilibrium:
    """``(top + bottom)/2`` of a structural dealing range (DR-03)."""

    value: float
    kind: str = "range_equilibrium"


@dataclass(frozen=True, slots=True)
class GapConsequentEncroachment:
    """``(top + bottom)/2`` of an FVG/IFVG (AR-07)."""

    value: float
    kind: str = "gap_ce"


@dataclass(frozen=True, slots=True)
class OrderBlockBodyMean:
    """``(body_high + body_low)/2`` of the selected OB candle (AR-17)."""

    value: float
    kind: str = "ob_body_mean"


@dataclass
class FibLevel:
    ratio: float
    price: float


@dataclass
class ICTResult:
    bars: pd.DataFrame
    swings: list[Swing] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    fvgs: list[FVG] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    pd_arrays: list[PDAArray] = field(default_factory=list)
    liquidity: list[Liquidity] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    killzones: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    fib: list[FibLevel] = field(default_factory=list)
    trend: pd.Series | None = None
    cisds: list[CISD] = field(default_factory=list)
    htf_cisds: list[CISD] = field(default_factory=list)
    dealing_range: DealingRange | None = None
    #: Why ``dealing_range`` is absent (``NT_RANGE_INVALID`` / ``NT_DATA_MISSING``).
    #: P0-16: absence carries a reason instead of vanishing silently.
    dealing_range_diagnostic: RangeDiagnostic | None = None
    sessions: list[SessionLevel] = field(default_factory=list)
    smts: list[SMT] = field(default_factory=list)
    key_levels: list[KeyLevel] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)
    config: ICTConfig = field(default_factory=ICTConfig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    rename = {
        "日期": "date", "时间": "date", "datetime": "date",
        "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    cols = ["date", "open", "high", "low", "close"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    else:
        df["volume"] = 0.0
    return df.dropna(subset=cols).reset_index(drop=True)


def find_pivots(df: pd.DataFrame, left: int = PIVOT_LEFT,
                right: int = PIVOT_RIGHT) -> list[Swing]:
    """Pine-like ta.pivothigh/pivotlow with the **declared** left/right bars.

    The width is :data:`PIVOT_WIDTH` (repo 5/1); it is a parameter only so a
    caller can state a different declaration, never so a module can carry a
    private copy (ST-03, P0-20).  Ties follow :data:`PIVOT_TIE_RULE`
    (``non_strict``: ``>=`` high, ``<=`` low), so two equal highs are two
    pivots (LQ-03).
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    out: list[Swing] = []
    n = len(df)
    for i in range(left, n - right):
        window_h = highs[i - left: i + right + 1]
        if highs[i] >= window_h.max():
            out.append(Swing(i, float(highs[i]), "H", i + right))
        window_l = lows[i - left: i + right + 1]
        if lows[i] <= window_l.min():
            out.append(Swing(i, float(lows[i]), "L", i + right))
    return out


def build_zigzag(pivots: list[Swing]) -> list[Swing]:
    """Alternating H/L zigzag, replacing the last point on extension.

    An equal extreme keeps the **later** pivot: the most recent confirmed swing
    is the object the dealing range and structure reads must anchor on, so the
    earlier equal high/low may not survive (ST-03/LQ-03, DR anchor).
    """
    pivots = sorted(pivots, key=lambda s: (s.index, s.kind))
    zz: list[Swing] = []
    for p in pivots:
        if not zz:
            zz.append(p)
            continue
        last = zz[-1]
        if p.kind == last.kind:
            if p.kind == "H":
                extends = p.price >= last.price
            else:
                extends = p.price <= last.price
            if extends:
                zz[-1] = p
            continue
        zz.append(p)
    return zz


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
def _candidate_high(level_selection: str, close_t: float, t: int,
                    last_high: Swing | None,
                    confirmed_prices: list[float],
                    index_at_price: dict[float, int]) -> tuple[int, float] | None:
    """The swing high the declared ST-09 selection tests at bar ``t``."""
    if level_selection == "most_recent_confirmed":
        if last_high is None or int(last_high.index) >= t:
            return None
        if close_t > float(last_high.price):
            return int(last_high.index), float(last_high.price)
        return None
    # all_confirmed: ST-04 existential.  The nearest confirmed high strictly
    # below the close is a witness of the break and the level to report.
    pos = bisect_left(confirmed_prices, float(close_t)) - 1
    if pos < 0:
        return None
    level = float(confirmed_prices[pos])
    sw_index = int(index_at_price.get(level, -1))
    if sw_index < 0 or sw_index >= t:
        return None
    return sw_index, level


def _candidate_low(level_selection: str, close_t: float, t: int,
                   last_low: Swing | None,
                   confirmed_prices: list[float],
                   index_at_price: dict[float, int]) -> tuple[int, float] | None:
    """The swing low the declared ST-09 selection tests at bar ``t``."""
    if level_selection == "most_recent_confirmed":
        if last_low is None or int(last_low.index) >= t:
            return None
        if close_t < float(last_low.price):
            return int(last_low.index), float(last_low.price)
        return None
    # all_confirmed: the nearest confirmed low strictly above the close.
    pos = bisect_right(confirmed_prices, float(close_t))
    if pos >= len(confirmed_prices):
        return None
    level = float(confirmed_prices[pos])
    sw_index = int(index_at_price.get(level, -1))
    if sw_index < 0 or sw_index >= t:
        return None
    return sw_index, level


def detect_structure(
    df: pd.DataFrame,
    swings: list[Swing],
    *,
    level_selection: str = DEFAULT_STRUCTURE_LEVEL_SELECTION,
) -> tuple[list[StructureEvent], pd.Series]:
    """Close-through-swing MSS/BOS with a simple trend state (ST-04/ST-06).

    Two formerly implicit choices are now declarations (P0-20):

    * **level selection** (ST-09).  ``most_recent_confirmed`` tests only the
      latest confirmed swing on each side -- the repo reading.  ``all_confirmed``
      applies ST-04 literally over every confirmed swing and reports the nearest
      level that was actually broken.
    * **de-dupe identity** (C12).  The key is ``(swing.index, swing.price)``,
      never the price alone: a re-break of the same price by a newer swing is a
      new event, while breaking the same swing twice is not.
    """
    if level_selection not in STRUCTURE_LEVEL_SELECTIONS:
        raise ValueError(
            f"unknown structure level selection {level_selection!r}; declare "
            f"one of {STRUCTURE_LEVEL_SELECTIONS} (ST-09)")
    n = len(df)
    close = df["close"].to_numpy()
    events: list[StructureEvent] = []
    trend = 0
    trend_series = np.zeros(n, dtype=int)
    broken_highs: set[tuple[int, float]] = set()
    broken_lows: set[tuple[int, float]] = set()

    # Point-in-time swing pointers.  The old implementation rebuilt
    # `[s for s in swings ...]` on every bar, which is O(N * swings) and
    # unusable on multi-year 15m histories.
    highs_sorted = sorted(
        (s for s in swings if s.kind == "H"),
        key=lambda s: (int(s.confirmed_at), int(s.index)),
    )
    lows_sorted = sorted(
        (s for s in swings if s.kind == "L"),
        key=lambda s: (int(s.confirmed_at), int(s.index)),
    )
    hi = li = 0
    last_high: Swing | None = None
    last_low: Swing | None = None
    # ``all_confirmed`` state: confirmed prices plus the latest index at each
    # price (so an equal-price newer swing changes the identity, as required).
    confirmed_high_prices: list[float] = []
    confirmed_low_prices: list[float] = []
    high_index_at: dict[float, int] = {}
    low_index_at: dict[float, int] = {}

    for t in range(n):
        while hi < len(highs_sorted) and int(highs_sorted[hi].confirmed_at) <= t:
            s = highs_sorted[hi]
            last_high = s
            if level_selection == "all_confirmed":
                price = float(s.price)
                insort(confirmed_high_prices, price)
                high_index_at[price] = max(
                    int(s.index), high_index_at.get(price, -1))
            hi += 1
        while li < len(lows_sorted) and int(lows_sorted[li].confirmed_at) <= t:
            s = lows_sorted[li]
            last_low = s
            if level_selection == "all_confirmed":
                price = float(s.price)
                insort(confirmed_low_prices, price)
                low_index_at[price] = max(
                    int(s.index), low_index_at.get(price, -1))
            li += 1

        cand_h = _candidate_high(level_selection, float(close[t]), t, last_high,
                                 confirmed_high_prices, high_index_at)
        cand_l = _candidate_low(level_selection, float(close[t]), t, last_low,
                                confirmed_low_prices, low_index_at)

        if cand_h is not None and cand_h not in broken_highs:
            kind: Literal["MSS", "BOS"] = "MSS" if trend <= 0 else "BOS"
            events.append(StructureEvent(
                t, cand_h[1], kind, "bull",
                swing_index=cand_h[0], level_selection=level_selection))
            broken_highs.add(cand_h)
            trend = 1
        elif cand_l is not None and cand_l not in broken_lows:
            kind = "MSS" if trend >= 0 else "BOS"
            events.append(StructureEvent(
                t, cand_l[1], kind, "bear",
                swing_index=cand_l[0], level_selection=level_selection))
            broken_lows.add(cand_l)
            trend = -1
        trend_series[t] = trend
    return events, pd.Series(trend_series, index=df.index, name="trend")


def detect_ipda_window(frame: pd.DataFrame, window: int = 40, *,
                       timeframe: str = "1d",
                       last_closed: int | None = None) -> IPDAWindow | None:
    """The last ``window`` **closed** candles as a separate IPDA window (DR-00).

    ``frame`` must already be the window's own timeframe (daily for the
    ``20/40/60`` set); ``last_closed`` is the index of the last bar whose close
    is known.  A fixed count is legal here and nowhere else, so this object is
    an :class:`IPDAWindow`, never a :class:`DealingRange`.  Wiring the window
    into ``analyze``/section 7.3 is P1-12; the P0 subset is the distinct type
    and the refusal to anchor a structural range with it.
    """
    if int(window) not in IPDA_WINDOWS:
        raise ValueError(
            f"IPDA windows are {IPDA_WINDOWS}; got {window!r} (IP-01/DR-00)")
    n = len(frame)
    if n == 0:
        return None
    end = n - 1 if last_closed is None else int(last_closed)
    start = end - int(window) + 1
    if end < 0 or end >= n or start < 0:
        return None
    high = float(frame["high"].iloc[start:end + 1].max())
    low = float(frame["low"].iloc[start:end + 1].min())
    if not high > low:
        return None
    return IPDAWindow(int(window), high, low, end, end, timeframe)


def detect_dealing_range_state(df: pd.DataFrame,
                               swings: list[Swing] | None = None, *,
                               swing_len: int = 5
                               ) -> tuple[DealingRange | None, RangeDiagnostic | None]:
    """Structural dealing range **and** the reason when there is none.

    This is the **only** structural range constructor.  ``known_at`` is the
    later confirmation of the two anchors (CB-07), never the earlier one.  A
    fixed count may not anchor it (DR-00/SN-R07), so there is no fallback
    branch: without a confirmed swing pair the result is ``(None,
    diagnostic)`` and no ``DEALING_RANGE`` can be emitted (P0-16).  The same
    ``RANGE_STALENESS_BARS`` (120) repo cap as the context read applies, so a
    stale pair is a diagnosed absence, not a silent ``None``.  The value-only
    twin is :func:`detect_dealing_range`.
    """
    n = len(df)
    last_t = n - 1
    if n < 20:
        return None, RangeDiagnostic(
            NT_DATA_MISSING,
            f"only {n} bars available; the structural range needs enough closed "
            f"bars for a confirmed swing pair (got < 20) "
            f"(NT_RANGE_TIME_ANCHORED names the prohibited fixed-count "
            f"fallback, which is not constructed).",
            bar=last_t,
        )
    if swings is None:
        swings = build_zigzag(find_pivots(df, swing_len, 1))
    highs = [s for s in swings
             if s.kind == "H" and int(getattr(s, "confirmed_at", s.index)) <= last_t]
    lows = [s for s in swings
            if s.kind == "L" and int(getattr(s, "confirmed_at", s.index)) <= last_t]
    if not highs or not lows:
        missing = ("high or low" if not highs and not lows
                   else "high" if not highs else "low")
        return None, RangeDiagnostic(
            NT_DATA_MISSING,
            f"no confirmed swing {missing} at or before bar {last_t}: a "
            "structural dealing range needs one confirmed high and one "
            "confirmed low.  The fixed-count 40-bar high/low fallback is "
            "refused (DR-00/SN-R07), so no DEALING_RANGE is emitted "
            "(NT_RANGE_TIME_ANCHORED would name that prohibited object).",
            bar=last_t,
        )
    h, l = highs[-1], lows[-1]
    known_at = max(int(getattr(h, "confirmed_at", h.index)),
                   int(getattr(l, "confirmed_at", l.index)))
    age = last_t - known_at
    if age > RANGE_STALENESS_BARS:
        return None, RangeDiagnostic(
            NT_RANGE_INVALID,
            f"dealing-range anchors are stale: the later anchor confirmed at "
            f"bar {known_at}, {age} bars before bar {last_t} > "
            f"{RANGE_STALENESS_BARS}-bar repo staleness cap (§13.3).  Redraw "
            "on the DR-06 triggers; the range is not usable as-is.",
            bar=last_t, age_bars=age, lookback_bars=RANGE_STALENESS_BARS,
        )
    top = max(float(h.price), float(l.price))
    bottom = min(float(h.price), float(l.price))
    if top <= bottom:
        return None, RangeDiagnostic(
            NT_RANGE_INVALID,
            f"dealing-range anchors are degenerate: top <= bottom at bar "
            f"{last_t}, so no range can be measured (DR-01).",
            bar=last_t, age_bars=age,
        )
    return DealingRange(top, bottom, known_at,
                        min(int(h.index), int(l.index)), last_t), None


def detect_dealing_range(df: pd.DataFrame, swings: list[Swing] | None = None,
                         *, swing_len: int = 5) -> DealingRange | None:
    """Value-only twin of :func:`detect_dealing_range_state` (P0-16)."""
    return detect_dealing_range_state(df, swings, swing_len=swing_len)[0]


def cisd_config(cfg: ICTConfig) -> CISDConfig:
    """Map the one ``ICTConfig`` onto the canonical CISD configuration.

    Both ``analyze`` and the realtime array lifecycle read this function, so a
    CISD parameter is never copied into a second module (A7).
    """
    return CISDConfig(
        enabled=cfg.show_cisd,
        min_grade=cfg.cisd_min_grade,
        max_per_side=(100000 if cfg.cisd_all else cfg.cisd_max),
        extend_bars=cfg.cisd_extend,
        require_origin=cfg.cisd_require_origin,
        min_displacement_atr=cfg.cisd_min_displacement_atr,
        sweep_window=cfg.sweep_window,
        sweep_reclaim_bars=cfg.sweep_reclaim_bars,
        lookback=500,
        hide_invalid=not cfg.cisd_all,
    )


def detect_fvgs(df: pd.DataFrame, cfg: ICTConfig) -> list[FVG]:
    if not cfg.show_fvg:
        return []
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    body = np.abs(close - open_)
    mean_body = pd.Series(body).rolling(cfg.swing_len, min_periods=1).mean().to_numpy()
    atr14 = atr(df).to_numpy()
    vol = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    vol_avg = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()
    out: list[FVG] = []
    n = len(df)
    for i in range(2, n):
        disp_up = body[i - 1] > mean_body[i - 1] and close[i - 1] > open_[i - 1]
        disp_dn = body[i - 1] > mean_body[i - 1] and close[i - 1] < open_[i - 1]
        body_ratio = body[i - 1] / max(high[i - 1] - low[i - 1], 1e-12)
        vol_ratio = vol[i - 1] / vol_avg[i - 1] if vol_avg[i - 1] > 0 else 0.0
        if low[i] > high[i - 2] and (disp_up or not cfg.displacement_filter):
            gap = float(low[i] - high[i - 2])
            if (gap >= cfg.fvg_min_atr * atr14[i] and body_ratio >= cfg.fvg_min_body
                    and gap >= cfg.fvg_min_width_atr * atr14[i]):
                out.append(FVG("bull", float(low[i]), float(high[i - 2]), i - 2, i, i,
                               size_atr=gap / max(atr14[i], 1e-12), body_ratio=body_ratio,
                               volume_ratio=vol_ratio))
        if high[i] < low[i - 2] and (disp_dn or not cfg.displacement_filter):
            gap = float(low[i - 2] - high[i])
            if (gap >= cfg.fvg_min_atr * atr14[i] and body_ratio >= cfg.fvg_min_body
                    and gap >= cfg.fvg_min_width_atr * atr14[i]):
                out.append(FVG("bear", float(low[i - 2]), float(high[i]), i - 2, i, i,
                               size_atr=gap / max(atr14[i], 1e-12), body_ratio=body_ratio,
                               volume_ratio=vol_ratio))

    # The first FVG of a displacement cluster is the highest-priority retest.
    by_dir: dict[str, list[FVG]] = {"bull": [], "bear": []}
    for f in sorted(out, key=lambda x: x.created):
        prior = [g for g in by_dir[f.direction] if f.created - g.created <= 5]
        f.first_presented = not prior
        by_dir[f.direction].append(f)

    # Update lifecycle and right edges.  AR-04: an FVG is invalidated only by a
    # **body close** through the far edge (bull ``close < bottom``, bear
    # ``close > top``).  A wick beyond that edge which closes back inside keeps
    # the gap alive as a *tested* FVG (AR-10a): ``touched_at`` records the wick,
    # ``active``/``mitigated`` record the body close.
    # Vectorised per-gap scan.  The old Python nested loop was O(FVG * N) in
    # interpreted code and became unusable on multi-year 15m histories.
    for f in out:
        start = f.created + 1
        if start >= n:
            f.right = n - 1
            continue
        if f.direction == "bull":
            touch = low[start:] < f.top  # wick inside the gap => tested
            invalidated = close[start:] < f.bottom  # body close beyond far edge
        else:
            touch = high[start:] > f.bottom
            invalidated = close[start:] > f.top
        if touch.any():
            f.touched_at = start + int(np.argmax(touch))
        if invalidated.any():
            f.active = False
            f.mitigated = True
            f.right = start + int(np.argmax(invalidated))
            continue
        f.right = n - 1
    return out


def detect_bpr(fvgs: list[FVG]) -> list[FVG]:
    """Balance Price Range = the intersection of two opposite-direction FVGs.

    AR-11: the band is the intersection (never the union) and exists only while
    ``bottom < top``.  AR-12: the constituents must be opposite-direction
    *gaps*; an IFVG is a flipped gap, not a gap, and a previously derived BPR is
    not a gap either, so only strict ``label == "FVG"`` rows that are still
    directional (``not inverted``) qualify.

    Polarity is a corpus conflict (PP:C3: first gap / newer gap / entry side;
    V18 must choose).  P0 declares the *newer gap* reading (b), which two of the
    three cited sources attest, instead of the old hardcoded ``"bull"``.
    """
    gaps = [f for f in fvgs if f.label == "FVG" and not f.inverted]
    bulls = [f for f in gaps if f.direction == "bull"]
    bears = [f for f in gaps if f.direction == "bear"]
    if not bulls or not bears:
        return []
    b, s = bulls[-1], bears[-1]
    top = min(b.top, s.top)
    bottom = max(b.bottom, s.bottom)
    if top <= bottom:
        return []
    direction: Direction = "bear" if s.created > b.created else "bull"
    return [FVG(direction, top, bottom, min(b.left, s.left),
                max(b.right, s.right), max(b.created, s.created),
                active=True, mitigated=False, label="BPR")]


def detect_ifvg(fvgs: list[FVG], df: pd.DataFrame) -> list[FVG]:
    """A close through an FVG flips it into an inverse FVG (IFVG)."""
    close = df["close"].to_numpy(float)
    n = len(df)
    out: list[FVG] = []
    for f in fvgs:
        if f.inverted or f.invert_bar is not None:
            continue
        for t in range(f.created + 1, n):
            if f.direction == "bull" and close[t] < f.bottom:
                f.inverted = True
                f.invert_bar = t
                out.append(FVG("bear", f.top, f.bottom, t, n - 1, t,
                               active=True, label="IFVG", inverted=True, invert_bar=t))
                break
            if f.direction == "bear" and close[t] > f.top:
                f.inverted = True
                f.invert_bar = t
                out.append(FVG("bull", f.top, f.bottom, t, n - 1, t,
                               active=True, label="IFVG", inverted=True, invert_bar=t))
                break
    return out


def detect_order_blocks(df: pd.DataFrame, events: list[StructureEvent], swings: list[Swing],
                        cfg: ICTConfig) -> list[OrderBlock]:
    """Order blocks selected by recency, not by size (AR-15, AR-17).

    The candidate is ``t* = max{t < e : sign(close[t] - open[t]) = -leg_sign}``
    -- the **last opposite-close candle** before the displacement leg that ends
    at the structure event ``e``.  The swing-extreme bar is a different object
    and is not the candidate (``swings`` is kept for API compatibility only).

    The zone is the repo's declared hybrid (AR-16: wick extreme on the far
    side, body on the near side), but ``OrderBlock.mean`` is read from the
    selected candle's body, never from the zone (AR-17).
    """
    if not cfg.show_ob:
        return []
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    n = len(df)

    # Last opposite-close bar at or before each index.  Precomputing keeps the
    # selection O(bars) overall instead of O(events * bars), which matters on
    # multi-year 15m histories.  A doji (close == open) has sign 0 and is
    # neither an up-close nor a down-close candle.
    last_down = np.full(n, -1, dtype=int)
    last_up = np.full(n, -1, dtype=int)
    down = up = -1
    for t in range(n):
        if closes[t] < opens[t]:
            down = t
        elif closes[t] > opens[t]:
            up = t
        last_down[t] = down
        last_up[t] = up

    out: list[OrderBlock] = []
    for ev in events:
        e = int(ev.index)
        if e <= 0 or e >= n:
            continue
        # AR-15: the last opposite-close candle strictly before the leg end.
        t_star = int(last_down[e - 1] if ev.direction == "bull" else last_up[e - 1])
        if t_star < 0:
            continue  # no opposite-close candle: refuse rather than fabricate one
        body_high = float(max(opens[t_star], closes[t_star]))
        body_low = float(min(opens[t_star], closes[t_star]))
        if ev.direction == "bull":
            top = body_high
            bottom = float(lows[t_star])
            if top <= bottom:
                top = float(highs[t_star])
            out.append(OrderBlock("bull", top, bottom, t_star, e, e,
                                  body_high=body_high, body_low=body_low))
        else:
            top = float(highs[t_star])
            bottom = body_low
            if top <= bottom:
                bottom = float(lows[t_star])
            out.append(OrderBlock("bear", top, bottom, t_star, e, e,
                                  body_high=body_high, body_low=body_low))

    # mitigation
    for ob in out:
        for t in range(ob.created + 1, n):
            if ob.direction == "bull":
                if closes[t] < ob.bottom:
                    ob.active = False
                    ob.mitigated = True
                    ob.right = t
                    break
                if closes[t] > ob.top:
                    ob.right = t
            else:
                if closes[t] > ob.top:
                    ob.active = False
                    ob.mitigated = True
                    ob.right = t
                    break
                if closes[t] < ob.bottom:
                    ob.right = t
        if ob.active:
            ob.right = n - 1
    return out


def detect_liquidity(df: pd.DataFrame, pivots: list[Swing], cfg: ICTConfig) -> list[Liquidity]:
    if not cfg.show_liquidity:
        return []
    atr_series = atr(df).to_numpy()
    tol = max(float(np.nanmedian(atr_series)) / max(cfg.liq_margin_atr, 0.1), 1e-9)
    out: list[Liquidity] = []
    n = len(df)
    for kind, direction in (("H", "buyside"), ("L", "sellside")):
        pts = [p for p in pivots if p.kind == kind]
        pts.sort(key=lambda p: p.index)
        used: set[int] = set()
        for i, p in enumerate(pts):
            if i in used:
                continue
            cluster = [p]
            for j in range(i + 1, len(pts)):
                if abs(pts[j].price - p.price) <= tol:
                    cluster.append(pts[j])
                    used.add(j)
                elif pts[j].index - p.index > 80:
                    break
            if len(cluster) >= 3:
                left, right = cluster[0].index, cluster[-1].index
                if direction == "buyside":
                    top = max(x.price for x in cluster) + tol
                    bottom = min(x.price for x in cluster) - tol
                else:
                    top = max(x.price for x in cluster) + tol
                    bottom = min(x.price for x in cluster) - tol
                out.append(Liquidity(direction, top, bottom, left,
                                     min(right + 10, n - 1), touches=len(cluster)))
    # break state
    closes = df["close"].to_numpy()
    for liq in out:
        for t in range(liq.left, n):
            if liq.direction == "buyside" and closes[t] > liq.top:
                liq.broken = True
                liq.right = t
                break
            if liq.direction == "sellside" and closes[t] < liq.bottom:
                liq.broken = True
                liq.right = t
                break
    return out


def detect_gaps(df: pd.DataFrame, cfg: ICTConfig) -> list[Gap]:
    out: list[Gap] = []
    n = len(df)
    dates = pd.to_datetime(df["date"])
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    for i in range(1, n):
        # NDOG: previous close -> today's open
        if cfg.show_ndog and abs(opens[i] - closes[i - 1]) > 1e-12:
            out.append(Gap("NDOG", max(opens[i], closes[i - 1]), min(opens[i], closes[i - 1]), i - 1, i))
        # NWOG: Friday close -> Monday open
        if cfg.show_nwog and dates.iloc[i].weekday() == 0 and dates.iloc[i - 1].weekday() == 4:
            out.append(Gap("NWOG", max(opens[i], closes[i - 1]), min(opens[i], closes[i - 1]), i - 1, i))
    # keep latest N per kind
    result: list[Gap] = []
    for kind, max_n in (("NWOG", cfg.max_nwog), ("NDOG", cfg.max_ndog)):
        subset = [g for g in out if g.kind == kind][-max_n:] if max_n else []
        result.extend(subset)
    return result


#: Killzone labels in ``ICTResult.killzones`` are aliases into the one session
#: table in :mod:`ict_trader.context` (P0-12).  They are presentation names, not
#: a second clock.
_KILLZONE_ALIASES: dict[str, str] = {
    "New York": "ny_am",
    "London Open": "london",
    "London Close": "london_close",
    "Asian": "asian",
}


def detect_killzones(df: pd.DataFrame, cfg: ICTConfig) -> dict[str, list[tuple[int, int]]]:
    """Annotate the canonical session windows that contain each bar.

    Every window is read through :func:`context.in_session_at`; this module
    keeps no timezone table of its own, so a killzone span cannot disagree with
    ``context.in_session`` (P0-12).  Silver Bullet is a model window, not a
    killzone (silver_bullet R12 / killzone R32), so it is deliberately absent.
    """
    if not cfg.show_killzones or len(df) < 3:
        return {}
    dt = pd.to_datetime(df["date"])
    if (dt.iloc[-1] - dt.iloc[0]).total_seconds() / max(len(df) - 1, 1) > 20 * 3600:
        return {}  # daily bars: no intraday killzones
    zones: dict[str, list[tuple[int, int]]] = {}
    for name, key in _KILLZONE_ALIASES.items():
        current: list[int] = []
        spans: list[tuple[int, int]] = []
        for i, ts in enumerate(dt):
            if in_session_at(ts, key):
                current.append(i)
            elif current:
                spans.append((current[0], current[-1]))
                current = []
        if current:
            spans.append((current[0], current[-1]))
        zones[name] = spans
    return zones


def fibonacci(swings: list[Swing]) -> list[FibLevel]:
    if len(swings) < 2:
        return []
    a, b = swings[-2], swings[-1]
    diff = b.price - a.price
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.618]
    return [FibLevel(r, a.price + diff * r) for r in ratios]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _previous_day_levels(df: pd.DataFrame) -> list[tuple[float, str]]:
    dates = pd.to_datetime(df["date"]).dt.date
    out: list[tuple[float, str]] = []
    for i in range(1, len(df)):
        if dates.iloc[i] != dates.iloc[i - 1]:
            prev = df.iloc[i - 1]
            out.append((float(prev["high"]), "H"))
            out.append((float(prev["low"]), "L"))
    # dedupe near-identical prices
    uniq: dict[tuple[float, str], None] = {}
    for p, k in out:
        uniq[(round(p, 4), k)] = None
    return list(uniq.keys())


#: Repo §13.6 "500-bar present-mode cut".  A display constant only: the
#: inventory returned by :func:`analyze` is complete, so no eligible array or
#: sweep can disappear before selection (P0-14).
PRESENT_WINDOW_BARS = 500


def presentation_view(result: ICTResult, *,
                      bars: int = PRESENT_WINDOW_BARS) -> ICTResult:
    """Return the chart/CSV view of ``result`` with the present-mode window.

    The 500-bar cut is a presentation rule, never a selection rule: it is
    applied here (and only here) so ``analyze`` can keep the full array/sweep
    inventory (P0-14, A1, §7.3).  ``historical`` mode has no window, so the
    result is returned unchanged.  The input is never mutated.
    """
    if result.config.mode != "present":
        return result
    cutoff = max(0, len(result.bars) - max(int(bars), 0))
    return replace(
        result,
        events=[e for e in result.events if int(e.index) >= cutoff],
        fvgs=[f for f in result.fvgs if int(f.created) >= cutoff],
        order_blocks=[o for o in result.order_blocks if int(o.created) >= cutoff],
        liquidity=[l for l in result.liquidity if int(l.created) >= cutoff],
        gaps=[g for g in result.gaps if int(g.left) >= cutoff],
        cisds=[c for c in result.cisds if int(c.end) >= cutoff],
        htf_cisds=[c for c in result.htf_cisds if int(c.end) >= cutoff],
        sessions=[s for s in result.sessions if int(s.end) >= cutoff],
        smts=[s for s in result.smts if int(s.end) >= cutoff],
        key_levels=[k for k in result.key_levels if int(k.known_at) >= cutoff],
        sweeps=[s for s in result.sweeps if int(s.reclaim_bar) >= cutoff],
    )


def analyze(df: pd.DataFrame, config: ICTConfig | None = None,
            smt_df: pd.DataFrame | None = None) -> ICTResult:
    cfg = config or ICTConfig()
    if len(df) < max(cfg.swing_len + 2, 10):
        raise ValueError("not enough bars")
    pivots = find_pivots(df, cfg.swing_len, PIVOT_RIGHT)
    swings = build_zigzag(pivots)
    events, trend = detect_structure(
        df, swings, level_selection=cfg.structure_level_selection)
    # Levels/sweeps are built first: a breaker is a failed OB **after a sweep**
    # (PP:C9/R4), so the declared tier needs the sweep inventory at detection
    # time instead of inheriting a bare "mitigated" flag (P0-20).
    liq = detect_liquidity(df, pivots, cfg)
    sessions = detect_session_levels(df, max_per=cfg.max_sessions) if cfg.show_sessions else []
    key_levels = detect_key_levels(df, swings, sessions, liq)
    sweeps = detect_sweeps(df, key_levels,
                           min_penetration_atr=cfg.sweep_min_penetration_atr,
                           reclaim_window=cfg.sweep_reclaim_bars) if cfg.show_sweeps else []
    score_key_levels(key_levels, sessions, df)
    fvgs = detect_fvgs(df, cfg)
    if cfg.show_ifvg:
        fvgs.extend(detect_ifvg(list(fvgs), df))
    if cfg.show_bpr:
        fvgs.extend(detect_bpr(fvgs))
    obs = detect_order_blocks(df, events, swings, cfg)
    pd_arrays = (detect_rejections(df, swings)
                 + detect_breakers(obs, sweeps, tier=cfg.breaker_tier)
                 + detect_mitigations(df, swings))
    pd_arrays.sort(key=lambda x: x.confirmed_at)
    gaps = detect_gaps(df, cfg)
    kz = detect_killzones(df, cfg)
    fib = fibonacci(swings) if cfg.show_fib else []
    htf_cisds = detect_htf_cisds(df, cfg.htf_tfs) if cfg.show_htf_cisd else []
    smts = detect_smt(df, smt_df, max_out=cfg.max_smt) if (cfg.show_smt and smt_df is not None) else []
    smt_bars = [(s.end, s.direction) for s in smts]
    sweep_bars = [(s.reclaim_bar, s.direction) for s in sweeps]

    cisds: list[CISD] = []
    if cfg.show_cisd:
        cisd_cfg = cisd_config(cfg)
        cisd_levels = [(s.price, s.kind, s.confirmed_at) for s in swings]
        cisd_zones = [(f.top, f.bottom, f.created, f.direction) for f in fvgs]
        cisd_sessions = [(s.price, s.kind, s.end) for s in sessions]
        cisds = detect_cisd(df, cisd_cfg, cisd_levels, cisd_zones, cisd_sessions, smt_bars, sweep_bars)

    dealing, dealing_diagnostic = detect_dealing_range_state(
        df, swings, swing_len=cfg.swing_len)

    # The 500-bar "present" cut is a *presentation* window (repo §13.6), not a
    # selection rule.  ``analyze`` therefore returns the full inventory: setup
    # selection, target selection and the array lifecycle must be able to see
    # an eligible array or sweep older than the visible window (P0-14, A1,
    # §7.3).  ``presentation_view`` applies the cut for chart/CSV output.
    return ICTResult(
        bars=df, swings=swings, events=events, fvgs=fvgs, order_blocks=obs,
        pd_arrays=pd_arrays,
        liquidity=liq, gaps=gaps, killzones=kz, fib=fib, trend=trend,
        cisds=cisds, htf_cisds=htf_cisds, dealing_range=dealing,
        dealing_range_diagnostic=dealing_diagnostic,
        sessions=sessions, smts=smts,
        key_levels=key_levels, sweeps=sweeps, config=cfg,
    )


def signals_frame(result: ICTResult) -> pd.DataFrame:
    # CSV export is a presentation surface, so the present-mode window applies
    # here rather than to the inventory returned by ``analyze`` (P0-14).
    result = presentation_view(result)
    rows: list[dict] = []
    for ev in result.events:
        rows.append({
            "date": result.bars["date"].iloc[ev.index],
            "type": ev.kind,
            "direction": ev.direction,
            "price": ev.price,
            "index": ev.index,
        })
    for f in result.fvgs:
        rows.append({
            "date": result.bars["date"].iloc[f.created],
            "type": f.label,
            "direction": f.direction,
            "price": (f.top + f.bottom) / 2,
            "index": f.created,
        })
    for c in result.cisds:
        rows.append({
            "date": result.bars["date"].iloc[c.end],
            "type": "CISD",
            "direction": c.direction,
            "price": c.level,
            "index": c.end,
        })
    for s in result.smts:
        rows.append({
            "date": result.bars["date"].iloc[s.end],
            "type": "SMT",
            "direction": s.direction,
            "price": s.price,
            "index": s.end,
        })
    for c in result.htf_cisds:
        rows.append({
            "date": result.bars["date"].iloc[c.end],
            "type": f"HTF_{c.tf}",
            "direction": c.direction,
            "price": c.level,
            "index": c.end,
        })
    for s in result.sweeps:
        rows.append({
            "date": result.bars["date"].iloc[s.reclaim_bar],
            "type": f"SWEEP_{s.name}",
            "direction": s.direction,
            "price": s.level,
            "index": s.reclaim_bar,
            "sweep_index": s.sweep_bar,
            "reclaim_bars": s.reclaim_bars,
        })
    for k in result.key_levels:
        if k.swept or k.broken:
            continue
        rows.append({
            "date": result.bars["date"].iloc[k.known_at],
            "type": f"KEY_{k.name}",
            "direction": "bear" if k.kind == "H" else "bull",
            "price": k.price,
            "index": k.known_at,
        })
    return pd.DataFrame(rows).sort_values("index").reset_index(drop=True)


def _latest_by_direction(items: Iterable, max_n: int, directions: tuple[str, ...]) -> list:
    """Keep only the latest N items per direction, like LuxAlgo's visible boxes."""
    if max_n <= 0:
        return []
    picked = []
    for direction in directions:
        subset = [x for x in items if x.direction == direction]
        subset.sort(key=lambda x: x.created)
        picked.extend(subset[-max_n:])
    picked.sort(key=lambda x: x.created)
    return picked


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot(result: ICTResult, out: str | Path, title: str = "ICT Concepts", bars: int | None = None) -> Path:
    # The chart is a presentation surface: apply the 500-bar present-mode
    # window here instead of truncating the inventory in ``analyze`` (P0-14).
    result = presentation_view(result)
    cfg = result.config
    n = len(result.bars)
    bars = bars or cfg.plot_bars
    start = max(0, n - bars)
    df = result.bars.iloc[start:].reset_index(drop=True)
    m = len(df)
    if m == 0:
        raise ValueError("empty plot window")

    fig, ax = plt.subplots(figsize=(15, 8), dpi=140)
    dates = pd.to_datetime(df["date"])

    # candles
    for i, row in df.iterrows():
        color = "#d62728" if row["close"] < row["open"] else "#2ca02c"
        ax.vlines(i, row["low"], row["high"], color=color, linewidth=0.8, zorder=3)
        body_low = min(row["open"], row["close"])
        body_high = max(row["open"], row["close"])
        height = max(body_high - body_low, 1e-9)
        ax.add_patch(Rectangle((i - 0.32, body_low), 0.64, height,
                               facecolor=color, edgecolor=color, alpha=0.85, zorder=4))

    def rel(idx: int) -> int:
        return idx - start

    # FVG / BPR boxes
    for f in _latest_by_direction(result.fvgs, max(cfg.max_fvg, 0), ("bull", "bear")):
        if f.right < start or f.left > n - 1:
            continue
        x0 = max(rel(f.left), 0)
        x1 = min(rel(f.right), m - 1)
        if x1 <= x0:
            continue
        color = "#00e676" if f.direction == "bull" else "#ff5252"
        alpha = 0.16 if f.active else 0.06
        ax.add_patch(Rectangle((x0, f.bottom), x1 - x0, f.top - f.bottom,
                               facecolor=color, edgecolor=color, alpha=alpha,
                               linestyle="--" if f.label == "BPR" else "-", zorder=1))
        ax.text(x0, (f.top + f.bottom) / 2, f.label, color=color, fontsize=7,
                va="center", alpha=0.9)

    # OB boxes: only the latest still-active order block(s)
    active_obs = [o for o in result.order_blocks if o.active]
    active_obs.sort(key=lambda o: o.created)
    active_obs = active_obs[-max(cfg.max_ob, 0):] if cfg.max_ob > 0 else []
    for ob in active_obs:
        if ob.right < start or ob.left > n - 1:
            continue
        x0 = max(rel(ob.left), 0)
        x1 = min(rel(ob.right), m - 1)
        if x1 <= x0:
            continue
        color = "#3e89fa" if ob.direction == "bull" else "#ff3131"
        ax.add_patch(Rectangle((x0, ob.bottom), x1 - x0, ob.top - ob.bottom,
                               facecolor=color, edgecolor=color, alpha=0.12,
                               linestyle=":", zorder=1))
        ax.text(x1, (ob.top + ob.bottom) / 2, "+OB" if ob.direction == "bull" else "-OB",
                color=color, fontsize=7, va="center", ha="left")

    # liquidity
    for liq in _latest_by_direction(result.liquidity, max(cfg.max_liq, 0), ("buyside", "sellside")):
        if liq.right < start or liq.left > n - 1:
            continue
        x0 = max(rel(liq.left), 0)
        x1 = min(rel(liq.right), m - 1)
        color = "#fa451c" if liq.direction == "buyside" else "#1ce4fa"
        ax.add_patch(Rectangle((x0, liq.bottom), x1 - x0, liq.top - liq.bottom,
                               facecolor="none", edgecolor=color, alpha=0.7,
                               linestyle=":", zorder=2))
        ax.text(x0, liq.top, "BSL" if liq.direction == "buyside" else "SSL",
                color=color, fontsize=7, va="bottom")

    # key levels: BSL/SSL, PDH/PDL, PWH/PWL, Asia/London
    shown_levels = [k for k in result.key_levels if not k.swept and not k.broken]
    shown_levels = sorted(shown_levels, key=lambda k: k.known_at)[-max(cfg.max_key_levels, 0):]
    for k in shown_levels:
        if k.known_at < start:
            continue
        x0 = max(rel(k.known_at), 0)
        x1 = m - 1
        color = "#7E57C2" if k.kind == "H" else "#26A69A"
        ax.hlines(k.price, x0, x1, colors=color, linestyles="--", linewidth=0.7, alpha=0.55, zorder=2)
        ax.text(x1, k.price, k.name, color=color, fontsize=6, va="center", ha="left", alpha=0.8)

    # liquidity sweeps: exact sweep bar, level and reclaim close
    for s in result.sweeps[-8:]:
        if s.sweep_bar < start:
            continue
        color = "#2962FF" if s.direction == "bull" else "#F23645"
        ax.scatter([rel(s.sweep_bar)], [s.sweep_price], marker="x", color=color, s=28, zorder=8)
        ax.text(rel(s.sweep_bar), s.sweep_price, f"${s.name}",
                color=color, fontsize=6, ha="center",
                va="bottom" if s.direction == "bull" else "top", zorder=9)

    # session liquidity: latest PDH/PDL/PWH/PWL and Asia/London levels
    for lvl in result.sessions:
        if lvl.end < start:
            continue
        x0 = max(rel(lvl.start), 0)
        x1 = min(rel(lvl.end) + 8, m - 1)
        color = "#607D8B" if not lvl.swept else "#B0BEC5"
        ax.hlines(lvl.price, x0, x1, colors=color, linestyles=":", linewidth=0.8, alpha=0.8, zorder=2)
        ax.text(x1, lvl.price, lvl.name, color=color, fontsize=6, va="center", ha="left")

    # SMT divergence lines
    for s in result.smts:
        if s.end < start or s.start < 0:
            continue
        y1 = float(result.bars["high"].iloc[s.start]) if s.direction == "bear" else float(result.bars["low"].iloc[s.start])
        y2 = float(result.bars["high"].iloc[s.end]) if s.direction == "bear" else float(result.bars["low"].iloc[s.end])
        color = "#2962FF" if s.direction == "bull" else "#F23645"
        ax.plot([rel(s.start), rel(s.end)], [y1, y2], color=color, linestyle=":", linewidth=0.9, alpha=0.8, zorder=4)
        ax.text(rel(s.end), y2, "SMT", color=color, fontsize=6, va="bottom", ha="center")

    # gaps
    for g in result.gaps:
        if g.right < start:
            continue
        x0 = max(rel(g.left), 0)
        x1 = min(rel(g.right) + 8, m - 1)
        color = "#ff5252" if g.kind == "NWOG" else "#ff9800"
        ax.add_patch(Rectangle((x0, g.bottom), x1 - x0, g.top - g.bottom,
                               facecolor=color, edgecolor=color, alpha=0.10, zorder=0))
        ax.text(x0, g.top, g.kind, color=color, fontsize=7, va="bottom")

    # CISD levels, stop loss and target
    grade_txt = {7: "A+", 6: "A", 5: "B+", 4: "B-", 3: "C", 0: "-"}
    for c in result.cisds:
        if c.end < start:
            continue
        x0 = max(rel(c.start), 0)
        x1 = min(rel(c.end) + cfg.cisd_extend, m - 1)
        color = "#2962FF" if c.direction == "bull" else "#F23645"
        alpha = 0.95 if c.active else 0.35
        ax.hlines(c.level, x0, x1, colors=color, linestyles="-" if c.active else ":",
                  linewidth=1.4, alpha=alpha, zorder=6)
        ax.text(x1, c.level, f"{c.label}{'+' if c.direction == 'bull' else '-'} {grade_txt.get(c.grade, '')}",
                color=color, fontsize=7, va="center", ha="left", alpha=alpha, zorder=7)
        if c.target is not None:
            ax.hlines(c.target, max(rel(c.end) - 2, 0), min(rel(c.end) + 22, m - 1),
                      colors=color, linestyles="--", linewidth=0.8, alpha=0.5, zorder=5)
            ax.text(min(rel(c.end) + 22, m - 1), c.target, "TP", color=color,
                    fontsize=6, va="center", ha="left", alpha=0.7)
        if c.active:
            ax.hlines(c.sl, max(rel(c.end) - 1, 0), min(rel(c.end) + 8, m - 1),
                      colors="#555555", linestyles=":", linewidth=0.8, alpha=0.7, zorder=5)
            ax.text(min(rel(c.end) + 8, m - 1), c.sl, "SL", color="#555555",
                    fontsize=6, va="center", ha="left", alpha=0.7)

    # HTF CISD levels
    for c in result.htf_cisds[-max(cfg.max_htf, 0):]:
        if c.end < start:
            continue
        x0 = max(rel(c.start), 0)
        x1 = min(rel(c.end) + cfg.cisd_extend, m - 1)
        color = "#64B5F6" if c.direction == "bull" else "#E57373"
        ax.hlines(c.level, x0, x1, colors=color, linestyles="--", linewidth=1.0, alpha=0.8, zorder=5)
        ax.text(x1, c.level, f"{c.tf} CISD{'+' if c.direction == 'bull' else '-'}",
                color=color, fontsize=6, va="center", ha="left", alpha=0.8)

    # swings + structure
    shown_events = result.events
    if cfg.mode == "present":
        shown_events = result.events[-max(cfg.max_events, 0):]
    for s in result.swings:
        if start <= s.index < n:
            ax.plot(rel(s.index), s.price, marker="v" if s.kind == "H" else "^",
                    color="#888888", markersize=4, zorder=5)
    for ev in shown_events:
        if start <= ev.index < n:
            color = "#00e6a1" if ev.direction == "bull" else "#e60400"
            y = result.bars["low"].iloc[ev.index] if ev.direction == "bull" else result.bars["high"].iloc[ev.index]
            offset = -0.012 * (df["high"].max() - df["low"].min())
            if ev.direction == "bear":
                offset = -offset
            ax.annotate(ev.kind, (rel(ev.index), y + offset), color=color,
                        fontsize=8, ha="center",
                        va="top" if ev.direction == "bull" else "bottom",
                        fontweight="bold", zorder=6)

    # fib
    if result.fib and result.swings:
        a, b = result.swings[-2], result.swings[-1]
        if b.index >= start:
            x0, x1 = rel(a.index), m - 1
            for lvl in result.fib:
                ax.hlines(lvl.price, x0, x1, colors="#999999",
                          linestyles="--", linewidth=0.5, alpha=0.6, zorder=1)
                ax.text(x1 - 0.5, lvl.price, f"{lvl.ratio:.3f}", fontsize=6,
                        color="#777777", va="center", ha="right", clip_on=True)

    # killzones
    for name, spans in result.killzones.items():
        for (a_i, b_i) in spans:
            if b_i < start:
                continue
            ax.axvspan(max(rel(a_i), 0), min(rel(b_i), m - 1), color="#999999", alpha=0.08, zorder=0)

    ax.set_title(
        f"{title}  |  bars={m}  MSS/BOS={len(shown_events)}  "
        f"FVG={len(_latest_by_direction(result.fvgs, cfg.max_fvg, ('bull', 'bear')))}  "
        f"OB={len(active_obs)}  CISD={len(result.cisds)}  "
        f"HTF={len(result.htf_cisds)}  SMT={len(result.smts)}  "
        f"SWEEP={len(result.sweeps)}  KL={len(result.key_levels)}"
    )
    ax.set_ylabel("Price")
    ax.grid(alpha=0.15)
    ax.set_xlim(-1, m)
    ticks = np.linspace(0, m - 1, min(10, m)).astype(int)
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates.iloc[i].strftime("%Y-%m-%d") for i in ticks], rotation=30, ha="right")
    fig.tight_layout()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Optional Futu adapter
# ---------------------------------------------------------------------------
def fetch_futu(code: str, start: str, end: str, ktype: str = "K_DAY") -> pd.DataFrame:
    """Fetch qfq bars from Futu OpenD.

    Requires: pip install futu-api, and OpenD running on 127.0.0.1:11111.
    """
    from futu import AuType, KLType, OpenQuoteContext  # lazy import

    kt = getattr(KLType, ktype)
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data, page_key = ctx.request_history_kline(
            code=code, start=start, end=end, ktype=kt,
            autype=AuType.QFQ, max_count=1000,
        )
        if ret != 0:
            raise RuntimeError(data)
        frames = [data]
        while page_key:
            ret, data, page_key = ctx.request_history_kline(
                code=code, start=start, end=end, ktype=kt,
                autype=AuType.QFQ, max_count=1000, page_req_key=page_key,
            )
            if ret != 0:
                raise RuntimeError(data)
            frames.append(data)
        df = pd.concat(frames, ignore_index=True)
        df = df.rename(columns={
            "time_key": "date", "open": "open", "close": "close",
            "high": "high", "low": "low", "volume": "volume",
        })
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    finally:
        ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="ICT Concepts in Python")
    ap.add_argument("--csv", default="output/csi300/csv/SH600519.csv")
    ap.add_argument("--symbol", default="SH600519")
    ap.add_argument("--out", default="output/ict/ict_chart.png")
    ap.add_argument("--signals", default="output/ict/ict_signals.csv")
    ap.add_argument("--bars", type=int, default=260)
    ap.add_argument("--swing-len", type=int, default=5)
    ap.add_argument("--max-events", type=int, default=4)
    ap.add_argument("--max-ob", type=int, default=1)
    ap.add_argument("--mode", choices=["present", "historical"], default="present")
    ap.add_argument("--no-ob", action="store_true")
    ap.add_argument("--no-fvg", action="store_true")
    ap.add_argument("--no-cisd", action="store_true")
    ap.add_argument("--no-htf", action="store_true")
    ap.add_argument("--no-sessions", action="store_true")
    ap.add_argument("--no-sweeps", action="store_true")
    ap.add_argument("--sweep-window", type=int, default=15)
    ap.add_argument("--sweep-penetration", type=float, default=0.0)
    ap.add_argument("--fvg-min-atr", type=float, default=0.0)
    ap.add_argument("--fvg-min-body", type=float, default=0.0)
    ap.add_argument("--smt-csv", help="correlated asset CSV for SMT")
    ap.add_argument("--smt-code", help="Futu code for correlated asset, e.g. SH.000300")
    ap.add_argument("--smt-sensitivity", choices=["Sensitive", "Normal", "Strict"], default="Normal")
    ap.add_argument("--min-grade", choices=["C", "B-", "B+", "A", "A+"], default="C")
    ap.add_argument("--cisd-max", type=int, default=2)
    ap.add_argument("--min-displacement", type=float, default=0.0,
                    help="minimum close-through distance in ATR units")
    ap.add_argument("--cisd-allow-no-origin", action="store_true")
    ap.add_argument("--bpr", action="store_true")
    ap.add_argument("--futu-code", help="e.g. HK.00700; fetch via OpenD instead of CSV")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    if args.futu_code:
        df = fetch_futu(args.futu_code, args.start, args.end)
        symbol = args.futu_code
    else:
        df = load_csv(args.csv)
        symbol = args.symbol

    smt_df = None
    if args.smt_csv:
        smt_df = load_csv(args.smt_csv)
    elif args.smt_code:
        smt_df = fetch_futu(args.smt_code, args.start, args.end)

    cfg = ICTConfig(
        mode=args.mode,
        swing_len=args.swing_len,
        max_events=args.max_events,
        max_ob=args.max_ob,
        show_ob=not args.no_ob,
        show_fvg=not args.no_fvg,
        show_cisd=not args.no_cisd,
        cisd_min_grade={"C": 3, "B-": 4, "B+": 5, "A": 6, "A+": 7}[args.min_grade],
        cisd_max=args.cisd_max,
        cisd_require_origin=not args.cisd_allow_no_origin,
        cisd_min_displacement_atr=args.min_displacement,
        show_htf_cisd=not args.no_htf,
        show_sessions=not args.no_sessions,
        show_sweeps=not args.no_sweeps,
        sweep_window=args.sweep_window,
        sweep_min_penetration_atr=args.sweep_penetration,
        fvg_min_atr=args.fvg_min_atr,
        fvg_min_body=args.fvg_min_body,
        show_smt=bool(args.smt_csv or args.smt_code),
        max_smt=3,
        show_bpr=args.bpr,
        plot_bars=args.bars,
    )
    df.attrs["cfg"] = cfg
    result = analyze(df, cfg, smt_df=smt_df)
    path = plot(result, args.out, title=f"ICT Concepts - {symbol}", bars=args.bars)
    sig = signals_frame(result)
    Path(args.signals).parent.mkdir(parents=True, exist_ok=True)
    sig.to_csv(args.signals, index=False, encoding="utf-8-sig")
    print(f"chart: {path}")
    print(f"signals: {args.signals} ({len(sig)} rows)")
    print(f"swings={len(result.swings)} events={len(result.events)} "
          f"fvg={len(result.fvgs)} ob={len(result.order_blocks)} liq={len(result.liquidity)} "
          f"gaps={len(result.gaps)} cisd={len(result.cisds)} "
          f"htf={len(result.htf_cisds)} smt={len(result.smts)}")
    if len(sig):
        print(sig.tail(8).to_string(index=False))


if __name__ == "__main__":
    main()

