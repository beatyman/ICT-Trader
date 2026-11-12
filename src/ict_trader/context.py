"""Optional market-context gates: session, premium/discount and OTE.

These are deliberately pure functions over already-known bars.  They do not
change the default frozen setup; `SetupConfig` opts into them one at a time so
each can be ablated.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import time as dtime
from enum import Enum, StrEnum
from typing import Iterable, Literal

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
