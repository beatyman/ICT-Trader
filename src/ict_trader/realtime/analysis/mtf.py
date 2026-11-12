"""Multi-timeframe scoring over a FeatureSnapshot. No execution here."""
from __future__ import annotations

from dataclasses import dataclass

from ...context import (DEFAULT_NEUTRAL_HANDLING, neutral_blocks,
                        neutral_weight, validate_neutral_handling)
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


def score_timeframe(snap: FeatureSnapshot, tf: str, direction: str, *,
                    neutral_handling: str = DEFAULT_NEUTRAL_HANDLING
                    ) -> TimeframeScore:
    view = snap.views.get(tf)
    if view is None:
        return TimeframeScore(tf, "neutral", "", "missing", 0.0, ("missing_view",))
    want = _want(direction)
    reasons = []
    if view.bias == "neutral":
        # BI-06/BI-07: the declared handling decides the weight of a neutral
        # read, so it is neither an implicit 0.5 pass nor an implicit veto.
        bias_score = neutral_weight(neutral_handling)
        reasons.append(f"neutral_{validate_neutral_handling(neutral_handling)}")
    elif view.bias == want:
        bias_score = 1.0
        reasons.append("bias_aligned")
    else:
        bias_score = 0.0
        reasons.append("bias_conflict")
    structure_score = 1.0 if view.structure_event in ("MSS", "BOS") else 0.0
    reasons.append(f"structure_{view.structure_event or 'none'}")
    # PD side is optional until a dealing range is attached; neutral is not a pass.
    pd_score = 1.0 if view.premium_discount in ("discount", "premium", "equilibrium") else 0.0
    score = 0.55 * bias_score + 0.30 * structure_score + 0.15 * pd_score
    alignment = "aligned" if view.bias == want else "conflict" if view.bias != "neutral" else "neutral"
    return TimeframeScore(tf, view.bias, view.structure_event, alignment, score, tuple(reasons))


def aggregate_mtf(snap: FeatureSnapshot, direction: str, *,
                  neutral_handling: str = DEFAULT_NEUTRAL_HANDLING
                  ) -> tuple[float, str, dict[str, TimeframeScore]]:
    handling = validate_neutral_handling(neutral_handling)
    want = _want(direction)
    scores = {tf: score_timeframe(snap, tf, direction, neutral_handling=handling)
              for tf in ("1D", "4H", "1H", "15m")}
    for tf, code in (("1D", "htf_1d_conflict"), ("4H", "htf_4h_conflict")):
        bias = scores[tf].bias
        if bias == "neutral":
            if neutral_blocks(handling):
                return 0.0, f"neutral_{tf.lower()}_no_trade", scores
        elif bias != want:
            return 0.0, code, scores
    context = 0.5 * scores["1D"].score + 0.5 * scores["4H"].score
    execution = 0.5 * scores["1H"].score + 0.5 * scores["15m"].score
    return 0.6 * context + 0.4 * execution, "ok", scores
