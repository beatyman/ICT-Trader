# 一目均衡表（Ichimoku Kinko Hyo）指标库与回测策略

无未来函数、可实战的一目均衡表实现。

## 公式

```text
MID(n)    = (n 根内最高 high + n 根内最低 low) / 2
Tenkan    = MID(9)               # 转换线
Kijun     = MID(26)              # 基准线
SenkouA   = (Tenkan + Kijun) / 2 # 先行带 A
SenkouB   = MID(52)              # 先行带 B
Chikou    = close 平移（仅信号，不作为输出线）
```

## 偏移规则（关键）

```text
先行带 A/B  只做「显示偏移」：向前 offset 根；数值写在计算它的 bar 上
迟行线      只做「显示偏移」：向后 kijun 根；不参与计算
```

- `leading_a[t] == senkou_a[t - offset]`，`leading_b[t] == senkou_b[t - offset]`。
- 判断当前价格与云区，用 `leading_a[t]` / `leading_b[t]`（即 `[kijun]` 偏移），
  **不是** `senkou_a[t - 2*kijun]`。
- `lookback = max(tenkan, kijun, senkou_b) - 1`（默认 51 根预热）。

### 偏移模式差异

| 模式 | offset | 说明 |
|---|---|---|
| PLATFORM（默认） | kijunPeriod = 26 | 现代平台约定，云区在 26 根后出现 |
| HOSODA | kijunPeriod - 1 = 25 | Hosoda 原版，云区比平台版早一根出现 |

差异：同一根 bar 的「当前云区」值，HOSODA 比 PLATFORM 早一根历史来源，
即云区判断相差一根。两者都不引用未来数据。

## 信号定义

```text
趋势（云区，用 [kijun] 偏移）
  多头   close > max(leading_a, leading_b)
  空头   close < min(leading_a, leading_b)
  云内   介于两者之间 → 中性，不交易
  云色   leading_a > leading_b → 偏多，否则偏空

动能
  TK 金叉 crossover(Tenkan, Kijun) / 死叉 crossunder
  价/基准线金叉 crossover(close, Kijun) / 死叉 crossunder

迟行确认（Chikou 不作为输出）
  多头 close[t] > close[t-kijun] / 空头 close[t] < close[t-kijun]

三役好轉（全部满足）：TK 金叉 + 迟行多头 + 价格上破云区
三役逆轉（全部满足）：TK 死叉 + 迟行空头 + 价格下破云区
```

## 信号分级（映射仓位系数）

| 级别 | 定义 | 系数 |
|---|---|---|
| 强 | 信号在云上（多）/ 云下（空），且云色同向 | 1.0 |
| 中 | 信号在云内 | 0.5 |
| 弱 | 信号逆云方向 | 0（可配 0.25） |

- 云内、薄云（云厚度 < `min_cloud_atr` × ATR）默认暂停开仓；阈值可配。
- 默认禁止短周期（< 4H）开仓，可 `--allow-short-tf` 覆盖。

## 风控与出场

```text
止损      多头 Kijun 下方 或 云区远端（云底）下方；空头镜像；
          取较近/较远可配（stop_mode = near / far）
移动止损  跟随 Kijun 或云区边缘（trail_mode = kijun / cloud / none）
仓位      risk_pct ÷ 止损距离 × 分级系数
出场      反向 TK 交叉 / 收盘穿越 Kijun / 移动止损触发
```

## 参数

```text
默认 9/26/52/26（PLATFORM）
对照 7/22/44、8/22/44、20/60/120
```

## 用法

```bash
# 单次回测
python scripts/ichimoku_backtest.py backtest --csv data/btc_daily.csv \
  --tenkan 9 --kijun 26 --senkou-b 52 --offset PLATFORM \
  --timeframe 1d --risk-pct 0.01 --json-out output/ichimoku_metrics.json

# 参数扫描（样本内/外）
python scripts/ichimoku_backtest.py scan --csv data/btc_daily.csv \
  --timeframe 1d --json-out output/ichimoku_scan.json
```

## 验收测试

```bash
PYTHONPATH=src python -m pytest tests/test_ichimoku.py -q
```

## 已知限制

1. 回测为单标的、无杠杆、按日收盘强平未平仓头寸。
2. 未建模滑点/手续费；实盘需在绩效上叠加成本。
3. 薄云阈值为 ×ATR 相对值，不同品种需各自校准。
4. 参数扫描未做 walk-forward 优化，仅作对照，避免过拟合。
5. Chikou 未输出为线；需要画图时可用 `close.shift(-kijun)` 自行构造。
