# 量化交易系统全链路架构师 · ICT-Trader 优化提示词（已填真实上下文）

> 本文件是「写一个提示词」的产物：把通用架构师提示词的 {{占位符}} 换成
> ICT-Trader 的真实状态，可直接交给子代理或作为优化执行清单。
> 执行产物见 `src/ict_trader/schema.py`（Output Schema + 自洽校验 + signal_id）、
> `src/ict_trader/ai_prompt.py`（三段式提示词 v2）。

## Context（真实项目状态）

- **项目形态**：Python 单仓库（pandas / numpy / matplotlib），`src/ict_trader/` + `scripts/`。
- **模块清单**（src/ict_trader/，22 个）：
  concepts / cisd / po3 / amd / fractal / ict_extras / ichimoku / wyckoff /
  wyckoff_quality / chanlun / cyq / vp_anatomy / patterns / adaptive_rsi /
  synapse / camarilla / orderflow / trendlines / structured_plan / confluence /
  fusion / trade_risk / trade_decision / timemachine / ai_prompt / schema
- **信号输入格式**：`structured_plan.build_input_from_data(df_15m, df_4h, df_daily, symbol, market, df_1h)`
  → input JSON（`ict_trader_15m` / `wyckoff` / `ichimoku` / `chanlun_htf` /
  `regime` / `levels` / `orderflow` / `momentum` / `profile_shape` / `patterns` / `metadata`）。
- **信号输出格式**：`structured_plan.structured_plan(inp)` → output JSON
  （`meta` / `long_setups[]` / `short_setups[]` / `no_trade_conditions[]` /
  `data_gaps[]` / `metadata` / `risk_notes[]`）。
- **AI 提示词**：`src/ict_trader/ai_prompt.py`（原为单段 `_SYSTEM` + JSON dump）。
- **图表渲染**：`scripts/plot_ict_clean.py`（matplotlib，从 concepts.py 的 detectors 直接画，
  与 structured_plan 输出无 signal_id 关联）。
- **已知痛点**：
  1. AI 输出不稳定：无 JSON 解析失败降级、无重试、无版本号。
  2. 图与信号对不上：渲染层不读 structured_plan 输出，无 signal_id 关联。
  3. 字段漂移：input/output 是裸 dict，无 JSON Schema / Pydantic 校验。
  4. `build_input_from_data` 是单体函数（~500 行），耦合数据/特征/信号三层。
  5. 无自洽性硬校验（多空价位顺序、confidence 范围、rr 实数计算）没人管。
- **约束**：保留 `structured_plan(inp)` 与 `build_ai_prompt(inp)` 接口；不引入重依赖；
  输出纯 JSON 可被 `structured_analyze.py` / `trade_decision.py` 直接消费。

## Task（A→E，先诊断后方案）

- **A 策略代码优化**：分层诊断（数据/特征/信号/AI/渲染/校验），目标架构，重构骨架，
  前视/过拟合/幸存者偏差风险点，单测清单。
- **B Input Schema**：meta + ict_15m + wyckoff + ichimoku + chanlun_htf 四子块，
  字段/类型/必填/缺失语义，JSON Schema + 校验规则。
- **C Output Schema**：setup 必含 id/status/setup_type/entry_zone/entry_trigger/sl/tp/
  confidence/invalidation/rationale/signal_refs/risk_check；自洽硬约束
  （long: sl<entry<tp，short: tp<entry<sl，confidence∈[0,100]，rr 实数计算）。
- **D AI 提示词**：System/User/Output Contract 三段；失败降级（解析失败→重试→neutral）；
  幂等（temperature=0/seed/prompt_version）；5 个边界 case。
- **E 图信号匹配**：signal_id 规范 + 图元契约 + 双向一致性校验 + 验收清单。

## Output Format

[0] 现状诊断摘要（严重/中等/轻微）
[1] 目标架构（目录级 + 分层职责）
[2] Input Schema + 校验规则
[3] Output Schema + 自洽约束 + 合法/非法示例
[4] 关键重构代码骨架
[5] 优化后 AI 提示词全文（System/User/Output Contract）
[6] 图信号匹配规范 + 校验流程 + 骨架 + 报告结构
[7] 回归测试清单（含 5 边界 case）
[8] 需补充信息
[9] 风险提示

## Constraints

不编造字段；Schema 带 schema_version；改动可回滚；不承诺盈利；
发现结构性缺陷直接指出；一次只改一层。
