# BASELINE — Imp3 Honest Benchmark (Phase-2 current engine)

- Captured: `2026-07-19T22:08:35.942263+00:00`
- Commit: `57148d7b64150395742b9923d4cd8afb2e4375dc`
- Token model: `gpt-4o` (real tiktoken BPE via the engine's tokenizer registry)
- Python: `3.13.14`  Platform: `macOS-15.7.8-x86_64-i386-64bit-Mach-O`

All numbers come from REAL captured data (no synthetic low-entropy
inputs). Token counts use the SAME tokenizer the engine uses
(`furl_ctx.tokenizers.get_tokenizer` -> `Tokenizer`).

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
| code@7 | 7 | 41025 | 1678 | 95.9% | 100.0% | 100.0% | LOSSY |
| logs@90 | 90 | 8556 | 632 | 92.6% | 91.1% | 100.0% | LOSSY |
| search@90 | 90 | 4102 | 336 | 91.8% | 85.6% | 100.0% | LOSSY |
| repeated_logs@90 | 90 | 3621 | 134 | 96.3% | 95.6% | 100.0% | LOSSY |
| disk@9 | 9 | 694 | 291 | 58.1% | 44.4% | 100.0% | LOSSY |
| multiturn@135 | 135 | 14686 | 2141 | 85.4% | 70.4% | 100.0% | LOSSY |
| ci_log@212 | 212 | 5161 | 692 | 86.6% | 79.2% | 100.0% | LOSSY |
| grep_raw@300 | 300 | 7472 | 894 | 88.0% | 90.0% | 100.0% | LOSSY |
| diff_raw@238 | 238 | 4673 | 2701 | 42.2% | 38.7% | 100.0% | LOSSY |
| markdown_doc@62 | 62 | 797 | 345 | 56.7% | 62.9% | 100.0% | LOSSY |

## Needle-recall

A known unique needle row is injected at start/middle/end into real
arrays of 30/90/300 rows, in two regimes:

- **search** — rg --json rows (lossless columnar path keeps all rows).
- **logs** — git-log rows (varying-field -> lossy drop path fires).

Two query arms per trial, reported separately (EFF-7): **naming**
(the query quotes the needle's sentinel token — best-case recall BY
CONSTRUCTION; this is the number the floor gate checks) and
**control** (the query describes the need without any of the needle's
literal tokens — the honest non-quoting-user number).

- Naming-arm recall (visible OR CCR-recoverable): **100.0%**
- Naming-arm *visible-in-output* recall: **100.0%**
- Control-arm recall (visible OR CCR-recoverable): **100.0%**
- Control-arm *visible-in-output* recall: **61.1%**

| arm | family | recall (output\|CCR) | recall (visible-only) | trials |
|---|---|---:|---:|---:|
| naming | search | 100.0% | 100.0% | 9 |
| naming | logs | 100.0% | 100.0% | 9 |
| control | search | 100.0% | 100.0% | 9 |
| control | logs | 100.0% | 22.2% | 9 |

### Per-trial needle outcomes

| arm | family | card | position | in_output | ccr_recoverable | recalled |
|---|---|---:|---|---|---|---|
| naming | search | 30 | start | True | False | True |
| naming | search | 30 | middle | True | False | True |
| naming | search | 30 | end | True | False | True |
| naming | search | 90 | start | True | False | True |
| naming | search | 90 | middle | True | False | True |
| naming | search | 90 | end | True | False | True |
| naming | search | 300 | start | True | False | True |
| naming | search | 300 | middle | True | False | True |
| naming | search | 300 | end | True | False | True |
| naming | logs | 30 | start | True | False | True |
| naming | logs | 30 | middle | True | False | True |
| naming | logs | 30 | end | True | False | True |
| naming | logs | 90 | start | True | False | True |
| naming | logs | 90 | middle | True | False | True |
| naming | logs | 90 | end | True | False | True |
| naming | logs | 300 | start | True | False | True |
| naming | logs | 300 | middle | True | False | True |
| naming | logs | 300 | end | True | False | True |
| control | search | 30 | start | True | False | True |
| control | search | 30 | middle | True | False | True |
| control | search | 30 | end | True | False | True |
| control | search | 90 | start | True | False | True |
| control | search | 90 | middle | True | False | True |
| control | search | 90 | end | True | False | True |
| control | search | 300 | start | True | False | True |
| control | search | 300 | middle | True | False | True |
| control | search | 300 | end | True | False | True |
| control | logs | 30 | start | True | False | True |
| control | logs | 30 | middle | False | True | True |
| control | logs | 30 | end | True | False | True |
| control | logs | 90 | start | False | True | True |
| control | logs | 90 | middle | False | True | True |
| control | logs | 90 | end | False | True | True |
| control | logs | 300 | start | False | True | True |
| control | logs | 300 | middle | False | True | True |
| control | logs | 300 | end | False | True | True |

## Data provenance

Raw captures are committed under `benchmarks/data/` so every number is
auditable and re-derivable. Capture commands:

- **code** (7 items, snapshot `benchmarks/data/code.raw.json` = 185070 bytes): Read of this repo's own source files: furl_ctx/compress.py, furl_ctx/tokenizer.py, furl_ctx/config.py, furl_ctx/transforms/smart_crusher.py, furl_ctx/transforms/search_compressor.py, furl_ctx/transforms/log_compressor.py, furl_ctx/cache/compression_store.py (relative to repo root).
- **logs** (90 items, snapshot `benchmarks/data/logs.raw.json` = 58377 bytes): git log --pretty=format:'%H<US>%an<US>%ae<US>%aI<US>%s' -n 300 (unit-separated), parsed to {commit,author,email,date,subject} rows.
- **search** (90 items, snapshot `benchmarks/data/search.raw.json` = 379314 bytes): rg --json 'def ' furl_ctx/ — match objects parsed to {path,line_number,absolute_offset,lines} rows.
- **repeated_logs** (90 items, snapshot `benchmarks/data/repeated_logs.raw.json` = 6242 bytes): ping -c 100 -i 0.01 127.0.0.1 (icmp_seq reply lines) — real ICMP echo replies. Content {bytes=64, from=127.0.0.1, ttl=64} recurs identically; only the monotone icmp_seq counter (+ real time latency) vary. icmp_seq is the canonical VaryingIdentity field that forces unique whole-item hashes and that the field-aware stable hash excludes.
- **disk** (9 items, snapshot `benchmarks/data/disk.raw.json` = 1371 bytes): df -k — real filesystem table from this machine, header skipped, fields right-split into {filesystem,kbytes,used,avail,capacity,iused,ifree,piused,mounted_on} rows.
- **multiturn** (135 items, snapshot `benchmarks/data/multiturn.raw.json` = 958121 bytes): Two real consecutive invocations each of: `rg --json --sort path def  furl_ctx/` (match rows parsed like the search dataset, first 90 rows per run — byte-identical across the two runs of the unchanged tree), `df -k` (rows parsed like the disk dataset, the two runs spaced ~3s apart like successive agent turns — free-space/inode cells genuinely drift between runs), and `git log -n 30` viewed one real commit apart (HEAD~1 then HEAD, parsed like the logs dataset — 29/30 rows byte-identical, the canonical post-commit re-check). All six raw captures snapshotted.
- **ci_log** (212 items, snapshot `benchmarks/data/ci_log.raw.json` = 14803 bytes): Synthesized CI log (rawtext_sources.synth_ci_log, seed=20260703): npm ci warnings + cargo build warning block + pytest run with python-logging timestamped INFO/WARNING lines, one native traceback failure, and the summary line. Deterministic: same seed ⇒ same bytes.
- **grep_raw** (300 items, snapshot `benchmarks/data/grep_raw.raw.json` = 28028 bytes): `grep -rn --include=*.py def  furl_ctx/transforms/` — raw file:line:content matches over this repo's own transforms package (real tool output, verbatim).
- **diff_raw** (238 items, snapshot `benchmarks/data/diff_raw.raw.json` = 12865 bytes): `git diff 3154e766~1 c8d5e41b -- Cargo.lock Cargo.toml crates/furl-core/Cargo.toml` — real unified diff between two committed refs of this repo (multi-file; Cargo.lock churn dominates).
- **markdown_doc** (62 items, snapshot `benchmarks/data/markdown_doc.raw.json` = 3915 bytes): README-shaped markdown/prose document (rawtext_sources.MARKDOWN_DOC): headers, lists, indented code blocks, paragraphs. Static committed text, snapshotted verbatim.

## Honest read

- **Deletion-backed savings**: 10/10 datasets (code@7, logs@90, search@90, repeated_logs@90, disk@9, multiturn@135, ci_log@212, grep_raw@300, diff_raw@238, markdown_doc@62) ship with rows dropped from the visible
  output — their savings are NOT free; every drop must be (and is)
  covered by a CCR recovery pointer.
- **Retention floor**: 100.0% — every dataset's dropped rows
  resolve through the emitted recovery pointers (sentinel `<<ccr:HASH>>`
  or the raw-text `Retrieve …: hash=…` marker) against the live store.
- **Needle honesty (EFF-7)**: the naming arm is best-case BY
  CONSTRUCTION (the query quotes the sentinel, query-aware keep pins
  it). The control arm is what a user who cannot quote the row gets:
  visible-in-output recall 61.1% vs 100.0% naming — the gap is the retrieval
  cost a non-quoting user pays on the lossy path (CCR round-trip,
  not silent loss, while recall-output-or-CCR holds).
