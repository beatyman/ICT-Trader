"""Execution journal helpers.

The backtest always fills at the planned price plus modelled slippage, so a
backtest journal cannot measure manual rule breaks.  It can, however, keep the
planned-vs-actual fields and a stable schema so a live log uses the same shape.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from .strategy import Trade

COLUMNS = [
    "symbol", "direction", "signal_time", "entry_time", "exit_time",
    "planned_entry", "actual_entry", "entry_deviation_r",
    "planned_stop", "actual_stop", "stop_deviation_r",
    "planned_target", "actual_target",
    "r_multiple", "reason", "grade", "quality", "sample_class", "rule_version",
    "origin", "pda_source", "htf_fvg_tf",
]


def journal_frame(trades: Iterable[Trade], sample_class: str = "observation") -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({
            "symbol": t.symbol,
            "direction": t.direction,
            "signal_time": t.signal_time,
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "planned_entry": t.planned_entry if t.planned_entry is not None else t.entry,
            "actual_entry": t.entry,
            "entry_deviation_r": t.entry_deviation_r,
            "planned_stop": t.planned_sl if t.planned_sl is not None else t.sl,
            "actual_stop": t.sl,
            "stop_deviation_r": t.stop_deviation_r,
            "planned_target": t.planned_tp if t.planned_tp is not None else t.tp,
            "actual_target": t.tp,
            "r_multiple": t.r_multiple,
            "reason": t.reason,
            "grade": t.grade,
            "quality": t.quality,
            "sample_class": sample_class,
            "rule_version": t.rule_version,
            "origin": t.origin,
            "pda_source": t.pda_source,
            "htf_fvg_tf": t.htf_fvg_tf,
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def process_summary(frame: pd.DataFrame) -> dict:
    """Count price deviations; P&L is deliberately not called process quality."""
    if frame.empty:
        return {"trades": 0, "fill_deviation": 0, "stop_deviation": 0, "process_errors": 0}
    fill_bad = (frame["entry_deviation_r"].abs() > 0.25).sum()
    stop_bad = (frame["stop_deviation_r"].abs() > 1e-9).sum()
    return {
        "trades": int(len(frame)),
        "fill_deviation": int(fill_bad),
        "stop_deviation": int(stop_bad),
        "process_errors": int(fill_bad + stop_bad),
    }
