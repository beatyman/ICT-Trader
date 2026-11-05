"""一目均衡表验收测试。

逐条覆盖验收标准：
1. 前 10 根 Tenkan/Kijun/SenkouA/SenkouB 与手工计算一致
2. 无未来函数（追加数据不改变历史值）
3. Chikou 不作为输出
4. 云判断使用 [kijun] 而非 [2*kijun]
5. 偏移模式 25/26 可切换
6. 云内/薄云过滤可开关；信号分级映射仓位
7. 回测可复现
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ichimoku.indicators import ichimoku, midpoint, span_offset  # noqa: E402
from ichimoku.signals import signals, GRADE_COEF  # noqa: E402
from ichimoku.backtest import StrategyConfig, run_backtest  # noqa: E402


def make_df(n=160, seed=7):
    rng = np.random.RandomState(seed)
    close = 100.0 + np.cumsum(rng.randn(n) * 1.0)
    high = close + np.abs(rng.randn(n) * 0.5)
    low = close - np.abs(rng.randn(n) * 0.5)
    open_ = close - rng.randn(n) * 0.3
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    df = pd.DataFrame({"date": idx, "open": open_, "high": high,
                       "low": low, "close": close, "volume": np.abs(rng.randn(n)) * 1000})
    df.index = idx
    return df


def manual_midpoint(high, low, period, i):
    return (max(high[i - period + 1: i + 1]) + min(low[i - period + 1: i + 1])) / 2.0


def test_1_first_10_values_match_manual():
    """验收 1：前 10 根有效值与手工计算一致（允许末位误差）。"""
    df = make_df()
    ind = ichimoku(df, 9, 26, 52, "PLATFORM")
    h = df["high"].to_list()
    l = df["low"].to_list()
    # lookback = 51，前 10 根有效值在 index 51..60
    for i in range(51, 61):
        t = manual_midpoint(h, l, 9, i)
        k = manual_midpoint(h, l, 26, i)
        sb = manual_midpoint(h, l, 52, i)
        sa = (t + k) / 2.0
        assert ind["tenkan"].iloc[i] == pytest.approx(t, rel=1e-9), f"tenkan@{i}"
        assert ind["kijun"].iloc[i] == pytest.approx(k, rel=1e-9), f"kijun@{i}"
        assert ind["senkou_b"].iloc[i] == pytest.approx(sb, rel=1e-9), f"senkou_b@{i}"
        assert ind["senkou_a"].iloc[i] == pytest.approx(sa, rel=1e-9), f"senkou_a@{i}"


def test_2_no_lookahead():
    """验收 2：追加数据不改变历史指标值。"""
    df = make_df(160)
    a = ichimoku(df.iloc[:100], 9, 26, 52, "PLATFORM")
    b = ichimoku(df, 9, 26, 52, "PLATFORM")
    for col in ["tenkan", "kijun", "senkou_a", "senkou_b"]:
        pd.testing.assert_series_equal(a[col], b[col].iloc[:100], check_names=False)


def test_3_chikou_not_output():
    """验收 3：Chikou 不作为输出线。"""
    df = make_df()
    ind = ichimoku(df)
    assert "chikou" not in ind.columns
    assert "chikou" not in ind.attrs


def test_4_cloud_uses_kijun_not_2x_kijun():
    """验收 4：leading_a[t] == senkou_a[t - kijun]，而非 t - 2*kijun。"""
    df = make_df()
    ind = ichimoku(df, 9, 26, 52, "PLATFORM")
    offset = ind.attrs["offset"]  # 26
    t = 80
    # [kijun] 偏移正确
    assert ind["leading_a"].iloc[t] == pytest.approx(ind["senkou_a"].iloc[t - 26], rel=1e-9)
    # 明确不是 2*kijun 偏移
    wrong = ind["senkou_a"].iloc[t - 52]
    if not np.isnan(wrong):
        assert ind["leading_a"].iloc[t] != pytest.approx(wrong, rel=1e-9) or \
            abs(ind["senkou_a"].iloc[t - 26] - wrong) < 1e-12


def test_5_offset_mode_switch():
    """验收 5：偏移模式 PLATFORM(26) / HOSODA(25) 可切换。"""
    df = make_df()
    p = ichimoku(df, 9, 26, 52, "PLATFORM")
    h = ichimoku(df, 9, 26, 52, "HOSODA")
    assert span_offset(26, "PLATFORM") == 26
    assert span_offset(26, "HOSODA") == 25
    assert p.attrs["offset"] == 26
    assert h.attrs["offset"] == 25
    # 同一根 bar 的显示云值来自不同历史位置
    t = 80
    assert p["leading_a"].iloc[t] == pytest.approx(p["senkou_a"].iloc[t - 26], rel=1e-9)
    assert h["leading_a"].iloc[t] == pytest.approx(h["senkou_a"].iloc[t - 25], rel=1e-9)


def test_6_grading_and_filters():
    """验收 6：云内/薄云过滤可开关；信号分级映射仓位系数。"""
    df = make_df(300)
    ind = ichimoku(df)
    sig = signals(df, ind, 26, weak_coef=0.25)
    assert set(GRADE_COEF) == {"strong", "medium", "weak"}
    assert GRADE_COEF["strong"] == 1.0
    assert GRADE_COEF["medium"] == 0.5
    # 分级与系数一致性
    strong_mask = sig["grade"] == "strong"
    assert (sig.loc[strong_mask, "coef"] == 1.0).all()
    medium_mask = sig["grade"] == "medium"
    assert (sig.loc[medium_mask, "coef"] == 0.5).all()
    weak_mask = sig["grade"] == "weak"
    assert (sig.loc[weak_mask, "coef"] == 0.25).all()  # weak_coef 配置生效
    # 云区状态互斥
    assert not (sig["above_cloud"] & sig["below_cloud"]).any()
    assert not (sig["in_cloud"] & (sig["above_cloud"] | sig["below_cloud"])).any()
    # 过滤开关
    cfg_no_filter = StrategyConfig(allow_in_cloud=True, min_cloud_atr=0.0)
    cfg_filter = StrategyConfig(allow_in_cloud=False, min_cloud_atr=0.5)
    tr1, _, _ = run_backtest(df, cfg_no_filter)
    tr2, _, _ = run_backtest(df, cfg_filter)
    # 默认过滤（禁止云内 + 薄云）应产生不超过宽松配置的交易数
    assert len(tr2) <= len(tr1)


def test_7_backtest_reproducible():
    """验收 7：回测结果可复现（相同配置 → 相同交易）。"""
    df = make_df(400)
    cfg = StrategyConfig(risk_pct=0.01)
    tr1, eq1, m1 = run_backtest(df, cfg)
    tr2, eq2, m2 = run_backtest(df, cfg)
    assert m1 == m2
    assert [t.entry for t in tr1] == [t.entry for t in tr2]
    assert [t.exit for t in tr1] == [t.exit for t in tr2]
    assert len(tr1) == m1["trades"]
