# Evidence and output contract

This contract is intentionally stricter than “the setup looks good”. It exists so a
reader can tell **what the engine observed**, **what was inferred**, and **what is
missing**.

## 1. Data provenance

Every analysis must record:

```text
source            broker / exchange / CSV / Futu / Binance
as_of             timestamp of the last CLOSED bar used
timezone          IANA zone used for session logic
bars              number of bars
data_verified     true / false / unknown
synthetic         true if bars were generated or replayed as a fixture
```

Rules:

- Never treat a forming bar as closed.
- Never silently replace missing bars, forward-fill prices, or change the timezone.
- Never infer numeric OHLCV from a screenshot. Ask for the CSV/JSON or a data source.
- A synthetic demo is labelled `synthetic: true` and cannot be presented as live evidence.

## 2. Evidence classes

Every material statement is tagged:

| Class | Meaning | Example |
|---|---|---|
| `observed` | direct engine output with a bar timestamp | sweep at bar 412, level 71.50 |
| `derived` | deterministic calculation from observed data | RR = 2.4, ATR = 0.38 |
| `interpretive` | a judgement or narrative | “this looks like a Judas swing” |
| `missing` | not available / not verified | no volume, no 1D history, no closed bar |

`interpretive` statements may be useful, but they never become `observed` by repetition.

## 3. Setup state

Use exactly one final state:

```text
confirmed    required evidence present, closed-bar confirmation, net RR >= 2
developing   valid premise but one or more confirmations still missing
invalidated  the original invalidation level/condition was hit
no-trade     conflict, stale signal, missed entry, missing data, or net RR < 2
```

`confirmed` requires all of:

- higher-timeframe bias and draw on liquidity;
- a meaningful sweep (not just a wick);
- displacement / CISD or MSS confirmation on a closed bar;
- a valid PD array or retest;
- stop at the invalidation point, target at a real opposing pool;
- **net** RR (after fees/slippage) >= 2;
- entry still reachable (see `entry_distance_atr`).

If any item is missing, the state is `developing` or `no-trade`, not `confirmed`.

## 4. Plan fields the skill must read

`ict-trader plan` emits:

```text
decision              TRADE / NO_TRADE
rejected              null / price_too_far_from_entry / price_outside_entry_risk
entry_distance_atr    distance from current price to the entry, in ATR
setup.r_multiple      target distance / stop distance
setup.quality         0..1 weighted score
setup.checks          sweep / pda / volume / session / targets / smt
```

Rules:

- `rejected != null` means **NO_TRADE**, even if a setup object exists.
- `entry_distance_atr > 0.5` (default) means the entry has already run; report it as
  stale unless the user explicitly asks for a historical review.
- `setup.r_multiple` is gross RR from the signal level; subtract fees/slippage before
  calling it net RR.
- A high `quality` does not override a failed gate.

## 5. Sample-size gate

`backtest` reports `sample_class`:

```text
<20    observation     no edge claim
20-50  review          review execution only
50-100 limited         may inspect one parameter
>100   walk_forward    run purged walk-forward before changing anything
```

Never present a 4-trade 100% win rate as proof. Report trade count, win rate, average R,
PF, max drawdown, and the sample class together.

## 6. Execution checklist

Before a trade:

```text
[ ] data source / as_of / timezone recorded
[ ] closed-bar confirmation
[ ] bias and draw on liquidity written down
[ ] sweep + displacement + structure/CISD complete
[ ] entry is still reachable
[ ] stop is at invalidation, not at a round number
[ ] net RR >= 2
[ ] risk <= 1-2% of account
```

After a loss:

```text
[ ] no revenge trade
[ ] no widening the stop
[ ] no adding to a losing position
[ ] log process quality separately from P&L
```

After three consecutive losses: pause and review the process, not the P&L.
After a daily loss limit: stop opening new trades for the day.
