# Furl — Claude Code plugin

Context compression for AI agents, bundled into Claude Code as a single plugin. The
on-demand MCP toolkit your agent calls directly works on every Claude Code version
today. Automatic, hands-off compression is pending an upstream Claude Code fix, issue
[#68951](https://github.com/anthropics/claude-code/issues/68951), and the Current
harness status section below states exactly where it stands. This plugin ships three
things:

- **MCP server** (`furl`) → the `furl_compress`, `furl_retrieve`, `furl_stats`, `furl_purge`, `furl_search`, `furl_list` tools. A seventh tool, `furl_read`, is off by default (enable with `FURL_MCP_READ=1`).
- **PostToolUse hook** → automatically compresses large tool outputs before they
  enter context (fail-open; never breaks a tool call).
- **Skill** (`furl`) → explains how it works, the `<<ccr:HASH>>` retrieval flow, and
  how to tune or disable it.

**Retrieval is pull-based, not push-based.** Compressed output does not contain the
dropped rows. To see a specific dropped item, the agent calls `furl_retrieve` for it
by pattern, field, or line range. Every retrieval is byte-exact and the data is never
lost, but a one-off anomaly buried in otherwise-repetitive data will not appear in the
compressed view unless someone knows to query for it.

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
status line; the observability counters below; and the `FURL_PRETOOL_PIPE` pipe (on by
default), which delivers real savings today.

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
project also prints a one-line stderr heads-up. (These counters are live as of this
plugin release — the pinned engine, `furl-ctx` 1.2.0+, ships the store counter API that
populates them.)
Once the pipe has run, `pipe_invocations_seen` / `pipe_compressions_applied` /
`pipe_noop_reasons` appear in the same `store.hook_activity` block.

### `FURL_PRETOOL_PIPE` — real savings on today's harness (on by default)

The PreToolUse pipe is **enabled by default** — real token savings on today's harness.
Disable it with `FURL_PRETOOL_PIPE=0`. **What you're getting by default** (the
trade-offs, up front):

- **Bash-only** — it rewrites a command's stdout; other tools are untouched.
- **The command mutation is visible in the transcript** — the rewrite carries a
  `# furl-pipe (FURL_PRETOOL_PIPE=0 to disable)` comment so it is never a silent
  substitution.
- **Permission rules are respected (provably):** the pipe rewrites Bash **only when you
  have no Bash permission rules at all**. If **any** `Bash` rule of any kind —
  `permissions.deny`, `permissions.ask`, **or** `permissions.allow` — exists in
  enterprise managed settings, project `.claude/settings.json` /
  `.claude/settings.local.json`, or the user-scope `~/.claude/` equivalents, the pipe
  **leaves every Bash command untouched**, so your rules apply exactly as native — no
  command shape can bypass them. (`allow` counts because an allow-list makes unlisted
  commands restricted.) Unreadable or malformed settings also force passthrough. The
  trade-off is honest: once you add any Bash permission rule, the pipe stops compressing
  Bash for that session.
- **Latency:** ~0.3–0.5 s added per rewritten Bash call (two `uv` resolves: the shell
  gate plus the in-command compressor). The first call in a fresh environment pays a
  one-time resolve/build — seconds to tens of seconds.
- **stderr is not captured and flows live** — but because stdout is buffered for
  compression, stderr/stdout **interleaving is not preserved**: in a merged view all
  stderr appears before the (possibly compressed) stdout. `cmd 2>&1` merges both into
  the compressed stream.
- **Exit code preserved exactly**; small outputs pass through raw; **fail-open** twice
  over — a compressor that cannot start falls back to the raw captured output, and if
  the stdout tempfile cannot even be created the original command runs **unwrapped**
  (uncompressed); never a broken command.

How it works: a **PreToolUse** hook rewrites a `Bash` command so its stdout is piped
through the Furl compressor **before** it becomes the tool result — so the model-visible
output **is** the compressed form (the original is stored under a `<<ccr:HASH>>` marker,
retrievable via `furl_retrieve`, exactly like the PostToolUse path). It does **not**
rely on `updatedToolOutput`, so it works now. Before rewriting, the hook reads the
deny/ask permission rules it can see and passes any matching, compound, or doubtful
command through untouched — it fails toward no-compression, never toward masking a
permission rule. Opt-out semantics: only an explicitly falsy value —
`0`/`false`/`off`/`no`/`disabled`, case-insensitive (whitespace ignored) — disables it;
unset, empty, or any other value (including typos) leaves it ON. Disabling is cheap:
the falsy path spends no `uv` resolve.

#### Known limitations

- **Redaction gaps on fail-open paths:** `FURL_REDACT_PATTERNS` applies on the normal
  path (same builder as the PostToolUse hook), but **not** to binary/undecodable stdout
  or when `furl_ctx` cannot load — those pass through raw and unredacted. The raw
  stdout also sits in a `0600` tempfile for the command's runtime.
- **Unterminated heredoc** (malformed input): bare bash is lenient, but the wrapped
  command fails with a shell syntax error (exit 2, empty output). No wrapper text or
  tempfile is leaked.
- **Trailing odd backslashes** (pathological input): the command still runs with its
  exact exit code, but the wrapper's line continuation means a literal trailing
  backslash is not preserved in stdout — and bare-bash behavior itself differs between
  versions here (GNU bash 5 `-c` keeps the dangling backslash literal; macOS bash 3.2
  drops it).
- **Permission-rule visibility:** the guard is intentionally coarse and total — it
  rewrites Bash **only when there are zero readable Bash permission rules**. It reads
  every scope Claude Code actually uses, including relocations: **enterprise managed
  settings** (the per-OS `managed-settings.json` + its `managed-settings.d` fragments, or
  the `CLAUDE_CODE_MANAGED_SETTINGS_PATH` override), **project** settings
  (`.claude/settings.json` / `.claude/settings.local.json` under both `CLAUDE_PROJECT_DIR`
  and the working dir), and **user** settings (under both `~/.claude` **and**
  `CLAUDE_CONFIG_DIR`). If **any** `Bash` rule (deny/ask/allow — an allowlist counts, since
  it makes unlisted commands restricted) exists in those, **all** Bash passes through
  untouched, so your native rules apply exactly — a coarse-but-provable boundary that no
  command shape (wrapper-hidden `env`/`sudo`/`flock`, compound, absolute path, …) can slip
  past, because when a rule exists nothing is rewritten (this also avoids the auto-mode
  obfuscation classifier entirely). The genuine residual blindness — set
  `FURL_PRETOOL_PIPE=0` if you restrict Bash **only** through these — is CLI
  `--permission-mode` / `--disallowedTools` flags, SDK `managedSettings` options, and
  API-fetched remote org policy (`CLAUDE_CODE_REMOTE_SETTINGS_PATH` / `remoteSettings`, a
  session-scope fetch rather than a file). (`~/.claude.json` is not read: it carries no
  deny/ask rules, only `allowedTools`.)
- **Cold-start cost:** with the pipe and the PostToolUse hook both enabled, one Bash
  call can spend up to 3 `uv` resolves before caches warm.
- **Cosmetic:** bash error messages gain a `line N:` prefix from the multi-line wrapper.

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

**Prerequisite:** [`uv`](https://docs.astral.sh/uv/) on your PATH — the same bootstrap
the official [serena](https://github.com/oraios/serena) plugin uses.

Inside Claude Code:

```
/plugin marketplace add omar-y-abdi/furl-ctx
/plugin install furl@furl
```

That is the whole install. It registers this repo as a marketplace named `furl`
and installs the plugin — MCP server, compression hook, and skill. **No `pip
install`, no venv:** Furl fetches itself on first use via `uv` from prebuilt
wheels on PyPI (Linux x86_64/aarch64, macOS arm64/Intel — no Windows wheels yet,
so Windows needs a Rust toolchain to build from source).

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
  "args": ["-lc", "exec uv run --no-project --with 'furl-ctx[mcp]==1.2.0' python -m furl_ctx.ccr.mcp_server"],
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
`furl-ctx[mcp]==1.2.0` pin is deterministic — every launch resolves the same wheel instead
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
| `FURL_PRETOOL_PIPE` | **on** | The PreToolUse pipe (Bash-only, real savings on today's harness — see "Current harness status") runs **by default**. Only an explicitly falsy value — `0`/`false`/`off`/`no`/`disabled`, case-insensitive, whitespace ignored — disables it; unset/empty/any other value leaves it on. It rewrites Bash only when there are zero readable Bash permission rules; if any deny/ask/allow `Bash` rule exists (enterprise/project/local/user settings), it leaves Bash untouched (see Known limitations). The gate runs via `sh -lc` (a login shell), so an export in your login profile or in the environment Claude Code launches from takes effect. |
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
│   ├── pretool_pipe.py      # PreToolUse pipe rewrite (on by default; FURL_PRETOOL_PIPE=0 off)
│   ├── pipe_compress.py     # the stdout compressor the pipe rewrite runs
│   └── _furl_ccr_counters.py     # shared cross-process observability counters
├── skills/
│   └── furl/
│       └── SKILL.md         # how-it-works skill
└── README.md
```
