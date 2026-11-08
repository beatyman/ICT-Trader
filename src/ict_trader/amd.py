"""AMD Po3 目标 + 实时优势统计 —— 从 WillyAlgoTrader AMD Po3 v2.4.0 吸收。

补上 po3.py 状态机缺的两个量化维度：
  · po3_fib_target：派发目标 = 操纵腿的斐波那契外延（默认 1.5）
    （网格：0 = 操纵极值，1 = 对侧吸筹边界；目标 = fib 外延水平）
  · AmdEdgeStats：AMD 周期走前（walk-forward）优势统计
    （目标命中率 / 平均 R / form 序列，诚实统计 edge）
"""
from __future__ import annotations

import math

__all__ = ["po3_fib_target", "AmdEdgeStats"]


def po3_fib_target(range_high: float, range_low: float, manipulation_extreme: float,
                   sweep_direction: str, fib: float = 1.5) -> tuple[float, str, float]:
    """返回 (target, direction, fib_leg)。

    sweep_direction = "bull"（sell-side 扫单，操纵跌破下沿后反转向上 → long）
                      或 "bear"（buy-side 扫单，操纵涨破上沿后反转向下 → short）。
    fib_leg = 操纵腿长度（操纵极值 到 对侧吸筹边界）。
    target  = 对侧边界 + (fib - 1) × fib_leg（默认 1.5 外延）。
    """
    if sweep_direction == "bull":
        fib_leg = range_high - manipulation_extreme
        target = range_high + (fib - 1.0) * fib_leg
        return target, "long", fib_leg
    else:
        fib_leg = manipulation_extreme - range_low
        target = range_low - (fib - 1.0) * fib_leg
        return target, "short", fib_leg


class AmdEdgeStats:
    """AMD 周期走前统计：target / stop / timeout 计数 + 目标命中率 + 平均 R + form。"""

    def __init__(self, min_sample: int = 10, form_len: int = 10):
        self.min_sample = min_sample
        self.form_len = form_len
        self.target = 0
        self.stop = 0
        self.timeout = 0
        self.sum_r = 0.0
        self._form: list[str] = []

    def record(self, outcome: str, r: float) -> None:
        """outcome ∈ {"target", "stop", "timeout"}；r = 该周期的 R。"""
        if outcome == "target":
            self.target += 1
            self._form.append("▰")
        elif outcome == "stop":
            self.stop += 1
            self._form.append("▱")
        else:
            self.timeout += 1
            self._form.append("◌")
        self.sum_r += r
        if len(self._form) > self.form_len:
            self._form = self._form[-self.form_len:]

    @property
    def total(self) -> int:
        return self.target + self.stop + self.timeout

    @property
    def target_rate(self) -> float | None:
        return self.target / self.total if self.total else None

    @property
    def avg_r(self) -> float | None:
        return self.sum_r / self.total if self.total else None

    @property
    def ready(self) -> bool:
        return self.total >= self.min_sample

    @property
    def form(self) -> str:
        return "".join(self._form)

    def as_dict(self) -> dict:
        return {
            "cycles": self.total,
            "target": self.target,
            "stop": self.stop,
            "timeout": self.timeout,
            "target_rate": None if self.target_rate is None else round(self.target_rate, 4),
            "avg_r": None if self.avg_r is None else round(self.avg_r, 3),
            "form": self.form,
            "ready": self.ready,
        }
