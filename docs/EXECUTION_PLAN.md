# ICT Realtime 执行计划 (Execution Plan)

> 目标不是一次性重写系统，而是把当前“可运行的实时 paper 终端”升级为
> “多引擎、规则优先、AI 增强、全链路可追溯、可回滚”的交易研究/执行系统。
> 任务顺序按依赖关系排列，避免先做 UI 或 AI 而返工数据契约。

---

## 1. 执行原则

1. **Contract first**：先定 Signal / Setup / AI decision / Error 契约，再接引擎。
2. **Vertical slice first**：先把 ICT 一条真实信号跑通到 Setup 和 paper TP/SL，
   再复制到其他引擎。
3. **规则永远是主路径**：AI 不可用时系统必须完整工作。
4. **先测量再优化**：每个阶段都有延迟/内存/错误率基线，禁止凭感觉调参。
5. **测试替身只用于测试**：生产与绩效统计绝不使用 mock/synthetic signal。
6. **一次只交付一个可运行切片**：每个任务独立可回滚。
7. **4GB 是硬预算**：任何新组件先报内存，再合并。

---

## 2. 当前基线（开工前复测）

| 项 | 现状 |
|---|---|
| Python | 3.12（venv），尚未锁定完整依赖 |
| 实时行情 | Binance public WS + REST seed/backfill |
| 存储 | Redis 7 + ClickHouse 24.8，Docker 内存限制已配置 |
| 策略 | 单一 ICT frozen setup，只对 closed 15m 计算 |
| AI | DeepSeek-V4.1-Flash 按需 review，不是 decision pipeline |
| 执行 | paper broker，无真实下单 |
| UI | 暗色 Canvas 单页，WebSocket 1Hz |
| 观测 | 进程内 metrics + event log，无 error_events 表 |
| 测试 | 244+，以实际运行结果为准 |
| 压测 | 6 symbols × 4 intervals Demo；未做 50×3 正式压测 |

基线命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/smoke_realtime.py
docker stats --no-stream
```

---

## 3. 技术评审门禁（T0）

在写业务代码前，先产出 6 份短文档/章节，放到 `docs/design/`：

- [ ] `ENGINES.md`：四引擎信号定义、窗口、多周期、复杂度、4GB 可行性；
- [ ] `SCHEMA.sql`：7 张新表完整 DDL，分区/排序/去重/TTL；
- [ ] `PIPELINE.md`：Signal → Rule → AI → Setup → TP/SL 数据流与接口；
- [ ] `UI_THEME.md`：双主题 token、图表标注、WS 数据量、浏览器/服务端内存；
- [ ] `EVOLUTION.md`：指标、版本、影子、A/B、回滚、人工审批；
- [ ] `BOTTLENECKS.md`：4GB、同步驱动、AI 不可靠性、最可能瓶颈与缓解。

**技术选型已定，评审不得反复摇摆：**

- 图表继续使用**现有原生 Canvas 2D 渲染器**，不引入 ECharts/TradingView/Node 构建；
  后端只下发聚合后的 candles + overlays，前端不重算指标；
- 双主题通过 CSS 设计 token 与 `localStorage` 持久化，切换不重载；
- AI provider 采用 OpenAI-compatible 适配层，DeepSeek V4.1 Flash 为主，
  第二 provider 通过环境变量接入；当前只有 DeepSeek key 时，
  failover 处于“已配置未激活”，不得伪造第二 provider 成功；
- 依赖锁定采用 `uv.lock` + Python 3.12.x；Docker 与 CI 使用同一 lock。

---

## 4. 工作流

```text
T0 技术评审
   │
P0-1 基线/锁版本
   │
P0-2 Signal/Engine 契约 ───────────────┐
   │                                   │
P0-3 DDL + 统一批量 writer             │
   │                                   │
P0-4 error/performance 遥测            │
   │                                   │
P1-1 ICT adapter ──► P1-2 Ichimoku ──► P1-3 Wyckoff ──► P1-4 Chanlun
   │                                   │
P2-1 RuleDecision + SetupService       │
   │                                   │
P2-2 AI provider/队列/校验/降级        │
   │                                   │
P2-3 TP/SL tracker + 分批/移动          │
   │                                   │
P1-5 Confluence ───────────────────────┘
   │
P3-1 双主题 ──► P3-2 图表标注 ──► P3-3 Setup/统计/错误/性能页面
   │
P4-1 量化指标 ──► P4-2 版本/影子/A-B/回滚 ──► P4-3 日报/周报
   │
P5-1 50×3 压测/soak/故障注入 ──► P5-2 AWS 东京部署/chrony/告警
```

---

## 5. 任务卡（按优先级）

### P0 — 基础契约与可观测性

| ID | 任务 | 依赖 | 规模 | 交付物 / DoD |
|---|---|---|---|---|
| P0-1 | 锁定 Python 3.12 与依赖；建立性能/内存基线 | T0 | S | `uv.lock`、CI、`docs/PERF_BASELINE.md`；6×15m 的 P50/P99/内存可复现 |
| P0-2 | 统一不可变 `Signal`/`AnalysisEngine` 协议与 registry | T0 | M | `analysis/base.py`；所有引擎只能返回 Signal；重复/非法字段测试 |
| P0-3 | 7 张表 DDL + 统一 batch writer + repository | P0-2 | L | `SCHEMA.sql`、迁移、writer queue；signal/trade 不再单条插入；失败重试测试 |
| P0-4 | `error_events` + `engine_performance` + 错误分类 | P0-3 | M | 所有异常结构化落库；可按 module/engine/symbol 查询；MTTR 指标 |
| P0-5 | event loop 阻塞检测与 engine timeout | P0-2 | S | `loop_lag_ms`、per-engine timeout/circuit breaker；超时有记录 |

**P0 出口标准**：现有 ICT 信号仍可运行；数据库新增表幂等；测试全绿；
所有新增写入走同一个 writer；无 secret 入库。

### P1 — 四引擎接入与共振

| ID | 任务 | 依赖 | 规模 | 交付物 / DoD |
|---|---|---|---|---|
| P1-1 | ICT adapter（复用 `concepts/cisd/sweeps/setup`） | P0-2 | M | 真实 ICT Signal → 统一 schema；证据含 OB/FVG/sweep/CISD；回归对比现有 setup |
| P1-2 | Ichimoku adapter（复用 `indicators.py`） | P0-2 | M | TK 交叉、云层、Chikou、三役；9/26/52 可配；多周期确认 |
| P1-3 | Wyckoff adapter（复用 `wyckoff.py`） | P0-2 | M | 阶段 + Spring/UT/SOS/SOW/LPS 等事件；量价证据；窗口参数 |
| P1-4 | Chanlun adapter（复用 `chanlun.py`） | P0-2 | L | 分型/笔/线段/中枢/背驰/买卖点；明确 chanpy 依赖与 fallback 一致性 |
| P1-5 | Confluence 聚合 | P1-1..4 | M | 同向、时间窗重叠合并；置信度可解释；无重复 Setup；单测覆盖冲突方向 |
| P1-6 | ComputeSupervisor + 50 symbols 基准 | P1-1..4 | L | 线程/进程池、每引擎 P50/P99、超时隔离；单次 closed bar 不阻塞 loop |

**P1 出口标准**：至少 2 个引擎同时产生真实 Signal；4 引擎可独立启停；
compute pool 下 50 symbols 的 15m 收盘计算可在一个 bar 周期内完成；
`engine_performance` 有数据。

### P2 — 规则优先的 Setup 与异步 AI

| ID | 任务 | 依赖 | 规模 | 交付物 / DoD |
|---|---|---|---|---|
| P2-1 | RuleDecision + SetupService（状态机） | P0-3, P1-5 | L | 规则信号立即生成 `setups/pending`；状态转换幂等；无 AI 也能完整运行 |
| P2-2 | AI provider 抽象 + Pydantic 校验 + 降级 | P0-3, P2-1 | L | 主/备 provider；connect 5s/read 30s；jitter 重试 3 次；schema 修复一次；降级落库 |
| P2-3 | 异步 AI 队列 + 触发阈值 + Semaphore | P2-2 | M | 仅高置信度触发；并发上限配置；不阻塞 WS；超时/429/空响应测试 |
| P2-4 | TP/SL tracker（分批、移动、滑点） | P2-1 | L | `tp_sl_events` 完整；计划 vs 实际偏差；状态机测试 |
| P2-5 | AI/Setup 查询与回放 API | P2-2..4 | S | 可按 setup/decision/时间查；输入快照和原始输出可追溯 |

**P2 出口标准**：规则路径在 AI 全故障时仍产生/管理 Setup；
AI 决策状态分布可查询；每个 Setup 都能追到 signals/ai_decisions/tp_sl_events；
`degraded_to_rules` 有真实测试。

### P3 — 双主题与可视化

| ID | 任务 | 依赖 | 规模 | 交付物 / DoD |
|---|---|---|---|---|
| P3-1 | 双主题 token + 切换 + 持久化 | P0-1 | S | 亮/暗切换不重载；截图无对比度问题；主题存 localStorage |
| P3-2 | 图表引擎标注 | P1-1..4 | L | OB/FVG/云层/中枢/Wyckoff 事件；后端聚合 overlays；不阻塞 |
| P3-3 | Setup/统计/错误/性能页面 | P2-4、P0-4 | L | Setup 详情、AI 理由、计划/实际 TP/SL、错误趋势、引擎 P50/P99 |

**P3 出口标准**：亮色/暗色都可读；前端不重复计算指标；
首屏资源与 WS 消息大小有记录；不引入高内存图表依赖。

### P4 — 量化反馈与自进化

| ID | 任务 | 依赖 | 规模 | 交付物 / DoD |
|---|---|---|---|---|
| P4-1 | 指标查询层（胜率/PF/期望/回撤/滑点/AI 质量） | P2-4、P3-3 | L | 按 engine/symbol/interval/session/type 下钻；Wilson 区间 |
| P4-2 | strategy_versions + 版本化参数 + 回滚 | P4-1 | M | 每次变更记录版本、diff、指标前后；回滚可用 |
| P4-3 | Shadow / A-B / 人工审批 | P4-2 | L | 新版本影子运行不交易；对比报告；人工 promote；kill switch |
| P4-4 | 日/周评估报告 | P4-1 | S | 定时生成；包含样本量、置信区间、限制声明 |

**P4 出口标准**：所有参数修改可追溯；没有未版本化的在线修改；
影子结果与线上分开存储；报告不把 Demo 数据混入。

### P5 — 规模与生产化

| ID | 任务 | 依赖 | 规模 | 交付物 / DoD |
|---|---|---|---|---|
| P5-1 | 50 symbols × 3 intervals：压测 + 24h soak + 故障注入 | P1-6, P2-4 | L | WS 断连、gap、CH 失败、AI 超时；内存曲线；无 OOM；恢复时间 |
| P5-2 | AWS 东京 t4g.medium 部署 | P5-1 | M | arm64 镜像、chrony、TLS/auth、备份、保留策略、告警；与本地指标对比 |

---

## 6. 第一轮冲刺（建议 5–7 个工作日）

只做“ICT 一条真实链路 + 契约”，不碰四引擎全量：

1. T0 的 6 份设计评审（可当天完成）；
2. P0-1 锁版本 + 性能基线；
3. P0-2 Signal 契约；
4. P0-3 7 张表 DDL + 统一 writer；
5. P1-1 ICT adapter；
6. P2-1 SetupService 最小状态机；
7. 验证：真实 BTCUSDT 15m 信号 → `signals` → `setups/pending` →
   paper 开仓 → `tp_sl_events`；重启后状态可从 ClickHouse 恢复；
8. 输出一份真实性能/内存报告。

**第一轮明确不做**：其他三引擎、AI 自动决策、双主题、自进化。
避免铺开导致每个模块都半成品。

---

## 7. 验收指标

### 数据完整性

- 已收盘 bar 在进程 kill/重启、CH 写入失败、WS 断连场景下不丢；
- WAL 重放后无重复逻辑行；
- gap 恢复期间不交易，回放后状态连续；
- 所有时间 UTC ms，跨时区查询无偏移。

### 延迟/资源

- 同区域 VPS：WS parse → Redis 更新 P99 < 5ms；
- 事件循环 lag P99 < 50ms；
- 4 引擎 × 50 symbols 的单次 closed-bar 计算在 15m 周期内完成；
- Python 进程 RSS < 1GB，总容器 < 4GB，无 OOM；
- ClickHouse 批量写 P99 有记录，不出现持续单条插入。

### 可追溯性

- 任一 Setup 可反查：原始 K 线 → 引擎 Signal → 规则决策 → AI 输入/输出 →
  TP/SL 事件 → 实际出场；
- 任一错误可按 module/engine/symbol 聚合；
- 任一策略版本可查变更前后指标，可回滚；
- AI 失败率、降级率、token、cost 可查。

### 质量

- 新增逻辑有单测；外部 API 用测试替身；
- 失败路径覆盖：AI timeout/schema invalid/429、CH fail、gap、SIGTERM；
- 不能以 Demo/合成数据作为胜率或策略有效性证据。

---

## 8. 风险登记（按优先级）

| 风险 | 影响 | 缓解 |
|---|---|---|
| 4 引擎 × 50 symbols 的 pandas 重计算 | 15m 收盘延迟、事件循环饥饿 | 固定窗口/ring buffer；进程池；每引擎 timeout；分批调度 |
| Chanlun `chanpy` 依赖/回退不一致 | 信号不可复现 | 固定版本/ vendored 校验；contract tests；engine_version |
| AI 输出 schema 不稳定 | 错误决策/阻塞 | Pydantic + repair once + 降级；不进入关键路径 |
| 单条 Insert 回归 | 写放大/延迟 | 统一 writer + code review + 测试拦截 |
| 双 provider 实际只有一个 key | failover 形同虚设 | 配置校验；状态面板显示“单 provider”；到货后演练 |
| 亮色主题对比度/图表性能 | 可用性下降 | token 审查；截图回归；overlay 数量上限 |
| 自进化过拟合 | 虚假提升 | 时间外推、shadow、最小样本、人工审批、回滚 |
| 4GB 内存下新增依赖 | OOM | 依赖准入；Docker budget；压测门槛 |

---

## 9. 下一步建议

先执行 **T0 + P0-1 + P0-2**，不要同时动 AI 和 UI。
契约稳定后，用 ICT 垂直切片跑通，再并行展开：

- Lane B：Ichimoku adapter；
- Lane C：AI provider 抽象（先不自动触发）；
- Lane D：双主题（纯前端，不依赖数据）。

这样可以在最短时间内得到一个“数据可信、状态可追、AI 可降级”的闭环，
而不是先堆出四个引擎和一套漂亮但无法归因的 UI。
