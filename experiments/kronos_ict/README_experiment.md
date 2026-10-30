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

## 首次实跑（BTC，真 Kronos-small，GPU 3090）

```text
环境：192.168.50.81，3×RTX 3090，torch 2.6.0+cu124，Clash 代理 127.0.0.1:7890
数据：BTCUSDT 15m 1000 根，最后收盘 2026-10-30 21:15，close 76892

Kronos 输出（as_of 21:15，horizon 12×15m，n_samples=32）：
  predicted_direction = long
  expected_return     = +0.62%
  confidence          = 0.969（31/32 采样路径同向）
  predicted_high      = 77504
  predicted_low       = 76722

ICT 计划（audit）：direction short，swept=True，conditional
  entry 77866 / sl 78110 / tp 76632

结合结果（injector）：
  strict   -> REJECTED_KRONOS      （Kronos 方向与 short 相反）
  judas    -> REJECTED_KRONOS_TRAP （sweep 后预测反向 = 疑似假突破陷阱）
  momentum -> REJECTED_KRONOS
  target   -> REJECTED_KRONOS
```

结论：ICT 想做空，真 Kronos 预测未来 3 小时上涨（96.9%），过滤器拦截了这次做空。
这正是「软硬结合」的目标场景——避免在 sweep 后追空被假反转套住。

## 复现命令

```bash
# 在 GPU 机上
cd /root/ict && source venv_kronos/bin/activate
export http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890
python run_latest.py                     # 单点最新预测
python run_forecast.py --csv btc_15m.csv --out cache_btc_kronos.csv \
  --repo /root/ict/Kronos --device cuda:0 --n-samples 8 --step 48
```
