# POI scope

`L3-01` / `L3-02` / `L3-09` must not count every active FVG/OB in history.

## Current setup range

```text
entry_ref = plan.entry if plan else last_close
poi_lo = entry_ref - 0.5 * ATR14
poi_hi = entry_ref + 0.5 * ATR14
time_lo = n - 60
```

Relevant:

```text
active == true
created >= time_lo
zone.top >= poi_lo
zone.bottom <= poi_hi
```

Fresh:

```text
relevant FVG and fresh == true
```

## BTC review (2026-10-30)

Conditional entry 78,700, ATR 256.5:

```text
old: 263 FVG / 8 OB / 261 fresh
new: 1 FVG / 0 OB / 1 fresh
```

No-plan current price 77,435:

```text
new: 3 FVG / 0 OB / 3 fresh
```

GS-06 uses either FVG or OB; it still passes when one relevant POI exists.


## Touch semantics

Three possible meanings:

```text
current:    current price inside zone ± tolerance
historical: any bar after creation touched the zone
overlap:    current 15m bar range overlaps the zone ± tolerance
```

Default:

```text
touch_semantics = current
```

Reason: L2-08 asks whether price is at the PDA now. "Historically touched"
is a separate mitigation field, not the current entry condition.

Current review:

```text
current + htf: touch=true
current + ltf: touch=false
historical:    touch=true
overlap:       touch=true
```

`historical` is why a naive implementation can make L2-08 PASS on almost any
old PDA. It is not the default.

## Touch ATR source

Default: `touch_atr_source = htf`.

```text
htf: touch / delivery tolerance uses the HTF ATR
ltf: uses the execution-timeframe ATR
```

Reason: an HTF PDA is an HTF object; its touch tolerance should scale with
HTF volatility. `--touch-atr-source htf|ltf` is exposed in the audit CLI.

## DOL freshness

`L2-07` ignores a pool created inside the last 20 bars:

```text
known_at < n - 20
```

Otherwise the current swing extreme would be selected as a draw, which is
already the price being traded.
