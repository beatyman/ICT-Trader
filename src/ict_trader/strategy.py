"""ICT/CISD strategy + event-driven backtest.

Signal:
  * a confirmed CISD on the base timeframe
  * optional HTF CISD alignment
  * minimum grade and minimum confluence

Execution:
  * enter at the next bar open by default, or optionally wait for a retest of
    the CISD key level (``entry_mode="retest_key"``)
  * the declared window gate applies to the **fill bar**, not only to the
    signal bar: a fill outside the window is refused with
    ``NT_FILL_OUTSIDE_WINDOW`` (P0-13, EN-04/§8.2)
  * stop at the CISD invalidation level
  * target supplied by the setup's declared ``target_mode``; a signal with no
    target, or a target below the RR floor at the actual fill, is refused rather
    than given an RR-derived substitute (TG-01, TG-07)
  * conservative same-bar rule: if both SL and TP are touched, SL wins
  * fixed-fractional risk, commission and slippage
  * counters for skipped / not-triggered signals (execution audit)

The module is deliberately data-source agnostic: feed it a CSV or a Futu frame.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from .concepts import ICTConfig, ICTResult, analyze, fetch_futu, load_csv
from .config import config_hash as _config_hash
from .context import (PO3PeriodContract, in_session, resolve_model_identity)
from .stats import mean_interval, n_for_margin, wilson_interval
from .variants import VariantConfig


#: §12 refusal codes recorded by the plan gate (A3).  A plan without a target
#: violates TG-01, and a target nearer than the declared floor is
#: ``NT_TARGET_TOO_NEAR`` (TG-07/TG-11).  Neither is ever replaced by an
#: RR-derived price (P0-05).
TARGET_MISSING_CODE = "NT_DATA_MISSING"
TARGET_TOO_NEAR_CODE = "NT_TARGET_TOO_NEAR"


# --------------------------------------------------------------------------- #
# Declared accounting register (P0-18, A4/A7)
# --------------------------------------------------------------------------- #
#: One register entry per accounting parameter.  Every surface -- the backtest,
#: the setup gate, the realtime settings, the paper broker and the risk plan --
#: reads these values; no module keeps a private copy.  ``label`` is the
#: provenance label required by §14.1-6: these are ``repo`` choices, not corpus
#: doctrine.
@dataclass(frozen=True, slots=True)
class RegisterEntry:
    name: str
    value: float | int | str
    label: str
    source: str


ACCOUNTING_REGISTER: dict[str, RegisterEntry] = {
    "risk_pct": RegisterEntry("risk_pct", 0.005, "repo",
                              "RK-07 / spec §13.8"),
    "commission_bps": RegisterEntry("commission_bps", 10.0, "repo",
                                    "GS-08 / RM R9 (one round trip)"),
    "slippage_bps": RegisterEntry("slippage_bps", 2.0, "repo",
                                  "GS-08 / RM R9 (per side)"),
    "sell_tax_bps": RegisterEntry("sell_tax_bps", 0.0, "repo",
                                  "GS-08 / RM R9 (exit only)"),
    "max_trades_per_day": RegisterEntry("max_trades_per_day", 4, "repo",
                                        "RE:RM-19 (unit = fills/day)"),
    "rr_mode": RegisterEntry("rr_mode", "filter_2_1", "repo",
                             "RE:RM-15 / V28"),
    "net_rr_floor": RegisterEntry("net_rr_floor", 2.0, "repo",
                                  "RE:RM-16 / GS-08"),
    # P1-13 risk algebra inputs.  ``value_per_unit`` is the I3 instrument
    # quantity sizing needs; 1.0 is the declared repo value for a USDT-quoted
    # spot pair (one quote unit per base unit per price unit).  ``lot_step`` is
    # RK-14's *missing* input: the repo declares the no-rounding step ``0`` so
    # an instrument-specific step must be supplied instead of guessed.
    "value_per_unit": RegisterEntry(
        "value_per_unit", 1.0, "repo",
        "I3 / RE:RM-13 (USDT-quoted spot)"),
    "lot_step": RegisterEntry(
        "lot_step", 0.0, "repo",
        "RK-14 input (corpus: missing); 0 = no rounding"),
}

#: P1-13 risk-algebra declarations that are not accounting floats.  They live
#: in a second labelled register so the P0-18 accounting register keeps its
#: invariant that every member is a repo accounting value.
RISK_ALGEBRA_REGISTER: dict[str, RegisterEntry] = {
    "weighted_rr_floor": RegisterEntry(
        "weighted_rr_floor", 1.5, "corpus (single)", "RE:TS-16"),
    "min_distance_floor": RegisterEntry(
        "min_distance_floor", 3.0, "corpus (single)",
        "RE:TS-19 / RE:C-20 (rr_3_1 form)"),
}


def declared(name: str) -> RegisterEntry:
    """Return the one register entry for ``name`` (KeyError if undeclared)."""
    return ACCOUNTING_REGISTER[name]


DEFAULT_RISK_PCT = float(declared("risk_pct").value)
DEFAULT_COMMISSION_BPS = float(declared("commission_bps").value)
DEFAULT_SLIPPAGE_BPS = float(declared("slippage_bps").value)
DEFAULT_SELL_TAX_BPS = float(declared("sell_tax_bps").value)
DEFAULT_MAX_TRADES_PER_DAY = int(declared("max_trades_per_day").value)
DEFAULT_RR_MODE = str(declared("rr_mode").value)
NET_RR_FLOOR = float(declared("net_rr_floor").value)

#: One floor per declared ``RR_MODE`` (RE:RM-15 / V28).  These are the only
#: admissible gross-RR floors: no gate may carry a private 1.2 / 2.0 / 2.5.
RR_MODE_FLOORS: dict[str, float] = {
    "floor_3_1": 3.0,
    "floor_session_1_5": 1.5,
    "filter_2_1": 2.0,
    "none": 0.0,
}


def rr_floor(mode: str = DEFAULT_RR_MODE) -> float:
    """The declared gross-RR floor for ``mode`` (ValueError if undeclared)."""
    try:
        return RR_MODE_FLOORS[mode]
    except KeyError as exc:
        raise ValueError(
            f"unknown RR mode {mode!r}; declare one of {tuple(RR_MODE_FLOORS)}"
        ) from exc


MIN_RR = rr_floor(DEFAULT_RR_MODE)

#: The declared gross-RR mode (V28).  ``DEFAULT_RR_MODE`` is the repo's
#: selection from the four attested modes; ``RR_MODE`` is the alias the risk
#: algebra surfaces read.
RR_MODE = DEFAULT_RR_MODE
RR_MODE_VARIANT = "V28"
RR_MODE_DEFAULT_LABEL = "repo"

#: Provenance of each V28 option.  The options are corpus attestations; the
#: *selection* of ``filter_2_1`` as the repo default is labelled ``repo``.
RR_MODE_SOURCES: dict[str, dict[str, str]] = {
    "floor_3_1": {"label": "corpus", "source": "[RE:RM-15] >=3:1"},
    "floor_session_1_5": {
        "label": "corpus", "source": "[RE:RM-15] 1.5:1 session"},
    "filter_2_1": {"label": "corpus", "source": "[RE:RM-15] >=2:1 filter"},
    "none": {"label": "corpus", "source": "[RE:RM-15] ratio is an output"},
}


def declared_rr_mode(mode: str = DEFAULT_RR_MODE) -> str:
    """Validate and return one declared V28 mode (never a private floor)."""
    if mode not in RR_MODE_FLOORS:
        raise ValueError(
            f"unknown rr_mode {mode!r}; declare one of {tuple(RR_MODE_FLOORS)}")
    return str(mode)


#: Sizing inputs (P1-13).  ``value_per_unit`` is the I3 instrument quantity;
#: ``lot_step`` is RK-14's missing lot step, declared as the repo no-rounding
#: step ``0``.  Both are read from the accounting register, never re-typed.
DEFAULT_VALUE_PER_UNIT = float(declared("value_per_unit").value)
DEFAULT_LOT_STEP = float(declared("lot_step").value)
LOT_STEP_POLICIES = ("down",)
DEFAULT_LOT_STEP_POLICY = "down"

#: Weighted-RR floor and the one attested form of the minimum-distance floor
#: (TG-11 lists four incompatible forms; exactly one is declared).
WEIGHTED_RR_FLOOR = float(RISK_ALGEBRA_REGISTER["weighted_rr_floor"].value)
MIN_DISTANCE_FLOOR_R = float(
    RISK_ALGEBRA_REGISTER["min_distance_floor"].value)


@dataclass(frozen=True, slots=True)
class RiskBand:
    """One attested risk-per-trade band (V30 / RK-07).

    ``lower_pct``/``upper_pct`` are fractions of equity.  ``source_id`` is the
    attestation the band comes from and must travel with the selected band, so
    a sizing surface cannot report a bare percentage without provenance.
    """

    band_id: str
    lower_pct: float
    upper_pct: float
    attested: str
    source_id: str
    label: str = "corpus"
    variant_id: str = "V30"

    def __post_init__(self) -> None:
        if not self.band_id.strip():
            raise ValueError("risk band id must not be empty")
        if self.lower_pct < 0 or self.upper_pct < self.lower_pct:
            raise ValueError(
                f"{self.band_id}: invalid band [{self.lower_pct}, "
                f"{self.upper_pct}]")
        if not self.source_id.strip():
            raise ValueError(f"{self.band_id}: a source_id is required (V30)")
        if not self.attested.strip():
            raise ValueError(f"{self.band_id}: the attestation text is empty")

    def contains(self, risk_pct: float) -> bool:
        return self.lower_pct <= float(risk_pct) <= self.upper_pct

    def require(self, risk_pct: float) -> float:
        """Return ``risk_pct`` if the declared band contains it, else refuse."""
        pct = float(risk_pct)
        if not self.contains(pct):
            raise ValueError(
                f"risk_pct {pct:g} is outside the declared risk band "
                f"{self.band_id} [{self.lower_pct:g}, {self.upper_pct:g}] "
                f"({self.source_id})")
        return pct


#: The attested RK-07 bands.  They intersect only at 1% (RE:C-02); selecting
#: one is V30, not an average of them.
RISK_BANDS: Mapping[str, RiskBand] = MappingProxyType({
    "band_025_150": RiskBand(
        "band_025_150", 0.0025, 0.015,
        "0.25-1.5% per trade", "[RE:RM-12; RE:C-02]"),
    "band_050_100_cap_150": RiskBand(
        "band_050_100_cap_150", 0.005, 0.010,
        "0.5-1.0% with a 1.5% maximum", "[RE:RM-12; RE:C-02]"),
    "band_cap_100": RiskBand(
        "band_cap_100", 0.0, 0.010,
        "<=1% per trade", "[RE:RM-12; RE:C-02]"),
    "band_100_200": RiskBand(
        "band_100_200", 0.010, 0.020,
        "1-2% per trade", "[RE:RM-12; RE:C-02]"),
})

#: The repo's declared V30 selection.  The default ``risk_pct`` (0.5%) lies on
#: its lower bound; the value itself is the labelled repo choice RE:RM-12.
DEFAULT_RISK_BAND_ID = "band_050_100_cap_150"
DEFAULT_RISK_BAND = RISK_BANDS[DEFAULT_RISK_BAND_ID]
RISK_BAND = DEFAULT_RISK_BAND
DECLARED_RISK_BAND = DEFAULT_RISK_BAND


def validate_risk_pct(risk_pct: float,
                      band: RiskBand = DEFAULT_RISK_BAND) -> float:
    """Fail closed when a sizing surface leaves the declared risk band."""
    return band.require(risk_pct)


@dataclass(frozen=True, slots=True)
class MinimumDistanceFloor:
    """The one declared target minimum-distance floor (TG-11).

    TG-11 names four *incompatible* forms (``~1:2`` skip, ``0.7R`` reject,
    ``<1.5R`` DOL reject, ``>=3:1`` to the nearest array).  A run must declare
    one; the repo selects the ``>=3:1`` form from ``[RE:TS-19; RE:C-20]``.
    ``value`` is an R multiple: ``reward / risk``.
    """

    mode: str
    value: float
    label: str
    source_id: str
    variant_id: str = "TG-11"

    def __post_init__(self) -> None:
        if self.mode not in MIN_DISTANCE_FORMS:
            raise ValueError(
                f"unknown minimum-distance form {self.mode!r}; declare one of "
                f"{tuple(MIN_DISTANCE_FORMS)}")
        expected = MIN_DISTANCE_FORMS[self.mode]
        if abs(float(self.value) - expected) > 1e-12:
            raise ValueError(
                f"{self.mode}: declared value {self.value} contradicts the "
                f"attested R multiple {expected}")
        if not self.source_id.strip():
            raise ValueError(f"{self.mode}: a source_id is required")
        if not self.label.strip():
            raise ValueError(f"{self.mode}: a provenance label is required")

    def admits(self, reward: float, risk: float) -> bool:
        """True when the target is at or beyond the declared R multiple."""
        if float(risk) <= 0:
            return False
        return float(reward) / float(risk) >= self.value


#: The four TG-11 forms, keyed by the declared name and valued in R.
MIN_DISTANCE_FORMS: Mapping[str, float] = MappingProxyType({
    "skip_below_rr_2_1": 2.0,
    "reject_below_0_7r": 0.7,
    "reject_below_1_5r": 1.5,
    "rr_3_1_to_nearest_array": 3.0,
})

#: The repo's one declared TG-11 form.  No run may carry a private floor.
DEFAULT_MIN_DISTANCE_FLOOR = MinimumDistanceFloor(
    "rr_3_1_to_nearest_array", MIN_DISTANCE_FLOOR_R, "corpus (single)",
    "[RE:TS-19; RE:C-20]")
MINIMUM_DISTANCE_FLOOR = DEFAULT_MIN_DISTANCE_FLOOR


#: Declared fixed-R target multiple (a target size, not an RR gate).
DEFAULT_FIXED_R = 2.0

#: Declared same-bar convention.  No corpus rule fixes an intrabar path, so the
#: repo declares one: when a bar touches both the stop and the target, the stop
#: is assumed to have been hit first.  It is conservative and, more
#: importantly, identical in the backtest and the paper broker (P0-18).
SAME_BAR_TIE_BREAK = "stop_first"


# --------------------------------------------------------------------------- #
# Declared execution enums (P0-19: no implicit choice may decide a trade)
# --------------------------------------------------------------------------- #
#: RE:EX-03: the corpus states the entry-price *menu* and ranks none of it.
#: ``next_open`` is the repo default and is **not** one of the four attested
#: prices; it stays available but is labelled below instead of running as an
#: undocumented market entry.
ENTRY_MODES = ("next_open", "retest_key", "fvg_ce", "ob_me", "ote")
DEFAULT_ENTRY_MODE = "next_open"

#: Provenance of each declared entry mode (``label`` is the §14.1-6 label).
ENTRY_MODE_SOURCES: dict[str, dict[str, str]] = {
    "next_open": {
        "label": "repo",
        "source": "RE:EX-03 has no next-open member; repo market fill"},
    "retest_key": {
        "label": "repo",
        "source": "key-level retest is a repo selector, not the entry menu"},
    "fvg_ce": {
        "label": "corpus",
        "source": "RE:EX-03 entry menu: FVG CE"},
    "ob_me": {
        "label": "repo",
        "source": "OB body mean is the repo OB construction"},
    "ote": {
        "label": "repo",
        "source": "PP:OTE-05 0.705 preference; the OTE band is not the menu"},
}

#: MG-08 / V31: break-even triggers are mutually exclusive and one must be
#: declared.  The repo implements three of the attestations; a bare
#: ``breakeven_at_r > 0`` float no longer encodes ``never`` implicitly.
BE_MODES = ("never", "at_r", "after_first_partial")
DEFAULT_BE_MODE = "never"

#: MG-01 / V31: the first-partial coordinate system.  ``level_anchored`` is the
#: best-attested corpus ladder but needs the first-IRL object (P1-09/P1-15), so
#: the P0 subset declares the two implementable modes and their default.
LADDER_MODES = ("off", "r_anchored", "level_anchored")
DEFAULT_LADDER_MODE = "off"


def resolve_ladder_mode(t1_at_r: float, t1_at_eq: bool) -> str:
    """Name the ladder mode implied by the numeric settings (MG-01/V31).

    Exactly one coordinate system may be active: ``t1_at_eq`` (the repo EQ
    variant) and a positive ``t1_at_r`` cannot both select a first leg.
    """
    if bool(t1_at_eq) and float(t1_at_r) > 0:
        raise ValueError(
            "two first-partial coordinate systems are enabled (t1_at_eq and "
            "t1_at_r); declare exactly one ladder mode (MG-01/V31)")
    if bool(t1_at_eq):
        return "level_anchored"
    if float(t1_at_r) > 0:
        return "r_anchored"
    return "off"


def resolve_be_mode(breakeven_at_r: float, t1_move_be: bool,
                    partial_enabled: bool) -> str:
    """Name the break-even trigger implied by the numeric settings (MG-08).

    ``never`` is a declared mode, not "the float happens to be zero".  The two
    implemented triggers are mutually exclusive; asking for both fails closed
    instead of silently running two BE rules on one trade.
    """
    at_r = float(breakeven_at_r) > 0
    after_partial = bool(t1_move_be) and bool(partial_enabled)
    if at_r and after_partial:
        raise ValueError(
            "two break-even triggers are enabled (breakeven_at_r > 0 and "
            "t1_move_be with a live first partial); declare one BE mode "
            "(MG-08/V31)")
    if at_r:
        return "at_r"
    if after_partial:
        return "after_first_partial"
    return "never"


def _declared_choice(value: str | None, allowed: tuple[str, ...],
                     name: str, implied: str) -> str:
    """Validate an optional declared enum against the behaviour it names."""
    if value is None:
        return implied
    declared = str(value).strip().lower()
    if declared not in allowed:
        raise ValueError(
            f"unknown {name} {value!r}; declare one of {allowed}")
    if declared != implied:
        raise ValueError(
            f"declared {name} {declared!r} contradicts the configured "
            f"behaviour {implied!r}; change the fields or the declaration")
    return declared


def _direction_sign(direction: str) -> int:
    if direction in {"long", "bull"}:
        return 1
    if direction in {"short", "bear"}:
        return -1
    raise ValueError(f"unknown direction {direction!r}")


@dataclass(frozen=True, slots=True)
class CostModel:
    """The one execution-cost basis in the package (P0-18, GS-08, RM R9).

    ``commission_bps`` is **one round trip** (so each leg pays half),
    ``slippage_bps`` is per side, and ``sell_tax_bps`` applies to the exit
    notional only.  ``net_rr`` is the documented repo formula::

        net = (reward - cost) / (risk + cost)
        cost = entry * per_leg + exit * (per_leg + sell_tax)
        per_leg = commission_bps / 2 + slippage_bps

    ``for_filled_prices`` is the variant used by a simulator whose fill price
    already embeds slippage; it charges commission and tax only, so slippage is
    not counted twice.  It is still the same declared basis, not a second one.
    """

    commission_bps: float = DEFAULT_COMMISSION_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    sell_tax_bps: float = DEFAULT_SELL_TAX_BPS

    def __post_init__(self) -> None:
        for name in ("commission_bps", "slippage_bps", "sell_tax_bps"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")

    @property
    def per_leg_bps(self) -> float:
        return float(self.commission_bps) / 2.0 + float(self.slippage_bps)

    def cost(self, entry: float, exit_price: float) -> float:
        """Round-trip cost in price units for one unit of the instrument."""
        per_leg = self.per_leg_bps
        return (abs(float(entry)) * per_leg / 10_000.0
                + abs(float(exit_price))
                * (per_leg + float(self.sell_tax_bps)) / 10_000.0)

    def sell_tax(self, exit_price: float) -> float:
        return abs(float(exit_price)) * float(self.sell_tax_bps) / 10_000.0

    def gross_rr(self, entry: float, stop: float, target: float,
                 direction: str) -> float:
        """Corpus RR (RM-14): ``reward / risk``, direction-invariant."""
        _direction_sign(direction)
        risk = abs(float(entry) - float(stop))
        if risk <= 0:
            return 0.0
        return abs(float(target) - float(entry)) / risk

    def net_rr(self, entry: float, stop: float, target: float,
               direction: str) -> tuple[float, float]:
        """Return ``(gross_rr, net_rr)`` from this one cost basis."""
        _direction_sign(direction)
        risk = abs(float(entry) - float(stop))
        reward = abs(float(target) - float(entry))
        if risk <= 0:
            return 0.0, 0.0
        cost = self.cost(entry, target)
        return reward / risk, (reward - cost) / (risk + cost)

    def realized_r(self, entry: float, stop: float, exit_price: float,
                   direction: str) -> float:
        """Realised net PnL divided by the initial risk (the backtest's R)."""
        sign = _direction_sign(direction)
        risk = abs(float(entry) - float(stop))
        if risk <= 0:
            return 0.0
        gross = (float(exit_price) - float(entry)) * sign
        return (gross - self.cost(entry, exit_price)) / risk

    def for_filled_prices(self) -> "CostModel":
        """Same basis for a fill that already embeds per-side slippage."""
        return CostModel(self.commission_bps, 0.0, self.sell_tax_bps)


#: The one default cost basis shared by every surface.
DEFAULT_COST_MODEL = CostModel()


def net_rr(entry: float, stop: float, target: float, direction: str,
           commission_bps: float = DEFAULT_COMMISSION_BPS,
           slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
           sell_tax_bps: float = DEFAULT_SELL_TAX_BPS) -> tuple[float, float]:
    """Return (gross_RR, net_RR); the one implementation in the package.

    Formula (``docs/GOOD_SETUP_CRITERIA.md`` GS-08)::

        net = (reward - cost) / (risk + cost)
        cost = entry * per_leg + target * (per_leg + sell_tax)
        per_leg = commission_bps / 2 + slippage_bps

    * ``commission_bps`` is ONE round trip, so each leg pays half
      (Binance futures taker 0.05%/side -> pass 10 -> 5 bps per leg);
    * ``slippage_bps`` is per side, so each leg pays all of it;
    * the cost is added to the risk denominator as well, because a stop-out
      costs ``risk + cost``, not ``risk``.

    Before this was unified there were four copies in the tree with two
    different formulas: ``rules._plan_net_rr`` / ``scenarios._net_rr`` /
    ``scalper._net_rr`` used the documented ``(reward-cost)/(risk+cost)`` with a
    hardcoded 7 bps per leg, while ``strategy.net_rr`` divided by ``risk`` only
    and charged ``commission_bps`` on both legs.  The effect was user-visible:
    the plan card printed net RR 1.60 while the GS-08 gate on the same plan
    said 1.09.
    """
    return CostModel(commission_bps, slippage_bps,
                     sell_tax_bps).net_rr(entry, stop, target, direction)


def same_bar_exit(direction: str, high: float, low: float, stop: float,
                  target: float) -> str | None:
    """Resolve one bar against stop and target under the declared convention.

    Returns ``"stop"``, ``"target"`` or ``None``.  When both are touched the
    stop wins (``SAME_BAR_TIE_BREAK``), so the backtest simulator and the paper
    broker cannot disagree about the same bar.  Accepts both the backtest's
    ``bull``/``bear`` and the broker's ``long``/``short`` vocabulary.
    """
    sign = _direction_sign(direction)
    if sign > 0:
        hit_stop = float(low) <= float(stop)
        hit_target = float(high) >= float(target)
    else:
        hit_stop = float(high) >= float(stop)
        hit_target = float(low) <= float(target)
    if hit_stop:
        return "stop"
    if hit_target:
        return "target"
    return None

#: §12 refusal code for a fill bar outside the declared window (P0-13,
#: EN-04/§8.2).  The signal bar passing the window gate is not enough when the
#: entry fills on a later bar: a fill after the window has closed is not that
#: model's trade.  Defined here (and re-exported by ``setup``) so the code has
#: one spelling, matching the plan-gate codes above.
NT_FILL_OUTSIDE_WINDOW = "NT_FILL_OUTSIDE_WINDOW"


@dataclass
class StrategyConfig:
    #: One declared fixed-fractional risk budget, shared with the setup and the
    #: realtime settings (P0-18, RK-07).
    risk_pct: float = DEFAULT_RISK_PCT  # fraction of equity risked per trade
    #: Retained for API compatibility.  Targets are authored by setup's declared
    #: ``target_mode``; the plan gate never manufactures one from this value.
    rr: float = DEFAULT_FIXED_R
    max_bars: int = 40              # time stop
    min_grade: int = 4              # 3=C, 4=B-, 5=B+, 6=A, 7=A+
    min_confluence: int = 3         # sweep / PDA / volume / session / target / SMT
    require_htf: bool = False       # require HTF CISD same direction
    t_plus_1: bool = False          # A-share T+1: cannot exit on entry day
    sell_tax_bps: float = DEFAULT_SELL_TAX_BPS  # A-share stamp duty on sell
    allow_long: bool = True
    allow_short: bool = True
    # Charged as (entry+exit)*commission_bps/2, i.e. ONE round-trip of
    # commission_bps, so a venue that charges X bps PER SIDE must be passed
    # 2*X here (Binance futures taker 5 bps/side -> commission_bps=10).
    commission_bps: float = DEFAULT_COMMISSION_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    entry_delay: int = 1            # bars after confirmation
    #: Declared daily budget and unit (fills per calendar date).  One value for
    #: every surface (P0-18, RE:RM-19).
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    max_concurrent: int = 5
    #: Declared gross-RR gate mode (RE:RM-15 / V28).  ``min_rr`` is an explicit
    #: numeric override; ``None`` reads the floor of ``rr_mode`` so no surface
    #: carries a private 1.2 / 2.0 / 2.5.
    rr_mode: str = DEFAULT_RR_MODE
    min_rr: float | None = None
    breakeven_at_r: float = 0.0     # move SL to entry at this favourable R
    #: Declared break-even trigger (MG-08/V31).  ``None`` reads the one implied
    #: by ``breakeven_at_r`` / ``t1_move_be``; an explicit value must agree with
    #: those fields.  ``never`` is a mode, never an implicit zero.
    be_mode: str | None = None
    min_displacement_atr: float = 0.0
    #: Declared entry-price member (RE:EX-03).  ``next_open`` is the labelled
    #: repo default; it is not one of the four attested menu prices.
    entry_mode: str = DEFAULT_ENTRY_MODE
    retest_max_bars: int = 20       # bars allowed for a retest to fill
    retest_tolerance_atr: float = 0.0  # allow a wick this close to the key
    trail_activate_r: float = 0.0     # activate ATR trail after this favourable R
    trail_atr_mult: float = 0.0       # trail distance in entry-bar ATR
    # Partial scale-out.  Take ``t1_fraction`` of the position off at
    # ``t1_at_r`` R, optionally pull the runner's stop to break-even.  This is
    # the only mechanism that can lift the win rate without giving up the RR of
    # the runner: the first leg is a booked win, the second leg keeps the 2.5R
    # target alive.  ``r_multiple`` becomes the size-weighted blend of both legs.
    t1_at_r: float = 0.0
    t1_fraction: float = 0.5
    t1_move_be: bool = True
    # Doc 631 L76: "For longs from discount, take 30-50% at equilibrium. For
    # shorts from premium, same."  When enabled, the first leg is taken at the
    # setup's dealing-range EQ (`quality_parts["dr_eq"]`) instead of at t1_at_r R.
    t1_at_eq: bool = False
    #: Declared first-partial coordinate system (MG-01/V31).  ``off`` is the
    #: labelled repo default (the corpus ladder needs the first-IRL object,
    #: P1-09/P1-15); ``None`` reads the one implied by the numeric fields.
    ladder_mode: str | None = None
    max_daily_loss_r: float = 0.0     # stop new entries after this daily R loss (0=off)
    # P0-13: the window gate must also cover the bar the order actually fills
    # on.  These mirror ``SetupConfig.session_gate``/``market`` so
    # ``setup.run_one`` can forward the declaration and a direct simulator
    # caller can declare it too.  ``none`` always passes and is the default.
    session_gate: str = "none"
    market: str = "other"
    #: The run's declared variants (P1-01, §14.1-3/A7).  Empty by design: an
    #: undeclared fork is never guessed by the simulator; a read refuses.
    variant_config: VariantConfig = field(default_factory=VariantConfig)
    #: P1-11: the same model/period identity the setup surface declares -- the
    #: PO3 six-field period contract (GW-02), the narrative order (§3.4) and
    #: one execution-model family (RE:EX-18).  All three are declared together
    #: or none is; the simulator never infers a family from the CISD path.
    po3_period: PO3PeriodContract | dict[str, object] | None = None
    narrative_order: str | None = None
    execution_model_id: str | None = None
    #: Refuse to simulate an unspecified model.  Off is the pre-P1-11
    #: compatibility path: the trade carries the empty identity, which is
    #: explicitly ``undeclared`` rather than a defaulted family.
    model_identity_gate: bool = False

    def read_variant(self, variant_id: str) -> str:
        """Read one declared fork through the single register (A7)."""
        return self.variant_config.read(variant_id)

    @property
    def config_hash(self) -> str:
        """The declared-variant configuration hash (SYSTEM_SPEC A2)."""
        extra: dict[str, object] = {}
        # P1-11: the declared identity is part of the digest; nothing is hashed
        # by implication and an undeclared identity adds nothing.
        if self.po3_period is not None:
            extra["po3_period"] = self.po3_period.to_public()
        if self.narrative_order is not None:
            extra["narrative_order"] = self.narrative_order
        if self.execution_model_id is not None:
            extra["execution_model_id"] = self.execution_model_id
        if self.model_identity_gate:
            extra["model_identity_gate"] = True
        return _config_hash(
            self.variant_config,
            be_mode=self.declared_be_mode(),
            entry_mode=self.entry_mode,
            ladder_mode=self.declared_ladder_mode(),
            rr_mode=self.rr_mode,
            **extra,
        )

    def __post_init__(self) -> None:
        if self.rr_mode not in RR_MODE_FLOORS:
            raise ValueError(
                f"unknown rr_mode {self.rr_mode!r}; "
                f"declare one of {tuple(RR_MODE_FLOORS)}")
        # V30: a sizing surface may only run inside its declared risk band.
        validate_risk_pct(self.risk_pct)
        if self.min_rr is not None and float(self.min_rr) < 0:
            raise ValueError("min_rr must be >= 0")
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(
                f"unknown entry_mode {self.entry_mode!r}; "
                f"declare one of {ENTRY_MODES} (RE:EX-03)")
        # Validate the two declared management enums and their behaviour at
        # construction, so an undeclared or contradictory choice cannot reach a
        # simulated fill (P0-19; MG-01/MG-08/V31).
        self.declared_ladder_mode()
        self.declared_be_mode()
        # P1-11: one model/period identity, declared together or not at all.
        # The simulator may only report a trade under a family the run named;
        # ``model_identity_gate`` refuses the empty identity instead of falling
        # back to the legacy CISD path as if it were a declared model.
        identity = resolve_model_identity(
            period=self.po3_period,
            narrative_order=self.narrative_order,
            execution_model_id=self.execution_model_id,
            required=self.model_identity_gate,
        )
        object.__setattr__(self, "po3_period", identity.period)
        object.__setattr__(
            self, "narrative_order",
            None if identity.narrative_order is None
            else identity.narrative_order.order.value)
        object.__setattr__(
            self, "execution_model_id",
            None if identity.execution_model is None
            else identity.execution_model.model_id)

    def rr_floor(self) -> float:
        """The declared gate floor: explicit ``min_rr`` else the mode's floor."""
        if self.min_rr is not None:
            return float(self.min_rr)
        return rr_floor(self.rr_mode)

    def declared_ladder_mode(self) -> str:
        """The declared first-partial coordinate system (MG-01/V31)."""
        implied = resolve_ladder_mode(self.t1_at_r, self.t1_at_eq)
        return _declared_choice(self.ladder_mode, LADDER_MODES,
                                "ladder_mode", implied)

    def declared_be_mode(self) -> str:
        """The one declared break-even trigger (MG-08/V31)."""
        implied = resolve_be_mode(
            self.breakeven_at_r, self.t1_move_be,
            self.declared_ladder_mode() != "off")
        return _declared_choice(self.be_mode, BE_MODES, "be_mode", implied)


@dataclass
class Trade:
    symbol: str
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    sl: float
    tp: float
    r_multiple: float
    reason: str
    grade: int
    confluence: int
    bars: int
    checks: dict[str, bool] = field(default_factory=dict)
    mae_r: float = 0.0
    mfe_r: float = 0.0
    time_to_mae: int = 0
    time_to_mfe: int = 0
    be_triggered: bool = False
    t1_locked: bool = False
    t1_filled: bool = False          # partial scale-out leg was taken
    t1_price: float | None = None
    sell_tax: float = 0.0
    planned_entry: float | None = None
    planned_sl: float | None = None
    planned_tp: float | None = None
    entry_deviation_r: float = 0.0
    stop_deviation_r: float = 0.0
    quality: float = 0.0
    rule_version: str = "frozen-v1"
    features: dict = field(default_factory=dict)
    origin: str = ""
    pda_source: str = ""
    htf_pda: bool = False
    htf_fvg_tf: str = ""
    regime_trend: str = ""
    regime_vol: str = ""
    #: P1-11: the model identity the run declared for this trade.  Empty means
    #: the pre-P1-11 path declared none; it is never a defaulted family.
    execution_model_id: str = ""
    narrative_order: str = ""


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade]
    equity: pd.Series
    metrics: dict


def _confluence(c: "CISD") -> int:  # type: ignore[name-defined]
    return sum(1 for k in ("sweep", "pda", "volume", "session", "targets", "smt") if c.checks.get(k))


def _latest_htf(result: ICTResult, t: int) -> str | None:
    candidates = [c for c in result.htf_cisds if c.end <= t]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.end).direction


def simulate_cisd(
    df: pd.DataFrame,
    result: ICTResult,
    config: StrategyConfig | None = None,
    symbol: str = "",
) -> BacktestResult:
    cfg = config or StrategyConfig()
    # One declared cost basis for the gate, the realised R and every partial
    # leg (P0-18).  The simulator's *entry* already embeds slippage, so the
    # realised-PnL model is ``for_filled_prices()``; the gate reports both
    # ratios from that same model.
    cost_model = CostModel(cfg.commission_bps, cfg.slippage_bps,
                           cfg.sell_tax_bps)
    filled_cost = cost_model.for_filled_prices()
    n = len(df)
    open_ = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    dates = pd.to_datetime(df["date"]).reset_index(drop=True)

    trades: list[Trade] = []
    equity = 1.0
    eq_points: list[tuple[pd.Timestamp, float]] = [(dates.iloc[0], equity)]
    day_counts: dict[object, int] = {}
    day_r: dict[object, float] = {}
    busy_until = -1
    atr = pd.Series(
        np.maximum.reduce([
            high - low,
            np.abs(high - np.r_[np.nan, close[:-1]]),
            np.abs(low - np.r_[np.nan, close[:-1]]),
        ])
    ).rolling(14, min_periods=1).mean().to_numpy()
    stats = {
        "signals": 0,
        "skipped_busy": 0,
        "skipped_direction": 0,
        "skipped_grade": 0,
        "skipped_confluence": 0,
        "skipped_htf": 0,
        "skipped_risk": 0,
        "skipped_day_limit": 0,
        "skipped_daily_loss": 0,
        "skipped_gap": 0,
        "not_triggered": 0,
        "refused_target_missing": 0,
        "refused_target_too_near": 0,
        "refused_target_wrong_side": 0,
        "refused_fill_outside_window": 0,
    }
    mode = cfg.entry_mode
    if mode not in ENTRY_MODES:
        raise ValueError(f"unknown entry_mode: {mode}")
    # One declared management plan per run (P0-19; MG-01/MG-08/V31).
    ladder_mode = cfg.declared_ladder_mode()
    be_mode = cfg.declared_be_mode()

    # result.cisds is already time-ordered; use only information at c.end
    for c in result.cisds:
        stats["signals"] += 1
        t = c.end
        if t + cfg.entry_delay >= n:
            continue
        if mode == "next_open" and t + cfg.entry_delay <= busy_until:
            stats["skipped_busy"] += 1
            continue
        if mode == "retest_key" and t + cfg.retest_max_bars <= busy_until:
            stats["skipped_busy"] += 1
            continue
        if mode == "fvg_ce" and t + cfg.retest_max_bars <= busy_until:
            stats["skipped_busy"] += 1
            continue
        if mode in {"ob_me", "ote"} and t + cfg.retest_max_bars <= busy_until:
            stats["skipped_busy"] += 1
            continue
        if c.direction == "bull" and not cfg.allow_long:
            stats["skipped_direction"] += 1
            continue
        if c.direction == "bear" and not cfg.allow_short:
            stats["skipped_direction"] += 1
            continue
        if c.grade < cfg.min_grade:
            stats["skipped_grade"] += 1
            continue
        conf = _confluence(c)
        if conf < cfg.min_confluence:
            stats["skipped_confluence"] += 1
            continue
        if cfg.require_htf:
            htf_dir = _latest_htf(result, t)
            if htf_dir is None or htf_dir != c.direction:
                stats["skipped_htf"] += 1
                continue

        signal_risk = abs(c.level - c.sl)
        if signal_risk <= 0:
            stats["skipped_risk"] += 1
            continue

        if mode in {"retest_key", "fvg_ce", "ob_me", "ote"}:
            entry_idx = None
            raw_entry = None
            target_level = c.entry_hint if mode in {"fvg_ce", "ob_me", "ote"} else c.level
            if target_level is None:
                stats["not_triggered"] += 1
                continue
            # A CE limit on the wrong side of invalidation is not a trade.
            if (c.direction == "bull" and target_level <= c.sl) or (
                c.direction == "bear" and target_level >= c.sl
            ):
                stats["skipped_gap"] += 1
                continue
            start = max(t + 1, t + cfg.entry_delay)
            stop_at = min(n, t + cfg.retest_max_bars + 1)
            for k in range(start, stop_at):
                if k <= busy_until:
                    continue
                a = atr[k] if not np.isnan(atr[k]) and atr[k] > 0 else 0.0
                tol = a * cfg.retest_tolerance_atr
                if c.direction == "bull" and low[k] <= target_level + tol:
                    # A buy limit at the level fills at the limit unless the bar
                    # opens below it, in which case the better open is used.
                    raw_entry = min(open_[k], target_level + tol)
                    entry_idx = k
                    break
                if c.direction == "bear" and high[k] >= target_level - tol:
                    raw_entry = max(open_[k], target_level - tol)
                    entry_idx = k
                    break
            if entry_idx is None:
                stats["not_triggered"] += 1
                continue
        else:
            entry_idx = t + cfg.entry_delay
            raw_entry = open_[entry_idx]

        # P0-13: the window gate must read the *fill* bar, not the signal bar.
        # ``next_open`` fills one bar after the signal, and a retest can fill
        # many bars later; a fill after the declared window has closed is not
        # that model's setup (EN-05), so the order is refused with a named §12
        # code and no position is opened.
        if cfg.session_gate != "none" and not in_session(
                df, int(entry_idx), cfg.market, cfg.session_gate):
            stats["refused_fill_outside_window"] += 1
            continue

        day = dates.iloc[entry_idx].date()
        if cfg.max_daily_loss_r > 0 and day_r.get(day, 0.0) <= -cfg.max_daily_loss_r:
            stats["skipped_daily_loss"] += 1
            continue
        if day_counts.get(day, 0) >= cfg.max_trades_per_day:
            stats["skipped_day_limit"] += 1
            continue

        slip = raw_entry * cfg.slippage_bps / 10000.0
        entry = raw_entry + slip if c.direction == "bull" else raw_entry - slip
        risk = abs(entry - c.sl)
        if risk <= 0:
            stats["skipped_risk"] += 1
            continue
        if mode in {"retest_key", "fvg_ce", "ob_me", "ote"}:
            # A limit that fills beyond its invalidation is not a valid trade.
            if (c.direction == "bull" and entry <= c.sl) or (c.direction == "bear" and entry >= c.sl):
                stats["skipped_gap"] += 1
                continue

        # TG-01: the plan gate requires a target before the order.  The target
        # is authored by setup's declared ``target_mode``; this gate never
        # derives one from the RR floor (P0-05).  If the authored target is too
        # near the actual fill, the setup is refused with
        # ``NT_TARGET_TOO_NEAR`` rather than reaching for a farther one (TG-07).
        tp = c.target
        if tp is None:
            stats["refused_target_missing"] += 1
            continue
        target_reward = (float(tp) - entry) if c.direction == "bull" else (entry - float(tp))
        if not target_reward > 0:
            stats["refused_target_wrong_side"] += 1
            continue
        # The declared gross-RR mode floor (RE:RM-15 / V28).  Gross RR is the
        # corpus gate (RM-14); the cost basis is still the one shared model and
        # the net ratio is reported alongside it.
        gross_rr, net_rr_value = cost_model.net_rr(entry, c.sl, tp,
                                                   c.direction)
        if gross_rr < cfg.rr_floor():
            stats["refused_target_too_near"] += 1
            continue

        # The daily budget is spent only by a plan that survives every
        # pre-trade refusal above.
        day_counts[day] = day_counts.get(day, 0) + 1

        # A-share T+1: the first bar on a later trading day is the earliest exit
        entry_day = dates.iloc[entry_idx].date()
        first_exit_idx = entry_idx + 1
        if cfg.t_plus_1:
            first_exit_idx = n - 1
            for k in range(entry_idx + 1, n):
                if dates.iloc[k].date() != entry_day:
                    first_exit_idx = k
                    break

        # gap through stop/target at the entry bar
        exit_price = None
        exit_idx = None
        reason = ""
        t1_locked = False
        if mode == "next_open":
            if c.direction == "bull":
                if open_[entry_idx] <= c.sl:
                    exit_price = open_[entry_idx]
                    exit_idx = entry_idx
                    reason = "gap_sl"
                elif open_[entry_idx] >= tp:
                    exit_price = open_[entry_idx]
                    exit_idx = entry_idx
                    reason = "gap_tp"
            else:
                if open_[entry_idx] >= c.sl:
                    exit_price = open_[entry_idx]
                    exit_idx = entry_idx
                    reason = "gap_sl"
                elif open_[entry_idx] <= tp:
                    exit_price = open_[entry_idx]
                    exit_idx = entry_idx
                    reason = "gap_tp"

        # A-share T+1: if the entry gaps through the stop, the trade cannot be
        # exited the same day, so the realistic action is to skip it.
        if cfg.t_plus_1 and reason == "gap_sl":
            stats["skipped_gap"] += 1
            continue

        mae = 0.0
        mfe = 0.0
        mae_idx = entry_idx
        mfe_idx = entry_idx
        be_armed = False
        trail_active = False
        best_price = entry
        t1_price: float | None = None
        t1_filled = False
        t1_at = (entry + cfg.t1_at_r * risk) if c.direction == "bull" else (entry - cfg.t1_at_r * risk)
        # doc 631: optional equilibrium-price partial exit
        t1_eq_armed = False
        if ladder_mode == "level_anchored":
            eq_price = (getattr(c, "quality_parts", None) or {}).get("dr_eq")
            if eq_price is not None:
                eq_price = float(eq_price)
                if (c.direction == "bull" and eq_price > entry) or (
                        c.direction == "bear" and eq_price < entry):
                    t1_at, t1_eq_armed = eq_price, True
        atr_entry = float(atr[max(0, entry_idx - 1)]) if len(atr) else 0.0
        if not np.isfinite(atr_entry) or atr_entry <= 0:
            atr_entry = risk
        if exit_idx is None:
            end_idx = min(n - 1, entry_idx + cfg.max_bars)
            sl_current = c.sl
            for k in range(entry_idx, end_idx + 1):
                # excursions are journaled even on a T+1 locked day
                if c.direction == "bull":
                    fav = high[k] - entry
                    adv = low[k] - entry
                else:
                    fav = entry - low[k]
                    adv = entry - high[k]
                if fav > mfe:
                    mfe, mfe_idx = fav, k
                if adv < mae:
                    mae, mae_idx = adv, k

                # cannot exit on the entry day for A shares
                if cfg.t_plus_1 and k < first_exit_idx:
                    continue

                # first tradable bar of a new day: gap can fill stop/target
                if cfg.t_plus_1 and k == first_exit_idx:
                    if c.direction == "bull":
                        if open_[k] <= sl_current:
                            exit_price, exit_idx, reason = open_[k], k, "gap_sl"
                            break
                        if open_[k] >= tp:
                            exit_price, exit_idx, reason = open_[k], k, "gap_tp"
                            break
                    else:
                        if open_[k] >= sl_current:
                            exit_price, exit_idx, reason = open_[k], k, "gap_sl"
                            break
                        if open_[k] <= tp:
                            exit_price, exit_idx, reason = open_[k], k, "gap_tp"
                            break

                # One declared same-bar convention, shared with the broker
                # (P0-18): if the bar touches both, the stop is assumed first.
                resolution = same_bar_exit(c.direction, high[k], low[k],
                                           sl_current, tp)
                if resolution == "stop":
                    exit_price, exit_idx = sl_current, k
                    reason = "trail" if trail_active and sl_current != c.sl else "sl"
                    break
                if resolution == "target":
                    exit_price, exit_idx, reason = tp, k, "tp"
                    break
                # partial take-profit on the first leg; the runner keeps the
                # original target and (optionally) a break-even stop.
                if (ladder_mode == "r_anchored" or t1_eq_armed) and not t1_filled:
                    reached = (high[k] >= t1_at) if c.direction == "bull" else (low[k] <= t1_at)
                    if reached:
                        t1_price, t1_filled = t1_at, True
                        if be_mode == "after_first_partial" and (
                                sl_current == c.sl or
                                (t1_at > sl_current if c.direction == "bull"
                                 else t1_at < sl_current)):
                            sl_current = entry
                if be_mode == "at_r" and not be_armed:
                    moved = (high[k] - entry) if c.direction == "bull" else (entry - low[k])
                    if moved >= cfg.breakeven_at_r * risk:
                        sl_current = entry
                        be_armed = True
                if cfg.trail_atr_mult > 0 and atr_entry > 0:
                    if c.direction == "bull":
                        best_price = max(best_price, float(high[k]))
                        moved = best_price - entry
                    else:
                        best_price = min(best_price, float(low[k]))
                        moved = entry - best_price
                    if moved >= max(cfg.trail_activate_r, 0.0) * risk:
                        if c.direction == "bull":
                            trail = best_price - cfg.trail_atr_mult * atr_entry
                            if trail > sl_current:
                                sl_current = trail
                                trail_active = True
                        else:
                            trail = best_price + cfg.trail_atr_mult * atr_entry
                            if trail < sl_current:
                                sl_current = trail
                                trail_active = True
            if exit_idx is None:
                if cfg.t_plus_1 and first_exit_idx > end_idx:
                    exit_idx = min(first_exit_idx, n - 1)
                    exit_price = open_[exit_idx]
                    reason = "t1_time"
                    t1_locked = True
                else:
                    exit_price, exit_idx, reason = close[end_idx], end_idx, "time"

        assert exit_price is not None and exit_idx is not None
        busy_until = exit_idx
        # Realised R uses the same declared cost basis as ``net_rr``; the fill
        # already embeds slippage, so ``filled_cost`` charges commission + tax.
        def leg_r(px: float) -> float:
            return filled_cost.realized_r(entry, c.sl, px, c.direction)

        tax = filled_cost.sell_tax(exit_price)
        r = filled_cost.realized_r(entry, c.sl, exit_price, c.direction)
        if t1_filled and t1_price is not None:
            # blend the two legs by size; both legs pay their own exit cost
            frac = min(max(cfg.t1_fraction, 0.0), 1.0)
            r = frac * leg_r(t1_price) + (1.0 - frac) * leg_r(exit_price)
            if reason in {"sl", "trail"} and exit_price == entry:
                reason = "t1_be"
        equity *= (1 + cfg.risk_pct * r)
        day_r[dates.iloc[exit_idx].date()] = day_r.get(dates.iloc[exit_idx].date(), 0.0) + r
        entry_dev_r = ((entry - c.level) if c.direction == "bull" else (c.level - entry)) / risk
        features = dict(getattr(c, "quality_parts", {}) or {})
        features.update({
            "grade": int(c.grade),
            "confluence": int(conf),
            "entry_rr": float(gross_rr),
            "entry_net_rr": float(net_rr_value),
            "risk_atr": float(risk / atr_entry) if atr_entry > 0 else 0.0,
        })
        trades.append(Trade(
            symbol=symbol,
            direction=c.direction,
            signal_time=dates.iloc[t],
            entry_time=dates.iloc[entry_idx],
            exit_time=dates.iloc[exit_idx],
            entry=float(entry),
            exit=float(exit_price),
            sl=float(c.sl),
            tp=float(tp),
            r_multiple=float(r),
            reason=reason,
            grade=int(c.grade),
            confluence=int(conf),
            bars=int(exit_idx - entry_idx + 1),
            checks=dict(c.checks),
            mae_r=float(mae / risk),
            mfe_r=float(mfe / risk),
            time_to_mae=int(mae_idx - entry_idx),
            time_to_mfe=int(mfe_idx - entry_idx),
            be_triggered=bool(be_armed),
            t1_locked=bool(t1_locked),
            t1_filled=bool(t1_filled),
            t1_price=float(t1_price) if t1_price is not None else None,
            sell_tax=float(tax),
            planned_entry=float(c.level),
            planned_sl=float(c.sl),
            planned_tp=float(tp),
            entry_deviation_r=float(entry_dev_r),
            stop_deviation_r=0.0,
            quality=float(getattr(c, "quality", 0.0)),
            rule_version="frozen-v1",
            features=features,
            origin=str(features.get("origin", "")),
            pda_source=str(features.get("pda_source", "")),
            htf_pda=bool(features.get("htf_pda", False)),
            htf_fvg_tf=str(features.get("htf_fvg_tf", "")),
            regime_trend=str(features.get("regime_trend", "")),
            regime_vol=str(features.get("regime_vol", "")),
            # P1-11: the setup's identity travels with the trade; a direct
            # simulator caller falls back to the declared config, never to an
            # inferred family.
            execution_model_id=str(
                features.get("execution_model_id")
                or cfg.execution_model_id or ""),
            narrative_order=str(
                features.get("narrative_order")
                or cfg.narrative_order or ""),
        ))
        eq_points.append((dates.iloc[exit_idx], equity))

    eq = pd.Series([p[1] for p in eq_points], index=pd.DatetimeIndex([p[0] for p in eq_points]))
    metrics = _metrics(trades, eq)
    metrics.update(stats)
    return BacktestResult(symbol=symbol, trades=trades, equity=eq, metrics=metrics)


def _sample_class(n: int) -> str:
    if n < 20:
        return "observation"
    if n < 50:
        return "review"
    if n < 100:
        return "limited"
    return "walk_forward"


def _metrics(trades: list[Trade], equity: pd.Series) -> dict:
    if not trades:
        return {
            "trades": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0,
            "profit_factor": 0.0, "max_dd": 0.0, "total_return": 0.0,
            "sharpe": 0.0, "avg_bars": 0.0, "sample_class": "observation",
        }
    r = np.array([t.r_multiple for t in trades], dtype=float)
    wins = r[r > 0]
    losses = r[r < 0]
    eq = equity.to_numpy(float)
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak - 1).min() if len(eq) else 0.0
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400), 1 / 365)
    total_return = eq[-1] - 1 if len(eq) else 0.0
    cagr = (eq[-1] ** (1 / years) - 1) if len(eq) and eq[-1] > 0 else 0.0
    tpy = len(trades) / years
    sharpe = (r.mean() / r.std() * math.sqrt(tpy)) if len(r) > 1 and r.std() > 0 else 0.0
    return {
        "trades": len(trades),
        "win_rate": float((r > 0).mean()),
        # Small samples: a 50% win rate on 23 trades is indistinguishable from
        # 30%.  The interval travels with the point estimate so it cannot be
        # quoted alone.
        "win_rate_ci95": [round(x, 4) for x in wilson_interval(int((r > 0).sum()), len(r))],
        "avg_r": float(r.mean()),
        "avg_r_ci95": [round(x, 4) for x in mean_interval([float(x) for x in r])],
        "total_r": float(r.sum()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf"),
        "max_dd": float(dd),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "sharpe": float(sharpe),
        "avg_bars": float(np.mean([t.bars for t in trades])),
        "sample_class": _sample_class(len(trades)),
        "n_for_5pct_win_rate": n_for_margin(0.5, 0.05),
    }


def run_one(path: str | Path, cfg: StrategyConfig, ict_cfg: ICTConfig | None = None,
            symbol: str | None = None) -> BacktestResult:
    df = load_csv(path)
    sym = symbol or Path(path).stem
    ict_cfg = ict_cfg or ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=cfg.require_htf,
                                   cisd_all=True, cisd_min_grade=0)
    ict_cfg.cisd_min_displacement_atr = cfg.min_displacement_atr
    if cfg.require_htf and len(df) > 2:
        median_delta = pd.to_datetime(df["date"]).diff().dt.total_seconds().median()
        ict_cfg.htf_tfs = ("1W", "1M") if median_delta and median_delta > 20 * 3600 else ("1h", "4h")
    res = analyze(df, ict_cfg)
    return simulate_cisd(df, res, cfg, sym)


def portfolio_curve(trades: list[Trade], risk_pct: float, max_concurrent: int) -> tuple[pd.Series, dict]:
    """Simple portfolio overlay: limited concurrent positions, fixed risk."""
    trades = sorted(trades, key=lambda t: t.entry_time)
    equity = 1.0
    open_exits: list[pd.Timestamp] = []
    points: list[tuple[pd.Timestamp, float]] = []
    taken_trades: list[Trade] = []
    taken = 0
    skipped = 0
    for t in trades:
        open_exits = [x for x in open_exits if x > t.entry_time]
        if max_concurrent > 0 and len(open_exits) >= max_concurrent:
            skipped += 1
            continue
        equity *= (1 + risk_pct * t.r_multiple)
        open_exits.append(t.exit_time)
        points.append((t.exit_time, equity))
        taken_trades.append(t)
        taken += 1
    eq = pd.Series([p[1] for p in points], index=pd.DatetimeIndex([p[0] for p in points])) if points else pd.Series(dtype=float)
    m = _metrics(taken_trades, eq)
    m["taken"] = taken
    m["skipped_concurrent"] = skipped
    return eq, m


def run_universe(paths: Iterable[str | Path], cfg: StrategyConfig,
                 ict_cfg: ICTConfig | None = None, limit: int | None = None) -> tuple[list[Trade], dict]:
    paths = list(paths)
    if limit:
        paths = paths[:limit]
    all_trades: list[Trade] = []
    per_symbol = []
    for i, p in enumerate(paths, 1):
        try:
            r = run_one(p, cfg, ict_cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {p}: {exc}")
            continue
        all_trades.extend(r.trades)
        per_symbol.append({"symbol": r.symbol, **r.metrics})
        if i % 25 == 0:
            print(f"progress {i}/{len(paths)} trades={len(all_trades)}")
    all_trades.sort(key=lambda t: t.entry_time)
    _, all_metrics = portfolio_curve(all_trades, cfg.risk_pct, 0)
    _, portfolio = portfolio_curve(all_trades, cfg.risk_pct, cfg.max_concurrent)
    return all_trades, {
        "trades": len(all_trades),
        **all_metrics,
        "portfolio": portfolio,
        "per_symbol": per_symbol,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="ICT/CISD strategy backtest")
    ap.add_argument("--csv", help="single CSV")
    ap.add_argument("--dir", help="directory of CSV files")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="output/ict/trades.csv")
    ap.add_argument("--metrics", default="output/ict/strategy_metrics.json")
    ap.add_argument("--rr", type=float, default=DEFAULT_FIXED_R)
    ap.add_argument("--max-bars", type=int, default=40)
    ap.add_argument("--breakeven-at-r", type=float, default=0.0)
    ap.add_argument("--t-plus-1", action="store_true")
    ap.add_argument("--sell-tax-bps", type=float, default=DEFAULT_SELL_TAX_BPS)
    ap.add_argument("--min-grade", type=int, default=4)
    ap.add_argument("--min-confluence", type=int, default=3)
    ap.add_argument("--min-displacement", type=float, default=0.0)
    ap.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT)
    ap.add_argument("--max-concurrent", type=int, default=5)
    ap.add_argument("--require-htf", action="store_true")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--no-long", action="store_true")
    args = ap.parse_args()

    cfg = StrategyConfig(
        rr=args.rr, max_bars=args.max_bars, min_grade=args.min_grade,
        breakeven_at_r=args.breakeven_at_r,
        t_plus_1=args.t_plus_1, sell_tax_bps=args.sell_tax_bps,
        min_confluence=args.min_confluence, risk_pct=args.risk_pct,
        require_htf=args.require_htf, allow_short=not args.no_short, allow_long=not args.no_long,
        min_displacement_atr=args.min_displacement,
        max_concurrent=args.max_concurrent,
    )
    ict_cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=args.require_htf,
                        cisd_all=True, cisd_min_grade=0, cisd_max=3)

    if args.csv:
        r = run_one(args.csv, cfg, ict_cfg)
        trades = r.trades
        metrics = {"trades": len(trades), **r.metrics, "per_symbol": [{"symbol": r.symbol, **r.metrics}]}
    elif args.dir:
        paths = sorted(Path(args.dir).glob("*.csv"))
        trades, metrics = run_universe(paths, cfg, ict_cfg, args.limit)
    else:
        raise SystemExit("--csv or --dir required")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [t.__dict__ for t in trades]
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    Path(args.metrics).parent.mkdir(parents=True, exist_ok=True)
    Path(args.metrics).write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))
    print(f"trades: {out}")


if __name__ == "__main__":
    main()
