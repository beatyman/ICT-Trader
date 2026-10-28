# Reference review — 10 repositories

Method: shallow clone, read README, core code and research docs; extract reusable
**ideas**, not copy code. Licenses and quality vary, so each item is treated as a
hypothesis until its own A/B test.

## Summary

| # | Project | Type | Take | Do not take | Adopted here |
|---:|---|---|---|---|---|
| 1 | `king-No/fvg-trading-strategy` | Freqtrade LGR strategy | sweep + FVG + ADX; ATR stop/trail; reversal exit | 1-month 100%+ claims, 5x/30% sizing, same-bar-only sweep | optional ATR trailing |
| 2 | `licq201/ICT-TRADE-NEW` | Python ICT + ML ensemble | 35-feature taxonomy; walk-forward; daily loss/cooldown gates | ML as a default without out-of-sample proof | daily-loss gate, diagnostics |
| 3 | `lumpa7223/aibbm` | Astro blog / ICT glossary | glossary, education structure | no executable trading logic | none directly |
| 4 | `zhaozhenxiong/KCPredict-skill` | Codex/Claude skill | provenance, as-of, evidence classes, quality states, output contract | heavy Kronos runtime, model predictions | skill evidence contract + validator |
| 5 | `FalwIpbfYygy/quant_review` | FastAPI review system | strict `asof`; verdict/tradable; causal backtest | LLM narrative as evidence | evidence/state language |
| 6 | `FalwIpbfYygy/signal_resonance_hub` | rule signal hub | normalization, dedupe, cross-TF resonance, delivery verification | alerts are not trades; no cost backtest | signal-quality/dedupe thinking |
| 7 | `JiajunGao88/trading-system` | Markdown trading OS | 1R, 6% heat, daily/streak circuit breakers, journal | no code | daily loss gate, skill checklist |
| 8 | `jieao0720-debug/polymarket-smart-money-monitor` | FastAPI/Next monitor | look-ahead-safe profiles, paper trading, fail-closed risk | prediction-market domain | audit/paper-trading mindset |
| 9 | `bywinds/mt5_smc` | MQL5 EA | weighted module score; H4 BOS/CHoCH projection; structural SL/TP | broker/point-specific defaults | weighted-quality precedent |
| 10 | `Lewsiafat/smc-scalping-research` | research docs | quantified swing/BOS/CHoCH/OB/FVG/sweep; ATR caps; look-ahead controls | no implementation, so no direct code | multi-bar reclaim, quality gate, ATR cap idea |

## 1. fvg-trading-strategy

**What it is:** a Freqtrade strategy for BTC/ETH 15m using sweep + FVG + ADX and ATR
stop/trailing take-profit.

**Useful:**

- `sweep → FVG confirmation` is a clean entry chain.
- ATR-based stop and a trailing exit that activates after a favourable move.
- It explicitly invalidates on an opposite signal.

**Problems:**

- README claims 74.7% win rate over a 31-day window; this is a tiny, likely
  regime-specific sample.
- Same-bar-only sweep detection misses the multi-bar reclaim the better research
  describes.
- 5x leverage / 30% stake is a risk profile we do not adopt.
- The handoff document contains live VPS credentials; **those are not used or copied**.

**Adopted:** optional `trail_activate_r` / `trail_atr_mult`, separately labelled
`trail` exits. Default off.

## 2. ICT-TRADE-NEW

**What it is:** a Python ICT detector + 35-feature ML ensemble (LightGBM/XGBoost) with a
dashboard and MT5 execution.

**Useful:**

- Feature taxonomy: structure recency/direction, sweep depth, EQH/EQL distance, FVG/OB
  distance, session one-hots, volatility, volume z-score.
- Walk-forward model validation and a separate risk manager.
- Daily loss limit, weekly loss limit, max open positions and cooldown after losses.

**Problems:**

- ML can easily learn the wrong regime; a high AUC is not an edge.
- Dashboard/MT5 scope is far larger than this repository needs.
- Feature normalization and correlation pruning must be fit on the training split only.

**Adopted:** daily-loss gate and richer execution counters. ML is deliberately
**deferred** until the rule-based setup has ≥100 out-of-sample trades.

## 3. aibbm

**What it is:** an Astro/Titan Core blog, with ICT glossary content.

**Useful:** terminology and presentation structure.

**Problems:** no executable strategy, no backtest, no risk model.

**Adopted:** none in code. Its glossary is a reminder that terms without a mechanical
definition are not testable.

## 4. KCPredict-skill

**What it is:** a very carefully engineered local analysis skill with a frozen engine,
quality states, provenance, an HTML/PNG/SVG report and explicit “unverified” handling.

**Useful (best-in-class):**

- `source / as_of / timezone / verified / synthetic` provenance.
- Separate `confirmed / developing / invalidated / no-trade` states.
- `observed / derived / interpretive / missing` evidence classes.
- “Prediction quality not passed” is surfaced, not hidden.
- A concrete output contract and validation gate.

**Problems:** heavy Kronos/PyTorch runtime and a large asset footprint; not appropriate
for this repository’s core.

**Adopted:** `references/evidence_contract.md`, stricter `SKILL.md` and
`scripts/validate_plan.py`.

## 5. quant_review

**What it is:** a FastAPI/MySQL review system for gold, with strict `asof` truncation,
fib “hunt zone” levels and LLM reports.

**Useful:**

- Every signal is cut at `asof`; no future data.
- A `verdict / tradable` field prevents an unvalidated model from looking authoritative.
- Costs, slippage, 1R/1.8R scale-out and drawdown reporting.
- Tests around the causal gate.

**Problems:** LLM narrative and discretionary fib levels are not a substitute for a
mechanical strategy.

**Adopted:** stricter state/evidence language; no LLM dependency in the engine.

## 6. signal_resonance_hub

**What it is:** a multi-source signal aggregator: normalized signal store, dedupe,
cross-timeframe resonance and chat/email delivery.

**Useful:**

- One normalized signal record instead of many incompatible alert formats.
- Dedupe by `(symbol, pattern, direction)`.
- Cooldown and delivery verification (`HTTP 200` is not success).
- Signals use only the closed bar.

**Problems:** README explicitly says there is no cost-inclusive backtest; alerts are not
a strategy.

**Adopted:** resonance/dedupe thinking and the “separate evidence from alert” stance.
A full signal store is deferred.

## 7. trading-system

**What it is:** a markdown trading operating system with rules, plan, journal, review and
performance modules.

**Useful:**

- 1R = 1% account; portfolio heat ≤ 6%; max 5 positions.
- Daily loss 3% → stop; 3 consecutive losses → pause; 5 → weekly review.
- No adding to losers, no widening stops, process score separate from P&L.
- Weekly/monthly review cadence.

**Problems:** no executable code; the 3%/6% numbers are for a specific account style.

**Adopted:** daily-loss gate, skill checklist and “process before P&L” language.

## 8. polymarket-smart-money-monitor

**What it is:** a prediction-market smart-money monitor with look-ahead-safe account
profiling, paper trading and a fail-closed manual live-trading gate.

**Useful:**

- Any signal-time statistic uses only positions resolved **before** that time.
- Paper P&L uses the actual taker ask, not the mid.
- Risk checks run twice; live trading is off by default; every trade needs manual
  confirmation.
- Signals carry uncertainty and are not called win probabilities.

**Problems:** not an OHLCV/ICT system; domain-specific.

**Adopted:** provenance/audit mindset and explicit “confidence is not probability”
wording. No domain code copied.

## 9. mt5_smc

**What it is:** an MQL5 SMC EA with swing, BOS/CHoCH, OB, FVG, liquidity, previous-period
levels and a weighted score.

**Useful:**

- Weighted score: previous high/low .15, aggregated BOS .10, BOS .20, CHoCH .10,
  OB .20, FVG .15, liquidity .10; entry .55 / strong .75.
- H4 BOS/CHoCH projected to the execution timeframe.
- Structural SL/TP with ATR buffer and broker minimum-stop validation.
- Sends opposite positions flat before opening.

**Problems:** broker-specific point values, fixed lot defaults and an EA architecture
that does not map directly to this Python engine.

**Adopted:** the weighted-score precedent is already reflected in `quality.py`;
no code copied. A “previous-period” score could be added later.

## 10. smc-scalping-research

**What it is:** two generations of research notes, including an algorithm spec and a
look-ahead protection plan.

**Useful:**

- Quantified swing `n±3`; close-confirmed BOS; CHoCH vs BOS; OB max 3 bars, ≤3 ATR,
  ≤200 bars old; FVG ≥0.5 ATR and mid-body ≥0.5 ATR.
- Sweep penetration 0.1–1.5 ATR, recovery ≤3 bars.
- 10-point score and explicit ATR SL caps.
- Seven look-ahead controls and a PoC/manual-label plan before building.

**Problems:** it is research, not a validated executable; the numeric thresholds are
starting hypotheses.

**Adopted:** multi-bar reclaim window, sweep-speed strength, `entry_distance_atr` and
stricter skill evidence rules. A sweep-depth cap is a candidate for the next A/B.

## What was actually integrated

```text
multi-bar sweep / reclaim           sweeps.py, cisd.py
optional retest entry               strategy.py
target: fixed / liquidity / hybrid  setup.py
plan distance gate                  cli.py
ATR trailing (opt-in)               strategy.py
daily-loss gate (opt-in)            strategy.py
execution counters + sample_class   strategy.py
evidence contract + validator       skills/ict-analysis/
```

## What was deliberately not integrated

- ML ensemble as a default (needs ≥100 out-of-sample trades and a frozen feature set).
- LLM narrative as evidence.
- 5x leverage / 30% stake / 1-month performance claims.
- MT5/broker-specific execution code.
- Prediction-market domain logic.
- A full signal store / chat-alert system.

## Sources

- https://github.com/king-No/fvg-trading-strategy
- https://github.com/licq201/ICT-TRADE-NEW
- https://github.com/lumpa7223/aibbm
- https://github.com/zhaozhenxiong/KCPredict-skill
- https://github.com/FalwIpbfYygy/quant_review
- https://github.com/FalwIpbfYygy/signal_resonance_hub
- https://github.com/JiajunGao88/trading-system
- https://github.com/jieao0720-debug/polymarket-smart-money-monitor
- https://github.com/bywinds/mt5_smc
- https://github.com/Lewsiafat/smc-scalping-research
