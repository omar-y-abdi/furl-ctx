# Held-out Verification Data Sources (SECOND run)

This is a **second, held-out** independent verification of Furl's
compression claims, fully disjoint from the first run under `verify/`.

The first run seeded from `sindresorhus/slugify`, `sindresorhus/is-plain-obj`,
the GitHub commits API for slugify, and the npm registry for slugify. The
engine improvers measured against that first-run data during their round-3
work. THIS run therefore uses **different public repos, different API dumps,
and different seeds** (base `2000`, stride `211` vs the first run's `1000`/`137`)
so the improvers provably never saw this data — any gain that replicates here is
not overfit to `verify/`'s fixtures.

This harness does **not** import, re-run, or reuse anything under `benchmarks/`
or `verify/`'s generators/data. The generators in
`verify/heldout/generators.py` harvest vocabulary and row shapes from the
captures below, then **synthesize fresh out-of-sample rows** (different content,
structure, seeds) with realistic per-row-unique fields (ISO timestamps, ids,
uuids, sha1 hashes). No real row is replayed verbatim as a test payload.

Captured: 2026-06-13 (UTC). Platform: macOS (darwin 24.6.0).

## Real external (freshly cloned / fetched over the network)

| # | Source | Type | Exact citation | Used for | File |
|---|--------|------|----------------|----------|------|
| 1 | GitHub repo `expressjs/express` | git repo (fresh clone, deep tree, many files) | `git clone --depth 1 https://github.com/expressjs/express.git` then `git fetch --unshallow`; pinned commit `dae209ae6559c29cfca2a1f4414c51d89ea643d5` | git-log rows (authors/messages/ISO dates/hashes), ripgrep rows (real paths/lines), real source blob | `express_gitlog.raw.txt`, `express_rg.raw.jsonl`, `express_application.js`, `express_commit.txt` |
| 2 | GitHub repo `chalk/chalk` | git repo (fresh clone) | `git clone --depth 1 https://github.com/chalk/chalk.git`; pinned commit `aa06bb5ac3f14df9fda8cfb54274dfc165ddfdef` | second real source blob (code case) | `chalk_index.js`, `chalk_commit.txt` |
| 3 | GitHub REST API — commits | JSON API dump | `curl -H 'Accept: application/vnd.github+json' 'https://api.github.com/repos/expressjs/express/commits?per_page=100'` -> 100-element array, 562,325 bytes | structured-log vocabulary provenance (authors, messages, ISO dates) | `github_commits_express.json` |
| 4 | npm registry — package metadata | JSON API dump | `curl 'https://registry.npmjs.org/express'` -> 804,947 bytes | real JSON-API dump (provenance) | `npm_registry_express.json` |
| 5 | `npm/cli` package-lock.json | real lockfile (fetched over the network from a DIFFERENT project than the first run's `threejs-devtools`) | `curl -L 'https://raw.githubusercontent.com/npm/cli/latest/package-lock.json'` -> 418,663 bytes, 1,196 packages | `disk`-case directory-entry vocabulary (real dependency names) | `npm_cli_package-lock.json` |

Citations above are the EXACT commands run; each is re-runnable by a third
party. The cloned repos are MIT-licensed (express, chalk). Pinned commit hashes
make the source bytes reproducible. The npm/cli lockfile is fetched from the
`latest` ref; for byte-exact reproduction use the committed snapshot.

## Real local-real (a genuine artifact already on this machine, not a project fixture)

| # | Source | Type | Exact citation | Used for | File |
|---|--------|------|----------------|----------|------|
| 6 | macOS `/var/log/install.log` | real OS log | `tail -c 300000 /var/log/install.log` -> 300,000 bytes, ~2,085 lines (committed as `.txt` because the repo `.gitignore` excludes `*.log`) | provenance / real-log reference (local-real) | `macos_install.log.txt` |

Source 6 is **local-real**: a genuine real-world artifact already on this
machine, NOT freshly cloned and NOT a project fixture. It is clearly labelled.

Note on the CI log: the GitHub Actions run-log download endpoint requires an
authenticated token (it 302-redirects to a signed artifact URL), so a real
Actions text log could not be captured unauthenticated. The local-real macOS
install log (source 6) serves as the real OS/CI-style log reference instead, as
the mandate permits ("If network/clone is unavailable, use REAL local data that
is NOT a project fixture, and say so"). The logs/search generators do not depend
on this file for synthesis — they seed from the express git log + ripgrep
captures (sources 1).

## What the generators do with these (out-of-sample synthesis)

- **logs / repeated_logs** — seed author/message/level/hash vocabulary from the
  real express git log; synthesize rows with fresh per-row ISO timestamps,
  monotonic ids, fresh sha1 commit hashes (`medium`) or fully unique
  uuid-bearing messages + random hashes (`high`). `repeated_logs` forces the
  low-entropy repetitive shape. `struct` makes messages share a path-root head +
  a `req-` id prefix (affix/head fold SHOULD fire); `genuine` uses random
  uuid/sha with no shared structure (folds should add no fake gain).
- **search** — seed real file paths + real match lines from the express ripgrep
  JSON capture; synthesize match rows with fresh line numbers / columns
  (`medium`), unique synthetic paths + uuid match text (`high`), shared-path-root
  near-unique rows (`struct`), or no-shared-structure random rows (`genuine`).
- **code** — assemble N source blobs from the two real cloned files
  (`express/lib/application.js`, `chalk/source/index.js`); identical copies
  (`low`), unique-header copies (`medium`/`struct`), or uuid-comment-perturbed
  near-unique copies (`high`/`genuine`).
- **multiturn** — a stable cached prefix (system + leading turns) followed by 6
  conversational turns each carrying a structured tool result; entropy tiers as
  above. The cached prefix MUST survive un-dropped and un-reordered.
- **disk** — seed real dependency names from the npm/cli lockfile; synthesize
  `ls -la`-shaped rows (perms, links, owner, size, ISO mtime, name) with fresh
  per-row sizes/mtimes (`medium`), shared module-root names (`struct`), or unique
  uuid names/inodes (`genuine`/`high`).

## Engine surface used (no re-implementation)

- Compression: `from furl_ctx import compress` — committed DEFAULT params only
  (no `config`, no kwargs => `CompressConfig` defaults + `RoutingPolicy`
  default `MinTokens`).
- Token counting: `furl_ctx.tokenizer.Tokenizer` over
  `furl_ctx.tokenizers.get_tokenizer("gpt-4o")` (real tiktoken BPE).
- Reconstruction decoder: `furl_ctx.transforms.csv_schema_decoder
  .decode_csv_schema_rows` (the documented reference decoder).
- CCR retrieve: `furl_ctx.cache.compression_store.get_compression_store()
  .retrieve(hash)`, keyed by the `<<ccr:HASH>>` pointer parsed out of the
  `{"_ccr_dropped": ...}` sentinel in the compressed output.

The measurement core `verify/heldout/measure.py` is the same engine-surface
contract as the first run (it uses only the engine's public API; reimplementing
compression or the decoder is forbidden). Only the generators and the seed data
differ — that is what makes this a held-out, out-of-sample run.
