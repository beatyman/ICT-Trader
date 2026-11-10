"""Build rule candidates from a closed-bar frame and canonical analysis result."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .feature_snapshot import build_feature_snapshot
from .sweeps import build_sweep_events
from ...setup_evaluator import Candidate, evaluate
from ...setup_rules import RULES


def build_candidates(frame, result, *, symbol: str, now: datetime | None = None,
                     atr: float | None = None) -> list[Candidate]:
    now = now or pd.to_datetime(frame["date"].iloc[-1], utc=True).to_pydatetime()
    snap = build_feature_snapshot(symbol, frame, result, as_of=now)
    sweeps = build_sweep_events(frame)
    price = float(frame["close"].iloc[-1])
    atr = atr or float((frame["high"].astype(float) - frame["low"].astype(float)).tail(14).mean())
    levels = sorted([float(k.price) for k in getattr(result, "key_levels", [])])
    out: list[Candidate] = []
    for direction in ("long", "short"):
        s = None
        want = "SSL" if direction == "long" else "BSL"
        cand = [x for x in sweeps if x.sweep_type == want and x.time <= int(now.timestamp() * 1000)]
        if cand:
            x = cand[-1]
            s = {"level": float(x.level), "time": int(x.time)}
        targets = ([p for p in levels if p > price][:2] if direction == "long"
                   else [p for p in reversed(levels) if p < price][:2])
        ev = result.events[-1] if getattr(result, "events", None) else None
        structure = ({"event": ev.kind, "price": float(ev.price),
                      "direction": "bullish" if ev.direction == "bull" else "bearish"}
                     if ev else None)
        features = {
            "direction": direction, "price": price,
            "htf_bias": {tf: {"bias": v.bias} for tf, v in snap.views.items()},
            "sweep": s, "structure": structure,
            "fvg": list(snap.fvg), "ob": list(snap.ob), "breaker": list(snap.breaker),
            "targets": targets,
        }
        for rule in RULES:
            c = evaluate(rule, features, now=now, min_rr=2.0, atr=atr)
            if c is not None and not c.blockers:
                out.append(c)
    return out
