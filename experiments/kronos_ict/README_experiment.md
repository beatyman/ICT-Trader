# Kronos × ICT-Trader 联合实验

## 目标

把 Kronos（Transformer K 线模型，黑盒概率）作为 ICT-Trader（GS/L 规则，逻辑确定）
的**软条件**叠加，而不是替换：

```text
ICT 决定「在哪里做 / 方向」（硬条件，Sweep→MSS→CISD→FVG→DOL）
Kronos 决定「现在是不是好时机」（软条件，方向概率 / 假突破过滤）
```

两者取交集，Kronos 只输出 `predicted_direction / confidence / expected_return`，
**不输出价格用于画图**。

## 隔离原则

```text
experiments/kronos_ict/   # 本实验，独立包
external/Kronos/          # 克隆的模型仓库（gitignored）
src/ict_trader/           # 主引擎，绝不修改
```

Kronos 的预测作为新的输入特征注入，不改 GS/L 检测器。离线批量生成预测并缓存
到 `cache/*.csv`，回测循环里不做实时推理。

## 目录

```text
experiments/kronos_ict/
├── adapter.py          # Kronos 封装（唯一 import Kronos 的地方）
├── injector.py         # filter_plan_with_kronos：纯函数软门
├── run_backtest.py     # 基线 vs 三种模式对比
├── cache/              # 离线预测 CSV
└── README_experiment.md
```

## 三种假设

| 模式 | 假设 | 逻辑 |
|---|---|---|
| A · Judas Filter | Sweep 后易被骗（假反转） | Kronos 预测价格回到扫荡起点 → `REJECTED_KRONOS_TRAP` |
| B · Momentum Boost | CISD 后有时缺动能 | Kronos 预测同向大幅移动 → `conditional` 升级 `triggered` |
| C · Target Tune | DOL 往往太远 | TP = min(ICT DOL, Kronos 预测极值) |

## 快速验证（无需 GPU）

`adapter.py` 提供 mock 模式（朴素动量桩），用于端到端跑通管线：

```bash
cd experiments/kronos_ict
python adapter.py                # 生成 cache/_smoke.csv
python injector.py               # 验证 filter_plan_with_kronos 三种状态
python run_backtest.py --csv ../data/15m.csv --plans-json plans.json --forecasts-csv cache/forecasts.csv
```

## 接入真实 Kronos

```bash
git clone https://github.com/shiyu-coder/Kronos.git ../external/Kronos
cd ../external/Kronos && pip install -e .
# 然后在 adapter 中删除 enable_mock()，确认 _call_kronos 的 API 与仓库 README 一致
```

## 评估指标

```text
总交易数、胜率、净 RR（Net Profit / Gross Loss）、假突破占比
```

关注三点：

```text
1. 交易数是否被过度过滤（模式 A 最容易把好信号也滤掉）
2. 胜率是否显著提升
3. 净 RR 是否改善（胜率提升但单笔盈利下降 = 净 RR 未必变好）
```

## 过拟合警示

```text
训练/验证集必须与测试集时间隔离
建议：前 6 个月零样本推理或微调，后 3 个月测试
Kronos 可能学到数据噪声，不要用同一段数据调阈值又验证
```

## 结论（待跑）

```text
（跑完回测后填写，例如：
模式 A 过滤了 30% 的假信号，胜率 +Xpp，但总盈利 -Y%；结论是否值得上线）
```
