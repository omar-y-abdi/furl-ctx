# Furl — Claude Code plugin

Bundles Furl's context compression into Claude Code as a single plugin:

- **MCP server** (`furl`) → the `furl_compress`, `furl_retrieve`, `furl_stats` tools.
- **PostToolUse hook** → automatically compresses large tool outputs before they
  enter context (fail-open; never breaks a tool call).
- **Skill** (`furl`) → explains how it works, the `<<ccr:HASH>>` retrieval flow, and
  how to tune or disable it.

## Install (2 commands)

**1 — Install Furl** into the same Python that Claude Code will run:

```bash
pip install "furl-ctx[mcp]"
```

**2 — Add the plugin** from this repo (it ships a marketplace manifest):

```
/plugin marketplace add /path/to/headroom/plugins/furl
/plugin install furl@furl
```

Run those two `/plugin …` lines inside Claude Code (they are slash commands, not
shell commands). The first registers this directory as a marketplace named `furl`;
the second installs the `furl` plugin from it. Restart the session (or re-enable the
plugin) so the MCP server and hook load.

> Once Furl is published to a public marketplace you'll be able to skip step 2's
> `marketplace add` and install directly. Until then, the local-path form above is
> the honest, working route. To install straight from Git instead of a local path,
> point `marketplace add` at the repo URL:
> `/plugin marketplace add https://github.com/omar-y-abdi/furl` (adjust to the real
> repository), then `/plugin install furl@furl`.

**Verify** it loaded: run `/plugin` (the `furl` plugin should be enabled) and ask
Claude to call `furl_stats` — it should return session stats from the `furl` MCP
server.

### Prerequisite detail (be honest about this)

Both the MCP server and the hook invoke the **`python` on your PATH** (they run
`python -m furl_ctx.ccr.mcp_server` and `import furl_ctx`). That interpreter must be
the one where you ran `pip install "furl-ctx[mcp]"`. If you use a virtualenv or
`pyenv`, make sure the active `python` resolves to it, or the MCP server won't start
and the hook will silently fail-open (do nothing) rather than error.

## What each piece does

### MCP server (`.mcp.json`)

Registers one server, keyed `furl` (short on purpose — keeps generated tool names
like `mcp__furl__furl_compress` well under Claude Code's 64-char limit):

```json
{ "mcpServers": { "furl": {
  "command": "python",
  "args": ["-m", "furl_ctx.ccr.mcp_server"],
  "env": { "FURL_CCR_BACKEND": "sqlite", "FURL_CCR_TTL_SECONDS": "86400" }
}}}
```

`FURL_CCR_BACKEND=sqlite` makes the CCR store durable at `~/.furl/ccr.sqlite3`, so
originals survive across processes and can be retrieved later.

### Compression hook (`hooks/hooks.json` + `hooks/compress_tool_output.py`)

A `PostToolUse` hook on external-output tools (`Bash`, `WebFetch`, `WebSearch`,
`Task`). Your own `Read`/`Grep`/`Glob` file access is deliberately left untouched by
default, so a later `Edit` still sees exact file bytes. For each result it:

1. Skips Furl's own tools and anything already carrying `<<ccr:` markers.
2. Skips outputs below `FURL_HOOK_MIN_CHARS` (default 2000).
3. Compresses the rest via `furl_ctx.compress(...)` and replaces the tool output
   **only if the result is genuinely smaller**.

It pins the **same** `FURL_CCR_BACKEND=sqlite` as the server, so markers it creates
are retrievable through `furl_retrieve`. It is **fail-open**: any error passes the
original output through unchanged (exit 0, no output), so a compression problem can
never break your tool call.

**Tuning / disabling** (env vars):

| Variable | Default | Effect |
|----------|---------|--------|
| `FURL_HOOK_ENABLED` | on | `0`/`false`/`off` disables the hook (MCP tools stay). |
| `FURL_HOOK_MIN_CHARS` | `2000` | Size threshold before compressing. |
| `FURL_HOOK_MODEL` | `claude-sonnet-4-5-20250929` | Model name for token counting. |

The full `FURL_*` reference is in the repo's top-level `README.md` → "Configuration".

### Skill (`skills/furl/SKILL.md`)

Auto-activates when you ask what Furl is doing, why output looks compressed, how to
retrieve originals, or how to tune/disable it.

## Structure

```
plugins/furl/
├── .claude-plugin/
│   ├── plugin.json          # manifest (name, version, skills)
│   └── marketplace.json     # lets `/plugin marketplace add` find this plugin
├── .mcp.json                # registers the `furl` MCP server
├── hooks/
│   ├── hooks.json           # PostToolUse registration (auto-loaded)
│   └── compress_tool_output.py   # the fail-open compression hook
├── skills/
│   └── furl/
│       └── SKILL.md         # how-it-works skill
└── README.md
```
