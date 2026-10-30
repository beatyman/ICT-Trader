# Expectation changelog

Every expected-value change must have evidence and a named confirmation.
No evidence, no merge.

## EC-20261030-01

- change_id: EC-20261030-01
- rule_id: L5-03 / HTF 4H score
- old_expected: 4H short 3/3
- new_expected: 4H short 2/3
- evidence: 4H Tenkan = Kijun = 79,198.85; strict `tenkan < kijun` is false
- classification: expectation_error
- confirmed_by: P0 regression owner
- regression_case: BTCUSDT 2026-10-30 12:45 UTC, L5-03

## EC-20261030-02

- change_id: EC-20261030-02
- rule_id: L5-03 / HTF 1H score
- old_expected: 1H short 3/3
- new_expected: 1H short 2/3
- evidence: S-01 eq_atr=0.05 turns MACD diff -30 into neutral; 1H short loses MACD point
- classification: expectation_error
- confirmed_by: P0 regression owner
- regression_case: BTCUSDT 2026-10-30 12:45 UTC, S-01 tolerance case

## EC-20261030-03

- change_id: EC-20261030-03
- rule_id: L2-07 / DOL
- old_expected: any unswept pool on plan side could be DOL
- new_expected: ignore pools with `known_at >= n - 20`; short DOL becomes 76,444
- evidence: 77,181 known_at 1495 is the current swing extreme, not an untested draw; expected 76,444 SSL
- classification: expectation_error
- confirmed_by: P1 regression owner
- regression_case: BTCUSDT 2026-10-30 12:45 UTC, short conditional plan

## EC-20261030-04

- change_id: EC-20261030-04
- rule_id: decision aggregator / GS-01..GS-08
- old_expected: decision TRADE if plan exists and only blocking L rules pass
- new_expected: all hard_gate GS-01..GS-08 must PASS; conditional plan -> CONDITIONAL
- evidence: GS-05/GS-06/GS-07 were necessary=true but not blocking; A2 could return TRADE
- classification: implementation_error
- confirmed_by: architecture owner
- regression_case: A2 only GS-06 FAIL; A3 only GS-08 FAIL

## EC-20261030-05

- change_id: EC-20261030-05
- rule_id: GS-07 / L4-01
- old_expected: conditional plan with entry/SL/TP -> L4-01 N/A
- new_expected: evaluate given SL against structural stop; current plan SL 79,300 vs expected 77,206.65 -> FAIL
- evidence: sweep 77,181 available, OB missing; delta 8.161 ATR
- classification: implementation_error
- confirmed_by: P1 regression owner
- regression_case: BTCUSDT 2026-10-30 12:45 UTC, conditional short plan

## Template

```text
## EC-YYYYMMDD-NN

- change_id: EC-YYYYMMDD-NN
- rule_id: L?-?? / GS-??
- old_expected:
- new_expected:
- evidence:
- classification: expectation_error | implementation_error | data_issue
- confirmed_by:
- regression_case:
```
