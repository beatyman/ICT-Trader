"""Optional market-context gates: session, premium/discount and OTE.

These are deliberately pure functions over already-known bars.  They do not
change the default frozen setup; `SetupConfig` opts into them one at a time so
each can be ablated.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, time as dtime, timedelta
from enum import Enum, StrEnum
from typing import Iterable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from .variants import Refusal, RefusalCode, read_variant_spec

Market = Literal["cn", "hk", "crypto", "other"]
SessionGate = Literal["none", "auto", "asian", "asian_dr", "london", "london_close", "ny_am", "ny_pm",
                      "silver_bullet", "macro_0950", "venom", "cn", "hk"]

#: Repo choice (§2.3): ATR is not defined by the corpus; the repository uses
#: ``ATR14``.  One function and one period serve every ATR-relative number
#: (FVG size, liquidity tolerance, sweep penetration, array gates).
ATR_PERIOD = 14


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """The one ATR series: true range, ``ATR_PERIOD`` = 14 (repo, §2.3).

    P0-16: this replaces the three co-existing definitions (a 10-period TR mean
    in :mod:`ict_trader.concepts`, a 14-period private copy in
    :mod:`ict_trader.sweeps`, and a high-low mean in the array layer).  A
    caller may repeat the declared period but may not select another one: a
    second period is exactly the defect this function removes, so it fails
    closed instead of silently computing a different quantity (A7/X8).
    """
    if int(period) != ATR_PERIOD:
        raise ValueError(
            f"one ATR period is declared ({ATR_PERIOD}); got {period!r} "
            "(SYSTEM_SPEC §2.3, P0-16)")
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()], axis=1,
    ).max(axis=1)
    return tr.rolling(ATR_PERIOD, min_periods=1).mean()


class BiasDirection(str, Enum):
    """The one declared bias reading vocabulary (BI-03; SPEC §13.2).

    The canonical wire values are ``bullish`` / ``bearish`` / ``neutral``.
    The repo's ``flat`` and the realtime layer's ``neutral`` name the same
    state; they are aliases into this enum, never separate states.

    ``unclear`` / ``ambiguous`` / ``both directions`` are the extra readings
    the corpus attests for the other BI-03 vocabularies.  They are *not*
    produced by the HH/HL primitive (:func:`structure_bias`): a run that
    declares ``+unclear``/``+ambiguous`` may hold them, and a run that
    declares ``binary`` may not hold ``neutral``.  The vocabulary is a declared
    fork (V03), never a spelling convention.
    """
    BULL = "bullish"
    BEAR = "bearish"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"
    AMBIGUOUS = "ambiguous"
    BOTH = "both directions"


#: Every repo spelling of a bias/direction token, mapped onto the enum.
_BIAS_ALIASES: dict[str, BiasDirection] = {
    "bull": BiasDirection.BULL,
    "bullish": BiasDirection.BULL,
    "long": BiasDirection.BULL,
    "bear": BiasDirection.BEAR,
    "bearish": BiasDirection.BEAR,
    "short": BiasDirection.BEAR,
    "flat": BiasDirection.NEUTRAL,
    "neutral": BiasDirection.NEUTRAL,
    "unclear": BiasDirection.UNCLEAR,
    "ambiguous": BiasDirection.AMBIGUOUS,
    "both": BiasDirection.BOTH,
    "both directions": BiasDirection.BOTH,
}


def parse_bias(value: object) -> BiasDirection:
    """Map any repo spelling of a bias reading onto :class:`BiasDirection`.

    Unknown spellings raise instead of being coerced: a second vocabulary is
    exactly the defect this enum removes.
    """
    if isinstance(value, BiasDirection):
        return value
    key = str(value).strip().lower()
    try:
        return _BIAS_ALIASES[key]
    except KeyError:
        raise ValueError(
            f"unknown bias state {value!r}; use one of "
            f"{[state.value for state in BiasDirection]}") from None


def serialize_bias(value: object) -> str:
    """The single serializer at every bias output boundary.

    ``None`` means *no reading* and is not ``neutral``: it serializes to the
    empty string so "no bias" is never collapsed into a direction or a neutral
    veto (P0-04/P1-02).  All other spellings go through :func:`parse_bias`.
    """
    if value is None:
        return ""
    return parse_bias(value).value


# --------------------------------------------------------------------------- #
# P1-02 — BiasState: one object per scope, held, revised only by §3.5
# --------------------------------------------------------------------------- #
#: BI-03 / V03: the five attested state vocabularies.  A run declares one; the
#: declaration is not reversible ("+neutral" is not a superset of "binary" for
#: a consumer that only knows binary).
BIAS_VOCABULARIES: tuple[str, ...] = (
    "binary", "+neutral", "+unclear", "+ambiguous", "both directions")

#: Which readings each vocabulary can express.  ``neutral`` in a binary run is
#: a state that vocabulary cannot name, so constructing one refuses instead of
#: silently downgrading it (BI-03).
_VOCABULARY_READINGS: dict[str, frozenset[BiasDirection]] = {
    "binary": frozenset({BiasDirection.BULL, BiasDirection.BEAR}),
    "+neutral": frozenset({BiasDirection.BULL, BiasDirection.BEAR,
                           BiasDirection.NEUTRAL}),
    "+unclear": frozenset({BiasDirection.BULL, BiasDirection.BEAR,
                           BiasDirection.UNCLEAR}),
    "+ambiguous": frozenset({BiasDirection.BULL, BiasDirection.BEAR,
                             BiasDirection.AMBIGUOUS}),
    "both directions": frozenset({BiasDirection.BULL, BiasDirection.BEAR,
                                  BiasDirection.BOTH}),
}


def validate_bias_vocabulary(vocabulary: str) -> str:
    """Return the declared BI-03 vocabulary or raise."""
    value = str(vocabulary).strip()
    if value not in BIAS_VOCABULARIES:
        raise ValueError(
            f"unknown bias vocabulary {vocabulary!r}; declare one of "
            f"{BIAS_VOCABULARIES} (BI-03)")
    return value


def bias_vocabulary_readings(vocabulary: str) -> frozenset[BiasDirection]:
    """The readings a vocabulary can express (BI-03)."""
    return _VOCABULARY_READINGS[validate_bias_vocabulary(vocabulary)]


class BiasHold(StrEnum):
    """§3.5 / `bias_hold`: the four attested holding periods.

    The corpus supplies four incompatible values and nothing adjudicates
    between them (``htf_bias`` C9), so the run must declare one.  A bar count
    is *not* part of the corpus value: it is a separate run declaration in
    :class:`BiasHoldPolicy`.
    """
    SESSION = "session"
    WEEK = "week"
    MULTIWEEK = "multiweek"
    OPEN = "open"


BIAS_HOLD_PERIODS: tuple[str, ...] = tuple(period.value for period in BiasHold)


@dataclass(frozen=True, slots=True)
class BiasHoldPolicy:
    """One declared hold period plus its declared horizon in base bars.

    ``session``/``week``/``multiweek`` have **no count in the corpus** (the
    multi-week count is explicitly `missing`), so ``horizon_bars`` is required
    for them and is never defaulted.  ``open`` is open-ended and refuses a
    horizon.  The policy only measures expiry; it never re-derives the bias --
    a new state requires a pre-session commitment or a §3.5 revision event.
    """

    hold: BiasHold
    horizon_bars: int | None = None

    def __post_init__(self) -> None:
        hold = BiasHold(str(self.hold).strip().lower())
        object.__setattr__(self, "hold", hold)
        if hold is BiasHold.OPEN:
            if self.horizon_bars is not None:
                raise ValueError(
                    "bias_hold='open' is open-ended and takes no horizon_bars")
            return
        if self.horizon_bars is None:
            raise ValueError(
                f"bias_hold={hold.value!r} has no corpus count; declare "
                "horizon_bars explicitly (no default is supplied)")
        horizon = int(self.horizon_bars)
        if horizon < 1:
            raise ValueError(
                f"bias_hold={hold.value!r} needs a positive horizon_bars, "
                f"got {self.horizon_bars!r}")
        object.__setattr__(self, "horizon_bars", horizon)

    def expiry_bar(self, state: "BiasState") -> int | None:
        """The first bar at which ``state`` is stale, or ``None`` if open."""
        if self.hold is BiasHold.OPEN:
            return None
        return int(state.as_of) + int(self.horizon_bars)

    def is_expired(self, state: "BiasState", bar: int) -> bool:
        """Whether the declared hold has ended at ``bar`` (inclusive)."""
        expiry = self.expiry_bar(state)
        return expiry is not None and int(bar) >= expiry

    def check(self, state: "BiasState", bar: int) -> Refusal | None:
        """The fail-closed result of :meth:`is_expired` (A3/A6)."""
        if not self.is_expired(state, bar):
            return None
        return Refusal(
            RefusalCode.NT_NO_BIAS,
            detail=(f"bias hold {self.hold.value!r} expired at bar "
                    f"{self.expiry_bar(state)} (state as_of={state.as_of}); "
                    "a new pre-session commitment is required"))

    def to_public(self) -> dict[str, object]:
        return {"hold": self.hold.value, "horizon_bars": self.horizon_bars,
                "missing_fields": []}


class RevisionEvent(StrEnum):
    """The six §3.5 revision events -- the only way out of ``B1``.

    The event is a *cause*, not a label: the store applies it through
    :func:`apply_bias_revision` and records it in
    :attr:`BiasDirection.revision_trigger`.  :attr:`FAILED_SETUP` is the one event
    that explicitly produces **no** state change.
    """
    CONFIRMED_HTF_CHOCH = "confirmed_htf_choch"
    BARE_HTF_CHOCH = "bare_htf_choch"
    BOTH_HTF_BROKEN = "both_htf_broken"
    OBJECTIVE_AND_OPPOSITE_ARRAY = "objective_and_opposite_array"
    REPEATED_ARRAY_FAILURES = "repeated_array_failures"
    FAILED_SETUP = "failed_setup"


#: The six events in SYSTEM_SPEC §3.5 table order.
REVISION_EVENTS: tuple[RevisionEvent, ...] = tuple(RevisionEvent)

#: The §3.5 effect of each event.  Every row is quoted from the spec table;
#: the repeated-failure count is `missing` there and is never invented here.
REVISION_EVENT_EFFECTS: dict[RevisionEvent, str] = {
    RevisionEvent.CONFIRMED_HTF_CHOCH: "re-derive bias",
    RevisionEvent.BARE_HTF_CHOCH: "flip, no confirmation test",
    RevisionEvent.BOTH_HTF_BROKEN: "flip; retracement becomes reversal",
    RevisionEvent.OBJECTIVE_AND_OPPOSITE_ARRAY: "re-declare, may flip",
    RevisionEvent.REPEATED_ARRAY_FAILURES: (
        "repair bias; the repeated-failure count is missing"),
    RevisionEvent.FAILED_SETUP: "no state change",
}

#: Events that must leave the state untouched (§3.5 last row).
NO_CHANGE_REVISION_EVENTS: tuple[RevisionEvent, ...] = (
    RevisionEvent.FAILED_SETUP,)


@dataclass(frozen=True, slots=True)
class BiasInvalidation:
    """The written invalidation condition (daily_bias R3, BI-04).

    The corpus asks for "the exact condition that would prove the bias wrong"
    and supplies no numeric rule (``htf_bias`` R31: no general invalidation or
    expiry rule).  A price/bar is therefore optional; when it is absent the
    field is explicitly ``None`` rather than a fabricated level.
    """

    condition: str
    price: float | None = None
    bar: int | None = None

    def __post_init__(self) -> None:
        condition = str(self.condition).strip()
        if not condition:
            raise ValueError("invalidation.condition must name the falsifier")
        object.__setattr__(self, "condition", condition)
        if self.price is not None:
            object.__setattr__(self, "price", float(self.price))
        if self.bar is not None:
            object.__setattr__(self, "bar", int(self.bar))

    @property
    def missing_fields(self) -> tuple[str, ...]:
        missing = []
        if self.price is None:
            missing.append("price")
        if self.bar is None:
            missing.append("bar")
        return tuple(missing)

    def to_public(self) -> dict[str, object]:
        return {"condition": self.condition, "price": self.price,
                "bar": self.bar, "missing_fields": list(self.missing_fields)}


@dataclass(frozen=True, slots=True)
class BiasState:
    """One stored directional state per scope (SYSTEM_SPEC §1.6/§3.1).

    The seven fields are the declared output contract:

    * ``scope``        - the bias tier/set this state covers (one per scope);
    * ``bias``         - a :class:`BiasDirection`, or ``None`` for *no reading*
      (never a fabricated neutral -- §1.5);
    * ``vocabulary``   - the declared BI-03 vocabulary the reading belongs to;
    * ``as_of``        - the bar index at which the state became known (a
      revision bar, or the pre-session anchor for the initial commitment);
    * ``invalidation`` - the written falsifier, or ``None`` when not written;
    * ``revision_trigger`` - the §3.5 event that produced this state; ``None``
      for the initial pre-session commitment;
    * ``variant_ids``  - the declared ``V##`` ids that produced the state.

    A per-call boolean is not a bias object and is not accepted here.  The
    object carries **no probability, confidence or score**: the corpus never
    converts a bias into one (``htf_bias`` R29), and a fabricated number in
    this type would be read as a calibrated forecast.  Absent fields are
    reported by :attr:`missing_fields` instead of being filled in.
    """

    scope: str
    bias: BiasDirection | None
    vocabulary: str
    as_of: int
    invalidation: BiasInvalidation | None
    revision_trigger: RevisionEvent | None
    variant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        scope = str(self.scope).strip()
        if not scope:
            raise ValueError("BiasState.scope must name the bias tier/set")
        object.__setattr__(self, "scope", scope)
        vocabulary = validate_bias_vocabulary(self.vocabulary)
        object.__setattr__(self, "vocabulary", vocabulary)
        if self.bias is not None:
            bias = parse_bias(self.bias)
            if bias not in bias_vocabulary_readings(vocabulary):
                raise ValueError(
                    f"bias reading {bias.value!r} is not expressible in the "
                    f"declared vocabulary {vocabulary!r} (BI-03)")
            object.__setattr__(self, "bias", bias)
        as_of = int(self.as_of)
        if as_of < 0:
            raise ValueError(f"BiasState.as_of must be a bar index, got {self.as_of!r}")
        object.__setattr__(self, "as_of", as_of)
        if self.invalidation is not None and not isinstance(
                self.invalidation, BiasInvalidation):
            raise TypeError(
                "BiasState.invalidation must be BiasInvalidation or None")
        if self.revision_trigger is not None:
            object.__setattr__(self, "revision_trigger",
                               RevisionEvent(self.revision_trigger))
        ids: list[str] = []
        raw_ids = ((self.variant_ids,) if isinstance(self.variant_ids, str)
                   else self.variant_ids)
        for raw in raw_ids:
            variant_id = str(raw).strip().upper()
            read_variant_spec(variant_id)  # refuse an undeclared V## id
            if variant_id not in ids:
                ids.append(variant_id)
        object.__setattr__(self, "variant_ids", tuple(ids))

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Declared fields that are absent, named rather than fabricated.

        ``revision_trigger`` is deliberately excluded: ``None`` there means
        "this is the initial pre-session state", which is a complete state, not
        a missing field.
        """
        missing = []
        if self.bias is None:
            missing.append("bias")
        if self.invalidation is None:
            missing.append("invalidation")
        if not self.variant_ids:
            missing.append("variant_ids")
        return tuple(missing)

    def expired(self, policy: BiasHoldPolicy, bar: int) -> bool:
        """Whether the given declared hold has ended at ``bar``."""
        return policy.is_expired(self, bar)

    def to_public(self) -> dict[str, object]:
        """The JSON-safe contract record; no probability/confidence field."""
        return {
            "scope": self.scope,
            "bias": None if self.bias is None else serialize_bias(self.bias),
            "vocabulary": self.vocabulary,
            "as_of": int(self.as_of),
            "invalidation": (None if self.invalidation is None
                             else self.invalidation.to_public()),
            "revision_trigger": (None if self.revision_trigger is None
                                 else self.revision_trigger.value),
            "variant_ids": list(self.variant_ids),
            "missing_fields": list(self.missing_fields),
        }


def apply_bias_revision(state: BiasState, event: RevisionEvent | str, *,
                        bar: int,
                        bias: object = None,
                        invalidation: BiasInvalidation | None = None,
                        variant_ids: Iterable[str] | None = None) -> BiasState:
    """Apply exactly one §3.5 revision event to ``state``.

    ``FAILED_SETUP`` returns the **same state object**: §3.5's last row says a
    single failed setup is not a reason to change the bias.  Every other event
    re-derives the bias at ``bar`` and records itself in
    ``revision_trigger``; the caller supplies the new reading (the store never
    infers a direction from the event name).  ``bias=None`` is only meaningful
    for ``FAILED_SETUP``; the other events must name a reading so a missing
    input cannot become a silent neutral.
    """
    revision = RevisionEvent(event)
    if revision in NO_CHANGE_REVISION_EVENTS:
        # §3.5's last row: a failed setup is explicitly *not* a reason to
        # change the bias.  Asking for a different reading through this event
        # is a caller bug, not a revision, so it refuses instead of being
        # ignored.
        if bias is not None and parse_bias(bias) is not state.bias:
            raise ValueError(
                "a single failed setup produces no state change (§3.5); "
                "declare the confirmed/bare CHoCH or another revision event")
        return state
    if bias is None:
        raise ValueError(
            f"revision event {revision.value!r} needs the re-derived bias; "
            "a missing reading is not a neutral state (§1.5)")
    bar = int(bar)
    if bar < int(state.as_of):
        raise ValueError(
            f"revision at bar {bar} precedes the state's as_of={state.as_of}; "
            "a state cannot be known before it exists (CB-07)")
    new_state = replace(
        state,
        bias=parse_bias(bias),
        as_of=bar,
        invalidation=(state.invalidation if invalidation is None
                      else invalidation),
        revision_trigger=revision,
        variant_ids=(state.variant_ids if variant_ids is None
                     else tuple(variant_ids)),
    )
    return new_state


@dataclass(frozen=True, slots=True)
class PreSessionBias:
    """The committed pre-session artefact (daily_bias R3, htf_bias R33).

    One object, written **before** the session opens, holding one
    :class:`BiasState` per scope.  ``anchor`` is the bar the run declares as
    its write instant; ``session_open`` is the first bar of the session the
    artefact governs, so ``anchor < session_open`` proves the pre-session
    property.  ``deadline`` is the declared write deadline; the corpus supplies
    four incompatible deadlines (``htf_bias`` C9) and no register row
    adjudicates them, so it is optional and ``None`` is reported explicitly in
    :attr:`missing_fields` rather than replaced with a chosen clock.
    """

    session_id: str
    anchor: int
    session_open: int
    states: tuple[BiasState, ...]
    deadline: int | None = None

    def __post_init__(self) -> None:
        session_id = str(self.session_id).strip()
        if not session_id:
            raise ValueError("session_id must name the session")
        object.__setattr__(self, "session_id", session_id)
        states = tuple(self.states)
        if not states:
            raise ValueError("a pre-session artefact needs at least one state")
        scopes: set[str] = set()
        for state in states:
            if not isinstance(state, BiasState):
                raise TypeError("states must be BiasState objects")
            if state.scope in scopes:
                raise ValueError(
                    f"duplicate scope {state.scope!r}; BiasState is one "
                    "object per scope (§1.6)")
            scopes.add(state.scope)
            if int(state.as_of) > int(self.anchor):
                raise ValueError(
                    f"state {state.scope!r} was read at bar {state.as_of}, "
                    f"after the declared commit anchor {self.anchor}")
        object.__setattr__(self, "states", states)
        anchor = int(self.anchor)
        session_open = int(self.session_open)
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "session_open", session_open)
        if anchor >= session_open:
            raise ValueError(
                f"pre-session artefact committed at bar {anchor} must precede "
                f"the session open at bar {session_open} (daily_bias R3)")
        if self.deadline is not None:
            deadline = int(self.deadline)
            object.__setattr__(self, "deadline", deadline)
            if deadline < anchor:
                raise ValueError(
                    f"state committed at bar {anchor} is after the declared "
                    f"pre-session deadline {deadline}")
            if deadline >= session_open:
                raise ValueError(
                    f"the pre-session deadline {deadline} must precede the "
                    f"session open {session_open}")

    @property
    def variant_ids(self) -> tuple[str, ...]:
        """The union of the states' declared variant ids, register order."""
        seen: list[str] = []
        for state in self.states:
            for variant_id in state.variant_ids:
                if variant_id not in seen:
                    seen.append(variant_id)
        return tuple(seen)

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Fields absent from the artefact; ``deadline`` is the declared one."""
        return () if self.deadline is not None else ("deadline",)

    def state(self, scope: str) -> BiasState | None:
        """The committed state for ``scope``, or ``None`` when not committed."""
        wanted = str(scope).strip()
        for state in self.states:
            if state.scope == wanted:
                return state
        return None

    def to_public(self) -> dict[str, object]:
        """The written artefact as a JSON-safe record."""
        return {
            "session_id": self.session_id,
            "anchor": int(self.anchor),
            "session_open": int(self.session_open),
            "deadline": self.deadline,
            "states": [state.to_public() for state in self.states],
            "variant_ids": list(self.variant_ids),
            "missing_fields": list(self.missing_fields),
        }

    def as_store(self, hold: BiasHoldPolicy) -> "BiasStateStore":
        """A read/revise store seeded with the committed states."""
        return BiasStateStore(self.states, hold=hold)


def commit_pre_session(states: Iterable[BiasState], *, session_id: str,
                       anchor: int, session_open: int,
                       deadline: int | None = None) -> PreSessionBias:
    """Write the one pre-session artefact, computing nothing at read time.

    This is the P1-02 entry point: a caller computes the readings once, commits
    them here at its declared ``anchor`` (before ``session_open``), and every
    later read goes through :class:`BiasStateStore` instead of re-deriving the
    bias per candidate.
    """
    return PreSessionBias(
        session_id=session_id, anchor=anchor, session_open=session_open,
        states=tuple(states), deadline=deadline)


class BiasStateStore:
    """One read/revise path over the pre-session :class:`BiasState` objects.

    The store holds exactly one state per scope, reads it only at
    ``bar >= as_of``, refuses after the declared hold expires, and applies
    revisions only through the six §3.5 events
    (:func:`apply_bias_revision`).  It never re-derives a bias from a bar and
    never fabricates one: a missing scope or an expired hold is a
    ``NT_NO_BIAS`` refusal.
    """

    def __init__(self, states: Iterable[BiasState], *,
                 hold: BiasHoldPolicy) -> None:
        if not isinstance(hold, BiasHoldPolicy):
            raise TypeError("a BiasStateStore needs a declared BiasHoldPolicy")
        self._hold = hold
        self._states: dict[str, BiasState] = {}
        for state in states:
            if not isinstance(state, BiasState):
                raise TypeError("states must be BiasState objects")
            if state.scope in self._states:
                raise ValueError(
                    f"duplicate scope {state.scope!r}; BiasState is one "
                    "object per scope (§1.6)")
            self._states[state.scope] = state

    @property
    def hold(self) -> BiasHoldPolicy:
        return self._hold

    @property
    def states(self) -> tuple[BiasState, ...]:
        return tuple(self._states.values())

    def state(self, scope: str) -> BiasState | None:
        return self._states.get(str(scope).strip())

    def read(self, scope: str, bar: int) -> tuple[BiasState | None, Refusal | None]:
        """Return the held state at ``bar``, or a typed ``NT_NO_BIAS`` refusal."""
        key = str(scope).strip()
        state = self._states.get(key)
        if state is None:
            return None, Refusal(
                RefusalCode.NT_NO_BIAS,
                detail=f"no committed bias state for scope {key!r}")
        bar = int(bar)
        if bar < int(state.as_of):
            return None, Refusal(
                RefusalCode.NT_NO_BIAS,
                detail=(f"bias state for scope {key!r} has as_of={state.as_of} "
                        f"and is not knowable at bar {bar} (CB-07)"))
        refusal = self._hold.check(state, bar)
        if refusal is not None:
            return None, refusal
        return state, None

    def revise(self, scope: str, event: RevisionEvent | str, *, bar: int,
               bias: object = None,
               invalidation: BiasInvalidation | None = None,
               variant_ids: Iterable[str] | None = None) -> BiasState:
        """Apply one §3.5 event and store the result.

        A single failed setup returns the current state unchanged (§3.5).  The
        caller must name the re-derived reading for every other event; the
        store never infers a direction from the event name.
        """
        key = str(scope).strip()
        try:
            state = self._states[key]
        except KeyError:
            raise KeyError(
                f"no committed bias state for scope {key!r}; commit the "
                "pre-session artefact before revising") from None
        revised = apply_bias_revision(
            state, event, bar=bar, bias=bias,
            invalidation=invalidation, variant_ids=variant_ids)
        self._states[key] = revised
        return revised

    def to_public(self, bar: int) -> dict[str, object]:
        """The held states at ``bar`` with refusals, for audit output."""
        records = []
        for scope in self._states:
            state, refusal = self.read(scope, bar)
            records.append({
                "scope": scope,
                "state": None if state is None else state.to_public(),
                "refusal": None if refusal is None else {
                    "code": refusal.code.value, "detail": refusal.detail},
            })
        return {
            "hold": self._hold.to_public(),
            "bar": int(bar),
            "scopes": records,
        }


#: BI-06/BI-07: the one declared neutral handling read by every bias consumer.
#:
#: The corpus attests more than one handling, so the choice is declared rather
#: than inferred from the caller.  ``no_trade`` is the BI-06 default ("neutral
#: days should be skipped"): a neutral reading blocks a directional setup.
#: ``score_half`` is the attested scoring variant (neutral counts ``0.5``), and
#: ``narrative_only`` keeps the reading informational so it never vetoes.
NEUTRAL_HANDLINGS = ("no_trade", "score_half", "narrative_only")
DEFAULT_NEUTRAL_HANDLING = "no_trade"


def validate_neutral_handling(handling: str) -> str:
    """Return the declared neutral handling or raise (BI-06/BI-07)."""
    value = str(handling).strip().lower()
    if value not in NEUTRAL_HANDLINGS:
        raise ValueError(
            f"unknown neutral_handling {handling!r}; declare one of "
            f"{NEUTRAL_HANDLINGS} (BI-06/BI-07)")
    return value


def neutral_blocks(handling: str = DEFAULT_NEUTRAL_HANDLING) -> bool:
    """Whether a neutral read blocks a directional setup under ``handling``."""
    return validate_neutral_handling(handling) == "no_trade"


def neutral_weight(handling: str = DEFAULT_NEUTRAL_HANDLING) -> float:
    """The score a neutral read contributes under ``handling`` (BI-06)."""
    value = validate_neutral_handling(handling)
    if value == "no_trade":
        return 0.0
    if value == "score_half":
        return 0.5
    return 1.0  # narrative_only: the read is recorded, never a veto


@dataclass(frozen=True)
class DealingRange:
    high: float
    low: float
    eq: float
    high_bar: int
    low_bar: int

    @property
    def span(self) -> float:
        return self.high - self.low

    def position(self, price: float) -> float:
        """0 at the low, 1 at the high, clamped."""
        if self.span <= 0:
            return 0.5
        return min(max((price - self.low) / self.span, 0.0), 1.0)


#: §12 codes named by the range diagnostic.  ``NT_RANGE_INVALID`` covers a
#: structural range that cannot be used (stale anchors or a degenerate pair);
#: ``NT_DATA_MISSING`` covers "no confirmed high/low pair at all".  The
#: prohibited fixed-count fallback keeps its declared name
#: ``NT_RANGE_TIME_ANCHORED`` so a caller can tell it apart from a missing feed.
NT_RANGE_INVALID = "NT_RANGE_INVALID"
NT_RANGE_TIME_ANCHORED = "NT_RANGE_TIME_ANCHORED"
NT_DATA_MISSING = "NT_DATA_MISSING"

#: Repo staleness expiry for the two structural anchors (§13.3 repo constant;
#: DR-06 says a range is redrawn, not expired silently).
RANGE_STALENESS_BARS = 120


@dataclass(frozen=True, slots=True)
class RangeDiagnostic:
    """Why a structural dealing range is absent -- never a silent ``None``.

    P0-16: a 120-bar staleness (or a missing structural pair) used to make the
    range vanish with no explanation.  The diagnostic names the cause and the
    declared lookback so a caller can record ``NT_RANGE_INVALID`` instead of
    treating an absent range as "no opinion".  P1-12 will promote this into the
    full ``RangeState {valid, redraw_reason}``; the P0 subset is the reason
    itself, not the redraw policy.
    """

    code: str
    reason: str
    bar: int | None = None
    age_bars: int | None = None
    lookback_bars: int | None = None

    def to_public(self) -> dict[str, object]:
        return {
            "code": self.code, "reason": self.reason, "bar": self.bar,
            "age_bars": self.age_bars, "lookback_bars": self.lookback_bars,
        }


def _no_pair_diagnostic(t: int, missing: str, lookback_bars: int) -> RangeDiagnostic:
    return RangeDiagnostic(
        NT_DATA_MISSING,
        f"no confirmed swing {missing} at or before bar {t}: a structural "
        "dealing range needs one confirmed high and one confirmed low.  The "
        "fixed-count high/low fallback is refused (DR-00/SN-R07), so no "
        "DEALING_RANGE is emitted "
        f"(NT_RANGE_TIME_ANCHORED would name that prohibited object).",
        bar=t, lookback_bars=int(lookback_bars),
    )


def _stale_diagnostic(t: int, later_conf: int, age: int,
                      lookback_bars: int) -> RangeDiagnostic:
    return RangeDiagnostic(
        NT_RANGE_INVALID,
        f"dealing-range anchors are stale: the later anchor confirmed at bar "
        f"{later_conf}, {age} bars before bar {t} > {int(lookback_bars)}-bar "
        "repo staleness cap (§13.3).  Redraw on the DR-06 triggers; the range "
        "is not usable as-is.",
        bar=t, age_bars=age, lookback_bars=int(lookback_bars),
    )


def _degenerate_diagnostic(t: int, age: int | None,
                           lookback_bars: int) -> RangeDiagnostic:
    return RangeDiagnostic(
        NT_RANGE_INVALID,
        f"dealing-range anchors are degenerate: high <= low at bar {t}, so no "
        "range can be measured (DR-01).",
        bar=t, age_bars=age, lookback_bars=int(lookback_bars),
    )


_MARKET_TZ: dict[str, str] = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "crypto": "UTC",
    "other": "UTC",
}

#: The one Silver Bullet clock (silver_bullet R13/R14, LT-B01, SPEC §10.2):
#: three one-hour windows on the America/New_York wall clock.  Every Silver
#: Bullet call site imports this table; no module keeps a second clock (P0-12).
SILVER_BULLET_TZ = "America/New_York"
SILVER_BULLET_WINDOWS: tuple[tuple[dtime, dtime], ...] = (
    (dtime(3, 0), dtime(4, 0)),
    (dtime(10, 0), dtime(11, 0)),
    (dtime(14, 0), dtime(15, 0)),
)

_SESSION_WINDOWS: dict[str, tuple[str, Sequence[tuple[dtime, dtime]]]] = {
    "cn": ("Asia/Shanghai", [(dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0))]),
    "hk": ("Asia/Hong_Kong", [(dtime(9, 30), dtime(12, 0)), (dtime(13, 0), dtime(16, 0))]),
    "london": ("America/New_York", [(dtime(2, 0), dtime(5, 0))]),
    # ICT teaching: Asian range accumulates 19:00-00:00 NY; London close 10:00-12:00 NY.
    # Declared half-open: the 00:00 end is exclusive, not 23:59:59.
    "asian": ("America/New_York", [(dtime(19, 0), dtime(0, 0))]),
    # doc 601 (liquiditysweeps.com): "The Asian range (00:00-02:00 EST) functions as
    # a micro-dealing range for the London open."  Kept as a separate key because
    # the legacy "asian" window above uses the 19:00-00:00 NY convention.
    "asian_dr": ("America/New_York", [(dtime(0, 0), dtime(2, 0))]),
    # doc 546: "the 09:50 NY-AM macro window ... concentrated inside that 20-minute
    # window"; doc 491 lists 09:50 as a highest-conviction FVG window.
    "macro_0950": ("America/New_York", [(dtime(9, 50), dtime(10, 10))]),
    # doc 69 (ICT Venom model): "a raid of the 08:00-09:30 New York range after the
    # 09:30 open".  The gate selects the pre-open range that gets raided.
    "venom": ("America/New_York", [(dtime(8, 0), dtime(9, 30))]),
    "london_close": ("America/New_York", [(dtime(10, 0), dtime(12, 0))]),
    "ny_am": ("America/New_York", [(dtime(7, 0), dtime(10, 0))]),
    "ny_pm": ("America/New_York", [(dtime(13, 30), dtime(16, 0))]),
    "silver_bullet": (SILVER_BULLET_TZ, SILVER_BULLET_WINDOWS),
}


def session_windows(key: str) -> tuple[str, tuple[tuple[dtime, dtime], ...]]:
    """Return the canonical ``(zone, windows)`` pair for one session gate.

    This is the only read path for the table above.  Callers that annotate the
    clock (``concepts.detect_killzones``) must derive their spans from it rather
    than defining a second timezone table (P0-12).
    """
    zone, windows = _SESSION_WINDOWS[key]
    return zone, tuple(windows)


def _as_zone(df: pd.DataFrame, idx: int, zone: str, market: str = "other") -> pd.Timestamp:
    ts = pd.Timestamp(df["date"].iloc[idx])
    if ts.tzinfo is None:
        source = _MARKET_TZ.get(market, "UTC")
        return ts.tz_localize(source, ambiguous="NaT", nonexistent="shift_forward").tz_convert(zone)
    return ts.tz_convert(zone)


def _gate_key(market: str, gate: SessionGate) -> str | None:
    """Resolve a gate (including ``auto``) to one key in the session table.

    ``None`` means "no gate" and always passes; it is distinct from a gate
    whose window is simply closed at this instant.
    """
    if gate == "none":
        return None
    if gate == "auto":
        if market in {"cn", "hk"}:
            return market
        if market == "other":
            return "ny_am"
        return None
    return gate


def _in_window(t: dtime, start: dtime, end: dtime) -> bool:
    """Half-open window test: ``[start, end)`` on the session wall clock.

    A declared end of ``00:00`` means the window runs to *next-day* midnight,
    which is the exclusive end instant (``19:00-00:00`` is 19:00 up to but not
    including 00:00).  Such a window crosses the date boundary, so its start is
    later than its end on the clock.
    """
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _stamp_in_zone(ts, zone: str, market: str = "other",
                   input_tz: str | None = None) -> pd.Timestamp | None:
    """Convert one timestamp into ``zone``; ``None`` when it is not a time.

    Naive stamps are read in ``input_tz`` when supplied, else in the market's
    native zone (UTC for crypto).  Aware stamps are converted directly, so the
    caller's input convention never moves the instant.
    """
    try:
        stamp = pd.Timestamp(ts)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is None:
        source = input_tz or _MARKET_TZ.get(market, "UTC")
        try:
            stamp = stamp.tz_localize(source, ambiguous="NaT",
                                      nonexistent="shift_forward")
        except (TypeError, ValueError):
            return None
    if pd.isna(stamp):
        return None
    try:
        return stamp.tz_convert(zone)
    except (TypeError, ValueError):
        return None


def in_session_at(ts, gate: SessionGate, market: str = "other",
                  input_tz: str | None = None) -> bool:
    """Timestamp-level twin of :func:`in_session` over the same table.

    ``in_session`` reads one bar of a frame; this reads a single instant.  Both
    resolve ``gate``/``auto`` through :func:`_gate_key` and test the same
    half-open windows, so a bar verdict and an instant verdict cannot diverge.
    """
    key = _gate_key(market, gate)
    if key is None:
        return True
    zone, windows = _SESSION_WINDOWS[key]
    local = _stamp_in_zone(ts, zone, market, input_tz)
    if local is None:
        return False
    t = local.time()
    return any(_in_window(t, start, end) for start, end in windows)


def in_silver_bullet(ts, tz: str = SILVER_BULLET_TZ,
                     input_tz: str = "UTC") -> bool:
    """Whether ``ts`` is inside the canonical Silver Bullet table (P0-12).

    This is the one Silver Bullet predicate.  ``tz`` is accepted only because
    the former ``rules.in_silver_bullet`` exposed it; it cannot select a
    second window set, because there is one table and it is defined on the
    ``America/New_York`` clock.  ``input_tz`` is the zone of a naive ``ts``.
    """
    return in_session_at(ts, "silver_bullet", "crypto", input_tz=input_tz)


def in_session(df: pd.DataFrame, idx: int, market: str, gate: SessionGate) -> bool:
    """Return whether a closed bar falls in the requested cash/session window.

    Naive timestamps are interpreted in the market's native timezone (UTC for
    crypto) and then converted to the session timezone.  This matters for the
    New York kill zones: Binance 15m timestamps are UTC, not New York local.
    """
    key = _gate_key(market, gate)
    if key is None:
        return True
    zone, windows = _SESSION_WINDOWS[key]
    t = _as_zone(df, idx, zone, market).time()
    return any(_in_window(t, start, end) for start, end in windows)


# --------------------------------------------------------------------------- #
# P1-03 — one declared day boundary (V04, SYSTEM_SPEC §10.4 GW-01)
# --------------------------------------------------------------------------- #
#: The four V04 options, read from the one register (A7).  The option strings
#: live in ``variants.py`` only; this module never keeps a second copy of the
#: vocabulary.
DAY_BOUNDARY_OPTIONS: tuple[str, ...] = tuple(read_variant_spec("V04").options)

#: The clock each V04 option anchors, and the provenance of that clock.
#:
#: ``NY midnight`` is the corpus's dominant anchor (daily_bias R5: "the 00:00
#: New York opening price"; GW-03: daily default ``00:00 America/New_York``).
#: ``futures open`` and ``cash open`` are corpus-attested instants (CME Globex
#: 18:00 ET; NYSE 09:30 ET).  ``broker day`` is the one convention whose clock
#: the corpus names but does not define (daily_bias R10; ``broker.py`` used to
#: roll on the UTC date): the broker supplies the roll time, so it is required
#: at construction and is never defaulted.
DAY_BOUNDARY_CLOCKS: dict[str, tuple[str, dtime | None, str]] = {
    "NY midnight": (
        "America/New_York", dtime(0, 0),
        "daily_bias R5 (00:00 New York); SYSTEM_SPEC GW-03"),
    "broker day": (
        "America/New_York", None,
        "daily_bias R10 names the convention; the roll time is "
        "broker-supplied (not corpus) and is required"),
    "futures open": (
        "America/New_York", dtime(18, 0),
        "CME Globex opens 18:00 ET (doc_0067; doc_0356)"),
    "cash open": (
        "America/New_York", dtime(9, 30),
        "NYSE cash open 09:30 ET (doc_0345; doc_0625)"),
}


def parse_day_clock(value: object) -> dtime:
    """Parse one declared boundary clock (``HH:MM[:SS]``); no implicit clock."""
    if isinstance(value, dtime):
        parsed = value
    elif isinstance(value, pd.Timestamp):
        parsed = value.time()
    elif isinstance(value, str):
        try:
            parsed = dtime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(
                f"{value!r} is not a clock; declare HH:MM or HH:MM:SS"
            ) from None
    else:
        raise TypeError(
            f"a day-boundary clock must be a time or 'HH:MM' string, got "
            f"{type(value).__name__}")
    if parsed.second or parsed.microsecond:
        raise ValueError(
            f"a day-boundary clock is minute-resolution; {value!r} carries "
            "seconds and would be silently truncated")
    return parsed


def _validate_day_zone(zone: object) -> str:
    text = str(zone).strip()
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"unknown day-boundary timezone {zone!r}; declare an IANA zone"
        ) from None
    return text


@dataclass(frozen=True, slots=True)
class DayBoundary:
    """One declared trading-day boundary (V04, GW-01).

    A day is the half-open interval ``[anchor, next anchor)`` on the declared
    wall clock, in a declared IANA zone.  The object is a *declaration*, not a
    preference: it is built from a V04 option (:func:`declare_day_boundary` /
    :func:`resolve_day_boundary`), it refuses an unknown option, and it carries
    the clock's provenance so no downstream artefact can present a repo guess
    as corpus doctrine (§14.1-6).

    The four options intentionally disagree: at one instant they can name four
    different days (daily_bias R10: "each can produce a different PO3 shape on
    the same date").  A run declares exactly one and reads it everywhere
    (GW-01); it never mixes two.
    """

    kind: str
    zone: str
    anchor_time: dtime
    source: str
    broker_supplied_clock: bool = False

    def __post_init__(self) -> None:
        kind = str(self.kind).strip()
        if kind not in DAY_BOUNDARY_OPTIONS:
            raise ValueError(
                f"unknown day boundary {self.kind!r}; V04 declares one of "
                f"{DAY_BOUNDARY_OPTIONS}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "zone", _validate_day_zone(self.zone))
        object.__setattr__(self, "anchor_time", parse_day_clock(self.anchor_time))
        source = str(self.source).strip()
        if not source:
            raise ValueError("a day boundary must carry its clock provenance")
        object.__setattr__(self, "source", source)

    # -- the one bucket arithmetic ----------------------------------------- #
    def _localize(self, day: date) -> pd.Timestamp:
        naive = pd.Timestamp.combine(day, self.anchor_time)
        return naive.tz_localize(
            self.zone, ambiguous=False, nonexistent="shift_forward")

    def _stamp(self, ts: object, *, input_tz: str | None = None,
               market: str | None = None) -> pd.Timestamp:
        stamp = pd.Timestamp(ts)
        if pd.isna(stamp):
            raise ValueError("a day boundary cannot be computed for NaT")
        if stamp.tzinfo is None:
            if input_tz is not None:
                zone = _validate_day_zone(input_tz)
            elif market is not None:
                zone = _validate_day_zone(_MARKET_TZ.get(market, "UTC"))
            else:
                raise ValueError(
                    "a naive timestamp needs the declared input timezone; pass "
                    "market= or input_tz= (the day boundary never assumes UTC)")
            stamp = stamp.tz_localize(
                zone, ambiguous=False, nonexistent="shift_forward")
        return stamp

    def bucket_start(self, ts: object, *, input_tz: str | None = None,
                     market: str | None = None) -> pd.Timestamp:
        """The anchor instant of the day containing ``ts`` (inclusive)."""
        local = self._stamp(ts, input_tz=input_tz,
                            market=market).tz_convert(self.zone)
        day = local.date()
        start = self._localize(day)
        if local < start:
            start = self._localize(day - timedelta(days=1))
        return start

    def bucket_end(self, ts: object, *, input_tz: str | None = None,
                   market: str | None = None) -> pd.Timestamp:
        """The exclusive end instant of the day containing ``ts``."""
        return self._next_anchor(
            self.bucket_start(ts, input_tz=input_tz, market=market))

    def _next_anchor(self, start: pd.Timestamp) -> pd.Timestamp:
        return self._localize(start.date() + timedelta(days=1))

    def bucket_bounds(self, ts: object, *, input_tz: str | None = None,
                      market: str | None = None
                      ) -> tuple[pd.Timestamp, pd.Timestamp]:
        """The half-open ``[start, end)`` day containing ``ts``."""
        start = self.bucket_start(ts, input_tz=input_tz, market=market)
        return start, self._next_anchor(start)

    def day_key(self, ts: object, *, input_tz: str | None = None,
                market: str | None = None) -> str:
        """The stable identity of the day containing ``ts``.

        The key is the anchor instant's ISO string (offset included), so two
        runs with the same declaration name the same day, a DST transition
        cannot collapse two days into one key, and a budget account keyed by it
        resets at the declared boundary rather than at UTC midnight.
        """
        return self.bucket_start(
            ts, input_tz=input_tz, market=market).isoformat()

    # -- frames ------------------------------------------------------------ #
    def _input_zone(self, market: str, input_tz: str | None) -> str:
        return _validate_day_zone(input_tz or _MARKET_TZ.get(market, "UTC"))

    def _starts(self, df: pd.DataFrame, *, market: str = "other",
                input_tz: str | None = None) -> pd.Series:
        """The anchor instant of every base bar, as a tz-aware Series."""
        zone = self._input_zone(market, input_tz)
        stamps = pd.to_datetime(df["date"])
        if stamps.dt.tz is None:
            aware = stamps.dt.tz_localize(
                zone, ambiguous=False, nonexistent="shift_forward")
        else:
            aware = stamps.dt.tz_convert(zone)
        local = aware.dt.tz_convert(self.zone)
        clock = pd.Timedelta(
            hours=self.anchor_time.hour, minutes=self.anchor_time.minute,
            seconds=self.anchor_time.second)
        # Local-midnight arithmetic on the already-parsed instants: converting
        # the dates back through strings is both slow and lossy.
        naive = local.dt.normalize().dt.tz_localize(None) + clock
        starts = naive.dt.tz_localize(
            self.zone, ambiguous=False, nonexistent="shift_forward")
        earlier = (starts > local).to_numpy()
        if earlier.any():
            previous = (naive - pd.Timedelta(days=1)).dt.tz_localize(
                self.zone, ambiguous=False, nonexistent="shift_forward")
            starts = starts.mask(earlier, previous)
        return starts.reset_index(drop=True)

    def day_keys(self, df: pd.DataFrame, *, market: str = "other",
                 input_tz: str | None = None) -> list[str]:
        """One day key per base bar, aligned to ``df`` (the budget account)."""
        return [start.isoformat()
                for start in self._starts(df, market=market, input_tz=input_tz)]

    def anchor_bars(self, df: pd.DataFrame, *, market: str = "other",
                    input_tz: str | None = None) -> np.ndarray:
        """Index of the first base bar in each bar's day (the day's anchor bar).

        The first bar of a bucket is the day's opening price as the run can
        know it; a budget reset and the daily anchor therefore share one
        boundary (GW-01) instead of one using UTC midnight and the other a
        session clock.
        """
        starts = self._starts(df, market=market, input_tz=input_tz)
        # Integer instants keep the group lookup cheap; the unit is whatever
        # resolution pandas parsed, and only equality is used here.
        values = starts.array.asi8
        out = np.empty(len(values), dtype=np.int64)
        first: dict[int, int] = {}
        for i, value in enumerate(values):
            key = int(value)
            anchor = first.get(key)
            if anchor is None:
                first[key] = i
                anchor = i
            out[i] = anchor
        return out

    def bucket_frame(self, df: pd.DataFrame, *, market: str = "other",
                     input_tz: str | None = None) -> pd.DataFrame:
        """Daily OHLCV bars bucketed on this boundary, labelled at their close.

        ``date`` is the bucket's exclusive end (its *known-at*), not a UTC
        midnight and not a left label.  The label is expressed in the input
        zone so it stays directly comparable with the base frame's timestamps;
        the last, still-forming bucket is left in place for
        ``setup._drop_incomplete_htf`` to drop, exactly like the repo's
        resample path.
        """
        if len(df) == 0:
            return df.copy()
        zone = self._input_zone(market, input_tz)
        stamps = pd.to_datetime(df["date"])
        naive_input = stamps.dt.tz is None
        starts = self._starts(df, market=market, input_tz=input_tz)
        work = pd.DataFrame({
            "bucket": starts.reset_index(drop=True),
            "open": df["open"].to_numpy(),
            "high": df["high"].to_numpy(),
            "low": df["low"].to_numpy(),
            "close": df["close"].to_numpy(),
            "volume": (df["volume"].to_numpy() if "volume" in df
                       else np.zeros(len(df), dtype=float)),
        })
        grouped = work.groupby("bucket", sort=True).agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"),
            volume=("volume", "sum"))
        ends = pd.DatetimeIndex([
            self._next_anchor(pd.Timestamp(start)) for start in grouped.index])
        labels = ends.tz_convert(zone)
        if naive_input:
            labels = labels.tz_localize(None)
        return pd.DataFrame({
            "date": labels,
            "open": grouped["open"].to_numpy(),
            "high": grouped["high"].to_numpy(),
            "low": grouped["low"].to_numpy(),
            "close": grouped["close"].to_numpy(),
            "volume": grouped["volume"].to_numpy(),
        }).reset_index(drop=True)

    def matches(self, other: "DayBoundary") -> bool:
        """Whether two declarations name the same boundary (kind, clock, zone)."""
        return (self.kind, self.zone, self.anchor_time) == (
            other.kind, other.zone, other.anchor_time)

    def to_public(self) -> dict[str, object]:
        """The JSON-safe declaration record."""
        return {
            "kind": self.kind,
            "zone": self.zone,
            "anchor_time": self.anchor_time.strftime("%H:%M"),
            "source": self.source,
            "broker_supplied_clock": bool(self.broker_supplied_clock),
            "variant_id": "V04",
        }


def declare_day_boundary(kind: str, *, broker_roll: object | None = None,
                         broker_zone: str | None = None) -> DayBoundary:
    """Build one V04 declaration; an option's clock is never guessed.

    ``broker day`` has no corpus clock (daily_bias R10 names the convention and
    stops there), so its ``broker_roll`` is required and its ``broker_zone`` may
    be supplied alongside it.  ``broker_roll``/``broker_zone`` on any other
    option refuse instead of being ignored: a declaration that carries a clock
    the model does not read is a silent second convention.
    """
    option = str(kind).strip()
    try:
        default_zone, clock, source = DAY_BOUNDARY_CLOCKS[option]
    except KeyError:
        raise ValueError(
            f"unknown day boundary {kind!r}; V04 declares one of "
            f"{DAY_BOUNDARY_OPTIONS}") from None
    if option == "broker day":
        if broker_roll is None:
            raise ValueError(
                "V04='broker day' has no corpus clock: declare the broker's "
                "roll time (broker_roll='HH:MM'); it is never defaulted")
        clock = parse_day_clock(broker_roll)
        zone = _validate_day_zone(broker_zone or default_zone)
        source = ("daily_bias R10 names the convention; the roll time and "
                  "zone are broker-supplied, not corpus")
        return DayBoundary(option, zone, clock, source,
                           broker_supplied_clock=True)
    if broker_roll is not None or broker_zone is not None:
        raise ValueError(
            f"broker_roll/broker_zone are only meaningful for V04='broker "
            f"day'; {option!r} has one corpus-attested clock")
    return DayBoundary(option, default_zone, clock, source)


def resolve_day_boundary(variant_config, *, broker_roll: object | None = None,
                         broker_zone: str | None = None) -> DayBoundary:
    """Read V04 from the one register and build its boundary.

    This is the only read path for the day boundary (A7).  An undeclared V04
    raises :class:`~ict_trader.variants.VariantUndeclared` carrying
    ``NT_VARIANT_UNDECLARED``; no option is selected on the caller's behalf
    (SYSTEM_SPEC §14.1-4).
    """
    option = variant_config.read("V04")
    return declare_day_boundary(option, broker_roll=broker_roll,
                                broker_zone=broker_zone)


# --------------------------------------------------------------------------- #
# P1-04 — the midnight open as a price object, V05 polarity, DB-03
# --------------------------------------------------------------------------- #
#: V05 options, read from the one register (A7).  The strings are not re-typed
#: here: ``majority`` is DB-01 and ``inverted`` is DB-02.
DAILY_POLARITY_OPTIONS: tuple[str, ...] = tuple(read_variant_spec("V05").options)

#: The source of each V05 reading.  DB-01 is attested by five sources; DB-02 is
#: one operational source that contradicts them.  §14.3 makes the fork
#: ``BLOCK``, so neither reading is a default and an undeclared V05 refuses.
DAILY_POLARITY_SOURCES: dict[str, str] = {
    "majority": ("DB-01 [SN-D01]: bullish iff price < midnight open; bearish "
                 "iff price > midnight open"),
    "inverted": ("DB-02 [SN-D02]: below the midnight open => bearish; above "
                 "=> bullish (one source; contradicts DB-01)"),
}

#: DB-03's one attested two-price reading.  It compares the midnight open with
#: the **daily CE**, not the current price with a CE: ``daily_bias`` C5 warns
#: that collapsing the two into one ``price > ce`` boolean silently merges
#: three rules.
DAILY_OPEN_VS_CE_SOURCE: str = (
    "DB-03 [SN-D03]: midnight_open > daily_CE => the day opens in premium => "
    "a bearish Judas from premium toward discount is more likely")

#: The two outcomes of the DB-03 comparison.  §14.3 makes DB-03 a ``MODE``:
#: both outcomes ship.  The corpus states the premium branch only, so the
#: discount branch carries no invented directional mirror.
DAILY_OPEN_LOCATIONS: tuple[str, ...] = ("premium", "discount")


@dataclass(frozen=True, slots=True)
class MidnightOpen:
    """The declared day's opening **price**, not a timestamp (P1-04).

    ``daily_bias`` R5 makes the day's unit a price -- "the 00:00 New York
    opening price, ICT's anchor for the 'true day'" -- and ``premium_discount``
    R33 separates the two objects: the true day is a *time period*, the
    midnight open is a *price level*.  This type carries both, so no consumer
    can mistake the instant for the price.

    The price is the open of the first base bar of the declared day.  That bar
    must start **at** the declared V04 boundary; otherwise its open is not the
    boundary's opening price and :func:`midnight_open_from_anchor` reports
    ``NT_DATA_MISSING`` instead of authoring a level (X1).

    ``kind``/``zone``/``anchor_time`` record the V04 declaration the price was
    read under (GW-01: exactly one boundary, never mixed).  When V04 is
    ``NY midnight`` this is literally the corpus's midnight open; under another
    V04 option it is that declared day's opening price, and
    :attr:`is_midnight_open` says so instead of renaming it.
    """

    price: float
    at: pd.Timestamp
    day: str
    bar: int
    kind: str
    zone: str
    anchor_time: dtime
    source: str
    variant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        price = float(self.price)
        if not np.isfinite(price):
            raise ValueError(
                f"the midnight open must be a finite price, got {self.price!r}")
        object.__setattr__(self, "price", price)
        at = pd.Timestamp(self.at)
        if pd.isna(at):
            raise ValueError("the midnight open instant is NaT")
        if at.tzinfo is None:
            raise ValueError(
                "the midnight open instant must be tz-aware; a naive stamp "
                "would need an undeclared input timezone")
        object.__setattr__(self, "at", at)
        day = str(self.day).strip()
        if not day:
            raise ValueError("the midnight open must name its day key")
        object.__setattr__(self, "day", day)
        bar = int(self.bar)
        if bar < 0:
            raise ValueError(f"the anchor bar must be >= 0, got {self.bar!r}")
        object.__setattr__(self, "bar", bar)
        kind = str(self.kind).strip()
        if kind not in DAY_BOUNDARY_OPTIONS:
            raise ValueError(
                f"unknown V04 day boundary {self.kind!r}; declare one of "
                f"{DAY_BOUNDARY_OPTIONS}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "zone", _validate_day_zone(self.zone))
        object.__setattr__(self, "anchor_time", parse_day_clock(self.anchor_time))
        source = str(self.source).strip()
        if not source:
            raise ValueError("the midnight open must carry its provenance")
        object.__setattr__(self, "source", source)
        ids: list[str] = []
        raw_ids = ((self.variant_ids,) if isinstance(self.variant_ids, str)
                   else self.variant_ids)
        for raw in raw_ids:
            variant_id = str(raw).strip().upper()
            read_variant_spec(variant_id)  # refuse an undeclared V## id
            if variant_id not in ids:
                ids.append(variant_id)
        object.__setattr__(self, "variant_ids", tuple(ids))

    @property
    def is_midnight_open(self) -> bool:
        """Whether V04 named the corpus's NY-midnight anchor for this price."""
        return self.kind == "NY midnight"

    def is_knowable(self, bar: int) -> bool:
        """The opening price is observable from its anchor bar onward.

        The open prints at the bar's first trade and does not repaint, so it is
        not withheld until the bar closes; a bar *before* the anchor cannot know
        it (CB-07).
        """
        return int(bar) >= self.bar

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """A constructed price object is complete; nothing is filled later."""
        return ()

    def to_public(self) -> dict[str, object]:
        """The JSON-safe contract record (price plus its anchor instant)."""
        return {
            "price": self.price,
            "at": self.at.isoformat(),
            "day": self.day,
            "bar": int(self.bar),
            "kind": self.kind,
            "zone": self.zone,
            "anchor_time": self.anchor_time.strftime("%H:%M"),
            "source": self.source,
            "is_midnight_open": self.is_midnight_open,
            "variant_id": "V04",
            "variant_ids": list(self.variant_ids),
            "missing_fields": list(self.missing_fields),
        }


def midnight_open_from_anchor(boundary: DayBoundary, *, price: object,
                              stamp: object, bar: int,
                              market: str = "other",
                              input_tz: str | None = None,
                              variant_ids: Iterable[str] = ("V04",)
                              ) -> tuple[MidnightOpen | None, Refusal | None]:
    """Build the opening price from a known anchor bar, checking alignment.

    The check is the point of the function: the first bar of a day is only the
    day's opening price when it starts exactly at the declared boundary.  A
    coarse frame whose first bar opens at 02:00 does not open the 00:00 day, so
    its open is reported as missing rather than passed off as the anchor (X1).
    """
    if not isinstance(boundary, DayBoundary):
        raise TypeError(
            "a midnight open needs the declared DayBoundary object, not a "
            f"{type(boundary).__name__}")
    local = boundary._stamp(stamp, input_tz=input_tz,
                            market=market).tz_convert(boundary.zone)
    start = boundary.bucket_start(stamp, input_tz=input_tz, market=market)
    if local != start:
        return None, Refusal(
            RefusalCode.NT_DATA_MISSING,
            detail=(f"the first bar of the declared {boundary.kind!r} day starts "
                    f"at {local.isoformat()}, not at the boundary "
                    f"{boundary.anchor_time.strftime('%H:%M')} "
                    f"{boundary.zone}; its open is not the day's opening price"))
    opening = MidnightOpen(
        price=price, at=local, day=start.isoformat(), bar=int(bar),
        kind=boundary.kind, zone=boundary.zone,
        anchor_time=boundary.anchor_time, source=boundary.source,
        variant_ids=variant_ids)
    return opening, None


def opening_price(boundary: DayBoundary, df: pd.DataFrame, bar: int, *,
                  market: str = "other", input_tz: str | None = None
                  ) -> tuple[MidnightOpen | None, Refusal | None]:
    """The declared day's opening price at ``bar`` as a :class:`MidnightOpen`.

    This is the frame-level read path: it locates the first base bar of the day
    containing ``bar`` (the same anchor the P1-03 budget account uses) and
    returns its open together with the anchor instant and the V04 provenance.
    """
    if not isinstance(boundary, DayBoundary):
        raise TypeError(
            "a midnight open needs the declared DayBoundary object, not a "
            f"{type(boundary).__name__}")
    index = int(bar)
    if index < 0 or index >= len(df):
        return None, Refusal(
            RefusalCode.NT_DATA_MISSING,
            detail=f"bar {index} is outside the {len(df)}-bar frame")
    anchors = boundary.anchor_bars(df, market=market, input_tz=input_tz)
    anchor = int(anchors[index])
    return midnight_open_from_anchor(
        boundary, price=df["open"].iloc[anchor],
        stamp=df["date"].iloc[anchor], bar=anchor, market=market,
        input_tz=input_tz)


def resolve_daily_polarity(variant_config) -> str:
    """Read V05 from the one register (A7).

    An undeclared V05 raises
    :class:`~ict_trader.variants.VariantUndeclared` with
    ``NT_VARIANT_UNDECLARED``.  No reading is selected on the caller's behalf:
    DB-01 and DB-02 give opposite trade sides on the same bar (§14.3 ``BLOCK``).
    """
    option = variant_config.read("V05")
    if option not in DAILY_POLARITY_OPTIONS:
        raise ValueError(
            f"V05 returned {option!r}, which is not one of "
            f"{DAILY_POLARITY_OPTIONS}")
    return option


def daily_polarity_mode(variant_config) -> tuple[str | None, Refusal | None]:
    """The fail-closed result form of :func:`resolve_daily_polarity` (A6).

    Declared: ``(option, None)``.  Undeclared: ``(None, Refusal)`` carrying
    ``NT_VARIANT_UNDECLARED``; the caller never receives a substitute reading.
    """
    refusal = variant_config.check("V05")
    if refusal is not None:
        return None, refusal
    return resolve_daily_polarity(variant_config), None


def daily_open_polarity(opening: MidnightOpen, price: object, *,
                        polarity: str
                        ) -> tuple[BiasDirection | None, Refusal | None]:
    """DB-01/DB-02: the one declared V05 reading of one price against the open.

    ``majority`` (DB-01) reads ``price < open`` as bullish and ``price > open``
    as bearish; ``inverted`` (DB-02) reads the opposite boolean.  Both rules
    declare ``<`` and ``>`` only, so equality has **no branch**: it returns a
    typed ``NT_NO_BIAS`` refusal rather than picking a side or inventing a
    tolerance.  The midnight open is also not the dealing-range EQ, so an
    equality is deliberately not reported as ``NT_AT_EQ`` (``premium_discount``
    R12: this divider has no EQ).
    """
    if not isinstance(opening, MidnightOpen):
        raise TypeError(
            "DB-01/DB-02 compare a price against the MidnightOpen object, not "
            f"a {type(opening).__name__}; the only timestamp is not a price")
    mode = str(polarity).strip()
    if mode not in DAILY_POLARITY_OPTIONS:
        raise ValueError(
            f"unknown V05 daily polarity {polarity!r}; declare one of "
            f"{DAILY_POLARITY_OPTIONS}")
    value = float(price)
    if not np.isfinite(value):
        raise ValueError(f"the compared price must be finite, got {price!r}")
    if value == opening.price:
        return None, Refusal(
            RefusalCode.NT_NO_BIAS,
            detail=(f"price {value!r} equals the midnight open "
                    f"{opening.price!r}; {mode!r} declares only < and >, so "
                    "there is no equality reading (no tolerance is invented)"))
    below = value < opening.price
    if mode == "majority":
        reading = BiasDirection.BULL if below else BiasDirection.BEAR
    else:  # inverted: DB-02 is the opposite boolean on the same bar
        reading = BiasDirection.BEAR if below else BiasDirection.BULL
    return reading, None


@dataclass(frozen=True, slots=True)
class OpenVsDailyCE:
    """DB-03: the midnight open against the **daily CE** -- two prices.

    ``daily_bias`` C5 and ``premium_discount`` R33 give this comparison its own
    identity: it is not DB-01's ``price`` vs ``open`` boolean, and it is not
    ``price > ce``.  ``location`` is the relation of the two prices
    (``premium`` when the open is above the daily CE, ``discount`` when below).
    §14.3 marks DB-03 ``MODE``, so both outcomes ship.

    The corpus states one directional reading only -- an open in premium makes a
    bearish Judas more likely -- so ``judas_lean`` is bearish on the premium
    branch and explicitly absent on the discount branch.  The mirror is **not**
    invented: :attr:`missing_fields` names the absent lean.
    """

    midnight_open: MidnightOpen
    daily_ce: float
    location: str
    judas_lean: BiasDirection | None
    judas_lean_source: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.midnight_open, MidnightOpen):
            raise TypeError(
                "DB-03's first price must be the MidnightOpen object; a bare "
                "float or timestamp is a different comparison")
        ce = float(self.daily_ce)
        if not np.isfinite(ce):
            raise ValueError(f"the daily CE must be finite, got {self.daily_ce!r}")
        object.__setattr__(self, "daily_ce", ce)
        location = str(self.location).strip()
        if location not in DAILY_OPEN_LOCATIONS:
            raise ValueError(
                f"unknown DB-03 location {self.location!r}; the two-price "
                f"comparison has {DAILY_OPEN_LOCATIONS}")
        object.__setattr__(self, "location", location)
        lean = None if self.judas_lean is None else parse_bias(self.judas_lean)
        if location == "premium":
            if lean is not BiasDirection.BEAR:
                raise ValueError(
                    "DB-03's premium branch is the corpus's bearish Judas lean")
        elif lean is not None:
            raise ValueError(
                "DB-03 attests the premium branch only; the discount branch "
                "may not carry an invented directional mirror")
        object.__setattr__(self, "judas_lean", lean)
        source = None if self.judas_lean_source is None else str(
            self.judas_lean_source).strip()
        if location == "premium" and not source:
            raise ValueError("the premium branch must carry its DB-03 citation")
        if location == "discount" and source:
            raise ValueError(
                "the discount branch has no DB-03 citation to carry")
        object.__setattr__(self, "judas_lean_source", source or None)

    @property
    def compares(self) -> tuple[str, str]:
        """The two price operands, named so no consumer substitutes a third."""
        return ("midnight_open.price", "daily_ce")

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """The absent directional lean is named, never filled in with a mirror."""
        return () if self.judas_lean is not None else ("judas_lean",)

    def to_public(self) -> dict[str, object]:
        """The JSON-safe two-price comparison record."""
        return {
            "rule": "DB-03",
            "midnight_open": {
                "price": self.midnight_open.price,
                "day": self.midnight_open.day,
                "bar": int(self.midnight_open.bar),
            },
            "daily_ce": self.daily_ce,
            "location": self.location,
            "judas_lean": (serialize_bias(self.judas_lean)
                           if self.judas_lean is not None else ""),
            "judas_lean_source": self.judas_lean_source,
            "compares": list(self.compares),
            "missing_fields": list(self.missing_fields),
        }


def open_vs_daily_ce(opening: MidnightOpen, daily_ce: object
                     ) -> tuple[OpenVsDailyCE | None, Refusal | None]:
    """DB-03: compare the midnight open with the daily CE (two prices).

    The first operand is the :class:`MidnightOpen` price, never the current
    price: passing the current price here would silently recreate the
    ``price > ce`` boolean that C5 forbids.  An exact equality is ``NT_AT_EQ``
    because the daily CE is the dealing-range midpoint and DB-03 has no
    equality branch.
    """
    if not isinstance(opening, MidnightOpen):
        raise TypeError(
            "DB-03 compares two prices: the first must be the MidnightOpen "
            f"object, got {type(opening).__name__}")
    ce = float(daily_ce)
    if not np.isfinite(ce):
        raise ValueError(f"the daily CE must be finite, got {daily_ce!r}")
    if opening.price == ce:
        return None, Refusal(
            RefusalCode.NT_AT_EQ,
            detail=(f"the midnight open {opening.price!r} equals the daily CE "
                    f"{ce!r}; DB-03 compares two prices and declares no "
                    "equality branch (no tolerance is invented)"))
    if opening.price > ce:
        comparison = OpenVsDailyCE(
            opening, ce, "premium", BiasDirection.BEAR, DAILY_OPEN_VS_CE_SOURCE)
    else:
        comparison = OpenVsDailyCE(opening, ce, "discount", None, None)
    return comparison, None


# --------------------------------------------------------------------------- #
# P1-05 — weekly state (WB-01..WB-06), V06 anchor fork, Tue/Wed scheduler
# --------------------------------------------------------------------------- #
#: V06 options, read from the one register (A7).  The fork is the P/D anchor:
#: the range-anchored weekly CE (WB-01) versus the time-anchored weekly open
#: (WB-04).  ``premium_discount`` C2 and ``weekly_bias`` R16 state that the two
#: anchors disagree on any week where price is above the open but below the
#: range midpoint, so a run declares one and never averages them.
WEEKLY_ANCHOR_OPTIONS: tuple[str, ...] = tuple(read_variant_spec("V06").options)

#: The citation each V06 anchor travels with.  Neither is a default.
WEEKLY_ANCHOR_SOURCES: dict[str, str] = {
    "weekly CE": (
        "WB-01 [SN-W01/SN-W02]: CE_wk = (PWH + PWL) / 2; below CE = bullish, "
        "above CE = bearish; price == CE_wk has no branch"),
    "weekly open": (
        "WB-04 [SN-W08/R15/R16]: below the weekly open is discount (the Judas "
        "side); above is premium; price == weekly open has no branch"),
}

#: The two corpus-attested weekly-open clocks (``weekly_bias`` R31 / SN-W11).
#:
#: §14.2 has no V## row for the week field, so the choice is a declared run
#: input rather than a register fork: :data:`WEEK_OPEN_OPTIONS` is the complete
#: attested set, and :func:`declare_week_open` refuses an unknown name.  There is
#: deliberately **no default** -- the Sunday 17:00 forex open and the Monday
#: 09:30 equities open are different weeks on the same bars.
WEEK_OPEN_CLOCKS: dict[str, tuple[int, dtime, str, str]] = {
    "sunday_1700_ny": (
        6, dtime(17, 0), "America/New_York",
        "weekly_bias R31/SN-W11: forex week open Sunday 17:00 America/New_York"),
    "monday_0930_ny": (
        0, dtime(9, 30), "America/New_York",
        "weekly_bias R31/SN-W11: equities week open Monday 09:30 America/New_York"),
}
WEEK_OPEN_OPTIONS: tuple[str, ...] = tuple(WEEK_OPEN_CLOCKS)

#: ``weekly_bias`` R20: the corpus states **no** weekly-bias probability, hit
#: rate or expectancy.  This constant is the declaration of that absence; no
#: number is ever produced or placed on :class:`WeeklyState`.
WEEKLY_PROBABILITY_STATUS = "missing"
WEEKLY_PROBABILITY_SOURCE = (
    "weekly_bias R20: no weekly-bias probability/frequency exists in the "
    "corpus; none is invented here")


def _local_stamps(df: pd.DataFrame, zone: str, market: str = "other",
                  input_tz: str | None = None) -> pd.Series:
    """Every base timestamp, aware and converted to ``zone``.

    Naive stamps are read in ``input_tz`` when supplied, else in the market's
    native zone (UTC for crypto).  Aware stamps are converted directly, so the
    caller's input convention never moves the instant.
    """
    stamps = pd.to_datetime(df["date"])
    source = _validate_day_zone(input_tz or _MARKET_TZ.get(market, "UTC"))
    if stamps.dt.tz is None:
        aware = stamps.dt.tz_localize(
            source, ambiguous=False, nonexistent="shift_forward")
    else:
        aware = stamps.dt.tz_convert(source)
    return aware.dt.tz_convert(zone)


@dataclass(frozen=True, slots=True)
class WeekOpen:
    """One declared week boundary and its opening clock (WB-06 / SN-W11).

    The object is a declaration, not a preference: it is built from one of the
    two attested clocks and carries the citation.  ``week_start`` is the most
    recent anchor instant at or before a timestamp; the *week open price* is the
    open of the first base bar that starts exactly at that instant (a frame that
    begins mid-week has no observable week open -- see
    :meth:`WeeklyIndex.state`).
    """

    kind: str
    zone: str
    weekday: int
    anchor_time: dtime
    source: str

    def __post_init__(self) -> None:
        kind = str(self.kind).strip()
        if kind not in WEEK_OPEN_OPTIONS:
            raise ValueError(
                f"unknown week-open clock {self.kind!r}; declare one of "
                f"{WEEK_OPEN_OPTIONS}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "zone", _validate_day_zone(self.zone))
        weekday = int(self.weekday)
        if not 0 <= weekday <= 6:
            raise ValueError(
                f"a week-open weekday is 0 (Monday) .. 6 (Sunday), got "
                f"{self.weekday!r}")
        object.__setattr__(self, "weekday", weekday)
        object.__setattr__(self, "anchor_time", parse_day_clock(self.anchor_time))
        source = str(self.source).strip()
        if not source:
            raise ValueError("a week-open clock must carry its provenance")
        object.__setattr__(self, "source", source)

    def _localize(self, day: date) -> pd.Timestamp:
        naive = pd.Timestamp.combine(day, self.anchor_time)
        return naive.tz_localize(
            self.zone, ambiguous=False, nonexistent="shift_forward")

    def stamp(self, ts: object, *, input_tz: str | None = None,
              market: str = "other") -> pd.Timestamp:
        """Convert one timestamp to the declared zone (never assumes NY)."""
        stamp = pd.Timestamp(ts)
        if pd.isna(stamp):
            raise ValueError("a week boundary cannot be computed for NaT")
        if stamp.tzinfo is None:
            source = _validate_day_zone(
                input_tz or _MARKET_TZ.get(market, "UTC"))
            stamp = stamp.tz_localize(
                source, ambiguous=False, nonexistent="shift_forward")
        return stamp.tz_convert(self.zone)

    def week_start(self, ts: object, *, input_tz: str | None = None,
                   market: str = "other") -> pd.Timestamp:
        """The most recent week anchor at or before ``ts`` (inclusive)."""
        local = self.stamp(ts, input_tz=input_tz, market=market)
        days_back = (local.weekday() - self.weekday) % 7
        day = local.date() - timedelta(days=days_back)
        start = self._localize(day)
        if start > local:
            start = self._localize(day - timedelta(days=7))
        return start

    def week_end(self, start: pd.Timestamp) -> pd.Timestamp:
        """The exclusive end of the week beginning at ``start``."""
        return self._localize(pd.Timestamp(start).date() + timedelta(days=7))

    def week_key(self, ts: object, *, input_tz: str | None = None,
                 market: str = "other") -> str:
        """The stable identity of the week containing ``ts``."""
        return self.week_start(
            ts, input_tz=input_tz, market=market).isoformat()

    def week_starts(self, df: pd.DataFrame, *, market: str = "other",
                    input_tz: str | None = None) -> pd.Series:
        """One week-start instant per base bar, aligned to ``df``."""
        local = _local_stamps(df, self.zone, market, input_tz)
        days_back = (local.dt.weekday - self.weekday) % 7
        base = (local.dt.normalize().dt.tz_localize(None)
                - pd.to_timedelta(days_back, unit="D"))
        naive = base + pd.Timedelta(
            hours=self.anchor_time.hour,
            minutes=self.anchor_time.minute,
            seconds=self.anchor_time.second)
        starts = naive.dt.tz_localize(
            self.zone, ambiguous=False, nonexistent="shift_forward")
        later = (starts > local).to_numpy()
        if later.any():
            previous = (naive - pd.Timedelta(days=7)).dt.tz_localize(
                self.zone, ambiguous=False, nonexistent="shift_forward")
            starts = starts.mask(later, previous)
        return starts.reset_index(drop=True)

    def anchor_bars(self, df: pd.DataFrame, *, market: str = "other",
                    input_tz: str | None = None) -> np.ndarray:
        """Index of the first base bar in each bar's week (the week's open bar)."""
        starts = self.week_starts(df, market=market, input_tz=input_tz)
        values = starts.array.asi8
        out = np.empty(len(values), dtype=np.int64)
        first: dict[int, int] = {}
        for i, value in enumerate(values):
            key = int(value)
            anchor = first.get(key)
            if anchor is None:
                first[key] = i
                anchor = i
            out[i] = anchor
        return out

    def bucket_frame(self, df: pd.DataFrame, *, market: str = "other",
                     input_tz: str | None = None) -> pd.DataFrame:
        """Weekly OHLCV bars on this clock, labelled at their exclusive end.

        ``aligned`` is true when the bucket's first base bar starts exactly at
        the week anchor (otherwise the week's earlier bars are absent and its
        high/low cannot be trusted); ``complete`` is true when the bucket's end
        is at or before the last base bar, so a still-forming week is never
        emitted.  Both flags are part of the object, not discarded: PWH/PWL must
        come from a complete, aligned week.
        """
        columns = ["start", "end", "date", "open", "high", "low", "close",
                   "volume", "aligned", "complete"]
        if len(df) == 0:
            return pd.DataFrame(columns=columns)
        starts = self.week_starts(df, market=market, input_tz=input_tz)
        local = _local_stamps(df, self.zone, market, input_tz)
        work = pd.DataFrame({
            "bucket": starts.reset_index(drop=True),
            "date": local.reset_index(drop=True),
            "open": df["open"].to_numpy(float),
            "high": df["high"].to_numpy(float),
            "low": df["low"].to_numpy(float),
            "close": df["close"].to_numpy(float),
            "volume": (df["volume"].to_numpy(float) if "volume" in df
                       else np.zeros(len(df), dtype=float)),
        })
        last = local.iloc[-1]
        rows = []
        for key, group in work.groupby("bucket", sort=True):
            start = pd.Timestamp(key)
            end = self.week_end(start)
            rows.append({
                "start": start,
                "end": end,
                "date": end,
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": float(group["volume"].sum()),
                "aligned": pd.Timestamp(group["date"].iloc[0]) == start,
                "complete": end <= last,
            })
        return pd.DataFrame(rows, columns=columns)

    def to_public(self) -> dict[str, object]:
        """The JSON-safe week-clock declaration record."""
        return {
            "kind": self.kind,
            "zone": self.zone,
            "weekday": self.weekday,
            "anchor_time": self.anchor_time.strftime("%H:%M"),
            "source": self.source,
        }


def declare_week_open(kind: str) -> WeekOpen:
    """Build one declared week-open clock; no clock is ever guessed."""
    option = str(kind).strip()
    try:
        weekday, clock, zone, source = WEEK_OPEN_CLOCKS[option]
    except KeyError:
        raise ValueError(
            f"unknown week-open clock {kind!r}; declare one of "
            f"{WEEK_OPEN_OPTIONS}") from None
    return WeekOpen(option, zone, weekday, clock, source)


def monthly_bars(df: pd.DataFrame, zone: str, *, market: str = "other",
                 input_tz: str | None = None) -> pd.DataFrame:
    """Monthly OHLCV bars in ``zone``, labelled at the exclusive month end.

    The monthly tier exists only to answer the WB-03 conjunct: monthly HH/HL.
    The calendar month is a repo choice (the corpus fixes no month boundary);
    the same ``aligned``/``complete`` flags as :meth:`WeekOpen.bucket_frame`
    keep a partial first month and a still-forming month out of the read.
    """
    columns = ["start", "end", "date", "open", "high", "low", "close",
               "volume", "aligned", "complete"]
    if len(df) == 0:
        return pd.DataFrame(columns=columns)
    zone = _validate_day_zone(zone)
    local = _local_stamps(df, zone, market, input_tz)
    # The month identity is a local-calendar label; dropping the tz here only
    # avoids pandas' PeriodArray warning and does not move the instant used for
    # the bucket's `start`/`end` labels below.
    periods = local.dt.tz_localize(None).dt.to_period("M")
    work = pd.DataFrame({
        "period": [str(period) for period in periods],
        "date": local.reset_index(drop=True),
        "open": df["open"].to_numpy(float),
        "high": df["high"].to_numpy(float),
        "low": df["low"].to_numpy(float),
        "close": df["close"].to_numpy(float),
        "volume": (df["volume"].to_numpy(float) if "volume" in df
                   else np.zeros(len(df), dtype=float)),
    })
    last = local.iloc[-1]
    first_period = pd.Period(local.iloc[0].tz_localize(None), freq="M")
    first_month_start = first_period.start_time.tz_localize(
        zone, ambiguous=False, nonexistent="shift_forward")
    rows = []
    for key, group in work.groupby("period", sort=True):
        period = pd.Period(key, freq="M")
        start = period.start_time.tz_localize(
            zone, ambiguous=False, nonexistent="shift_forward")
        end = (period + 1).start_time.tz_localize(
            zone, ambiguous=False, nonexistent="shift_forward")
        first_stamp = pd.Timestamp(group["date"].iloc[0])
        # Only the frame's *first* month can be partial.  A later month is
        # whole even when its first bar is the market's first trade (09:30 for
        # cash equities), so alignment is not a midnight test there; the first
        # month is aligned only when a bar really opens at its boundary.
        aligned = start > first_month_start or first_stamp == start
        rows.append({
            "start": start,
            "end": end,
            "date": end,
            "open": float(group["open"].iloc[0]),
            "high": float(group["high"].max()),
            "low": float(group["low"].min()),
            "close": float(group["close"].iloc[-1]),
            "volume": float(group["volume"].sum()),
            "aligned": aligned,
            "complete": end <= last,
        })
    return pd.DataFrame(rows, columns=columns)


def structure_from_bars(bars: pd.DataFrame) -> BiasDirection | None:
    """WB-02 monthly/weekly HH/HL on the last two completed bars.

    ``bullish`` requires higher high **and** higher low; ``bearish`` requires
    lower high **and** lower low.  A mixed pair is ``None`` -- the corpus gives
    no direction to it, and a neutral spelling is not invented here.
    """
    if bars is None or len(bars) < 2:
        return None
    previous = bars.iloc[-2]
    latest = bars.iloc[-1]
    hh = float(latest["high"]) > float(previous["high"])
    hl = float(latest["low"]) > float(previous["low"])
    lh = float(latest["high"]) < float(previous["high"])
    ll = float(latest["low"]) < float(previous["low"])
    if hh and hl:
        return BiasDirection.BULL
    if lh and ll:
        return BiasDirection.BEAR
    return None


def weekly_ce(pwh: object, pwl: object) -> float:
    """WB-01 arithmetic: ``CE_wk = (PWH + PWL) / 2`` (no tolerance band)."""
    high = float(pwh)
    low = float(pwl)
    if not (np.isfinite(high) and np.isfinite(low)):
        raise ValueError(f"PWH/PWL must be finite, got {pwh!r}/{pwl!r}")
    if high <= low:
        raise ValueError(
            f"PWH ({high!r}) must be above PWL ({low!r}); a degenerate week "
            "has no CE (WB-01)")
    return (high + low) / 2.0


@dataclass(frozen=True, slots=True)
class WeeklyRange:
    """The prior week's ``[PWL, PWH]`` and its CE (WB-01)."""

    week_key: str
    pwh: float
    pwl: float

    def __post_init__(self) -> None:
        week_key = str(self.week_key).strip()
        if not week_key:
            raise ValueError("a weekly range must name its prior week")
        object.__setattr__(self, "week_key", week_key)
        pwh, pwl = float(self.pwh), float(self.pwl)
        object.__setattr__(self, "pwh", pwh)
        object.__setattr__(self, "pwl", pwl)
        weekly_ce(pwh, pwl)  # validates finite and ordered

    @property
    def ce(self) -> float:
        """The weekly consequent encroachment (WB-01)."""
        return weekly_ce(self.pwh, self.pwl)

    @property
    def span(self) -> float:
        return self.pwh - self.pwl

    def to_public(self) -> dict[str, object]:
        return {
            "week_key": self.week_key,
            "pwh": self.pwh,
            "pwl": self.pwl,
            "ce": self.ce,
            "source": "WB-01 [SN-W01]: prior week's wick extremes; CE=(PWH+PWL)/2",
        }


@dataclass(frozen=True, slots=True)
class WeeklyState:
    """The current week's state object: week open, prior range, HH/HL, gate.

    The corpus's six WB rows are carried as typed fields instead of a per-call
    boolean:

    * ``range``  -- the prior **complete** week's PWH/PWL and ``CE_wk`` (WB-01);
    * ``weekly_structure`` -- WB-02 HH/HL over the last two complete weeks;
    * ``monthly_structure`` -- the monthly leg of the WB-03 conjunct;
    * ``week_open`` -- the time-anchored anchor of the V06 fork (WB-04);
    * the WB-05 sequence and WB-06 scheduler are separate objects
      (:class:`WeeklySequence`, :class:`WeeklySchedule`) because they are
      state machines over events, not snapshots.

    The object carries **no probability, confidence or score**: ``weekly_bias``
    R20 records that the corpus defines none for a weekly bias, and a number
    here would be read as a calibrated forecast.  :attr:`missing_fields` names
    the absent legs instead.
    """

    scope: str
    as_of: int
    price: float
    week_key: str
    week_start: pd.Timestamp
    week_open: float
    week_open_bar: int
    range: WeeklyRange
    weekly_structure: BiasDirection | None
    monthly_structure: BiasDirection | None
    variant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        scope = str(self.scope).strip()
        if not scope:
            raise ValueError("WeeklyState.scope must name the weekly tier")
        object.__setattr__(self, "scope", scope)
        as_of = int(self.as_of)
        if as_of < 0:
            raise ValueError(f"WeeklyState.as_of must be a bar index, got {self.as_of!r}")
        object.__setattr__(self, "as_of", as_of)
        price = float(self.price)
        if not np.isfinite(price):
            raise ValueError(f"the weekly-state price must be finite, got {self.price!r}")
        object.__setattr__(self, "price", price)
        week_key = str(self.week_key).strip()
        if not week_key:
            raise ValueError("WeeklyState.week_key must name the current week")
        object.__setattr__(self, "week_key", week_key)
        start = pd.Timestamp(self.week_start)
        if pd.isna(start) or start.tzinfo is None:
            raise ValueError(
                "WeeklyState.week_start must be a tz-aware week anchor")
        object.__setattr__(self, "week_start", start)
        week_open = float(self.week_open)
        if not np.isfinite(week_open):
            raise ValueError(
                f"the weekly open must be a finite price, got {self.week_open!r}")
        object.__setattr__(self, "week_open", week_open)
        bar = int(self.week_open_bar)
        if bar < 0:
            raise ValueError(f"week_open_bar must be >= 0, got {self.week_open_bar!r}")
        object.__setattr__(self, "week_open_bar", bar)
        if not isinstance(self.range, WeeklyRange):
            raise TypeError("WeeklyState.range must be a WeeklyRange")
        for name in ("weekly_structure", "monthly_structure"):
            value = getattr(self, name)
            if value is None:
                continue
            reading = parse_bias(value)
            if reading not in (BiasDirection.BULL, BiasDirection.BEAR):
                raise ValueError(
                    f"WeeklyState.{name} is HH/HL or LH/LL, not {reading.value!r}")
            object.__setattr__(self, name, reading)
        ids: list[str] = []
        raw_ids = ((self.variant_ids,) if isinstance(self.variant_ids, str)
                   else self.variant_ids)
        for raw in raw_ids:
            variant_id = str(raw).strip().upper()
            read_variant_spec(variant_id)  # refuse an undeclared V## id
            if variant_id not in ids:
                ids.append(variant_id)
        object.__setattr__(self, "variant_ids", tuple(ids))

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Absent legs, named rather than fabricated (no probability field)."""
        missing = []
        if self.weekly_structure is None:
            missing.append("weekly_structure")
        if self.monthly_structure is None:
            missing.append("monthly_structure")
        if not self.variant_ids:
            missing.append("variant_ids")
        return tuple(missing)

    def to_public(self) -> dict[str, object]:
        """The JSON-safe weekly record; no probability/confidence key."""
        return {
            "scope": self.scope,
            "as_of": int(self.as_of),
            "price": self.price,
            "week_key": self.week_key,
            "week_start": self.week_start.isoformat(),
            "week_open": self.week_open,
            "week_open_bar": int(self.week_open_bar),
            "weekly_range": self.range.to_public(),
            "weekly_structure": (
                "" if self.weekly_structure is None
                else serialize_bias(self.weekly_structure)),
            "weekly_structure_source": (
                "WB-02 [SN-W03]: HH/HL over the last two complete weeks"),
            "monthly_structure": (
                "" if self.monthly_structure is None
                else serialize_bias(self.monthly_structure)),
            "monthly_structure_source": (
                "WB-03 [SN-W04]: monthly HH/HL must agree with the weekly read"),
            "probability_status": WEEKLY_PROBABILITY_STATUS,
            "probability_source": WEEKLY_PROBABILITY_SOURCE,
            "variant_ids": list(self.variant_ids),
            "missing_fields": list(self.missing_fields),
        }


class WeeklyIndex:
    """Per-frame weekly/monthly buckets, read once per base bar (P1-05).

    The weekly tier cannot be recomputed per signal: PWH/PWL must come from a
    **complete, aligned** prior week, the weekly HH/HL from two complete weeks,
    and the monthly leg from two complete months.  Building those buckets once
    also makes the no-lookahead property structural -- the live week's bars are
    never in any aggregate the read can see (CB-07).
    """

    def __init__(self, df: pd.DataFrame, clock: WeekOpen, *,
                 market: str = "other", input_tz: str | None = None) -> None:
        if not isinstance(clock, WeekOpen):
            raise TypeError("a WeeklyIndex needs the declared WeekOpen clock")
        self.clock = clock
        self.market = market
        self.input_tz = input_tz
        self.n = len(df)
        self.dates = pd.to_datetime(df["date"])
        self.close = (df["close"].to_numpy(float) if self.n
                      else np.zeros(0, dtype=float))
        self.open = (df["open"].to_numpy(float) if self.n
                     else np.zeros(0, dtype=float))
        self.weekly = clock.bucket_frame(df, market=market, input_tz=input_tz)
        self.monthly = monthly_bars(
            df, clock.zone, market=market, input_tz=input_tz)
        self.starts = clock.week_starts(df, market=market, input_tz=input_tz)
        self.anchor_bars = clock.anchor_bars(
            df, market=market, input_tz=input_tz)
        self._states: dict[tuple[int, tuple[str, ...]],
                           tuple[WeeklyState | None, Refusal | None]] = {}

    def _completed(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars is None or len(bars) == 0:
            return bars
        return bars[(bars["complete"]) & (bars["aligned"])]

    def state(self, t: int, *, variant_ids: Iterable[str] = ()
              ) -> tuple[WeeklyState | None, Refusal | None]:
        """The weekly state at bar ``t``, or a typed refusal with the reason."""
        t = int(t)
        ids = tuple(str(v).strip().upper() for v in variant_ids)
        key = (t, ids)
        if key in self._states:
            return self._states[key]
        if t < 0 or t >= self.n:
            return None, Refusal(
                RefusalCode.NT_DATA_MISSING,
                detail=f"bar {t} is outside the {self.n}-bar frame")
        current_start = pd.Timestamp(self.starts.iloc[t])
        anchor = int(self.anchor_bars[t])
        current_stamp = self.clock.stamp(
            self.dates.iloc[t], input_tz=self.input_tz, market=self.market)
        first_stamp = self.clock.stamp(
            self.dates.iloc[anchor], input_tz=self.input_tz, market=self.market)
        if first_stamp != current_start:
            return None, Refusal(
                RefusalCode.NT_DATA_MISSING,
                detail=(f"the first bar of the declared week starts at "
                        f"{first_stamp.isoformat()}, not at the week anchor "
                        f"{self.clock.anchor_time.strftime('%H:%M')} "
                        f"{self.clock.zone} ({current_start.isoformat()}); its "
                        "open is not the weekly open"))
        weeks = self._completed(self.weekly)
        prior = weeks[weeks["end"] <= current_start]
        if len(prior) == 0:
            return None, Refusal(
                RefusalCode.NT_DATA_MISSING,
                detail=("no complete prior week before "
                        f"{current_start.isoformat()}; PWH/PWL and CE_wk need a "
                        "closed prior week (WB-01)"))
        last = prior.iloc[-1]
        weekly_range = WeeklyRange(
            week_key=pd.Timestamp(last["start"]).isoformat(),
            pwh=float(last["high"]), pwl=float(last["low"]))
        weekly_structure = (structure_from_bars(prior)
                            if len(prior) >= 2 else None)
        months = self._completed(self.monthly)
        months = months[months["end"] <= current_stamp]
        monthly_structure = (structure_from_bars(months)
                             if len(months) >= 2 else None)
        state = WeeklyState(
            scope="1W",
            as_of=t,
            price=float(self.close[t]),
            week_key=current_start.isoformat(),
            week_start=current_start,
            week_open=float(self.open[anchor]),
            week_open_bar=anchor,
            range=weekly_range,
            weekly_structure=weekly_structure,
            monthly_structure=monthly_structure,
            variant_ids=ids,
        )
        self._states[key] = (state, None)
        return state, None

    def to_public(self) -> dict[str, object]:
        return {
            "week_open": self.clock.to_public(),
            "weeks": int(len(self.weekly)),
            "months": int(len(self.monthly)),
        }


def weekly_state_at(df: pd.DataFrame, bar: int, *, week_open: str | WeekOpen,
                    market: str = "other", input_tz: str | None = None,
                    variant_ids: Iterable[str] = ()
                    ) -> tuple[WeeklyState | None, Refusal | None]:
    """The weekly state for one bar under the declared week-open clock."""
    clock = (declare_week_open(week_open) if isinstance(week_open, str)
             else week_open)
    if not isinstance(clock, WeekOpen):
        raise TypeError(
            "weekly_state_at needs a declared WeekOpen or one of "
            f"{WEEK_OPEN_OPTIONS}")
    return WeeklyIndex(
        df, clock, market=market, input_tz=input_tz).state(
            bar, variant_ids=variant_ids)


@dataclass(frozen=True, slots=True)
class WeeklyAnchor:
    """The declared V06 anchor reading for one weekly state (WB-01/WB-04)."""

    mode: str
    anchor_price: float
    location: str
    bias: BiasDirection
    source: str

    def __post_init__(self) -> None:
        mode = str(self.mode).strip()
        if mode not in WEEKLY_ANCHOR_OPTIONS:
            raise ValueError(
                f"unknown V06 weekly anchor {self.mode!r}; declare one of "
                f"{WEEKLY_ANCHOR_OPTIONS}")
        object.__setattr__(self, "mode", mode)
        anchor_price = float(self.anchor_price)
        if not np.isfinite(anchor_price):
            raise ValueError(
                f"the weekly anchor must be finite, got {self.anchor_price!r}")
        object.__setattr__(self, "anchor_price", anchor_price)
        location = str(self.location).strip()
        if location not in ("premium", "discount"):
            raise ValueError(
                f"a weekly anchor is premium/discount, got {self.location!r}")
        object.__setattr__(self, "location", location)
        bias = parse_bias(self.bias)
        expected = (BiasDirection.BULL if location == "discount"
                    else BiasDirection.BEAR)
        if bias is not expected:
            raise ValueError(
                "the weekly polarity is fixed by WB-01/WB-04: below the anchor "
                "is discount => bullish, above is premium => bearish")
        object.__setattr__(self, "bias", bias)
        source = str(self.source).strip()
        if not source:
            raise ValueError("a V06 anchor reading must carry its citation")
        object.__setattr__(self, "source", source)

    def to_public(self) -> dict[str, object]:
        return {
            "variant_id": "V06",
            "mode": self.mode,
            "anchor_price": self.anchor_price,
            "location": self.location,
            "bias": serialize_bias(self.bias),
            "source": self.source,
        }


def resolve_weekly_anchor(variant_config) -> str:
    """Read V06 from the one register (A7); an undeclared V06 refuses."""
    option = variant_config.read("V06")
    if option not in WEEKLY_ANCHOR_OPTIONS:
        raise ValueError(
            f"V06 returned {option!r}, not one of {WEEKLY_ANCHOR_OPTIONS}")
    return option


def weekly_anchor_mode(variant_config) -> tuple[str | None, Refusal | None]:
    """The fail-closed result form of :func:`resolve_weekly_anchor` (A6)."""
    refusal = variant_config.check("V06")
    if refusal is not None:
        return None, refusal
    return resolve_weekly_anchor(variant_config), None


def weekly_anchor_bias(state: WeeklyState, mode: str
                       ) -> tuple[WeeklyAnchor | None, Refusal | None]:
    """WB-01/WB-04: read ``price`` against the declared V06 anchor.

    The two anchors take the same polarity -- below is discount and therefore
    bullish -- but they are different prices: the weekly CE of the prior range
    or the current week's open.  Equality has no branch in either source (no
    tolerance band is stated), so it returns ``NT_NO_BIAS`` instead of picking
    a side; the *daily* midnight polarity is never ported onto this read.
    """
    if not isinstance(state, WeeklyState):
        raise TypeError(
            "the weekly anchor reads a WeeklyState, got "
            f"{type(state).__name__}")
    option = str(mode).strip()
    if option not in WEEKLY_ANCHOR_OPTIONS:
        raise ValueError(
            f"unknown V06 weekly anchor {mode!r}; declare one of "
            f"{WEEKLY_ANCHOR_OPTIONS}")
    anchor_price = (state.range.ce if option == "weekly CE"
                    else state.week_open)
    if state.price == anchor_price:
        return None, Refusal(
            RefusalCode.NT_NO_BIAS,
            detail=(f"price {state.price!r} equals the {option} "
                    f"{anchor_price!r}; WB-01/WB-04 declare only < and >, so "
                    "there is no equality reading (no tolerance is invented)"))
    location = "discount" if state.price < anchor_price else "premium"
    bias = (BiasDirection.BULL if location == "discount"
            else BiasDirection.BEAR)
    return WeeklyAnchor(option, anchor_price, location, bias,
                        WEEKLY_ANCHOR_SOURCES[option]), None


@dataclass(frozen=True, slots=True)
class WeeklyGateResult:
    """WB-03's ``monthly ∧ weekly ∧ direction ∧ location`` gate result."""

    direction: BiasDirection
    weekly_structure: BiasDirection | None
    monthly_structure: BiasDirection | None
    ce_wk: float
    price: float
    price_side_ok: bool
    bias_ok: bool
    downgraded: bool
    refusal: Refusal | None

    def __post_init__(self) -> None:
        direction = parse_bias(self.direction)
        if direction not in (BiasDirection.BULL, BiasDirection.BEAR):
            raise ValueError("the weekly gate takes a long/short direction")
        object.__setattr__(self, "direction", direction)
        if self.bias_ok and self.refusal is not None:
            raise ValueError("a passing weekly gate carries no refusal")
        if not self.bias_ok and self.refusal is None:
            raise ValueError("a failing weekly gate must name its §12 code")

    def to_public(self) -> dict[str, object]:
        return {
            "rule": "WB-03",
            "direction": serialize_bias(self.direction),
            "weekly_structure": (
                "" if self.weekly_structure is None
                else serialize_bias(self.weekly_structure)),
            "monthly_structure": (
                "" if self.monthly_structure is None
                else serialize_bias(self.monthly_structure)),
            "ce_wk": self.ce_wk,
            "price": self.price,
            "price_side_ok": bool(self.price_side_ok),
            "bias_ok": bool(self.bias_ok),
            "downgraded": bool(self.downgraded),
            "refusal": (None if self.refusal is None else {
                "code": self.refusal.code.value,
                "detail": self.refusal.detail}),
            "source": (
                "WB-03 [SN-W04]: monthly_structure == weekly_structure == "
                "direction AND (bullish: price on the discount side of CE_wk)"),
        }


def weekly_bias_gate(state: WeeklyState, direction: object) -> WeeklyGateResult:
    """WB-03: the conjunctive ``monthly ∧ weekly`` gate for one direction.

    The corpus gives no weekly-bias probability, so a weekly read *without* the
    monthly leg is not scored down by a fabricated number: it is marked
    ``downgraded`` and the WB-03 conjunction fails closed.  The CE side is
    strict (``price < CE_wk`` for a long, ``price > CE_wk`` for a short), which
    is the same strict midpoint the corpus states and never a repo tolerance.
    """
    reading = parse_bias(direction)
    if reading not in (BiasDirection.BULL, BiasDirection.BEAR):
        raise ValueError(
            "the weekly gate takes a long/short direction, not "
            f"{reading.value!r}")
    weekly = state.weekly_structure
    monthly = state.monthly_structure
    ce = state.range.ce
    price_side_ok = (state.price < ce if reading is BiasDirection.BULL
                     else state.price > ce)
    downgraded = monthly is None or (weekly is not None and monthly != weekly)

    def result(refusal: Refusal | None) -> WeeklyGateResult:
        return WeeklyGateResult(
            direction=reading, weekly_structure=weekly,
            monthly_structure=monthly, ce_wk=ce, price=state.price,
            price_side_ok=price_side_ok, bias_ok=refusal is None,
            downgraded=downgraded, refusal=refusal)

    if weekly is None:
        return result(Refusal(
            RefusalCode.NT_NO_BIAS,
            detail=("the weekly structure has no HH/HL reading (WB-02); the "
                    "weekly gate cannot pass on a missing leg")))
    if monthly is None:
        return result(Refusal(
            RefusalCode.NT_NO_BIAS,
            detail=("monthly_structure is missing; WB-03 says 'weekly bias "
                    "without monthly = lower probability', so the conjunctive "
                    "gate fails closed instead of scoring the weekly read")))
    if monthly is not weekly:
        return result(Refusal(
            RefusalCode.NT_BIAS_CONFLICT,
            detail=(f"monthly structure {serialize_bias(monthly)!r} conflicts "
                    f"with weekly structure {serialize_bias(weekly)!r}; WB-03 "
                    "requires them to agree")))
    if weekly is not reading:
        return result(Refusal(
            RefusalCode.NT_BIAS_GATE,
            detail=(f"the setup direction {reading.value!r} contradicts the "
                    f"weekly structure {weekly.value!r} (WB-02/WB-03)")))
    if not price_side_ok:
        return result(Refusal(
            RefusalCode.NT_WRONG_HALF,
            detail=(f"price {state.price!r} is not on the discount side of "
                    f"CE_wk {ce!r} required for a {reading.value!r} weekly "
                    "read (WB-01/WB-03)")))
    return result(None)


# --------------------------------------------------------------------------- #
# P1-05 — WB-05 weekly sequence (five ordered states per direction)
# --------------------------------------------------------------------------- #
class WeeklyPhase(StrEnum):
    """The WB-05 weekly sequence, in order (R15)."""
    OPEN_MARKED = "open_marked"
    MANIPULATION = "manipulation"
    EXTREME = "extreme"
    CONFIRMATION = "confirmation"
    EXPANSION = "expansion"
    REASSIGNED = "reassigned"


class WeeklySequenceEvent(StrEnum):
    """The events the WB-05 sequence consumes; order is enforced."""
    PRICE_WRONG_SIDE = "price_wrong_side"
    EXTREME_FORMED = "extreme_formed"
    CONFIRMATION = "confirmation"
    EXPANSION = "expansion"
    WEDNESDAY_CLOSE = "wednesday_close"


#: The two days the corpus names for the weekly extreme (WB-05/R15).
WEEKLY_EXTREME_DAYS: tuple[str, ...] = ("Tuesday", "Wednesday")


@dataclass(frozen=True, slots=True)
class WeeklySequence:
    """WB-05's five-state weekly sequence for one direction.

    Bearish mirror: mark the weekly open -> price trades **above** the open ->
    weekly high forms Tuesday or Wednesday -> bearish confirmation -> expansion
    lower.  The bullish mirror puts the manipulation below the weekly open (the
    Judas).  Events are order-checked: a confirmation before the extreme, or an
    expansion before the confirmation, raises instead of being accepted.  A
    Wednesday close without an extreme reassigns the profile (R23) and never
    forces one.
    """

    direction: BiasDirection
    weekly_open: float
    as_of: int
    phase: WeeklyPhase = WeeklyPhase.OPEN_MARKED
    extreme_day: str | None = None
    extreme_price: float | None = None
    confirmation_price: float | None = None
    reassigned: bool = False

    def __post_init__(self) -> None:
        direction = parse_bias(self.direction)
        if direction not in (BiasDirection.BULL, BiasDirection.BEAR):
            raise ValueError("the weekly sequence takes a long/short direction")
        object.__setattr__(self, "direction", direction)
        weekly_open = float(self.weekly_open)
        if not np.isfinite(weekly_open):
            raise ValueError("the weekly open must be a finite price")
        object.__setattr__(self, "weekly_open", weekly_open)
        as_of = int(self.as_of)
        if as_of < 0:
            raise ValueError("WeeklySequence.as_of must be a bar index")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "phase", WeeklyPhase(self.phase))
        if self.extreme_day is not None:
            day = str(self.extreme_day).strip().title()
            if day not in WEEKLY_EXTREME_DAYS:
                raise ValueError(
                    "WB-05 forms the weekly extreme Tuesday or Wednesday, not "
                    f"{self.extreme_day!r}; do not force a Monday template")
            object.__setattr__(self, "extreme_day", day)
        for name in ("extreme_price", "confirmation_price"):
            value = getattr(self, name)
            if value is None:
                continue
            price = float(value)
            if not np.isfinite(price):
                raise ValueError(f"{name} must be a finite price")
            object.__setattr__(self, name, price)

    def advance(self, event: WeeklySequenceEvent | str, *, bar: int,
                day: str | None = None, price: float | None = None
                ) -> "WeeklySequence":
        """Apply one WB-05 event, refusing an out-of-order transition."""
        step = WeeklySequenceEvent(event)
        bar = int(bar)
        if bar < int(self.as_of):
            raise ValueError(
                f"the weekly sequence cannot move backwards: bar {bar} < "
                f"as_of {self.as_of}")
        value = None if price is None else float(price)
        if value is not None and not np.isfinite(value):
            raise ValueError("a weekly-sequence price must be finite")
        bullish = self.direction is BiasDirection.BULL
        if step is WeeklySequenceEvent.PRICE_WRONG_SIDE:
            if self.phase is not WeeklyPhase.OPEN_MARKED:
                raise ValueError(
                    "WB-05: the wrong-side trade is the manipulation phase and "
                    f"must follow the open, not {self.phase.value!r}")
            if value is None:
                raise ValueError("the wrong-side event needs the traded price")
            wrong_side = value < self.weekly_open if bullish else (
                value > self.weekly_open)
            if not wrong_side:
                side = "below" if bullish else "above"
                raise ValueError(
                    f"WB-05: a {self.direction.value!r} week manipulates {side} "
                    f"the weekly open {self.weekly_open!r}; {value!r} is not")
            return replace(self, as_of=bar, phase=WeeklyPhase.MANIPULATION)
        if step is WeeklySequenceEvent.EXTREME_FORMED:
            if self.phase is not WeeklyPhase.MANIPULATION:
                raise ValueError(
                    "WB-05: the weekly extreme follows the manipulation phase, "
                    f"not {self.phase.value!r}")
            extreme_day = (None if day is None
                           else str(day).strip().title())
            if extreme_day not in WEEKLY_EXTREME_DAYS:
                raise ValueError(
                    "WB-05 forms the weekly extreme Tuesday or Wednesday, not "
                    f"{day!r}; do not force a Monday template")
            if value is None:
                raise ValueError("the weekly extreme needs its price")
            extreme_side = value < self.weekly_open if bullish else (
                value > self.weekly_open)
            if not extreme_side:
                side = "below" if bullish else "above"
                raise ValueError(
                    f"WB-05: a {self.direction.value!r} week's extreme forms "
                    f"{side} the weekly open; {value!r} is not")
            return replace(
                self, as_of=bar, phase=WeeklyPhase.EXTREME,
                extreme_day=extreme_day, extreme_price=value)
        if step is WeeklySequenceEvent.CONFIRMATION:
            if self.phase is not WeeklyPhase.EXTREME:
                raise ValueError(
                    "WB-05: confirmation follows the weekly extreme, not "
                    f"{self.phase.value!r}")
            if value is None:
                raise ValueError("confirmation needs its price")
            return replace(
                self, as_of=bar, phase=WeeklyPhase.CONFIRMATION,
                confirmation_price=value)
        if step is WeeklySequenceEvent.EXPANSION:
            if self.phase is not WeeklyPhase.CONFIRMATION:
                raise ValueError(
                    "WB-05: expansion follows confirmation, not "
                    f"{self.phase.value!r}")
            return replace(self, as_of=bar, phase=WeeklyPhase.EXPANSION)
        # WEDNESDAY_CLOSE: no extreme by the Wednesday close -> reassign.
        if self.phase in (WeeklyPhase.EXTREME, WeeklyPhase.CONFIRMATION,
                          WeeklyPhase.EXPANSION):
            raise ValueError(
                "an extreme has already formed; the Wednesday-close "
                "reassignment does not apply (R23)")
        if str(day).strip().title() != "Wednesday":
            raise ValueError(
                "the profile reassignment is a Wednesday-close event (R23)")
        return replace(self, as_of=bar, phase=WeeklyPhase.REASSIGNED,
                       reassigned=True)

    def to_public(self) -> dict[str, object]:
        return {
            "rule": "WB-05",
            "direction": serialize_bias(self.direction),
            "weekly_open": self.weekly_open,
            "as_of": int(self.as_of),
            "phase": self.phase.value,
            "extreme_day": self.extreme_day,
            "extreme_price": self.extreme_price,
            "confirmation_price": self.confirmation_price,
            "reassigned": bool(self.reassigned),
            "source": (
                "WB-05 [SN-W08]: mark WO -> wrong-side trade -> weekly extreme "
                "Tue/Wed -> confirmation -> expansion"),
        }


def start_weekly_sequence(direction: object, weekly_open: object, *,
                          as_of: int = 0) -> WeeklySequence:
    """Mark the weekly open and begin WB-05's sequence."""
    return WeeklySequence(
        direction=parse_bias(direction), weekly_open=weekly_open, as_of=as_of)


def advance_weekly_sequence(sequence: WeeklySequence,
                            event: WeeklySequenceEvent | str, *, bar: int,
                            day: str | None = None, price: float | None = None
                            ) -> WeeklySequence:
    """Module-level form of :meth:`WeeklySequence.advance`."""
    if not isinstance(sequence, WeeklySequence):
        raise TypeError("advance_weekly_sequence needs a WeeklySequence")
    return sequence.advance(event, bar=bar, day=day, price=price)


# --------------------------------------------------------------------------- #
# P1-05 — WB-06 Tue/Wed scheduler and the R19 Tuesday condition set
# --------------------------------------------------------------------------- #
WEEKDAYS: tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday",
                             "Friday")

#: WB-06's fallback chain days (R18).
WEEKLY_SCHEDULER_DAYS: tuple[str, ...] = ("Tuesday", "Wednesday")

#: The R19 Tuesday condition set: six AND-ed gates, none with a number.
TUESDAY_CONDITIONS: tuple[str, ...] = (
    "higher_timeframe_bias_bullish",
    "discount_pd_array_below_price",
    "monday_failed_to_reach_discount_array",
    "tuesday_trades_below_monday_range",
    "sell_side_liquidity_taken",
    "bullish_displacement_from_array",
)


class WeeklyScheduleState(StrEnum):
    """WB-06's three outcomes: still waiting, printed, or profile reassigned."""
    PENDING = "pending"
    COMPLETE = "complete"
    REASSIGNED = "reassigned"


@dataclass(frozen=True, slots=True)
class WeeklySchedule:
    """WB-06's Tue/Wed expectation for one observed day."""

    day: str
    monday_reached: bool
    tuesday_reached: bool
    extreme_formed: bool
    extreme_day: str | None
    expected_day: str | None
    state: WeeklyScheduleState
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        day = str(self.day).strip().title()
        if day not in WEEKDAYS:
            raise ValueError(f"a weekly schedule day is one of {WEEKDAYS}, got {day!r}")
        object.__setattr__(self, "day", day)
        object.__setattr__(self, "state", WeeklyScheduleState(self.state))
        object.__setattr__(self, "reasons", tuple(str(r) for r in self.reasons))
        if self.expected_day is not None:
            expected = str(self.expected_day).strip().title()
            if expected not in WEEKLY_SCHEDULER_DAYS:
                raise ValueError(
                    "WB-06 expects the delayed extreme Tuesday or Wednesday, "
                    f"not {self.expected_day!r}")
            object.__setattr__(self, "expected_day", expected)
        if self.extreme_day is not None:
            extreme = str(self.extreme_day).strip().title()
            if extreme not in WEEKLY_EXTREME_DAYS:
                raise ValueError(
                    "WB-05 forms the weekly extreme Tuesday or Wednesday, not "
                    f"{self.extreme_day!r}")
            object.__setattr__(self, "extreme_day", extreme)

    def to_public(self) -> dict[str, object]:
        return {
            "rule": "WB-06",
            "day": self.day,
            "monday_reached": bool(self.monday_reached),
            "tuesday_reached": bool(self.tuesday_reached),
            "extreme_formed": bool(self.extreme_formed),
            "extreme_day": self.extreme_day,
            "expected_day": self.expected_day,
            "state": self.state.value,
            "reasons": list(self.reasons),
            "source": (
                "WB-06 [SN-W09/R18]: Monday fails => Tuesday; Monday and "
                "Tuesday fail => Wednesday; no extreme by Wednesday close => "
                "reassign, do not force"),
        }


def week_schedule(*, day: str, monday_reached: bool, tuesday_reached: bool,
                  extreme_formed: bool, extreme_day: str | None = None,
                  wednesday_close: bool = False) -> WeeklySchedule:
    """WB-06: the Tue/Wed fallback chain, ending in reassignment not force.

    ``monday_reached``/``tuesday_reached`` answer R18's "failed to reach the
    HTF array" question; ``extreme_formed`` says whether the weekly extreme has
    printed.  A Thursday/Friday read (or Wednesday's close) with no extreme is
    a profile **reassignment** (R23), never a delayed extreme pushed forward.
    """
    label = str(day).strip().title()
    if label not in WEEKDAYS:
        raise ValueError(f"a weekly schedule day is one of {WEEKDAYS}, got {day!r}")
    if extreme_formed:
        return WeeklySchedule(
            day=label, monday_reached=bool(monday_reached),
            tuesday_reached=bool(tuesday_reached), extreme_formed=True,
            extreme_day=extreme_day,
            expected_day=extreme_day,
            state=WeeklyScheduleState.COMPLETE,
            reasons=("the weekly extreme has printed; the profile is complete",))
    if label in ("Thursday", "Friday") or (label == "Wednesday"
                                           and wednesday_close):
        return WeeklySchedule(
            day=label, monday_reached=bool(monday_reached),
            tuesday_reached=bool(tuesday_reached), extreme_formed=False,
            extreme_day=None, expected_day=None,
            state=WeeklyScheduleState.REASSIGNED,
            reasons=("no weekly extreme by the Wednesday close: reassign the "
                     "profile, do not force it (R18/R23)",))
    if not monday_reached:
        expected = "Tuesday"
        reasons = ("Monday failed to reach the HTF array: the extreme is "
                   "expected Tuesday (R18)",)
    elif not tuesday_reached:
        expected = "Wednesday"
        reasons = ("Monday and Tuesday both failed to reach the HTF array: the "
                   "extreme is expected Wednesday (R18)",)
    else:
        expected = None
        reasons = ("the delay chain is not triggered: both Monday and Tuesday "
                   "reached the array and the extreme window stays Tue/Wed",)
    return WeeklySchedule(
        day=label, monday_reached=bool(monday_reached),
        tuesday_reached=bool(tuesday_reached), extreme_formed=False,
        extreme_day=None, expected_day=expected,
        state=WeeklyScheduleState.PENDING, reasons=reasons)


def tuesday_profile_ok(**conditions: object) -> tuple[bool, tuple[str, ...]]:
    """R19's six-gate AND-list; returns ``(all_met, missing_names)``.

    Every gate is boolean by construction -- the corpus supplies no number for
    any of the six -- so an unknown keyword raises and an omitted gate counts as
    unmet.  No default profile is returned when a gate fails.
    """
    unknown = sorted(set(conditions) - set(TUESDAY_CONDITIONS))
    if unknown:
        raise ValueError(
            f"unknown Tuesday condition(s) {unknown}; R19 declares exactly "
            f"{list(TUESDAY_CONDITIONS)}")
    missing = tuple(name for name in TUESDAY_CONDITIONS
                    if not bool(conditions.get(name, False)))
    return (not missing, missing)


def dealing_range_state(swings: Iterable, idx: int,
                        lookback_bars: int = RANGE_STALENESS_BARS
                        ) -> tuple[DealingRange | None, RangeDiagnostic | None]:
    """The last confirmed swing high/low pair **and** why it is absent.

    Returns ``(range, None)`` when the pair is usable, else ``(None,
    diagnostic)``.  P0-16: the 120-bar staleness is a repo expiry (§13.3), so
    an absent range is always accompanied by a reason; a caller that only wants
    the value calls :func:`dealing_range`.
    """
    t = int(idx)
    highs = [s for s in swings if getattr(s, "kind", "") == "H"
             and int(getattr(s, "confirmed_at", 0)) <= t]
    lows = [s for s in swings if getattr(s, "kind", "") == "L"
            and int(getattr(s, "confirmed_at", 0)) <= t]
    if not highs or not lows:
        missing = ("high or low" if not highs and not lows
                   else "high" if not highs else "low")
        return None, _no_pair_diagnostic(t, missing, lookback_bars)
    h, l = highs[-1], lows[-1]
    h_conf = int(getattr(h, "confirmed_at", h.index))
    l_conf = int(getattr(l, "confirmed_at", l.index))
    age = max(t - h_conf, t - l_conf)
    if age > int(lookback_bars):
        return None, _stale_diagnostic(t, max(h_conf, l_conf), age,
                                       lookback_bars)
    high = max(float(h.price), float(l.price))
    low = min(float(h.price), float(l.price))
    if high <= low:
        return None, _degenerate_diagnostic(t, age, lookback_bars)
    return DealingRange(high, low, (high + low) / 2,
                        int(h.index), int(l.index)), None


def dealing_range(swings: Iterable, idx: int,
                  lookback_bars: int = RANGE_STALENESS_BARS) -> DealingRange | None:
    """Last confirmed swing high/low pair, with both sides recent enough.

    This is the value-only twin of :func:`dealing_range_state`; an absent
    range has a reason there (``NT_RANGE_INVALID`` / ``NT_DATA_MISSING``), so a
    staleness can no longer be mistaken for "no range" with no cause (P0-16).
    """
    return dealing_range_state(swings, idx, lookback_bars)[0]


class SwingIndex:
    """O(1)-per-bar view over one zigzag swing list.

    ``dealing_range`` and ``structure_bias`` filter the whole swing list on
    every call, which is O(swings) inside a per-signal loop (measured: 3.6 s of
    a 12.8 s ``build_setup`` on 20k BTC 15m bars).  The zigzag list is ordered
    by bar index and ``confirmed_at`` grows with it, so "the last N confirmed
    swings at bar t" is a binary search instead of a scan.

    The pure functions above are the reference implementation.  No test in this
    checkout proves bar-by-bar equivalence between the two, so this class makes
    no such claim (P0-19 removed a docstring that asserted one).
    """

    def __init__(self, swings: Iterable) -> None:
        swings = list(swings)
        for key, kind in (("h", "H"), ("l", "L")):
            sub = [s for s in swings if getattr(s, "kind", "") == kind]
            setattr(self, f"{key}_index", np.array([int(s.index) for s in sub], dtype=np.int64))
            setattr(self, f"{key}_conf", np.array([int(getattr(s, "confirmed_at", s.index))
                                                  for s in sub], dtype=np.int64))
            setattr(self, f"{key}_price", np.array([float(s.price) for s in sub], dtype=float))

    def _n(self, key: str, t: int) -> int:
        return int(np.searchsorted(getattr(self, f"{key}_conf"), int(t), side="right"))

    def structure_bias(self, t: int, lookback: int = 2) -> str:
        """HH/HL (``bullish``) or LH/LL (``bearish``) over confirmed swings.

        This is the only structure read that sets bias (BI-01).  A mixed pair
        or too few confirmed swings is ``neutral``: the repo declares the
        mixed-pair outcome that the corpus leaves undefined (BI-02/BI-03).
        """
        nh, nl = self._n("h", t), self._n("l", t)
        if nh >= lookback and nl >= lookback:
            hp, lp = self.h_price, self.l_price
            hh = hp[nh - 1] > hp[nh - lookback]
            hl = lp[nl - 1] > lp[nl - lookback]
            lh = hp[nh - 1] < hp[nh - lookback]
            ll = lp[nl - 1] < lp[nl - lookback]
            if hh and hl:
                return serialize_bias(BiasDirection.BULL)
            if lh and ll:
                return serialize_bias(BiasDirection.BEAR)
        return serialize_bias(BiasDirection.NEUTRAL)

    def dealing_range_state(self, t: int,
                            lookback_bars: int = RANGE_STALENESS_BARS
                            ) -> tuple[DealingRange | None, RangeDiagnostic | None]:
        """Binary-search twin of :func:`dealing_range_state` (same reasons)."""
        t = int(t)
        nh, nl = self._n("h", t), self._n("l", t)
        if nh == 0 or nl == 0:
            missing = ("high or low" if nh == 0 and nl == 0
                       else "high" if nh == 0 else "low")
            return None, _no_pair_diagnostic(t, missing, lookback_bars)
        hi, li = nh - 1, nl - 1
        h_conf, l_conf = int(self.h_conf[hi]), int(self.l_conf[li])
        age = max(t - h_conf, t - l_conf)
        if age > int(lookback_bars):
            return None, _stale_diagnostic(t, max(h_conf, l_conf), age,
                                           lookback_bars)
        high = max(float(self.h_price[hi]), float(self.l_price[li]))
        low = min(float(self.h_price[hi]), float(self.l_price[li]))
        if high <= low:
            return None, _degenerate_diagnostic(t, age, lookback_bars)
        return DealingRange(high, low, (high + low) / 2,
                            int(self.h_index[hi]), int(self.l_index[li])), None

    def dealing_range(self, t: int,
                      lookback_bars: int = RANGE_STALENESS_BARS) -> DealingRange | None:
        return self.dealing_range_state(t, lookback_bars)[0]


def ote_from_signal(df: pd.DataFrame, start: int, end: int, entry: float, direction: str) -> float | None:
    """Retracement measured from the signal's own delivery range.

    The global zigzag is often mid-leg when a CISD fires, so using the last two
    zigzag points can point the wrong way.  The CISD start/end range is known at
    the signal bar and is the range that actually produced the entry.
    """
    if start < 0 or end < start or end >= len(df):
        return None
    high = float(df["high"].iloc[start:end + 1].max())
    low = float(df["low"].iloc[start:end + 1].min())
    if high <= low or not (low <= entry <= high):
        return None
    if direction == "bull":
        return (high - entry) / (high - low)
    return (entry - low) / (high - low)


def ote_retracement(swings: Iterable, entry: float, direction: str) -> float | None:
    """Retracement of the latest completed impulse leg, as a 0..1 fraction.

    For a bullish leg the leg is low -> high and retracement is
    ``(high - entry) / (high - low)``.  For a bearish leg the mirror is used.
    """
    zz = [s for s in swings if getattr(s, "kind", "") in {"H", "L"}]
    if len(zz) < 2:
        return None
    a, b = zz[-2], zz[-1]
    if direction == "bull":
        if a.kind != "L" or b.kind != "H":
            return None
        low, high = float(a.price), float(b.price)
        if high <= low:
            return None
        return min(max((high - entry) / (high - low), 0.0), 1.0)
    if a.kind != "H" or b.kind != "L":
        return None
    high, low = float(a.price), float(b.price)
    if high <= low:
        return None
    return min(max((entry - low) / (high - low), 0.0), 1.0)


def candle2_closure(prior_high: float, prior_low: float,
                    cur_high: float, cur_low: float, cur_close: float) -> str | None:
    """Repo Candle-2 fake-break closure (labelled ``repo``; not a corpus rule).

    A bullish C2 sweeps the prior low and closes back inside the prior range
    (above the prior low); a bearish C2 sweeps the prior high and closes back
    inside (below the prior high).  ``None`` when neither happens.

    This is a repo bias-filter primitive, not a standalone entry, and the
    corpus's only readable daily Candle-2 rule ("a strong close through the
    prior delivering candles") is explicitly insufficient alone
    (``daily_bias.md`` §7.3-5).  The label is part of the declaration: no
    caller may present it as an ICT bias rule (§14.1-6).
    """
    if cur_low < prior_low and cur_close > prior_low:
        return "bull"
    if cur_high > prior_high and cur_close < prior_high:
        return "bear"
    return None


def candle3_closure(prior_high: float, prior_low: float,
                    c2_open: float, c2_close: float, c2_high: float, c2_low: float,
                    c3_open: float, c3_close: float) -> str | None:
    """Repo Candle-3 reversal (labelled ``repo``; not a corpus rule).

    C2 fails when the next bar (C3) engulfs C2's body and closes through it.
    The returned direction is the *new* bias (opposite of the failed C2):
    a bullish C2 that gets engulfed downward yields ``"bear"``, a bearish C2
    engulfed upward yields ``"bull"``.
    """
    c2_dir = candle2_closure(prior_high, prior_low, c2_high, c2_low, c2_close)
    if c2_dir is None:
        return None
    body_hi = max(float(c2_open), float(c2_close))
    body_lo = min(float(c2_open), float(c2_close))
    if c2_dir == "bull":
        # C2 hinted long; C3 engulfs its body and closes through the low.
        if float(c3_close) < body_lo:
            return "bear"
    else:
        # C2 hinted short; C3 engulfs its body and closes through the high.
        if float(c3_close) > body_hi:
            return "bull"
    return None


def structure_bias(swings: Iterable, t: int, lookback: int = 2) -> str:
    """ICT bias structure: HH/HL vs LH/LL (BI-01, corpus high-consensus).

    Bullish = the last two swing highs are rising AND the last two swing lows
    are rising (higher-high / higher-low).  Bearish = the mirror.  Anything
    else (mixed, or fewer than two swings on each side) is ``"neutral"``, the
    declared repo outcome for the mixed pair the corpus leaves undefined
    (BI-02/BI-03).  This HH/HL read is the **only** ``structure_bias``; the
    realtime pivot/BOS read is named ``pivot_bos_bias`` instead of sharing
    this name (P0-09).
    """
    highs = [s for s in swings if getattr(s, "kind", "") == "H" and int(s.confirmed_at) <= t]
    lows = [s for s in swings if getattr(s, "kind", "") == "L" and int(s.confirmed_at) <= t]
    if len(highs) >= lookback and len(lows) >= lookback:
        h1, h2 = highs[-lookback], highs[-1]
        l1, l2 = lows[-lookback], lows[-1]
        hh = float(h2.price) > float(h1.price)
        hl = float(l2.price) > float(l1.price)
        lh = float(h2.price) < float(h1.price)
        ll = float(l2.price) < float(l1.price)
        if hh and hl:
            return serialize_bias(BiasDirection.BULL)
        if lh and ll:
            return serialize_bias(BiasDirection.BEAR)
    return serialize_bias(BiasDirection.NEUTRAL)
