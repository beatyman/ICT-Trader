# ICT setup checklist

The enforceable sequence is:

```text
1. Daily bias
2. Draw on liquidity (target)
3. Kill zone
4. Structure confirmation (MSS / CHoCH / BOS)
5. PD array entry (OB / FVG)
6. RR gate at the structural stop
```

## Hard conditions

| # | Condition | Implementation |
|---|---|---|
| 1 | 1D bias aligned | `stack=1d_1h_15m`, `min_1d_score=3` |
| 2 | External liquidity target | `target_mode=dol` (or fixed 2R as a fallback) |
| 3 | Session / kill zone | `session_gate=auto|hk|ny_am|silver_bullet|...` |
| 4 | Structure event | CISD in the bias direction |
| 5 | PD array | `origin_filter=fvg` or `require_pda_array` |
| 6 | RR ≥ 2 from the structural stop | `min_target_rr=2.0` |

The best research candidate currently uses 1, 4, 5, 6 and fixed 2R; it does **not**
enforce 2/3 yet.  That is why the DOL variant matters.

## Draw-on-liquidity mode

```bash
--target-mode dol --min-target-rr 2.0 --max-target-rr 6.0
```

`dol` chooses the nearest unswept liquidity level *known at the signal bar* whose
distance from entry is at least `min_target_rr`.  It skips the trade if no such pool
exists.  This replaced an earlier bug where key levels that were swept later in the
file were filtered retroactively; `broken_at`/`sweep_bar` now make the filter
point-in-time correct.

## Measured effect

| instrument | fixed 2R | DOL |
|---|---:|---:|
| Xiaomi HK.01810 | 30 trades, +0.132R | 15 trades, **+0.255R** |
| Zijin HK.02899 | 26 trades, -0.009R | 10 trades, **+0.137R** |
| 8-case basket | 45 trades, +0.384R | 29 trades, +0.037R |

DOL improves the two HK names but is much worse on the crypto-heavy basket.  It is an
optional target model, not a default.

## Kill zone caveat

The best candidate currently has `session_gate=none`.  For HK cash equities all bars
are already inside the cash session, so this does not change the HK tests; for crypto
it removes the 24h assumption.  A kill-zone filter must be tested separately, not
assumed to improve the result.
