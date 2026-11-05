"""模块 4：融合层（ICT 主导，辅助只做否决与缩放）。

规则：
  1. ICT 无信号 → 不交易。
  2. ICT 有信号，但 Wyckoff / Ichimoku 任一方 veto → 不交易（硬性）。
  3. ICT 有信号，辅助均 allow：
     两者方向一致 → size=1.0；一方 neutral 一方一致 → 0.7；两者 neutral → 0.5。
  4. size_factor = min(辅助 size) × (ICT 置信度 / 100)。
  5. 冲突：ICT 多但 Wyckoff 派发 / Ichimoku 云下 → veto。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .main_strategy import ICTSignal
from .background import WyckoffBackground, IchimokuBackground


@dataclass
class FusionOutput:
    direction: str = "neutral"
    entry: float = 0.0
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    invalidation: float = 0.0
    size_factor: float = 0.0
    confidence: float = 0.0
    action: str = "wait"              # enter / wait / avoid
    veto_reason: str = ""
    reasons: list = field(default_factory=list)


def fuse(ict: ICTSignal, wy: WyckoffBackground, ichi: IchimokuBackground) -> FusionOutput:
    out = FusionOutput()
    # 1. ICT 无信号
    if ict.direction == "neutral":
        out.action = "wait"
        out.reasons.append("ICT no signal")
        return out

    # 2. 辅助 veto（硬性）
    if ict.direction == "long":
        if not wy.allow_long:
            out.action = "avoid"
            out.veto_reason = "Wyckoff veto (no long allowed)"
            out.reasons.append(out.veto_reason)
            return out
        if not ichi.allow_long:
            out.action = "avoid"
            out.veto_reason = "Ichimoku veto (no long allowed)"
            out.reasons.append(out.veto_reason)
            return out
    else:  # short
        if not wy.allow_short:
            out.action = "avoid"
            out.veto_reason = "Wyckoff veto (no short allowed)"
            out.reasons.append(out.veto_reason)
            return out
        if not ichi.allow_short:
            out.action = "avoid"
            out.veto_reason = "Ichimoku veto (no short allowed)"
            out.reasons.append(out.veto_reason)
            return out

    # 3. 均 allow → 缩放
    size = min(wy.size_factor, ichi.size_factor)
    wy_align = (ict.direction == "long" and wy.bias == "long") or \
               (ict.direction == "short" and wy.bias == "short")
    ichi_align = (ict.direction == "long" and ichi.cloud_state == "above") or \
                 (ict.direction == "short" and ichi.cloud_state == "below")
    if wy_align and ichi_align:
        scale = 1.0
        out.confidence = min(100, ict.confidence + 15)
    elif wy_align or ichi_align:
        scale = 0.7
        out.confidence = min(100, ict.confidence + 5)
    else:
        scale = 0.5
        out.confidence = ict.confidence
    # 4. size_factor = min(辅助 size) × scale × ICT 置信度系数
    out.size_factor = round(size * scale * (ict.confidence / 100.0), 3)

    out.direction = ict.direction
    out.entry = ict.entry
    out.stop = ict.stop
    out.tp1 = ict.tp1
    out.tp2 = ict.tp2
    out.invalidation = ict.invalidation
    out.action = "enter" if out.size_factor > 0 else "wait"
    out.reasons.append(f"size={out.size_factor:.2f}, scale={scale}")
    return out
