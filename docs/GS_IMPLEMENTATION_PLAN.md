# GS 规则引擎未完成项实现方案

Last updated: 2026-10-30

## 0. 现有代码基线

| 文件 | 现有职责 | 与本方案关系 |
|---|---|---|
| `src/ict_trader/rules.py` | `RuleResult`、`_status`、`audit()`、`good_setup` | 新增规则并接入 GS |
| `scripts/audit_ict_rules.py` | 规则审计 CLI | 新增参数与错误码 |
| `src/ict_trader/cli.py` | `plan` 生成后调用 `audit()` | P0 规则接入 |
| `scripts/plot_ict_plotly.py` | Confluence 面板 | 新增字段展示 |
| `src/ict_trader/concepts.py` | `Swing/FVG/OrderBlock/CISD` | 新增 breaker/displacement 字段 |
| `src/ict_trader/indicators.py` | `resample_htf` | HTF PDA |
| `docs/GS_IMPLEMENTATION_SPEC.md` | 规则规格 | 本文的实现层 |

现有规则：

```text
L2-08 HTF PDA（基础版）
L2-09 RVOL（最后一根）
L2-10 SMT（基础版）
L3-07 OTE（基础版）
L3-09 fresh FVG（计数）
GS-01..GS-15
```

未完成项：

```text
P0：GS-09、GS-14、GS-04→L3-10、GS-05 sequence
P1：GS-10、GS-11、GS-12、GS-13、GS-15、GS-07/08
外部数据：session、第二标的、新闻、点差、consolidated volume、
          账户、滑点、dealing range
```

---

# 1. 共享接口与配置

## 1.1 新增配置

`config/rules.yaml`：

```yaml
version: gs_v2

session:
  default_market: us
  allow_outside_rth: false

breaker:
  close_buffer_atr: 0.10
  retest_buffer_atr: 0.05
  retest_window: 12

displacement:
  body_ratio_min: 0.60
  range_atr_min: 1.00
  close_beyond_atr: 0.10

sequence:
  mss_after_sweep_max: 12
  cisd_after_mss_max: 6

rvol:
  window: 20
  min: 1.50
  baseline: median

smt:
  min_corr: 0.70
  min_divergence_atr: 0.10

ote:
  low: 0.62
  high: 0.79

fresh_fvg:
  ce_distance_atr: 0.25
  min_score: 2

auto_plan:
  stop_buffer_atr: 0.10
  min_net_rr: 2.50
```

## 1.2 规则结果

继续使用：

```python
@dataclass
class RuleResult:
    rule_id: str
    layer: str
    name: str
    status: str          # PASS / FAIL / N/A
    blocking: bool
    value: Any = None
    reason: str = ""
```

新增：

```python
@dataclass
class RuleError:
    code: str            # DATA_MISSING / TIMEZONE_INVALID / NO_PLAN / ...
    message: str
    rule_id: str | None
```

## 1.3 错误码

```text
DATA_MISSING
TIMEZONE_INVALID
CALENDAR_MISSING
NO_PLAN
INSUFFICIENT_BARS
INVALID_OHLC
CORRELATION_TOO_LOW
NO_DOL
INVALID_ATR
```

---

# 2. P0

## 2.1 GS-09 Killzone / Session

### 类型 / 优先级 / 依赖

```text
类型：缺数据 + 缺逻辑
优先级：P0
依赖：config/markets.yaml、zoneinfo、OHLCV 的 date
规则：L5-01、L0-02
```

### 目标

回答：

```text
当前 bar 是否属于允许交易的 RTH / Killzone，session_id 是什么？
```

### 代码改动

新增：

```text
src/ict_trader/sessions.py
config/markets.yaml
tests/test_sessions.py
```

修改：

```text
src/ict_trader/rules.py       L5-01 调用 session_context
scripts/audit_ict_rules.py    新增 --session-config
src/ict_trader/cli.py         传入 market/timezone
```

新增函数：

```python
def load_market_config(path: str | Path) -> dict
def session_context(ts: pd.Timestamp, market: str,
                    config: dict) -> SessionInfo
def in_rth(ts: pd.Timestamp, market: str, config: dict) -> bool
def in_killzone(ts: pd.Timestamp, market: str, config: dict) -> str | None
```

返回：

```python
@dataclass
class SessionInfo:
    session_id: str | None
    in_rth: bool
    in_killzone: bool
    timezone: str
    local_time: pd.Timestamp | None
    utc_time: pd.Timestamp | None
    error: str | None
```

### 数据契约

`config/markets.yaml` 必须包含：

```yaml
markets:
  us:
    timezone: America/New_York
    rth: [["09:30", "16:00"]]
    killzones:
      london: ["02:00", "05:00"]
      ny_am: ["07:00", "10:00"]
      silver_bullet: ["10:00", "11:00"]
    holidays: []
    half_days: {}
  hk:
    timezone: Asia/Hong_Kong
    rth: [["09:30", "12:00"], ["13:00", "16:00"]]
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

校验：

```text
timezone 必须能被 zoneinfo 解析
rth/killzone 必须 HH:MM
start < end，跨日窗口单独标记
holidays 必须 YYYY-MM-DD
```

无数据：

```text
无 config → L5-01 = N/A
无 timezone → N/A
DST 不存在时间 → FAIL + TIMEZONE_INVALID
```

### 算法

```text
1. ts 是市场本地 naive time
2. ZoneInfo(market.timezone) 本地化
3. 不存在时间 → invalid
4. 歧义时间 → fold=0 + warning
5. 转 UTC
6. holidays → fail
7. half_days → 替换 RTH
8. RTH 判断
9. Killzone 判断
10. 生成 session_id
```

### 接口契约

输入：

```python
{
  "ts": "2026-10-30 10:00:00",
  "market": "us",
  "config": "config/markets.yaml"
}
```

输出：

```json
{
  "session_id": "us_ny_am",
  "in_rth": true,
  "in_killzone": true,
  "timezone": "America/New_York",
  "local_time": "2026-10-30T10:00:00-04:00",
  "utc_time": "2026-10-30T14:00:00Z",
  "error": null
}
```

### 验收

```text
PASS：US 10:00 ET → us_ny_am
FAIL：US 12:00 ET → in_killzone=false
FAIL：HK 12:30 HKT → in_rth=false
PASS：crypto 08:00 UTC → crypto_london
```

### 回归

```text
L5-01 从 N/A 变 PASS/FAIL
L0-02 保留，不与 L5-01 合并
```

### 风险

```text
需确认：US 数据是否 ET naive
需确认：HK 午休是否禁止交易
需确认：crypto 用 UTC 还是 exchange local
```

### 工作量

```text
M
可并行：与 GS-14 并行
```

---

## 2.2 GS-14 Breaker

### 类型 / 优先级

```text
类型：缺逻辑
优先级：P0
依赖：OrderBlock、ATR、OHLCV
规则：L3-08、GS-14
```

### 代码改动

新增：

```text
src/ict_trader/breakers.py
tests/test_breakers.py
```

修改：

```text
src/ict_trader/concepts.py   新增 Breaker dataclass
src/ict_trader/rules.py      L3-08 调用 detect_breakers
```

新增：

```python
def detect_breakers(df: pd.DataFrame,
                    obs: list[OrderBlock],
                    atr: float,
                    cfg: BreakerConfig) -> list[Breaker]
```

### 数据契约

```json
{
  "kind": "breaker",
  "origin_ob": {"direction": "bull", "top": 82.4, "bottom": 82.1, "created": 120},
  "failed_at": 138,
  "close_through": true,
  "retest_at": 144,
  "state": "confirmed"
}
```

### 状态机

```text
OB 存在
→ close 穿越 OB + 0.10 ATR
→ failed_at
→ 12 根内回踩
→ rejection close
→ confirmed
```

规则：

```text
bull OB: close < bottom - 0.10 ATR
bear OB: close > top + 0.10 ATR
bull breaker retest: low <= top, close > top + 0.05 ATR
bear breaker retest: high >= bottom, close < bottom - 0.05 ATR
```

### 验收

```text
PASS：bull OB 82.1–82.4，close 82.0，回踩 low 82.2 close 82.5
FAIL：只有影线跌破，close 82.3
FAIL：失效后 13 根才回踩 → candidate
```

### 回归

```text
L3-08 从 N/A 变为 PASS/FAIL
OB 规则不变，只增加 breaker 分类
```

### 风险

```text
需确认：回踩窗口是否按品种调整
需确认：close 用 close 还是 body close
```

### 工作量

```text
M
可并行：与 GS-09 并行
```

---

## 2.3 GS-04 → L3-10 Displacement quality

### 类型 / 优先级

```text
类型：映射需重写
优先级：P0
依赖：OHLCV、ATR、swing
规则：GS-04、L3-10（新增）
```

### 代码改动

新增：

```text
src/ict_trader/displacement.py
tests/test_displacement.py
```

修改：

```text
src/ict_trader/rules.py  新增 L3-10，GS-04 指向 L3-10
```

新增：

```python
def displacement_quality(df: pd.DataFrame, i: int, atr: float,
                         cfg: DisplacementConfig) -> DisplacementResult
```

### 计算

```text
body_ratio = abs(close-open) / (high-low)
range_atr = (high-low) / ATR
close_beyond = close 越过最近 swing 的幅度 / ATR
follow_through = 下一根未收回 50%
```

默认：

```text
body_ratio >= 0.60
range_atr >= 1.00
close_beyond >= 0.10
follow_through = true
```

### 与 L3-04 关系

```text
L3-04 CISD = 收盘穿过结构
L3-10 displacement = 位移质量
两者并列，不互相替代
GS-04 → L3-10
```

### 验收

```text
PASS：1.4 ATR、body 0.72、越过 swing 0.2 ATR
FAIL：0.8 ATR
边界：1.0 ATR、body 0.60、无 follow-through → weak/FAIL
```

### 工作量

```text
S
必须串行：先于 GS-05 sequence
```

---

## 2.4 GS-05 sequence_id

### 类型 / 优先级

```text
类型：映射需重写
优先级：P0
依赖：sweep、MSS、CISD、entry
规则：GS-05、L1-04、L3-05
```

### 代码改动

新增：

```text
src/ict_trader/sequence.py
tests/test_sequence.py
```

修改：

```text
src/ict_trader/rules.py  L3-05 使用 sequence
```

新增：

```python
@dataclass
class SetupSequence:
    sequence_id: str
    sweep_bar: int
    mss_bar: int | None
    cisd_bar: int | None
    entry_bar: int | None
    valid: bool
    reason: str

def build_sequence(sweeps, events, cisds, entry_bar: int,
                   cfg: SequenceConfig) -> SetupSequence
```

### 约束

```text
sweep_bar < mss_bar <= cisd_bar <= entry_bar
mss_bar - sweep_bar <= 12
cisd_bar - mss_bar <= 6
```

### 验收

```text
PASS：sweep 123 → MSS 130 → CISD 132 → entry 134
FAIL：MSS 在 sweep 前
边界：mss 间隔 13 → FAIL
```

### 工作量

```text
M
必须串行：L3-10 → sequence
```

---

# 3. P1

## 3.1 GS-10 HTF PDA delivery

### 类型

```text
部分实现
```

### 代码改动

新增：

```text
src/ict_trader/htf_pda.py
tests/test_htf_pda.py
```

修改：

```text
src/ict_trader/rules.py L2-08
```

新增：

```python
def htf_pda_delivery(df, daily, atr, cfg) -> HtfPdaResult
```

### 判定

```text
touch：进入 PD Array ±0.25 ATR
delivery：touch + HTF CISD 同向 + 离开区域 >= 0.5 ATR
1D > 4H > 1H
```

### 验收

```text
PASS：4H bearish FVG + HTF CISD + close 离开 0.5 ATR
FAIL：只触碰，无 CISD
FAIL：反向突破
```

### 工作量

```text
M
可并行：RVOL
```

---

## 3.2 GS-11 RVOL

### 类型

```text
部分实现
```

### 代码改动

新增：

```text
src/ict_trader/volume.py
tests/test_rvol.py
```

修改：

```text
L2-09
```

新增：

```python
def rvol_at(df: pd.DataFrame, bar: int, window: int = 20,
            min_rvol: float = 1.5) -> RvolResult
```

### 规则

```text
event_volume = sweep_bar 或 cisd.end 的 volume
baseline = 前 window 根 median
rvol = event_volume / baseline
```

### 验收

```text
PASS：2.0M / 1.0M = 2.0
FAIL：1.1M / 1.0M
边界：venue volume + rvol 1.6 → PASS with warning
```

### 工作量

```text
S
```

---

## 3.3 GS-12 SMT

### 类型

```text
缺数据 + 部分实现
```

### 代码改动

新增：

```text
src/ict_trader/smt.py
config/smt_pairs.yaml
tests/test_smt.py
```

修改：

```text
L2-10
scripts/audit_ict_rules.py --smt-csv
```

新增：

```python
def smt_divergence(primary: pd.DataFrame, secondary: pd.DataFrame,
                   cfg: SmtConfig) -> SmtResult
```

### 数据契约

```csv
date,open,high,low,close,volume
```

配对：

```yaml
pairs:
  - primary: BTCUSDT
    secondary: ETHUSDT
  - primary: ES
    secondary: NQ
  - primary: XAUUSD
    secondary: XAGUSD
```

### 验收

```text
PASS：BTC HH，ETH 未 HH，corr 0.85
FAIL：两者 HH
N/A：corr 0.45
```

### 工作量

```text
M
必须等数据
```

---

## 3.4 GS-13 OTE

### 类型

```text
部分实现
```

### 代码改动

新增：

```text
src/ict_trader/ote.py
tests/test_ote.py
```

修改：

```text
L3-07
```

新增：

```python
def ote_quality(df, swings, entry: float, direction: str,
                cfg: OteConfig) -> OteResult
```

### 规则

```text
impulse leg >= 1.0 ATR
retracement 0.62–0.79
与 FVG/OB 重叠
无 plan → N/A
```

### 验收

```text
PASS：100→110，entry 103，FVG overlap
FAIL：entry 105（50%）
N/A：无 plan
```

### 工作量

```text
S
```

---

## 3.5 GS-15 Fresh FVG

### 类型

```text
部分实现
```

### 代码改动

新增：

```text
src/ict_trader/freshness.py
tests/test_freshness.py
```

修改：

```text
L3-09
```

新增：

```python
def fresh_fvg_score(fvg, df, entry: float, atr: float,
                    cfg: FreshConfig) -> FreshResult
```

### 评分

```text
first_presented +1
fresh +1
ce_distance_atr <= 0.25 +1
score >= 2 → PASS
```

### 验收

```text
PASS：first + fresh + CE 0.1 → 3
FAIL：touched 2 + CE 0.5 → 0
边界：fresh + CE 0.3 → 2 → PASS
```

### 工作量

```text
S
```

---

## 3.6 GS-07/08 自动 stop/target

### 类型

```text
缺逻辑
```

### 代码改动

新增：

```text
src/ict_trader/auto_plan.py
tests/test_auto_plan.py
```

修改：

```text
L4-01 / L4-03
```

新增：

```python
def auto_stop(direction, entry, sweep, ob, atr) -> float
def auto_target(direction, entry, levels) -> float | None
def auto_net_rr(...) -> tuple[float, float]
```

### 规则

```text
long stop = min(sweep.low, ob.bottom) - 0.10 ATR
short stop = max(sweep.high, ob.top) + 0.10 ATR
target = 最近未扫 DOL
net RR >= 2.5
```

### 验收

```text
PASS：stop 79.9，target 82.0，net 2.8
FAIL：target 81.0，net 1.8
N/A：无 DOL
```

### 工作量

```text
M
必须串行：sequence → auto_plan
```

---

# 4. 外部数据

| 数据 | Schema | 接入 | 影响 | 无数据 |
|---|---|---|---|---|
| session calendar | `markets.yaml` | 本地 | L5-01 | N/A |
| 第二标的 | OHLCV CSV | `--smt-csv` | L2-10 | N/A |
| 新闻日历 | `timestamp_utc,event,impact` | CSV/API | L0-03/L5-02 | N/A |
| 点差 | `timestamp,bid,ask` | 券商 API | L0-04 | N/A |
| consolidated volume | `volume,source` | 数据商 | L2-09 | venue + warning |
| 账户/仓位 | `equity,risk_pct` | 账户 API | L4-05 | N/A |
| 执行滑点 | `fill_price,ts` | 成交回报 | L4-06 | 2bps 保守 |
| dealing range | `start,end` | 人工/算法 | L3-07 | 自动 swing |

---

# 5. 排期

## 5.1 P0 最小闭环

```text
1. sessions.py + markets.yaml
2. breakers.py
3. displacement.py + L3-10
4. sequence.py
5. rules.audit 接入
6. 回归测试
```

前置关系：

```text
sessions ─┐
breakers ─┼─→ audit 接入
displacement → sequence ─┘
```

## 5.2 任务表

| 缺口 | 类型 | 优先级 | 依赖 | 工作量 | 并行 | 验收角色 |
|---|---|---|---|---|---|---|
| GS-09 session | 数据+逻辑 | P0 | 时区/日历 | M | 与 breaker | 数据工程 + 规则 |
| GS-14 breaker | 逻辑 | P0 | OB/ATR | M | 与 session | 规则工程 |
| L3-10 displacement | 映射 | P0 | OHLCV | S | 与 breaker | 规则工程 |
| GS-05 sequence | 映射 | P0 | sweep/MSS/CISD | M | 需 displacement | 规则工程 |
| GS-10 HTF PDA | 部分 | P1 | 1H/4H/1D | M | 与 RVOL | 规则工程 |
| GS-11 RVOL | 部分 | P1 | volume | S | 与 HTF | 数据工程 |
| GS-12 SMT | 数据 | P1 | 第二标的 | M | 等数据 | 数据工程 |
| GS-13 OTE | 部分 | P1 | swing/plan | S | 与 fresh | 规则工程 |
| GS-15 fresh | 部分 | P1 | FVG 元数据 | S | 与 OTE | 规则工程 |
| GS-07/08 auto | 逻辑 | P1 | plan/DOL | M | 需 sequence | 规则工程 |

## 5.3 里程碑

```text
M1：P0 四项 + 回归测试
M2：P1 数据类（SMT、session 扩展）
M3：P1 逻辑类（HTF delivery、fresh scoring、auto stop/target）
```

---

# 6. 测试与回滚

## 6.1 测试

```text
单元：每个函数独立 fixture
构造：PASS/FAIL/边界三类
快照：固定 OHLCV 样本
shadow：新旧 audit 并行
基线：保留旧 JSON
```

## 6.2 shadow mode

新增 CLI：

```text
--rule-mode strict | shadow
```

shadow：

```text
新规则只记录，不阻断
输出 old_decision / new_decision / diff
```

观察：

```text
PASS→FAIL 数量
FAIL→PASS 数量
blocking_failures 分布
人工复核 100 例
```

## 6.3 回滚

```text
config/rules.yaml version
--rule-mode strict 回退
新增规则默认 warning，稳定后 blocking
保留旧 audit JSON 字段
```

---

# 7. 十行总结

```text
P0 先做：GS-09 session、GS-14 breaker、L3-10 displacement、GS-05 sequence。
GS-09 与 GS-14 可并行；displacement 必须先于 sequence。
缺数据：session 日历、第二标的、新闻、点差、consolidated volume、账户、滑点。
缺逻辑：breaker、displacement、sequence、auto stop/target。
部分实现：HTF PDA 只有 touch、RVOL 只看最后一根、OTE leg 不严、fresh 只有计数。
映射重写：GS-04 → L3-10，GS-05 → sequence_id。
P1 先做数据类：SMT 与 session 扩展；再做逻辑类。
所有 N/A 保留，shadow mode 不阻断，稳定后再 strict。
```
