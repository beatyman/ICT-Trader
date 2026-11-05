# 自动分析工具 README（ICT 主 + Wyckoff/Ichimoku 辅）

每 5 分钟分析一次的多方法论自动分析系统。ICT 是唯一入场信号源；
Wyckoff/Ichimoku 只做方向过滤、否决与仓位缩放。

## 主辅边界（硬性）

```text
ICT      决定「做不做、在哪做、何时做」→ 输出 entry/stop/tp1/tp2/invalidation
Wyckoff  只输出 allow/veto/size_factor（4H/1D 背景）
Ichimoku 只输出 allow/veto/size_factor（4H/1D 背景）
辅助策略不得覆盖 ICT 的入场触发；veto 硬性生效，ICT 高分不能覆盖。
```

## ICT 信号定义表（模块3）

| 步骤 | 条件 | 输出 |
|---|---|---|
| 1 结构方向 | 1H/15m BOS/CHoCH（L5-03） | htf_alignment |
| 2 流动性扫单 | 5m 前高/前低被扫后收回（L2-04） | has_sweep |
| 3 结构转变/位移 | MSS / 强动能（L1-04 或 L3-10） | has_mss/displacement |
| 4 回踩 OB/FVG | 折价区（做多）/ 溢价区（做空） | premium_discount |
| 5 触发 | 突破触发 K 线极值 或 收盘确认 | entry |

方向：bull sweep → long（需在 discount）；bear sweep → short（需在 premium）。
止损：扫单极值 ± 0.1 ATR；TP1 = 2R；TP2 = 3R。
约束：未确认扫单不赌；结构不明不发信号。

## Wyckoff / Ichimoku 背景输出表（模块2）

| 字段 | Wyckoff | Ichimoku |
|---|---|---|
| bias / cloud | long/short/neutral | above/below/inside |
| 状态 | range_state + phase A-E | cloud_color + thickness + tk + kijun |
| allow | allow_long/allow_short | allow_long/allow_short |
| size_factor | 0–1 | 0–1 |

veto 条件：
- Wyckoff：区间内、薄区间、阶段 B（不提前断定）。
- Ichimoku：云内、云色冲突、薄云。
- 迟行线：close 与 close[26] 比较（不是「迟行线穿当前 K」）。
- 云判断：leading_a[26]/leading_b[26]，不是 [52]。

## 融合规则表（模块4）

| 情况 | 结果 |
|---|---|
| ICT 无信号 | 不交易 |
| ICT 有信号 + 辅助 veto | 不交易（硬性） |
| 两者方向一致 | size=1.0，置信度上调 |
| 一方 neutral 一方一致 | size=0.7 |
| 两者 neutral | size=0.5 |
| 冲突（ICT 多 + Wyckoff 派发/Ichimoku 云下） | veto |

size_factor = min(辅助 size) × scale × (ICT 置信度/100)。

## 评分 Schema（模块5）

```json
{
  "timestamp": "...", "symbol": "...",
  "ict": {"direction","confidence","entry","stop","tp1","tp2","invalidation",
          "setup_type","htf_alignment","premium_discount"},
  "wyckoff_background": {"bias","range_state","phase","allow_long","allow_short","size_factor"},
  "ichimoku_background": {"cloud_state","cloud_color","cloud_thickness","tk_state","kijun_state","allow_long","allow_short","size_factor"},
  "fused_direction": "long|short|neutral",
  "fused_confidence": 0-100,
  "size_factor": 0-1,
  "score": -100..100,
  "score_breakdown": {"ict_setup_quality":0-40,"liquidity_and_structure":0-20,
                      "wyckoff_alignment":0-15,"ichimoku_alignment":0-15,
                      "risk_reward_quality":0-10},
  "action": "enter|wait|avoid",
  "entry","stop","tp1","tp2","invalidation","reasoning","red_flags"
}
```

评分规则：ICT 质量占大头（0-40 + 0-20）；Wyckoff/Ichimoku 只做加分或否决；
veto/冲突/云内/区间内/逆背景/扫单未确认 → 扣分（red_flags，每条 -10）。

## 反馈评分（模块6）

```text
方向正确：TP2 +2；TP1 +1；未达 TP1 +0.5。
方向错误：正常止损 -1；亏损>止损距离按比例加重（最高 -3）；保本/小亏 -0.5。
附加：逆背景 -1；云内/区间内 -1；veto 仍入场 -2；无失效条件 -1。
```

反馈 DB 字段（TradeRecord）：
time / symbol / direction / entry / stop / tp1 / tp2 / invalidation /
confidence / score / size_factor / setup_type / wyckoff_bias / ichimoku_cloud /
session / exit_price / exit_reason / pnl / direction_correct / feedback / penalty_flags

## 归因与优化（模块7）

每 50 笔或每周归因：
1. 分组统计：ICT 单独、ICT+Wyckoff、ICT+Ichimoku、三者组合的胜率/PF/期望/回撤。
2. 重点检查：扫单未确认、veto 被绕过、云内/区间内交易、止损过紧/过宽、时段不适、size 与盈亏关系。
3. 输出「问题假设 → 验证方法 → 参数/规则调整 → 回测对比」。
4. 每次只改少量变量；保留样本外验证；记录版本号 + 前后绩效。

## 参数

```text
risk_pct 0.01（单笔风险）；min_rr 2.0（TP1）；warmup 300；step 1（5m/15m）
Wyckoff min_range_atr 2.0；Ichimoku thin_cloud_atr 0.5
score 阈值：enter 需 score ≥ 60
```

## 局限与主观判断点

1. 成交量：crypto tick volume 非真实量（原文局限），港美股更可信。
2. Wyckoff 事件是「被验证的假设」，高潮/量能递减是相对描述 → 阈值代理。
3. 大模型评分在本实现用确定性函数替代（无 LLM 运行时）；Schema 相同，可替换为 LLM 调用。
4. 5 分钟循环的 scheduler 未实现（本环境无常驻进程）；live 模式是「一拍」。
5. 回测为单标的、无滑点/手续费；短周期（15m）信号稀少，推荐日线/H4。

## 如何避免未来函数

```text
- 每个决策点只用 [0:t] 数据（df.iloc[:i+1]）
- Ichimoku leading_a[t] = senkou_a[t-26]（显示偏移，不引用未来）
- 迟行线确认用 close[t] vs close[t-26]
- Wyckoff 状态机只用已收盘 bar；pivot 确认窗口内不判定
- 入场在信号 bar 收盘后（stop_order 代理 / 收盘确认）
```
