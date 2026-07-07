---
name: furl
description: How the Furl context-compression plugin works — the furl_compress / furl_retrieve / furl_stats MCP tools, the PostToolUse hook that shrinks large tool outputs, the <<ccr:HASH>> retrieval flow, and the FURL_* environment knobs to tune or disable it. Use when the user asks what Furl is doing, why a tool output looks compressed or contains <<ccr:...>> markers, how to retrieve original content, how to tune compression thresholds, or how to turn the hook off.
version: 0.1.0
---

# Furl — context compression for Claude Code

Furl reduces the tokens large tool outputs cost by compressing them, while keeping
every dropped byte **retrievable on demand**. It ships two things to this session:

1. An **MCP server** (`furl`) exposing three tools.
2. A **PostToolUse hook** that compresses big tool outputs automatically.

## The MCP tools

- `furl_compress` — compress a string on demand. Returns compressed text plus a
  `hash`; the original is stored for later retrieval.
- `furl_retrieve` — get original, uncompressed content back. Pass a `<<ccr:HASH>>`
  marker's hash (or a free-text query to search stored entries).
- `furl_stats` — session compression statistics (compressions, tokens saved, cost).

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
hash** — it returns the byte-exact original (within the retention window). The hook
and the `furl` MCP server share one durable SQLite store (`~/.furl/ccr.sqlite3`),
so markers the hook creates are retrievable through `furl_retrieve`.

## Tuning (environment variables)

Set these in the plugin's `hooks/hooks.json` / `.mcp.json` env, or your shell:

| Variable | Default | Effect |
|----------|---------|--------|
| `FURL_HOOK_ENABLED` | on | Set `0`/`false`/`off` to disable the hook entirely. |
| `FURL_HOOK_MIN_CHARS` | `2000` | Minimum tool-output length before the hook attempts compression. Raise to compress less, lower to compress more. |
| `FURL_HOOK_MODEL` | `claude-sonnet-4-5-20250929` | Model name used for token counting during compression. |
| `FURL_HOOK_EXCLUDE_TOOLS` | (none) | Comma-separated tool names never to compress — exact (`Bash`) or fnmatch globs (`mcp__db__*`). Furl's own tools are always excluded. |
| `FURL_HOOK_MODE` | `normal` | `aggressive` also compresses code in the blob and squeezes smaller outputs; `normal` keeps the default behavior. |
| `FURL_CCR_BACKEND` | `sqlite` (set by the plugin) | CCR store backend. Must match between the hook and the `furl` server for retrieval to work. |
| `FURL_CCR_TTL_SECONDS` | `86400` (set by the plugin) | How long offloaded originals stay retrievable. |

The full `FURL_*` reference (workspace dir, store paths, row caps, circuit breaker)
is in [`LIBRARY.md`](../../../../LIBRARY.md) → "Configuration".

## How to disable

- **Just the hook:** set `FURL_HOOK_ENABLED=0` (leaves the MCP tools available).
- **Everything:** disable the `furl` plugin in Claude Code.

## Prerequisite

Both the hook and the MCP server launch through [`uv`](https://docs.astral.sh/uv/)
(`uv run --with "furl-ctx[mcp]" …`), which fetches Furl from PyPI on first use — no
`pip install`, no Rust toolchain. The
only requirement is `uv` on the PATH. If `uv` is missing, the MCP server won't start
and the hook fails open (passes output through unchanged).
