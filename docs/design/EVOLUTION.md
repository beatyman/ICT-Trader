# 指标、版本、影子、A/B、回滚与自进化

> 现状：只有进程内 `metrics.summarize_trades()`（trades/wins/losses/win_rate/profit_factor/
> avg_r/net_pnl）与 `metrics.py` 的 P50/P99 环；`strategy_versions` 表已建但**没有写入方**，
> 没有 shadow/A-B/回滚/审批流程。`stats.py` 的 Wilson/样本量函数、`walk_forward.py` 的
> purged walk-forward、`feedback.py` 的归因与问题发现是**研究层**工具，未接实时库。
> 本章定义目标口径；未实现处明确标注。

## 1. 指标定义（全部 UTC，按 closed setup 统计）

| 指标 | 定义 | 口径与陷阱 |
|---|---|---|
| 胜率 | `wins / n`，`win = pnl > 0`（不含手续费前） | 必须给 Wilson 95% 区间：`stats.wilson_interval(wins, n)` |
| Profit Factor | `gross_win / gross_loss` | `gross_loss == 0` 时**未定义**；现有 `summarize_trades` 返回哨兵 `99.0`，报告中必须显示 `n/a`，不得参与目标函数 |
| 期望 | `mean(r_multiple)`（每笔）与 `mean(pnl)`（每笔 USDT） | 同时报 `n`、标准差与均值区间 `stats.mean_interval()`；R 与 USDT 不可混用 |
| 最大回撤 | 峰谷回撤：`max(peak - equity) / peak` | 两个口径都报：已平仓权益曲线、含浮盈的 mark-to-market；注明采样频率 |
| Sharpe | `mean(daily_ret) / std(daily_ret) * sqrt(365)`，rf=0 | 需要 ≥30 个 UTC 日收益；按笔计算会因间隔不均失真，不采用 |
| Sortino | 同上，分母只用下行标准差 | 样本 <30 日同样不报 |
| 引擎准确率 | ①信号→Setup 转化率；②信号精度 = 该引擎主导的 setup 中 TP 先于 SL 的比例；③校准 = 平均置信度 vs 实际命中率（Brier） | 单引擎样本少，必须带 `n` 与区间；不得把共振 setup 的胜负归给单个引擎 |
| 执行滑点 | `slippage_bps = (actual_fill - planned_price)/planned_price * 10000 * side`，买入为正表示更差 | 入场/出场分开；paper 模型本身固定 10 bps 往返佣金 + 2 bps/次滑点，实测值要与模型对比 |
| AI 质量 | verdict 分布 × 实际 R；schema 有效率；降级率；P50/P95 延迟；token/成本 | AI 只做审查，不能把「AI 通过」当成胜率来源；观察性对比必须标注非因果 |
| 系统质量 | 引擎超时率、`loop_lag_ms` P99、WAL pending、CH batch P99、`degraded_to_rules` 率 | 与策略指标同表展示，避免只看收益不看可运行性 |

所有指标查询必须带过滤：`strategy_version`、`engine`、`symbol`、`interval`、时间窗、
`is_sim = 0`。**SIM/demo 数据禁止进入任何绩效结论**（当前 signals/setups 表还没有 `is_sim` 列，
见 §6）。

## 2. 样本量（先算再说话）

`stats.py` 已实现三个工具，直接复用：

- `n_for_margin(p=0.5, margin)`：±5% → 385 笔；±10% → 97 笔；±3% → 1068 笔；
- `n_to_detect(p1, p2)`：45% → 55% 需要每臂 392 笔（α=0.05，power=0.8）；
- `wilson_interval(wins, n)`：20 笔 60% 胜率的区间约 39%–78%，不能据此下结论。

规则：
1. 样本 <100 笔只报点估计 + 区间 + 「不足」标记，不做版本 promote；
2. 同一版本的分组对比超过 20 个格子时，按 exploratory 处理（多重比较），
   或预先声明主指标（例如只按 engine 分组）；
3. 参数搜索必须走 `walk_forward.purged_walk_forward(folds=5, embargo_days=5)`，
   禁止在全样本上挑阈值再报告同一段样本；
4. 每份报告写明数据区间、`n`、区间、是否含 SIM、未包含的成本（资金费、借币、冲击成本）。

## 3. strategy_versions 生命周期

状态机：`draft → shadow → active → retired`，另有 `rejected`（人工否决）。
每次变更必须写一行（表已存在，写入方待实现）：

- `strategy_version`：不可变 ID（如 `sv-0007`），与 `setups.strategy_version` 对应；
- `config_json`：完整参数快照（SetupConfig + Settings 中影响信号的字段）；
- `config_diff`：相对父版本的 JSON diff（当前表无此列，需新增或复用 `notes`）；
- `engine_versions`：四引擎的 `engine_version` 组合（含 chan.py 校验和）；
- `checksum`：`config_json` 的 sha256，用于运行时校验「配置未被静默修改」；
- `promoted_at`：上线时间；**审批人当前无列**，需新增 `approved_by/approved_at` 或独立审批表。

硬规则：运行时只读 `active` 版本；任何阈值/权重变更必须产生新版本；
禁止在线改内存里的配置；`ReplacingMergeTree(ingested_at)` 只保证同版本最新行，
不保证「不可变」，不可变由应用层拒绝写入已 `active` 版本实现。

## 4. Shadow 与 A/B

Shadow（影子）：
- 全流程照跑（引擎→共振→规则→Setup→TP/SL 模拟），但**不经过 broker**、不占仓位、
  不触发风控熔断；
- 存储必须与线上隔离：当前 9 张表**没有 `is_shadow` 列**，需新增列或使用独立
  `shadow_` 前缀表；否则影子结果会污染线上胜率（这是 P4-3 的前置 DDL 变更）；
- 影子至少运行 2 周且 ≥100 笔，才允许进入 promote 评审。

A/B：
- 分流单位用 **symbol 或连续时间块**，不要在同一根 bar 内随机分（币种高度相关，会串味）；
- 两臂使用同一时间窗、同一数据源、同一手续费/滑点模型；
- 样本量用 `n_to_detect(p1, p2)` 事先算；两臂都需 ≥ 该值；
- 报告必须包含「未达样本量」或「已按序贯检验提前停止」的说明；
- AI 参与与否的 A/B 需要随机化分配，当前只有观察性数据 → 只能写相关性，不能写因果。

## 5. 回滚与人工审批

回滚：
- 版本指针单点可切（配置/DB 一行），目标 <1 分钟，不需要重启进程（读取端每次决策前取缓存版本）；
- 回滚时对已存在的 open setups 必须显式选择：`manage_to_close`（旧版本继续管理到平仓）
  或 `flatten`（立即按市价平），并把策略写进 `notes` 与 `error_events`；
- 回滚演练每季度一次，记录在 `strategy_versions`。

人工审批（不可被自动化绕过）：
- 触发条件：新增版本、修改风控参数（`risk_pct`、`max_open_positions`、
  `max_daily_loss_pct`、`allow_short`）、启用真实交易相关开关、修改 AI prompt；
- 审批人：至少一名非作者；风控参数需双人（4-eyes）；
- 审批证据：shadow/A-B 报告、`n` 与区间、最大回撤、错误率、回滚演练结果；
- 否决也写库（`status="rejected"` + 理由），保留审计链；
- kill switch：`halt_new_setups` 运行时开关，打开后规则仍评估、Setup 仍记录但不执行，
  动作写 `error_events(category="risk")`。

## 6. 目标函数（明确、可复现）

自进化不是「把胜率调高」，而是在约束下最大化单位风险期望：

```text
maximize    E[R] = mean(r_multiple over closed setups)
subject to  max_drawdown_pct        <= DD_MAX        (策略默认 15%，非拟合值)
            profit_factor           >= 1.20          (且 PF 的 bootstrap 下界 > 1.0)
            n_closed                >= 100
            engine_timeout_rate     <= 2%
            degraded_to_rules_rate  <= 20%（AI 开启时）
            daily_ai_cost_usd       <= budget
tie-break   turnover（越少越好），再比 AI 调用次数
```

报告里同时给 `E[R]` 的均值区间；如果区间跨 0，结论必须是「无证据」。
禁止用胜率作为唯一目标（高胜率低盈亏比会通过），禁止在样本不足时做多参数搜索。

## 7. 报告与审计

- 日报：当日 setups 数、TP/SL/超时退出、滑点、错误 Top5、AI 状态与成本、引擎 P50/P99；
- 周报：按 engine/symbol/interval 的胜率+Wilson、PF、期望、回撤、版本对比；
- 每份报告固定声明：样本量、区间、SIM 是否剔除、未建模成本、数据缺口；
- 所有数字来自 ClickHouse 查询（可复跑 SQL），不允许手工填写；
- 归因可复用 `feedback.attribution()` / `feedback.discover_problems()` 的思路，
  但输入必须是 DB 中的真实平仓记录。
