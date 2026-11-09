# 信号 → 共振 → 规则决策 → Setup → 异步 AI → TP/SL 流水线

> 现状：**AI 仍只是按需人工 review，不是决策流水线**（`realtime/ai.py::DeepSeekReviewer`）。
> 已实现的只有左侧数据链路与 ICT 一条垂直切片：
> `StreamSession → ICTStrategyRunner → PaperBroker → signals/paper_trades`。
> 工作区已有 `EngineSignal` 契约、`EngineRegistry`、`AnalyticsBatchWriter`
> 与 9 张 analytics 表，但**尚未接线**（registry 没被 `RealtimeEngine` 持有，
> `setups/tp_sl_events/ai_decisions/...` 没有写入方）。本文用「已实现 / 待实现」标注每一段。

## 1. 端到端数据流（目标态）

```text
Binance WS ─► feed.dispatch ─► StreamSession.queue ─► _handle_closed
                                                        │
                                    GapDetector.observe ─┤ gap? ─► REST backfill/replay (不交易)
                                                        ▼
                        _process_closed: redis.store_closed + ch.enqueue(bar)
                                                        │
                     ┌──────────────────────────────────┴─────────────────────────┐
                     ▼ (现有 ICT 切片)                                            ▼ (目标)
        runner.on_closed ─► to_thread(_compute)                    ComputeSupervisor
              analyze()+build_setup() ─► TradeSignal[]               registry.analyze_all（线程池，每引擎超时）
                     │                                                    │
                     ▼                                          EngineSignal[] ─► ch.insert_engine_signals
              broker.execute() → _persist_signal                     │
                     │                                     ConfluenceEngine.merge → RuleDecision
                     ▼                                               │
        signals / paper_trades (batch writer)              SetupService.create → setups(pending)
                                                                     │            │
                                                                     │            └─► AIEnqueue（异步，可选）
                                                                     │                     │
                                                                     └─► PaperBroker ◄────┘（AI 不能下单）
                                                                           │
                                                                    SetupTracker → tp_sl_events
```

关键约束：引擎只产出 `EngineSignal`；规则引擎同步、零 AI 依赖；AI 异步且可失败；
所有写库走 `AnalyticsBatchWriter`，请求路径不单条 INSERT。

## 2. 已实现接口（可直接复用，勿重造）

| 位置 | 接口 | 说明 |
|---|---|---|
| `analysis/contracts.py` | `EngineSignal(...)`、`PriceZone`、`AnalysisContext` | 不可变；`signal_id` 确定性派生，重放同 id |
| `analysis/registry.py` | `EngineRegistry.register/get/run`、`await analyze_all(window, ctx, timeout_seconds=8.0)` | 每引擎 `to_thread` + `wait_for`；超时线程仍在跑 |
| `strategy_runner.py` | `ICTStrategyRunner.bootstrap/on_closed/_compute/update_signal` | 现有 ICT 适配，输出旧 `TradeSignal` |
| `broker.py` | `execute(signal, mark, *, bar_time)`、`on_bar_closed`、`on_price`、`increment_bar`、`close_time_stop`、`on_trade` | 确定性 paper 执行；同 bar 触 TP+SL 按 SL 处理 |
| `storage/batch_writer.py` | `AnalyticsBatchWriter.enqueue(table, columns, rows)` | 按 (table, columns) 分组，500 行/2 s flush，失败进 `analytics_queue` WAL |
| `storage/clickhouse_store.py` | `insert_engine_signals(list[EngineSignal])`、`insert_signal(dict)`（legacy 适配） | 都经由 batch writer |
| `metrics.py` | `inc/set/observe/event/snapshot` | 进程内 P50/P99 环；`strategy_eval_ms` 等 |

## 3. 待实现的模块与最小接口

```python
# realtime/analysis/window.py —— 每 symbol 的有界 ring buffer
@dataclass(slots=True)
class MarketWindow:                     # 新增
    symbol: str
    bars: dict[str, deque[Kline]]       # interval -> 已收盘 bar
    as_of_ms: int
    def frame(self, interval: str, limit: int) -> list[Kline]: ...

# realtime/analysis/adapters/{ict,ichimoku,wyckoff,chanlun}.py —— 新增
class IctEngine:                        # 实现 AnalysisEngine Protocol
    engine_id = "ict"; version = "ict-<config-hash>"; warmup_bars = 120
    def analyze(self, window, context) -> list[EngineSignal]: ...

# realtime/decision/models.py + rules.py —— 新增
@dataclass(frozen=True, slots=True)
class RuleDecision:
    approved: bool; direction: str; entry_mode: str
    entry_zone: PriceZone; stop: float; targets: tuple[float, ...]
    rr: float; reasons: tuple[str, ...]; blocked_by: tuple[str, ...]
    strategy_version: str

class RuleEngine:
    def decide(self, signals: list[EngineSignal],
               confluence: "ConfluenceResult") -> RuleDecision: ...

# realtime/setups/service.py + tracker.py —— 新增
class SetupService:
    async def create(self, decision: RuleDecision,
                     signals: list[EngineSignal]) -> dict: ...   # -> setups 行
    async def transition(self, setup_id: str, to_status: str,
                         *, reason: str = "") -> bool: ...        # 幂等状态机
class SetupTracker:                     # 消费 broker 的成交/退出回调
    async def on_entry(self, setup_id: str, fill: float, qty: float) -> None: ...
    async def on_exit(self, setup_id: str, fill: float, reason: str) -> None: ...
```

`realtime/decision/confluence.py` 是**新建**的实时共振层；根目录 `confluence.py` 是研究层的
「ICT 主 + Ichimoku/Wyckoff 辅」融合（`FusionInput/FusionPlan/decide`），语义与实时多引擎
等权共振不同，不能直接当实时 confluence 用。

## 4. 阶段契约与幂等键

| 阶段 | 执行位置 | 输出 | 幂等键 |
|---|---|---|---|
| 引擎计算 | 线程池（未来进程池） | `EngineSignal[]` | `dedupe_key(engine,symbol,interval,bar,signal_type,direction)` |
| 信号落库 | 事件循环 enqueue | `signals` 行 | `signal_id`（含 engine_version 与价位）；CH `ReplacingMergeTree(ingested_at)` |
| 共振 | 事件循环（纯 CPU，微秒级） | `ConfluenceResult` | 不落库；由成员 `signal_id` 排序集合去重 |
| 规则决策 | 事件循环 | `RuleDecision` | `(strategy_version, symbol, bar, direction)` 决定是否已有 setup |
| Setup 创建 | 事件循环 | `setups(pending)` | `setup_id = "SET-"+sha1(strategy_version\|symbol\|primary_signal_id\|direction)[:20]` |
| AI 入队 | 事件循环 | 队列项 | `decision_id = sha1(setup_id\|prompt_version\|input_hash)[:20]`；Redis `ai:done:<id>` TTL 24h |
| AI 调用 | 独立 worker | `ai_decisions` / `llm_api_calls` | `decision_id` / `call_id`（每次 attempt 一个） |
| TP/SL | 事件循环 + broker | `tp_sl_events` | `event_id = sha1(setup_id\|event_type\|leg\|price\|occurred_ms)[:20]` |
| 错误 | 任意 | `error_events` | `error_id` 在捕获点生成一次，随 WAL 行重放 |

`setups` 不允许从 `pending` 直接跳到 `tp_hit`：允许边为
`pending→open|cancelled|expired`、`open→tp_hit|sl_hit|cancelled`、`tp_hit/sl_hit→closed`（若保留收盘态）。

## 5. 异步 AI 设计（待实现）

触发：仅当 `decision.approved and confluence.score >= AI_MIN_CONFLUENCE(默认 0.7)
and decision.rr >= 2.0`，且同 symbol 冷却 15 min、同 setup 单飞（in-flight 集合）。
禁止每根 closed bar 全量调用。

队列与并发：`asyncio.Queue(maxsize=64)`；2 个 worker；`asyncio.Semaphore(2)` 限制并发
HTTP。队列满时**丢弃新任务**并写 `error_events(category="ai", subtype="queue_full")`，
规则路径不受影响。

超时与重试：`httpx.Timeout(connect=5, read=30, write=10, pool=5)`，单次上层 deadline 45 s；
最多 3 次尝试，指数退避 `0.5*2^n + jitter(0~0.25s)`，上限 8 s；429/5xx/超时重试，
401/403/418 不重试；尊重 `Retry-After`。熔断：10 min 窗口内失败率 >20% 或连续 5 次失败
→ 打开 10 min，半开仅放 1 个探测请求。

校验：`orjson.loads` → Pydantic v2 模型（`extra="forbid"`）：
`approve: bool`、`direction: Literal["long","short","neutral"]`、`confidence: float(0..1)`、
`entry_mode: Literal["market_next_open","limit_retest","none"]`、
`tp: list[{price,size_pct,r}]`（≤4 腿）、`sl: float`、`rationale: str(≤2000)`、
`risk_flags: list[str](≤8)`、`invalidations: list[str](≤8)`、
`data_quality: Literal["ok","degraded","insufficient"]`。
校验失败 → 带错误摘要修复一次（temperature=0）→ 再失败 `decision_status="schema_invalid"`
并降级。重试次数记在 `llm_api_calls.retry_count`；修复标记写入 `parsed_decision` JSON
（表未单列该字段）。

降级：AI 超时/限流/schema 错 → `decision_status` 取 `api_timeout|api_error|schema_invalid`，
Setup 由规则决策继续管理；AI 的拒绝**不撤销**已创建的 pending setup，只记录
（AI 不得成为交易前置条件）。任何 AI 结果都不直接调用 broker。

预算：`AI_DAILY_TOKEN_BUDGET`（默认 1_000_000）用尽后停止入队并写 `error_events`；
每个决策记录 `total_tokens`、`cost_usd`、`latency_ms`，按 provider/model 聚合。

provider：当前只有 DeepSeek key（`deepseek-flash`，`reasoning_effort=low`）。
第二 provider 由 `AI_PROVIDER_B_*` 环境变量接入；只有 A 个 key 时，状态页必须显示
「failover 已配置未激活」，禁止伪造 B 成功。

## 6. 现有代码路径与迁移顺序

1. `RealtimeEngine` 持有 `EngineRegistry` 与 `MarketWindow`，closed bar fanout 调 `analyze_all`；
2. `IctEngine` 先只把现有 `analyze()/build_setup()` 包成 `EngineSignal`（保持 `TradeSignal`
   路径并行一个版本，做回归对比）；
3. 接 `insert_engine_signals` → 验证 `signals` 行与旧 `insert_signal` 一致；
4. 上 `ConfluenceEngine` + `RuleEngine` + `SetupService`（仍只生成 pending，不交易）；
5. `SetupTracker` 复用 broker 的止损/目标/时间止损逻辑，写 `tp_sl_events`；
6. 最后才开 AI 队列；开关默认关闭，先用历史回放验证 schema 与降级。

## 7. 失败模式（必须有测试）

- 引擎超时：registry 返回 `EngineRun(error=...)`；写 `engine_performance(status="timeout")`；
  其余引擎结果仍可用；线程未停要限并发。
- CH 写失败：batch writer → WAL；重启后重放；`ReplacingMergeTree` 保证不重复。
- 队列满/事件循环阻塞：`error_events` + `metrics` 计数；AI 与引擎均不得阻塞 feed。
- AI 全故障：规则路径继续；`degraded_to_rules` 可查询。
- 重放/重启：同一 bar 重发不产生第二个 setup、第二个 TP 事件。
