# ICT Playbook

## Key levels

| Term | Meaning |
|---|---|
| BSL | buyside liquidity, usually a swing high / equal highs |
| SSL | sellside liquidity, usually a swing low / equal lows |
| PDH / PDL | previous day high / low |
| PWH / PWL | previous week high / low |
| Asia.H / Asia.L | Asia session high / low |
| London.H / London.L | London session high / low |

A level has a `known_at` bar. It is not usable before that bar.

## Liquidity sweep

A sweep is a wick through a level followed by a close back through it:

```text
BSL: high > level and close < level
SSL: low  < level and close > level
```

A close through the level without reclaim is a **break**, not a sweep.

Sweep strength:

```text
1 = reclaim
2 = reclaim + volume >= 1.2x
3 = reclaim + volume + penetration >= 0.5 ATR
```

## FVG / IFVG

FVG:

```text
bull: low[i] > high[i-2]
bear: high[i] < low[i-2]
```

Quality gate:

```text
gap >= 0.25 ATR
impulse body / range >= 0.50
```

IFVG:

```text
bull FVG later closes below its bottom -> bearish IFVG
bear FVG later closes above its top    -> bullish IFVG
```

A wick through is not a flip.

## Order block

An OB is the last opposite-colour candle before a displacement leg that breaks
structure. Only fresh, unmitigated OBs are candidates.

## CISD

CISD = change in state of delivery.

```text
price closes back through the open-extreme of the prior delivery leg
```

The engine uses a Pine-style state machine:

```text
cm_legExtremeCurrent
cm_legExtremePrevious
pivot update only on new high/low
close through level = CISD
```

## The frozen setup

```text
1D  Ichimoku cloud + Tenkan/Kijun + MACD = 3/3
4H  Ichimoku cloud + Tenkan/Kijun + MACD >= 2/3
15m CISD long
     + sweep in last 15 bars
     + FVG/IFVG/OB PDA
     + displacement quality
     + weighted score >= 0.55
```

Entry: next 15m open.  
Stop: CISD invalidation wick.  
Target: 2R by default.  
Risk: 1% per trade, one position per symbol.

## Quality score

```text
1D            0.15
4H            0.10
CISD grade    0.20
sweep         0.10
PDA           0.15
displacement  0.15
volume        0.10
session       0.05
```

## What invalidates a plan

- 1D close back below the cloud;
- 4H close back below the cloud;
- 15m close through the CISD stop;
- the sweep level is reclaimed in the opposite direction;
- data window too short to compute the 1D cloud.

## Sample-size rule

```text
< 20 trades: observation only
20-50: review, do not optimize
50-100: may change one parameter
> 100: walk-forward gate
```
