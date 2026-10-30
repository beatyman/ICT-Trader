# Aggregator architecture

## Q1 necessary vs blocking

Decision: **S3 explicit gates**, with all eight necessary GS as `hard_gate`.

```text
hard_gate:    GS-01..GS-08
quality_gate: GS-09..GS-15
```

`necessary` means hard. `blocking` on an individual L rule is no longer the
decision contract; it is retained as execution metadata.

## Q2 N/A

```text
hard_gate N/A -> NOT pass -> NO_TRADE
quality_gate N/A -> skipped
```

No-plan case:

```text
GS-07/08 N/A -> decision NO_TRADE
```

Conditional plan case:

```text
plan_status=conditional
all hard gates PASS -> CONDITIONAL
not TRADE
```

## Q3 decision domain

```text
NO_TRADE
CONDITIONAL
TRADE
```

Rules:

```text
any hard_gate != PASS       -> NO_TRADE
no plan                     -> NO_TRADE
plan_status=conditional    -> CONDITIONAL
plan_status=triggered      -> TRADE
plan_status=expired        -> NO_TRADE
```

`good_setup`:

```text
PASS        all hard gates PASS
FAIL        any hard gate FAIL
INCOMPLETE  any hard gate N/A
```

## Implementation

`src/ict_trader/rules.py::decide_from_good`

```python
def decide_from_good(good, plan):
    hard = [g for g in good if g["necessary"]]
    bad = [g for g in hard if g["status"] != "PASS"]
    status = plan.get("plan_status", "conditional") if plan else None
    if bad or plan is None:
        return "NO_TRADE", bad
    if status == "triggered":
        return "TRADE", bad
    if status == "conditional":
        return "CONDITIONAL", bad
    return "NO_TRADE", bad
```

`audit()` now calls this after building `good_setup`.

## A2/A3 aggregator-level result

```text
A2 only GS-06 FAIL -> NO_TRADE, blocker GS-06
A3 only GS-08 FAIL -> NO_TRADE, blocker GS-08
A1 all PASS + triggered -> TRADE
A1 all PASS + conditional -> CONDITIONAL
```

Full-market A1 fixture is still missing.
