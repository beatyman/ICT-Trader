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

Total: **193 full articles**, about **3.7M characters**.

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
