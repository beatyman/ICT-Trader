---
name: ict-rule-audit
description: Audit a CSV against the layered ICT rule system (L0-L5). Use before drawing an ICT chart or producing a trade plan; returns PASS/FAIL/N/A and blocks on missing or failed rules.
---

# ICT rule audit

Use this skill when someone asks:

- "这套 ICT 规则能不能回测"
- "帮我检查这个信号是否符合规则"
- "先跑一遍规则审计"
- "这个 setup 能不能做"
- "规则变成代码了吗"

## Command

```bash
python scripts/audit_ict_rules.py \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market us \
  --plan-json output/ict/plan.json \
  --json-out output/ict/rule_audit.json
```

`--market` is one of `crypto`, `us`, `hk`, `other`.

`--smt-csv` is optional.  Without it, SMT is `N/A`, not `PASS`.

## Output

The JSON has:

```text
decision
last_time
last_close
atr
blocking_failures
rules[]  ->  rule_id, layer, name, status, blocking, value, reason
```

Status:

```text
PASS
FAIL
N/A
```

`N/A` is not a pass.

## Decision rule

```text
TRADE only when:
  a plan exists
  and no blocking rule is FAIL or N/A

otherwise NO_TRADE
```

Blocking currently includes:

```text
L0-01 data integrity
L0-02 market session
L0-05 history length
L1-01 swing definition
L2-04 liquidity sweep
L2-07 DOL
L3-04 CISD
L3-05 entry model
L4-03 net RR
L5-03 HTF alignment
```

## Honest implementation status

Implemented from real OHLCV:

```text
L0 data/session/history
L1 swings / trend / BOS / CHoCH-MSS / range
L2 PDH-PDL / EQH-EQL / sweep / DOL
L3 FVG / OB / CISD / entry model
L4 stop / target / net RR
L5 HTF alignment
```

`N/A` until the required input exists:

```text
L0-03 news calendar
L0-04 spread
L2-03 session range
L2-06 SMT second symbol
L3-03 Breaker classifier
L3-06 OTE dealing range
L4-04 time stop policy
L4-05 portfolio sizing
L4-06 execution/overnight policy
L5-01 killzone session data
L5-04 session sweep data
L5-05 SMT filter
```

Do not claim these as passed.

## Workflow

```text
1. Run the audit.
2. Read decision and blocking_failures.
3. Only then draw the chart.
4. If decision=NO_TRADE, do not turn it into a discretionary trade.
5. Chart plan must be labelled PLAN or AUTO CONDITIONAL.
```

## Files

```text
src/ict_trader/rules.py          engine
scripts/audit_ict_rules.py       CLI
docs/ICT_RULE_SYSTEM.md          rule definitions
docs/ICT_CHART_STANDARD.md       chart contract
```

## Rules

- never invent a structure to make a rule pass;
- never treat `N/A` as a pass;
- keep the audit JSON next to the chart;
- if a rule is not implemented, say so.
