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
