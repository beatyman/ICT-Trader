# 什么是好的 setup：ICT + PA 语料归纳

Last updated: 2026-10-30

## 0. 语料范围与口径

模板中的语料范围、用途没有填写。本报告按仓库现有语料固定：

```text
语料：corpus/ 下 383 篇全文，14 个来源
用途：提炼可回测 setup 判据 + 统一复盘标准
市场：外汇 / crypto / 指数，语料未限定单一品种
周期：示例集中在 5m / 15m / 1H / 4H / D1
时间：语料未统一标注发布日期；导出日期 2026-10-29
```

来源分层：

| Tier | Sources | 文章数 | 用途 |
|---|---|---:|---|
| A | Inner Circle Traders, TTrades, Stoic Edge, Trading Strategy Guides | 127 | 模型逻辑 |
| B | ePlanet, Altrady, Volity, Flux, Trade The Pool, ITBFX | 110 | 应用与市场适配 |
| C | Vantage, Tradervue, Finxsol, 4xPip | 146 | 风控、执行、警告 |
| Blocked | ChartingLens | 0 | 只有摘要，不算证据 |

> 文章数不等于证据强度。A 类来源定义模型；B 类补充应用；C 类不投票决定 ICT 入场定义。

---

## 1. 语料概览

| 来源类型 | 代表来源 | 主要观点 | 证据强度 |
|---|---|---|---|
| 官方/主模型 | ICT, TTrades, Stoic, TSG | HTF → sweep → MSS/CISD → POI → DOL | A |
| 应用/加密 | Altrady, Volity, ePlanet, Flux | FVG/OB 回踩、HTF→LTF、风控 | B |
| 执行/复盘 | Tradervue, Vantage, Finxsol, 4xPip | RR、止损、journal、失败模式 | C |

### 【原文要点】

```text
Altrady, corpus/altrady_course/corpus.md:276
“read the chart in the same order: higher-timeframe structure first,
 then a sweep of liquidity, then a shift in structure,
 then an entry at a zone...”

Volity, corpus/volity_course/corpus.md:1183
“a clean sweep of a known swing point, then a decisive close back inside the range.”

Altrady, corpus/altrady_course/corpus.md:548
FVG is “a three-candle intrabar imbalance where consecutive candles show no price overlap”;
it is different from a session gap.

TTP, corpus/ttp_course/corpus.md:125
displacement “almost always results in a Market Structure Shift and a Fair Value Gap.”

Volity, corpus/volity_course/corpus.md:305
mark the block on a higher timeframe, refine the mitigation entry on a lower one.
```

---

## 2. “setup”与“好的 setup”定义

### 【原文归纳】

语料里的 setup 不是单一形态，而是一条**事件序列**：

```text
HTF context
→ liquidity pool
→ sweep / raid
→ structure shift (MSS/CHoCH/CISD)
→ PD Array (FVG/OB/Breaker/OTE)
→ entry trigger
→ stop / target / RR
```

### 【归纳】

```text
setup = 可重复的“上下文 + 流动性事件 + 结构确认 + 入场位置 + 风控”组合。
```

“好的 setup”不是：

```text
形态漂亮
看起来强
指标金叉
胜率高
```

而是：

```text
1. 方向有 HTF 依据
2. 流动性事件真实发生
3. 结构确认与流动性事件同向
4. 入场位置有 PD Array 支撑
5. 目标是对立流动性，不是随意 R
6. 扣除成本后仍有正期望
7. 失效条件清晰、可执行
```

### 共性与分歧

| 维度 | 共性 | 分歧 |
|---|---|---|
| 方向 | HTF bias 先行（12/14 来源） | 用 D1 还是 4H 为主 |
| 触发 | sweep 是核心（14/14） | sweep 与 grab 是否同一事件 |
| 确认 | MSS/CISD（12/14） | 是否必须 MSS，还是 BOS 即可 |
| 位置 | FVG/OB（13/14、12/14） | 是否必须 fresh / first FVG |
| 目标 | DOL（10/14） | 固定 RR 还是对立池 |
| 时段 | Killzone（9/14） | Asian range 具体时间 |
| SMT | 4/14 | 是否硬门槛 |

---

## 3. 好 setup 要素表

| 要素 | 语料覆盖 | 权重 | 证据强度 | 说明 |
|---|---:|---:|---|---|
| HTF bias / D1-4H | 12/14 | 高 | A | 不做逆 HTF 的核心模型 |
| Liquidity sweep | 14/14 | 高 | A | 没有 sweep 不做核心反转 |
| DOL target | 10/14 | 高 | A | 目标必须是对立流动性 |
| Displacement | 11/14 | 高 | A | 位移是 MSS/FVG 的燃料 |
| MSS / CHoCH / CISD | 12/14 | 高 | A | 结构确认，不是形态确认 |
| FVG | 13/14 | 高 | A | 入场区，不是单独理由 |
| Order Block | 12/14 | 中高 | A | 需要位移与 fresh |
| Killzone | 9/14 | 中 | B | 可配置，不是所有市场 |
| OTE 0.62–0.79 | 7/14 | 中 | B | 加分项 |
| Breaker / mitigation | 8/14 | 中 | B | 加分项 |
| Judas / AMD | 6/14 | 中 | C | 研究过滤 |
| SMT | 4/14 | 低 | C | 数据依赖，非硬门槛 |
| Volume | 分散 | 低中 | C | 语料有提及，未统一阈值 |

---

## 4. 可验证判据

### 必要条件（core）

#### GS-01 HTF 方向不相反

```text
ID: GS-01
名称: HTF alignment
定义: 1D 与 4H 的 3-factor score 同向，或至少不相反
计算:
  1D score = cloud side + TK + MACD，0–3
  4H score = 同上
  PASS if 1D>=2 and 4H>=2 and 方向一致
阈值: 2/3
正例: 1D 3/3 bull, 4H 2/3 bull
反例: 1D 3/3 bull, 4H 0/3 bear
边界: 1D 2/3, 4H 1/3 → 降级，不重仓
证据: A（12/14）
必要: 是
```

#### GS-02 对立 DOL 存在

```text
ID: GS-02
名称: clear draw on liquidity
定义: 交易方向前方存在未扫的 BSL/SSL/EQH/EQL/PDH/PDL
计算: 取最近未扫池；distance / risk >= 2.5
正例: 上方 84,330 BSL 未扫
反例: 所有池已扫
边界: 只有 1.8R → 不做
证据: A（10/14）
必要: 是
```

#### GS-03 流动性扫荡

```text
ID: GS-03
名称: liquidity sweep
定义:
  bear sweep: high > pool + 0.05 ATR, close < pool
  bull sweep: low < pool - 0.05 ATR, close > pool
  reclaim <= 3 bars
正例: 扫 EQH 后收回
反例: 收盘站上池，属于 breakout
边界: 只刺破 0.04 ATR → 不算
证据: A（14/14）
必要: 是
```

#### GS-04 位移确认

```text
ID: GS-04
名称: displacement
定义: leg range >= 1.0 ATR, body_ratio >= 0.60
正例: 一根 1.4 ATR、70% 实体的反向 K
反例: 小实体阴线
边界: 0.9 ATR → 弱位移，降级
证据: A（11/14）
必要: 是
```

#### GS-05 MSS / CISD

```text
ID: GS-05
名称: structure confirmation
定义:
  close 越过最近 swing 至少 0.10 ATR
  MSS 需 CHoCH + displacement
  CISD 需 close 穿过 opposing candle series
正例: 扫低后收盘站上 last swing high
反例: 只有影线突破
边界: 实体 45% → 不确认
证据: A（12/14）
必要: 是
```

#### GS-06 POI 入场

```text
ID: GS-06
名称: PD Array entry
定义:
  FVG: 三根 K 线、size >= 0.10 ATR、CE = 50%
  OB: 位移前最后反向 K 线，位移 >= 1.0 ATR
  价格回到 CE / OB 边缘
正例: 回踩 FVG CE 后出现 rejection
反例: 离 POI 1.5 ATR 追入
边界: 只回到 FVG 外沿，未到 CE → 降级
证据: A（13/14, 12/14）
必要: 是（至少一个 POI）
```

#### GS-07 结构止损

```text
ID: GS-07
名称: structural stop
定义:
  long SL = min(sweep low, OB bottom) - 0.10 ATR
  short SL = max(sweep high, OB top) + 0.10 ATR
  risk >= 0.30 ATR
正例: 止损在 sweep wick 外
反例: 止损放在 FVG 中间
边界: 止损距离 0.25 ATR → 太紧
证据: A/B
必要: 是
```

#### GS-08 净 RR

```text
ID: GS-08
名称: net RR gate
定义:
  net_rr = (reward - cost) / (risk + cost)
  cost = 佣金 + 滑点 + 卖出税
阈值: net_rr >= 2.5
正例: gross 3.2, net 2.8
反例: gross 2.0, net 1.6
边界: net 2.4 → 不做
证据: B/C；语料没有统一 1:3 硬门槛
必要: 是
```

### 加分条件（enhancing）

#### GS-09 Killzone / session

```text
ID: GS-09
定义: 入场落在 London / NY AM / Silver Bullet / HK 时段
证据: B（9/14）
必要: 否，但强加分
```

#### GS-10 HTF PDA delivery

```text
ID: GS-10
定义: 价格正在交付 1H/4H FVG 或 OB
证据: A/B
必要: 加分；与 GS-06 不重复
```

#### GS-11 Volume / RVOL

```text
ID: GS-11
定义: sweep/displacement bar volume >= 1.5 × 20-bar average
证据: C，语料无统一阈值
必要: 否
```

#### GS-12 SMT

```text
ID: GS-12
定义: 关联标的在相同 swing 窗口出现背离；相关性 >= 0.70
证据: C（4/14）
必要: 否；无第二标的数据则 N/A
```

#### GS-13 OTE

```text
ID: GS-13
定义: 回撤 0.62–0.79
证据: B（7/14）
必要: 否
```

#### GS-14 Breaker

```text
ID: GS-14
定义: OB 失效后回踩另一侧
证据: B（8/14）
必要: 否
```

#### GS-15 Fresh FVG / CE

```text
ID: GS-15
定义: FVG 未被回补，入场在 CE 附近
证据: B；语料支持“fresh 更好”，但不支持 hard reject non-first
必要: 否
```

---

## 5. 低质量与失败模式

| 失败模式 | 语料依据 | 判据 |
|---|---|---|
| 逆 HTF 做反转 | 12/14 强调 HTF bias | GS-01 fail |
| 没有 sweep 直接追 FVG | 14/14 强调 sweep | GS-03 fail |
| 把大 K 线当 displacement | TTP/4xPip 对 displacement 有实体/连续要求 | GS-04 fail |
| FVG 未回踩就入场 | Altrady 强调 retest/CE | GS-06 fail |
| 止损放在结构内部 | Volity sweep 示例止损在 wick 外 | GS-07 fail |
| RR 不足 | Tradervue 强调 RR 可视化 | GS-08 fail |
| 新闻窗口入场 | B/C 来源反复警告 | GS-09 fail |
| 区间中部入场 | 结构语料要求边界/池 | GS-03 fail |
| 把 session gap 当 FVG | Altrady 明确区分 | GS-06 fail |
| 过度交易 / 无 journal | Tradervue/Finxsol | 过程失败 |

---

## 6. 分层结论

### 最小必要条件组合

```text
GS-01 HTF 不相反
+ GS-02 对立 DOL 存在
+ GS-03 有效 sweep
+ GS-04 displacement
+ GS-05 MSS/CISD
+ GS-06 FVG/OB/CE 回踩
+ GS-07 结构止损
+ GS-08 net RR >= 2.5
```

### 理想增强条件

```text
GS-09 Killzone
GS-10 HTF PDA delivery
GS-11 volume / RVOL
GS-12 SMT
GS-13 OTE
GS-14 Breaker
GS-15 fresh FVG / CE
```

### 按 setup 类型

#### A. Sweep + CISD reversal

```text
画像：
  HTF 对齐
  扫 PDH/PDL/EQH/EQL
  收盘收回
  displacement >= 1 ATR
  CISD 越过 swing
  回踩 CISD origin / FVG
  SL 在 sweep 外
  TP 对立 DOL
证据：A；语料共识最高
```

#### B. Fresh FVG CE retest

```text
画像：
  先有 displacement
  FVG >= 0.10 ATR
  未回补
  回踩 CE
  rejection close
  SL FVG 远侧
  TP DOL
证据：A/B；执行细节强，但不能单独成立
```

#### C. Order Block retest

```text
画像：
  位移前最后反向 K
  OB fresh
  回踩 OB 边缘
  结构未破
  SL OB 外
证据：A/B；必须配合 sweep/CISD
```

#### D. Breaker

```text
画像：
  OB 失效
  回踩另一侧
  结构确认
证据：B；加分项，不单独作为核心模型
```

#### E. Turtle Soup

```text
画像：
  扫前高/前低
  快速收回
  MSS
证据：B；与 sweep+CISD 高度重叠，不另算独立 edge
```

#### F. Silver Bullet

```text
画像：
  特定时间窗
  FVG
  displacement
证据：B；样本小，时间窗依赖市场
```

#### G. Judas Swing

```text
画像：
  开盘假突破
  反向 sweep
  MSS
证据：C；研究过滤，不是核心
```

---

## 7. 冲突与空白

| 问题 | 语料状态 | 处理 |
|---|---|---|
| Sweep vs grab | 术语混用 | 统一为 sweep 价格事件 |
| Asian range 时间 | 19:00–00:00 / 19:00–02:00 / 23:00–06:00 | 固定一个配置，A/B 测试 |
| RR 1:3 / 1:5 | 无跨市场证据 | 用 net RR >= 2.5 |
| FVG first/fresh | first 过于严格，fresh 更好但非硬门槛 | fresh 加分，不 hard reject |
| 止损 Candle 2 | 与 sweep extreme 冲突 | sweep extreme 为默认 |
| SMT | 4/14，数据依赖 | 可选，N/A 不通过 |
| Displacement 阈值 | 语料多为主观描述 | 1.0 ATR + body 0.60，待 A/B |
| Equal highs 容差 | 未统一 | max(5bp, 0.10 ATR)，待定 |
| Volume | 提及但无阈值 | 1.5× 均量，待定 |
| News calendar | 语料无统一日历 | 外部补充，N/A |

---

## 8. 可回测化建议

可直接写成脚本：

```text
GS-01 HTF score
GS-02 DOL distance
GS-03 sweep
GS-04 displacement
GS-05 MSS/CISD
GS-06 FVG/OB/CE
GS-07 stop
GS-08 net RR
GS-09 session
GS-10 HTF PDA
```

需要人工/外部数据：

```text
GS-11 volume：有 OHLCV 可算 RVOL，但阈值待定
GS-12 SMT：需要第二标的
GS-13 OTE：可算，但权重待定
GS-14 Breaker：需分类器
GS-15 fresh FVG：可算，但“first”定义有争议
```

---

## 9. 待确认问题

```text
1. 语料是否只限 corpus/，是否需要逐条引用原文？
2. 目标市场是外汇、crypto、指数，还是多市场？
3. 是否有新闻日历？
4. 是否有逐笔/盘口/volume profile？
5. Displacement 用 1.0 ATR 还是按品种调整？
6. Equal highs 容差用 5bp 还是 0.10 ATR？
7. Asian range 采用哪个时区版本？
8. SMT 关联标的池如何固定？
9. 是否允许逆 HTF 的 counter-trend 模型？
10. 最小样本、最大回撤、通过标准如何定？
```

---

## 10. 一句话结论

> **语料中的“好 setup”不是某个形态，而是一条完整链路：HTF 不相反 → 有对立 DOL → 真实 sweep → displacement → MSS/CISD → FVG/OB/CE 回踩 → 结构止损 → 净 RR ≥ 2.5。Killzone、HTF PDA、volume、SMT、OTE、Breaker 是加分项，不是替代核心的捷径。**

---

## 11. 已集成到代码

`src/ict_trader/rules.py` 输出 `good_setup`：

```text
GS-01 HTF alignment        -> L5-03
GS-02 clear DOL            -> L2-07
GS-03 liquidity sweep      -> L2-04
GS-04 displacement / CISD  -> L3-04
GS-05 structure            -> L1-04
GS-06 FVG / OB / CE        -> L3-01 / L3-02
GS-07 structural stop      -> L4-01
GS-08 net RR >= 2.5        -> L4-03
GS-09 killzone             -> L5-01 (N/A without session data)
GS-10 HTF PDA              -> L2-08
GS-11 volume / RVOL        -> L2-09
GS-12 SMT                  -> L2-10 (N/A without second symbol)
GS-13 OTE                  -> L3-07 (N/A without plan)
GS-14 Breaker              -> L3-08 (N/A classifier)
GS-15 fresh FVG / CE       -> L3-09
```

`scripts/audit_ict_rules.py` 会输出：

```json
"good_setup": {
  "decision": "FAIL",
  "failed": ["GS-01", "GS-03", "GS-07", "GS-08"],
  "incomplete": []
}
```

`ict-trader plan` 在生成 setup 后自动跑审计；任何 blocking fail 都会把计划降为 `NO_TRADE`。

