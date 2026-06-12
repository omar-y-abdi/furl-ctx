# BASELINE — Imp3 Honest Benchmark (Phase-2 current engine)

- Captured: `2026-06-12T13:15:43.310182+00:00`
- Commit: `0795e63ede835e5398f77c72c7f0be8fdb96ab0a`
- Token model: `gpt-4o` (real tiktoken BPE via the engine's tokenizer registry)
- Python: `3.13.12`  Platform: `macOS-15.7.6-x86_64-i386-64bit-Mach-O`

All numbers come from REAL captured data (no synthetic low-entropy
inputs). Token counts use the SAME tokenizer the engine uses
(`headroom.tokenizers.get_tokenizer` -> `Tokenizer`).

## Metrics (defined)

- **Lossless token reduction** — token savings ratio. Only a true
  zero-loss number when *drop ratio = 0*. On the lossy path the savings
  partly come from deletion, so the row is flagged `LOSSY`.
- **Lossy drop ratio** — fraction of distinct input rows NOT visible in
  the output (removed / offloaded).
- **Information retention** — fraction of distinct rows present in the
  output OR recoverable from the CCR store via `<<ccr:HASH>>`.

## Dataset metrics

| dataset | items | tok before | tok after | lossless reduction | lossy drop ratio | info retention | path |
|---|---:|---:|---:|---:|---:|---:|---|
| code@7 | 7 | 41025 | 41025 | 0.0% | 0.0% | 100.0% | lossless |
| logs@90 | 90 | 8595 | 1332 | 84.5% | 83.3% | 100.0% | LOSSY |
| search@90 | 90 | 4102 | 2462 | 40.0% | 0.0% | 100.0% | lossless |

## Needle-recall

A known unique needle row is injected at start/middle/end into real
arrays of 30/90/300 rows, in two regimes:

- **search** — rg --json rows (lossless columnar path keeps all rows).
- **logs** — git-log rows (varying-field -> lossy drop path fires).

- Overall recall (visible OR CCR-recoverable): **100.0%**
- Overall *visible-in-output* recall: **72.2%**

| family | recall (output\|CCR) | recall (visible-only) | trials |
|---|---:|---:|---:|
| search | 100.0% | 100.0% | 9 |
| logs | 100.0% | 44.4% | 9 |

### Per-trial needle outcomes

| family | card | position | in_output | ccr_recoverable | recalled |
|---|---:|---|---|---|---|
| search | 30 | start | True | False | True |
| search | 30 | middle | True | False | True |
| search | 30 | end | True | False | True |
| search | 90 | start | True | False | True |
| search | 90 | middle | True | False | True |
| search | 90 | end | True | False | True |
| search | 300 | start | True | False | True |
| search | 300 | middle | True | False | True |
| search | 300 | end | True | False | True |
| logs | 30 | start | True | False | True |
| logs | 30 | middle | False | True | True |
| logs | 30 | end | True | False | True |
| logs | 90 | start | True | False | True |
| logs | 90 | middle | False | True | True |
| logs | 90 | end | True | False | True |
| logs | 300 | start | False | True | True |
| logs | 300 | middle | False | True | True |
| logs | 300 | end | False | True | True |

## Data provenance

Raw captures are committed under `benchmarks/data/` so every number is
auditable and re-derivable. Capture commands:

- **code** (7 items, snapshot `benchmarks/data/code.raw.json` = 185070 bytes): Read of this repo's own source files: headroom/compress.py, headroom/tokenizer.py, headroom/config.py, headroom/transforms/smart_crusher.py, headroom/transforms/search_compressor.py, headroom/transforms/log_compressor.py, headroom/cache/compression_store.py (relative to repo root).
- **logs** (90 items, snapshot `benchmarks/data/logs.raw.json` = 58377 bytes): git log --pretty=format:'%H<US>%an<US>%ae<US>%aI<US>%s' -n 300 (unit-separated), parsed to {commit,author,email,date,subject} rows.
- **search** (90 items, snapshot `benchmarks/data/search.raw.json` = 379314 bytes): rg --json 'def ' headroom/ — match objects parsed to {path,line_number,absolute_offset,lines} rows.

## Honest read

- **search** takes the *lossless columnar* path: all rows survive, no
  drop, needle-recall is 100% visible. The audited 90->24 needle drop
  does NOT reproduce on real ripgrep search results through `compress()`.
- **logs** (varying commit-hash/date) takes the *lossy* path: ~83% of
  rows are dropped from the visible output and the mid/high-cardinality
  needle is silently removed from what the LLM sees — but every dropped
  row is CCR-recoverable, so information retention is 100% *while the
  marker/store path is on*.
- **code** (large distinct source files) is a passthrough: no crush,
  0% reduction, 100% retention.

Net: the audited *needle-loss* reproduces on log-shaped varying-field
data at the *visible-output* level (the LLM loses sight of mid-array
needles), recoverable only via CCR. The audited *~30% real savings*
reproduces as the lossless figure on search (~40%) and code (0%); the
high log 'savings' (84%+) are inflated by deletion, not free.
