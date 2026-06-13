# Verification Data Sources

Independent out-of-sample verification of Headroom compression claims. This
harness uses **only** data captured fresh from REAL external sources or
REAL local-real sources (a different repo on disk, a real package lockfile, a
real OS log). It does **not** import, re-run, or reuse anything under
`benchmarks/` or its generators.

All captures live under `verify/data/` (committed snapshots). The generators in
`verify/generators.py` harvest vocabulary and row shapes from these captures and
then SYNTHESIZE fresh out-of-sample rows (different content, structure, seeds)
with realistic per-row-unique fields (ISO timestamps, ids, uuids, sha1 hashes).
No real row is replayed verbatim as a test payload.

Captured: 2026-06-13 (UTC). Platform: macOS (darwin 24.6.0).

## Real external (freshly cloned / fetched over the network)

| # | Source | Type | Exact citation | Used for | File |
|---|--------|------|----------------|----------|------|
| 1 | GitHub repo `sindresorhus/slugify` | git repo (fresh clone) | `git clone --depth 1 https://github.com/sindresorhus/slugify.git` then `git fetch --unshallow`; pinned commit `7c318bd1aa4b4affab29761f15a9604323fe2a3b` | code blobs (real source), git-log rows, ripgrep rows | `slugify_index.js`, `slugify_gitlog.raw.txt`, `slugify_rg.raw.jsonl` |
| 2 | GitHub repo `sindresorhus/is-plain-obj` | git repo (fresh clone) | `git clone --depth 1 https://github.com/sindresorhus/is-plain-obj.git`; pinned commit `97f38e8836f86a642cce98fc6ab3058bc36df181` | second real source blob (code case) | `isplainobj_index.js` |
| 3 | GitHub REST API — commits | JSON API dump | `curl -H 'Accept: application/vnd.github+json' 'https://api.github.com/repos/sindresorhus/slugify/commits?per_page=100'` → 71-element array, 291,779 bytes | structured-log vocabulary (authors, messages, ISO dates) | `github_commits_slugify.json` |
| 4 | npm registry — package metadata | JSON API dump | `curl 'https://registry.npmjs.org/slugify'` → 103,522 bytes | additional real JSON-API dump (provenance) | `npm_registry_slugify.json` |

Citations above are the EXACT commands run; each is re-runnable by a third
party. The cloned repos are MIT-licensed (sindresorhus). Pinned commit hashes
make the source bytes reproducible.

## Real local-real (NOT freshly cloned; a real artifact already on this machine)

| # | Source | Type | Exact citation | Used for | File |
|---|--------|------|----------------|----------|------|
| 5 | `threejs-devtools-mcp/package-lock.json` | real lockfile (a DIFFERENT project on disk, not Headroom) | `cp /Users/k/dev/threejs-devtools-mcp/package-lock.json` → 122,205 bytes | `disk`-case directory-entry vocabulary (real dependency names) | `threejs_devtools_package-lock.json` |
| 6 | macOS `/var/log/install.log` | real OS log | `tail -c 400000 /var/log/install.log` → 400,000 bytes, 2,796 lines (committed as `.txt` because the repo `.gitignore` excludes `*.log`) | provenance / real-log reference (local-real) | `macos_install.log.txt` |

Sources 5–6 are **local-real**: genuine real-world artifacts that already
existed on this machine, NOT freshly cloned over the network and NOT project
fixtures. They are clearly labelled as such.

## What the generators do with these (out-of-sample synthesis)

- **logs / repeated_logs** — seed author/message/level vocabulary from the real
  git log + GitHub commits API; synthesize rows with fresh per-row ISO
  timestamps, monotonic ids, fresh sha1 commit hashes (`medium`) or fully
  unique uuid-bearing messages + random hashes (`high`). `repeated_logs` forces
  the low-entropy repetitive shape.
- **search** — seed real file paths + real match lines from the ripgrep JSON
  capture; synthesize match rows with fresh line numbers / columns (`medium`)
  or unique synthetic paths + uuid-bearing match text (`high`).
- **code** — assemble N source blobs from the two real cloned `index.js` files;
  identical copies (`low`), unique-header copies (`medium`), or uuid-comment-
  perturbed near-unique copies (`high`).
- **multiturn** — a stable cached prefix (system + leading turns) followed by 6
  conversational turns each carrying a structured tool result; entropy tiers as
  above. The cached prefix MUST survive un-dropped and un-reordered.
- **disk** — seed real dependency names from the real lockfile; synthesize
  `ls -la`-shaped rows (perms, links, owner, size, ISO mtime, name) with fresh
  per-row sizes/mtimes (`medium`) or unique inodes/owners (`high`).

## Engine surface used (no re-implementation)

- Compression: `from headroom import compress` — committed DEFAULT params only
  (no `config`, no kwargs ⇒ `CompressConfig` defaults + `RoutingPolicy`
  default `MinTokens`, confirmed in
  `crates/headroom-core/src/transforms/smart_crusher/config.rs:183`).
- Token counting: `headroom.tokenizer.Tokenizer` over
  `headroom.tokenizers.get_tokenizer("gpt-4o")` (real tiktoken BPE).
- Reconstruction decoder: `headroom.transforms.csv_schema_decoder
  .decode_csv_schema_rows` (the documented reference decoder).
- CCR retrieve: `headroom.cache.compression_store.get_compression_store()
  .retrieve(hash)`, keyed by the `<<ccr:HASH>>` pointer parsed out of the
  `{"_ccr_dropped": ...}` sentinel in the compressed output.
