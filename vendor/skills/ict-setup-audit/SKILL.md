---
name: "ict-setup-audit"
description: "Socratic self-audit of an ICT liquidity-sweep trade setup against the core rules: sweep must take swing-point liquidity, then produce a violent reaction and a direct reversal, and the target must be the opposite unswept liquidity pool (FVG is only a delivery phenomenon, never the reason). Trigger phrases: 'audit this setup', 'check this trade', 'does this setup hold up', '审一下这个单', '这套逻辑成立吗'. Any failed question -> NO_TRADE."
---

# ICT Setup Audit（流动性清扫入场自审）

Use the Socratic method to interrogate a proposed ICT trade setup. Do NOT lecture;
ask the setup one hard question at a time and let the evidence answer. The default
verdict is **NO_TRADE** — a setup must actively prove itself, or it is rejected.

## Core thesis（这套体系的精髓）

```text
入场触发 = 价格清扫了「摆动点的流动性」(swing low/high 的止损单)
        → 出现剧烈反应（位移）
        → 直接反转（MSS）
目标     = 对侧「未清扫的流动性池」
FVG      = 价格扫完流动性后「路过」的地方（现象），不是目的
```

Any setup that treats FVG as the entry reason, or that sweeps a level with no
violent reaction + direct reversal, is a false setup.

## The four hard questions（任一答否 → NO_TRADE）

### Q1. 这个 sweep 扫的是「摆动点流动性」吗？

- 被扫的价位必须是 swing low（多头入场）/ swing high（空头入场）——那里有真实止损单堆积。
- 如果扫的是「中间随机价位」或「自己画的线」，不是有效流动性 → 否。
- 标的本身流动性是否充沛？BTC 排除（其「清扫」经常是假扫，历史胜率 <20%）。

### Q2. sweep 之后有「剧烈反应」吗？

- 位移实体 ≥ 1.5×ATR（剧烈），且伴随放量（RVOL ≥ 1.5）。
- 如果 sweep 后价格慢慢磨、无大实体、无量 → 否（这不是「拿流动性」，是「磨盘」）。

### Q3. 直接反转了吗？

- sweep 后 1-3 根内出现 MSS（结构反转确认），不是慢慢漂。
- 如果 sweep 后价格继续顺着原方向走，或很久才回头 → 否。

### Q4. 目标是对侧「未清扫的流动性池」吗？

- TP 必须指向对侧最近未扫的 BSL/SSL（或 DOL/日线目标价）。
- 如果 TP 拿的是「FVG 中点」「斐波那契回撤位」这类假目标 → 否。

## FVG 定位（防止本末倒置）

- 只有当一个 FVG 区间内「叠着一个已被扫的摆动点」时，它才值得标记为 valid。
- 其余 FVG 一律忽略：不进 score、不进 rationale、不做入场理由。
- 价格进入 FVG 是「现象」——它只告诉你价格扫完流动性后往哪交付，不是入场信号。

## Verdict

```text
Q1-Q4 全过 + 净 RR ≥ 2.0 → 可执行（记录 signal_refs + 证据分类）
任一答否 → NO_TRADE，并说清是哪一问没过（如 "Q2 位移仅 0.6×ATR，无剧烈反应"）
```

## Evidence classification

Every claim must be tagged `observed`（引擎实测）/ `derived`（由实测计算）/
`interpretive`（你的判断）/ `missing`（缺数据）。不得把 interpretive 说成 observed。
