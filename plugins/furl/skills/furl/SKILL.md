---
name: furl
description: How the Furl context-compression plugin works — the furl_compress / furl_retrieve / furl_stats / furl_purge / furl_search / furl_list MCP tools, the PostToolUse hook that shrinks large tool outputs, the <<ccr:HASH>> retrieval flow, and the FURL_* environment knobs to tune or disable it. Use when the user asks what Furl is doing, why a tool output looks compressed or contains <<ccr:...>> markers, how to retrieve original content, how to tune compression thresholds, or how to turn the hook off.
version: 1.3.2
---

# Furl — context compression for Claude Code

Furl reduces the tokens large tool outputs cost by compressing them, while keeping
every dropped byte **retrievable on demand**. It ships two things to this session:

1. An **MCP server** (`furl`) exposing six tools.
2. A **PostToolUse hook** that compresses big tool outputs automatically.

**Retrieval is pull-based, not push-based.** The compressed text you read does not
contain the dropped rows. To inspect a specific dropped item, call `furl_retrieve` for
it by pattern, field, or line range. Retrieval is byte-exact for raw text, and a
structured JSON array comes back as a semantically-complete re-serialization of the
same rows rather than the original bytes. Nothing is lost, but a one-off anomaly
buried in otherwise-repetitive data will not appear in the compressed
view unless you query for it. Trust a compressed summary for the shape of the data, not
for surfacing an anomaly you were not already looking for.

## Current harness status

Automatic, hands-off compression works on Claude Code 2.1.163 and newer. The PostToolUse hook emits `updatedToolOutput` mirrored to the originating tool's output shape; Claude Code 2.1.163 and newer validate that replacement against the tool's schema, and the mirrored shape passes, so the compressed output reaches the model. Both external audits confirmed this live on 2.1.212. Shape-mirroring was built for [anthropics/claude-code#68951](https://github.com/anthropics/claude-code/issues/68951), where an earlier bare-string replacement was dropped on a schema mismatch. `WebSearch` still passes through uncompressed, because its whole-object result has no single text field to mirror onto. Unaffected on every version: the manual MCP tools furl_compress, furl_retrieve, and the rest, and durable `<<ccr:HASH>>` storage and retrieval. Below 2.1.163 the SessionStart status line and the first-run note say so directly instead of claiming PostToolUse compression is active, naming the detected version; the PreToolUse pipe (below) is unaffected either way. LIBRARY.md carries the canonical harness status.

**Counters:** `furl_stats` shows `store.hook_activity.hook_invocations_seen` and `hook_compressions_applied`, cross-process and cumulative. A rising `hook_compressions_applied` confirms the hook is compressing and, on 2.1.163 and newer, delivering the shorter output the model reads; below the floor it stops incrementing instead, bucketed under `hook_noop:below-version-floor`. `store.post_tool_use_compression` reports the detected host version and whether it can receive a replacement at all, independent of the counters.

**Real savings now (enabled by default):** a **PreToolUse** pipe compresses a `Bash`
command's stdout at the source (so the tool result *is* the compressed form, original
retrievable via `furl_retrieve`) — it doesn't use `updatedToolOutput`, so it works
today. Disable it with `FURL_PRETOOL_PIPE=0` (`false`/`off`/`no`/`disabled` also work,
case-insensitively); unset, empty, or any other value leaves it on. Trade-offs:
Bash-only; the rewrite is transcript-visible (a `# furl-pipe` comment); exit code
preserved exactly; stderr is not captured and flows live, but stderr/stdout
interleaving is not preserved (all stderr precedes the compressed stdout; `2>&1`
merges); fail-open (worst case the command runs unwrapped, uncompressed); adds
~0.3–0.5 s per rewritten call (two `uv` resolves; a fresh environment pays a
one-time resolve/build on the first call). It rewrites Bash **only when there
are zero readable Bash permission rules**: if any `Bash` deny/ask/allow rule
exists in any scope Claude Code uses — enterprise managed (incl. the
`CLAUDE_CODE_MANAGED_SETTINGS_PATH` override), project (both `CLAUDE_PROJECT_DIR`
and the working dir), and user (both `~/.claude` and `CLAUDE_CONFIG_DIR`)
settings — it leaves all Bash untouched so your rules apply exactly as native, a
total boundary no command shape can bypass. Unreadable settings (or a set-but-
unresolvable config-path override) also force passthrough. Residual blindness:
CLI `--permission-mode`/`--disallowedTools` flags, SDK `managedSettings`, and
API-fetched remote org policy (`CLAUDE_CODE_REMOTE_SETTINGS_PATH`); if you
restrict Bash only through those, set `FURL_PRETOOL_PIPE=0`. Known limitations
(redaction gaps on fail-open paths, heredoc edge, permission-rule visibility
bounds): see the plugin README.

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
store and leaves a marker like `<<ccr:a1b2c3>>` in its place. `<<ccr:a1b2c3>>` is
the representative shape. The engine emits several marker shapes across two hash
widths, including bracket forms like `[N items compressed to M. Retrieve more:
hash=H]`. They all retrieve the same way: pass the hash to `furl_retrieve`.
LIBRARY.md carries the full CCR marker grammar.

When you need the full content behind a marker, **call `furl_retrieve` with that
hash** — it returns the stored original, byte-exact for raw text and a
semantically-complete re-serialization for a structured JSON array, as long as the
entry is still within its retention window. **The plugin's 24-hour default
(`FURL_CCR_TTL_SECONDS=86400`) is a ceiling, not a guarantee: the store also caps
at 1000 live entries per project, and a single moderately-sized tool output can
consume dozens of those, so a handful of large outputs typically evict well
before 24 hours.** Past either limit the entry is gone and a retrieve is a loud
miss, never a silent wrong answer; check `furl_stats` for live entry counts
against the cap. The hook and the `furl` MCP server share one durable
per-project SQLite store, `~/.furl/ccr-ns-<hash>.sqlite3`, keyed by
`FURL_CCR_PROJECT_DIR` so one project never sees another's entries. The global
`~/.furl/ccr.sqlite3` is used only when project scoping is turned off, and it is the
`furl` CLI default. Either way, markers the hook creates are retrievable through
`furl_retrieve`.

What a marker leaves in place depends on the offloaded **input shape** — the
columnar table is not the universal case:

- A **structured JSON array of objects** compresses to a compact columnar table
  (`[N]{col:type,...}`, decoded by the MCP legend) with the full rows behind the marker.
- A **JSON object with one dominant inner array** (e.g. a Chrome trace) leaves an
  `_ccr_summary` preview: schema, per-field value histograms, and numeric ranges.
- **Line-oriented text** (logs, stack traces) is *not* tabled — it leaves a head+tail
  excerpt, plus any ERROR, Traceback, or other severity lines lifted from the omitted
  middle so a buried error stays visible in the compressed view, with the full text
  behind the marker.

**Dedup markers in a table (`_dup_count`, `<varies>`).** A kept row in the columnar
table can carry a `_dup_count: N` field: N original rows shared this row's content and
collapsed into it, so the row stands for N originals, not one. When those rows differed
in a high-cardinality identity column — a per-row id, timestamp, or counter — that
column shows the `<varies>` sentinel instead of a concrete value, because the N rows
each had a *different* one. Read `<varies>` as "N distinct values here", never as one
id or timestamp that recurred N times; the concrete per-row values are behind the
marker, so `furl_retrieve` the rows when you need them. `<varies>` is a **reserved**
sentinel: if a row's real identity value ever is literally the string `<varies>` and
is constant across its family it is kept as-is and reads the same, and either way the
exact per-row originals stay recoverable with `furl_retrieve`.

For the array and summary cases you usually want a **slice, not the whole thing**.
The summary carries a `retrieve` hint telling you which fields to filter on. Pass a
row-select to `furl_retrieve`:
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
| `FURL_PRETOOL_PIPE` | on | The PreToolUse pipe (Bash-only, real savings on today's harness — see "Current harness status") runs by default. Set `0`/`false`/`off`/`no`/`disabled` (case-insensitive) to disable; unset, empty, or any other value leaves it on. Disabled is a byte-identical no-op. |
| `FURL_STATUS_LINE` | on | Set `0` to silence the one-line `furl … · engine furl-ctx …` SessionStart status signal. Must be exported in the environment Claude Code launches from — the status hook runs `sh -c`, which does not source login profiles. |
| `FURL_CCR_BACKEND` | `sqlite` (set by the plugin) | CCR store backend. Must match between the hook and the `furl` server for retrieval to work. |
| `FURL_CCR_SPILL` | `1` (set by the plugin) | Durable per-namespace spill tier. When on, a capacity-evicted entry is demoted to a per-project `ccr-ns-<hash>-spill.sqlite3` file instead of dropped, so its marker stays retrievable past the 1000-entry cap (bounded by the spill's own row cap and TTL). Set `0` to opt out. Must match between the hook and the `furl` server. |
| `FURL_CCR_TTL_SECONDS` | `86400` = 24h (set by the plugin) | How long offloaded originals stay retrievable before they expire. Lower to reclaim disk sooner; raise for a longer retrieval window. |
| `FURL_CCR_PROJECT_DIR` | auto, per project (set by the plugin) | Scopes the CCR store to the current project so one machine-global `~/.furl` store never surfaces or evicts another project's entries. Derived automatically from the project root; set to `""` to share one store across all projects — this is also how you read a pre-1.0 global store after upgrading. |

The full `FURL_*` reference (workspace dir, store paths, row caps, circuit breaker)
is in [`LIBRARY.md`](../../../../LIBRARY.md) → "Configuration".

## How to disable

- **Just the hook:** set `FURL_HOOK_ENABLED=0` (leaves the MCP tools available).
- **Everything:** disable the `furl` plugin in Claude Code.

## Prerequisite

Both the hook and the MCP server launch through [`uv`](https://docs.astral.sh/uv/)
(`uv run --with "furl-ctx[mcp]==1.3.0" …`), which fetches Furl from PyPI on first use — no
`pip install`, no Rust toolchain. The version is pinned so every launch resolves the same
wheel deterministically instead of whatever `uv`'s cache last held; upgrades arrive through
plugin updates, which bump the pin. The
only requirement is `uv` on the PATH. If `uv` is missing, the MCP server won't start
and the hook fails open (passes output through unchanged).
