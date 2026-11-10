"""Multi-timeframe scoring over a FeatureSnapshot. No execution here."""
from __future__ import annotations

from dataclasses import dataclass

from .feature_snapshot import FeatureSnapshot


@dataclass(frozen=True, slots=True)
class TimeframeScore:
    timeframe: str
    bias: str
    structure_event: str
    alignment: str
    score: float
    reasons: tuple[str, ...]


def _want(direction: str) -> str:
    return "bullish" if direction == "long" else "bearish"


def score_timeframe(snap: FeatureSnapshot, tf: str, direction: str) -> TimeframeScore:
    view = snap.views.get(tf)
    if view is None:
        return TimeframeScore(tf, "neutral", "", "missing", 0.0, ("missing_view",))
    want = _want(direction)
    reasons = []
    bias_score = 0.5 if view.bias == "neutral" else 1.0 if view.bias == want else 0.0
    reasons.append("neutral_bias" if view.bias == "neutral" else
                   "bias_aligned" if view.bias == want else "bias_conflict")
    structure_score = 1.0 if view.structure_event in ("MSS", "BOS") else 0.0
    reasons.append(f"structure_{view.structure_event or 'none'}")
    # PD side is optional until a dealing range is attached; neutral is not a pass.
    pd_score = 1.0 if view.premium_discount in ("discount", "premium", "equilibrium") else 0.0
    score = 0.55 * bias_score + 0.30 * structure_score + 0.15 * pd_score
    alignment = "aligned" if view.bias == want else "conflict" if view.bias != "neutral" else "neutral"
    return TimeframeScore(tf, view.bias, view.structure_event, alignment, score, tuple(reasons))


def aggregate_mtf(snap: FeatureSnapshot, direction: str) -> tuple[float, str, dict[str, TimeframeScore]]:
    want = _want(direction)
    scores = {tf: score_timeframe(snap, tf, direction) for tf in ("1D", "4H", "1H", "15m")}
    if scores["1D"].bias not in ("neutral", want):
        return 0.0, "htf_1d_conflict", scores
    if scores["4H"].bias not in ("neutral", want):
        return 0.0, "htf_4h_conflict", scores
    context = 0.5 * scores["1D"].score + 0.5 * scores["4H"].score
    execution = 0.5 * scores["1H"].score + 0.5 * scores["15m"].score
    return 0.6 * context + 0.4 * execution, "ok", scores
