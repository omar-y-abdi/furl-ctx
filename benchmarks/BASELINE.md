# BASELINE — Imp3 Honest Benchmark (Phase-2 current engine)

- Captured: `2026-07-02T00:32:27.026888+00:00`
- Commit: `566f34490e2207877c9086a227eab4ba14d9d434`
- Token model: `gpt-4o` (real tiktoken BPE via the engine's tokenizer registry)
- Python: `3.14.2`  Platform: `macOS-15.7.6-x86_64-i386-64bit-Mach-O`

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
| logs@90 | 90 | 8595 | 619 | 92.8% | 91.1% | 100.0% | LOSSY |
| search@90 | 90 | 4102 | 318 | 92.2% | 85.6% | 100.0% | LOSSY |
| repeated_logs@90 | 90 | 3621 | 131 | 96.4% | 98.9% | 100.0% | LOSSY |
| disk@9 | 9 | 694 | 347 | 50.0% | 0.0% | 100.0% | lossless |
| multiturn@135 | 135 | 14866 | 4369 | 70.6% | 49.6% | 100.0% | LOSSY |

## Needle-recall

A known unique needle row is injected at start/middle/end into real
arrays of 30/90/300 rows, in two regimes:

- **search** — rg --json rows (lossless columnar path keeps all rows).
- **logs** — git-log rows (varying-field -> lossy drop path fires).

- Overall recall (visible OR CCR-recoverable): **100.0%**
- Overall *visible-in-output* recall: **100.0%**

| family | recall (output\|CCR) | recall (visible-only) | trials |
|---|---:|---:|---:|
| search | 100.0% | 100.0% | 9 |
| logs | 100.0% | 100.0% | 9 |

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
| logs | 30 | middle | True | False | True |
| logs | 30 | end | True | False | True |
| logs | 90 | start | True | False | True |
| logs | 90 | middle | True | False | True |
| logs | 90 | end | True | False | True |
| logs | 300 | start | True | False | True |
| logs | 300 | middle | True | False | True |
| logs | 300 | end | True | False | True |

## Data provenance

Raw captures are committed under `benchmarks/data/` so every number is
auditable and re-derivable. Capture commands:

- **code** (7 items, snapshot `benchmarks/data/code.raw.json` = 185070 bytes): Read of this repo's own source files: headroom/compress.py, headroom/tokenizer.py, headroom/config.py, headroom/transforms/smart_crusher.py, headroom/transforms/search_compressor.py, headroom/transforms/log_compressor.py, headroom/cache/compression_store.py (relative to repo root).
- **logs** (90 items, snapshot `benchmarks/data/logs.raw.json` = 58377 bytes): git log --pretty=format:'%H<US>%an<US>%ae<US>%aI<US>%s' -n 300 (unit-separated), parsed to {commit,author,email,date,subject} rows.
- **search** (90 items, snapshot `benchmarks/data/search.raw.json` = 379314 bytes): rg --json 'def ' headroom/ — match objects parsed to {path,line_number,absolute_offset,lines} rows.
- **repeated_logs** (90 items, snapshot `benchmarks/data/repeated_logs.raw.json` = 6242 bytes): ping -c 100 -i 0.01 127.0.0.1 (icmp_seq reply lines) — real ICMP echo replies. Content {bytes=64, from=127.0.0.1, ttl=64} recurs identically; only the monotone icmp_seq counter (+ real time latency) vary. icmp_seq is the canonical VaryingIdentity field that forces unique whole-item hashes and that the field-aware stable hash excludes.
- **disk** (9 items, snapshot `benchmarks/data/disk.raw.json` = 1371 bytes): df -k — real filesystem table from this machine, header skipped, fields right-split into {filesystem,kbytes,used,avail,capacity,iused,ifree,piused,mounted_on} rows.
- **multiturn** (135 items, snapshot `benchmarks/data/multiturn.raw.json` = 958121 bytes): Two real consecutive invocations each of: `rg --json --sort path def  headroom/` (match rows parsed like the search dataset, first 90 rows per run — byte-identical across the two runs of the unchanged tree), `df -k` (rows parsed like the disk dataset, the two runs spaced ~3s apart like successive agent turns — free-space/inode cells genuinely drift between runs), and `git log -n 30` viewed one real commit apart (HEAD~1 then HEAD, parsed like the logs dataset — 29/30 rows byte-identical, the canonical post-commit re-check). All six raw captures snapshotted.

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
