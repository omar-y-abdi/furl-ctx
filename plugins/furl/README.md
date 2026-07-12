# Furl — Claude Code plugin

Bundles Furl's context compression into Claude Code as a single plugin:

- **MCP server** (`furl`) → the `furl_compress`, `furl_retrieve`, `furl_stats`, `furl_purge`, `furl_search`, `furl_list` tools. A seventh tool, `furl_read`, is off by default (enable with `FURL_MCP_READ=1`).
- **PostToolUse hook** → automatically compresses large tool outputs before they
  enter context (fail-open; never breaks a tool call).
- **Skill** (`furl`) → explains how it works, the `<<ccr:HASH>>` retrieval flow, and
  how to tune or disable it.

## Current harness status (Claude Code ≥ 2.1.163)

**Heads-up:** Claude Code ≥ 2.1.163 currently **ignores a PostToolUse hook's
replacement output** — `hookSpecificOutput.updatedToolOutput` is silently dropped, so
the model still sees the **original** tool output
([anthropics/claude-code#68951](https://github.com/anthropics/claude-code/issues/68951);
[our 2.1.207 repro](https://github.com/anthropics/claude-code/issues/68951#issuecomment-4951540435)).
The compression hook still **runs** and still **stores** originals — you just don't get
the token savings from the default path until upstream fixes the drop.

**What still works today:** the MCP tools (`furl_compress`, `furl_retrieve`,
`furl_search`, `furl_list`, `furl_stats`, `furl_purge`) — manual compression and
retrieval are unaffected; durable storage + `<<ccr:HASH>>` retrieval; the SessionStart
status line; the observability counters below; and the opt-in `FURL_PRETOOL_PIPE` path.

The hook **keeps emitting** `updatedToolOutput`, so the default path revives
automatically — with no plugin release — the moment upstream fixes #68951.

### Observability counters

Every PostToolUse hook run tallies into the shared per-project CCR store (cross-process,
cumulative), surfaced by `furl_stats` under `store.hook_activity`:

- `hook_invocations_seen` — how many times the hook ran.
- `hook_compressions_applied` — how many replacements it produced (and *would* have
  delivered if not dropped).

**How to read them:** if `hook_invocations_seen` is rising but your context still shows
raw tool output, the harness is dropping the replacements — see
[#68951](https://github.com/anthropics/claude-code/issues/68951). The first hook run per
project also prints a one-line stderr heads-up. (These activate once the runtime
`furl-ctx` engine ships the store counter API; the hook is armed for them now.)

### `FURL_PRETOOL_PIPE` — real savings on today's harness (opt-in, default OFF)

Set `FURL_PRETOOL_PIPE=1` to enable a **PreToolUse** hook that rewrites a `Bash`
command so its stdout is piped through the Furl compressor **before** it becomes the
tool result — so the model-visible output **is** the compressed form (the original is
stored under a `<<ccr:HASH>>` marker, retrievable via `furl_retrieve`, exactly like the
PostToolUse path). It does **not** rely on `updatedToolOutput`, so it works now.

Trade-offs:

- **Bash-only** — it rewrites a command's stdout; other tools are untouched.
- **The command mutation is visible in the transcript** — the rewrite carries a
  `# furl-pipe (FURL_PRETOOL_PIPE=1)` comment so it is never a silent substitution.
- **Exit code preserved exactly**, **stderr passes through untouched**, small outputs
  pass through raw, and it is **fail-open** — a compressor that cannot even start falls
  back to the raw captured output; never a broken command.
- Default **OFF** is a byte-identical no-op (the default-off path spends no `uv` resolve).

## Scope (global install, per-project opt-out)

Installing the plugin enables Furl **globally** — the compression hook and MCP tools
apply to every project you open in Claude Code. To opt a **single project** out of the
hook, set `FURL_HOOK_ENABLED=0` in that project's `.claude/settings.json` `env` block:

```json
{ "env": { "FURL_HOOK_ENABLED": "0" } }
```

Claude Code applies `settings.json` `env` to the session and to the subprocesses it
spawns — this PostToolUse hook included — and project settings override user settings
([settings docs](https://code.claude.com/docs/en/settings)), so this scopes the opt-out
to that one project while the MCP tools stay available. Equivalent broader alternatives:
`export FURL_HOOK_ENABLED=0` in the shell you launch `claude` from, or disable the plugin.

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
  "args": ["-lc", "exec uv run --no-project --with 'furl-ctx[mcp]==1.1.0' python -m furl_ctx.ccr.mcp_server"],
  "env": { "FURL_CCR_BACKEND": "sqlite", "FURL_CCR_TTL_SECONDS": "86400" }
}}}
```

The `sh -lc` wrapper runs a login shell so `uv` is found on PATH even when Claude
Code launches with a minimal environment (a login shell that lacks `uv` is the one
failure mode — install [`uv`](https://docs.astral.sh/uv/) and it resolves). `exec`
hands stdio and signals straight to the server. `FURL_CCR_BACKEND=sqlite` makes the
CCR store durable on disk under `~/.furl/` (a per-project `ccr-ns-<hash>.sqlite3`),
so originals survive across processes. `FURL_CCR_TTL_SECONDS=86400` keeps each
offloaded original retrievable for 24 hours (the plugin default) — governing the
hook's offloads and the MCP tools' stores alike; raise or lower it to widen or
shrink the retention window. The
`furl-ctx[mcp]==1.1.0` pin is deterministic — every launch resolves the same wheel instead
of whatever `uv`'s cache last held; upgrades ship through plugin updates, which bump the pin.

**Plugin version vs. engine version.** This plugin (`plugin.json`) and the pinned engine
`furl-ctx` (the pin above) version independently — a plugin release doesn't imply an engine
bump, or vice versa. The plugin version is what the SessionStart status line and the Claude
Code plugin marketplace show; the engine version is what's pinned above and what ships to
[PyPI](https://pypi.org/project/furl-ctx/) / [CHANGELOG.md](../../CHANGELOG.md).

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

**Very large outputs.** When a tool result is so large that Claude Code itself
persists it to a file and hands the model a file reference instead of the inline
text, the hook has no inline output to compress — compression applies to tool
output that actually enters context.

**Tuning / disabling** (env vars):

| Variable | Default | Effect |
|----------|---------|--------|
| `FURL_HOOK_ENABLED` | on | `0`/`false`/`off` disables the hook (MCP tools stay). |
| `FURL_HOOK_MIN_CHARS` | `2000` | Size threshold before compressing. |
| `FURL_HOOK_MODEL` | `claude-sonnet-4-5-20250929` | Model name for token counting. |
| `FURL_HOOK_EXCLUDE_TOOLS` | (none) | Comma-separated tools never to compress — exact or `mcp__db__*` globs. |
| `FURL_HOOK_MODE` | `normal` | `aggressive` compresses more (code + smaller outputs). |
| `FURL_HOOK_VERBOSE` | off | `1` prints a one-line per-compression savings summary to stderr. |
| `FURL_PRETOOL_PIPE` | off | `1`/`true`/`on` enables the opt-in PreToolUse pipe (Bash-only, real savings on today's harness — see "Current harness status"). Default off is a byte-identical no-op. |
| `FURL_STATUS_LINE` | on | `0` silences the one-line SessionStart status signal. Export it in the environment Claude Code launches from — the status hook runs `sh -c`, which does not source login profiles. |

The full `FURL_*` reference is in [`LIBRARY.md`](../../LIBRARY.md) → "Configuration".

**Per-call overhead (measured).** On a warm `uv` cache the hook adds a median of
**~0.18 s per matched tool call** (N=10: 0.173–0.183 s on a macOS/Apple-silicon dev
machine), measured by feeding the shipped pinned command a below-threshold no-op payload
so the figure is pure invocation cost — `uv` resolve + Python startup + hook import, not
compression. It runs only on `Bash`/`WebFetch`/`WebSearch`/`Task` outputs and is
fail-open. A fresh `uv` cache dir still resolved in ~0.25–0.30 s here (`uv` reused
already-present wheels rather than re-downloading), so the only genuinely cold cost is
the one-time wheel fetch from PyPI on first-ever use (network-bound, paid once, then
cached). These numbers are machine- and cache-dependent — treat ~0.2 s as an
order-of-magnitude guide, not a guarantee.

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
│   ├── hooks.json           # PostToolUse + PreToolUse + SessionStart registration
│   ├── compress_tool_output.py   # the fail-open PostToolUse compression hook
│   ├── pretool_pipe.py      # opt-in PreToolUse pipe rewrite (FURL_PRETOOL_PIPE)
│   ├── pipe_compress.py     # the stdout compressor the pipe rewrite runs
│   └── _furl_ccr_counters.py     # shared cross-process observability counters
├── skills/
│   └── furl/
│       └── SKILL.md         # how-it-works skill
└── README.md
```
