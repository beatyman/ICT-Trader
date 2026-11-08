"""成交量分布形状分类（D / B / P / Double Zone）—— 从 WillyAlgoTrader Volume Profile Anatomies 吸收。

在 cyq.py 的 volume_profile（成交量分布）之上，补上：
  · POC（Point of Control）：成交量峰值价
  · VAH / VAL（Value Area）：覆盖 pct 成交量（默认 70%）的高低界
  · 形状分类：
      1 D-shape      = POC 居中（均衡市，正常波动）
      2 B-shape      = POC 偏下（均衡市，强短期波动）
      3 P-shape      = POC 偏上（单边趋势动量：bull/bear run）
      4 Double Zone  = 双峰（bimodal 盘整）
"""
from __future__ import annotations

__all__ = ["poc_vah_val", "classify_profile", "KIND_NAME"]

KIND_NAME = {1: "D-shape", 2: "B-shape", 3: "P-shape", 4: "Double Zone"}


def poc_vah_val(prices: list[float], density: list[float], pct: float = 0.7):
    """返回 (POC, VAH, VAL)：成交量峰值价 + 覆盖 pct 成交量的价值区高低界。"""
    n = len(prices)
    if n == 0:
        return None, None, None
    poc = max(range(n), key=lambda i: density[i])
    total = sum(density)
    target = total * pct
    acc = density[poc]
    up = down = poc
    while acc < target and (up < n - 1 or down > 0):
        up_v = density[up + 1] if up < n - 1 else -1.0
        down_v = density[down - 1] if down > 0 else -1.0
        if up_v >= down_v:
            up += 1
            acc += up_v
        else:
            down -= 1
            acc += down_v
    return prices[poc], prices[up], prices[down]


def classify_profile(density: list[float], smooth_n: int = 2,
                     second_ratio: float = 0.60, valley_ratio: float = 0.70,
                     pos_hi: float = 0.66, pos_lo: float = 0.34):
    """分类成交量分布形状。返回 (kind, poc_idx, second_peak_idx)。

    kind: 1=D, 2=B, 3=P, 4=Double Zone。second_peak_idx 无第二峰时为 -1。
    """
    n = len(density)
    if n == 0:
        return 0, -1, -1

    # 平滑（1-2-1 加权，默认 2 遍）
    s = list(density)
    for _ in range(smooth_n):
        t = list(s)
        for i in range(n):
            a = t[max(i - 1, 0)]
            b = t[i]
            c = t[min(i + 1, n - 1)]
            s[i] = (a + 2.0 * b + c) / 4.0

    poc = max(range(n), key=lambda i: s[i])

    # 第二峰：离 POC 至少 min_sep 行的最大局部峰
    min_sep = max(3, round(n * 0.2))
    p2 = -1
    p2_v = 0.0
    for i in range(n):
        if abs(i - poc) >= min_sep:
            lv = s[max(i - 1, 0)]
            rv = s[min(i + 1, n - 1)]
            is_peak = (i == 0 or s[i] >= lv) and (i == n - 1 or s[i] >= rv)
            if is_peak and s[i] > p2_v:
                p2_v = s[i]
                p2 = i

    kind = 0
    if p2 >= 0 and p2_v >= second_ratio * s[poc]:
        a, b = min(poc, p2), max(poc, p2)
        valley = min(s[a:b + 1])
        if valley <= valley_ratio * p2_v:
            kind = 4  # Double Zone（双峰，中间有谷）

    if kind == 0:
        pos = poc / (n - 1.0) if n > 1 else 0.5
        kind = 3 if pos > pos_hi else 2 if pos < pos_lo else 1

    return kind, poc, p2
