# P0 之后质量加固与 P1 启动

Last updated: 2026-10-30

## 0. 依据

P0 执行报告中的实际证据：

```text
4H Tenkan = Kijun = 79,198.85
L5-03 short: 1D 2/3, 1H 3/3, 4H 2/3
L3-02: PASS → FAIL（8 → 0）
GS-06: FVG PASS + OB FAIL → 仍 PASS
GS-10: 仍 PASS（touch_only）
shadow：离线对比，无 --rule-mode
回归集：仅 2026-10-30 BTCUSDT 单日
POI：n-60、±0.5 ATR 无敏感性依据
blocking：4 → 3，decision 不变
audit：≈158.7 ms
```

---

# 1. S-01 评分函数边界规范

- 证据：`4H Tenkan = Kijun = 79,198.85`，strict short 要求 `tenkan < kijun`，相等不成立，实际 2/3。
- 风险等级：高
- 归类：并入 P1，作为 P1 第 0 项
- 改动点：

```text
src/ict_trader/rules.py::_score_row
config/rules.json::htf.eq_atr
docs/HTF_SCORING.md
tests/test_htf_boundary.py
```

规则：

```text
eq_atr = 0.05
tk_state = bull if tenkan > kijun + eq_atr
           bear if tenkan < kijun - eq_atr
           neutral otherwise
macd_state = bull if macd > signal + eq_atr
             bear if macd < signal - eq_atr
             neutral otherwise
```

评分：

```text
long_score  += 1 only for bull state
short_score += 1 only for bear state
neutral 不给分
```

输出：

```json
"tk_state": "neutral",
"macd_state": "bear",
"eq_atr": 0.05
```

验收：

```text
PASS：tenkan = kijun + 0.06 ATR → bull
FAIL：tenkan = kijun + 0.04 ATR → neutral，不计分
边界：tenkan = kijun → neutral，稳定
```

敏感性：

```text
eq_atr = 0.02 / 0.05 / 0.10
观察 L5-03 状态变化
```

工作量：S  
依赖：无

---

# 2. S-02 any-of 规则的最小覆盖

- 证据：`GS-06 = FVG PASS + OB FAIL → PASS`；`L3-02` 从 8 个 OB 变成 0 个仍不阻断。
- 风险等级：中高
- 归类：并入 P1
- 改动点：

```text
src/ict_trader/rules.py::gs
src/ict_trader/rules.py::L3-01/L3-02
config/rules.json::poi
tests/test_gs06_quality.py
```

当前：

```text
any FVG PASS or any OB PASS → GS-06 PASS
```

目标：

```text
至少一个相关 POI，且 POI quality >= 1
```

POI quality：

```text
+1 size_atr >= 0.10
+1 fresh
+1 CE distance <= 0.25 ATR
+1 created within n-20
```

默认：

```text
min_poi_quality = 1
```

验收：

```text
PASS：1 个 FVG，size 0.2 ATR + fresh → quality 2
FAIL：1 个 FVG，size 0.05 ATR + touched 2 次 → quality 0
边界：quality 1 → PASS
```

输出：

```json
"L3-01": {"count": 1, "quality": 2, "passed": true}
```

工作量：M  
依赖：O-03、O-04

---

# 3. S-03 未完成项的 provisional 语义

- 证据：`GS-10` 仍 PASS，因为 O-06 未执行；报告只能写 `touch_only`。
- 风险等级：高
- 归类：P1 前必须补
- 改动点：

```text
src/ict_trader/rules.py::RuleResult
src/ict_trader/rules.py::_status
src/ict_trader/rules.py::gs
scripts/audit_ict_rules.py
```

新增：

```python
provisional: bool = False
```

语义：

```text
PASS + provisional=false  → 真通过
PASS + provisional=true   → 临时放行，不计入 good_setup 硬性项
N/A                       → 缺失
```

GS 处理：

```text
necessary + provisional → 视为 N/A，good_setup 记 incomplete
enhancing + provisional → 记 PASS(provisional)，不参与硬性判定
```

GS-10 在 O-06 完成前：

```json
{"status": "PASS", "provisional": true, "reason": "touch only; delivery not implemented"}
```

验收：

```text
GS-10 不再污染 good_setup
audit 输出 provisional=true
shadow diff 中单独计数
```

工作量：S  
依赖：无

---

# 4. S-04 shadow 基础设施

- 证据：报告第 4 节写“`--rule-mode` CLI 尚未实现，以上是离线对比”。
- 风险等级：高
- 归类：P1 前必须补
- 改动点：

```text
src/ict_trader/rules.py::audit(..., mode)
src/ict_trader/cli.py::cmd_plan
scripts/audit_ict_rules.py
tests/test_rule_mode.py
```

接口：

```python
audit(..., mode="strict") -> dict
```

shadow 输出：

```json
"shadow": {
  "old_decision": "TRADE",
  "new_decision": "NO_TRADE",
  "diff": ["L3-04", "L5-03"],
  "would_block": true
}
```

分支点：

```text
strict: decision = new_decision
shadow: decision = old_decision
```

old_decision 来源：

```text
plan.decision if plan else "NO_TRADE"
```

验收：

```text
同一数据 strict → NO_TRADE
同一数据 shadow → 原 decision 不变
diff 可复现
```

工作量：M  
依赖：无

---

# 5. S-05 回归覆盖不足

- 证据：全部回归基于 2026-10-30 BTCUSDT 单日。
- 风险等级：高
- 归类：并入 P1（第一优先）
- 改动点：

```text
tests/fixtures/regression/
tests/test_regression_set.py
docs/REGRESSION_SET.md
```

最小回归集：

```text
1. BTC 2026-10-30 趋势下跌日
2. BTC 2026-10-29 高波动/跳空日
3. BTC 震荡日（选一个 range 日）
4. US.MSFT 2026-10-29 财报缺口日
5. HK.09988 2026-10-29 支撑日
6. Synthetic：tenkan=kijun
7. Synthetic：263 FVG / 0 relevant
8. Synthetic：session 边界的 16:00 bar
```

通过标准：

```text
不是 decision 不变
而是每条规则变化都有解释
```

输出：

```text
regression_report.json
  case_id
  rules_changed
  expected_change
  actual_change
  consistent
```

工作量：M  
依赖：S-03、S-04

---

# 6. S-06 魔法数字

- 证据：`n-60`、`±0.5 ATR` 没有依据。
- 风险等级：中
- 归类：并入 P1
- 改动点：

```text
config/rules.json::poi
docs/POI_SCOPE.md
tests/test_poi_sensitivity.py
```

先做两个扫描：

```text
time window: 30 / 60 / 90
price window: 0.25 / 0.50 / 0.75 ATR
```

指标：

```text
relevant POI count
GS-06/GS-15 status
false PASS rate
missed POI rate（人工复核）
```

验收：

```text
给出 3×3 表
选定默认值并写明依据
```

工作量：M  
依赖：S-05

---

# 7. S-07 预期表纠错流程

- 证据：4H 预期 3/3 被改为 2/3。
- 风险等级：高
- 归类：P1 前必须补
- 改动点：

```text
docs/EXPECTATION_CHANGELOG.md
tests/expected/README.md
```

模板：

```text
change_id:
date:
rule_id:
old_expected:
new_expected:
evidence:
classification: expectation_error | implementation_error | data_issue
confirmed_by:
regression_case:
```

判定：

```text
expectation_error：
  公式/定义明确，旧预期与公式矛盾
implementation_error：
  代码输出与定义矛盾
data_issue：
  输入数据/时区/未完成 bar 导致
```

规则：

```text
没有 evidence 不允许改预期
没有 confirmed_by 不允许合并
```

工作量：S  
依赖：无

---

# 8. S-08 blocker 冗余与重要性

- 证据：blocking 4→3，decision 不变；no-plan 与 conditional blocker 不同。
- 风险等级：中
- 归类：并入 P1
- 改动点：

```text
scripts/blocker_ablation.py
docs/BLOCKER_GRAPH.md
tests/test_blocker_ablation.py
```

方法：

```text
对每个 blocker 单独移除
观察 decision 是否翻转
再两两组合
```

输出：

```text
blocker_id
alone_changes_decision
with_others
dependency
criticality
```

验收：

```text
复盘日：
  L3-04 / L3-05 是核心
  L5-03 移除不翻转
  L4-03 仅在 conditional plan 下相关
```

工作量：M  
依赖：S-05

---

# 9. S-09 性能预算

- 证据：audit ≈ 158.7ms。
- 风险等级：中
- 归类：并入 P1
- 改动点：

```text
scripts/bench_audit.py
docs/PERFORMANCE.md
```

预算：

```text
单标的 30d 15m audit < 300ms
内存新增 < 50MB
单规则 < 50ms
```

每项 P1 完成记录：

```text
O-05..O-10
before_ms / after_ms
```

工作量：S  
依赖：无

---

# 10. S-10 文档与代码一致性

- 证据：新增 `HTF_SCORING.md`、`POI_SCOPE.md`，没有自动校验。
- 风险等级：中
- 归类：并入 P1
- 改动点：

```text
tests/test_doc_contract.py
docs/HTF_SCORING.md
docs/POI_SCOPE.md
```

检查：

```text
HTF_SCORING 的 0/3–3/3 与 _score_row 输出一致
POI_SCOPE 的 n-60、0.5 ATR 与 config 一致
```

验收：

```text
文档值改变但代码未改 → test fail
代码默认值改变但文档未改 → test fail
```

工作量：S  
依赖：S-01、S-06

---

# 11. 总表

| 编号 | 名称 | 风险 | 归类 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| S-01 | 评分边界规范 | 高 | P1 第 0 项 | S | 无 |
| S-02 | any-of 最小覆盖 | 中高 | 并入 P1 | M | O-03/04 |
| S-03 | provisional 语义 | 高 | P1 前必须补 | S | 无 |
| S-04 | shadow 基础设施 | 高 | P1 前必须补 | M | 无 |
| S-05 | 回归集 | 高 | 并入 P1（优先） | M | S-03/04 |
| S-06 | 魔法数字 | 中 | 并入 P1 | M | S-05 |
| S-07 | 预期纠错流程 | 高 | P1 前必须补 | S | 无 |
| S-08 | blocker 消融 | 中 | 并入 P1 | M | S-05 |
| S-09 | 性能预算 | 中 | 并入 P1 | S | 无 |
| S-10 | 文档一致性 | 中 | 并入 P1 | S | S-01/06 |

---

# 12. P1 前必须补

```text
S-03 provisional
S-04 shadow
S-07 预期变更流程
```

理由：

```text
S-03：GS-10 的 PASS 会污染 good_setup 与 shadow
S-04：没有 shadow 无法灰度 P1
S-07：P0 已经发生过一次预期修改，必须留痕
```

S-01 归类为 P1 第 0 项，而不是 P1 前：

```text
它不影响 P1 启动
但必须在 O-06 之前完成
否则 touch/delivery 会继承旧的相等边界
```

---

# 13. 更新后的 P1 顺序

```text
0. S-01 评分边界
1. S-03 provisional
2. S-04 shadow
3. O-06 touch vs delivery
4. O-07 L3-10 displacement
5. O-08 sequence
6. O-05 DOL 方向性
7. O-09 条件 plan audit
8. O-10 低成交概率
9. S-02 any-of 最小覆盖
10. S-05 回归集
11. S-06 敏感性
12. S-08 blocker 消融
13. S-09 性能
14. S-10 文档一致性
```

理由：

```text
S-01 先定边界
S-03/S-04 先保证审计与灰度
O-06 修复 GS-10 假 PASS
O-07/O-08 修 GS-04/05
O-05/O-09/O-10 修计划层
S-02 收紧 POI 质量
S-05/06/08/09/10 是质量加固
```

---

# 14. 回归集最小可行范围

```text
BTC 2026-10-30 趋势下跌
BTC 2026-10-29 高波动
BTC range day
US.MSFT 2026-10-29 财报缺口
HK.09988 2026-10-29 支撑
Synthetic tenkan=kijun
Synthetic 263 FVG / 0 relevant
Synthetic 16:00 session boundary
```

每条记录：

```text
case_id
rules_changed
expected_change
actual_change
consistent
```

---

# 15. 敏感性分析

先做两个：

```text
HTF eq_atr: 0.02 / 0.05 / 0.10
POI time: 30 / 60 / 90
POI price: 0.25 / 0.50 / 0.75 ATR
```

指标：

```text
L5-03 status
GS-06/15 status
relevant POI count
false PASS rate
missed POI rate
```

判定：

```text
如果 0.25/0.75 导致 GS-06 大面积翻转
说明 ±0.5 ATR 是敏感参数，需要更多样本
```

---

# 16. 文档-代码一致性

```text
tests/test_doc_contract.py
  assert HTF_SCORING 的 0/3–3/3 与 _score_row 一致
  assert POI_SCOPE 的 n-60/0.5 ATR 与 config 一致
```

失败条件：

```text
代码默认值改变但文档未改
文档阈值改变但代码未改
```

---

# 17. 最终 12 行

```text
P0 之后最该先补：S-03 provisional、S-04 shadow、S-07 预期变更流程。
S-03 防止 GS-10 的 PASS 污染 good_setup。
S-04 没有 shadow 就无法安全灰度 P1。
S-07 防止再次静默改预期。
S-01 不阻塞 P1 启动，但必须是 P1 第 0 项。
S-02/S-05/S-06/S-08/S-09/S-10 并入 P1。
回归集最小范围：BTC 趋势/高波动/震荡 + MSFT 财报缺口 + HK 支撑 + 3 个 synthetic。
先做两个敏感性：HTF eq_atr、POI ±0.5 ATR / n-60。
更新后的 P1 第一项：S-01 评分边界。
之后 S-03、S-04，再 O-06、O-07、O-08。
GS-10 在 O-06 前必须标 PASS(provisional)。
```
