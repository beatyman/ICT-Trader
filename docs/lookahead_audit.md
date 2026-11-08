# 前视偏差审计（lookahead bias audit）

目标：核查 structured_plan.py 的 `build_input_from_data` 里，哪些特征计算
可能用了「信号时点之后」的数据（前视），以及数据层对齐是否正确。

## 结论：未发现前视偏差，但有两处「对齐/时效」需注意

### 1. PO3 当日窗口 —— 无前视，有「薄数据」风险

```python
win = df_15m[df_15m["date"] >= str(df_15m["date"].iloc[-1])[:10]]
po3, _ = detect_po3(win, accumulate_bars=20)
```

- **无前视**：`detect_po3` 逐 bar 顺序处理，PO3 状态只依赖「当前 bar 及之前」的
  已收盘 K 线（数据源已丢弃未收盘 bar）。
- **薄数据风险**：只取「当日」窗口。若当日刚开盘、bar 数 < accumulate_bars(20)，
  PO3 状态不可靠（返回 INVALID 或早期阶段）。这是「数据不足」不是「前视」。
- **建议**：当 `len(win) < accumulate_bars` 时显式把 po3_phase 置 null 并在
  data_gaps 记一条，避免用「半成品日」的 PO3 状态做决策。

### 2. 缠论 1d / Ichimoku（日线）—— 无前视，有一日滞后

```python
analyze_chanlun(df_daily, symbol, "1d")   # df_daily 末行 = 最近「已完成」日线
```

- **无前视**：日线数据末行是最近一个「已收盘」交易日（Futu 拉取时丢弃未收盘日线）。
  缠论 1d 趋势只基于已完成日线，不含当日未收盘的日内数据。
- **一日滞后（正确行为）**：ETH 实测日线末行 = 11-07，而 15m 末行 = 11-08 20:00。
  这是「日线滞后于 15m 一天」，方向正确（11-08 的日线还没收盘，不该用）。
  不是前视，反而是「保守滞后」。
- **注意**：15m 用 11-08 收盘价，但缠论 1d 用 11-07 日线 → HTF 方向可能滞后
  最新一根日内波动。这是设计使然（HTF 用已完成周期），无需修。

### 3. 数据层对齐（多周期）

- 15m/1h/4h/1d 各自独立拉取，`build_input_from_data` 内部 `sort_values("date")`
  + `reset_index`，无跨周期错位。
- **4h 是 1h 重采样**（`resample('4h').agg(...)`），注意 HK 4h 桶边界与交易所
  会话边界可能不一致（如 4h 桶从 08:00 而非 09:30 开始），但不影响「已收盘」判定。

## 审计方法（可复现）

```bash
# 1) 确认日线末行早于 15m 末行（无前视的充要条件之一）
python - <<'PY'
import pandas as pd
m15 = pd.read_csv("ethusdt_15m_full.csv", parse_dates=["date"])
d   = pd.read_csv("ethusdt_1d_full.csv", parse_dates=["date"])
print("15m last:", m15["date"].max(), " daily last:", d["date"].max())
# 应满足：daily.last < 15m.last（日线尚未包含当日未收盘）
PY
```

## 待办（不阻塞，单独一轮）

- `build_input_from_data` 仍是 ~500 行单体函数（数据对齐 + 特征 + 信号合成三层
  耦合）。建议拆分：
  - `_prepare_data(...)` —— 数据对齐（sort/dedupe/resample）
  - `_build_ict_features(...)` / `_build_wyckoff(...)` / ... —— 特征纯函数（无副作用、可单测）
  - `build_input_from_data(...)` —— 编排（只做接线）
  这是一次「纯机械重构」，行为不变、只搬代码；风险低但量大，应单独一轮做 +
  用现有 223 测试做回归护栏。
