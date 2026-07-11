---
name: furl
description: How the Furl context-compression plugin works — the furl_compress / furl_retrieve / furl_stats / furl_purge / furl_search / furl_list MCP tools, the PostToolUse hook that shrinks large tool outputs, the <<ccr:HASH>> retrieval flow, and the FURL_* environment knobs to tune or disable it. Use when the user asks what Furl is doing, why a tool output looks compressed or contains <<ccr:...>> markers, how to retrieve original content, how to tune compression thresholds, or how to turn the hook off.
version: 1.0.5
---

# Furl — context compression for Claude Code

Furl reduces the tokens large tool outputs cost by compressing them, while keeping
every dropped byte **retrievable on demand**. It ships two things to this session:

1. An **MCP server** (`furl`) exposing six tools.
2. A **PostToolUse hook** that compresses big tool outputs automatically.

## The MCP tools

- `furl_compress` — compress a string on demand. Returns compressed text plus a
  `hash`; the original is stored for later retrieval.
- `furl_retrieve` — get original, uncompressed content back. Pass a `<<ccr:HASH>>`
  marker's hash for the full original, or narrow it with a filter: `pattern` +
  `context_lines` / `line_range` (regex or a line window over the text),
  `fields` (project keys of a JSON array), or a **row-select** —
  `select_field` + `select_equals` (a value) or `select_min`/`select_max` (a
  numeric range), with optional `fields` and `limit` — to pull just the matching
  ROWS of a large offloaded JSON array (or a dominant-array object like a Chrome
  trace) instead of the whole thing. A free-text `query` searches stored entries.
- `furl_stats` — session compression statistics (compressions, tokens saved, cost).
- `furl_purge` — permanently erase stored originals: one hash, or all of them. No undo.
- `furl_search` — find stored originals by a case-insensitive content substring; returns a hash + preview per hit.
- `furl_list` — list stored entries, newest first, for paging through what's been compressed this session. Each entry carries an `expires_in` — humanized time left before its retention TTL evicts it (e.g. `23h`).

A seventh tool, `furl_read`, exists but is off by default — enable with `FURL_MCP_READ=1` (see [The `furl_read` tool](#the-furl_read-tool-opt-in) below).

## When the hook fires

The hook runs **after** a tool returns, on external-output tools: `Bash`,
`WebFetch`, `WebSearch`, `Task`. (Your own `Read`/`Grep`/`Glob` file access is left
untouched by default, so later edits still see exact file bytes.) For each result it:

1. Skips Furl's own tool traffic and anything already carrying `<<ccr:` markers
   (no double-compression).
2. Skips outputs below the size threshold (default 2000 characters).
3. Compresses the rest and replaces the tool output the model sees — but **only if
   compression actually made it smaller**.

It is **fail-open**: any error (compression failure, missing dependency, odd
payload) passes the original output through unchanged. It never blocks a tool call.

## How retrieval works (the `<<ccr:HASH>>` flow)

Compression is often *lossy-but-reversible* (CCR = Compressed Context Retrieval).
Instead of shrinking large low-redundancy content, Furl offloads it to a local
store and leaves a marker like `<<ccr:a1b2c3>>` in its place.

When you need the full content behind a marker, **call `furl_retrieve` with that
hash** — it returns the byte-exact original, as long as the entry is still within
its retention window. **The plugin keeps offloaded originals for 24 hours by
default (`FURL_CCR_TTL_SECONDS=86400`); lower it to expire them sooner or raise it
for a longer window.** After that the entry expires and a retrieve is a loud miss,
never a silent wrong answer. The hook and the `furl` MCP server share one durable
SQLite store (`~/.furl/ccr.sqlite3`), so markers the hook creates are retrievable
through `furl_retrieve`.

When a marker offloaded a large JSON array (an `_ccr_summary` preview shows its
schema, per-field value histograms, and numeric ranges), you usually want a
**slice, not the whole array**. The summary carries a `retrieve` hint telling you
which fields to filter on. Pass a row-select to `furl_retrieve`:
`select_field=<a categorical field>, select_equals=<one of its values>` for just
those rows, or `select_field=<a numeric field>, select_min=…, select_max=…` for a
range window (add `fields=[…]` to project columns, `limit` to cap). The slice is
tiny compared to the full original, so locality and anomaly questions are
answerable without pulling megabytes back into context.

## The `furl_read` tool (opt-in)

`furl_read` is a seventh MCP tool, **off by default**. It reads a file with
session caching: the first read returns the full content and caches it, and later
reads of the *same unchanged file* return a lightweight `<<ccr:HASH>>` marker
(~20 tokens instead of the whole file) — pull the full body back with
`furl_retrieve` on the hash if you need it. Pass `fresh: true` to bypass the cache
(after a context compaction, inside a sub-agent, or whenever you need
guaranteed-current bytes).

**Why it's off by default:** it is a filesystem-reading tool. Reads are jailed to
`FURL_WORKSPACE_DIR` (and, when that is unset, to the MCP server's working
directory), and a caching layer over file reads can serve stale bytes if a file
changes out of band. Rather than silently shadow the built-in `Read` tool, Furl
ships it opt-in so enabling the extra read surface is a deliberate choice.

**How to enable:** set `FURL_MCP_READ=1` (`on`/`true`/`yes`/`enabled`) in the
plugin's `.mcp.json` env or your shell, then restart the session. Once enabled,
call it like the built-in Read:

```
furl_read(file_path="/abs/path/to/big_file.py")
```

## Tuning (environment variables)

Set these in the plugin's `hooks/hooks.json` / `.mcp.json` env, or your shell:

| Variable | Default | Effect |
|----------|---------|--------|
| `FURL_HOOK_ENABLED` | on | Set `0`/`false`/`off` to disable the hook entirely. |
| `FURL_HOOK_MIN_CHARS` | `2000` | Minimum tool-output length before the hook attempts compression. Raise to compress less, lower to compress more. |
| `FURL_HOOK_MODEL` | `claude-sonnet-4-5-20250929` | Model name used for token counting during compression. |
| `FURL_HOOK_EXCLUDE_TOOLS` | (none) | Comma-separated tool names never to compress — exact (`Bash`) or fnmatch globs (`mcp__db__*`). Furl's own tools are always excluded. |
| `FURL_HOOK_MODE` | `normal` | `aggressive` also compresses code in the blob and squeezes smaller outputs; `normal` keeps the default behavior. |
| `FURL_HOOK_VERBOSE` | off | `1`/`true` prints a one-line savings summary per compression to stderr (`furl: Bash 12.4 KB -> 0.3 KB  -97%`). |
| `FURL_STATUS_LINE` | on | Set `0` to silence the one-line `furl … active` SessionStart status signal. Must be exported in the environment Claude Code launches from — the status hook runs `sh -c`, which does not source login profiles. |
| `FURL_CCR_BACKEND` | `sqlite` (set by the plugin) | CCR store backend. Must match between the hook and the `furl` server for retrieval to work. |
| `FURL_CCR_TTL_SECONDS` | `86400` = 24h (set by the plugin) | How long offloaded originals stay retrievable before they expire. Lower to reclaim disk sooner; raise for a longer retrieval window. |
| `FURL_CCR_PROJECT_DIR` | auto, per project (set by the plugin) | Scopes the CCR store to the current project so one machine-global `~/.furl` store never surfaces or evicts another project's entries. Derived automatically from the project root; set to `""` to share one store across all projects — this is also how you read a pre-1.0 global store after upgrading. |

The full `FURL_*` reference (workspace dir, store paths, row caps, circuit breaker)
is in [`LIBRARY.md`](../../../../LIBRARY.md) → "Configuration".

## How to disable

- **Just the hook:** set `FURL_HOOK_ENABLED=0` (leaves the MCP tools available).
- **Everything:** disable the `furl` plugin in Claude Code.

## Prerequisite

Both the hook and the MCP server launch through [`uv`](https://docs.astral.sh/uv/)
(`uv run --with "furl-ctx[mcp]==1.0.2" …`), which fetches Furl from PyPI on first use — no
`pip install`, no Rust toolchain. The version is pinned so every launch resolves the same
wheel deterministically instead of whatever `uv`'s cache last held; upgrades arrive through
plugin updates, which bump the pin. The
only requirement is `uv` on the PATH. If `uv` is missing, the MCP server won't start
and the hook fails open (passes output through unchanged).
