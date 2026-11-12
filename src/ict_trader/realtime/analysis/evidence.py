"""Small, deterministic ICT evidence helpers.

No strategy thresholds live here.  These functions only map already-computed
events into JSON-safe evidence and record explicit data gaps.

P1-07 adds two objects to this boundary:

* the **V25 displacement detector** (:class:`DisplacementSpec`,
  :func:`evaluate_displacement`).  The corpus supplies no magnitude gate
  (DP-04), so a detector form that needs one refuses with
  ``NT_NO_DISPLACEMENT`` until the run declares the value; no default is
  invented (F10);
* the **closed-bar guard** (:func:`last_bar_closed`) used by the engine before
  it evaluates anything (CB-01/CB-10).

P1-04 adds the opening-price boundary, :func:`midnight_open_evidence`: it
carries the midnight open as a price object, its declared V05 reading
(DB-01/DB-02), and DB-03's two-price comparison against the daily CE.  The two
booleans are separate rules (``daily_bias`` C5) and an undeclared V05 is a
coded refusal, never a majority default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from ...context import (DAILY_POLARITY_SOURCES, BiasDirection,
                        BiasInvalidation, BiasState, DEFAULT_NEUTRAL_HANDLING,
                        JudasWindow, MidnightOpen, RevisionEvent,
                        daily_open_polarity, daily_polarity_mode,
                        open_vs_daily_ce, parse_bias, serialize_bias,
                        validate_neutral_handling, window_status)
from ...variants import Refusal, RefusalCode, VariantConfig, VariantUndeclared

#: §12 refusal codes used by this boundary.  They are read from the one typed
#: vocabulary (:class:`~ict_trader.variants.RefusalCode`), never re-typed, and
#: the engine re-exports them for backwards compatibility.
NT_NO_DISPLACEMENT = RefusalCode.NT_NO_DISPLACEMENT.value
NT_NO_SHIFT = RefusalCode.NT_NO_SHIFT.value
NT_SHIFT_UNCONFIRMED = RefusalCode.NT_SHIFT_UNCONFIRMED.value
NT_DATA_MISSING = RefusalCode.NT_DATA_MISSING.value
NT_VARIANT_UNDECLARED = RefusalCode.NT_VARIANT_UNDECLARED.value


#: V15 ``sweep direction vs bias``: the corpus attests three mutually exclusive
#: readings and the repo used to hardcode the first one with no declaration.
#: ``sweep_sets_direction`` is the labelled repo default; the other two require
#: the caller to supply the bias they compare against.
SWEEP_DIRECTION_MODES = ("sweep_sets_direction", "oppose_bias",
                         "match_daily_bias")
DEFAULT_SWEEP_DIRECTION_MODE = "sweep_sets_direction"

SWEEP_DIRECTION_SOURCES: dict[str, dict[str, str]] = {
    "sweep_sets_direction": {
        "label": "repo",
        "source": "V15 reading 1 (sweep side sets the trade side)"},
    "oppose_bias": {
        "label": "repo",
        "source": "V15 reading 2 (sweep must oppose the prevailing bias)"},
    "match_daily_bias": {
        "label": "repo",
        "source": "V15 reading 3 (sweep must match the daily bias)"},
}


def _validate_sweep_mode(mode: str) -> str:
    value = str(mode).strip().lower()
    if value not in SWEEP_DIRECTION_MODES:
        raise ValueError(
            f"unknown sweep_direction_mode {mode!r}; declare one of "
            f"{SWEEP_DIRECTION_MODES} (V15)")
    return value


def _bias_gate(direction: str, bias: object, *, mode: str,
               what: str) -> bool:
    """Apply the declared V15 reading against a supplied bias state."""
    if bias is None:
        raise ValueError(
            f"sweep_direction_mode={mode!r} requires a {what} bias; the "
            "declared reading cannot be evaluated without it (V15)")
    state = parse_bias(bias)
    side = parse_bias(direction)
    if mode == "oppose_bias":
        # A neutral bias neither opposes nor matches, so it never satisfies
        # "must oppose": the reading fails closed (BI-06 no_trade).
        opposing = (BiasDirection.BEAR if side is BiasDirection.BULL
                    else BiasDirection.BULL)
        return state is opposing
    return state is side


def extract_sweep(sweeps: list[dict], *, direction: str,
                  source_kline_open_time: int,
                  sweep_direction_mode: str = DEFAULT_SWEEP_DIRECTION_MODE,
                  bias: object = None,
                  daily_bias: object = None) -> dict | None:
    """The one recent sweep that satisfies the declared V15 reading.

    The sweep *side* is always the counter-side of the trade (long ⇒ an SSL
    sweep, short ⇒ a BSL sweep).  Which bias that sweep must agree with is a
    declared fork: ``sweep_sets_direction`` (the labelled repo default) needs
    no bias input; ``oppose_bias`` requires ``bias`` and rejects a sweep that
    agrees with it; ``match_daily_bias`` requires ``daily_bias`` and rejects a
    sweep that disagrees with it.  A missing context refuses instead of falling
    back to the unlabelled reading.
    """
    mode = _validate_sweep_mode(sweep_direction_mode)
    want_type = "SSL" if direction == "long" else "BSL"
    want_bias = "bullish" if direction == "long" else "bearish"
    if mode == "oppose_bias":
        if not _bias_gate(direction, bias, mode=mode, what="prevailing"):
            return None
    elif mode == "match_daily_bias":
        if not _bias_gate(direction, daily_bias, mode=mode, what="daily"):
            return None
    candidates = [
        s for s in sweeps
        if s.get("sweep_type") == want_type
        and s.get("direction") == want_bias
        and int(s.get("time", 0)) <= int(source_kline_open_time)
        and int(source_kline_open_time) <= int(s.get("expires_at", 0))
    ]
    if not candidates:
        return None
    s = max(candidates, key=lambda x: int(x["time"]))
    return {
        "type": s["sweep_type"],
        "direction": s["direction"],
        "time": int(s["time"]),
        "level": float(s["level"]),
        # doc 69 L181: the stop anchors on the sweep extreme, not on `level`.
        "extreme": float(s.get("sweep_price") or s["level"]),
        "reclaim_atr": float(s.get("reclaim_strength", 0.0)),
        "expires_at": int(s["expires_at"]),
        "sweep_direction_mode": mode,
    }


_MSS_DIRECTIONS = {
    "long": "bull", "bull": "bull", "bullish": "bull",
    "short": "bear", "bear": "bear", "bearish": "bear",
}


def structure_evidence(events: list[Any], end_idx: int,
                       times: list[int], *,
                       direction: str | None = None,
                       after_index: int | None = None) -> dict[str, Any] | None:
    """Latest **MSS** at/before ``end_idx`` -- never a BOS.

    The §8.1 conjunction requires an MSS *after* the sweep, not "any structure
    event": a BOS extends the existing trend and cannot confirm the reversal
    the setup needs (ST-06/ST-07; ``structure_arrays.md`` P0-17).  When
    ``after_index`` is supplied (the sweep bar), only MSS events at or after it
    are eligible.  ``direction`` is the entry side (long/short) and filters the
    MSS to the same shift, so a counter-direction break cannot be attached to
    the setup.
    """
    wanted = None
    if direction is not None:
        wanted = _MSS_DIRECTIONS.get(str(direction).strip().lower())
        if wanted is None:
            raise ValueError(f"unknown direction: {direction!r}")
    best: tuple[int, Any] | None = None
    for e in events:
        if str(getattr(e, "kind", "")) != "MSS":
            continue
        if not bool(getattr(e, "confirmed", True)):
            continue  # an unconfirmed label is never admissible (ST-08)
        try:
            idx = int(getattr(e, "index"))
        except (TypeError, ValueError):
            continue
        if idx > int(end_idx):
            continue
        if after_index is not None and idx < int(after_index):
            continue
        if wanted is not None and str(getattr(e, "direction", "")) != wanted:
            continue
        if best is None or idx > best[0]:
            best = (idx, e)
    if best is None:
        return None
    idx, e = best
    return {
        "event": str(e.kind),
        "direction": "bullish" if e.direction == "bull" else "bearish",
        "price": float(e.price),
        "time": int(times[min(idx, len(times) - 1)]) if times else 0,
    }


def unconfirmed_shift_evidence(events: list[Any], end_idx: int,
                               times: list[int] | None = None, *,
                               direction: str | None = None,
                               after_index: int | None = None
                               ) -> dict[str, Any] | None:
    """The latest shift when it is a CHoCH **not yet confirmed** (ST-08).

    The corpus's CHoCH is a warning only: it "stays unconfirmed" until the first
    BOS in the new direction (MS-R25), so a setup built on it must be refused
    with ``NT_SHIFT_UNCONFIRMED`` rather than treated as a missing MSS.  Only
    the latest matching structure event is considered -- an older unconfirmed
    CHoCH superseded by a later break is history, not the current state.
    """
    wanted = None
    if direction is not None:
        wanted = _MSS_DIRECTIONS.get(str(direction).strip().lower())
        if wanted is None:
            raise ValueError(f"unknown direction: {direction!r}")
    best: tuple[int, Any] | None = None
    for e in events:
        try:
            idx = int(getattr(e, "index"))
        except (TypeError, ValueError):
            continue
        if idx > int(end_idx):
            continue
        if after_index is not None and idx < int(after_index):
            continue
        if wanted is not None and str(getattr(e, "direction", "")) != wanted:
            continue
        if best is None or idx > best[0]:
            best = (idx, e)
    if best is None:
        return None
    idx, e = best
    if str(getattr(e, "kind", "")) != "CHoCH":
        return None
    if bool(getattr(e, "confirmed", True)):
        return None
    return {
        "event": str(e.kind),
        "direction": "bullish" if e.direction == "bull" else "bearish",
        "price": float(e.price),
        "time": (int(times[min(idx, len(times) - 1)]) if times else 0),
        "index": idx,
        "confirmed": False,
        "family": str(getattr(e, "family", "")),
        "level_selection": str(getattr(e, "level_selection", "")),
    }


#: Fields ``setup.build_setup`` records for its measured displacement.  These
#: are the raw measurements; the V25 gate below decides whether they establish
#: displacement, and it never converts a measurement into a threshold.
DISPLACEMENT_KEYS = ("disp_body_ratio", "disp_body_atr",
                     "disp_body_multiple", "disp_volume_ratio")
#: At least one *body* measure is required; a volume ratio alone is not a
#: displacement reading.
_REQUIRED_DISPLACEMENT_KEYS = ("disp_body_ratio", "disp_body_atr",
                               "disp_body_multiple")


def displacement_evidence(quality_parts: Any) -> dict[str, float] | None:
    """Measured displacement attached to a setup, or ``None`` when absent.

    ``quality_parts`` comes from ``setup.build_setup``.  At least one body
    measure (body/range, body/ATR or body/average-body) is required: a volume
    ratio alone does not establish expansion.  Absence is a refusal, never an
    implicit pass -- the V25 gate below records ``NT_NO_DISPLACEMENT`` and
    ``ICTEngine`` emits no signal (P0-17, DP-07).
    """
    if quality_parts is None or not hasattr(quality_parts, "get"):
        return None
    if not any(key in quality_parts for key in _REQUIRED_DISPLACEMENT_KEYS):
        return None
    out: dict[str, float] = {}
    for key in DISPLACEMENT_KEYS:
        if key in quality_parts:
            try:
                out[key] = float(quality_parts[key])
            except (TypeError, ValueError):
                return None
    return out


# ---------------------------------------------------------------------------
# V25 displacement detector (P1-07, DP-01..DP-07, PP:C17)
# ---------------------------------------------------------------------------
#: PP:C17 attests **four** incompatible detector forms, none of them ICT, and
#: says outright that any single one is "choosing one of four".  ``V25``
#: therefore blocks the DP1 gate unless the run picks one form -- or the
#: explicitly *labelled repo* detector (P1-01 option).
DISPLACEMENT_FORM_RANGE_ATR = "range_atr_and_body_ratio"
DISPLACEMENT_FORM_BODY_RATIO_ATR = "body_ratio_atr_close"
DISPLACEMENT_FORM_BODY_RATIO = "body_ratio"
DISPLACEMENT_FORM_BODY_MULTIPLE = "body_multiple_wick_volume"
DISPLACEMENT_FORMS = (
    DISPLACEMENT_FORM_RANGE_ATR,
    DISPLACEMENT_FORM_BODY_RATIO_ATR,
    DISPLACEMENT_FORM_BODY_RATIO,
    DISPLACEMENT_FORM_BODY_MULTIPLE,
)
#: The labelled repo detector: it **grades** the body measures (DP-04: magnitude
#: is `missing` and is graded, never bounded) and adds no magnitude gate of its
#: own.
REPO_DISPLACEMENT_FORM = "repo_measured_body"

#: The V25 option -> the forms it permits.  ``one of the four declared forms``
#: still needs the form named; the repo option has exactly one form.
V25_OPTIONS = (
    "one of the four declared forms",
    "labelled repo detector",
)
V25_FORMS: dict[str, tuple[str, ...]] = {
    "one of the four declared forms": DISPLACEMENT_FORMS,
    "labelled repo detector": (REPO_DISPLACEMENT_FORM,),
}

#: Threshold field -> the measure it gates.  ``min_*`` is a lower bound,
#: ``max_*`` an upper bound.
DISPLACEMENT_THRESHOLD_MEASURES: dict[str, str] = {
    "min_body_ratio": "body_ratio",
    "min_body_atr": "body_atr",
    "min_body_multiple": "body_multiple",
    "min_range_atr": "range_atr",
    "min_close_atr": "close_atr",
    "min_volume_ratio": "volume_ratio",
    "max_wick_ratio": "wick_ratio",
}

#: The magnitudes each form requires the run to declare.  DP-04: the corpus
#: supplies **none** of them, so every entry below is a run declaration, never
#: a corpus rule.  The repo form requires none because it grades only.
DISPLACEMENT_FORM_THRESHOLDS: dict[str, tuple[str, ...]] = {
    DISPLACEMENT_FORM_RANGE_ATR: ("min_range_atr", "min_body_ratio"),
    DISPLACEMENT_FORM_BODY_RATIO_ATR: (
        "min_body_ratio", "min_range_atr", "min_close_atr"),
    DISPLACEMENT_FORM_BODY_RATIO: ("min_body_ratio",),
    DISPLACEMENT_FORM_BODY_MULTIPLE: (
        "min_body_multiple", "max_wick_ratio", "min_volume_ratio"),
    REPO_DISPLACEMENT_FORM: (),
}

DISPLACEMENT_FORM_LABELS: dict[str, str] = {
    DISPLACEMENT_FORM_RANGE_ATR: "corpus (PP:C17 form a)",
    DISPLACEMENT_FORM_BODY_RATIO_ATR: "corpus (PP:C17 form b)",
    DISPLACEMENT_FORM_BODY_RATIO: "corpus (PP:C17 form c)",
    DISPLACEMENT_FORM_BODY_MULTIPLE: "corpus (PP:C17 form d, vendor)",
    REPO_DISPLACEMENT_FORM: "repo",
}

#: The measure a detector reads.  ``range_atr = body_atr / body_ratio`` exactly,
#: and ``wick_ratio = 1 - body_ratio``; the remaining fields (notably
#: ``close_atr``) are not recorded by the setup layer, so a form that requires
#: one refuses instead of approximating it.
def _derived_range_atr(raw: dict[str, float]) -> float | None:
    ratio = raw.get("disp_body_ratio")
    body_atr = raw.get("disp_body_atr")
    if ratio is None or body_atr is None or float(ratio) <= 0:
        return None
    return float(body_atr) / float(ratio)


def _derived_wick_ratio(raw: dict[str, float]) -> float | None:
    ratio = raw.get("disp_body_ratio")
    return None if ratio is None else 1.0 - float(ratio)


_DISPLACEMENT_DERIVED = {
    "body_ratio": lambda raw: raw.get("disp_body_ratio"),
    "body_atr": lambda raw: raw.get("disp_body_atr"),
    "body_multiple": lambda raw: raw.get("disp_body_multiple"),
    "volume_ratio": lambda raw: raw.get("disp_volume_ratio"),
    "wick_ratio": _derived_wick_ratio,
    "range_atr": _derived_range_atr,
}


@dataclass(frozen=True)
class DisplacementSpec:
    """One declared V25 displacement detector and its run-declared magnitudes.

    The form is validated against :data:`V25_FORMS` and every threshold is
    optional; :meth:`missing_thresholds` names the ones the form still needs.
    A form with a non-empty ``missing_thresholds`` **cannot** evaluate: DP-04
    supplies no magnitude, so the caller must declare it (F10).  The repo form
    has no threshold on purpose -- DP-04 says magnitude is graded, never
    bounded.
    """

    form: str
    variant_option: str = ""
    label: str = ""
    min_body_ratio: float | None = None
    min_body_atr: float | None = None
    min_body_multiple: float | None = None
    min_range_atr: float | None = None
    min_close_atr: float | None = None
    min_volume_ratio: float | None = None
    max_wick_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.form not in DISPLACEMENT_FORM_THRESHOLDS:
            raise ValueError(
                f"unknown V25 displacement form {self.form!r}; declare one of "
                f"{DISPLACEMENT_FORMS + (REPO_DISPLACEMENT_FORM,)} (PP:C17)")
        if self.variant_option and self.form not in V25_FORMS.get(
                self.variant_option, ()):
            raise ValueError(
                f"V25 option {self.variant_option!r} does not permit form "
                f"{self.form!r}")
        for name in DISPLACEMENT_THRESHOLD_MEASURES:
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, float(value))

    @property
    def thresholds(self) -> dict[str, float]:
        return {name: float(getattr(self, name))
                for name in DISPLACEMENT_THRESHOLD_MEASURES
                if getattr(self, name) is not None}

    def missing_thresholds(self) -> tuple[str, ...]:
        """The magnitudes this form needs and the run has not declared."""
        return tuple(name for name in DISPLACEMENT_FORM_THRESHOLDS[self.form]
                     if getattr(self, name) is None)

    def to_public(self) -> dict[str, object]:
        return {
            "form": self.form,
            "label": self.label or DISPLACEMENT_FORM_LABELS[self.form],
            "variant_option": self.variant_option,
            "thresholds": self.thresholds,
            "missing_thresholds": list(self.missing_thresholds()),
        }


def default_displacement_spec() -> DisplacementSpec:
    """The labelled repo detector used when a run declares no V25 form.

    It is the P0-17 measured-body conjunct without a magnitude gate: DP-04
    grades magnitude and the corpus bounds none, so this default never turns a
    measured body ratio into a pass/fail number.
    """
    return DisplacementSpec(
        form=REPO_DISPLACEMENT_FORM,
        variant_option="labelled repo detector",
        label="repo")


def displacement_spec_from_variants(
    variants: VariantConfig,
    *,
    form: str | None = None,
    **thresholds: float,
) -> DisplacementSpec:
    """Build a :class:`DisplacementSpec` from the declared V25 option.

    ``VariantConfig.read`` is the one selection path (A7): an undeclared V25
    raises :class:`~ict_trader.variants.VariantUndeclared`, and the four-form
    option must also name its form because the register records only "one of
    the four", not which one.
    """
    option = variants.read("V25")
    allowed = V25_FORMS[option]
    if form is None:
        if len(allowed) == 1:
            form = allowed[0]
        else:
            raise VariantUndeclared(
                "V25", "the four-form option must also name its form")
    if form not in allowed:
        raise ValueError(
            f"V25 option {option!r} does not permit form {form!r}")
    return DisplacementSpec(
        form=form, variant_option=option,
        label=DISPLACEMENT_FORM_LABELS[form], **thresholds)


def _displacement_measures(quality_parts: Any) -> dict[str, float] | None:
    raw = displacement_evidence(quality_parts)
    if raw is None:
        return None
    out: dict[str, float] = {}
    for name, read in _DISPLACEMENT_DERIVED.items():
        try:
            value = read(raw)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            out[name] = float(value)
    return out


def evaluate_displacement(quality_parts: Any,
                          spec: DisplacementSpec | None = None) -> dict[str, Any]:
    """Evaluate the declared V25 detector over ``quality_parts``.

    Returns a JSON-safe verdict: ``ok``, the §12 ``code`` when it fails, the
    form/label, the declared thresholds, the measured values and the checks.
    The reasons are ordered so a missing *declaration* is never reported as a
    missing measurement:

    * no spec (V25 undeclared) -> ``NT_VARIANT_UNDECLARED``;
    * a form whose magnitudes are still missing -> ``NT_NO_DISPLACEMENT``
      (DP-04: the corpus supplies none, so no value is invented -- F10);
    * no body measurement -> ``NT_NO_DISPLACEMENT``;
    * a required measurement this layer does not record -> ``NT_NO_DISPLACEMENT``;
    * a measured body that misses a **declared** bound -> ``NT_NO_DISPLACEMENT``
      (DP-07: no displacement means take nothing).
    """
    if spec is None:
        return {
            "ok": False, "code": NT_VARIANT_UNDECLARED,
            "reason": ("V25 is undeclared, so the DP1 displacement gate cannot "
                       "be evaluated (F10/F12)"),
            "form": "", "label": "", "thresholds": {},
            "missing_thresholds": [], "measures": {}, "checks": {},
            "variant_option": "",
        }
    base: dict[str, Any] = {
        "ok": False, "code": None, "reason": "",
        "form": spec.form, "label": spec.label or DISPLACEMENT_FORM_LABELS[spec.form],
        "variant_option": spec.variant_option,
        "thresholds": spec.thresholds,
        "missing_thresholds": list(spec.missing_thresholds()),
        "measures": {}, "checks": {},
    }
    missing = spec.missing_thresholds()
    if missing:
        base.update(
            code=NT_NO_DISPLACEMENT,
            reason=(f"V25 form {spec.form!r} needs the declared magnitude(s) "
                    f"{list(missing)}; the corpus supplies none (DP-04), so no "
                    f"value is invented and DP1 refuses (F10)"))
        return base
    measures = _displacement_measures(quality_parts)
    if measures is None:
        base.update(code=NT_NO_DISPLACEMENT,
                    reason="no body displacement measure on the setup")
        return base
    base["measures"] = measures
    checks: dict[str, bool] = {}
    absent: list[str] = []
    for name, measure in DISPLACEMENT_THRESHOLD_MEASURES.items():
        threshold = getattr(spec, name)
        if threshold is None:
            continue
        value = measures.get(measure)
        if value is None:
            absent.append(measure)
            continue
        if name.startswith("max_"):
            checks[name] = bool(float(value) <= float(threshold))
        else:
            checks[name] = bool(float(value) >= float(threshold))
    base["checks"] = checks
    if absent:
        base.update(
            code=NT_NO_DISPLACEMENT,
            reason=(f"V25 form {spec.form!r} requires the measurement(s) "
                    f"{sorted(set(absent))}, which the setup layer does not "
                    f"record; the form refuses instead of approximating them"))
        return base
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        base.update(
            code=NT_NO_DISPLACEMENT,
            reason=(f"declared V25 check(s) failed: {failed} "
                    f"(DP-07: no displacement means take nothing)"))
        return base
    base.update(ok=True, reason="declared V25 detector satisfied")
    return base


# ---------------------------------------------------------------------------
# Closed-bar guard (CB-01/CB-10, RE:EX-01 ``window_closed_bar``)
# ---------------------------------------------------------------------------
def last_bar_closed(frame: Any, *, as_of_ms: int | None = None,
                    interval: str | None = None) -> bool:
    """Is the last bar of ``frame`` closed at the evaluation instant?

    A bar is *forming* exactly while ``as_of_ms`` lies inside its own
    ``[open_time, open_time + step)`` span.  An ``as_of`` outside the frame's
    last bar (a historical/replay window, or an instant after its close) means
    the bar is closed for the purpose of the guard.  When the step cannot be
    determined the caller's frame is taken as closed -- the guard never invents
    a bar boundary.
    """
    import pandas as pd

    from ..models import INTERVAL_MS

    if frame is None or len(frame) == 0:
        return False
    if as_of_ms is None:
        return True
    last_open = int(pd.Timestamp(frame["date"].iloc[-1]).timestamp() * 1000)
    step = INTERVAL_MS.get(str(interval or "").strip().lower())
    if step is None and len(frame) >= 2:
        prev = int(pd.Timestamp(frame["date"].iloc[-2]).timestamp() * 1000)
        step = last_open - prev
    if not step or step <= 0:
        return True
    now = int(as_of_ms)
    return not (last_open <= now < last_open + int(step))


def breaker_evidence(pd_arrays: list[Any], end_idx: int,
                     *, lookback: int = 500, limit: int = 3) -> list[dict[str, Any]]:
    out = []
    for a in pd_arrays:
        if str(getattr(a, "kind", "")) != "breaker":
            continue
        at = int(getattr(a, "confirmed_at", 0))
        if at > end_idx or end_idx - at > lookback:
            continue
        out.append({
            "direction": "bullish" if a.direction == "bull" else "bearish",
            "top": float(a.top),
            "bottom": float(a.bottom),
        })
    return out[-limit:]


def _box(b, kind: str, inverted: bool = False) -> dict:
    return {
        "kind": kind,
        "direction": "bullish" if getattr(b, "direction", "") == "bull" else "bearish",
        "top": float(b.top),
        "bottom": float(b.bottom),
        "created": int(getattr(b, "created", 0)),
        "confirmed_at": int(getattr(b, "confirmed_at", getattr(b, "created", 0))),
        "inverted": bool(inverted),
    }


def pd_array_evidence(result: Any, end_idx: int, *, limit: int = 5) -> dict:
    fvgs = [f for f in getattr(result, "fvgs", [])
            if int(getattr(f, "created", 0)) <= end_idx]
    obs = [o for o in getattr(result, "order_blocks", [])
           if int(getattr(o, "created", 0)) <= end_idx]
    arrays = [a for a in getattr(result, "pd_arrays", [])
              if int(getattr(a, "confirmed_at", 0)) <= end_idx]
    return {
        "fvg": [_box(f, "fvg", bool(getattr(f, "inverted", False)))
                for f in fvgs[-limit:]],
        "ifvg": [_box(f, "ifvg", True) for f in fvgs
                 if bool(getattr(f, "inverted", False))][-limit:],
        "ob": [_box(o, "ob") for o in obs[-limit:]],
        "pd_array": [{
            "kind": str(getattr(a, "kind", "")),
            "direction": "bullish" if getattr(a, "direction", "") == "bull" else "bearish",
            "top": float(a.top), "bottom": float(a.bottom),
        } for a in arrays[-limit:]],
    }


def hidden_ob_evidence(frame: Any, end_idx: int, *, limit: int = 3,
                       max_scan: int = 400) -> list[dict]:
    """Hidden order blocks near ``end_idx`` (doc 570).

    Bar indices are relative to the scanned window, so ``bars_ago`` is exposed
    instead of absolute indices.
    """
    from .hidden_ob import detect_hidden_ob

    if frame is None or len(frame) < 3 or end_idx < 1:
        return []
    lo = max(0, int(end_idx) - max_scan + 1)
    sub = frame.iloc[lo:int(end_idx) + 1]
    if len(sub) < 3:
        return []
    zones = detect_hidden_ob(sub)
    n = len(sub)
    out = []
    for z in zones[-limit:]:
        row = z.to_public()
        row.pop("first_bar", None)
        row.pop("second_bar", None)
        row["bars_ago"] = n - 1 - int(z.second_bar)
        out.append(row)
    return out


#: AS-04 requires the IFVG side tag's mode to be **declared**, never inferred
#: from whichever implementation happens to run.  The repo declares
#: ``by_position``: the side is the dealing-range half containing the IFVG CE.
#: ``valid_ifvg_context`` encodes the same reading, so the emitted ``side`` and
#: the context verdict cannot disagree (P0-14, AS-01..AS-04).
IFVG_SIDE_MODE = "by_position"
IFVG_SIDE_MODES = ("by_type", "by_position", "by_role")

_POSITION_SIDES = {
    "premium": "premium", "deep_premium": "premium", "above_range": "premium",
    "discount": "discount", "deep_discount": "discount", "below_range": "discount",
}


def ifvg_side(direction: str, pd_zone: str, *,
              side_mode: str = IFVG_SIDE_MODE) -> str:
    """Return the IFVG side tag under the declared ``side_mode`` (AS-04).

    Only the declared mode is implemented: labelling a position reading as
    ``by_type``/``by_role`` would silently swap AS-01/AS-02 for AS-03 on the
    arrays where they disagree, so an unsupported mode is refused.
    """
    if side_mode not in IFVG_SIDE_MODES:
        raise ValueError(f"unknown IFVG side_mode: {side_mode!r}")
    if side_mode != "by_position":
        raise ValueError(f"undeclared IFVG side_mode: {side_mode!r}")
    if str(direction).strip().lower() not in ("bullish", "bearish"):
        return "unknown"
    return _POSITION_SIDES.get(str(pd_zone), "unknown")


def ifvg_evidence(ifvgs: list[dict], dealing_range: Any = None) -> dict | None:
    """Annotate the latest IFVG with its doc-502 context and CE entry price.

    The side tag carries the declared ``side_mode`` with it (AS-04), so a
    reader can tell a by-position label from the by-type/by-role alternatives
    that the corpus also attests (P0-14).
    """
    from .pda_confluence import ifvg_ce, valid_ifvg_context

    if not ifvgs:
        return None
    best = ifvgs[-1]
    ce = ifvg_ce(best)
    zone = "unknown"
    if dealing_range is not None:
        try:
            zone = dealing_range.zone(ce)
        except Exception:  # pragma: no cover - zone() is total, kept defensive
            zone = "unknown"
    ok, reasons = valid_ifvg_context(str(best.get("direction", "")), zone)
    return {
        "direction": best.get("direction"), "top": best.get("top"),
        "bottom": best.get("bottom"), "ce": ce, "pd_zone": zone,
        "side": ifvg_side(str(best.get("direction", "")), zone),
        "side_mode": IFVG_SIDE_MODE,
        "tradable": bool(ok), "reasons": list(reasons),
    }


def rvol_evidence(frame: Any, end_idx: int, *, window: int = 20) -> float | None:
    if "volume" not in frame or end_idx < 1:
        return None
    vol = frame["volume"].astype(float).to_numpy()
    lo = max(0, end_idx - window)
    base = float(vol[lo:end_idx].mean()) if end_idx > lo else 0.0
    if base <= 0:
        return None
    return float(vol[end_idx] / base)


#: Repo pivot width used to build the HTF swings (``ICTConfig.swing_len``'s
#: default).  It is a repo value, not a corpus constant (P0-20 declares it).
_HTF_PIVOT_LEFT = 5


def htf_bias_states(frame: Any, end_idx: int, *, vocabulary: str,
                    variant_ids: Iterable[str] = (),
                    invalidation: BiasInvalidation | None = None,
                    revision_trigger: RevisionEvent | None = None,
                    neutral_handling: str = DEFAULT_NEUTRAL_HANDLING
                    ) -> dict[str, BiasState | None] | None:
    """One :class:`BiasState` per closed HTF tier (P1-02, §1.6).

    The reading rule is the HH/HL primitive (:func:`ict_trader.context.
    structure_bias`, BI-01), exactly as :func:`htf_bias_evidence` uses it.  The
    difference is the output type: a stored object with ``scope``/``as_of``/
    ``vocabulary``/``invalidation``/``revision_trigger``/``variant_ids`` rather
    than an ad-hoc dict, so one pre-session artefact can hold it and a later
    read cannot silently re-derive it.

    ``vocabulary`` is required: BI-03 makes the state vocabulary a declared
    fork (V03), and a reading the vocabulary cannot express refuses instead of
    being downgraded.  ``end_idx`` is the knowledge bar (``as_of``); the
    still-forming resample bucket is dropped before the swings are built, so
    an incomplete bucket never enters the state (CB-01/CB-07).

    ``invalidation``/``revision_trigger``/``variant_ids`` are the caller's
    declarations.  They are not inferred here; a ``BiasState`` reports every
    absent one through ``missing_fields``.
    """
    validate_neutral_handling(neutral_handling)
    import pandas as pd
    from ...concepts import find_pivots
    from ...context import structure_bias

    sub = frame.iloc[:end_idx + 1].copy()
    if len(sub) < 20:
        return None
    sub["date"] = pd.to_datetime(sub["date"], utc=True)
    last_base = pd.Timestamp(sub["date"].iloc[-1])
    sub = sub.set_index("date")
    out: dict[str, BiasState | None] = {}
    for rule in ("4h", "1D"):
        x = sub.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna().reset_index()
        # Left-labelled buckets are knowable only at their close.  Drop the
        # still-forming bucket so its partial OHLC (and in particular its
        # close) never reaches the bias attached to a signal (P0-01).
        if len(x):
            bucket_close = (pd.Timestamp(x["date"].iloc[-1])
                            + pd.tseries.frequencies.to_offset(rule))
            if bucket_close > last_base:
                x = x.iloc[:-1].reset_index(drop=True)
        if len(x) < 20:
            out[rule] = None
            continue
        swings = find_pivots(x, _HTF_PIVOT_LEFT, 1)
        out[rule] = BiasState(
            scope=rule,
            bias=parse_bias(structure_bias(swings, len(x) - 1)),
            vocabulary=vocabulary,
            as_of=int(end_idx),
            invalidation=invalidation,
            revision_trigger=revision_trigger,
            variant_ids=tuple(variant_ids),
        )
    return out


def htf_bias_evidence(frame: Any, end_idx: int, *,
                      neutral_handling: str = DEFAULT_NEUTRAL_HANDLING,
                      vocabulary: str = "+neutral") -> dict | None:
    """HH/HL bias for the 4H and 1D tiers, from **closed** buckets only.

    The bias primitive is :func:`ict_trader.context.structure_bias` (BI-01:
    HH/HL ⇒ bullish, LH/LL ⇒ bearish).  The realtime pivot/BOS read is a
    different, repo rule and is deliberately not used here (P0-09); it is
    named ``pivot_bos_bias`` in :mod:`.structure`.

    ``neutral_handling`` is the one declared BI-06/BI-07 choice; it is accepted
    here so the boundary validates it, while the returned bias stays the raw
    reading (a consumer applies the handling through
    :func:`ict_trader.context.neutral_blocks` / ``neutral_weight``).  The two
    alternative handlings are therefore no longer decided independently by
    setup, this module and ``mtf``.

    ``vocabulary`` is the declared BI-03/V03 choice; the default ``+neutral``
    is the repo's current three-state reading and is labelled as such.

    A left-labelled resample bucket is knowable only at its close, so the
    still-forming bucket is dropped before the swings are built: its partial
    OHLC must never reach the bias attached to a signal (P0-01, CB-01/CB-07).
    """
    states = htf_bias_states(
        frame, end_idx, vocabulary=vocabulary,
        neutral_handling=neutral_handling)
    if states is None:
        return None
    return {
        rule: (None if state is None
               else {"bias": serialize_bias(state.bias)})
        for rule, state in states.items()
    }


def _refusal_record(refusal: Refusal | None) -> dict[str, object] | None:
    """A refusal as a JSON-safe record, or ``None`` when there is none."""
    if refusal is None:
        return None
    return {
        "code": refusal.code.value,
        "variant_id": refusal.variant_id,
        "detail": refusal.detail,
    }


def midnight_open_evidence(opening: MidnightOpen, *, price: object,
                           variant_config: VariantConfig,
                           daily_ce: object | None = None) -> dict:
    """P1-04 evidence: the opening **price object**, V05, and DB-03.

    The three objects are deliberately kept apart, because two of them share a
    name in the corpus and produce opposite trade sides on the same bar
    (``daily_bias`` C2/C5):

    * ``midnight_open`` is the price object (price, anchor instant, day key,
      V04 provenance) -- never a timestamp alone;
    * ``polarity`` is DB-01/DB-02 applied to the current ``price`` under the
      **declared** V05 option.  An undeclared V05 yields ``polarity=None`` and
      a coded ``polarity_refusal`` with ``NT_VARIANT_UNDECLARED``; no majority
      reading is substituted (§14.3 ``BLOCK``);
    * ``open_vs_daily_ce`` is DB-03's two-price comparison (the open against
      the daily CE), and it is never derived from ``price > ce`` (C5).  Without
      a ``daily_ce`` it is reported missing rather than fabricated.

    Every refusal travels as its §12 code (A3/A6); an empty value is never the
    evidence for a refusal.
    """
    if not isinstance(opening, MidnightOpen):
        raise TypeError(
            "midnight_open_evidence needs the MidnightOpen price object, got "
            f"{type(opening).__name__}")
    value = float(price)
    if not math.isfinite(value):
        raise ValueError(f"the compared price must be finite, got {price!r}")

    mode, mode_refusal = daily_polarity_mode(variant_config)
    reading: BiasDirection | None = None
    polarity_refusal: Refusal | None = mode_refusal
    if mode is not None:
        reading, polarity_refusal = daily_open_polarity(
            opening, value, polarity=mode)

    comparison = None
    comparison_refusal: Refusal | None = None
    if daily_ce is not None:
        comparison, comparison_refusal = open_vs_daily_ce(opening, daily_ce)

    missing: list[str] = []
    if mode is None or reading is None:
        missing.append("polarity")
    if daily_ce is None:
        missing.append("daily_ce")
    elif comparison is None:
        missing.append("open_vs_daily_ce")

    return {
        "midnight_open": opening.to_public(),
        "price": value,
        "polarity": (
            None if mode is None else {
                "variant_id": "V05",
                "mode": mode,
                "reading": ("" if reading is None else serialize_bias(reading)),
                "source": DAILY_POLARITY_SOURCES[mode],
            }),
        "polarity_refusal": _refusal_record(polarity_refusal),
        "daily_ce": (None if daily_ce is None else float(daily_ce)),
        "open_vs_daily_ce": (
            None if comparison is None else comparison.to_public()),
        "open_vs_daily_ce_refusal": _refusal_record(comparison_refusal),
        "missing_fields": missing,
    }


# --------------------------------------------------------------------------- #
# P1-10 -- one window evidence record over context.WINDOW_TABLE
# --------------------------------------------------------------------------- #
def window_evidence(ts, *, instrument: str | None = None,
                    input_tz: str | None = None,
                    market: str = "other") -> dict[str, object]:
    """The one window/dead-zone/non-window evidence record for an instant.

    It delegates every predicate to :func:`context.window_status`, so the
    realtime boundary cannot keep a second clock.  ``refusal`` carries the §12
    code (``NT_DEAD_ZONE`` / ``NT_OUT_OF_WINDOW`` / ``NT_DATA_MISSING``); the
    dead zone is data, not an empty window list (A3).
    """
    record = window_status(ts, instrument=instrument, input_tz=input_tz,
                           market=market).to_public()
    record["instrument"] = instrument
    return record


def judas_window_evidence(judas: JudasWindow, ts, *,
                          input_tz: str | None = None,
                          market: str = "other") -> dict[str, object]:
    """The declared Judas window's phase/entry verdict at ``ts`` (P1-10).

    ``phase == "blackout"`` is the corpus's observe-only inner interval
    (J-C5); ``allows_entry`` further applies the 05:00-07:00 dead zone and the
    named non-windows, so the Judas window cannot re-open a dead-zone bar.
    """
    if not isinstance(judas, JudasWindow):
        raise TypeError("judas_window_evidence needs a JudasWindow")
    return {
        "variant": judas.variant.name,
        "phase": judas.phase(ts, market=market, input_tz=input_tz),
        "allows_entry": judas.allows_entry(ts, market=market, input_tz=input_tz),
        "blackout_minutes": int(judas.blackout_minutes),
        "hard_close": judas.hard_close.strftime("%H:%M"),
    }
