# ICT 规则引擎缺口实现规格

Last updated: 2026-10-30

## 0. 文档定位

本文只处理现状文档中“未实现 / 部分实现 / 映射不严谨 / 缺数据”的部分。

```text
规则ID：沿用 GS-xx / L层-序号
优先级：P0 / P1 / P2
所有阈值：默认值 + 可配置
验收用例：可构造、可复现
不确定处：标“需确认”
```

现状基线：

```text
src/ict_trader/rules.py        audit + good_setup
scripts/audit_ict_rules.py     CLI
src/ict_trader/cli.py          plan 后自动审计
scripts/plot_ict_plotly.py     Confluence 面板
```

---

# A. GS-09 Killzone / Session（P0）

## 现状

```text
L5-01 = N/A
GS-09 = N/A
```

原因：没有 timezone-aware timestamp、RTH、Killzone、holidays、half_days。

## 缺口类型

```text
缺数据 + 缺逻辑
```

## 依赖

```text
15m / 1D OHLCV
market metadata
timezone
exchange calendar
session config
```

## A1. 目标定义

回答：

```text
当前 bar 是否属于该市场允许交易的 session / killzone？
```

输出：

```json
{
  "session_id": "us_ny_am",
  "in_rth": true,
  "in_killzone": true,
  "timezone": "America/New_York",
  "local_time": "2026-10-30T10:00:00-04:00",
  "utc_time": "2026-10-30T14:00:00Z"
}
```

## A2. 数据契约

### `config/markets.yaml`

```yaml
markets:
  us:
    timezone: America/New_York
    rth:
      - ["09:30", "16:00"]
    killzones:
      london: ["02:00", "05:00"]
      ny_am: ["07:00", "10:00"]
      silver_bullet: ["10:00", "11:00"]
    holidays: ["2026-12-25"]
    half_days:
      "2026-11-27": [["09:30", "13:00"]]

  hk:
    timezone: Asia/Hong_Kong
    rth:
      - ["09:30", "12:00"]
      - ["13:00", "16:00"]
    killzones:
      hk_am: ["09:30", "12:00"]
      hk_pm: ["13:00", "16:00"]
    holidays: []
    half_days: {}

  crypto:
    timezone: UTC
    rth: [["00:00", "23:59"]]
    killzones:
      asia: ["00:00", "08:00"]
      london: ["07:00", "16:00"]
      ny: ["12:00", "21:00"]
    holidays: []
    half_days: {}
```

### CSV 元数据

```csv
date,open,high,low,close,volume,timezone,market
```

若 CSV 没有 `timezone`，用 `--market` 查配置；仍缺失则 `N/A`。

## A3. 计算定义

```text
1. 读取 bar.date，视为市场本地 naive time
2. 用 zoneinfo 本地化：
   - 不存在的时间（DST spring forward）→ invalid
   - 歧义时间（fall back）→ fold=0，记录 ambiguous=true
3. 转 UTC
4. 交易日检查：holidays → fail；half_days → 用半日 RTH
5. RTH 检查：local_time 是否落在任一 RTH 区间
6. Killzone 检查：local_time 是否落在任一 KZ 区间
7. 生成 session_id = market + "_" + killzone_name
8. 输出 in_rth / in_killzone
```

默认阈值：无。  
可配置：KZ 起止时间、是否允许 RTH 外。

边界：

```text
bar 时间为 16:00 且 bar 是 15:45–16:00 → 仍属于 RTH
12:00–13:00 HK 午休 → in_rth=false
周末 → in_rth=false
DST 不存在时间 → invalid
```

## A4. 验收用例

### PASS — US 10:00 ET

```text
market=us
local=2026-10-30 10:00 ET
expected: in_rth=true, killzone=ny_am, session_id=us_ny_am
```

### FAIL — US 12:00 ET

```text
market=us
local=2026-10-30 12:00 ET
expected: in_rth=true, in_killzone=false, session_id=us_rth
```

### FAIL — HK 12:30 HKT

```text
market=hk
local=2026-10-30 12:30 HKT
expected: in_rth=false, in_killzone=false
```

### PASS — crypto 08:00 UTC London

```text
market=crypto
local=2026-10-30 08:00 UTC
expected: in_killzone=true, session_id=crypto_london
```

## A5. 与现有规则衔接

```text
L5-01 Killzone
GS-09 → L5-01
```

修改：

```text
L5-01 从 N/A 变为 PASS/FAIL
```

新增字段：

```text
session_id
in_rth
in_killzone
timezone
```

冲突：

```text
L0-02 market session 仍保留
L5-01 更细，不替代 L0-02
```

## A6. 风险与待确认

```text
需确认：
1. US 数据是否 ET naive？
2. Futu 是否提供半日市日历？
3. crypto 用 UTC 还是 exchange local？
4. HK 午休是否算 no-trade？
```

性能：每 bar 时区转换 O(n)，可缓存交易日历。

---

# B. GS-14 Breaker（P0）

## 现状

```text
L3-08 = N/A
GS-14 = N/A
```

原因：没有 breaker 状态机；`order_blocks` 只有 active/mitigated，没有 failed_at/retest_at/state。

## 缺口类型

```text
缺逻辑
```

## B1. 目标定义

回答：

```text
一个 OB 失效后，是否完成了回踩确认，形成 breaker？
```

输出：

```json
{
  "kind": "breaker",
  "origin_ob": {
    "direction": "bull",
    "top": 82.4,
    "bottom": 82.1,
    "created": 120
  },
  "failed_at": 138,
  "close_through": true,
  "retest_at": 144,
  "state": "confirmed"
}
```

## B2. 数据契约

新增 `breakers` 列表；字段：

```json
{
  "kind": "breaker",
  "origin_ob": {"direction": "bull|bear", "top": 0.0, "bottom": 0.0, "created": 0},
  "failed_at": 0,
  "close_through": true,
  "retest_at": 0,
  "state": "candidate|confirmed|invalid"
}
```

## B3. 计算定义

```text
1. 取已有 OB：
   bull OB: direction=bull
   bear OB: direction=bear

2. 失效：
   bull OB: close < OB.bottom - 0.10 * ATR
   bear OB: close > OB.top + 0.10 * ATR
   仅影线穿越不算

3. 记录 failed_at = 第一根满足失效的 bar

4. 回踩窗口：
   failed_at + 1 .. failed_at + 12
   价格进入 [OB.bottom, OB.top]

5. 回踩确认：
   bull breaker:
     low <= OB.top
     close > OB.top + 0.05 * ATR
   bear breaker:
     high >= OB.bottom
     close < OB.bottom - 0.05 * ATR

6. state：
   无失效 → invalid
   失效但无回踩 → candidate
   失效 + 回踩确认 → confirmed
```

默认值：

```text
close_buffer_atr = 0.10
retest_buffer_atr = 0.05
retest_window = 12 bars
```

## B4. 验收用例

### PASS — bull OB 失效后回踩

```text
构造 bull OB 82.1–82.4
bar 138 close 82.0（低于 82.1 - 0.1 ATR）
bar 144 low 82.2, close 82.5
expected: state=confirmed
```

### FAIL — 只有影线穿越

```text
bar 138 low 82.0, close 82.3
expected: state=invalid
```

### FAIL — 无回踩

```text
bar 138 close 82.0
bar 139–150 不回到 82.1–82.4
expected: state=candidate
```

## B5. 与现有规则衔接

```text
L3-08 Breaker
GS-14 → L3-08
```

新增：

```text
L3-08 从 N/A 变为 PASS/FAIL
```

冲突：

```text
OB 与 Breaker 同时提到 OB 时，取 breaker 状态
```

## B6. 风险与待确认

```text
需确认：
1. breake 用 close 还是 body close？
2. 回踩窗口 12 根是否按周期调整？
3. 是否允许 breaker 与 FVG 重叠？
```

---

# C. GS-10 HTF PDA（P1）

## 现状

```text
L2-08 已实现基础版：
  1H/4H active FVG/OB
  price 在 ±0.5 ATR 内 → PASS
```

## 缺口类型

```text
部分实现
```

## C1. 目标定义

区分：

```text
touch：价格进入 PD Array
delivery：价格进入后，HTF 结构/位移确认向该方向交付
```

输出：

```json
{
  "tf": "4h",
  "kind": "FVG",
  "bottom": 81.8,
  "top": 82.2,
  "touch": true,
  "delivery": true,
  "direction": "bear",
  "htf_cisd": true
}
```

## C2. 数据契约

```text
15m OHLCV
1D OHLCV
1H/4H 重采样
```

无新增外部数据。

## C3. 计算定义

```text
1. 对 1D / 4H / 1H 分别 analyze
2. 取 active FVG/OB
3. touch：
   price 进入 [bottom - 0.25 ATR, top + 0.25 ATR]
4. delivery direction：
   - 若 PD Array 是 bearish FVG/OB，目标方向 bear
   - bullish → bull
5. HTF CISD：
   HTF close 越过最近 swing 至少 0.10 ATR
6. delivery：
   touch + HTF CISD 同向 + 后续 HTF close 离开区域 >= 0.5 ATR
7. 1D 优先于 4H，4H 优先于 1H
```

默认：

```text
touch_atr = 0.25
delivery_atr = 0.50
cisd_atr = 0.10
```

## C4. 验收用例

### PASS — 4H bearish FVG delivery

```text
4H FVG 81.8–82.2
price 进入 82.1
4H bearish CISD close 81.6
后续 4H close 81.0
expected: touch=true, delivery=true
```

### FAIL — 仅触碰

```text
price 进入 82.1
没有 HTF CISD
expected: touch=true, delivery=false
```

### FAIL — 反向交付

```text
price 进入 bearish FVG
HTF close 突破 82.6 上方
expected: delivery=false
```

## C5. 与现有规则衔接

```text
L2-08
GS-10
```

新增：

```text
delivery_direction
htf_cisd
```

## C6. 风险

```text
HTF 重采样边界需用 _drop_incomplete_htf
1D 数据不足时降级为 4H
```

---

# D. GS-11 RVOL（P1）

## 现状

```text
L2-09 已实现：
  最后一根 volume / 20 根均量
  阈值 1.5
```

## 缺口类型

```text
部分实现
```

## D1. 目标定义

回答：

```text
sweep 当根 / CISD 当根的成交量，是否显著高于近期均值？
```

## D2. 数据契约

```csv
date,open,high,low,close,volume,volume_source
```

`volume_source`：

```text
consolidated / venue / unknown
```

## D3. 计算定义

```text
1. 取事件 bar：
   - sweep_bar（优先）
   - cisd.end
2. baseline = 前 20 根 volume 的 median（默认）
3. rvol = event_volume / baseline
4. winsorize baseline：
   剔除 > 99th percentile 的异常值
5. PASS if rvol >= 1.5
```

默认：

```text
rvol_window = 20
rvol_min = 1.5
baseline = median
```

## D4. 验收用例

### PASS

```text
sweep bar volume 2.0M
前 20 根 median 1.0M
rvol=2.0 → PASS
```

### FAIL

```text
sweep bar volume 1.1M
median 1.0M
rvol=1.1 → FAIL
```

### 边界

```text
volume_source=venue
rvol=1.6
expected: PASS with warning volume_quality=venue
```

## D5. 与现有规则

```text
L2-09
GS-11
```

## D6. 风险

```text
US venue volume 不是 consolidated
crypto 交易所 volume 可能刷量
需确认数据口径
```

---

# E. GS-12 SMT（P1）

## 现状

```text
L2-10 已实现：
  --smt-csv
  相关性 >= 0.70
  最近两个 swing high/low
```

## 缺口类型

```text
部分实现 + 缺数据
```

## E1. 目标定义

回答：

```text
关联标的在同一 swing 窗口是否出现背离？
```

## E2. 数据契约

```csv
date,open,high,low,close,volume
```

配对：

```yaml
pairs:
  - primary: BTCUSDT
    secondary: ETHUSDT
    min_corr: 0.70
  - primary: ES
    secondary: NQ
  - primary: XAUUSD
    secondary: XAGUSD
```

## E3. 计算定义

```text
1. 按 UTC 日期对齐
2. 取共同窗口最后 120 根
3. corr = pct_change 的 Pearson
4. corr < 0.70 → N/A
5. 取最近两个 swing high / low
6. bearish SMT：
   A higher high，B 未 higher high（差 >= 0.10 ATR）
7. bullish SMT：
   A lower low，B 未 lower low（差 >= 0.10 ATR）
8. 强度：
   A = 差 >= 0.25 ATR
   B = 0.10–0.25 ATR
   C = < 0.10 ATR → FAIL
```

## E4. 验收

### PASS

```text
BTC 高点 84,000 → 84,500
ETH 高点 3,200 → 3,180
corr 0.85
expected: bearish SMT PASS
```

### FAIL

```text
BTC HH，ETH HH
expected: FAIL
```

### N/A

```text
corr 0.45
expected: N/A
```

## E5. 衔接

```text
L2-10
GS-12
```

## E6. 风险

```text
需确认配对稳定性
相关性会随 regime 变化
```

---

# F. GS-13 OTE（P1）

## 现状

```text
L3-07 已实现基础版：
  最近 3 个 swing
  retracement 0.62–0.79
```

## 缺口类型

```text
部分实现
```

## F1. 目标定义

回答：

```text
entry 是否在有效 impulse leg 的 0.62–0.79 回撤，并且与 FVG/OB 重叠？
```

## F2. 数据契约

```text
OHLCV
swings
plan.entry
```

## F3. 计算定义

```text
1. 找 impulse leg：
   - 最近一个 swing low → swing high（bull）
   - 最近一个 swing high → swing low（bear）
   - leg range >= 1.0 ATR
2. range high/low
3. long retracement = (high - entry) / (high - low)
   short retracement = (entry - low) / (high - low)
4. OTE = 0.62 <= r <= 0.79
5. overlap = entry zone 与 FVG/OB 有交集
6. PASS = OTE + overlap
```

## F4. 验收

```text
PASS：leg 100→110，entry 103（70%），与 FVG 重叠
FAIL：entry 105（50%）
N/A：没有 plan
```

## F5. 衔接

```text
L3-07
GS-13
```

## F6. 风险

```text
leg 起点/终点定义需确认
```

---

# G. GS-15 Fresh FVG / CE（P1）

## 现状

```text
L3-09 已实现：
  active + fresh FVG 计数
```

## 缺口类型

```text
部分实现
```

## G1. 目标定义

```text
这个 FVG 是否 fresh、是否 first-presented、entry 离 CE 多远？
```

## G2. 数据契约

FVG 字段：

```json
{
  "direction": "bear",
  "top": 82.3,
  "bottom": 81.9,
  "ce": 82.1,
  "created": 130,
  "left": 130,
  "right": 150,
  "fresh": true,
  "first_presented": true,
  "touched_at": null,
  "touch_count": 0,
  "ce_distance_atr": 0.12
}
```

## G3. 计算定义

```text
1. first_presented：
   同方向、同 displacement 之后第一个 FVG
2. touch_count：
   price 进入 FVG 的次数
3. fresh = touch_count == 0
4. ce_distance_atr = |entry - ce| / ATR
5. fresh_score：
   first_presented +1
   fresh +1
   ce_distance <= 0.25 ATR +1
6. PASS if fresh_score >= 2
```

## G4. 验收

```text
PASS：first + fresh + CE 0.1 ATR → 3
FAIL：touched 2 次 + CE 0.5 ATR → 0
边界：fresh + CE 0.3 ATR → 2 → PASS
```

## G5. 衔接

```text
L3-09
GS-15
```

---

# H. GS-04 / GS-05 映射修正（P0）

## H1. GS-04 Displacement quality

现状：

```text
GS-04 → L3-04 CISD
```

问题：CISD 不等于 displacement。

新增 `L3-10`：

```text
body_ratio >= 0.60
range >= 1.0 ATR
close beyond swing >= 0.10 ATR
follow-through：下一根未收回 50%
```

输出：

```json
{"displacement": true, "body_ratio": 0.72, "range_atr": 1.4}
```

映射：

```text
GS-04 → L3-10
```

验收：

```text
PASS：1.4 ATR + 0.72 body + follow-through
FAIL：0.8 ATR
边界：1.0 ATR + 0.60 body + 无 follow-through → weak
```

## H2. GS-05 Sequence

问题：L1-04 只看最近 50 根有无 MSS，不看是否与当前 sweep 同一序列。

新增字段：

```json
{
  "sequence_id": "sweep_123",
  "sweep_bar": 123,
  "mss_bar": 130,
  "cisd_bar": 132,
  "entry_bar": 134
}
```

规则：

```text
sweep_bar < mss_bar <= cisd_bar <= entry_bar
mss_bar - sweep_bar <= 12
cisd_bar - mss_bar <= 6
```

映射：

```text
GS-05 → L1-04 + L3-05 sequence
```

验收：

```text
PASS：sweep 123 → MSS 130 → CISD 132 → entry 134
FAIL：MSS 在 sweep 之前
边界：间隔 13 根 → 降级
```

---

# I. GS-07 / GS-08 自动 stop/target（P1）

## 现状

```text
L4-01/L4-03 只有 plan 时判断
无 plan → N/A
```

## I1. 自动 stop

```text
long: min(sweep low, OB bottom) - 0.10 ATR
short: max(sweep high, OB top) + 0.10 ATR
risk = |entry - stop|
```

## I2. 自动 target

```text
long: 最近未扫上方 BSL/EQH/PDH
short: 最近未扫下方 SSL/EQL/PDL
```

## I3. net RR

```text
net_rr = (reward - cost) / (risk + cost)
PASS >= 2.5
```

## I4. 验收

```text
PASS：sweep 80.0, entry 80.5, stop 79.9, DOL 82.0, net 2.8
FAIL：DOL 81.0, net 1.8
N/A：无 DOL
```

---

# J. 外部数据缺口

| 数据 | 用途 | Schema | 接入 | 无数据降级 |
|---|---|---|---|---|
| timezone/session calendar | GS-09 | `markets.yaml` | 本地配置 | N/A |
| 第二标的 CSV | GS-12 | OHLCV | `--smt-csv` | N/A |
| 新闻日历 | L0-03/L5-02 | timestamp,event,impact | CSV/API | N/A |
| 点差/盘口 | L0-04 | timestamp,bid,ask | 券商 API | N/A |
| consolidated volume | GS-11 | volume,source | 数据商 | volume_quality=venue |
| 账户/仓位 | L4-05 | equity,risk_pct | 账户 API | N/A |
| 执行滑点 | L4-06 | fill_price,ts | 成交回报 | 2bps 保守 |
| dealing range 标注 | GS-13 | start,end | 人工/算法 | 自动 swing |

---

# K. 任务表

| 缺口 | 优先级 | 依赖 | 工作量 | 验收角色 |
|---|---|---|---|---|
| GS-09 session | P0 | 时区/日历 | M | 数据工程 + 规则 |
| GS-14 breaker | P0 | OB/ATR | M | 规则工程 |
| GS-04 L3-10 displacement | P0 | OHLCV | S | 规则工程 |
| GS-05 sequence | P0 | sweep/MSS/CISD | M | 规则工程 |
| GS-10 HTF PDA | P1 | 1H/4H/1D | M | 规则工程 |
| GS-11 RVOL | P1 | volume | S | 数据工程 |
| GS-12 SMT | P1 | 第二标的 | M | 数据工程 |
| GS-13 OTE | P1 | swing/plan | S | 规则工程 |
| GS-15 fresh FVG | P1 | FVG 元数据 | S | 规则工程 |
| GS-07/08 auto stop/target | P1 | plan/DOL | M | 规则工程 |
| 新闻/点差 | P2 | 外部数据 | M | 数据工程 |

## P0 最小闭环

```text
1. GS-09 session/timezone
2. L3-10 displacement
3. GS-05 sequence_id
4. GS-14 breaker
5. 回归测试
6. 影子模式对比
```

## 并行 / 串行

```text
可并行：
  GS-09 与 GS-14
  GS-10 与 GS-11
  GS-12 与 GS-13

必须串行：
  L3-10 → GS-04
  sequence_id → GS-05
  stop/target → GS-07/08
```

## 回归测试

```text
1. 保留旧版 audit 输出
2. 新增规则先 shadow mode，不阻断
3. 对比新旧 PASS/FAIL 差异
4. 对 100 个历史 setup 做人工复核
5. 任何 blocking 规则变化必须有 golden case
```

---

# L. 总结

```text
缺数据：
  GS-09 时区/session
  GS-12 第二标的
  新闻、点差、consolidated volume、账户、滑点

缺逻辑：
  GS-14 Breaker
  GS-04 Displacement quality
  GS-05 sequence
  GS-07/08 自动 stop/target

部分实现：
  GS-10 只有 touch，没有 delivery
  GS-11 只看最后一根
  GS-13 leg 定义不严
  GS-15 只有计数，没有评分

映射需重写：
  GS-04：L3-04 → L3-10
  GS-05：L1-04 → L1-04 + sequence
```

一句话：

> **先把 GS-09、GS-14、L3-10、sequence 做成 P0 闭环；GS-10/11/12/13/15 是增强项，按数据到位逐步启用；所有 N/A 都保留，不伪造 PASS。**
