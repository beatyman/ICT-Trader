# Sequence entry_bar

Priority:

```text
plan.trigger_bar
> plan.trigger_time mapped to a bar
> null (pending)
```

A conditional plan is not triggered, so it yields `entry_bar = null`.
`_sequence_check` can still validate sweep → MSS → CISD; entry is not required
to mark the sequence valid.

BTC review 2026-10-30:

```text
sweep_bar = 1494 (11:45)
no MSS
no CISD
entry_bar = null
sequence.valid = false
```
