"""Deterministic review aggregation for signal → setup → paper trade."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key) or "unknown")].append(row)
    out = []
    for name, items in sorted(buckets.items()):
        out.append({"key": name, **summarize(items)})
    return out


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if float(t.get("r_multiple", 0)) > 0]
    losses = [t for t in trades if float(t.get("r_multiple", 0)) < 0]
    pnls = [float(t.get("pnl", 0)) for t in trades]
    rs = [float(t.get("r_multiple", 0)) for t in trades]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    planned = [float(t.get("planned_rr", 0)) for t in trades
               if t.get("planned_rr") is not None]
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "net_pnl": round(sum(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "avg_r": round(sum(rs) / n, 3) if n else 0.0,
        "expectancy_r": round(sum(rs) / n, 3) if n else 0.0,
        "avg_planned_rr": round(sum(planned) / len(planned), 3) if planned else 0.0,
        "rr_gap": round(
            (sum(rs) / n) - (sum(planned) / len(planned)), 3
        ) if n and planned else None,
        "avg_bars": round(
            sum(float(t.get("bars_held", 0) or 0) for t in trades) / n, 2
        ) if n else 0.0,
    }


def build_review(trades: list[dict[str, Any]],
                 setups: list[dict[str, Any]]) -> dict[str, Any]:
    setup_by_id = {str(s.get("setup_id")): s for s in setups}
    for trade in trades:
        setup = setup_by_id.get(str(trade.get("signal_id"))) or setup_by_id.get(
            str(trade.get("setup_id"))
        )
        trade["setup_id"] = setup.get("setup_id") if setup else trade.get("signal_id")
        trade["engine"] = setup.get("engine", "unknown") if setup else "unknown"
        trade["planned_rr"] = setup.get("rr") if setup else None
        trade["resonance_score"] = setup.get("resonance_score", 0) if setup else 0
        trade["interval"] = trade.get("interval") or (
            setup.get("interval") if setup else ""
        )
    return {
        "summary": summarize(trades),
        "by_symbol": _group(trades, "symbol"),
        "by_engine": _group(trades, "engine"),
        "by_direction": _group(trades, "direction"),
        "by_exit": _group(trades, "exit_reason"),
        "trades": sorted(
            trades, key=lambda t: int(t.get("exit_time", 0)), reverse=True
        )[:200],
    }
