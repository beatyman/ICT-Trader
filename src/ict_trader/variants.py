"""The declared variant register (SYSTEM_SPEC §14.2) and §12 refusals.

A conflict in this corpus is a decision point, not a defect (§14.1-1).  Every
fork that changes a price, a state label or a gate must therefore be declared
once (§14.1-2) and read from one place (§14.1-3, A7).  This module is that one
place:

* :data:`VARIANT_REGISTER` holds the 35 spec rows ``V01``–``V35`` with their
  name, admissible options, default policy, source citation and the state the
  row blocks when it is undeclared.
* :class:`VariantConfig` holds the run's **declared** selections and exposes
  the single selection read path, :meth:`VariantConfig.read`.
* :class:`RefusalCode` types the §12 ``NT_*`` vocabulary; :class:`Refusal`
  carries one code with its diagnostic.

Fail-closed is deliberate: an undeclared fork is an unspecified model
(§14.1-4, F12), so :meth:`VariantConfig.read` never falls back to the first
option, to the documented repo default, or to any other value.  It refuses
with :class:`VariantUndeclared`, whose ``refusal.code`` is exactly
``NT_VARIANT_UNDECLARED``.

The register does not choose a variant.  ``DEFAULT-REPO`` only marks a fork
where §14.1-5 permits a labelled repo default; the default is still recorded as
an explicit selection and is never applied silently.  ``MODE`` marks a fork
that ships both positions as an exposed mode field.  ``BLOCK`` permits no
default at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "DEFAULT_POLICIES",
    "SPEC_REFUSAL_CODES",
    "VARIANT_REGISTER",
    "DefaultPolicy",
    "Refusal",
    "RefusalCode",
    "UnknownVariant",
    "VariantConfig",
    "VariantSpec",
    "VariantUndeclared",
    "read_variant_spec",
]


# --------------------------------------------------------------------------- #
# §12 refusal vocabulary (typed)
# --------------------------------------------------------------------------- #
class RefusalCode(StrEnum):
    """Every ``NO_TRADE`` code in SYSTEM_SPEC §12, as a type.

    The §12 table lists 39 rows (``CODE_MAPPING.md`` rounds the same set to
    "37"); the enum is transcribed from the table itself, so no code is
    dropped.  A refusal is a first-class result (A3/A6), not an exception
    string: callers compare ``refusal.code`` with a member of this enum.
    """

    NT_DATA_MISSING = "NT_DATA_MISSING"
    NT_VARIANT_UNDECLARED = "NT_VARIANT_UNDECLARED"
    NT_INSTRUMENT_UNSUPPORTED = "NT_INSTRUMENT_UNSUPPORTED"
    NT_NO_BIAS = "NT_NO_BIAS"
    NT_BIAS_GATE = "NT_BIAS_GATE"
    NT_BIAS_CONFLICT = "NT_BIAS_CONFLICT"
    NT_OUTSIDE_DAY = "NT_OUTSIDE_DAY"
    NT_NO_DRAW = "NT_NO_DRAW"
    NT_DRAW_TAKEN = "NT_DRAW_TAKEN"
    NT_DRAW_TOO_CLOSE = "NT_DRAW_TOO_CLOSE"
    NT_RANGE_INVALID = "NT_RANGE_INVALID"
    NT_RANGE_TIME_ANCHORED = "NT_RANGE_TIME_ANCHORED"
    NT_WRONG_HALF = "NT_WRONG_HALF"
    NT_AT_EQ = "NT_AT_EQ"
    NT_AT_EQ_NO_ARRAY = "NT_AT_EQ_NO_ARRAY"
    NT_MATRIX_EMPTY = "NT_MATRIX_EMPTY"
    NT_ARRAY_WRONG_SIDE = "NT_ARRAY_WRONG_SIDE"
    NT_OUT_OF_WINDOW = "NT_OUT_OF_WINDOW"
    NT_DEAD_ZONE = "NT_DEAD_ZONE"
    NT_NO_RAID = "NT_NO_RAID"
    NT_LEVEL_BROKEN_NOT_SWEPT = "NT_LEVEL_BROKEN_NOT_SWEPT"
    NT_NO_DISPLACEMENT = "NT_NO_DISPLACEMENT"
    NT_NO_SHIFT = "NT_NO_SHIFT"
    NT_SHIFT_UNCONFIRMED = "NT_SHIFT_UNCONFIRMED"
    NT_CHASE = "NT_CHASE"
    NT_TARGET_TAKEN = "NT_TARGET_TAKEN"
    NT_TARGET_TOO_NEAR = "NT_TARGET_TOO_NEAR"
    NT_WINDOW_EXPIRED = "NT_WINDOW_EXPIRED"
    NT_FILL_TIMEOUT = "NT_FILL_TIMEOUT"
    NT_ONE_ATTEMPT_USED = "NT_ONE_ATTEMPT_USED"
    NT_RR_BELOW_FLOOR = "NT_RR_BELOW_FLOOR"
    NT_SIZE_UNSOLVABLE = "NT_SIZE_UNSOLVABLE"
    NT_BUDGET_SPENT = "NT_BUDGET_SPENT"
    NT_EVENT_VETO = "NT_EVENT_VETO"
    NT_CORRELATION = "NT_CORRELATION"
    NT_SESSION_CLOSED = "NT_SESSION_CLOSED"
    NT_STOP_ANCHOR_UNRESOLVED = "NT_STOP_ANCHOR_UNRESOLVED"
    NT_FAILED_MANIPULATION = "NT_FAILED_MANIPULATION"
    NT_VOID_OR_GAP_UNUSABLE = "NT_VOID_OR_GAP_UNUSABLE"


#: The §12 codes in table order.  Useful for provenance tests and for a
#: vocabulary export that does not depend on ``StrEnum`` iteration details.
SPEC_REFUSAL_CODES: tuple[str, ...] = tuple(code.value for code in RefusalCode)


@dataclass(frozen=True, slots=True)
class Refusal:
    """One typed ``NO_TRADE`` result with its diagnostic (A3).

    ``variant_id`` is set when the refusal names the fork that was not
    declared; ``detail`` is the failing guard's explanation.  A refusal never
    carries a guessed price or a substitute option.
    """

    code: RefusalCode
    variant_id: str | None = None
    detail: str = ""

    def __str__(self) -> str:
        if self.variant_id:
            return f"{self.code.value}: {self.variant_id} ({self.detail})"
        return f"{self.code.value}: {self.detail}".rstrip(": ")


class VariantUndeclared(ValueError):
    """A fork is operational but no option was declared (fail-closed).

    The exception is the programming-surface form of the refusal; the
    first-class result form is :meth:`VariantConfig.check`, and both carry the
    same :class:`Refusal` with ``NT_VARIANT_UNDECLARED``.
    """

    def __init__(self, variant_id: str, detail: str = "") -> None:
        self.variant_id = variant_id
        self.refusal = Refusal(
            RefusalCode.NT_VARIANT_UNDECLARED, variant_id, detail
        )
        super().__init__(str(self.refusal))


class UnknownVariant(KeyError):
    """``variant_id`` is not one of the spec's ``V01``–``V35`` rows."""

    def __init__(self, variant_id: object) -> None:
        self.variant_id = str(variant_id)
        super().__init__(self.variant_id)


# --------------------------------------------------------------------------- #
# §14.2 register
# --------------------------------------------------------------------------- #
class DefaultPolicy(StrEnum):
    """What §14.3 permits when a fork is declared.

    ``BLOCK``      no default permitted, must be configured (§14.3).
    ``MODE``       the fork is an exposed mode field; both positions ship.
    ``DEFAULT-REPO`` a labelled repo default is permitted by §14.1-5.
    """

    BLOCK = "BLOCK"
    MODE = "MODE"
    DEFAULT_REPO = "DEFAULT-REPO"


DEFAULT_POLICIES: tuple[str, ...] = tuple(policy.value for policy in DefaultPolicy)


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """One row of the §14.2 register.

    ``options`` is the flat set of declarations the spec attests for the row.
    Where §14.2 gives two explicit axes (for example roster size × ordering),
    the cross-product is expanded, because §14.1-3 stores exactly one selected
    option per fork.  Where the spec defers a sub-choice to another table (the
    window table, the seven OTE anchors, the five BE triggers), ``notes`` names
    the deferral instead of inventing values that the spec does not enumerate.
    """

    id: str
    name: str
    options: tuple[str, ...]
    default_policy: DefaultPolicy
    source: str
    blocks: str
    repo_default: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id or not self.id.startswith("V"):
            raise ValueError(f"variant id must be V01..V35, got {self.id!r}")
        if not self.name.strip():
            raise ValueError(f"{self.id}: name is empty")
        if not self.options:
            raise ValueError(f"{self.id}: no options declared")
        if len(set(self.options)) != len(self.options):
            raise ValueError(f"{self.id}: duplicate options")
        if self.default_policy is DefaultPolicy.DEFAULT_REPO:
            if not self.repo_default:
                raise ValueError(
                    f"{self.id}: DEFAULT-REPO requires a labelled repo_default"
                )
            if self.repo_default not in self.options:
                raise ValueError(
                    f"{self.id}: repo_default {self.repo_default!r} is not an option"
                )
        elif self.repo_default is not None:
            raise ValueError(
                f"{self.id}: only DEFAULT-REPO variants carry a repo_default"
            )
        if not self.source.strip():
            raise ValueError(f"{self.id}: source citation is empty")
        if not self.blocks.strip():
            raise ValueError(f"{self.id}: the undeclared block target is empty")

    def accepts(self, option: str) -> bool:
        """True only for an exact declared option; no fuzzy or empty match."""
        return option in self.options


def _cross(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    """The explicit two-axis option set, in spec order."""
    return tuple(f"{a} · {b}" for a in left for b in right)


def _spec(
    variant_id: str,
    name: str,
    options: tuple[str, ...],
    default_policy: DefaultPolicy,
    source: str,
    blocks: str,
    *,
    repo_default: str | None = None,
    notes: str = "",
) -> VariantSpec:
    return VariantSpec(variant_id, name, options, default_policy, source, blocks,
                       repo_default, notes)


_VARIANT_ROWS: tuple[VariantSpec, ...] = (
    _spec("V01", "bias primitive",
          ("structure read", "liquidity objective", "macro/seasonal stance",
           "permission side", "candle colour"),
          DefaultPolicy.BLOCK, "[SN:C1]", "B1"),
    _spec("V02", "bias timeframe set",
          ("M/W/D/4H", "D+4H", "weekly only", "M+W", "1D", "style triple", "none"),
          DefaultPolicy.MODE, "[SN:C2]", "B1",
          notes="§14.3: BLOCK + MODE; one stored state per declared scope."),
    _spec("V03", "state vocabulary",
          ("binary", "+neutral", "+unclear", "+ambiguous", "both directions"),
          DefaultPolicy.BLOCK, "[SN:C4]", "B1"),
    _spec("V04", "day boundary",
          ("NY midnight", "broker day", "futures open", "cash open"),
          DefaultPolicy.BLOCK, "[SN:C6]", "G0"),
    _spec("V05", "daily polarity",
          ("majority", "inverted"),
          DefaultPolicy.BLOCK, "[SN:C5]", "D1"),
    _spec("V06", "weekly P/D anchor",
          ("weekly CE", "weekly open"),
          DefaultPolicy.BLOCK, "[SN:C9]", "weekly state"),
    _spec("V07", "MSS family",
          ("F1", "F2", "F3", "F4", "F5", "F6"),
          DefaultPolicy.BLOCK, "[SN:C12]", "MS1"),
    _spec("V08", "range anchor",
          ("swing pair", "swing pair that took liquidity", "impulse leg",
           "CISD leg", "HTF→objective", "20/40/60"),
          DefaultPolicy.BLOCK, "[SN:C18]", "R1"),
    _spec("V09", "matrix roster + order",
          _cross(("3 members", "6 members", "7 members", "8 members", "9 members"),
                 ("total order", "proximity", "none")),
          DefaultPolicy.BLOCK, "[SN:C25/C26]", "A1",
          notes="§14.2 gives roster size × ordering; the cross-product is the "
                "explicit option set."),
    _spec("V10", "IPDA variant",
          _cross(("rolling", "anchored-monthly", "weekly"),
                 ("target ordered", "nearest un-taken", "equilibrium-first")),
          DefaultPolicy.BLOCK, "[SN:C29/C30]", "IPDA levels",
          notes="§14.2 gives IPDA variant × target order; both are declared."),
    _spec("V11", "gate strength",
          ("hard veto", "quality multiplier", "score", "narrative only"),
          DefaultPolicy.BLOCK, "[SN:C13]", "every gate"),
    _spec("V12", "sweep test",
          ("close-back", "wick-only", "grab"),
          DefaultPolicy.BLOCK, "[LT:C8]", "SW1"),
    _spec("V13", "window table + boundary",
          ("half-open", "inclusive", "inclusive-at-11:00"),
          DefaultPolicy.DEFAULT_REPO, "[LT:C1/C10]", "W1",
          repo_default="half-open",
          notes="§14.4 declares half-open as the repo default; the §10.2 window "
                "table is declared separately and is never merged."),
    _spec("V14", "window authority",
          ("blocking", "penalty", "none"),
          DefaultPolicy.MODE, "[LT:C3; RE:C-14]", "W1",
          notes="GT-09: window_mode ∈ {blocking, penalty, none}."),
    _spec("V15", "sweep direction vs bias",
          ("sweep side sets direction", "must oppose bias", "must match daily bias"),
          DefaultPolicy.BLOCK, "[LT:C23]", "SW1→DP1"),
    _spec("V16", "FVG boundary convention",
          ("wick-to-wick", "body-cross", "open/close-extended"),
          DefaultPolicy.BLOCK, "[PP:C1]", "FVG"),
    _spec("V17", "body-close consequence",
          ("flip to IFVG", "full-fill flip", "second close deletes",
           "deep mitigation only"),
          DefaultPolicy.BLOCK, "[PP:C2]", "IFVG"),
    _spec("V18", "BPR polarity + existence",
          _cross(("first gap", "newer gap", "entry side"),
                 ("overlap-only", "both-traded-through")),
          DefaultPolicy.BLOCK, "[PP:C3/C6]", "BPR"),
    _spec("V19", "OB zone + gates",
          _cross(("full range", "body", "unspecified"),
                 ("break required", "imbalance constitutive", "grab")),
          DefaultPolicy.BLOCK, "[PP:C4/C5]", "OB"),
    _spec("V20", "breaker failure + geometry",
          _cross(("trade-through", "close", "full body", "buffered 0.10·ATR"),
                 ("G1", "G2", "G3", "G4")),
          DefaultPolicy.BLOCK, "[PP:C7/C8]", "breaker"),
    _spec("V21", "mitigation ontology",
          ("new block at a failed swing", "state of an existing OB"),
          DefaultPolicy.BLOCK, "[PP:C11]", "mitigation"),
    _spec("V22", "IFVG removal",
          ("close-back", "dead-return", "second close", "never"),
          DefaultPolicy.BLOCK, "[PP:C10]", "IFVG lifecycle"),
    _spec("V23", "rejection cardinality + bounds",
          _cross(("one candle", "cluster"),
                 ("[body, wick]", "[close, wick]")),
          DefaultPolicy.BLOCK, "[PP:C21]", "rejection"),
    _spec("V24", "CISD reference + unit",
          _cross(("leg-start open", "extreme open", "last opposing close"),
                 ("1", "≥2")),
          DefaultPolicy.BLOCK, "[PP:C19/C20]", "CISD"),
    _spec("V25", "displacement detector",
          ("one of the four declared forms", "labelled repo detector"),
          DefaultPolicy.BLOCK, "[PP:C17]", "DP1",
          notes="PP:C17 names four detectors; an alternative must be explicitly "
                "labelled repo, never silently substituted."),
    _spec("V26", "array priority",
          ("declared explicit ordering",),
          DefaultPolicy.BLOCK, "[PP:C24]", "A1 ordering",
          notes="The corpus supplies no ranking; a repo ordering must be "
                "declared explicitly."),
    _spec("V27", "OTE bounds + anchor",
          ("0.62–0.79", "0.618–0.79", "0.618–0.786"),
          DefaultPolicy.BLOCK, "[PP:C13/C14]", "OTE model",
          notes="§14.2 defers the anchor to one of seven attested anchors; it "
                "is declared alongside the selected bounds."),
    _spec("V28", "RR mode",
          ("floor_3_1", "floor_session_1_5", "filter_2_1", "none"),
          DefaultPolicy.BLOCK, "[RE:C-01]", "admission"),
    _spec("V29", "stop policy",
          ("one anchor + one distance construction",),
          DefaultPolicy.BLOCK, "[RE:C-03/C-04]", "PS1",
          notes="One anchor and one distance construction are selected from the "
                "attested families; a second anchor is not a fallback."),
    _spec("V30", "risk band",
          ("one attested band + source_id",),
          DefaultPolicy.BLOCK, "[RE:C-02]", "sizing",
          notes="RK-07 requires the band's source id to travel with the value."),
    _spec("V31", "ladder + BE",
          ("level-anchored", "R-anchored"),
          DefaultPolicy.MODE, "[RE:C-05/C-06]", "PS1",
          notes="The BE trigger is declared separately (MG-08) and MODE must "
                "include off (never)."),
    _spec("V32", "target ranking",
          ("nearest", "quality/magnet", "furthest"),
          DefaultPolicy.BLOCK, "[RE:C-16]", "target"),
    _spec("V33", "holding class",
          ("intraday", "swing", "position"),
          DefaultPolicy.MODE, "[RE:C-22/C-23]", "management",
          notes="Session flat vs do-not-exit is a MODE via holding_class."),
    _spec("V34", "seasonal gate",
          ("mandatory", "counted 2 of 4", "partial", "off"),
          DefaultPolicy.BLOCK, "[RE:C-29]", "D1 if enabled"),
    _spec("V35", "daily budget reset",
          ("NY true day", "UTC date", "broker day"),
          DefaultPolicy.BLOCK, "[RE:C-26]", "budget"),
)

#: The one register of §14.2.  Read-only: no module may replace or extend it at
#: run time (A7); a new fork means a spec change, not a private dict entry.
VARIANT_REGISTER: Mapping[str, VariantSpec] = MappingProxyType(
    {spec.id: spec for spec in _VARIANT_ROWS}
)


def read_variant_spec(variant_id: str) -> VariantSpec:
    """The single spec read path: ``V01``–``V35`` or :class:`UnknownVariant`."""
    key = str(variant_id).strip().upper()
    try:
        return VARIANT_REGISTER[key]
    except KeyError:
        raise UnknownVariant(variant_id) from None


# --------------------------------------------------------------------------- #
# The run's declared selections: one read path, no default
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class VariantConfig:
    """The declared selection for each operational fork.

    ``selections`` maps a variant id to exactly one of its registered options.
    Construction validates the ids and options, so an invalid declaration can
    never reach a guard, and normalizes the field to an immutable, register
    ordered tuple so copy/deepcopy/pickle and ``dataclasses.asdict`` all work.
    The only selection read path is :meth:`read`; it raises
    :class:`VariantUndeclared` for a missing fork regardless of the row's
    ``default_policy``.  :meth:`check` is the first-class refusal form (A6) for
    callers that must continue and emit ``NT_VARIANT_UNDECLARED``.
    """

    selections: Mapping[str, str] | tuple[tuple[str, str], ...] = field(
        default_factory=dict)

    def __post_init__(self) -> None:
        declared: dict[str, str] = {}
        for raw_id, raw_option in dict(self.selections or {}).items():
            try:
                spec = read_variant_spec(str(raw_id))
            except UnknownVariant:
                raise VariantUndeclared(
                    str(raw_id), "not in the V01-V35 register") from None
            if not isinstance(raw_option, str):
                raise ValueError(
                    f"{spec.id}: option must be a string, got {type(raw_option).__name__}")
            option = raw_option.strip()
            if not option:
                # An empty declaration is not a declaration: fail closed with
                # the typed refusal instead of choosing the first option.
                raise VariantUndeclared(
                    spec.id, "empty option; the fork is still undeclared")
            if not spec.accepts(option):
                raise ValueError(
                    f"{spec.id}: {option!r} is not a declared option; "
                    f"choose one of {spec.options}")
            declared[spec.id] = option
        # Register order makes the tuple canonical for equality, hashing and
        # audit output, independent of the caller's insertion order.
        object.__setattr__(
            self, "selections",
            tuple((vid, declared[vid]) for vid in VARIANT_REGISTER
                  if vid in declared))

    def _as_mapping(self) -> dict[str, str]:
        return dict(self.selections)

    # -- one read path ------------------------------------------------------ #
    def read(self, variant_id: str) -> str:
        """Return the one declared option for ``variant_id``.

        This is the only way a consumer may obtain a variant selection.  A
        missing declaration refuses with ``NT_VARIANT_UNDECLARED``; it never
        returns a default, the first option, or the documented repo default.
        """
        try:
            spec = read_variant_spec(variant_id)
        except UnknownVariant:
            raise VariantUndeclared(
                str(variant_id), "not in the V01-V35 register") from None
        try:
            return self._as_mapping()[spec.id]
        except KeyError:
            raise VariantUndeclared(spec.id, spec.name) from None

    def check(self, variant_id: str) -> Refusal | None:
        """The fail-closed result form of :meth:`read` (A6).

        Declared: ``None``.  Undeclared or unknown: a :class:`Refusal` whose
        code is ``NT_VARIANT_UNDECLARED``.  No option is ever substituted.
        """
        try:
            self.read(variant_id)
        except VariantUndeclared as exc:
            return exc.refusal
        return None

    # -- introspection ------------------------------------------------------ #
    def is_declared(self, variant_id: str) -> bool:
        try:
            spec = read_variant_spec(variant_id)
        except UnknownVariant:
            return False
        return spec.id in self._as_mapping()

    @property
    def declared_ids(self) -> tuple[str, ...]:
        """The declared variant ids in register order."""
        return tuple(vid for vid, _ in self.selections)

    def as_dict(self) -> dict[str, str]:
        """A deterministic copy for audit records and the config hash."""
        return self._as_mapping()

    def with_selection(self, variant_id: str, option: str) -> "VariantConfig":
        """Return a new config with one fork declared (never mutates ``self``)."""
        selections = self._as_mapping()
        selections[read_variant_spec(variant_id).id] = option
        return VariantConfig(selections)

    def __contains__(self, variant_id: object) -> bool:
        return self.is_declared(str(variant_id))

    def __len__(self) -> int:
        return len(self.selections)
