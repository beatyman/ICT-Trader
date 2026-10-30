"""Inject Kronos forecasts into an ICT plan without touching the GS/L rules.

The single public function is :func:`filter_plan_with_kronos`.  It is a pure
function: it reads an ICT ``plan`` dict and a :class:`KronosForecast` dict and
returns an updated plan.  Nothing here imports the ICT engine or Kronos.
"""
from __future__ import annotations

import copy

import pandas as pd

# plan_status values this module may produce
REJECTED_KRONOS = "REJECTED_KRONOS"
REJECTED_KRONOS_TRAP = "REJECTED_KRONOS_TRAP"
CONDITIONAL = "CONDITIONAL"
TRADE = "TRADE"
TRIGGERED = "TRIGGERED"


def _dir_from_plan(plan: dict) -> str:
    d = plan.get("direction")
    return "long" if d in {"bull", "long"} else "short"


def _aligns(plan_dir: str, k_dir: str) -> bool:
    return plan_dir == k_dir


def filter_plan_with_kronos(
    plan: dict,
    kronos_forecast: dict,
    mode: str = "strict",
    conf_threshold: float = 0.6,
) -> dict:
    """Combine an ICT plan with one Kronos forecast.

    Parameters
    ----------
    plan :
        ICT plan dict: ``direction``, ``entry``, ``sl``, ``tp``,
        ``plan_status``, optionally ``swept`` / ``triggered`` flags.
    kronos_forecast :
        One row of the adapter CSV: ``predicted_direction``, ``confidence``,
        ``expected_return``, ``predicted_max_drawdown``.
    mode :
        One of ``"strict"`` (soft gate), ``"judas"`` (Mode A),
        ``"momentum"`` (Mode B), ``"target"`` (Mode C).
    conf_threshold :
        Confidence above which a same-direction forecast is treated as a boost.

    Returns
    -------
    A *new* plan dict (the input is not mutated) with updated ``plan_status``
    and a human-readable ``filter_reason``.
    """
    out = copy.deepcopy(plan)
    plan_dir = _dir_from_plan(plan)
    k_dir = kronos_forecast.get("predicted_direction", "flat")
    conf = float(kronos_forecast.get("confidence", 0.0))
    exp_ret = float(kronos_forecast.get("expected_return", 0.0))
    k_dd = float(kronos_forecast.get("predicted_max_drawdown", 0.0))

    out["filter_reason"] = ""
    out["kronos"] = {
        "predicted_direction": k_dir,
        "confidence": conf,
        "expected_return": exp_ret,
    }

    # 1) Mode A — Judas / trap filter (sweep + reversal predicted).
    if mode == "judas" and plan.get("swept") and k_dir != "flat":
        if not _aligns(plan_dir, k_dir):
            out["plan_status"] = REJECTED_KRONOS_TRAP
            out["filter_reason"] = (
                "kronos predicts reversal back to sweep origin (possible Judas)"
            )
            return out

    # 2) hard rejection: opposite direction.
    if k_dir not in {"flat"} and not _aligns(plan_dir, k_dir):
        out["plan_status"] = REJECTED_KRONOS
        out["filter_reason"] = (
            f"kronos direction {k_dir} opposes plan {plan_dir}"
        )
        return out

    # 3) low confidence -> downgrade to conditional.
    if conf < conf_threshold:
        out["plan_status"] = CONDITIONAL
        out["filter_reason"] = f"kronos confidence {conf:.2f} < {conf_threshold}"
        return out

    # 4) Mode B — momentum boost: same direction + strong forecast.
    if mode == "momentum" and _aligns(plan_dir, k_dir) and conf >= conf_threshold:
        out["plan_status"] = TRIGGERED
        out["filter_reason"] = (
            f"kronos confirms {plan_dir} (conf {conf:.2f}, ret {exp_ret:+.4f})"
        )
        return out

    # 5) Mode C — target fine-tuning: cap TP at Kronos predicted extreme.
    if mode == "target":
        tp = float(out.get("tp", 0.0))
        entry = float(out.get("entry", 0.0))
        k_extreme = entry + k_dd  # k_dd is signed worst excursion
        if plan_dir == "long":
            # Kronos path suggests an earlier ceiling than ICT DOL
            if k_extreme < tp and k_extreme > entry:
                out["tp"] = round(k_extreme, 8)
                out["filter_reason"] = (
                    f"tp capped to kronos extreme {out['tp']} (< ICT DOL {tp})"
                )
        else:
            if k_extreme > tp and k_extreme < entry:
                out["tp"] = round(k_extreme, 8)
                out["filter_reason"] = (
                    f"tp raised to kronos extreme {out['tp']} (< ICT DOL {tp})"
                )

    # default: same direction + confident, keep whatever ICT said.
    if out.get("plan_status") in (None, "", "conditional"):
        out["plan_status"] = CONDITIONAL
    return out


def batch_filter(plans: list[dict], forecasts: pd.DataFrame, mode: str = "strict"):
    """Join plans to cached forecasts by ``as_of`` timestamp."""
    fmap = {str(r["as_of"]): r for r in forecasts.to_dict("records")}
    out = []
    for p in plans:
        f = fmap.get(str(p.get("last_time")))
        if f is None:
            out.append(p)
            continue
        out.append(filter_plan_with_kronos(p, f, mode=mode))
    return out


if __name__ == "__main__":
    import pandas as pd

    plan = {
        "direction": "long",
        "entry": 100.0,
        "sl": 99.0,
        "tp": 103.0,
        "plan_status": "conditional",
        "swept": True,
    }
    fc = {
        "as_of": "2026-10-30 16:45:00",
        "predicted_direction": "short",
        "confidence": 0.8,
        "expected_return": -0.01,
        "predicted_max_drawdown": -1.2,
    }
    for mode in ("strict", "judas", "momentum", "target"):
        print(mode, "->", filter_plan_with_kronos(plan, fc, mode=mode)["plan_status"])
    # same-direction case
    fc2 = dict(fc, predicted_direction="long", confidence=0.75, predicted_max_drawdown=2.5)
    print("target(same dir) ->", filter_plan_with_kronos(plan, fc2, mode="target")["tp"])
