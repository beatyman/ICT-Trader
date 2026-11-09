# 引擎设计：ICT / Ichimoku / Wyckoff / Chanlun

> 基线：分支 `feat/realtime-paper-trading`，已提交 P0 契约/表/批量写入
> 与 `ICEngine` adapter。本文只描述仓库里真实存在的入口/函数；
> 「待实现」= 没有 adapter 或没有接入 `RealtimeEngine` 的引擎注册表。
> 所有性能数字是**本地一次性微基准**（方法见 §5），不是 t4g.medium 的承诺；
> 未测项一律标 `needs benchmark`。

## 0. 统一契约（已实现；ICT adapter 已接线，registry 已由 RealtimeEngine 持有并以影子模式运行）

`analysis/contracts.py`：

| 对象 | 关键约束 |
|---|---|
| `PriceZone(low, high)` | 不可变；`low <= high`；价格 > 0 |
| `AnalysisContext(symbol, interval, as_of_ms, params, htf)` | 只读；`interval` 必须属于 `models.INTERVAL_MS`；`as_of_ms` 为 UTC ms |
| `EngineSignal` | `frozen=True, slots=True`；含 engine、engine_version、symbol、interval、direction、signal_type、trigger_price_zone、invalidation_price、confidence∈[0,1]、source_kline_open_time、created_at、evidence、signal_id |
| `signal_id` | `"SIG-" + sha1(engine\|version\|symbol\|interval\|direction\|type\|zone.low\|zone.high\|invalidation\|bar_open)[:24]`，重放可复现 |
| `dedupe_key` | `(engine, symbol, interval, source_kline_open_time, signal_type, direction)` |

`analysis/registry.py`：`AnalysisEngine` Protocol（`engine_id`、`version`、`warmup_bars`、
`analyze(window, context) -> list[EngineSignal]`）；`EngineRegistry.register/get/names/run`；
`async analyze_all(window, context, timeout_seconds=8.0)` 用 `asyncio.to_thread` 并发 + `wait_for`。
**注意**：超时只取消 await，线程仍跑完，超时不是硬隔离。

现状：`RealtimeEngine` 还没有持有 registry；实时路径仍是 `ICTStrategyRunner`（旧
`TradeSignal`）→ `PaperBroker`。`analysis/adapters/` 不存在，四个引擎都没有 adapter。

## 1. ICT（唯一已接入实时路径；已实现）

入口：
- `concepts.analyze(df, ICTConfig) -> ICTResult`（swings / structure / FVG / OB / PD arrays /
  liquidity / NWOG-NDOG / fib / CISD / sweeps / key levels）；
- `setup.build_setup(df, result, SetupConfig, daily=None) -> list[CISD]`（多周期闸门 + 质量分）；
- 实时适配 `strategy_runner.ICTStrategyRunner`：`bootstrap()`、`append_recovered()`、
  `on_closed()`、`_compute()`；`on_signal` → `RealtimeEngine._on_signal` → `broker.execute()`。

实际参数（`realtime/config.py` 默认）：`strategy_interval="15m"`、`stack="4h_15m"`、
`eval_window = max(400, 1800)`（含 `1d` 的 stack 为 8000）、`bootstrap_bars=5000`、
`min_grade=3`、`min_confluence=2`、`require_sweep=true`、`allow_short=false`、`rr=2.0`、
`max_bars=40`。runner 以 `ICTConfig(mode="historical", cisd_all=True, cisd_min_grade=0)` 跑全量 CISD，
再只保留 `c.end >= n-3` 的候选，最后只接受 `c.end == len(frame)-1` 的 setup（无盘中信号）。

窗口：实时评估要求 ≥120 根已收盘 bar（bootstrap 的 `_compute` 门槛是 100 根）；
CISD 内部 `lookback=500`；`detect_cisd` 对整个窗口单遍扫描。

多周期：`SetupConfig.stack` 经 `setup._stack_spec()` 解析（`1d_4h_15m`、`1d_1h_15m`、
`1d_15m`、`4h_15m`、`4h_1h_15m`）；HTF 由 `setup._htf()` 内部
`resample_htf(label="left", closed="left")` → `_drop_incomplete_htf()` → `align_htf(merge_asof backward)`。
只有 `strategy_interval` 的收盘 bar 触发评估；其它 interval 只进 Redis/ClickHouse 喂 UI。

输出证据：`CISD.checks`（sweep / pda / volume / session / targets / smt）、`quality_parts`、
`grade`、`quality`、`level/sl/target/rr`；`ICTResult` 另带 FVG、OB、PD array、sweep、key level。
落库映射见 `clickhouse_store.insert_signal()`：engine=`ict`、signal_type=`ict_cisd`、
zone=`sorted(entry, setup_level)`、invalidation=`stop`。

实测（1800 根、synthetic demo tape、本机）：`analyze` 553 ms（冷）/276 ms（热）；
`build_setup` 376–399 ms（冷）/228 ms（热）；5000 根时 1856/527 ms。主循环近似 O(n)，CISD 每次确认对
levels/zones 做窗口扫描，最坏 O(n·(L+Z))。

集成风险：
- `insert_signal()` 把旧 `TradeSignal` 折进新 `signals` 契约；字段语义（entry vs trigger zone）
  只在 evidence 里保留，回放时不能再用 `entry/stop/target` 列查询。
- 内存去重 `_seen` 上限 20000 → 超限截断为 10000，长跑后重放可能重复评估（写库仍幂等）。
- 1D stack 的云层窗口：`span_b = rolling(52).max().shift(26)` 需要 ≥78 根日线。
  `eval_window` 已从 6500 提高到 8000 根 15m（约 83 天）；`BOOTSTRAP_BARS`
  也必须 ≥8000，否则仍没有足够日线。该值仍需在目标环境用真实数据确认。
- HTF 完整桶：`resample_htf()` 现在丢弃最后一个未完成桶，
  `mark_htf_known_at()` 把左标签桶移到收盘时刻后再交给 `align_htf()`。
  已新增测试覆盖 09:00 桶在 10:00 才可见，防止 4H/1D 形成中桶泄漏到
  09:15–09:45 的决策。A 股 `resample_cn_4h()` 使用收盘标签，不需要该偏移。

## 2. Ichimoku（算法已实现；无 adapter、无独立信号）

入口：`indicators.ichimoku(df, tenkan=9, kijun=26, senkou=52)` →
`tenkan/kijun/span_a/span_b/chikou_ok/cloud_top/cloud_bottom`；`add_indicators()`；
`resample_htf(rule)`；`align_htf(base, htf, cols)`。当前只在 `setup.py` 的 HTF 闸门里被使用
（`require_*_cloud/macd/tk`、`min_*_score`），没有独立 entry/exit。

窗口：`span_a` 先 `rolling(26)` 再 `shift(26)`，`span_b` 为 `rolling(52)` 再 `shift(26)`，
因此同周期至少 78 根才有非 NaN 云；建议 ≥200 根；1D 云需要 ≥78 根日线（15m 合成约 7500 根）。

多周期（已实现 adapter）：`analysis/engines/ichimoku.py` 在 base 15m 上求 TK 交叉，
4H 由同一窗口 resample，最后一个未完成桶丢弃，并用 `known_at` 收盘时刻过滤；
同时校验云层、Chikou 和 ATR 云厚。默认 `htf_interval=4h`，1D 需要至少 8000 根
15m（当前默认窗口不足，必须显式配置）。

输出证据（拟）：`tenkan/kijun/span_a/span_b/cloud_top/cloud_bottom/price_vs_cloud/tk_cross/
chikou_ok/cloud_thickness/kumo_twist`。不得把 `span_a/span_b` 当「未来云」画在 bar 之后。

实测：`ichimoku()` 1800 根 18.2 ms；纯 pandas rolling，内存 O(n)。

集成风险：
- `ichimoku()` 返回的 span 已 shift，是「当前 bar 上应显示的云」，不是未来位移云；
- `chikou_ok` 只是 `close > close.shift(26)` 的简化，不等于完整 Chikou 位移；
- `mark_htf_known_at()` 必须在所有 left-labelled HTF 路径上使用；
  当前 `setup._htf()` / `_raw_htf()` 已接入，其他未来模块不得绕过；
- TK 交叉是「状态」还是「事件」必须先在 adapter 里定死，否则同一根 bar 会反复出信号。

## 3. Wyckoff（已实现 adapter）

入口：`wyckoff.detect_wyckoff(df, trend_len=50, pivot_len=3, min_range_bars=15,
max_range_bars=300, max_leg_bars=300, ...) -> (WyckoffState, phases: list[int])`；
事件信号在 `state.signals`（`WyckoffSignal.kind ∈ {spring, utad, sos, sow, lps, lpsy}`），
阶段 0–5 对应 A–E；质量分用 `wyckoff_quality.effort_ratio/climax_score/spring_score/sos_score`。

窗口：高潮搜索回看 `trend_len=50`，AR/ST/Spring 需要 `min_range_bars=15` 以上的箱体，
区间上限 `max_range_bars=300`；建议 ≥300 根，500–1000 更稳。

多周期（待实现）：函数是单周期状态机；拟用 15m 出事件、4h 判阶段（resample 时 volume 必须 sum）。

输出证据：`stage/phase/direction`、`range_top/bottom/mid/height`、`creek_ice`、`climax_side`、
`spring_extreme/spring_bar`、`t1/t2`、`invalidation`、`completed/failed`、`events[(bar, price, label)]`。

实测：1800 根 50 ms；`pivot_high/pivot_low` 是 O(n·(left+right)) 的 Python 循环，主循环 O(n)。

集成风险：
- `pivot_high[i]` 需要右侧 `pivot_len=3` 根确认 → 事件天然滞后 3 根；**不能**把 bar i 当成实时触发点；
- 返回值是「最后一个结构」，中途失败结构只留在 `events`；`signals` 在新周期不清空（跨周期累积），
  adapter 必须按 `signal.bar` 与窗口尾部过滤；
- 输出没有 symbol/interval，也没有 `confirmed_at`，时间戳需 adapter 按窗口起点换算；
- 单根 bar 的 `phases` 会随新数据回退（状态机重跑），落库要用 `engine_version` 标记。

## 4. Chanlun（已实现 adapter）

入口：`chanlun.analyze_chanlun(df, symbol="SYM", freq="1d") -> ChanlunResult`；
`chanpy_available()`；`df_to_klus()`；`FREQ_MAP`。结果含 `bi_count/seg_count/zs_count`、
`last_bi_direction`、`last_zs_zd/zg`、`position(above/inside/below)`、`bias(long/short/wait)`、
`bsp_type`、`bsp_count`、`reasons`。

窗口：`len(df) < 30` 直接返回空并写 reason；chan.py 配置
（`bi_strict=True, trigger_step=True, divergence_rate=inf, bs_type="1,2,3a,1p,2s,3b"`）
在 300–1000 根上稳定，建议 ≥300。

多周期（待实现）：函数是单级别；区间套 = 两次调用（1d 定方向 + 15m 买卖点），
`confluence.FusionInput` 已有 `chanlun_htf_direction/chanlun_ltf_bias` 字段但未接实时。

实测：1800 根 1539–2391 ms（全仓最重的引擎）；chan.py 为纯 Python 结构计算，
5000 根是否超线性 `needs benchmark`。

集成风险：
- 依赖 vendored `vendor/chan.py`（`CHANPY_PATH` 或默认路径），启动时 `sys.path.insert` 有全局副作用；
- `_fallback_czsc()` 需要 `czsc`，但 `pyproject.toml` 与 venv 都没有该包 → chan.py 缺失时
  fallback 会 `ImportError`，`chanpy_available()` 的可用性检查不足以兜底；
- `FREQ_MAP` 把 `4h` 映射到 `K_60M`（级别近似错误），且没有 `3m/2h` 键；
- `engine_version` 必须编码 chan.py 提交/校验和 + 上述 config hash，否则信号不可复现。

## 5. 汇总与 4GB / 2 vCPU 判断

微基准方法：`.venv` Python 3.12、pandas 3.0.5、本机 i5-1135G7（4C/8T、16GB），
输入为 `realtime.demo.generate_history()` 的确定性合成 15m bar（**不是**真实行情，只测 CPU），
同一进程内先冷跑一次，再热跑一次；只记录单次耗时，未做 P50/P99。

| 引擎 | 入口 | 最小窗口 | 1800 根实测 | 50 symbols 串行推算 | 状态 |
|---|---|---|---:|---:|---|
| ICT | `analyze`+`build_setup` | 120（建议 ≥1800） | 0.50–0.95 s | 25–48 s | 已接入实时 |
| Ichimoku | `ichimoku` | 78（建议 ≥200） | 0.018 s | ~1 s | 算法已有，adapter 待做 |
| Wyckoff | `detect_wyckoff` | 300（建议 ≥500） | 0.05 s | ~3 s | 算法已有，adapter 待做 |
| Chanlun | `analyze_chanlun` | 30（建议 ≥300） | 1.5–2.4 s | 77–120 s | 算法已有，adapter 待做 |

合计约 2.1–3.4 s/symbol → 50 symbols 约 105–170 s（本机 4C/8T，串行）。
2 vCPU 的 t4g.medium 预计更慢，`needs benchmark`；无论如何都必须离开事件循环并限制窗口。

内存：1800 根 × 50 symbols ≈ 9 万 `Kline`（slots dataclass）；8000 根 × 50 ≈ 40 万。
进程 RSS：空载 133 MB，导入 pandas + chan.py 并跑完一轮后 149–150 MB（不含 4 个 worker）。
进程池方案要先测 «每 worker RSS × 并发 ≤ 700 MB»，否则与 1 GB Python 预算冲突。

结论：
1. Chanlun 是唯一会单独吃掉 bar 周期预算的引擎，必须降频（如仅 1h/4h）或缩短窗口；
2. 四引擎全开时必须进程池 + 每引擎超时 + 放弃「同一根 bar 等全部引擎」的同步屏障；
3. 1D 云窗口（`eval_window=8000`, `BOOTSTRAP_BARS>=8000`）与 HTF
   known-at 已在代码中修正，仍需在目标机用真实数据回归；
4. 本文所有数字只用于容量规划，对外报告必须换成目标机实测的 P50/P99。
