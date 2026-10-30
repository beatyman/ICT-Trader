# GS 规则引擎深化实现规格

Last updated: 2026-10-30

## 0. 结论先行

`GS_IMPLEMENTATION_PLAN.md` 已到“模块 + 函数 + 里程碑”级，但还不能直接编码，主要缺：

```text
1. 配置格式与加载器未定（YAML vs JSON；pyproject 里没有 PyYAML）
2. 规则注册/执行顺序仍在 audit() 单体里
3. audit JSON 没有 schema_version / rules_version / rule_mode
4. 错误码只在文档里，代码没有统一异常层
5. P0 任务的 DoD 仍偏“功能级”，没到“断言级”
6. session 的真实时区口径未确认
7. breaker / sequence / HTF delivery 的边界语义未定死
8. shadow mode 没有 old_decision 的稳定来源
```

本文件把这些补到“开发者拿到就能写、测试拿到就能验”的粒度。

---

# 1. 横切设施（必须先于业务规则）

## 1.1 XC-01 配置加载

### 决策

现有 `pyproject.toml` 依赖只有：

```text
numpy
pandas
matplotlib
```

没有 PyYAML，也没有 pydantic。  
因此：

```text
默认配置文件用 JSON
YAML 只作为可选输入，需额外装 pyyaml
```

新增：

```text
src/ict_trader/config.py
config/rules.json
config/markets.json
config/smt_pairs.json
tests/test_config.py
```

### 加载顺序

```text
1. 内置默认值（代码常量）
2. config/*.json
3. CLI 显式参数
4. 环境变量（仅 debug/CI，不允许覆盖生产阈值）
```

覆盖规则：

```text
标量：后者覆盖前者
list：整体替换，不合并
dict：浅合并，显式 null 表示删除
```

### schema 校验

```python
def load_rules_config(path: str | Path | None = None) -> dict
def validate_config(obj: dict, schema: dict) -> list[str]
```

错误：

```text
E_CONFIG_MISSING
E_CONFIG_SCHEMA
E_CONFIG_TYPE
E_CONFIG_RANGE
```

校验：

```text
version 必填，semver 字符串
所有 ATR 阈值 > 0
rvol window >= 5
retest_window >= 1
timezone 能被 zoneinfo 解析
killzone start/end 为 HH:MM
```

### 版本

```json
{
  "version": "gs_v2",
  "schema_version": "2.0",
  "thresholds": {
    "displacement": {"body_ratio_min": 0.60, "range_atr_min": 1.0},
    "breaker": {"close_buffer_atr": 0.10, "retest_window": 12},
    "sequence": {"mss_after_sweep_max": 12, "cisd_after_mss_max": 6},
    "rvol": {"window": 20, "min": 1.5},
    "smt": {"min_corr": 0.70, "min_divergence_atr": 0.10},
    "ote": {"low": 0.62, "high": 0.79},
    "fresh_fvg": {"ce_distance_atr": 0.25, "min_score": 2},
    "auto_plan": {"stop_buffer_atr": 0.10, "min_net_rr": 2.5}
  }
}
```

迁移：

```text
gs_v1 → gs_v2：新增 thresholds；旧字段保留
未知字段：warning，不 fail
缺失字段：用默认值 + warning
```

### 任务

| ID | 任务 | 输入 | 输出 | 估算 | 依赖 | DoD |
|---|---|---|---|---|---|---|
| XC-01a | 默认配置常量 | 无 | dict | S | 无 | 所有默认值有单测 |
| XC-01b | JSON loader | path | dict | S | XC-01a | 缺失/非法 JSON 返回 E_CONFIG_* |
| XC-01c | schema validator | dict | errors | S | XC-01b | 每个 error 有测试 |
| XC-01d | CLI 接入 | args | config | S | XC-01c | `--config` 可覆盖默认 |

---

## 1.2 XC-02 规则注册与执行顺序

### 现状

`rules.audit()` 是单体顺序代码，新增规则容易破坏顺序。

### 目标

```python
@dataclass
class RuleContext:
    df: pd.DataFrame
    daily: pd.DataFrame | None
    result: ICTResult
    plan: dict | None
    atr: float
    config: dict
    market: str

RuleFn = Callable[[RuleContext], RuleResult]

@dataclass
class RuleSpec:
    rule_id: str
    layer: str
    fn: RuleFn
    blocking: bool
    order: int
```

注册：

```python
RULES: list[RuleSpec] = [
    RuleSpec("L0-01", "L0", check_data, True, 10),
    ...
]
```

执行：

```python
def run_rules(ctx: RuleContext, mode: str = "strict") -> list[RuleResult]:
    for spec in sorted(RULES, key=lambda x: (x.layer, x.order)):
        ...
```

顺序：

```text
L0 → L1 → L2 → L3 → L5 → L4
```

说明：L4 最后执行，但 L4-03 净 RR 是下单前 veto，不能因为顺序最后而失效。

冲突：

```text
同一 rule_id 不允许重复
同一层 order 重复 → E_RULE_ORDER
```

### 任务

| ID | 任务 | 估算 | DoD |
|---|---|---|---|
| XC-02a | 定义 RuleContext/RuleSpec | S | mypy/运行时可构造 |
| XC-02b | 拆分现有 audit 为函数 | M | 旧输出字段不变 |
| XC-02c | 注册表 + 顺序校验 | S | 重复/缺序测试 |
| XC-02d | 规则开关 | S | config 可禁用单条规则 |

---

## 1.3 XC-03 rule-mode strict / shadow

新增 CLI：

```bash
--rule-mode strict|shadow
```

签名：

```python
def audit(df, daily=None, market="crypto", plan=None, smt_df=None,
          mode="strict", config=None) -> dict
```

strict：

```text
blocking fail → decision=NO_TRADE
```

shadow：

```text
new_decision = strict 结果
old_decision = plan 存在 ? "TRADE" : "NO_TRADE"
decision = old_decision
```

输出：

```json
"shadow": {
  "old_decision": "TRADE",
  "new_decision": "NO_TRADE",
  "diff": ["L2-04", "L5-03"],
  "would_block": true
}
```

无法产出 diff 的情况：

```text
没有 old plan → old_decision=NO_TRADE
旧 audit 不存在 → 用 plan 的 decision 作为 old
```

切换 strict 的条件：

```text
shadow 至少 2 周
>= 100 个 setup
PASS→FAIL 的原因可解释
人工复核 >= 30 例
无 golden case 回退
```

### 任务

| ID | 任务 | 估算 | DoD |
|---|---|---|---|
| XC-03a | mode 参数 | S | strict/shadow 单测 |
| XC-03b | shadow schema | S | JSON 字段稳定 |
| XC-03c | 切换阈值文档 | S | 评审通过 |

---

## 1.4 XC-04 audit JSON schema

顶层：

```json
{
  "schema_version": "2.0",
  "rules_version": "gs_v2",
  "rule_mode": "strict",
  "generated_at": "2026-10-30T12:00:00Z",
  "decision": "NO_TRADE",
  "good_setup": {"decision": "FAIL", "failed": [], "incomplete": []},
  "blocking_failures": [],
  "rules": [],
  "shadow": null,
  "errors": []
}
```

单条规则：

```json
{
  "rule_id": "L2-04",
  "layer": "L2",
  "name": "liquidity sweep",
  "status": "FAIL",
  "blocking": true,
  "value": {"bars_since": 31},
  "reason": "no sweep within 20 bars",
  "evidence": {"bars": [100, 120], "price": 0.0}
}
```

兼容：

```text
旧消费者只读 decision / blocking_failures / rules
新增字段可选
rules[] 的旧字段保留
```

### 任务

| ID | 任务 | 估算 | DoD |
|---|---|---|---|
| XC-04a | 定义 schema | S | JSON schema 文件 |
| XC-04b | 输出版本字段 | S | 单测 |
| XC-04c | 兼容旧字段 | S | 旧解析器测试 |

---

## 1.5 XC-05 错误码与降级

统一：

```text
E_CONFIG_MISSING
E_CONFIG_SCHEMA
E_CONFIG_TYPE
E_TZ_INVALID
E_CALENDAR_MISSING
E_ATR_ZERO
E_INSUFFICIENT_BARS
E_DATA_GAP
E_NO_PLAN
E_NO_DOL
E_CORR_LOW
E_RULE_ORDER
E_INTERNAL
```

策略：

| 错误 | 策略 | 结果 |
|---|---|---|
| E_CONFIG_MISSING | 用默认值 + warning | 可继续 |
| E_CONFIG_SCHEMA | 抛异常 | 启动失败 |
| E_TZ_INVALID | N/A | L5-01 N/A |
| E_ATR_ZERO | 用 price*0.005 保守值 + warning | 继续 |
| E_INSUFFICIENT_BARS | N/A | 规则 N/A |
| E_NO_PLAN | N/A | L4 N/A |
| E_NO_DOL | FAIL | GS-02 FAIL |
| E_CORR_LOW | N/A | GS-12 N/A |

---

## 1.6 XC-06 性能与测试基础

### 性能预算

```text
30 天 15m = 2880 bars
config load < 10ms
session 计算：向量化，< 20ms
audit 新增规则：< 100ms
总 audit（不含 analyze）< 300ms
内存新增 < 50MB
```

复杂度：

```text
所有新增模块 O(n)
禁止 O(n²) 的 breaker/FVG 全表两两比较
```

### 测试夹具

```text
tests/fixtures/
  ohlcv_15m.csv
  ohlcv_1d.csv
  markets.json
  rules.json
tests/golden/
  gs_v2_audit.json
```

约定：

```text
固定 UTC 时间戳
固定 ATR
固定随机种子
不依赖网络
```

---

# 2. P0 模块深化

## 2.1 GS-09 sessions.py

### 里程碑 / DoD

```text
M1
DoD：给定任何 market+timestamp，返回确定 session_id 或 E_TZ_*，四个验收用例全过。
```

### 任务

| ID | 任务 | 输入 | 输出 | 估算 | 依赖 | DoD |
|---|---|---|---|---|---|---|
| GS09-T1 | `markets.json` | 无 | 配置 | S | XC-01 | schema 通过 |
| GS09-T2 | `SessionInfo` dataclass | 无 | 类型 | S | 无 | 字段齐全 |
| GS09-T3 | `load_market_config` | path | dict | S | T1 | 缺失/非法测试 |
| GS09-T4 | `session_context` | ts, market, cfg | SessionInfo | M | T2/T3 | 四用例 |
| GS09-T5 | 接入 L5-01 | ctx | RuleResult | S | T4 | audit 出现 session_id |
| GS09-T6 | 测试 | fixtures | pass | S | T4 | 4+2 边界 |

### 数据结构

```python
@dataclass
class SessionInfo:
    session_id: str | None
    in_rth: bool
    in_killzone: bool
    timezone: str
    local_time: pd.Timestamp | None
    utc_time: pd.Timestamp | None
    error: str | None = None
```

### 函数

```python
def session_context(ts: pd.Timestamp, market: str, cfg: dict) -> SessionInfo:
    ...
```

步骤：

```text
1. 从 cfg 取 market 配置
2. ZoneInfo(timezone)
3. 本地化 naive ts
4. DST 不存在 → E_TZ_INVALID
5. 转 UTC
6. holidays/half_days
7. RTH 判断
8. Killzone 判断
9. 生成 session_id
```

边界：

```text
16:00 bar 属于 RTH
HK 12:30 午休
DST 缺失时间
跨日 KZ
```

### 配置

```json
{
  "us": {
    "timezone": "America/New_York",
    "rth": [["09:30", "16:00"]],
    "killzones": {
      "london": ["02:00", "05:00"],
      "ny_am": ["07:00", "10:00"],
      "silver_bullet": ["10:00", "11:00"]
    }
  }
}
```

### 测试

```text
US 10:00 → us_ny_am PASS
US 12:00 → us_rth, killzone=false
HK 12:30 → in_rth=false
crypto 08:00 → crypto_london
DST 2026-03-08 02:30 → E_TZ_INVALID
holiday → in_rth=false
```

### 观测

audit 新增：

```json
"session": {"session_id": "...", "in_rth": true, "in_killzone": true}
```

---

## 2.2 GS-14 breakers.py

### DoD

```text
给定 OB 序列，返回 confirmed/candidate/invalid；三类验收全过。
```

### 任务

| ID | 任务 | 估算 | 依赖 | DoD |
|---|---|---|---|---|
| GS14-T1 | `Breaker` dataclass | S | 无 | 字段齐全 |
| GS14-T2 | `detect_breakers` 失效扫描 | M | T1 | 收盘阈值测试 |
| GS14-T3 | 回踩状态机 | M | T2 | 窗口测试 |
| GS14-T4 | 接入 L3-08 | S | T3 | audit 字段 |
| GS14-T5 | 测试 | S | T3 | PASS/FAIL/candidate |

### 数据结构

```python
@dataclass
class Breaker:
    kind: str
    origin_ob: dict
    failed_at: int
    close_through: bool
    retest_at: int | None
    state: str  # candidate | confirmed | invalid
```

### 函数

```python
def detect_breakers(df, obs, atr, cfg) -> list[Breaker]:
    ...
```

步骤：

```text
1. 遍历 OB
2. bull: close < bottom - 0.10 ATR
   bear: close > top + 0.10 ATR
3. 记录 failed_at
4. 12 根内进入 OB
5. rejection close
6. confirmed
```

边界：

```text
影线不算
ATR=0 → E_ATR_ZERO
跨日不影响
```

---

## 2.3 L3-10 displacement.py

### DoD

```text
给定 bar i，返回 body_ratio/range_atr/close_beyond/follow_through，并给出 PASS/FAIL。
```

### 任务

| ID | 任务 | 估算 | 依赖 | DoD |
|---|---|---|---|---|
| L310-T1 | `DisplacementResult` | S | 无 | 字段 |
| L310-T2 | `displacement_quality` | S | T1 | 公式测试 |
| L310-T3 | 接入 L3-10 | S | T2 | GS-04 指向 |
| L310-T4 | 测试 | S | T2 | 3 用例 |

### 公式

```text
body_ratio = abs(close-open)/(high-low)
range_atr = (high-low)/ATR
close_beyond = (close-swing)/ATR
follow_through = next close 未收回 50%
```

默认：

```text
body_ratio >= 0.60
range_atr >= 1.00
close_beyond >= 0.10
```

### 与 L3-04

```text
L3-04 仍是 CISD
L3-10 是 displacement quality
GS-04 → L3-10
```

---

## 2.4 GS-05 sequence.py

### DoD

```text
给定 sweep/MSS/CISD/entry，返回 sequence_id 和 valid；跨序列 FAIL。
```

### 任务

| ID | 任务 | 估算 | 依赖 | DoD |
|---|---|---|---|---|
| GS05-T1 | `SetupSequence` | S | 无 | 字段 |
| GS05-T2 | `build_sequence` | M | L310 | 顺序约束 |
| GS05-T3 | 接入 L3-05 | S | T2 | audit 字段 |
| GS05-T4 | 测试 | S | T2 | 3 用例 |

### 约束

```text
sweep_bar < mss_bar <= cisd_bar <= entry_bar
mss - sweep <= 12
cisd - mss <= 6
```

### 输出

```json
{
  "sequence_id": "sweep_123",
  "sweep_bar": 123,
  "mss_bar": 130,
  "cisd_bar": 132,
  "entry_bar": 134,
  "valid": true,
  "reason": ""
}
```

---

## 2.5 audit 接入

### 任务

| ID | 任务 | 估算 | DoD |
|---|---|---|---|
| AUD-T1 | `audit(..., mode, config)` | M | 旧字段不变 |
| AUD-T2 | GS 映射更新 | S | GS-04/05 指向 |
| AUD-T3 | shadow 输出 | S | old/new/diff |
| AUD-T4 | 回归基线 | M | 100 例对比 |

### 回归

```text
旧版 audit JSON 存 tests/golden/gs_v1_audit.json
新版输出 diff：
  decision
  blocking_failures
  rules status
```

---

# 3. P1 模块深化

## 3.1 GS-10 htf_pda.py

```text
DoD：给定 1D/4H/1H，返回 touch/delivery/direction/htf_cisd。
任务：T1 1D 数据；T2 touch；T3 delivery；T4 接入 L2-08；T5 测试。
估算：M
依赖：resample_htf, analyze
```

## 3.2 GS-11 volume.py

```text
DoD：给定 event_bar，返回 rvol/baseline/volume_source。
任务：T1 event selector；T2 median baseline；T3 配置；T4 接入 L2-09。
估算：S
```

## 3.3 GS-12 smt.py

```text
DoD：给定第二标的，返回 corr/divergence/strength。
任务：T1 CSV loader；T2 对齐；T3 corr；T4 swing；T5 接入 L2-10。
估算：M
依赖：第二标的 CSV
```

## 3.4 GS-13 ote.py

```text
DoD：给定 impulse leg + entry，返回 retracement/overlap/valid。
任务：T1 leg；T2 range；T3 overlap；T4 接入 L3-07。
估算：S
```

## 3.5 GS-15 freshness.py

```text
DoD：给定 FVG/entry，返回 first_presented/touch_count/ce_distance/score。
任务：T1 metadata；T2 score；T3 接入 L3-09。
估算：S
```

## 3.6 GS-07/08 auto_plan.py

```text
DoD：无 plan 时生成 stop/target/net_rr，或明确 E_NO_DOL。
任务：T1 stop；T2 target；T3 net RR；T4 接入 L4-01/03。
估算：M
```

---

# 4. 汇总任务总表

| 任务ID | 模块 | 里程碑 | 依赖 | 估算 | DoD |
|---|---|---|---|---|---|
| XC-01 | config | M1 前置 | 无 | M | 配置可加载/校验 |
| XC-02 | registry | M1 前置 | XC-01 | M | 顺序可测 |
| XC-03 | rule-mode | M1 前置 | XC-02 | S | strict/shadow |
| XC-04 | audit schema | M1 前置 | XC-02 | S | 版本字段 |
| XC-05 | errors | M1 前置 | 无 | S | 错误码表 |
| GS09-T1..T6 | sessions | M1 | XC-01 | M | 4 用例 |
| GS14-T1..T5 | breakers | M1 | XC-02 | M | 3 用例 |
| L310-T1..T4 | displacement | M1 | XC-02 | S | 3 用例 |
| GS05-T1..T4 | sequence | M1 | L310 | M | 3 用例 |
| AUD-T1..T4 | audit | M1 | 全部 P0 | M | 回归 |
| GS10-T1..T5 | htf_pda | M3 | 1D/4H | M | delivery 用例 |
| GS11-T1..T4 | volume | M2 | volume | S | rvol 用例 |
| GS12-T1..T5 | smt | M2 | 第二标的 | M | corr/divergence |
| GS13-T1..T4 | ote | M3 | plan | S | OTE 用例 |
| GS15-T1..T3 | freshness | M3 | FVG 元数据 | S | score 用例 |
| AP-T1..T4 | auto_plan | M3 | sequence | M | stop/target |

---

# 5. P0 编码顺序

```text
第 0 步（横切）：
  XC-01 config loader
  XC-05 errors

第 1 步（可并行）：
  GS09 sessions
  GS14 breakers

第 2 步：
  L310 displacement

第 3 步：
  GS05 sequence（依赖 L310）

第 4 步：
  AUD 接入 + shadow + 回归

可并行：GS09 与 GS14
必须串行：L310 → GS05 → AUD
```

---

# 6. 测试计划

## 单元

```text
每个函数 ≥ 3 用例
固定 fixture
断言具体字段，不只 assert True
```

## 集成

```text
session + audit
breaker + audit
displacement + sequence
shadow vs strict
```

## 验收

```text
每个缺口至少 PASS / FAIL / 边界
用例写入 tests/test_gs_acceptance.py
```

## 回归

```text
旧版 baseline 快照
新旧 diff
PASS→FAIL 人工复核
```

---

# 7. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 时区口径不一致 | GS-09 全错 | 数据契约 + DST 测试 |
| Breaker 过拟合 | 假 breaker | 固定窗口 + shadow |
| Sequence 依赖顺序 | 误杀好 setup | 先 shadow |
| 第二标的数据质量 | SMT 假信号 | 相关性 + 对齐检查 |
| Volume 口径 | RVOL 偏差 | volume_source + warning |
| 规则版本漂移 | 回测不可复现 | rules_version + golden |

---

# 8. 缺口自检清单

### 1. 哪些函数只有签名、没有内部步骤？

```text
detect_breakers
build_sequence
htf_pda_delivery
smt_divergence
auto_plan
```

影响：开发者仍需自行设计。  
补齐：本文件已给步骤；仍需把伪代码落到 `GS_IMPLEMENTATION_SPEC.md` 的对应小节。  
角色：规则工程。

### 2. 哪些数据结构字段未定类型/单位？

```text
Breaker.origin_ob：dict 还是 OrderBlock
HtfPdaResult.direction：bull/bear 还是 long/short
SmtResult.strength：A/B/C 还是 numeric
```

建议：

```text
origin_ob 用 dict（可 JSON）
direction 统一 long/short
strength 用 0/1/2
```

### 3. 哪些阈值没有可配置路径？

```text
RVOL 1.5
SMT 0.70
OTE 0.62/0.79
fresh CE 0.25
```

补齐：全部进 `config/rules.json`。

### 4. 哪些验收用例无法用现有数据构造？

```text
HK 半日市
US DST 不存在时间
真实 SMT 背离
consolidated volume
```

补齐：fixture 构造 + 数据方提供。

### 5. 哪些规则在 shadow 下无法产出 diff？

```text
没有旧 audit 的新规则
没有 plan 的 L4
```

补齐：`old_decision` 用 plan.decision 或 `NO_TRADE`。

### 6. 哪些外部数据缺口没有明确降级？

```text
consolidated volume
dealing range 标注
执行滑点
```

补齐：见外部数据表。

### 7. 哪些模块之间的执行顺序未在代码层落实？

```text
L310 → GS05
L4-03 veto 在 L5 之后
```

补齐：`RULES` 注册表 order。

### 8. 哪些错误路径没有错误码？

```text
breaker retest 窗口无效
session half_day 配置缺失
SMT 日期无法对齐
```

补齐：加入错误码表。

### 9. 哪些配置项没有 schema 校验？

```text
markets.killzones
smt_pairs.pairs
```

补齐：`validate_config`。

### 10. 哪些任务无法估算？

```text
SMT 多标的扩展
real consolidated volume
```

补齐：先做单配对，再扩展。

---

# 9. 最终 15 行总结

```text
1. 方案仍差横切设施：config loader、rule registry、audit schema、rule-mode、错误码。
2. P0 先做 XC-01/05，再做 GS-09 与 GS-14（可并行）。
3. 然后做 L3-10 displacement。
4. 再做 GS-05 sequence，必须依赖 L3-10。
5. 最后接 audit + shadow + 回归。
6. GS-09 缺时区/session 数据，不能只加 if。
7. GS-14 缺 breaker 状态机，不能只看 mitigated。
8. GS-04 要新增 L3-10，不要继续映射 CISD。
9. GS-05 要 sequence_id，不要只看最近 50 根。
10. GS-10/11/13/15 是部分实现，先补 delivery/event bar/leg/score。
11. GS-12 需要第二标的 CSV，先做 BTC/ETH 单配对。
12. GS-07/08 需要 auto stop/target，否则无 plan 永远 N/A。
13. 所有新规则先 shadow，不阻断。
14. 旧 audit JSON 必须保留，golden 快照必须更新。
15. 最小行动序列：config/errors → sessions/breakers → L3-10 → sequence → audit/shadow。
```
