"""FVG lifecycle: CREATED/ACTIVE/PARTIAL/MITIGATED/INVERTED/EXPIRED."""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class FVGState:
    fvg_id: str
    direction: str
    top: float
    bottom: float
    state: str = "CREATED"
    age_bars: int = 0
    quality: float = 0.0
    reasons: tuple[str, ...] = ()


def update_fvg_states(fvgs: list[dict], frame, *, max_age: int = 500) -> list[FVGState]:
    out = []
    closes = frame["close"].astype(float).to_numpy()
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    n = len(frame)
    for i, f in enumerate(fvgs):
        top, bottom = float(f["top"]), float(f["bottom"])
        created = int(f.get("created", 0))
        age = max(0, n - 1 - created)
        state = "EXPIRED" if age > max_age else "CREATED"
        if state != "EXPIRED":
            if f["direction"] == "bullish":
                if closes[-1] < bottom:
                    state = "INVERTED"
                elif lows[-1] <= bottom:
                    state = "MITIGATED"
                elif lows[-1] <= top:
                    state = "ACTIVE" if lows[-1] > bottom else "PARTIAL"
            else:
                if closes[-1] > top:
                    state = "INVERTED"
                elif highs[-1] >= top:
                    state = "MITIGATED"
                elif highs[-1] >= bottom:
                    state = "ACTIVE" if highs[-1] < top else "PARTIAL"
        out.append(FVGState(f"FVG-{i}", f["direction"], top, bottom, state, age))
    return out


def fvg_quality(state: FVGState, *, displacement_atr: float = 0.0,
                htf_overlap: bool = False, correct_pd: bool = False) -> FVGState:
    q, reasons = 0.0, []
    if displacement_atr >= 0.5:
        q += 0.30; reasons.append("displacement")
    if state.state in ("CREATED", "ACTIVE"):
        q += 0.25; reasons.append("unmitigated")
    elif state.state == "PARTIAL":
        q += 0.10; reasons.append("partial")
    if htf_overlap:
        q += 0.20; reasons.append("htf_overlap")
    if correct_pd:
        q += 0.15; reasons.append("correct_pd")
    if state.age_bars <= 20:
        q += 0.10; reasons.append("fresh")
    return replace(state, quality=min(q, 1.0), reasons=tuple(reasons))
