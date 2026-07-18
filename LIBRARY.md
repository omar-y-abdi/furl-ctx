# Furl as a Python library

Furl is primarily a Claude Code plugin (see the [root README](README.md)), but
the same engine is a plain Python library you can drop into any app or MCP host.

## Install

Prebuilt wheels ship on [PyPI](https://pypi.org/project/furl-ctx/) — **no Rust
toolchain**, and pip auto-selects your platform's wheel (macOS arm64/x86_64,
Linux arm64/x86_64):

```bash
pip install "furl-ctx[all]"
```

**Windows has no prebuilt wheel.** PyPI ships wheels for macOS arm64/x86_64 and
Linux arm64/x86_64 only. On Windows, pip falls back to a source build, which
needs a Rust toolchain. Install Rust first with rustup and a stable toolchain,
or run under WSL where the Linux wheel applies.

Granular extras: `[mcp]` (MCP server), `[code]` (tree-sitter AST-verified code
compression, ~50 MB, opt-in), `[dev]`. Requires **Python 3.10+**.

Using `pipx`? `pipx install --python python3.13 "furl-ctx[all]"`.

## Use it

```python
# Compress inline in any Python app
from furl_ctx import compress

result = compress(messages, model="claude-sonnet-4")
# result.messages  → compressed; CCR keeps originals retrievable
```

First-call warmup: the first `compress()` in a fresh process takes about 6
seconds while the tokenizer and store initialize. Every later call in that same
process runs in about 0.02 to 0.5 seconds.

```bash
# Or run the MCP server for Claude Code / Cursor / any MCP host
python3 -m furl_ctx.ccr.mcp_server      # exposes furl_compress / _retrieve / _stats / _purge / _search / _list
```

| Your setup     | Hook in with                                  |
|----------------|-----------------------------------------------|
| Any Python app | `compress(messages, model=…)`                 |
| MCP clients    | `python3 -m furl_ctx.ccr.mcp_server`          |

## Docker

A `Dockerfile` at the repo root runs the MCP server on stdio inside a container.
Build the image once:

```bash
docker build -t furl-mcp .
```

The server speaks JSON-RPC over stdin and stdout, so run it with an interactive
stdin and no TTY:

```bash
docker run --rm -i furl-mcp
```

For Claude Code, register that command directly:

```bash
claude mcp add furl -- docker run --rm -i furl-mcp
```

The CCR store lives at `/home/furl/.furl` inside the container. Mount a volume
there to keep retrievable originals across restarts:

```bash
docker run --rm -i -v furl-store:/home/furl/.furl furl-mcp
```

## Retrieve — full or sliced

**Retrieval is pull-based, not push-based.** A `<<ccr:HASH>>` marker replaces the
dropped content in the compressed output, so the dropped rows are not in the view the
model reads. To get a specific one back, an agent has to call `retrieve()` for it by
pattern, field, or line range. Recovery is byte-exact for raw text, and a structured
JSON array comes back as a semantically-complete re-serialization of the same rows
rather than the original bytes. Nothing is lost, but a lone
anomaly inside otherwise-repetitive data will not surface in the compressed view unless
someone queries for it. A compressed summary is trustworthy for the shape of the data,
not for spotting an outlier no one thought to look for.

`compress()` offloads large, low-redundancy content to the CCR store and leaves a
`<<ccr:HASH>>` marker. `retrieve(hash)` turns a marker's hash back into content.
With **no filter argument it returns the full stored original**. That is
byte-identical for a raw-text offload, and a semantically-complete re-serialization
of the rows for a structured JSON array. If the hash has left the store window it
returns `None`, a loud and explicit miss. Passing a
filter narrows what comes back **without dumping the whole original**, so an agent
can drill into a huge offloaded array cheaply:

```python
from furl_ctx import retrieve

# Full stored original: byte-exact for text, re-serialized for JSON arrays
original = retrieve(hash)

# ROW-SELECT — keep only the rows of a JSON array of objects (or a JSON object
# with one dominant inner array, e.g. a Chrome trace {"metadata":…, "traceEvents":[…]})
# whose field matches a value:
dropped = retrieve(hash, select_field="name", select_equals="DroppedFrame")

# …or a numeric range window (inclusive; open-ended if a bound is omitted):
window = retrieve(hash, select_field="ts", select_min=404733, select_max=404999)

# Project only some columns of the selected rows, and cap the result:
cols = retrieve(hash, select_field="name", select_equals="Paint",
                fields=["name", "ts"], limit=200)

# TEXT filters over the original as lines (regex + context, or a line window):
lines = retrieve(hash, pattern=r"ERROR", context_lines=2)
head  = retrieve(hash, line_range=[1, 50])

# FIELDS projection over a top-level JSON array of objects:
ids = retrieve(hash, fields=["id", "status"])
```

Rules (they mirror the `furl_retrieve` MCP tool and share one validated spec):

- A **row-select** needs `select_field` plus **either** `select_equals` (equality)
  **or** `select_min`/`select_max` (a numeric range) — never both. A row whose
  field is missing or non-numeric is skipped from a range (never an error). It
  composes with `fields` (project the selected rows) but not with
  `pattern`/`line_range`. The result is always bounded by `limit` (default 1000);
  when more rows match, a `{"_truncated": …}` marker row is appended so a
  truncated slice is never mistaken for the full set.
- `select`/`fields` need a JSON array (or a dominant-array object for select). On
  any other shape they raise `ValueError` — never a silent empty result.
- `pattern`/`line_range` operate on the original as text lines and return matching
  lines prefixed with 1-based line numbers.
- Bad usage (an incompatible filter mix, an invalid regex/range, a filter on the
  wrong shape, or `query` together with a filter) raises `ValueError`. A store
  miss returns `None` on every path.

`resolve_markers(messages)` expands **every** resolvable marker in a message list
back to its original inline (bulk recovery), leaving unresolvable markers in place.

## CCR marker grammar

`<<ccr:HASH>>` is the representative marker, but the engine emits several shapes
across two hash widths. `furl_ctx/ccr/marker_grammar.py` is the single owner of
the wire format and pins it against the Rust producers. Whatever the shape, you
recover the content the same way: pass its `HASH` to `retrieve()` or the
`furl_retrieve` MCP tool.

Two hash widths:

- **12-hex** keys the SmartCrusher row offloads. The key is the first 12 hex
  characters of `sha256(payload)`.
- **24-hex** keys the diff, log, and search compressors, the cross-message
  deduper, the read-lifecycle cache, and the store default. The key is 24 hex
  characters of `md5(payload)` or `sha256(payload)`, depending on the producer.

Two families:

- **Double-angle `<<ccr:...>>`.** The bare `<<ccr:HASH>>`, plus annotated forms
  that carry a count or kind after the hash: `<<ccr:HASH N_rows_offloaded>>`,
  `<<ccr:HASH#rows N_chunks>>`, `<<ccr:HASH,KIND,SIZE>>`,
  `<<ccr:HASH N_bytes_duplicate>>`, and `<<ccr:HASH N_bytes_near_duplicate>>`.
- **Bracket forms.** Readable sentences that end in a hash:
  `[N items compressed to M. Retrieve more: hash=H]`,
  `[N lines compressed to M. Retrieve full diff: hash=H]`, and
  `[Read content stale: ... Retrieve original: hash=H]`. The read-stale form is
  recovered by a direct store lookup on its hash, not by the marker scanner.

The text after the hash is metadata for the reader. The hash alone is what you
retrieve.

## Redact & purge — security

Offloaded content is stored for later `retrieve()`, byte-exact for raw text and as a
semantically-complete re-serialization for structured JSON arrays, so by default a
secret inside a tool output is stored and stays recoverable — unencrypted, in a
local per-project SQLite file under `~/.furl` (`0600` perms). See
[SECURITY.md](SECURITY.md) → "Stored originals: at-rest posture" for the full
threat model. Redaction scrubs secrets **before** storage (two forms below); purge
deletes a stored original **after**.

**Env patterns (`FURL_REDACT_PATTERNS`) — reachable from the plugin.** Set this
environment variable to a list of regexes (one per line) — or `@/path/to/file` to
read them from a file — in the Claude Code plugin's `hooks/hooks.json` / `.mcp.json`
env or your shell. Every match is replaced with a `[REDACTED:<n>]` marker (`n` = the
pattern's 1-based line) **before** compression and storage, applied identically by
the PostToolUse hook, every MCP store path (`furl_compress` including its
pattern-filtered per-run stores, `furl_read`, and the pipeline's internal offload),
and `compress()`. This is the redaction channel the env-only plugin can reach — it
cannot pass a Python callable. A later `retrieve()` returns the already-redacted
original (the secret is gone by design). An invalid regex is warned about once
(stderr) and skipped; the remaining patterns still apply. Unset → a no-op,
byte-identical. Commas are **not** separators (regexes use them in `{n,m}`
quantifiers) — separate patterns with newlines. Patterns compile with
`re.MULTILINE`: `^`/`$` anchor at line boundaries, so `^password=\S+` matches a
`password=` line anywhere in the output. Patterns apply in list order, each over
the previous pattern's output — overlapping patterns can therefore nest markers
(e.g. `[REDACTED:[REDACTED:2]]`); no secret bytes survive either way, and
retrieval stays byte-exact against the stored (redacted) original.

```
FURL_REDACT_PATTERNS='(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*\S+
(?i)\bbearer\s+[A-Za-z0-9._-]{12,}
^password=\S+'
```

`FURL_REDACT_PATTERNS` and the callback below **compose**: the env patterns run
first, then the callback — both apply. Caveat (hook only): a catastrophically-
backtracking pattern that hangs past the hook's 30 s timeout gets the hook killed,
and fail-open then ships that call's output raw and unredacted — prefer bounded,
anchored patterns (see SECURITY.md for the full failure mode).

**Redactor (fail-closed).** Pass a pure `redactor: str -> str` on `CompressConfig`
to scrub content **before** it is compressed or stored. Redaction runs **outside**
`compress()`'s fail-open boundary: if the redactor **raises**, `compress()` raises
too — unredacted content is never compressed, stored, returned, or swallowed. On a
redactor error you get **no output rather than a leak**. When it succeeds, every
downstream step only sees redacted content, so a later `retrieve()` returns the
**redacted** original (the secret is gone by design). No redactor configured →
behavior is byte-identical to today. Non-string content passes through untouched;
your input is never mutated.

```python
import re
from furl_ctx import compress, CompressConfig

def scrub(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)

result = compress(messages, config=CompressConfig(redactor=scrub))
```

**Purge.** `purge(hash) -> bool` deletes a stored original from the active CCR
store so it can no longer be `retrieve()`d — the companion for content stored
before a redaction policy existed, or that must be erased on request. It acts on
the same namespace-scoped store as `retrieve` (honoring `FURL_CCR_NAMESPACE` /
`session_id` / `agent_id`), returning `True` if an entry was removed and `False`
if the hash was absent.

```python
from furl_ctx import purge

purge(hash)  # True if an entry was deleted, False if already gone
```

## `furl_read` — opt-in cached file reads (MCP)

The MCP server exposes a seventh tool, `furl_read`, **off by default**. It reads a
file with session caching: the first read returns full content and caches it;
later reads of the *same unchanged file* return a lightweight `<<ccr:HASH>>` marker
(~20 tokens instead of the whole file), and `furl_retrieve` on that hash returns
the full body. A `fresh: true` argument forces a cache-bypassing read (use after a
context compaction, in a sub-agent, or when you need guaranteed-current bytes).

It ships **off** because it is a filesystem-reading surface: reads are jailed to
`FURL_WORKSPACE_DIR` (or the server's working directory when that is unset), and a
cache over file reads can serve stale bytes when a file changes out of band. Rather
than silently shadow the host's built-in read tool, Furl makes enabling the extra
read surface a deliberate choice. Turn it on with `FURL_MCP_READ=1`
(`on`/`true`/`yes`/`enabled`):

```bash
FURL_MCP_READ=1 python3 -m furl_ctx.ccr.mcp_server
```

Once enabled, call `furl_read(file_path="/abs/path/to/file.py")` in place of the
host's built-in read for repeat-read token savings.

## How it works

```
  tool output · logs · diffs · JSON · RAG chunks
                      │
                      ▼
               ┌─────────────┐
               │    Furl     │
               └──────┬──────┘
                      │
            ┌─────────┴─────────┐
            ▼                   ▼
     compressed context    CCR store (retrievable originals)
            │                    ▲
            ▼                    │
          LLM  ──► needs detail? ┘
```

- **ContentRouter** — detects content type, selects the right compressor.
- **SmartCrusher** — universal JSON: arrays of dicts, nested objects, mixed types.
- **SearchCompressor / LogCompressor / DiffCompressor** — search results, build logs, diffs.
- **CrossMessageDeduper** — deduplicates repeated content across conversation turns.
- **CacheAligner**, opt-in and OFF by default: a detector-only prefix-stability check.
  Even when enabled it never rewrites or reorders messages; it only measures the frozen
  prefix and reports cache metrics, so it changes no bytes the model sees.
- **CodeAwareCompressor**, opt-in and OFF by default, needs the `[code]` extra: tree-sitter
  AST-verified source-code compression. With the default config, or without that extra
  installed, source code passes through untouched.
- **CCR** — reversible compression; the LLM retrieves originals on demand. Large
  distinct content no compressor can shrink (e.g. source files) takes the
  reversible CCR offload: an identity preview plus a retrieval marker.

### Pipeline internals

`compress()` emits three compression lifecycle stages:

`Input Received` → `Input Routed` → `Input Compressed`

- **Transforms** do the work. The default chain is CrossMessageDeduper then ContentRouter,
  which owns SmartCrusher and the per-content-type compressors. CacheAligner is opt-in and,
  even when enabled, only measures the prefix.
- **Pipeline extensions** observe or customize these stages via `on_pipeline_event(...)`; `compress()` passes your `hooks` object as the extension.
- **Compression hooks** sit alongside the lifecycle as an additional extension seam.

### Prompt caching (`cache_control`) — the frozen-prefix contract

Furl never modifies messages up to and including the highest Anthropic
`cache_control` marker (the **frozen prefix**), so provider prompt caches keep
hitting. Two rules keep caching and compression compatible:

- **Mark the breakpoint before the live zone.** `cache_control` on the *last*
  message freezes the whole conversation — every transform skips everything and
  0 tokens are saved (`error` stays `None`). `compress()` flags this in
  `result.warnings` and logs at WARNING. Either mark the breakpoint before the
  turns you want compressed, or compress before marking.
- **Pass back what Furl shipped.** The provider cached the bytes Furl
  *returned* last turn, not your originals. On multi-turn conversations, feed
  the previous `result.messages` back in — or don't move the marker forward
  past turns that already shipped compressed. Re-sending original history with
  a forward-moved marker guarantees a prefix-cache miss at the previously
  compressed message and pins it uncompressed forever (it is frozen). A
  best-effort detector (CCR registry hit inside the frozen prefix) surfaces
  this in `result.warnings`.

## Multiple sessions on one project

Two Claude Code windows (or any two MCP hosts) open on the same project run two
Furl MCP server processes that share one per-project SQLite store —
`~/.furl/ccr.sqlite3`, or a per-namespace `~/.furl/ccr-ns-<hash>.sqlite3`. This
is a normal, supported setup.

**What happens under concurrency.** The store runs in SQLite WAL mode: many
readers plus one writer at a time. When two servers write at once the loser waits
on the store's `busy_timeout` **and** a bounded retry/backoff before a durable
write is decided, so everyday cross-session contention lands durably instead of
failing. A durable write only *vetoes* when a sibling holds the write lock longer
than the whole retry budget (a few seconds — a hung or stale process), and the
veto is honest, never a false "lost":

- `furl_compress` still returns the retrieval **hash** and states plainly that
  the entry is *retrievable now from this server, but not after a restart and not
  from other processes.* It never claims the data is gone while it is retrievable
  this moment, and the uncompressed original is always in the caller's hands.
- The returned note and the server log name the likely cause: *another Furl MCP
  server process — possibly a second, live or stale, Claude Code session — holds
  the store.*

**Check for a stale server.**

```bash
pgrep -fl furl                       # Furl MCP processes (ps aux | grep furl works too)
lsof ~/.furl/ccr.sqlite3 2>/dev/null # who currently holds the store open
```

A server from a Claude Code session you already closed can linger and keep the
store open. If a *live* session is legitimately using it, leave it — contention
is handled.

**Killing a stale server is safe.** The store is on disk (WAL-journaled), not in
the server's memory, so stopping a stale server loses nothing that was durably
written; the next server reopens the same file and every retained entry
(`furl_list` / `furl_retrieve`) is still there.

```bash
kill <pid>   # graceful; the OS releases the SQLite lock on exit
```

The only entries that live solely in one process are those a veto already flagged
as non-durable (volatile fallback) — and the caller was told exactly that at veto
time and still holds those originals.

## Claude Code harness status, 2.1.163 and newer

This is the canonical harness-status statement; the README, the plugin README, the skill,
and the site all point here.

The Claude Code plugin's PostToolUse compression hook replaces a large tool output by
emitting `hookSpecificOutput.updatedToolOutput`, mirrored to the originating tool's output
shape. Claude Code 2.1.163 and newer validate that replacement against the tool's output
schema and keep the original on a shape mismatch. Because the hook mirrors the exact
incoming shape, the replacement passes that validation by construction and **is honored**,
so automatic hands-off compression reaches the model today. Both external auditors
confirmed this live on Claude Code 2.1.212. The shapes furl mirrors, and therefore
compresses automatically, are `Bash` stdout, `WebFetch` results, and `Task` sub-agent
output. `WebSearch` results still pass through uncompressed for now, because their
whole-object shape carries no single text field to mirror the compressed text onto.

Shape-mirroring exists because of upstream issue
[anthropics/claude-code#68951](https://github.com/anthropics/claude-code/issues/68951): an
earlier bare-string replacement did not match the tool's schema and was silently dropped,
so no compressed output reached the model. Mirroring the incoming shape removed that
failure by construction, which is why the automatic path works now rather than waiting on
an upstream change. Everything alongside the hook is independent of it and works on every
Claude Code version: the manual MCP tools `furl_compress`, `furl_retrieve`, and
`furl_search`, durable `<<ccr:HASH>>` storage and retrieval, and the SessionStart status
line. The opt-out PreToolUse pipe below adds automatic `Bash` savings whenever no Bash
permission rules are configured.

**Observability counters.** Every hook run tallies into the shared per-project CCR store,
cross-process and cumulative, surviving entry eviction, surfaced by `furl_stats` under
`store.hook_activity` as `hook_invocations_seen` and `hook_compressions_applied`, plus
bucketed `hook_noop_reasons`. A rising `hook_compressions_applied` confirms the hook is
both compressing and, on 2.1.163 and newer, delivering the shorter output the model reads.
The counters are stored via `CompressionStore.increment_counter` and `get_counters` and
read back cross-process through the durable SQLite backend.

**`FURL_PRETOOL_PIPE` — real savings on today's harness (on by default).** Unless
explicitly disabled, a **PreToolUse** hook rewrites a `Bash` command so its stdout is piped
through the Furl compressor **before** it becomes the tool result — so the model-visible
output *is* the compressed form, with the original stored under a `<<ccr:HASH>>` marker in
the same per-project store (same TTL as the PostToolUse path; `FURL_REDACT_PATTERNS`
redaction applies on the normal path but **not** to binary/undecodable output or when the
engine cannot load — see the plugin README's "Known limitations"). It does not use
`updatedToolOutput`, so it is unaffected by #68951. Disable it with `FURL_PRETOOL_PIPE=0`
(`false`/`off`/`no`/`disabled` also work, case-insensitively); unset, empty, or any other
value leaves it on, and disabling is a byte-identical no-op. Trade-offs: **Bash-only**; the
command mutation is **visible in the transcript** (a `# furl-pipe (FURL_PRETOOL_PIPE=0 to
disable)` comment, never a silent substitution); the original command's **exit code is
preserved exactly**; its **stderr is not captured and flows live** — but stdout is buffered
for compression, so stderr/stdout **interleaving is not preserved** (in a merged view all
stderr precedes the possibly-compressed stdout; `cmd 2>&1` merges both into the compressed
stream); small outputs pass through raw; it adds **~0.3–0.5 s per rewritten call** (two
`uv` resolves; the first call in a fresh environment pays a one-time resolve/build —
seconds to tens of seconds); and it is **fail-open** (a compressor that cannot start
falls back to the raw captured output, and a tempfile that cannot be created means the
original command runs unwrapped, uncompressed — never a broken command). **Permission
rules are respected (provably, total):** the pipe rewrites Bash **only when there are
zero readable Bash permission rules**. If any `Bash` rule of any kind —
`permissions.deny`, `permissions.ask`, or `permissions.allow` (an allow-list is itself a
restrictive posture) — exists in any scope Claude Code actually uses, including
relocations — enterprise managed settings (the per-OS `managed-settings.json` + its
`managed-settings.d` fragments, or the `CLAUDE_CODE_MANAGED_SETTINGS_PATH` override),
project settings (`.claude/settings{,.local}.json` under both `CLAUDE_PROJECT_DIR` and the
working dir), or user settings (under both `~/.claude` and `CLAUDE_CONFIG_DIR`) — **all**
Bash passes through untouched so your rules apply exactly as native. No command shape
(wrapper-hidden `env`/`sudo`/`flock`, compound, absolute path) can bypass it, because when
a rule exists nothing is rewritten. Unreadable/malformed settings, or a config-path
override env var set but unresolvable, also force passthrough. The genuine residual
blindness is CLI `--permission-mode`/`--disallowedTools` flags, SDK `managedSettings`
options, and API-fetched remote org policy (`CLAUDE_CODE_REMOTE_SETTINGS_PATH` /
`remoteSettings`); if you restrict Bash only through those, set `FURL_PRETOOL_PIPE=0`
(details in the plugin README's Known limitations).

## CLI

`pip install furl-ctx` also installs a `furl` command — shell-native access to the
same engine (pipelines, CI log reduction, offline eval, no LLM harness):

```bash
psql -c 'table events' | furl compress        # FILE, or stdin, -> compressed stdout
furl compress big.json --json                 # compressed text + token stats as JSON
furl retrieve <hash>                          # original content for a <<ccr:HASH>> marker
furl purge <hash>                             # delete a stored original by hash (0 if removed, 1 if absent)
furl eval <corpus> --recall                   # corpus compression ratio + needle-recall gate
furl doctor                                   # check the install: native core, tokenizer, store
furl mcp                                      # run the stdio MCP server for AI coding tools
```

Unlike the library default (in-memory), the `furl` CLI defaults its CCR store to the
durable **sqlite** backend (the global `~/.furl/ccr.sqlite3`) so a hash from `furl
compress` is retrievable by a later `furl retrieve` in a separate process; override
with `FURL_CCR_BACKEND` (e.g. `=memory` for an ephemeral store) and retention with
`FURL_CCR_TTL_SECONDS` (CLI default `86400` / 24 h, matching the Claude Code plugin's
window; the bare library default is 30 min). Hashes minted inside Claude Code live in
that project's PER-PROJECT store, not the global one — set
`FURL_CCR_PROJECT_DIR=<project root>` to point the CLI at it.

## Configuration (environment variables)

Every live `FURL_*` knob. All are optional — the defaults are the shipped behavior.

Store configuration is read once, when the CCR store is first built in a process.
`FURL_CCR_TTL_SECONDS`, `FURL_CCR_BACKEND`, `FURL_CCR_SPILL`, and the other store
knobs are snapshotted at that first use. Changing them later in the same process
has no effect. Set them before the first `compress()` or `retrieve()`.

| Variable | Default | What it does |
|----------|---------|--------------|
| `FURL_WORKSPACE_DIR` | `~/.furl` | Workspace root: home of the durable CCR SQLite store and the shared session-stats file. Also the **security boundary for `furl_read`** — file reads are jailed to it (the jail alone defaults to the server's working directory when unset). |
| `FURL_CCR_TTL_SECONDS` | `1800` (30 min) | CCR retention window in seconds — how long "reversible" lasts before an entry expires (an expired/evicted retrieval is a loud miss, never silent). Positive integer; invalid values warn and fall back. The Claude Code plugin overrides this to `86400` (24h) via its MCP env — honored by the MCP tools' own stores too — and the `furl` CLI defaults to `86400` as well; a bare MCP server without a valid value uses a 1 h session TTL for its own tool writes (unset AND invalid values fall back there), while dropped-row originals embedded in compressed output follow this library default instead — the 24 h lockstep needs the env set and valid. |
| `FURL_CCR_BACKEND` | unset → in-memory for the library; the **`furl` CLI** and the **MCP server** default to `sqlite` | CCR store backend: `memory`, `sqlite`, or the name of a third-party `furl_ctx.ccr_backend` entry point. Split by surface: a plain `from furl_ctx import compress` stays in-memory (a library must not write disk unbidden), while the `furl` CLI (`cli.py`) and the plugin's MCP server opt into durable `sqlite` so their separate-process `compress`→`retrieve` composes. Explicitly selecting a backend that cannot be loaded **raises at startup** — no silent downgrade to memory. |
| `FURL_CCR_BACKEND_OPTS` | unset (`{}`) | JSON object of keyword arguments passed to a third-party backend factory, e.g. `{"url": "..."}`. |
| `FURL_CCR_SQLITE_PATH` | `<workspace>/ccr.sqlite3` | File path of the durable SQLite CCR store. |
| `FURL_CCR_SQLITE_MAX_ROWS` | `10000` | Row cap for the SQLite store (oldest-created evicted first). |
| `FURL_CCR_PROJECT_DIR` | auto, per project (the plugin hook + MCP server set it from `CLAUDE_PROJECT_DIR`, else cwd) | Scopes the durable CCR store to a single project: an un-namespaced call resolves a per-project store (`ccr-ns-<hash>.sqlite3`) instead of the shared global `ccr.sqlite3`, so `furl_search` / `furl_list` / `furl_retrieve` and eviction never cross project boundaries. Set to `""` to disable scoping and use the global store — the way to share one store across projects, and to read a **pre-1.0 (global) store** after upgrade. An explicit `FURL_CCR_NAMESPACE` overrides this with a named shared store. Note: the per-namespace store follows the library backend default — with `FURL_CCR_BACKEND` unset it is **in-memory** (process-local, gone at exit); set `FURL_CCR_BACKEND=sqlite` for a durable per-namespace file (the Claude Code plugin already pins it). |
| `FURL_CCR_SPILL` | `off` | Q10 retention. When truthy (`1`/`true`/`yes`/`on`), an **in-memory** primary demotes evicted entries to a durable SQLite **spill** tier instead of deleting them, so a `retrieve()` past the in-memory cap still recovers (byte-identical, read-only — no promotion back). Ignored when the primary is already `sqlite` (`FURL_CCR_BACKEND=sqlite`, the MCP server's default): a durable primary has nothing to spill to. |
| `FURL_REDACT_PATTERNS` | unset (off) | Preventive secret redaction reachable from the env-only plugin. A newline-separated list of `re` regexes (or `@/path/to/file`); every match is replaced with `[REDACTED:<n>]` **before** compression and storage, from the hook, every MCP store path (`furl_compress` incl. filtered runs, `furl_read`, pipeline offload), and `compress()`. Compiled with `re.MULTILINE` (`^`/`$` anchor per line). Invalid regex → warned once + skipped (rest still apply). Composes with `CompressConfig.redactor` (env patterns first). Commas are not separators — use newlines. Unset → byte-identical no-op. See "Redact & purge — security". |
| `FURL_MCP_READ` | `off` | Enables the `furl_read` MCP tool (`on`/`true`/`1`/`yes`/`enabled`). Reads are jailed to `FURL_WORKSPACE_DIR`. |
| `FURL_COMPRESS_WORKERS` | `4` | Worker threads for the router's parallel per-message compression. |
| `FURL_PIPELINE_BREAKER_THRESHOLD` | `3` | Consecutive pipeline failures before the circuit breaker opens and messages pass through **uncompressed** for the cooldown window. `<= 0` disables the breaker. |
| `FURL_PIPELINE_BREAKER_COOLDOWN_S` | `60` | Seconds an open circuit breaker keeps passing messages through untouched before retrying. |
| `FURL_COMPACTION_FORMAT` | `csv-schema` | Lossless render format for SmartCrusher compaction: `csv-schema`, `json`, or `markdown-kv`. Unknown values raise. |
| `FURL_COST_RATE_USD_PER_MTOK` | `3.0` | Blended $/1M-token rate for the MCP `furl_stats` cost-saved estimate. Invalid/negative values fall back to the default. |
| `FURL_CCR_NAMESPACE` | unset | Names a shared CCR store, overriding per-project scoping. When set, the isolation key becomes this namespace plus any `session_id` and `agent_id`, so several projects or agents can deliberately share one store. Takes precedence over `FURL_CCR_PROJECT_DIR`. |
| `FURL_REDACT_BUILTINS` | `on` | Built-in credential redaction, ON by default. High-confidence secret shapes, among them private keys, AWS `AKIA`/`ASIA` keys, GCP, OpenAI, GitHub, and Slack tokens, and JWTs, are scrubbed from the stored original before compression, on every store path. Set `0`/`false`/`no`/`off` to opt out. Security-relevant: opting out stores those secrets verbatim in the CCR store. Composes with `FURL_REDACT_PATTERNS`, built-ins first. See SECURITY.md. |
| `FURL_MCP_LEGEND` | `on` | Prepends the columnar-table legend that decodes the `[N]{col:type,...}` compaction grammar to MCP tool output. ON by default; `off`/`false`/`0`/`no`/`disabled` hides it for callers that already know the grammar. |
| `FURL_MAX_COMPRESS_BYTES` | `8388608` | Byte ceiling, 8 MB by default. A content block larger than this is offloaded straight to the reversible CCR store instead of the super-linear mixed compression path. Positive integer; invalid values fall back to the default. |
| `FURL_PROFILE_BANNER` | `1` | `furl` CLI only. Prints a one-line profile banner to stderr on `furl` commands, keeping stdout pure compressed output. Set `0`/`false`/`no`/`off`/`disabled` to silence it. |

The Claude Code plugin's own hook/MCP knobs (`FURL_HOOK_*`) are documented in
[`plugins/furl/README.md`](plugins/furl/README.md).

A direct `furl_compress` call (the MCP tool or the `compress()` API) never runs
through that hook, so `FURL_HOOK_MIN_CHARS` does not gate it; a small or
already-dense input is instead governed by the router's own
`min_tokens_to_compress` floor (250 tokens by default) and by the router's
refusal to ship a compression that would not actually shrink the input — either
gate reports back a `router:noop:below_min_tokens` or `router:noop:no_savings`
reason rather than a silent pass-through.

## Compared to

furl-ctx runs **locally** as a native Claude Code plugin, compresses **every content type** in the tool outputs it sees, and is **reversible**. It is its own upstream Headroom with the ML compressor, the proxy transport, and the telemetry plane removed.

| | Scope | Deploy | Local | Reversible |
|---|---|---|:-:|:-:|
| **furl-ctx** | Tool outputs, logs, RAG chunks; not file reads or history | native plugin · library · MCP | Yes | Yes |
| [Headroom](https://github.com/headroomlabs-ai/headroom) | All context, through a request-compression proxy | proxy | Yes | Yes |
| [RTK](https://github.com/rtk-ai/rtk) | CLI command outputs | CLI wrapper | Yes | No |
| [lean-ctx](https://github.com/yvgude/lean-ctx) | File reads, shell output, plus history and tool results through a local proxy | CLI wrapper · MCP · proxy | Yes | Yes |
| [LLMLingua](https://github.com/microsoft/LLMLingua) | Prompt text | Python library | Yes | No |
| [Compresr](https://compresr.ai), [Token Co.](https://thetokencompany.ai) | Text sent to their API | Hosted API call | No | No |
| OpenAI Compaction | Conversation history | Provider-native | No | No |
| Claude Code compaction | Conversation history | Provider-native | No | No |

Local and reversible no longer set furl-ctx apart on their own: lean-ctx and Headroom share both. furl-ctx's real difference is delivery. It is a native Claude Code plugin with no ML model, no proxy process, and no telemetry, scoped to tool outputs rather than reaching into your file reads or conversation history. LLMLingua drops tokens with a small language model, so its output is lossy and not recoverable; furl-ctx keeps every original byte retrievable instead.

> **RTK** ([rtk-ai/rtk](https://github.com/rtk-ai/rtk)) is a complementary CLI-output rewriter — a peer in the table above, **not** bundled with or a dependency of Furl. If you already use it for shell-output rewriting, Furl compresses everything downstream; the two compose cleanly. Credit to the RTK team for a great tool.

## Corporate / SSL-inspection environments

The prebuilt-wheel install needs no Rust and avoids this entirely. It only
applies if you force a **source build** (`--no-binary`, `git+…`, or an unsupported
platform) and `pip` fails with `CERTIFICATE_VERIFY_FAILED`
(`unable to get local issuer certificate`): your network uses **SSL inspection** — a MITM
proxy presenting a company-issued CA. The build backend (`maturin`) downloads `rustup` over a
connection your TLS stack doesn't trust. **Install Rust first** so the build doesn't fetch it:

```bash
# macOS / Linux
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh && rustup default stable
# Windows
winget install Rustlang.Rustup && rustup default stable
```

Restart your shell, then re-run the install. Simplest of all: install the prebuilt
wheel from PyPI (`pip install "furl-ctx[all]"`), which skips the Rust build — and this
whole issue — entirely.

One runtime asset is fetched over TLS; if it is blocked, trust your corporate CA via
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`:

- **`openaipublic.blob.core.windows.net`** — tiktoken's BPE encoding files, downloaded once on
  first use and cached locally. Pre-populate the cache and point `TIKTOKEN_CACHE_DIR` at it to
  run fully offline.

## Contributing

```bash
git clone <your-fork-url> && cd <repo-dir>
pip install -e ".[dev]" && pytest
```

A devcontainer ships in `.devcontainer/`. See [CONTRIBUTING.md](CONTRIBUTING.md).
