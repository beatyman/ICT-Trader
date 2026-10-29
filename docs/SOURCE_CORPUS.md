# Source corpus: what was actually read

Last updated: 2026-10-29

This file separates **full text** from **search-result summaries**.  A source that
blocks full-text access is not promoted to evidence just because a summary file exists.

## Full-text corpora

| Corpus | Articles | Chars | Local path | Access |
|---|---:|---:|---|---|
| The Inner Circle Traders course | 51 | 701,989 | `output/ict/ict_course/` | full HTML text |
| Trading Strategy Guides | 16 | 223,026 | `output/ict/tsg_course/` | full HTML text |
| Vantage Academy (UK) | 126 | 2,780,659 | `output/ict/vantage_course/` | full HTML text |
| ePlanet Brokers Training | 75 | 1,833,168 | `output/ict/eplanet_course/` | full HTML text |
| Volity Forex Education | 13 | 115,276 | `output/ict/volity_course/` | full HTML text |
| Tradervue Blog | 12 | 227,529 | `output/ict/tradervue_course/` | full HTML text |
| Altrady Crypto | 8 | 93,078 | `output/ict/altrady_course/` | full HTML text |
| TTrades Education | 34 | 162,948 | `output/ict/ttrades_course/` | full HTML text |
| Finxsol Blog | 4 | 48,577 | `output/ict/finxsol_course/` | full HTML text |
| ITBFX Blog | 1 | 22,109 | `output/ict/itbfx_course/` | full HTML text |
| Trade The Pool | 3 | 47,917 | `output/ict/ttp_course/` | full HTML text |
| Stoic Edge | 26 | 157,699 | `output/ict/stoic_course/` | full HTML text |
| Flux Charts | 10 | 49,257 | `output/ict/flux_course/` | full HTML text |

Total: **379 full articles**, about **6.5M characters**.

## Blocked / summary-only

| Source | Status | Use |
|---|---|---|
| ChartingLens | Cloudflare 403 on blog, RSS, REST API and sitemap | summary/index only; claims are `interpretive` |
| Vantage en-za / .com | Cloudflare 403 | UK mirror used for full text instead |

Do not claim ChartingLens full-text review.  The local file
`ChartingLens_Blog_Full_Scrape.md` says it was built from search results and page
snippets, not full articles.

## What each corpus is good for

- **Inner Circle Traders**: the 51-article learning path, 2022 Model order, kill
  zones, PD-array hierarchy, FVG/OB rules, risk and backtesting.
- **Trading Strategy Guides**: concrete FVG execution details — aggressive boundary,
  conservative CE, confirmation entry, Candle-2 stop, freshness, target liquidity.
- **Vantage Academy**: generic risk, position sizing, psychology, Fibonacci,
  candlesticks and platform mechanics.  It is **not** an ICT edge source.
- **ePlanet Brokers**: SMC-first explanations of OB/FVG/liquidity/inducement,
  plus the claim that ICT is SMC with extra layers.  Its FVG article says fresh
  gaps are more reliable; the scrape summary's “6-hour validity” is **not** in the
  article and is not adopted.
- **Volity**: concise ICT/SMC explainers (rejection block, SFP, AMD, liquidity)
  and a five-layer risk framework: 1% per trade, 3% daily loss, leverage cap,
  weekly risk budget, drawdown circuit breaker.
- **Tradervue**: execution and journaling, not ICT edge.  Its ICT article is
  descriptive; its value is risk-reward, MFE/MAE, daily loss limits and review.
- **Altrady**: crypto adaptation of ICT/SMC.  It explicitly warns that 24/7 crypto
  has no clean kill zones, so session filters need re-testing and wider stops.
- **TTrades**: Fractal Model.  The usable additions are Candle 2/Candle 3 closure,
  CISD as swing confirmation, internal/external liquidity and protected swings.
  It also argues for using **one** PD array, not stacking all of them.
- **Finxsol / ITBFX**: mostly narrative and broker education.  Finxsol's
  sweep-vs-grab distinction is not standardized across sources; do not hard-code it.
  ITBFX's UTC kill-zone table conflicts with the NY tables, another reason to keep
  session windows configurable.
- **Trade The Pool**: stock-prop context and beginner ICT.  Useful for cash-session
  examples, not for a new entry model.
- **Stoic Edge**: the most evidence-aware source.  One range map, two setups
  (sweep-retest, break-retest), explicit test specification, no-hindsight rule and
  structural stop sizing.  This supports our `NO_TRADE`/audit direction more than
  any new indicator.
- **Flux Charts**: clean definitions of liquidity grab/sweep, Unicorn (breaker+FVG),
  Turtle Soup and SMT.  Useful as confluence labels; no measured edge.

## Source conflicts to keep explicit

| Topic | Conflict | Current handling |
|---|---|---|
| Asian range | 19:00–00:00 vs 20:00–00:00 vs 19:00–02:00 NY | keep one configured window, record the choice |
| Minimum RR | 2.0 vs 2.5 vs 3.0 | 2.0 hard floor; 2.5 preferred; do not hard-code 3.0 |
| Stop | CISD invalidation vs Candle-2 extreme | A/B before changing default |
| FVG entry | CISD close vs FVG boundary vs CE | CE metadata added; entry mode still default |
| MSS | mandatory vs simplified Keyshotvv | separate models; do not mix results |

## Rule

Full text can support `observed` claims only when the engine reproduces the rule on
closed bars.  Even full text is not a backtest.  Summaries and snippets stay
`interpretive` or `missing`.
