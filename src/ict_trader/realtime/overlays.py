"""Canonical ICT FVG/IFVG/OB overlays.

The chart API and the ICT engine both call this module, so the visual zones
and `EngineSignal.evidence` can never disagree about the same K-line window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .models import dt_to_ms


def _ms(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    return int(pd.Timestamp(value).timestamp() * 1000)


def build_ict_overlays(frame: pd.DataFrame, *, max_fvg: int = 3,
                       max_ob: int = 1) -> dict[str, list[dict[str, Any]]]:
    if frame is None or len(frame) < 5:
        return {"rectangles": []}
    df = frame.copy()
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    step = 900_000
    if len(df) > 1:
        step = max(1, _ms(df["date"].iloc[-1]) - _ms(df["date"].iloc[-2]))
    last_time = _ms(df["date"].iloc[-1])
    end_time = last_time + step
    atr = 0.0
    n = min(14, len(df) - 1)
    if n > 0:
        atr = sum(float(df["high"].iloc[i] - df["low"].iloc[i])
                  for i in range(len(df) - n, len(df))) / n
    min_gap = max(atr * 0.035, abs(float(df["close"].iloc[-1])) * 0.00005)
    start = max(2, len(df) - 140)
    rects: list[dict[str, Any]] = []

    def add(kind: str, direction: str, start_ms: int, low: float, high: float,
            label: str, end_ms: int | None = None) -> None:
        if end_ms is None:
            end_ms = min(end_time, start_ms + 12 * step)
        rects.append({
            "kind": kind, "direction": direction,
            "start_time": start_ms, "end_time": end_ms,
            "low": float(low), "high": float(high), "label": label,
        })

    for i in range(start, len(df) - 1):
        a, c = df.iloc[i - 2], df.iloc[i]
        bull = float(c["low"]) - float(a["high"])
        bear = float(a["low"]) - float(c["high"])
        if bull > min_gap:
            failed = any(float(df["close"].iloc[j]) < float(a["high"])
                         for j in range(i + 1, len(df)))
            add("IFVG" if failed else "FVG", "bearish" if failed else "bullish",
                _ms(a["date"]), float(a["high"]), float(c["low"]), "IFVG" if failed else "FVG")
        elif bear > min_gap:
            failed = any(float(df["close"].iloc[j]) > float(a["low"])
                         for j in range(i + 1, len(df)))
            add("IFVG" if failed else "FVG", "bullish" if failed else "bearish",
                _ms(a["date"]), float(c["high"]), float(a["low"]), "IFVG" if failed else "FVG")

    for i in range(start + 1, len(df)):
        prev, cur = df.iloc[i - 1], df.iloc[i]
        body = abs(float(cur["close"]) - float(cur["open"]))
        if body < atr * 0.95:
            continue
        bullish = float(cur["close"]) > float(cur["open"])
        prev_bear = float(prev["close"]) < float(prev["open"])
        if bullish != prev_bear:
            continue
        add("OB", "bullish" if bullish else "bearish", _ms(prev["date"]),
            min(float(prev["open"]), float(prev["close"]), float(prev["low"])),
            max(float(prev["open"]), float(prev["close"]), float(prev["high"])), "OB")

    from .analysis.sweeps import build_sweep_events
    fvg = [r for r in rects if r["kind"] in {"FVG", "IFVG"}][-max_fvg:]
    ob = [r for r in rects if r["kind"] == "OB"][-max_ob:]
    sweeps = [e.to_public() for e in build_sweep_events(df)][-12:]
    return {"rectangles": fvg + ob, "sweeps": sweeps}


def overlays_public(overlays: dict[str, Any]) -> dict[str, Any]:
    """Convert millisecond boundaries to Lightweight Charts seconds."""
    out = []
    for r in overlays.get("rectangles", []):
        out.append({
            **r,
            "start_time": int(r["start_time"] / 1000),
            "end_time": int(r["end_time"] / 1000),
        })
    sweeps = []
    for e in overlays.get("sweeps", []):
        sweeps.append({
            **e,
            "time": int(e["time"] / 1000),
            "expires_at": int(e["expires_at"] / 1000),
        })
    return {"rectangles": out, "sweeps": sweeps, "source": "backend"}
