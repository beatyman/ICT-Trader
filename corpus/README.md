# ICT research corpus

This directory keeps the full-text research corpus used by the ICT strategy work.
It is intentionally separate from production code and from `output/`.

## Contents

```text
14 source directories
383 articles
6,525,807 characters
```

Each source directory contains:

```text
corpus.md      human-readable concatenated text, with source URL per article
articles.json  structured list: number, title, url, text
```

`index.json` records article counts, character counts and a SHA-256 prefix for each
`corpus.md`.

## Sources

| Directory | Articles | Notes |
|---|---:|---|
| `ict_course` | 51 | Inner Circle Traders learning path |
| `tsg_course` | 16 | Trading Strategy Guides |
| `vantage_course` | 126 | Vantage UK Academy; generic risk/education |
| `eplanet_course` | 75 | ePlanet Brokers training |
| `volity_course` | 13 | Volity forex education |
| `tradervue_course` | 12 | Tradervue journal/execution |
| `altrady_course` | 8 | Altrady crypto ICT/SMC |
| `ttrades_course` | 34 | TTrades Fractal Model |
| `finxsol_course` | 4 | Finxsol liquidity articles |
| `itbfx_course` | 1 | ITBFX ICT guide |
| `ttp_course` | 3 | Trade The Pool ICT basics |
| `stoic_course` | 26 | Stoic Edge range/research method |
| `flux_course` | 10 | Flux Charts concepts |
| `4xpip_course` | 4 | 4xPip ICT/Martingale risk |

## Not included

ChartingLens is not included: its blog, RSS, REST API and sitemap all returned
Cloudflare 403.  Only its summary/index was available, so it is not represented as
full text here.

## Provenance and use

The files are third-party educational content, kept for internal research,
reproducibility and rule extraction.  Each article retains its source URL in
`articles.json` and `corpus.md`.  Do not present this corpus as original work or
redistribute it beyond the rights you have.  The repository's own conclusions are in
`docs/SETUP_RESEARCH.md`, `docs/SOURCE_CORPUS.md` and `docs/OPTIMIZATION.md`.

## Integrity

Run:

```bash
python - <<'PY'
import json, hashlib
from pathlib import Path
root = Path("corpus")
idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
for row in idx["items"]:
    p = root / row["source_dir"] / "corpus.md"
    h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    assert h == row["corpus_sha256_prefix"], (p, h, row["corpus_sha256_prefix"])
print("corpus hashes OK")
PY
```
