# HTF scoring

## Closed bars only

For 15m data, an HTF bar is usable only if its period has closed:

```text
1h: last_htf_bar_start + 1h <= last_15m_time
4h: last_htf_bar_start + 4h <= last_15m_time
1d: last_htf_bar_start + 1D <= last_15m_time
```

Otherwise drop the last HTF bar.

Example (BTC review 2026-10-30 12:45 UTC):

```text
1H raw 12:00 -> use 11:00
4H raw 12:00 -> use 08:00
1D raw 10/30 -> use 10/29
```

## Direction-aware score

```text
long_score  = above cloud + tenkan > kijun + macd > signal
short_score = below cloud + tenkan < kijun + macd < signal
```

Range: 0–3.

`L5-03` uses the plan direction:

```text
long:  long_score >= 2 on 1D and 4H
short: short_score >= 2 on 1D and 4H
```

No plan and trend is 0 → N/A.

## BTC review actual

```text
1D: long 0 / short 2  (inside cloud, TK<, MACD<)
1H: long 0 / short 3  (below cloud, TK<, MACD<)
4H: long 0 / short 2  (below cloud, TK=Kijun, MACD<)
L5-03 short: PASS
```

4H is 2/3 because Tenkan equals Kijun; strict `<` is false.

## Output

```json
"L5-03": {
  "direction": "short",
  "1d": {"long": 0, "short": 2, "bar": "..."},
  "1h": {"long": 0, "short": 3, "bar": "..."},
  "4h": {"long": 0, "short": 2, "bar": "..."}
}
```
