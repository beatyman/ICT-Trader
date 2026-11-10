"""Dynamic ATR/structural risk model. Pure functions, no broker."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskPlan:
    stop: float
    targets: tuple[float, ...]
    gross_rr: float
    net_rr: float
    quantity: float
    sl_atr: float
    blockers: tuple[str, ...] = ()


def structural_stop(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None) -> float:
    buffer = max(0.25 * atr, 0.0)
    level = float((sweep or {}).get("level") or (structure or {}).get("price") or entry)
    if direction == "long":
        return min(level - buffer, entry - 1.0 * atr)
    return max(level + buffer, entry + 1.0 * atr)


def build_risk_plan(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None,
                    pools: list[float], *, equity: float = 100_000,
                    risk_pct: float = 0.005, fee_bps: float = 7.0) -> RiskPlan:
    blockers = []
    if atr <= 0:
        return RiskPlan(entry, (), 0, 0, 0, 0, ("atr_invalid",))
    stop = structural_stop(direction, entry, atr, sweep, structure)
    sl_atr = abs(entry - stop) / atr
    if not (1.0 <= sl_atr <= 3.0):
        blockers.append("sl_atr_out_of_bounds")
    valid = [float(p) for p in pools if (p > entry if direction == "long" else p < entry)]
    valid.sort(reverse=direction == "short")
    if not valid:
        return RiskPlan(stop, (), 0, 0, 0, sl_atr, tuple(blockers + ["no_liquidity_target"]))
    tp1 = valid[0] - 0.1 * atr if direction == "long" else valid[0] + 0.1 * atr
    risk = abs(entry - stop)
    gross = abs(tp1 - entry) / risk
    fees_r = (abs(entry) + abs(tp1)) * (fee_bps / 10_000.0) / risk
    net = gross - fees_r
    if net < 2.0:
        blockers.append("net_rr_below_2")
    qty = (equity * risk_pct) / risk
    return RiskPlan(stop, tuple(valid[:3]), gross, net, qty, sl_atr, tuple(blockers))
