# Furl — Claude Code plugin

Bundles Furl's context compression into Claude Code as a single plugin:

- **MCP server** (`furl`) → the `furl_compress`, `furl_retrieve`, `furl_stats`, `furl_purge`, `furl_search`, `furl_list` tools. A seventh tool, `furl_read`, is off by default (enable with `FURL_MCP_READ=1`).
- **PostToolUse hook** → automatically compresses large tool outputs before they
  enter context (fail-open; never breaks a tool call).
- **Skill** (`furl`) → explains how it works, the `<<ccr:HASH>>` retrieval flow, and
  how to tune or disable it.

## Install (2 commands)

Inside Claude Code:

```
/plugin marketplace add omar-y-abdi/furl
/plugin install furl@furl
```

That is the whole install. It registers this repo as a marketplace named `furl`
and installs the plugin — MCP server, compression hook, and skill. **No `pip
install`, no venv:** Furl fetches itself on first use via
[`uv`](https://docs.astral.sh/uv/) from prebuilt wheels on PyPI (Linux
x86_64/aarch64, macOS arm64/Intel — no Windows wheels yet, so Windows needs a
Rust toolchain to build from source). The one requirement is
`uv` on your PATH — the same bootstrap the official
[serena](https://github.com/oraios/serena) plugin uses.

Restart the session (or re-enable the plugin) so the MCP server and hook load.
First use triggers a one-time wheel download (a few seconds).

**Verify:** run `/mcp` (the `furl` server should be listed) and ask Claude to call
`furl_stats` — it returns session stats from the `furl` MCP server.

> **From a local clone instead of GitHub:** `/plugin marketplace add /path/to/headroom`
> (the repo root ships `.claude-plugin/marketplace.json` pointing at `./plugins/furl`),
> then `/plugin install furl@furl`.

## What each piece does

### MCP server (`.mcp.json`)

Registers one server, keyed `furl` (short on purpose — keeps generated tool names
like `mcp__furl__furl_compress` under Claude Code's 64-char limit). It launches
through `uv`, which fetches Furl on first use — no prior install:

```json
{ "mcpServers": { "furl": {
  "command": "sh",
  "args": ["-lc", "exec uv run --no-project --with 'furl-ctx[mcp]' python -m furl_ctx.ccr.mcp_server"],
  "env": { "FURL_CCR_BACKEND": "sqlite", "FURL_CCR_TTL_SECONDS": "86400" }
}}}
```

The `sh -lc` wrapper runs a login shell so `uv` is found on PATH even when Claude
Code launches with a minimal environment (a login shell that lacks `uv` is the one
failure mode — install [`uv`](https://docs.astral.sh/uv/) and it resolves). `exec`
hands stdio and signals straight to the server. `FURL_CCR_BACKEND=sqlite` makes the
CCR store durable at `~/.furl/ccr.sqlite3`, so originals survive across processes.
`FURL_CCR_TTL_SECONDS=86400` keeps each offloaded original retrievable for 24 hours
(the plugin default); raise or lower it to widen or shrink the retention window.

### Compression hook (`hooks/hooks.json` + `hooks/compress_tool_output.py`)

A `PostToolUse` hook on external-output tools (`Bash`, `WebFetch`, `WebSearch`,
`Task`). Your own `Read`/`Grep`/`Glob` file access is deliberately left untouched by
default, so a later `Edit` still sees exact file bytes. For each result it:

1. Skips Furl's own tools and anything already carrying `<<ccr:` markers.
2. Skips outputs below `FURL_HOOK_MIN_CHARS` (default 2000).
3. Compresses the rest via `furl_ctx.compress(...)` and replaces the tool output
   **only if the result is genuinely smaller**.

Like the server, the hook launches through the same `uv` wrapper
(`sh -lc 'uv run … python3 … || true'`), so it needs no prior `pip install`. It
pins the **same** `FURL_CCR_BACKEND=sqlite` as the server, so markers it creates
are retrievable through `furl_retrieve`. It is **fail-open**: any error — including
`uv` being unavailable — passes the original output through unchanged (exit 0, no
output), so a compression problem can never break your tool call.

**Tuning / disabling** (env vars):

| Variable | Default | Effect |
|----------|---------|--------|
| `FURL_HOOK_ENABLED` | on | `0`/`false`/`off` disables the hook (MCP tools stay). |
| `FURL_HOOK_MIN_CHARS` | `2000` | Size threshold before compressing. |
| `FURL_HOOK_MODEL` | `claude-sonnet-4-5-20250929` | Model name for token counting. |
| `FURL_HOOK_EXCLUDE_TOOLS` | (none) | Comma-separated tools never to compress — exact or `mcp__db__*` globs. |
| `FURL_HOOK_MODE` | `normal` | `aggressive` compresses more (code + smaller outputs). |
| `FURL_HOOK_VERBOSE` | off | `1` prints a one-line per-compression savings summary to stderr. |

The full `FURL_*` reference is in [`LIBRARY.md`](../../LIBRARY.md) → "Configuration".

### Skill (`skills/furl/SKILL.md`)

Auto-activates when you ask what Furl is doing, why output looks compressed, how to
retrieve originals, or how to tune/disable it.

## Structure

```
.claude-plugin/
└── marketplace.json         # repo-root marketplace → source ./plugins/furl
plugins/furl/
├── .claude-plugin/
│   └── plugin.json          # plugin manifest (name, version, skills)
├── .mcp.json                # registers the `furl` MCP server
├── hooks/
│   ├── hooks.json           # PostToolUse registration (auto-loaded)
│   └── compress_tool_output.py   # the fail-open compression hook
├── skills/
│   └── furl/
│       └── SKILL.md         # how-it-works skill
└── README.md
```
