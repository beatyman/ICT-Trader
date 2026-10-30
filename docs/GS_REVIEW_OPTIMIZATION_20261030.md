# BTCUSDT 复盘优化方案（2026-10-30）

## 0. 复盘证据

```text
方案时间：2026-10-30 12:45 UTC
方案收盘：77,435.0
复盘时价格：77,287.8
状态：NO_TRADE
ATR：256.5
1D / 1H / 4H：0/3 / 1/3 / 1/3
11:45 扫荡：SSL 77,181
L3-01 FVG 计数：263
L3-02 OB 计数：8
L3-09 fresh FVG：261
L2-08：1H FVG 77,535–77,889，价格 77,435
L3-04：20 根内无 CISD
L4-01 / L4-03：无 plan，N/A
条件 plan：空 78,700 / SL 79,300 / TP 76,444
```

---

# 1. 优化项

## O-01 HTF 评分方向性（P0，假 FAIL）

### 问题类别

```text
口径错误 / 规则误判
```

### 证据

```text
1D 0/3，1H 1/3，4H 1/3
1H/4H 价格在云下，Tenkan < Kijun，MACD < Signal
当前 _htf_score 只检查 tenkan > kijun 和 macd > signal
```

结果：

```text
空头永远拿不到 2/3 或 3/3
纯空头 1H/4H 被压成 1/3
```

### 改动

文件：

```text
src/ict_trader/rules.py
docs/HTF_SCORING.md（新增）
tests/test_htf_score.py
```

函数：

```python
@dataclass
class HtfScore:
    long_score: int   # 0..3
    short_score: int  # 0..3
    direction: str    # bull | bear | neutral

def htf_scores(df, tf: str) -> HtfScore
```

公式：

```text
long_score:
  close > cloud_top        +1
  tenkan > kijun           +1
  macd > signal            +1

short_score:
  close < cloud_bottom     +1
  tenkan < kijun           +1
  macd < signal            +1
```

`L5-03` 改为按计划方向取分：

```text
long: long_score >= 2 on 1D and 4H
short: short_score >= 2 on 1D and 4H
```

无 plan：

```text
用 15m trend 或 N/A
```

输出：

```json
"L5-03": {
  "direction": "short",
  "1d": {"long": 1, "short": 2},
  "1h": {"long": 0, "short": 3},
  "4h": {"long": 0, "short": 3},
  "status": "PASS"
}
```

### 验收

```text
PASS：1D 云下 + TK< + MACD< → short 3/3
PASS：1D 云上 + TK> + MACD> → long 3/3
边界：云内 + TK< + MACD< → short 2/3
FAIL：云上 + TK> + MACD> 对 short → short 0/3
```

复盘日预期：

```text
1D short 2/3
1H short 3/3
4H short 2/3（Tenkan = Kijun，严格 < 不成立；预期 3/3 是错的）
L5-03 由 FAIL → PASS（仅 short 方向）
```

### 回归

```text
GS-01 可能 FAIL → PASS
需要 shadow
所有空头 setup 的 HTF 结论会变化
```

### 风险

```text
可能放行更多逆势空
必须 shadow + 人工复核
```

### 工作量

```text
M
前置：O-02
```

---

## O-02 未完成 HTF bar（P0，口径错误）

### 证据

```text
15m 数据最后 12:45
1H 最后一根 12:00，未收盘
4H 最后一根 12:00，未收盘
当前 _htf_score / _htf_pda 仍可能使用
```

### 改动

文件：

```text
src/ict_trader/rules.py
tests/test_htf_incomplete.py
```

函数：

```python
def _drop_incomplete_htf(df, h, tf) -> pd.DataFrame:
    ...
```

规则：

```text
bar_end = last_htf_bar_start + timeframe
if df.last_time < bar_end: drop last
```

周期：

```text
1h → +1h
4h → +4h
1d → +1D
```

### 验收

```text
PASS：15m 最后 12:45，1H 12:00 被剔除，使用 11:00
PASS：15m 最后 12:45，4H 12:00 被剔除，使用 08:00
FAIL：15m 最后 13:00，1H 13:00 不应被剔除（已收盘）
```

复盘日修复前后：

```text
1H：12:00 → 11:00，score 仍 1/3（旧口径）
4H：12:00 → 08:00，score 仍 1/3（旧口径）
1D：不变
```

### 工作量

```text
S
```

---

## O-03 POI 范围限定（P0，假 PASS）

### 证据

```text
L3-01 = 263 个 active FVG
L3-02 = 8 个 active OB
GS-06 = PASS
但大多数与当前 entry 78,700 无关
```

### 改动

```text
src/ict_trader/rules.py
tests/test_poi_scope.py
```

定义当前 setup 范围：

```text
entry_ref = plan.entry if plan else last_close
price_lo = entry_ref - k * ATR
price_hi = entry_ref + k * ATR
time_lo = max(0, n - 60)
relevant = active 且 created >= time_lo
           且 [bottom, top] 与 [price_lo, price_hi] 有交集
k = 0.5 默认，可配置
```

L3-01 / L3-02 改为：

```text
PASS if len(relevant_fvg) > 0
PASS if len(relevant_ob) > 0
```

### 验收

复盘日 entry 78,700，ATR 256.5：

```text
旧：FVG 263，OB 8
新：FVG 1（78,374–78,660），OB 0
```

用例：

```text
PASS：构造 300 个远端 FVG + 1 个 entry 附近 FVG → relevant=1
FAIL：300 个远端 FVG，entry 附近 0 → FAIL
边界：FVG 距 entry 0.49 ATR → 计入；0.51 ATR → 不计
```

### 回归

```text
GS-06 可能 PASS → FAIL（如果附近无 POI）
复盘日仍有 1 个相关 FVG → GS-06 仍 PASS
但计数从 263 变 1
```

### 风险

```text
窗口过窄会误杀
k 可配置：0.5 / 1.0
```

### 工作量

```text
S
```

---

## O-04 Fresh FVG 范围（P0，假 PASS）

### 证据

```text
L3-09 = 261 fresh FVG
GS-15 = PASS
与当前 setup 无关
```

### 改动

同 O-03 的 relevant 集合：

```text
relevant fresh = relevant FVG 且 fresh=true
```

L3-09：

```text
PASS if len(relevant_fresh) > 0
```

### 验收

复盘日：

```text
旧：261
新：1（78,374–78,660）
```

用例：

```text
PASS：entry 附近 1 个 fresh
FAIL：只有远端 fresh
边界：fresh 但距 entry 0.6 ATR → 不计
```

### 回归

```text
GS-15 可能 PASS → FAIL
复盘日仍 PASS，但计数变 1
```

### 工作量

```text
S
```

---

## O-05 DOL 方向性（P1）

### 证据

```text
L2-07 = PASS
但没有方向也能 PASS
```

### 改动

```text
src/ict_trader/rules.py
tests/test_dol.py
```

规则：

```text
long DOL = 上方最近未扫 BSL/EQH/PDH
short DOL = 下方最近未扫 SSL/EQL/PDL
无 plan → N/A
```

### 验收

```text
PASS：long → 78,776
PASS：short → 76,444
N/A：无 plan
FAIL：方向侧无未扫池
```

### 回归

```text
L2-07 从任意池 PASS → 方向性 PASS/N/A
```

### 工作量

```text
S
```

---

## O-06 touch vs delivery（P1）

### 证据

```text
1H FVG 77,535–77,889
价格 77,435
当前 L2-08 = PASS
但没有 HTF CISD
```

### 改动

```text
src/ict_trader/rules.py
tests/test_htf_pda.py
```

输出：

```json
{
  "touch": true,
  "delivered": false,
  "direction": "bear",
  "htf_cisd": false
}
```

规则：

```text
touch：进入 PD Array ±0.25 ATR
delivery：touch + HTF CISD 同向 + 离开区域 >= 0.5 ATR
GS-10 PASS only if delivered
```

### 验收

复盘日：

```text
touch=true
delivered=false
GS-10 PASS → FAIL（或 touch_only）
```

用例：

```text
PASS：4H FVG + HTF CISD + close 离开 0.5 ATR
FAIL：只有 touch
边界：离开 0.49 ATR → FAIL
```

### 工作量

```text
M
```

---

## O-07 L3-10 displacement（P1）

### 证据

```text
L3-04 无 CISD
但 GS-04 只映射 CISD，没有 displacement quality
```

### 改动

```text
src/ict_trader/displacement.py
tests/test_displacement.py
```

公式：

```text
body_ratio >= 0.60
range_atr >= 1.0
close_beyond >= 0.10 ATR
follow_through = next close 未收回 50%
```

映射：

```text
GS-04 → L3-10
L3-04 CISD 保留
```

### 验收

```text
PASS：1.4 ATR + body 0.72 + follow-through
FAIL：0.8 ATR
边界：1.0 ATR + body 0.60 + 无 follow-through → weak
```

### 回归

```text
GS-04 不再由 CISD 单独决定
```

### 工作量

```text
S
```

---

## O-08 Sequence 绑定（P1）

### 证据

```text
sweep 11:45
没有 MSS
没有 CISD
L1-04 只看最近 50 根有没有 MSS
```

### 改动

```text
src/ict_trader/sequence.py
src/ict_trader/rules.py
tests/test_sequence.py
```

字段：

```text
sequence_id
sweep_bar
mss_bar
cisd_bar
entry_bar
```

约束：

```text
sweep < mss <= cisd <= entry
mss - sweep <= 12
cisd - mss <= 6
```

### 验收

```text
PASS：sweep 123 → MSS 130 → CISD 132 → entry 134
FAIL：MSS 在 sweep 前
FAIL：sweep 11:45，无 MSS/CISD
```

### 工作量

```text
M
```

---

## O-09 条件 plan 纳入 audit（P1）

### 证据

```text
条件 plan：78,700 / 79,300 / 76,444
L4-01 / L4-03 = N/A
GS-07 / GS-08 = N/A
```

### 改动

```text
src/ict_trader/cli.py
src/ict_trader/rules.py
schemas/plan.json
tests/test_conditional_plan.py
```

plan 字段：

```json
{
  "plan_status": "conditional",
  "trigger": "retest 78,700-78,776 + CISD",
  "entry": 78700,
  "sl": 79300,
  "tp": 76444,
  "distance_to_entry_atr": 4.93,
  "fill_probability": "low"
}
```

audit：

```text
conditional plan 也进入 L4-01 / L4-03
GS-07/08 不再 N/A
```

### 验收

复盘日：

```text
GS-07 PASS（risk 2.34 ATR）
GS-08 PASS（net 3.03）
fill_probability=low
```

### 回归

```text
GS-07/08 从 N/A 变为 PASS/FAIL
```

### 工作量

```text
M
```

---

## O-10 低成交概率标注（P2）

### 证据

```text
Entry 78,700
当前 77,435
距离 1,265 = 4.93 ATR = 1.63%
```

### 改动

```text
src/ict_trader/rules.py
src/ict_trader/cli.py
```

规则：

```text
distance_to_entry_atr > 1.0
或 distance_pct > 1.5%
→ low_fill_probability=true
```

### 验收

```text
复盘日：true
边界：1.0 ATR → false；1.01 ATR → true
```

### 工作量

```text
S
```

---

## O-11 Session / Breaker / SMT

不重复展开，只记录：

```text
GS-09 session：N/A，缺 timezone/calendar
GS-14 breaker：N/A，未实现
GS-12 SMT：N/A，缺第二标的
```

它们与本次复盘的关系：

```text
本次 NO_TRADE 不依赖这三项
但它们仍是结构缺口
```

---

# 2. 优化项总表

| 编号 | 名称 | 类别 | 优先级 | 影响规则 | 工作量 | 依赖 |
|---|---|---|---|---|---|---|
| O-01 | HTF 评分方向性 | 口径/假 FAIL | P0 | L5-03、GS-01 | M | O-02 |
| O-02 | 未完成 HTF bar | 口径 | P0 | L5-03、L2-08 | S | 无 |
| O-03 | POI 范围 | 假 PASS | P0 | L3-01/02、GS-06 | S | 无 |
| O-04 | Fresh FVG 范围 | 假 PASS | P0 | L3-09、GS-15 | S | O-03 |
| O-05 | DOL 方向 | 映射 | P1 | L2-07、GS-02 | S | plan |
| O-06 | touch/delivery | 部分实现 | P1 | L2-08、GS-10 | M | O-02 |
| O-07 | L3-10 displacement | 映射 | P1 | L3-10、GS-04 | S | 无 |
| O-08 | sequence | 映射 | P1 | L1-04、GS-05 | M | O-07 |
| O-09 | 条件 plan audit | 流程 | P1 | L4-01/03、GS-07/08 | M | O-05 |
| O-10 | 低成交概率 | 流程 | P2 | plan | S | O-09 |
| O-11 | session/breaker/SMT | 未实现 | P1/P2 | L5-01、L3-08、L2-10 | M | 数据 |

---

# 3. P0 修复顺序

```text
1. O-02 未完成 HTF bar
2. O-01 HTF 评分方向性
3. O-03 POI 范围
4. O-04 Fresh FVG 范围
5. 回归 + shadow
```

理由：

```text
O-02 是数据口径，必须先修
O-01 依赖 O-02 的已收盘 bar
O-03/O-04 是假 PASS，影响 GS-06/15
```

可并行：

```text
O-03 与 O-04
```

必须串行：

```text
O-02 → O-01
O-03 → O-04
```

---

# 4. 回归计划

## 修复前后预期

| 规则 | 修复前 | 修复后 | 说明 |
|---|---|---|---|
| L5-03 | FAIL（1D0/1H1/4H1） | PASS（short 1D2/1H3/4H2） | O-01 + O-02；4H TK=Kijun |
| GS-01 | FAIL | PASS | 方向性评分 |
| L3-01 | 263 FVG | 1 相关 FVG | O-03 |
| L3-02 | 8 OB | 0 相关 OB | O-03 |
| L3-09 | 261 fresh | 1 相关 fresh | O-04 |
| GS-06 | PASS | PASS（1 FVG） | O-03 |
| GS-15 | PASS | PASS（1 fresh） | O-04 |
| GS-10 | PASS（touch） | FAIL（not delivered） | O-06 |
| GS-07 | N/A | PASS（risk 2.34 ATR） | O-09 |
| GS-08 | N/A | PASS（net 3.03） | O-09 |
| GS-04 | FAIL | FAIL（no CISD） | O-07 |
| GS-05 | FAIL | FAIL（no sequence） | O-08 |

最终：

```text
decision 仍为 NO_TRADE
原因从“HTF 0/3”变为“没有 CISD / sequence”
```

## shadow 指标

```text
PASS→FAIL
FAIL→PASS
blocking_failures 分布
NO_TRADE 比例
人工复核 30 例
```

---

# 5. 文档更新

```text
docs/HTF_SCORING.md
docs/POI_SCOPE.md
docs/CONDITIONAL_PLAN.md
docs/GS_IMPLEMENTATION_SPEC.md
```

必须写清：

```text
0/3–3/3 的方向性定义
POI 的 entry_ref / time window / ATR window
conditional plan 的字段与 audit 路径
低成交概率阈值
```

---

# 6. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| HTF 方向性放宽空头 | 更多逆势空 | shadow + 人工复核 |
| POI 窗口过窄 | 误杀好 setup | k 可配置 0.5/1.0 |
| Fresh 范围过窄 | GS-15 全 FAIL | 先 warning |
| 条件 plan 审计 | 旧 plan JSON 不兼容 | schema_version |
| 序列扫描 | 性能 | O(n)，缓存 |

---

# 7. 最终 10 行

```text
最该先修的口径：L5-03 HTF 评分方向性。
当前 1H/4H 纯空头只能拿 1/3，空头永远过不了 2/3。
第二口径：1H 12:00 / 4H 12:00 未收盘却被使用。
最该先修的假 PASS：L3-01/02/09 全历史计数。
复盘日 263 FVG / 261 fresh，相关只有 1 个。
修复后预期 GS-06 仍 PASS，但从 263 → 1。
GS-15 仍 PASS，但从 261 → 1。
GS-10 从 touch PASS → not delivered FAIL。
GS-07/08 从 N/A → PASS（条件 plan 纳入）。
decision 仍 NO_TRADE，因为 GS-04/05 没有 CISD/sequence。
```
