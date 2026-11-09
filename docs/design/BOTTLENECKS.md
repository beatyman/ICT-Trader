# 4GB / 2 vCPU 瓶颈清单与护栏

> 目标机：AWS 东京 `t4g.medium`（2 vCPU / 4GB / 50GB gp3）；本地基线为 i5-1135G7
> （4C/8T/16GB）。下列「实测」均在本地，**不是目标机承诺**；目标机一律 `needs benchmark`。
> 容器预算（`docker-compose.yml` + `deploy/clickhouse`）：Redis 512 MB（`maxmemory 384mb`,
> `noeviction`）、ClickHouse 2 GB（`max_server_memory_usage 1.5GB`, `max_threads=2`,
> `mark_cache 128MB`, `background_pool 4`）、Python app 1200 MB。

## 0. 已有实测（本地）

- Python 进程 RSS：空载 133 MB；导入 pandas + chan.py 并跑完一轮后 149–150 MB（单 worker）。
- ICT 单 symbol 单次收盘：`analyze` 553 ms（冷）/276 ms（热）+ `build_setup` 399/228 ms；
  1800 根窗口。
- Chanlun 单次：1539–2391 ms（1800 根）——全仓最贵。
- Ichimoku 18 ms、Wyckoff 50 ms（1800 根）。
- 方法：`realtime.demo.generate_history()` 合成 15m bar，只测 CPU，不代表真实行情/内存峰值。

## 1. B1：引擎计算串行化（最可能先撞墙）

现象：`strategy_runner.ICTStrategyRunner._eval_sem = asyncio.Semaphore(1)`，
所有 symbol 的评估串行；`asyncio.to_thread` 用的是事件循环默认线程池。
50 symbols × 约 0.5–0.95 s（ICT）≈ 25–48 s；若四引擎全开 ≈ 105–170 s/次收盘（本机）。
15m 周期有 900 s，看似够，但 1h/4h 与 1d 的评估也会在同一把信号量上排队，且冷缓存更慢。

护栏：
- 每引擎 `timeout`（registry 默认 8 s，chanlun 单独设 5 s），超时写
  `engine_performance(status="timeout")` 并跳过该引擎，不阻塞其他引擎；
- 评估队列深度 > 2×symbol 数时，只算 base timeframe，跳过辅助引擎（降级而非堆积）；
- 目标机验收：50 symbols × 15m 的 ICT 全量评估 P99 < 120 s；四引擎 P99 < 600 s；
- 结构化升级顺序：先 `ProcessPoolExecutor(2)`，再考虑增量指标缓存；进程池前必须实测
  `每 worker RSS × 2 + 父进程 ≤ 700 MB`，否则与 1 GB 预算冲突。

## 2. B2：Chanlun 的 CPU 与依赖不确定性

现象：单次 1.5–2.4 s；chan.py 是纯 Python 结构计算，5000 根是否超线性未知
（`needs benchmark`）；`FREQ_MAP` 把 4h 映射成 `K_60M`，级别语义本身有偏差。
`sys.path.insert` 与全局 import 也让常驻内存不可控。

护栏：
- 只在 1h/4h（不是 15m）触发，或用「每 N 根 bar 一次」降频，N 可配（默认 4）；
- 窗口上限 500 根（超过则裁剪尾部），超时 5 s，超时后连续失败 3 次则熔断 30 min；
- 进程池隔离（chan.py 崩溃不拖垮主进程）；`engine_version` 记录 chan.py 校验和；
- 验收：单次 P99 < 3 s，50 symbols 下 chanlun 总占用 < 每 bar 周期 30%。

## 3. B3：每次收盘重建 DataFrame

现象：`_compute()` 每次 `pd.DataFrame([b.to_csv_row() for b in bars])`，1800 根要构造
1800 个 dict + datetime 对象；这是纯 Python 开销，且会让 GC 峰值上升。

护栏：
- 每个 `(symbol, interval)` 维护 `numpy` 列式 ring buffer（OHLCV float64 + open_time int64），
  只在评估时零拷贝切片构造 DataFrame（`pd.DataFrame(arrays, copy=False)`）；
- 目标：数据准备 <50 ms/1800 根（当前未单独测量，`needs benchmark`）；
- 禁止在 tick/未收盘路径调用 `_compute()`（现在已满足：只对 closed 且 `c.end==n-1`）。

## 4. B4：ClickHouse 同步驱动 + 单锁 + FINAL

现象：`clickhouse-connect` 是同步客户端，所有调用走 `asyncio.to_thread` 并共享
`threading.RLock` → 写批次、健康探测、图表查询**完全串行**。`load_recent()` 使用
`FROM ... FINAL`，在月分区多 part 时开销随 part 数上升；启动时 150 个 stream 各查一次，
串行放大为几十秒。

护栏：
- 热路径禁止 `FINAL`：改用 `argMax(col, ingested_at)` 子查询或按 `ORDER BY` 取最新；
  `load_recent` 读 klines 时若必须去重，限定 `open_time` 范围并允许 `FINAL`（低频）；
- `ch.count()` 每 30 s 一次（现状 `tick % 6` × 5 s）保留，但改为近似值或系统表统计；
- 查询超时：`send_receive_timeout=30` 已设；再加应用层 `wait_for(10s)`，超时返回空并记
  `error_events(category="clickhouse", subtype="query_timeout")`；
- 验收：`clickhouse_batch_ms` P99 < 500 ms；锁等待（需新增埋点）P99 < 100 ms；
- 若 2 vCPU 上锁竞争明显，再拆「写入客户端」与「只读查询客户端」两个连接（各自加锁）。

## 5. B5：Redis 内存（按 150 streams 计算会超）

现状：`REDIS_CLOSED_KEEP` 已从 5000 降到 1000/stream，每个 closed hash 15 个字段。
24 streams → 2.4 万 hash；150 streams → 15 万 hash。
按 0.8–1.2 KB/hash 估算约 120–180 MB，给 384 MB `maxmemory` 留出余量。
5000/stream 的旧配置会到 600–900 MB，已废弃。
`noeviction` 下写失败会抛 OOM，`RedisStore.store_closed` 失败 → stream worker 重试 →
该 stream 停止推进（不会静默丢 bar，但会卡住）。

护栏：
- 按预算反算：`keep = floor(280MB / (n_streams * 1.2KB))`；150 streams → 约 1500，
  默认取 1000 留余量；用 `REDIS_CLOSED_KEEP` 下发，ZSet 同步 `ZREMRANGEBYRANK`；
- `used_memory` 已有采集（`metrics.set("redis_memory")`），加阈值告警：>300 MB 警告、>360 MB 紧急；
- 启动自检：`streams × keep × 1.2KB > 0.8 × maxmemory` 时拒绝启动并打印建议值；
- 验收：50×3 配置下 `used_memory` 峰值 < 320 MB，`redis_error` 计数为 0。

## 6. B6：多处无界队列

现象：`StreamSession.queue = asyncio.Queue()`、`ClickHouseStore._queue`、
`AnalyticsBatchWriter.queue` 都无上限。CH 变慢或 Redis 卡住时，内存增长没有刹车。

护栏：
- stream 队列 `maxsize=2000`；满时对**未收盘** bar 丢弃并计数，对 closed bar 阻塞等待
  （不能丢已收盘 bar），同时 `error_events(category="backpressure")`；
- CH 队列 `maxsize=20000`，满时把 bar 直接写 WAL 而不是堆内存；
- analytics 队列 `maxsize=5000`，满时丢弃低优先级 `engine_performance` 样本（保留 setups/AI）；
- 验收：故障注入（停 CH 5 min）后 RSS 增长 <100 MB，恢复后 WAL 回放无重复。

## 7. B7：50 symbols × 3 intervals 的 WS 与广播

- 150 条 stream，`max_streams_per_conn=100` → 2 条连接（分组按 interval）；断线重连的
  风暴半径可控。消息速率未实测（`needs measurement`）；按每 stream 1–2 msg/s 估算
  150–300 msg/s，`orjson` 解析不是瓶颈，真正的风险是回调里做重活（当前只 enqueue）。
- `/ws` 广播 1 Hz 全量快照；150 个 session 明细每秒全发，是 payload 膨胀的主因
  （大小 `needs measurement`）。慢客户端会串行阻塞 `broadcast()`。

护栏：
- 广播只发 `rev` 变化的区块；sessions 只发选中 symbol + 全局计数；
- 每个连接用独立发送队列（容量 8，满则断开该客户端），单条发送超时 1 s；
- 验收：单条消息 <32 KB、单次广播 CPU <10 ms、慢客户端不影响其他连接。

## 8. B8：AI API 不可靠（当前连「决策流水线」都还不是）

现状：`DeepSeekReviewer` 只在人工点击时调用；`deepseek_timeout=90 s`，
`max_tokens=4000`，`reasoning_effort=low`；文档记录本地 review 延迟约 10–16 s、
prompt 约 6–8k tokens。没有第二 provider（只有 DeepSeek key 时 failover 不可用）。

目标态护栏（AI pipeline 落地时）：
- 连接 5 s / 读 30 s，单决策 deadline 45 s；最多 3 次尝试 + 抖动退避；429/5xx/超时重试，
  401/403/418 不重试；
- 并发 `Semaphore(2)`、队列 `maxsize=64`、同 setup 单飞；队列满丢弃并记
  `error_events(category="ai", subtype="queue_full")`；
- 熔断：10 min 窗口失败率 >20% 或连续 5 次失败 → 打开 10 min；
- 每日 token 预算硬上限，超限停止入队；成本按 `provider/model` 日聚合；
- **任何 AI 失败都不得阻塞规则信号**：`decision_status=degraded_to_rules` 落库即可；
- 验收：注入超时/429/非 JSON 响应，规则路径的 Setup 生成量与 AI 关闭时一致。

## 9. B9：SQLite WAL 增长与回放

CH 或 Redis 长时间不可用时，`wal.sqlite3` 会持续增长（bars + analytics 行）。
analytics 行含 `stack_trace/context`，单行可达 8 KB；`error_events` 风暴会快速放大。

护栏：
- WAL 文件 >200 MB 时开始按表丢弃低优先级行（performance 先丢，error/setup 保留）；
- `error_events` 单进程限速（如 50 条/s，超出聚合计数）；
- 回放后校验 `WAL pending → 0`，且 `signals/setups` 行数与首次写入一致（幂等）；
- 验收：停 CH 30 min，WAL <500 MB，恢复后 15 min 内回放完毕。

## 10. B10：事件循环与健康探测

- `_health_loop` 每 5 s 探测 Redis/CH，`wal.stats()` 走线程；CH `count()` 每 30 s。
  两个同步驱动调用都持锁，若 CH 慢，健康探测会与写批次抢锁 → 需要应用层超时（见 B4）。
- `engine.snapshot()` 每秒构造一次大 dict，并序列化给所有连接；随 symbols 增加而变重。

护栏：
- 新增 `loop_lag_ms` 埋点（每 250 ms 打点），P99 >50 ms 记 `error_events`；
- `snapshot()` 加缓存（同一 broadcast 周期只构造一次，已是此结构）；
- 验收：50 symbols 下 `loop_lag_ms` P99 <50 ms，`strategy_eval_ms` P99 与基线对比有记录。

## 11. 护栏汇总（可测量）

| 指标 | 阈值 | 超限动作 |
|---|---|---|
| `strategy_eval_ms` P99 | <8 s/引擎 | 跳过该引擎、降级到 base TF |
| 四引擎 50×15m 墙钟 | <600 s | 降低 chanlun 频率 |
| Python RSS | <1 GB | 拒绝新 stream / 缩短窗口 |
| Redis `used_memory` | <320 MB | 降 `closed_keep`、告警 |
| CH batch P99 | <500 ms | 增大 batch、降并发 |
| `loop_lag_ms` P99 | <50 ms | 把计算移出进程 |
| WAL pending | <5000 行 | 暂停非关键写入 |
| AI 降级率 | <20%（开启时） | 熔断、只走规则 |
| 广播消息 | <32 KB | 拆 topic、裁剪 sessions |

所有阈值都需要在 t4g.medium 上复测；本文件中的数字只作为回归对比的起点。
