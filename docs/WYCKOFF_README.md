# 威科夫方法策略原型 — README

## 文件结构

```text
src/ict_trader/wyckoff.py           检测器（WyckoffState / detect_wyckoff / WyckoffSignal）
src/ict_trader/wyckoff_strategy.py  可回测策略（风险/仓位/TP1/TP2/保本 + 绩效）
scripts/wyckoff_detect.py           检测 CLI（输出 JSON）
scripts/wyckoff_strategy.pine       Pine Script v5（图表检测 + 信号 + 警报）
tests/test_wyckoff.py               检测器验收测试（4 条）
docs/WYCKOFF_KNOWLEDGE_BASE.md      知识库（结构化整理）
```

## 信号定义（量化代理）

```text
SC/BC  高潮：trendLen 内新低/新高 + 振幅 ≥ climRng×ATR + 量 ≥ climVol×20均量
AR     自动反弹/回落：反向 pivot 距高潮 ≥ arMin×ATR
ST     次级测试：回到高潮边，距离 ≤ stTol×区间
Spring/UTAD  假突破：穿高潮边又收回（sprBars 内）
SOS/SOW  突破：收盘越过突破边（Creek/ICE）
LPS/LPSY  回测：突破后 pivot 回到区间中部上方不破
```

## 参数与默认值

| 参数 | 默认 | 说明 |
|---|---|---|
| trend_len | 50 | 高潮 lookback |
| climax_range_atr | 1.2 | 高潮振幅阈值 |
| climax_vol_mult | 1.5 | 高潮量阈值 |
| pivot_len | 3 | 摆动强度 |
| ar_min_atr | 2.0 | 最小 AR |
| st_tol | 0.35 | ST 区域 |
| spring_max_bars | 3 | 震仓收回时限 |
| fail_depth | 0.30 | 失效深度 |
| risk_pct | 0.01 | 单笔风险 |
| stop_mode | scenario | trigger/scenario/structural |
| entry_mode | stop_order | stop_order/close |
| partial_at_tp1 | 0.5 | TP1 平仓比例 |
| breakeven_at | 0.5 | 保本触发点 |

## 风控

```text
止损三档（原文）：trigger（意图 K 线反向极值）/ scenario（Spring 低点·UTAD 高点）/ structural（结构另一侧）
仓位：(资金 × risk%) ÷ (止损距离 × 点值)
止盈：TP1 = 突破边 + 1×区间（平 partial），TP2 = +2×区间（平剩余）
保本：达到投影 breakeven_at 后止损移到入场价
```

## 已知局限与主观判断点

1. **成交量**：crypto 是 tick volume（非真实成交量），高潮/吸收判读会失真（原文局限 #1）。
2. **事件是假设**：SC vs PS 实时难分，AR/ST 确认前「高潮」只是「潜在」（原文事件段）。
3. **量能递减/宽幅/急停** 是相对描述，本实现用 ×ATR/×均量阈值代理。
4. **背景过滤**：detect_wyckoff 的 direction 内建「最近区间类型 + 价格位置」，但区间识别本身有误判率。
5. **15m 短周期**：Wyckoff 更适合日线/H4；短周期噪声大，原型结果仅作对照。
6. **Pine v5** 未在本环境运行（无 Pine runtime），逻辑与 Python 一致但未实测。

## 用法

```bash
# 检测
python scripts/wyckoff_detect.py --csv data/btc_15m.csv --json-out out/wyckoff.json

# 回测（Python）
python -c "import pandas as pd; from ict_trader.wyckoff_strategy import *; \
df=pd.read_csv('data/btc_15m.csv', parse_dates=['date']); \
t,e,m=run_backtest(df, WyckoffStrategyConfig()); print(m)"
```

## 验收清单

见文档末「验收清单」或 `tests/test_wyckoff.py`（4 条）：
1. 结构输出对齐 + 字段有效 ✅
2. 无未来函数（追加数据不改变历史阶段）✅
3. 吸筹 direction=1 ✅
4. 事件时间有序 ✅
