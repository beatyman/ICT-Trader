---
name: ict-corpus-distilled
description: ICT 语料蒸馏出的可执行规则：Dealing Range / Premium-Discount / OTE / OB / FVG / Sweep。用于把 ICT_Corpus 方法论映射到 src/ict_trader，不生成 AI 点位。
version: 0.1.0
---

# ICT Corpus Distilled

## Scope

Source corpus: `C:\zhangxin\workerspace\ICT_Corpus` (819 docs).

Distilled from:
- doc 601 `liquiditysweeps.com` — ICT Dealing Range
- doc 556 `innercircletrader.net` — Order Block
- doc 491 `innercircletrader.net` — Fair Value Gap
- doc 546 `innercircletrader.net` — Liquidity Sweep vs Liquidity Run
- doc 233 `theinnercircletraders.com` — FVG vs Order Block

## Deterministic rules

### Dealing Range

```text
CE = (high + low) / 2
Q1 top = (low + CE) / 2
Q3 bottom = (high + CE) / 2

above CE = premium
below CE = discount
Q1 = deep discount
Q4 = deep premium
```

### OTE

```text
long  = high - 0.79..0.62 * (high-low)
short = low  + 0.62..0.79 * (high-low)
```

### Order Block validation

```text
bullish OB:
  last bearish candle before bullish impulse
  impulse engulfs body and wick
  lower-TF FVG inside/adjacent
  lower-TF MSS in impulse direction

bearish OB: mirror
```

### FVG

```text
bullish FVG: low[3] > high[1]
bearish FVG: high[3] < low[1]
body close through far edge = invalidated
```

### Sweep

```text
BSL: high > level and close back below
SSL: low < level and close back above
reclaim within N bars; no reclaim = break
```

## Code mapping

```text
src/ict_trader/realtime/analysis/dealing_range.py
  DealingRange
  valid_ob

src/ict_trader/realtime/analysis/pda_confluence.py
  overlap_ratio
  annotate_confluence
  body_close_invalidated

src/ict_trader/concepts.py
  detect_fvgs / detect_order_blocks / detect_sweeps

src/ict_trader/realtime/analysis/candidate_builder.py
  candidate evidence
```

## Non-goals

- Does not create Setup.
- Does not change ICT entry/SL/TP.
- Does not call broker.
- Does not make AI approve/reject.
