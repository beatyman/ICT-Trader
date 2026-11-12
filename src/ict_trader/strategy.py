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
from dataclasses import dataclass, field, replace
from datetime import time as dtime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

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

#: P1-14 veto declarations (RK-11/RK-12).  The two P1-14 inputs the corpus
#: leaves *missing* -- the portfolio heat cap (§13.9 lists the string
#: "portfolio heat cap") and RK-13's correlation threshold (no attested value)
#: -- are deliberately absent: a run must declare them, and an undeclared
#: value is a refusal, never a default.  :data:`MISSING_VETO_INPUTS` names them
#: so the absence is a readable declaration rather than an oversight.
VETO_REGISTER: dict[str, RegisterEntry] = {
    "max_daily_loss_pct": RegisterEntry(
        "max_daily_loss_pct", 0.03, "repo",
        "§13.8 / RE:I-08 (live daily cap)"),
    "consecutive_loss_limit": RegisterEntry(
        "consecutive_loss_limit", 2, "corpus",
        "RK-11 / RE:RM-18 (consecutive losses in the killzone)"),
    "event_veto_window_hours": RegisterEntry(
        "event_veto_window_hours", 48.0, "corpus (single)",
        "RK-12 / [RE:RM-21]"),
    "event_veto_post_release_wait_minutes": RegisterEntry(
        "event_veto_post_release_wait_minutes", 10.0, "repo",
        "RK-12 / [RE:RM-21] (corpus range 5-10 min; upper end selected)"),
    "event_veto_cap_fraction": RegisterEntry(
        "event_veto_cap_fraction", 0.5, "corpus (single)",
        "RK-12 / [RE:RM-21] (cap at 50%)"),
}

#: §13.9 / RK-13 inputs the corpus does not supply.  They stay unfilled: the
#: veto surface refuses with ``NT_DATA_MISSING`` until a run declares them.
MISSING_VETO_INPUTS: tuple[str, ...] = (
    "portfolio_heat_cap", "correlation_threshold")

DEFAULT_MAX_DAILY_LOSS_PCT = float(
    VETO_REGISTER["max_daily_loss_pct"].value)
CONSECUTIVE_LOSS_LIMIT = int(
    VETO_REGISTER["consecutive_loss_limit"].value)
EVENT_VETO_WINDOW_HOURS = float(
    VETO_REGISTER["event_veto_window_hours"].value)
EVENT_VETO_POST_RELEASE_WAIT_MINUTES = float(
    VETO_REGISTER["event_veto_post_release_wait_minutes"].value)
EVENT_VETO_CAP = float(VETO_REGISTER["event_veto_cap_fraction"].value)


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


# --------------------------------------------------------------------------- #
# P1-15 management objects (SYSTEM_SPEC §9.6: MG-01…MG-11, V33, TG-08)
# --------------------------------------------------------------------------- #
# The corpus treats post-entry management as five separable decisions (TM-02),
# not as one parameter.  This section owns the four declarations a management
# plan cannot run without -- the holding class (V33/MG-11), the break-even form
# (MG-08), the session flat (MG-05) and the target expiry (TG-08) -- and the
# level ladder (MG-01/MG-02).  Nothing here invents a threshold:
#
# * every fraction, pip band and clock is the cited source's own value;
# * a quantity the corpus leaves *missing* (a universal partial fraction, a
#   reduce fraction, a trailing family) is never filled: the run must declare
#   it, and an absent declaration is a refusal (`NT_*`), not a default;
# * the ladder's per-level permissions are the corpus table, not a synthesis
#   of it.  The full MG-02 table is a permission schedule, not one executable
#   ladder: its tranches do not sum to 1, and :meth:`ManagementLadder.validate`
#   says so instead of silently normalising it.
#
#: §12 codes the management surface refuses with.  A plan is never executed
#: with a guessed holding class, an undeclared break-even form or an
#: unstipulated target expiry.
MANAGEMENT_UNDECLARED_CODE = "NT_VARIANT_UNDECLARED"
MANAGEMENT_MISSING_CODE = "NT_DATA_MISSING"

#: V33 is the holding-class fork; MG-08 names the BE-trigger vocabulary.
HOLDING_CLASS_ID = "V33"
BE_MODE_RULE_ID = "MG-08"


class HoldingClass(StrEnum):
    """V33: ``holding_class ∈ {intraday, swing, position}`` (MG-11).

    MG-11 makes the field **required**; the enum is only the vocabulary.  A
    run that does not declare it keeps the legacy path (no management plan),
    and the plan builder refuses with ``NT_VARIANT_UNDECLARED`` rather than
    choosing a class.
    """

    INTRADAY = "intraday"
    SWING = "swing"
    POSITION = "position"


HOLDING_CLASSES: tuple[str, ...] = tuple(item.value for item in HoldingClass)


def read_holding_class(value: HoldingClass | str) -> str:
    """The one V33 read path: an exact declared option, else ``ValueError``."""
    if isinstance(value, HoldingClass):
        return value.value
    text = str(value).strip().lower()
    if text not in HOLDING_CLASSES:
        raise ValueError(
            f"unknown holding_class {value!r}; declare one of "
            f"{HOLDING_CLASSES} (V33)")
    return text


class BEMode(StrEnum):
    """MG-08: the five mutually exclusive break-even triggers.

    ``never`` is a declared form, not a zero-valued float.  ``position`` admits
    only ``never`` (TM-33: break-even is prohibited for position trades); the
    other classes must declare one of the five, because the corpus supplies no
    arbiter between them (TM-66, C2).
    """

    AT_FIRST_INTERNAL_POOL = "at_first_internal_pool"
    AFTER_DAILY_RANGE_40_50 = "after_daily_range_40_50"
    AFTER_FIRST_PROFIT_THIRD_OF_MOVE = "after_first_profit_third_of_move"
    AFTER_SWING_MITIGATION = "after_swing_mitigation"
    NEVER = "never"


BE_MODE_FORMS: tuple[str, ...] = tuple(item.value for item in BEMode)

#: Provenance of each MG-08 form.  Every form is a corpus attestation and the
#: citation travels with it, so no form can be quoted as doctrine without its
#: source (TM-31, TM-32, TM-33).
BE_MODE_SOURCES: Mapping[str, dict[str, str]] = MappingProxyType({
    BEMode.AT_FIRST_INTERNAL_POOL.value: {
        "label": "corpus",
        "source": "[RE:MG-10] first internal pool / first LRLR "
                  "(doc_0505, doc_0508; TM-31)",
    },
    BEMode.AFTER_DAILY_RANGE_40_50.value: {
        "label": "corpus (single)",
        "source": "[RE:MG-10] after 40-50% of the expected daily range "
                  "(doc_0306; TM-32)",
    },
    BEMode.AFTER_FIRST_PROFIT_THIRD_OF_MOVE.value: {
        "label": "corpus (single)",
        "source": "[RE:MG-10] after first profit and ~1/3 of the intended "
                  "move (doc_0435; TM-32)",
    },
    BEMode.AFTER_SWING_MITIGATION.value: {
        "label": "corpus (single)",
        "source": "[RE:MG-10] after the entry is swing-mitigated "
                  "(proj_c141d918; TM-32)",
    },
    BEMode.NEVER.value: {
        "label": "corpus",
        "source": "[RE:MG-10] never, for position trades (doc_0406; TM-33)",
    },
})

#: The trigger names the pre-P1-15 simulator implements, mapped onto the MG-08
#: vocabulary.  ``at_r`` has **no** attested form: TM-66 records that no source
#: endorses a break-even move with neither a partial nor a maturity trigger, so
#: it stays a labelled repo trigger (``REPO_ONLY_BE_MODES``) and cannot be
#: reported as an MG-08 form.
BE_MODE_IMPLEMENTED_FORMS: Mapping[str, str | None] = MappingProxyType({
    "never": BEMode.NEVER.value,
    "after_first_partial": BEMode.AT_FIRST_INTERNAL_POOL.value,
    "at_r": None,
})

#: Implemented triggers that are repo choices, not corpus forms (TM-66).
REPO_ONLY_BE_MODES: tuple[str, ...] = ("at_r",)


def read_be_mode(value: BEMode | str) -> str:
    """Return one exact MG-08 form; an unknown value is a ``ValueError``."""
    text = value.value if isinstance(value, BEMode) else str(value).strip().lower()
    if text not in BE_MODE_FORMS:
        raise ValueError(
            f"unknown break-even form {value!r}; declare one of "
            f"{BE_MODE_FORMS} (MG-08)")
    return text


def mg08_be_mode(implementation: str | None) -> str | None:
    """Map an implemented trigger onto its MG-08 form (``None`` if it has none).

    ``never`` and ``after_first_partial`` are corpus forms; ``at_r`` is the
    labelled repo trigger with no MG-08 entry (TM-66).  A missing
    implementation returns ``None`` as well, so the caller refuses it instead
    of reading an empty string as ``never``.
    """
    if implementation is None:
        return None
    text = str(implementation).strip().lower()
    if text in BE_MODE_FORMS:
        return text
    if text in BE_MODE_IMPLEMENTED_FORMS:
        return BE_MODE_IMPLEMENTED_FORMS[text]
    raise ValueError(
        f"unknown be_mode {implementation!r}; declare one of "
        f"{BE_MODE_FORMS} or {tuple(BE_MODE_IMPLEMENTED_FORMS)}")


@dataclass(frozen=True, slots=True)
class HoldingClassPolicy:
    """What V33 selects for one holding class (MG-11).

    Every field is a corpus attestation or an explicit *missing* value:

    * ``session_flat`` -- MG-11 says the class selects whether the flat
      applies; TM-67 names applying an intraday flat to a position trade a
      category error, so only ``intraday`` carries it.
    * ``be_modes`` -- the MG-08 forms the class admits.  ``position`` admits
      only ``never`` (TM-33).
    * ``max_levels`` -- the ladder length the class's source states.  ``None``
      means the corpus does not attest one for that class; the canonical count
      is *missing* (1-5 levels across sources, TM-07).
    * ``stop_lookback_days`` -- MG-11's position-trade stop (40 then 20
      trading days); ``None`` where no such rule is attested.
    * ``trailing_family`` -- ``None`` where the four incompatible trail
      families (MG-12) have no per-class arbiter and the run must declare one.
    """

    holding_class: str
    session_flat: bool
    be_modes: tuple[str, ...]
    max_levels: int | None
    stop_lookback_days: tuple[int, int] | None
    trailing_family: str | None
    source_id: str
    label: str = "corpus"
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "holding_class",
                           read_holding_class(self.holding_class))
        if not self.be_modes:
            raise ValueError(f"{self.holding_class}: no BE form is admitted")
        for mode in self.be_modes:
            read_be_mode(mode)
        if self.max_levels is not None and int(self.max_levels) <= 0:
            raise ValueError(f"{self.holding_class}: max_levels must be > 0")
        if self.stop_lookback_days is not None:
            if len(self.stop_lookback_days) != 2 or any(
                    int(day) <= 0 for day in self.stop_lookback_days):
                raise ValueError(
                    f"{self.holding_class}: stop_lookback_days must be two "
                    f"positive integers")
        if not self.source_id.strip():
            raise ValueError(f"{self.holding_class}: a source_id is required")

    def admits_be(self, mode: BEMode | str) -> bool:
        """True only for an MG-08 form this class admits."""
        return read_be_mode(mode) in self.be_modes


_ALL_BE_FORMS: tuple[str, ...] = BE_MODE_FORMS

#: The three V33 policies.  The per-class selection is the corpus' own horizon
#: grouping (TM-31/TM-32/TM-33); see each row's ``source_id``.
HOLDING_CLASS_POLICIES: Mapping[str, HoldingClassPolicy] = MappingProxyType({
    HoldingClass.INTRADAY.value: HoldingClassPolicy(
        HoldingClass.INTRADAY, session_flat=True, be_modes=_ALL_BE_FORMS,
        max_levels=5, stop_lookback_days=None, trailing_family=None,
        source_id="[RE:MG-07; MG-02; TM-67]",
        notes="MG-02's session ladder is five levels. The canonical partial "
              "count is missing (TM-07), and the trailing family is contested "
              "(MG-12), so neither is defaulted here."),
    HoldingClass.SWING.value: HoldingClassPolicy(
        HoldingClass.SWING, session_flat=False, be_modes=_ALL_BE_FORMS,
        max_levels=3, stop_lookback_days=None, trailing_family=None,
        source_id="[TM-14; TM-32; TM-67]",
        notes="doc_0635's swing ladder is PDH/PWH/PMH (three rungs). A swing "
              "position is not managed like an intraday scalp (TM-67)."),
    HoldingClass.POSITION.value: HoldingClassPolicy(
        HoldingClass.POSITION, session_flat=False,
        be_modes=(BEMode.NEVER.value,), max_levels=None,
        stop_lookback_days=(40, 20), trailing_family="position_lookback",
        source_id="[TM-33; TM-34; MG-11]",
        notes="BE is prohibited for position trades (TM-33); the stop is MG-11's "
              "40-then-20 trading-day lookback, not a ladder. No ladder length "
              "is attested for this class."),
})


class LadderPermission(StrEnum):
    """MG-02's per-level permission vocabulary (the source's own words)."""

    MANDATORY = "mandatory"
    OPTIONAL = "optional"
    REMAINING = "remaining"
    RUNNER = "runner"
    SWING_ONLY = "swing_only"


LADDER_PERMISSIONS: tuple[str, ...] = tuple(item.value for item in LadderPermission)


@dataclass(frozen=True, slots=True)
class LadderLevel:
    """One rung of the MG-02 permission schedule.

    ``fraction`` is ``None`` only where the source states no fraction (L5 is
    "swing only"); the pip band is the source's own EUR/USD-scaled figure and is
    carried as metadata, never used to derive a price.  ``requires`` is the
    source's gate on the permission ("33/33/33 split", "4/4 alignment").
    """

    level_id: str
    anchor: str
    fraction: float | None
    permission: str
    pip_band: tuple[float, float] | None = None
    requires: tuple[str, ...] = ()
    move_stop_to_be: bool = False
    source_id: str = "MG-02"

    def __post_init__(self) -> None:
        if not self.level_id.strip():
            raise ValueError("ladder level id must not be empty")
        if not self.anchor.strip():
            raise ValueError(f"{self.level_id}: an anchor is required")
        if self.permission not in LADDER_PERMISSIONS:
            raise ValueError(
                f"{self.level_id}: unknown permission {self.permission!r}; "
                f"declare one of {LADDER_PERMISSIONS}")
        if self.fraction is not None:
            value = float(self.fraction)
            if not 0.0 < value <= 1.0:
                raise ValueError(
                    f"{self.level_id}: fraction {value} must be in (0, 1]")
        if self.pip_band is not None:
            low, high = (float(bound) for bound in self.pip_band)
            if low < 0 or high < low:
                raise ValueError(
                    f"{self.level_id}: invalid pip band [{low}, {high}]")
        if not self.source_id.strip():
            raise ValueError(f"{self.level_id}: a source_id is required")

    def to_public(self) -> dict:
        return {
            "level_id": self.level_id,
            "anchor": self.anchor,
            "fraction": self.fraction,
            "permission": self.permission,
            "pip_band": None if self.pip_band is None else list(self.pip_band),
            "requires": list(self.requires),
            "move_stop_to_be": self.move_stop_to_be,
            "source_id": self.source_id,
        }


#: MG-02's five-level permission schedule, transcribed from the source table
#: (doc_0584 `dedup_batch_107.md:149`).  The bands are EUR/USD-scaled; L4's
#: permission is gated on 4/4 alignment, L2's on a 33/33/33 split, and L5 is
#: swing-only with no stated fraction.
MG02_LEVELS: Mapping[str, LadderLevel] = MappingProxyType({
    "L1": LadderLevel(
        "L1", "first_irl", 0.50, LadderPermission.MANDATORY.value,
        pip_band=(5.0, 15.0), move_stop_to_be=True,
        source_id="[RE:MG-01/MG-02] (doc_0584 L1; doc_0589 T1)"),
    "L2": LadderLevel(
        "L2", "secondary_irl_1h_ce", 0.25,
        LadderPermission.OPTIONAL.value, pip_band=(10.0, 25.0),
        requires=("33/33/33 split",),
        source_id="[RE:MG-02] (doc_0584 L2)"),
    "L3": LadderLevel(
        "L3", "pdh", 0.50, LadderPermission.REMAINING.value,
        pip_band=(20.0, 60.0),
        source_id="[RE:MG-02] (doc_0584 L3)"),
    "L4": LadderLevel(
        "L4", "pwh", 0.25, LadderPermission.RUNNER.value,
        pip_band=(50.0, 150.0), requires=("4/4 alignment",),
        source_id="[RE:MG-02] (doc_0584 L4)"),
    "L5": LadderLevel(
        "L5", "pmh_ipda", None, LadderPermission.SWING_ONLY.value,
        requires=("swing horizon",),
        source_id="[RE:MG-02] (doc_0584 L5)"),
})


@dataclass(frozen=True, slots=True)
class ManagementLadder:
    """An executable partial ladder: level fractions plus the runner leg.

    MG-01 is the corpus' only well-attested ladder: close ``50%`` at the first
    IRL, move the stop to entry, leave ``50%`` working to the ERL.  A wider
    MG-02 selection is executable only when the selected tranches sum to 1;
    :meth:`validate` enforces the TM-63 invariants (each fraction in ``(0, 1]``,
    the fractions sum to 1, each level strictly beyond the previous one in the
    trade direction and before the terminal target) instead of normalising a
    schedule that was never a ladder.
    """

    levels: tuple[LadderLevel, ...]
    terminal_anchor: str = "erl"
    terminal_fraction: float = 0.50
    source_id: str = "MG-01"

    def __post_init__(self) -> None:
        object.__setattr__(self, "levels", tuple(self.levels))
        if not self.levels:
            raise ValueError("a management ladder needs at least one level")
        ids = [level.level_id for level in self.levels]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate ladder levels: {ids}")
        if not self.terminal_anchor.strip():
            raise ValueError("the runner leg needs a terminal anchor")
        if not 0.0 < float(self.terminal_fraction) <= 1.0:
            raise ValueError(
                f"terminal_fraction {self.terminal_fraction} must be in (0, 1]")
        if not self.source_id.strip():
            raise ValueError("a ladder needs a source_id")

    @property
    def fractions(self) -> tuple[float, ...]:
        """Every tranche including the runner leg."""
        return tuple(
            [float(level.fraction) for level in self.levels
             if level.fraction is not None]
            + [float(self.terminal_fraction)])

    @property
    def total_fraction(self) -> float:
        return sum(self.fractions)

    def validate(self, *, entry: float | None = None,
                 direction: str | None = None,
                 terminal_target: float | None = None,
                 level_prices: Sequence[float] | None = None) -> None:
        """Raise ``ValueError`` when the ladder is not executable (TM-63).

        The fraction invariants need no prices.  The ordering invariants are
        checked only when both ``entry``/``direction`` and ``level_prices`` are
        supplied; a ladder cannot prove that a level is before the terminal
        target without the target price, so a caller that has one must pass it.
        """
        fractions = self.fractions
        if any(not 0.0 < value <= 1.0 for value in fractions):
            raise ValueError(
                f"ladder fractions must be in (0, 1]; got {fractions}")
        if abs(sum(fractions) - 1.0) > 1e-9:
            raise ValueError(
                f"ladder fractions sum to {sum(fractions):g}, not 1; the "
                f"MG-02 schedule is a permission table, not one executable "
                f"ladder (TM-63)")
        if level_prices is None:
            return
        prices = [float(price) for price in level_prices]
        if len(prices) != len(self.levels):
            raise ValueError(
                f"expected {len(self.levels)} level prices, got {len(prices)}")
        if entry is None or direction is None:
            return
        if direction in {"long", "bull"}:
            sign = 1
        elif direction in {"short", "bear"}:
            sign = -1
        else:
            raise ValueError(f"unknown direction {direction!r}")
        previous = float(entry)
        for price in prices:
            if sign * (price - previous) <= 0:
                raise ValueError(
                    f"ladder levels must be strictly beyond the previous one "
                    f"in the trade direction; {price} does not follow "
                    f"{previous} for a {direction} trade")
            previous = price
        if terminal_target is not None:
            if sign * (float(terminal_target) - previous) <= 0:
                raise ValueError(
                    f"the terminal target {terminal_target} must be beyond the "
                    f"last ladder level {previous}")

    def to_public(self) -> dict:
        return {
            "levels": [level.to_public() for level in self.levels],
            "terminal_anchor": self.terminal_anchor,
            "terminal_fraction": self.terminal_fraction,
            "total_fraction": self.total_fraction,
            "source_id": self.source_id,
        }


#: MG-01's baseline ladder: 50% at the first IRL and 50% working to the ERL.
DEFAULT_MANAGEMENT_LADDER = ManagementLadder(
    (MG02_LEVELS["L1"],), terminal_anchor="erl", terminal_fraction=0.50,
    source_id="[RE:MG-01] (doc_0589, doc_0635, doc_0410, doc_0584)")

#: Readable alias for callers that name the rule rather than the default.
MG01_LADDER = DEFAULT_MANAGEMENT_LADDER


# --------------------------------------------------------------------------- #
# Session flat (MG-05) and target expiry (TG-08)
# --------------------------------------------------------------------------- #
#: The declared management clock.  TM-68 records that no corpus source states
#: whether its "EST" clocks follow daylight saving; the repo's one convention
#: is ``America/New_York`` (the convention ``setup.session_gate`` already uses),
#: so this surface resolves DST instead of carrying a fixed offset.
SESSION_FLAT_ZONE = "America/New_York"


@dataclass(frozen=True, slots=True)
class SessionFlat:
    """MG-05: exit ``100%`` at the session's flat instant, overriding scores.

    ``fraction`` is fixed at 1.0: the rule is "exit 100%", so a partial flat is
    not representable.  ``alert_minutes`` is the source's ``04:55`` alert ahead
    of the ``05:00`` flat.
    """

    clause: str
    at: dtime
    fraction: float = 1.0
    alert_minutes: int = 5
    overrides_score: bool = True
    zone: str = SESSION_FLAT_ZONE
    source_id: str = "[RE:MG-07] (TM-39)"

    def __post_init__(self) -> None:
        if not self.clause.strip():
            raise ValueError("a session-flat clause needs a name")
        if not isinstance(self.at, dtime):
            raise ValueError(f"{self.clause}: 'at' must be a datetime.time")
        if abs(float(self.fraction) - 1.0) > 1e-12:
            raise ValueError(
                f"{self.clause}: MG-05 exits 100% of the position; "
                f"fraction {self.fraction} contradicts the rule")
        if int(self.alert_minutes) < 0:
            raise ValueError(f"{self.clause}: alert_minutes must be >= 0")
        if not self.zone.strip():
            raise ValueError(f"{self.clause}: a time zone is required")

    @property
    def alert_at(self) -> dtime:
        """The pre-flat alert clock (``04:55`` for a ``05:00`` flat)."""
        return (pd.Timestamp("2000-01-01") + pd.Timedelta(
            hours=self.at.hour, minutes=self.at.minute,
            seconds=self.at.second) - pd.Timedelta(
                minutes=int(self.alert_minutes))).time()

    def to_public(self) -> dict:
        return {
            "clause": self.clause,
            "at": self.at.strftime("%H:%M"),
            "zone": self.zone,
            "fraction": self.fraction,
            "alert_at": self.alert_at.strftime("%H:%M"),
            "overrides_score": self.overrides_score,
            "source_id": self.source_id,
        }


#: MG-05's two sessions: the London kill-zone close at 05:00 NY and the NY AM
#: close at 11:00 NY.  This table is the only flat scheduler; nothing derives a
#: third clock.
SESSION_FLAT_CLAUSES: Mapping[str, SessionFlat] = MappingProxyType({
    "london": SessionFlat(
        "london", dtime(5, 0),
        source_id="[RE:MG-07] London KZ close (TM-39, TM-43)"),
    "ny_am": SessionFlat(
        "ny_am", dtime(11, 0),
        source_id="[RE:MG-07] NY AM close (TM-39, TM-43)"),
})


def _as_new_york(ts, input_tz: str = "UTC") -> pd.Timestamp | None:
    """Read one stamp on the ``America/New_York`` clock (``None`` if unusable).

    A naive stamp is read in ``input_tz`` (the repo's declared default for an
    unmarked frame is UTC, matching ``context.in_session``); an aware stamp is
    converted, so the input convention never moves the instant.
    """
    try:
        stamp = pd.Timestamp(ts)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is None:
        try:
            stamp = stamp.tz_localize(input_tz, ambiguous="NaT",
                                      nonexistent="shift_forward")
        except (TypeError, ValueError):
            return None
    if pd.isna(stamp):
        return None
    try:
        return stamp.tz_convert(SESSION_FLAT_ZONE)
    except (TypeError, ValueError):
        return None


def session_flat_due(ts, flat: SessionFlat, *, input_tz: str = "UTC") -> bool:
    """True once ``ts`` is at or past the flat instant on the NY clock."""
    local = _as_new_york(ts, input_tz)
    return local is not None and local.time() >= flat.at


def session_flat_deadline(entry_ts, flat: SessionFlat, *,
                          input_tz: str = "UTC") -> pd.Timestamp | None:
    """The flat instant on the trade's NY calendar date.

    The deadline is rebuilt on the calendar date rather than by adding 24 hours,
    so a DST transition inside the holding period cannot move the wall-clock
    flat (TM-68's unresolved clock is at least DST-safe in this one declared
    zone).  An entry at or after the instant is already at its deadline: the
    window gate should have refused such an entry, and this is the fail-closed
    backstop MG-05 asks for.
    """
    local = _as_new_york(entry_ts, input_tz)
    if local is None:
        return None
    return _local_flat_instant(local.date(), flat)


def _local_flat_instant(day, flat: SessionFlat) -> pd.Timestamp | None:
    try:
        naive = pd.Timestamp.combine(day, flat.at)
        return naive.tz_localize(
            flat.zone, ambiguous="NaT", nonexistent="shift_forward")
    except (TypeError, ValueError):
        return None


#: TG-08: the expiry clock a target is stated against, and the only two
#: dispositions the rule allows when the target is not reached.
TARGET_EXPIRY_DEADLINES: tuple[str, ...] = ("session_flat", "next_window")
TARGET_EXPIRY_ACTIONS: tuple[str, ...] = ("reduce", "flatten")


@dataclass(frozen=True, slots=True)
class TargetExpiry:
    """TG-08: the target's expiry, stated with the plan.

    "Not reached by session flat or next window ⇒ reduce or flatten".  Which
    clock ends the target is declared (``deadline``) and so is the disposition
    (``on_miss``).  The corpus states no reduce fraction, so ``reduce`` requires
    the run to declare one; ``flatten`` takes 100% and a fraction would
    contradict it.
    """

    deadline: str
    on_miss: str
    reduce_fraction: float | None = None
    source_id: str = "TG-08"

    def __post_init__(self) -> None:
        if self.deadline not in TARGET_EXPIRY_DEADLINES:
            raise ValueError(
                f"unknown target-expiry deadline {self.deadline!r}; declare "
                f"one of {TARGET_EXPIRY_DEADLINES} (TG-08)")
        if self.on_miss not in TARGET_EXPIRY_ACTIONS:
            raise ValueError(
                f"unknown target-expiry action {self.on_miss!r}; declare one "
                f"of {TARGET_EXPIRY_ACTIONS} (TG-08)")
        if self.on_miss == "flatten":
            if self.reduce_fraction is not None:
                raise ValueError(
                    "flatten exits 100%; a reduce_fraction contradicts it")
        else:
            if self.reduce_fraction is None:
                raise ValueError(
                    "TG-08 permits 'reduce', but the corpus states no reduce "
                    "fraction (missing); declare reduce_fraction explicitly")
            if not 0.0 < float(self.reduce_fraction) < 1.0:
                raise ValueError(
                    f"reduce_fraction {self.reduce_fraction} must be in (0, 1)")

    def action(self, *, reached: bool) -> str:
        """``hold`` when the target was reached, else the declared disposition."""
        return "hold" if reached else self.on_miss

    def to_public(self) -> dict:
        return {
            "deadline": self.deadline,
            "on_miss": self.on_miss,
            "reduce_fraction": self.reduce_fraction,
            "source_id": self.source_id,
        }


class PlanImmutabilityError(ValueError):
    """MG-09: the ladder may not be redrawn after entry.

    The corpus names redrawing the ladder to justify holding as an invalidation
    (TM-60), and "widen the stop / modify a ladder after entry" as one of the
    five manufacturing routes (X17).  The plan is a frozen value; this error is
    raised by the explicit revision guards, never by a silent in-place write.
    """


@dataclass(frozen=True, slots=True)
class ManagementPlan:
    """One post-entry plan (MG-01/02/05/08/09, TG-08, V33).

    ``blockers`` follows the ``RiskPlan`` convention: a refused plan carries the
    §12 code(s) and **no** ladder, expiry, session flat or BE form, so it can
    never be executed partially.  ``entry``/``stop``/``terminal_target`` are the
    observed prices the ladder is validated against; they are kept on a refused
    plan for the audit trail but authorise nothing.
    """

    holding_class: str
    ladder: ManagementLadder | None = None
    be_mode: str | None = None
    session_flat: SessionFlat | None = None
    target_expiry: TargetExpiry | None = None
    entry: float | None = None
    stop: float | None = None
    terminal_target: float | None = None
    blockers: tuple[str, ...] = ()
    variant_id: str = HOLDING_CLASS_ID
    source_id: str = "SYSTEM_SPEC §9.6 / MG-01-MG-11 / TG-08"

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.holding_class:
            object.__setattr__(self, "holding_class",
                               read_holding_class(self.holding_class))
        if self.be_mode is not None:
            object.__setattr__(self, "be_mode", read_be_mode(self.be_mode))
        if self.blockers:
            # Fail-closed: a refused plan hands out no management decision.
            if any(item is not None for item in
                   (self.ladder, self.be_mode, self.session_flat,
                    self.target_expiry)):
                raise ValueError(
                    "a refused management plan carries no ladder, BE form, "
                    "session flat or target expiry (P0-04)")
            return
        if not self.holding_class:
            raise ValueError(
                "MG-11 requires a holding_class; a usable plan cannot omit "
                "V33")
        policy = HOLDING_CLASS_POLICIES[self.holding_class]
        if self.be_mode is None:
            raise ValueError("MG-08 requires a declared break-even form")
        if not policy.admits_be(self.be_mode):
            raise ValueError(
                f"{self.holding_class}: break-even {self.be_mode!r} is not "
                f"admitted by its policy {policy.be_modes}")
        if self.target_expiry is None:
            raise ValueError(
                "TG-08 requires a target expiry stated with the plan")
        if policy.session_flat and self.session_flat is None:
            raise ValueError(
                f"{self.holding_class}: MG-05's session flat applies but no "
                f"clause was declared")
        if not policy.session_flat and self.session_flat is not None:
            raise ValueError(
                f"{self.holding_class}: the session flat does not apply to "
                f"this holding class (MG-11/TM-67)")
        if (self.ladder is not None and policy.max_levels is not None
                and len(self.ladder.levels) > policy.max_levels):
            raise ValueError(
                f"{self.holding_class}: ladder has {len(self.ladder.levels)} "
                f"levels, more than the {policy.max_levels} attested for the "
                f"class (MG-11)")

    @property
    def is_usable(self) -> bool:
        """True when no §12 blocker withheld the plan."""
        return not self.blockers

    def redraw(self, **changes: object) -> "ManagementPlan":
        """Return a revised plan, or refuse once the plan is post-entry.

        Pre-entry revision is legitimate (the plan is still being authored);
        after an entry exists the ladder is immutable (MG-09/TM-60).
        """
        if self.entry is not None:
            raise PlanImmutabilityError(
                "MG-09: the ladder may not be redrawn after entry")
        return replace(self, **changes)

    def check_redraw(self, revised: "ManagementPlan") -> None:
        """Refuse a post-entry revision that changes the plan's decisions."""
        if self.entry is None:
            return
        changed = (
            revised.entry != self.entry
            or revised.stop != self.stop
            or revised.terminal_target != self.terminal_target
            or revised.ladder != self.ladder
            or revised.be_mode != self.be_mode
        )
        if changed:
            raise PlanImmutabilityError(
                "MG-09: the ladder, stop and target are immutable after entry")

    def check_stop(self, new_stop: float) -> None:
        """Refuse a stop that widens; moving inward is permitted (RK-05/MG-09)."""
        if self.entry is None or self.stop is None:
            return
        sign = 1 if float(self.stop) < float(self.entry) else -1
        if sign * (float(new_stop) - float(self.stop)) < 0:
            raise PlanImmutabilityError(
                f"RK-05/MG-09: the stop may only move inward; "
                f"{new_stop} widens {self.stop}")

    def to_public(self) -> dict:
        return {
            "holding_class": self.holding_class,
            "ladder": None if self.ladder is None else self.ladder.to_public(),
            "be_mode": self.be_mode,
            "session_flat": (None if self.session_flat is None
                             else self.session_flat.to_public()),
            "target_expiry": (None if self.target_expiry is None
                              else self.target_expiry.to_public()),
            "entry": self.entry,
            "stop": self.stop,
            "terminal_target": self.terminal_target,
            "blockers": list(self.blockers),
            "variant_id": self.variant_id,
            "source_id": self.source_id,
        }


def resolve_session_flat(holding_class: str | None,
                         clause: str | None) -> SessionFlat | None:
    """Resolve the declared MG-05 clause for one holding class.

    ``None`` means no clause is declared.  A clause on a class whose policy
    excludes the flat is a category error (TM-67), not a silently ignored
    setting.
    """
    if holding_class is not None:
        hc = read_holding_class(holding_class)
        policy = HOLDING_CLASS_POLICIES[hc]
    else:
        policy = None
    if clause is None:
        return None
    if holding_class is None:
        raise ValueError(
            "MG-11 requires a holding_class before a session flat can apply; "
            "declare V33 or omit the clause")
    text = str(clause).strip().lower()
    try:
        flat = SESSION_FLAT_CLAUSES[text]
    except KeyError:
        raise ValueError(
            f"unknown session-flat clause {clause!r}; declare one of "
            f"{tuple(SESSION_FLAT_CLAUSES)} (MG-05)") from None
    if policy is not None and not policy.session_flat:
        raise ValueError(
            f"{hc}: the session flat does not apply to this holding class "
            f"(MG-11/TM-67)")
    return flat


def build_management_plan(
    *,
    holding_class: HoldingClass | str | None,
    ladder: ManagementLadder | None = None,
    be_mode: BEMode | str | None = None,
    session_clause: str | None = None,
    target_expiry: TargetExpiry | None = None,
    entry: float | None = None,
    stop: float | None = None,
    terminal_target: float | None = None,
    level_prices: Sequence[float] | None = None,
) -> ManagementPlan:
    """Build the one fail-closed management plan (P1-15).

    Absent declarations become §12 blockers, never defaults:

    * no ``holding_class``            -> ``NT_VARIANT_UNDECLARED`` (V33/MG-11);
    * no ``be_mode``                  -> ``NT_VARIANT_UNDECLARED`` (MG-08);
    * an ``at_r`` implementation      -> ``NT_DATA_MISSING`` (it has no MG-08
      form; TM-66);
    * ``intraday`` with no session clause -> ``NT_DATA_MISSING`` (MG-05's
      flat cannot be scheduled without naming the session);
    * no ``target_expiry``            -> ``NT_DATA_MISSING`` (TG-08).

    Contradictory declarations raise ``ValueError`` instead: an unknown
    holding class, an unknown BE form, a session flat on a swing/position plan
    (TM-67), a position plan with a non-``never`` BE (TM-33), or a ladder longer
    than the class's attested length.
    """
    if holding_class is None or (
            isinstance(holding_class, str) and not holding_class.strip()):
        return ManagementPlan(
            holding_class="",
            blockers=(MANAGEMENT_UNDECLARED_CODE,),
            entry=entry, stop=stop, terminal_target=terminal_target)

    hc = read_holding_class(holding_class)
    policy = HOLDING_CLASS_POLICIES[hc]
    blockers: list[str] = []

    be_form: str | None = None
    if be_mode is None:
        blockers.append(MANAGEMENT_UNDECLARED_CODE)
    else:
        be_form = mg08_be_mode(
            be_mode.value if isinstance(be_mode, BEMode) else be_mode)
        if be_form is None:
            blockers.append(MANAGEMENT_MISSING_CODE)
        elif not policy.admits_be(be_form):
            raise ValueError(
                f"{hc}: break-even {be_form!r} is not admitted; position "
                f"trades never move to break-even (TM-33)")

    flat = resolve_session_flat(hc, session_clause)
    if policy.session_flat and flat is None:
        blockers.append(MANAGEMENT_MISSING_CODE)

    if target_expiry is None:
        blockers.append(MANAGEMENT_MISSING_CODE)
    elif not isinstance(target_expiry, TargetExpiry):
        raise ValueError(
            f"target_expiry must be a TargetExpiry, got "
            f"{type(target_expiry).__name__}")

    if ladder is not None:
        if not isinstance(ladder, ManagementLadder):
            raise ValueError(
                f"ladder must be a ManagementLadder, got "
                f"{type(ladder).__name__}")
        if policy.max_levels is not None and len(ladder.levels) > policy.max_levels:
            raise ValueError(
                f"{hc}: ladder has {len(ladder.levels)} levels, more than the "
                f"{policy.max_levels} attested for the class (MG-11)")
        direction = None
        if entry is not None and stop is not None:
            direction = "long" if float(stop) < float(entry) else "short"
        try:
            ladder.validate(entry=entry, direction=direction,
                            terminal_target=terminal_target,
                            level_prices=level_prices)
        except ValueError:
            # A ladder that is not executable is an inadmissible input, not a
            # partially usable plan (P0-04).
            blockers.append(MANAGEMENT_MISSING_CODE)

    unique = tuple(dict.fromkeys(blockers))
    if unique:
        return ManagementPlan(
            holding_class=hc, blockers=unique,
            entry=entry, stop=stop, terminal_target=terminal_target)
    return ManagementPlan(
        holding_class=hc, ladder=ladder, be_mode=be_form,
        session_flat=flat, target_expiry=target_expiry,
        entry=entry, stop=stop, terminal_target=terminal_target)


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
    #: P1-15: the required holding class (V33/MG-11) and the declared session
    #: flat (MG-05).  ``None`` is the legacy path -- no class is declared, so no
    #: management plan is built and no session flat is enforced.  It is not a
    #: default class: the plan builder refuses an undeclared V33.
    holding_class: str | None = None
    session_flat_clause: str | None = None
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
        # P1-15: V33/MG-05 are declarations too, so they join the digest.  An
        # undeclared holding class adds nothing (the legacy path is unchanged).
        if self.holding_class is not None:
            extra["holding_class"] = self.declared_holding_class()
        if self.session_flat_clause is not None:
            extra["session_flat_clause"] = self.session_flat_clause
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
        # P1-15: V33 and MG-05 are validated up front; a session flat on a
        # non-intraday class is a category error (TM-67), not an ignored flag.
        self.declared_holding_class()
        self.declared_session_flat()
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

    def declared_holding_class(self) -> str | None:
        """The declared V33 holding class, or ``None`` for the legacy path."""
        if self.holding_class is None:
            return None
        return read_holding_class(self.holding_class)

    def declared_session_flat(self) -> SessionFlat | None:
        """The declared MG-05 session flat, or ``None`` when not applicable."""
        return resolve_session_flat(self.declared_holding_class(),
                                    self.session_flat_clause)


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
    #: P1-15: the declared V33 holding class (empty on the legacy path).  It
    #: selects the management policy, so it travels with the trade record.
    holding_class: str = ""


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
        "exits_session_flat": 0,
    }
    mode = cfg.entry_mode
    if mode not in ENTRY_MODES:
        raise ValueError(f"unknown entry_mode: {mode}")
    # One declared management plan per run (P0-19; MG-01/MG-08/V31).
    ladder_mode = cfg.declared_ladder_mode()
    be_mode = cfg.declared_be_mode()
    # P1-15: MG-05's session flat is declared once.  ``None`` is the legacy
    # path (no holding class declared); an intraday run that declares a clause
    # is flat at that session's close regardless of score.
    holding_class = cfg.declared_holding_class() or ""
    session_flat = cfg.declared_session_flat()

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
        # MG-05: the trade's flat deadline is the declared flat instant on the
        # entry bar's NY calendar date.  Rebuilt per date (DST-safe); an entry
        # at/after the clock is already at its deadline and the loop flattens it
        # on the first resolvable bar.
        flat_deadline = (session_flat_deadline(dates.iloc[entry_idx],
                                               session_flat)
                         if session_flat is not None else None)
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

                # MG-05: the session flat overrides every score.  It is checked
                # before the stop/target resolution of this bar, so a position
                # open at the flat instant cannot survive into a later bar.
                if flat_deadline is not None:
                    bar_local = _as_new_york(dates.iloc[k])
                    if bar_local is not None and bar_local >= flat_deadline:
                        exit_price, exit_idx = float(close[k]), k
                        reason = "session_flat"
                        stats["exits_session_flat"] += 1
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
            holding_class=holding_class,
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
