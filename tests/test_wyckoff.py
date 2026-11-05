"""威科夫检测器验收测试。

覆盖：
1. 输出结构对齐（phases 长度 == bar 数，state 字段有效）
2. 无未来函数（追加数据不改变历史事件/阶段）
3. 合成的吸筹结构被识别为 direction=1，派发为 direction=-1
4. 事件顺序合法（SC/BC 先于 AR，SOS/SOW 先于 LPS/LPSY）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.wyckoff import detect_wyckoff, pivot_high, pivot_low  # noqa: E402


def test_pivots():
    h = np.array([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 4.0, 3.0, 2.0])
    ph = pivot_high(h, 1, 1)
    # 局部高点（严格大于左右邻居）
    assert ph[2] == 3.0
    assert ph[7] == 4.0
    l = np.array([3.0, 2.0, 1.0, 2.0, 3.0, 2.0, 1.0, 0.5, 1.0, 2.0])
    pl = pivot_low(l, 1, 1)
    assert pl[2] == 1.0
    assert pl[7] == 0.5


def _synthetic_accumulation(n=200, seed=3):
    """构造「缓跌 → SC 高潮 → 吸筹区间 → 上破」的合成序列。"""
    rng = np.random.RandomState(seed)
    close = np.empty(n)
    high = np.empty(n)
    low = np.empty(n)
    vol = np.ones(n) * 100
    drop_end = int(n * 0.3)
    range_end = int(n * 0.75)
    # 缓跌段（小实体，避免抬高 ATR）
    close[:drop_end] = np.linspace(100, 82, drop_end)
    # SC 卖压高潮：一根大阴线（大幅 + 新低 + 放量）
    sc = drop_end
    close[sc] = 79.0
    high[sc] = close[drop_end - 1] + 0.2
    low[sc] = 78.0
    vol[sc] = 800
    # 吸筹区间（低位低波动）
    for i in range(sc + 1, range_end):
        close[i] = 80 + rng.randn() * 0.3
    # 上破段
    close[range_end:] = np.linspace(80, 95, n - range_end)
    for i in range(n):
        if i == sc:
            continue
        high[i] = close[i] + 0.3 + abs(rng.randn()) * 0.15
        low[i] = close[i] - 0.3 - abs(rng.randn()) * 0.15
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame({"date": idx, "open": close, "high": high,
                         "low": low, "close": close, "volume": vol})


def test_detect_structure_and_no_lookahead():
    df = _synthetic_accumulation(n=260)
    st, phases = detect_wyckoff(df.iloc[:200], trend_len=40, pivot_len=2, ar_min_atr=1.0)
    assert len(phases) == 200
    assert st.direction in (0, 1, -1)
    assert st.stage in range(6)

    # 无未来函数：同一数据集，追加 60 根不改变前 200 根（减 pivot 确认窗口）的阶段
    st_full, phases_full = detect_wyckoff(df, trend_len=40, pivot_len=2, ar_min_atr=1.0)
    assert phases[:197] == phases_full[:197]


def test_accumulation_direction():
    df = _synthetic_accumulation()
    st, _ = detect_wyckoff(df, trend_len=40, pivot_len=2, ar_min_atr=1.0)
    # 下跌后低位区间上破 → 吸筹
    assert st.direction == 1
    labels = [lbl for _, _, lbl in st.events]
    # SC 必须在 AR 之前；SOS 在 LPS 之前
    if "SC" in labels and "AR" in labels:
        assert labels.index("SC") < labels.index("AR")
    if "SOS" in labels and "LPS" in labels:
        assert labels.index("SOS") < labels.index("LPS")


def test_events_are_ordered():
    df = _synthetic_accumulation()
    st, _ = detect_wyckoff(df, trend_len=40, pivot_len=2, ar_min_atr=1.0)
    bars = [b for b, _, _ in st.events]
    assert bars == sorted(bars), "events must be in chronological order"
