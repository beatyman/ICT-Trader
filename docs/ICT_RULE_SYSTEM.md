# ICT 规则体系（强可验证 · 边界分层版）

## 0. 适用范围假设

任务模板中的四个占位字段未填写，以下先按仓库现有能力固定；替换标的时只改参数，不改层级。

```text
语料：本仓库 corpus/ 下 383 篇 ICT 文章
      14 个来源：教学、复盘、行情评论、交易计划
用途：回测规则化 + 交易信号审核 + 标注抽取
市场：BTCUSDT / XAUUSD / HK / US
周期：15m 入场；1D / 4H / 1H 为 HTF 背景
数据：OHLCV；15m / 1H / 4H / 1D
      无逐笔、无订单簿、无完整新闻日历
```

数据不足时，规则必须输出 `N/A`，不得降级为“看起来像”。

---

## 1. 规则总览

| ID | 层级 | 名称 | 周期/时段 | 所需数据 | 验证方式 | 优先级 |
|---|---|---|---|---|---|---|
| L0-01 | L0 | 数据完整性 | 全部 | OHLCV、时间戳 | 单元测试 | 1 |
| L0-02 | L0 | 市场/时段范围 | 全部 | 市场日历 | 覆盖率 | 2 |
| L0-03 | L0 | 事件排除 | 全部 | 新闻日历 | 样本审计 | 3 |
| L0-04 | L0 | 流动性与点差 | 全部 | 盘口/点差 | 分布检验 | 4 |
| L0-05 | L0 | 历史长度 | 全部 | 多周期 | 门槛检查 | 5 |
| L1-01 | L1 | Swing 定义 | 15m+ | OHLC | 重放测试 | 10 |
| L1-02 | L1 | 趋势/区间状态 | 15m+ | Swing | 状态机 | 11 |
| L1-03 | L1 | BOS | 15m+ | Swing、ATR | 事件回放 | 12 |
| L1-04 | L1 | CHoCH / MSS | 15m+ | BOS、位移 | 事件回放 | 13 |
| L1-05 | L1 | 区间禁做 | 15m+ | Swing | 分组统计 | 14 |
| L2-01 | L2 | PDH/PDL | 1D | 日线 | 点位核对 | 20 |
| L2-02 | L2 | EQH/EQL | 15m+ | Swing、ATR | 容差测试 | 21 |
| L2-03 | L2 | Session range | 15m | 时段表 | 时段覆盖 | 22 |
| L2-04 | L2 | 流动性扫荡 | 15m | 池、收盘 | 事件回放 | 23 |
| L2-05 | L2 | 扫荡 vs 突破 | 15m | 收盘、连续 bar | 边界测试 | 24 |
| L2-06 | L2 | SMT | 15m+ | 第二标的 | 相关性检验 | 25 |
| L2-07 | L2 | DOL 目标 | 15m+ | 未扫池 | 目标审计 | 26 |
| L3-01 | L3 | FVG | 15m | 三根 K 线 | 结构回放 | 30 |
| L3-02 | L3 | OB | 15m | 位移、K 线 | 结构回放 | 31 |
| L3-03 | L3 | Breaker | 15m | OB、收盘 | 结构回放 | 32 |
| L3-04 | L3 | CISD | 15m | 收盘、swing | 事件回放 | 33 |
| L3-05 | L3 | 入场模型 | 15m | L2+L3 | 回测 | 34 |
| L3-06 | L3 | OTE（可选） | 15m | 区间 | 分组测试 | 35 |
| L4-01 | L4 | 止损 | 15m | 极值、ATR | 回测 | 40 |
| L4-02 | L4 | 目标 | 15m+ | DOL | 回测 | 41 |
| L4-03 | L4 | 净 RR 门槛 | 15m | 成本模型 | 回测 | 42 |
| L4-04 | L4 | 时间止损 | 15m | 时段 | 回测 | 43 |
| L4-05 | L4 | 仓位/暴露 | 组合 | 账户 | 压力测试 | 44 |
| L4-06 | L4 | 失效/隔夜 | 15m+ | HTF DOL | 分组测试 | 45 |
| L5-01 | L5 | Killzone | 15m | 时段 | 分组统计 | 50 |
| L5-02 | L5 | 新闻过滤 | 15m | 日历 | 样本审计 | 51 |
| L5-03 | L5 | HTF 一致性 | 1D/4H/1H | 多周期 | 回测 | 52 |
| L5-04 | L5 | Session sweep | 15m | Session 池 | 分组统计 | 53 |
| L5-05 | L5 | SMT 过滤 | 15m+ | 第二标的 | 相关性检验 | 54 |

同一层内，ID 小的优先；只允许一个“主规则”生成信号。

---

## 2. 分层规则明细

### L0 适用范围与排除

#### L0-01 数据完整性

```text
适用前提：CSV 已加载
排除情形：重复时间戳、OHLC 非法、单根缺失
判定条件：
  high >= max(open, close, low)
  low  <= min(open, close, high)
  volume >= 0
  连续缺失 <= 2 根；超过则 N/A
判定方法：逐行校验 + 缺口扫描
正例：15m 连续 200 根，无重复
反例：两天数据被拼接到同一时间戳
边界例：单根缺失，前后可确认，可插值但不参与信号
回测定义：坏数据区间不产生信号
验证：单元测试 + 数据质量报告
冲突：直接 veto 所有层
```

#### L0-02 市场/时段范围

```text
适用前提：标的已声明 market
排除情形：休市、半日市、非交易时段
判定条件：
  crypto：24/7
  HK：09:30–12:00、13:00–16:00 HKT
  US：09:30–16:00 ET
  无 session 数据时，session 规则输出 N/A
判定方法：交易日历 + 时区转换
正例：HK 10:15 的 15m bar
反例：HK 12:30 的 bar
边界例：开盘第一根 09:45，允许但单独统计
回测定义：非时段 bar 不生成入场
验证：覆盖率、时区单测
冲突：优先于所有信号层
```

#### L0-03 事件排除

```text
适用前提：有新闻日历
排除情形：高影响事件前后 30 分钟
判定条件：event_impact >= high 且 |t - event| <= 30min
判定方法：日历 join
正例：FOMC 前 20 分钟不开新仓
反例：低影响数据不排除
边界例：事件前已持仓，只允许减仓/移动止损
回测定义：事件窗口内不产生新入场
验证：事件样本审计
冲突：veto
```

#### L0-04 流动性与点差

```text
适用前提：有点差/盘口数据
排除情形：点差 > 2 倍过去 100 bar 中位数
判定条件：spread_bps > 2 * median_spread_bps
判定方法：滚动中位数
正例：正常时段点差 1 bp
反例：开盘瞬间 15 bps
边界例：点差 2.1 倍，标记 warning，不交易
回测定义：用保守成交价，不用中间价
验证：滑点敏感性
冲突：veto
```

#### L0-05 历史长度

```text
适用前提：要生成 HTF 背景
排除情形：15m < 300 根；1H < 60；4H < 30；1D < 20
判定条件：各周期 bar 数门槛
判定方法：加载时断言
正例：15m 1000 根，1D 200 根
反例：新上市标的只有 50 根 15m
边界例：刚好 300 根，允许但标记 low_history
回测定义：不足则不生成信号
验证：断言测试
冲突：veto
```

---

### L1 市场结构

#### L1-01 Swing 定义

```text
适用前提：15m OHLC
排除情形：未确认的 swing
判定条件：
  swing_high[i]：high[i] > high[i-k..i-1] 且 high[i] > high[i+1..i+k]
  swing_low[i]：low[i] < low[i-k..i-1] 且 low[i] < low[i+1..i+k]
  k = 3
  在 i+k 收盘后才确认，禁止重绘
判定方法：分形 + 确认延迟
正例：i=100 高点高于前后 3 根，103 确认
反例：i=100 只高于前 2 根
边界例：相等高点，按 EQH 处理，不算独立 swing
回测定义：只用确认后的 swing
验证：逐 bar 重放，确认不引用未来
冲突：L1 的基础，先于 BOS
```

#### L1-02 趋势/区间状态

```text
适用前提：至少 4 个已确认 swing
排除情形：swing 冲突
判定条件：
  上升：最近两个 swing high 抬高，且最近两个 swing low 抬高
  下降：最近两个 swing high 降低，且最近两个 swing low 降低
  否则：range
判定方法：状态机，逐 bar 更新
正例：HH + HL
反例：HH + LL
边界例：一个 HH + 一个 HL，但前低被扫，标 range
回测定义：range 状态禁止趋势入场
验证：分层统计
冲突：L1-05 可 veto
```

#### L1-03 BOS

```text
适用前提：趋势状态已知
排除情形：只有影线突破
判定条件：
  上升趋势：close > last_swing_high + 0.10 * ATR14
  下降趋势：close < last_swing_low - 0.10 * ATR14
  以收盘确认
判定方法：收盘价比较
正例：15m 收盘 1.002 × swing high
反例：上影线刺破，收盘收回
边界例：收盘刚好 +0.09 ATR，不算
回测定义：BOS 本身不入场，只更新结构
验证：事件回放
冲突：与 MSS 互斥，先判 L1-04
```

#### L1-04 CHoCH / MSS

```text
适用前提：已有趋势
排除情形：无反向 swing
判定条件：
  CHoCH：趋势中第一次反向 BOS
  MSS：CHoCH 且反向 leg 满足
       range >= 1.0 * ATR14
       body_ratio >= 0.60
       close 越过 swing 至少 0.10 ATR
判定方法：BOS 事件 + 位移质量
正例：上升趋势跌破 last swing low，实体 70%，1.4 ATR
反例：跌破后立刻收回
边界例：实体 55%，只标 CHoCH，不标 MSS
回测定义：MSS 可作为反转前提，不直接入场
验证：人工抽样 100 例，Kappa ≥ 0.8
冲突：MSS > CHoCH > BOS
```

#### L1-05 区间禁做

```text
适用前提：L1-02 = range
排除情形：区间边界扫荡 + 收回
判定条件：range_width <= 3 * ATR14 且无 BOS
判定方法：区间状态
正例：价格在 1.5 ATR 区间内来回
反例：区间被 BOS 打破
边界例：区间边缘 sweep，允许观察，不直接入场
回测定义：禁止中线入场
验证：分组统计
冲突：可被 L2 sweep 覆盖，但需 L3 确认
```

---

### L2 流动性

#### L2-01 PDH/PDL

```text
适用前提：有前一日日线
排除情形：数据缺失
判定条件：PDH = 前一日 high；PDL = 前一日 low
判定方法：日线对齐
正例：昨日高 1.1050
反例：用当日高点冒充 PDH
边界例：前一日半日市，标注 low_quality
回测定义：作为池和目标
验证：点位核对
冲突：低于 EQH/EQL
```

#### L2-02 EQH/EQL

```text
适用前提：至少 2 个 swing
排除情形：两个 swing 距离 > 容差
判定条件：
  eq_tol = max(0.0005 * price, 0.10 * ATR14)
  |high1 - high2| <= eq_tol → EQH
  |low1 - low2| <= eq_tol → EQL
判定方法：配对扫描
正例：两次高点差 0.03%，ATR 的 5%
反例：差 0.5%
边界例：差 0.11 ATR，标“近似 EQH”，降级
回测定义：作为池，不单独入场
验证：容差敏感性
冲突：与 PDH 同时存在时，取更近的池
```

#### L2-03 Session range

```text
适用前提：有 session 定义
排除情形：无 session 数据
判定条件：
  Asia：00:00–08:00 UTC（crypto）
  London：07:00–16:00 UTC（crypto）
  NY：12:00–21:00 UTC（crypto）
  HK/US 用本地交易时段
判定方法：时区映射
正例：London 高/低
反例：把隔夜高当 London 高
边界例：跨 session 的池，取重叠部分
回测定义：session high/low 作为池
验证：时段覆盖
冲突：session 池优先于普通 swing
```

#### L2-04 流动性扫荡

```text
适用前提：有明确池（PDH/PDL/EQH/EQL/session）
排除情形：只有影线但没有池
判定条件：
  bear sweep：high > pool + 0.05 * ATR，且 close < pool
  bull sweep：low < pool - 0.05 * ATR，且 close > pool
  reclaim_bars <= 3
判定方法：池 + 极值 + 收盘
正例：扫 PDH 后收回，收盘在池下
反例：收盘站上 PDH，属于突破
边界例：扫 0.04 ATR，不达标
回测定义：sweep 是入场前提，不直接入场
验证：事件回放 + 人工抽样
冲突：L2-05 优先判定
```

#### L2-05 扫荡 vs 突破

```text
适用前提：价格穿越池
排除情形：无收盘确认
判定条件：
  sweep：穿越后收回池内，且 3 根内未连续收在池外
  breakout：连续 2 根 close 在池外，且超过 0.10 ATR
判定方法：收盘序列
正例：刺破后收回 → sweep
反例：两根收盘站上 → breakout
边界例：一根收在外，一根收回 → 待定，不做
回测定义：breakout 不按 sweep 处理
验证：边界样本
冲突：breakout 否决 sweep
```

#### L2-06 SMT

```text
适用前提：有第二关联标的，相关性 >= 0.70（100 bar）
排除情形：无第二标的，或相关性不足
判定条件：
  bearish SMT：A 创 higher high，B 未创 higher high（差 >= 0.10 ATR）
  bullish SMT：A 创 lower low，B 未创 lower low
判定方法：同一时间窗 swing 对比
正例：BTC 创新高，ETH 不创新高
反例：两者同步创新高
边界例：差 0.05 ATR，标 N/A
回测定义：SMT 是过滤器，不单独入场
验证：相关性 + 人工抽样
冲突：无数据时 N/A，不视为通过
```

#### L2-07 DOL 目标

```text
适用前提：已有多空方向
排除情形：无未扫的对立池
判定条件：
  多头 DOL = 上方最近未扫 BSL/EQH/PDH
  空头 DOL = 下方最近未扫 SSL/EQL/PDL
  DOL 距离 >= 2.5R（净）
判定方法：池排序
正例：上方 1.1050 未扫
反例：所有池都已扫
边界例：DOL 只有 1.8R，不做
回测定义：target = DOL
验证：目标审计
冲突：L4-03 可 veto
```

---

### L3 入场模型

#### L3-01 FVG

```text
适用前提：15m 三根 K 线
排除情形：缺口小于阈值
判定条件：
  bull FVG：low[i] > high[i-2]
  bear FVG：high[i] < low[i-2]
  size >= 0.10 * ATR14
  CE = (top + bottom) / 2
  fresh：未被回踩
判定方法：三根结构
正例：三根上涨，第二根留下缺口
反例：只有影线重叠
边界例：size = 0.09 ATR，标“弱 FVG”，不优先
回测定义：entry = CE 或 50% 回踩
验证：结构回放
冲突：FVG 与 OB 重叠时，取更近的 POI
```

#### L3-02 OB

```text
适用前提：有位移
排除情形：普通支撑阻力
判定条件：
  位移 leg range >= 1.0 * ATR
  body_ratio >= 0.60
  OB = 位移前最后一根反向 K 线实体
  fresh：未被回踩
判定方法：位移 + 反向 K 线
正例：大阳线前最后一根阴线
反例：横盘中的随机阴线
边界例：位移 0.9 ATR，降级为弱 OB
回测定义：entry = OB 近端或 50%
验证：结构回放
冲突：Breaker 优先于 OB
```

#### L3-03 Breaker

```text
适用前提：OB 失效
排除情形：OB 仍有效
判定条件：
  OB 被收盘穿越，随后回踩 OB 另一侧
  close 越过 OB 至少 0.10 ATR
判定方法：失效 + 回踩
正例：bull OB 失守后回踩做空
反例：只是影线穿越
边界例：收盘刚好在 OB 边缘，待定
回测定义：entry = 回踩 OB 边缘
验证：事件回放
冲突：高于普通 OB
```

#### L3-04 CISD

```text
适用前提：有 swing / 结构
排除情形：无收盘确认
判定条件：
  close 越过最近 swing 至少 0.10 ATR
  body_ratio >= 0.50
  leg range >= 0.80 ATR
判定方法：收盘 + 位移
正例：收盘穿过 last swing high
反例：影线刺穿
边界例：实体 45%，不达标
回测定义：entry = 下一根开盘或回踩 CE
验证：事件回放
冲突：MSS 质量更高
```

#### L3-05 入场模型

```text
适用前提：L2 sweep + L3 CISD + POI
排除情形：缺任一
判定条件：
  1. sweep 方向与计划一致
  2. CISD 与 sweep 同向
  3. 价格回到 FVG/OB/CE
  4. HTF 不反向
判定方法：事件序列
正例：扫 SSL → 收回 → CISD → 回踩 FVG
反例：扫 SSL 后继续下跌
边界例：扫后横盘 5 根才 CISD，降级
回测定义：
  entry = 下一根开盘 / CE
  SL = sweep 极值 ± 0.10 ATR
  TP = DOL
验证：回测 + 人工抽样
冲突：L4-03 净 RR 可 veto
```

#### L3-06 OTE（可选）

```text
适用前提：有清晰 leg
排除情形：leg 不明确
判定条件：回撤 62%–79%
判定方法：斐波那契
正例：回踩 70% 后 CISD
反例：回踩 30%
边界例：79.5%，待定
回测定义：仅作为加分项
验证：分组测试
冲突：不优先于 FVG/OB
```

---

### L4 风控与出场

#### L4-01 止损

```text
判定条件：
  long：SL = min(sweep low, OB bottom) - 0.10 ATR
  short：SL = max(sweep high, OB top) + 0.10 ATR
  risk = |entry - SL|
排除：SL 距离 < 0.30 ATR（太紧）
回测定义：固定 1R 风险
验证：MAE 分布
冲突：L4-03 先算净 RR
```

#### L4-02 目标

```text
判定条件：TP = 最近未扫 DOL
分批：TP1 = 1.5R（50%），TP2 = DOL（50%）
排除：DOL 距离 < 1.5R
回测定义：以 DOL 为主目标
验证：MFE 分布
冲突：DOL 优先于固定 R
```

#### L4-03 净 RR 门槛

```text
判定条件：
  gross_rr = reward / risk
  net_rr = (reward - cost) / (risk + cost)
  cost = entry*(comm+slip) + exit*(comm+slip+tax)
  net_rr >= 2.5
排除：net_rr < 2.5
回测定义：不足则不生成订单
验证：成本敏感性
冲突：veto 入场
```

#### L4-04 时间止损

```text
判定条件：
  15m：24 根（6 小时）未到 TP1 且未触发 SL → 平仓
  或 session 结束前 1 根
排除：HTF swing 计划
回测定义：时间退出
验证：分组统计
冲突：L4-06 可覆盖
```

#### L4-05 仓位/暴露

```text
判定条件：
  单笔风险 <= 0.5% 权益
  日风险 <= 1.5%
  同方向相关品种合计 <= 1.0%
回测定义：固定风险模型
验证：压力测试
冲突：L0-04 流动性可降仓
```

#### L4-06 失效/隔夜

```text
判定条件：
  收盘跌破 OB/FVG → 失效
  反向 CISD → 失效
  隔夜仅允许 HTF DOL 未到且 risk <= 0.5%
排除：HK/US 财报/事件夜
回测定义：失效即平仓
验证：隔夜/日内分组
冲突：L0-03 事件 veto
```

---

### L5 时段与过滤器

#### L5-01 Killzone

```text
适用前提：session 数据
判定条件：
  London：02:00–05:00 ET
  NY AM：07:00–10:00 ET
  Silver Bullet：10:00–11:00 ET
  HK：09:30–12:00 / 13:00–16:00 HKT
排除：非窗口
回测定义：只在这些窗口入场
验证：时段分组
冲突：L0-02 优先
```

#### L5-02 新闻过滤

同 L0-03；L5 只做二次确认。

#### L5-03 HTF 一致性

```text
判定条件：
  1D / 4H 至少 2/3 同向
  1H 不反向
  15m 入场
排除：HTF 冲突
回测定义：只做 HTF 同向
验证：分组统计
冲突：L2 sweep 可逆势，但需降级
```

#### L5-04 Session sweep

```text
判定条件：L2-04 的 sweep 池是当前 session high/low
排除：无 session 数据 → N/A
回测定义：session sweep 加分
验证：分组统计
冲突：N/A 不通过
```

#### L5-05 SMT 过滤

```text
判定条件：L2-06 = PASS
排除：N/A 或 FAIL
回测定义：仅过滤，不生成信号
验证：相关性检验
冲突：N/A 不通过
```

---

## 3. 边界判定表

| 易混对 | 判 A 不判 B 的最小差异 | 结论 |
|---|---|---|
| FVG vs 普通缺口 | FVG 必须三根 K 线、缺口 ≥0.10 ATR、由位移产生 | 普通跳空不算 FVG |
| FVG vs imbalance | FVG 是三根特定结构；imbalance 更宽泛 | 只有三根结构才画 FVG |
| OB vs 支撑阻力 | OB 必须是位移前最后一根反向 K 线，位移 ≥1.0 ATR | 普通 S/R 不算 OB |
| Sweep vs breakout | sweep 收盘收回池内；breakout 连续 2 根收在池外 | 收盘行为决定 |
| MSS vs BOS | BOS 顺趋势；MSS 逆趋势且有位移 | 方向 + 位移 |
| CHoCH vs MSS | CHoCH 只要求反向 BOS；MSS 还要 body≥0.6、range≥1.0 ATR | 质量不同 |
| Displacement vs 大 K 线 | 位移要求 body≥0.6、range≥1.0 ATR、且越过 swing 0.10 ATR | 大 K 线不一定位移 |
| EQH vs 双顶 | EQH 是流动性池，容差 ≤ max(5bp, 0.10 ATR) | 双顶是形态，EQH 是池 |
| Session sweep vs 普通 sweep | 池必须是 session high/low | 无 session 数据则 N/A |
| SMT vs 同向 | 一个创新高/低，另一个未创新高/低 | 必须有关联标的 |

---

## 4. 验证方案

### 4.1 回测脚本要点

```text
逐 bar 重放
信号只在收盘确认
入场用下一根开盘
止损/止盈用 bar 内 high/low，先 SL 后 TP
成本：commission 5bp + slippage 2bp + sell tax
禁止未来数据
每笔记录：signal_time、entry、sl、tp、MFE、MAE、exit_reason
```

### 4.2 样本划分

```text
训练 60%
验证 20%
测试 20%
purged walk-forward，embargo >= 24 bar
测试集只跑一次
```

### 4.3 人工复核

```text
随机抽 200 个信号
2 名标注员独立判定
Cohen's Kappa >= 0.80
分歧由第三人裁决
```

### 4.4 过拟合防范

```text
一次只改一个变量
参数网格不超过 3×3
最小样本 100 笔
报告 bootstrap 置信区间
分趋势/区间/高波动/低波动
```

### 4.5 通过标准

```text
net expectancy > 0
profit factor > 1.2
max drawdown < 15%
样本 >= 100
测试集不显著劣于验证集
```

---

## 5. 待确认问题

```text
1. 语料范围是否只限 corpus/，是否要逐条引用来源？
2. 市场是否固定 BTC/XAU/HK/US，还是只做单一标的？
3. 新闻日历从哪里来？
4. 是否有逐笔/盘口数据？
5. EQH/EQL 容差用 5bp 还是 0.10 ATR？
6. Displacement 阈值 1.0 ATR 是否按品种调整？
7. Session 定义用 UTC 还是交易所本地？
8. SMT 关联标的池如何固定？
9. 成本模型是否含 funding/融资？
10. 是否允许做空/隔夜？
```

---

## 7. 代码化与 Skill 状态

规则已落为代码和 Skill，但不是“34 条全部自动判定”。当前状态如下。

### 已实现

```text
L0-01 数据完整性
L0-02 市场时段
L0-05 历史长度
L1-01 Swing
L1-02 趋势/区间
L1-03 BOS
L1-04 CHoCH/MSS
L1-05 区间状态
L2-01 PDH/PDL
L2-02 EQH/EQL
L2-04 Sweep
L2-07 DOL
L3-01 FVG
L3-02 OB
L3-04 CISD
L3-05 入场序列
L4-01 止损
L4-02 目标
L4-03 净 RR
L5-03 HTF 一致性
```

### 仍为 N/A（缺输入或未分类）

```text
L0-03 新闻日历
L0-04 点差
L2-03 Session range
L2-05 Breakout/Sweep 的完整边界分类
L2-06 SMT
L3-03 Breaker
L3-06 OTE
L4-04 时间止损
L4-05 组合仓位
L4-06 隔夜/执行
L5-01 Killzone
L5-02 新闻过滤
L5-04 Session sweep
L5-05 SMT 过滤
```

`N/A` 永远不算通过。

### 代码

```text
src/ict_trader/rules.py        规则引擎
scripts/audit_ict_rules.py     CLI
skills/ict-rule-audit/SKILL.md Skill
```

### 命令

```bash
python scripts/audit_ict_rules.py \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market us \
  --plan-json output/ict/plan.json \
  --json-out output/ict/rule_audit.json
```

输出：

```text
decision
blocking_failures
rules[] -> PASS / FAIL / N/A
```

只有 `decision=TRADE` 且没有任何 blocking fail 时，才可以继续画计划图。

---

## 8. 与仓库脚本的对应

```text
FVG / OB / sweep / CISD / key levels
  → src/ict_trader/concepts.py

setup / RR / costs
  → src/ict_trader/setup.py
  → src/ict_trader/strategy.py

chart
  → scripts/plot_ict_plotly.py

standard
  → docs/ICT_CHART_STANDARD.md
```

规则未实现的部分必须标 `待定`，不得在图表里补画。
