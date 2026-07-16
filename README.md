<div align="center"><pre>
  ███████╗ ██╗   ██╗ ██████╗  ██╗
  ██╔════╝ ██║   ██║ ██╔══██╗ ██║
  █████╗   ██║   ██║ ██████╔╝ ██║
  ██╔══╝   ██║   ██║ ██╔══██╗ ██║
       ██║      ╚██████╔╝ ██║  ██║ ███████╗
       ╚═╝       ╚═════╝  ╚═╝  ╚═╝ ╚══════╝
 The context compression layer for AI agents
  <img src="typing.svg" width="42" />
</pre></div>


<p align="center"><strong>0–54% token savings on real high-entropy content · reaching 95% on repetitive logs/fixtures (<a href="#proof">honest read</a>)</strong></p>
<p align="center"><strong>Claude Code & Codex plugin · MCP server · Reversible Compression</strong></p>

<p align="center">
  <a href="https://github.com/omar-y-abdi/furl-ctx/releases/latest"><img src="https://img.shields.io/github/v/release/omar-y-abdi/furl-ctx?sort=semver&color=blue" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#proof">Proof</a> ·
  <a href="LIBRARY.md">Library</a> ·
  <a href="#community">Community</a>
</p>

---

**Keep finding yourself waiting on the next usage limit reset?** 

**Answer:** Stop making your AI agent read everything.

By using Furl you'll never need to touch grass again, Furl works as a context compression and retrieval layer for AI agents. It shrinks large tool outputs, logs, web fetches, and RAG chunks before they consume your agent's context window, while keeping the original data available for exact retrieval when needed.

# Quick install

**Prerequisite:** [`uv`](https://docs.astral.sh/uv/) on your PATH (same as the official [serena](https://github.com/oraios/serena) plugin).

Then two commands inside Claude Code:

```
/plugin marketplace add omar-y-abdi/furl-ctx
/plugin install furl@furl
```

That's it — this installs the compression hook, the MCP tools, and the skill. No `pip install`, no setup: Furl fetches itself on first use.

## Furl also works as a Python library

The same engine drops into any Python app or MCP host:

```python
from furl_ctx import compress

messages = [{"role": "tool", "content": "..."}]
result = compress(messages, model="claude-sonnet-4")
# result.messages → compressed when content is large enough; CCR keeps originals retrievable
```

Install, usage, pipeline internals, prompt-caching contract, and the full `FURL_*` config reference live in [LIBRARY.md](LIBRARY.md).

# How it works
**Furl filters out all unwanted noise** while agent is searching for the desired sections. Resulting in decreased input token usage while the answer always staying the same. 

**What works today** is a on-demand toolkit for Furl:
Your agent calls the MCP tools directly
- `furl_compress` — compress large payloads into an agent-readable summary
- `furl_retrieve` — recover exact original content by pattern, field, or line range
- `furl_search` — locate relevant sections inside compressed data
- `furl_list` — inspect stored payloads
- `furl_stats` — view compression results
- `furl_purge` — remove stored payloads

Instead of pushing thousands of irrelevant lines into the model, Furl gives the agent a compressed view of the data. If it later needs something that was omitted, it explicitly retrieves just that portion—by pattern, field, or line range—without materializing the entire payload again.

Unlike token compressors or summarizers, Furl never throws data away. Compression is **reversible**: every original payload remains byte-exact and retrievable.

**Compression savings vary by data type:**

- 0–54% on high-entropy content
- up to 95% on repetitive logs and fixtures

**Retrieval model:** Furl is pull-based, not push-based.

Dropped content does not automatically reappear. The compressed representation intentionally removes those sections from the model-visible context. If the agent needs a specific omitted item by pattern, field, or line range, it retrieves it explicitly. The data is never lost, every retrieval is byte-exact and done by the agent. 

**Tradeoff is visibility:**

A unique anomaly hidden inside repetitive data will not appear in the compressed summary unless the agent already knows to search for it. Furl preserves data availability, not automatic anomaly discovery.

**Why "Furl"?**

To furl a sail is to roll it up and keep it out of the way until needed.
Furl does the same for context: it rolls large amounts of information out of the active window while keeping it ready to unfurl when retrieval is required.

Furl originated from the author's *Headroom* context-engineering experimentation project, the early commit history carries that lineage.

## **What you get**

- **Auto-compression hook** — shrinks large `Bash` / `WebFetch` / `WebSearch` / `Task` (sub-agent) outputs before they enter context. Fail-open: never breaks a tool call. **It does *not* touch your `Read` / `Grep` / `Glob` file reads — by design**, so a later `Edit` still sees exact file bytes; those reads (often a coding agent's largest context cost) pass through uncompressed ([why](#proof)). One honest limit: when an output is so large that Claude Code itself persists it to a file and hands the model only a file reference, there is no inline output for the hook to compress.
- **Known issue:** Claude Code ≥2.1.163 currently ignores hooks' replacement output ([anthropics/claude-code#68951](https://github.com/anthropics/claude-code/issues/68951)), so the automatic PostToolUse compression above stores and accounts savings, but the model may still receive the original text until that bug is fixed. Manual tools (`furl_compress` / `furl_retrieve` / `furl_search`) are unaffected, and real savings still land today via the **on-by-default PreToolUse pipe** (Bash-only; disable with `FURL_PRETOOL_PIPE=0`). See [LIBRARY.md](LIBRARY.md) for current harness status and pipe details.
- **Signal-aware offload + sliceable retrieval** — a payload too big to compress inline (e.g. a 33 MB trace) comes back as a structured summary (schema, per-field value histograms, example rows) instead of a truncated head/tail, and the agent pulls a narrow slice on demand — `retrieve(hash, select_field="name", select_equals="DroppedFrame")` or a numeric range — without materializing the whole thing.
- **MCP tools** — `furl_compress`, `furl_retrieve`, `furl_stats`, `furl_purge` (erase stored originals), `furl_search` (find by content substring), `furl_list` (list stored entries). A seventh tool, `furl_read`, exists but is off by default — enable with `FURL_MCP_READ=1` (see [LIBRARY.md](LIBRARY.md)).
- **Skill** — explains the `<<ccr:HASH>>` retrieval flow and how to tune or disable it.

Tuning, disabling (`FURL_HOOK_ENABLED=0`), and the full reference: [`plugins/furl/README.md`](plugins/furl/README.md). Retrieval TTL differs by surface: the library defaults to 30 minutes; this plugin ships a 24 h window (`FURL_CCR_TTL_SECONDS=86400`) governing both the hook's offloads and the MCP tools' stores; the `furl` CLI (no bare binary on PATH by default — run it via `uv run --no-project --with 'furl-ctx[mcp]' furl ...`, or `pip install furl-ctx` for a persistent one) defaults to the same 24 h. 

A bare MCP server without a valid `FURL_CCR_TTL_SECONDS` keeps a 1 h session TTL for its tool-stored entries, while dropped-row originals embedded in compressed output follow the library's 30-minute default — the full 24 h window needs the env set, as the plugin ships it.

**A note on version numbers:** the Claude Code plugin versions independently from the `furl-ctx` engine it pins — a plugin release doesn't always mean an engine release, and vice versa. `/plugin` shows the plugin version; GitHub Releases and `CHANGELOG.md` track the engine version; the SessionStart banner shows both together (`furl <plugin> · engine furl-ctx <engine>`), which is the quickest way to see both numbers at once.

# Proof

Token reduction on real captured data — a dated snapshot (inputs committed under `benchmarks/data/` for auditability; a re-run measures the current engine, so absolute counts can drift from this table — the honest-read band below is the authoritative check). 
Every number uses the engine's own tokenizer and measures `compress()` directly — independent of the PostToolUse hook-delivery issue noted above; needle recall is 100% (a known unique row is always recoverable, in the output or via CCR). 

Read every figure below as a **best-case ceiling**, not a typical — the honest read follows.

*Best-case ceilings — low-entropy dev fixtures (the compressor's happy path):*

| Dataset       | Items | Before | After  | Reduction | Info retention |
|---------------|------:|-------:|-------:|----------:|---------------:|
| code          |     7 | 41,025 |    471 |       99% |           100% |
| multiturn     |   135 | 14,866 |  2,073 |       86% |           100% |
| logs          |    90 |  8,595 |    619 |       93% |           100% |
| search        |    90 |  4,102 |    318 |       92% |           100% |
| repeated logs |    90 |  3,621 |    120 |       97% |           100% |
| disk          |     9 |    694 |    279 |       60% |           100% |

Across the corpus: **95% fewer tokens** (72,903 → 3,880) at 100% information retention. Full methodology and the 6-seed adversarial sweep: [BENCHMARKS.md](BENCHMARKS.md).

Information retention here means every byte is recoverable byte-exact through `furl_retrieve`. It does not mean the compressed view shows every row. Retrieval is pull-based, so an agent has to query for a specific dropped item to see it, and a lone anomaly will not surface in the compressed summary on its own.

**Honest read:** the numbers above are best-case, low-entropy *ceilings* measured on the dev fixtures — two independent, out-of-sample audits show they degrade by 6–43pp on fresh high-entropy / near-unique / realistic data (exactly where real logs and listings live). 
On genuinely high-entropy content, honest lossless savings sit in the **0–54% band**, not 60–95% (code 0%, search 40%, repeated_logs 54%); read every figure here as a ceiling, not a typical, and see the tier-aware breakdown in [BENCHMARKS.md](BENCHMARKS.md).

The `code` row's 99% is CCR-offload of a large non-file-read tool output (e.g. `Bash` dumping source text); an agent's own `Read`/`Grep`/`Glob` file access bypasses the compression hook by design and passes through unchanged, at 0%.

**Stability:** The public API is what `furl_ctx` exports at the top level, including `compress()`, `retrieve()`, `purge()`, and `resolve_markers()`. Those signatures are the surface to build against. Submodule internals under `furl_ctx.*` may change between releases, so import from the top-level package rather than reaching into submodules. Releases have been frequent during early development, so pin a minor version if you need a fixed surface to depend on.

**Automatic, hands-off compression is pending an upstream Claude Code fix, issue [#68951](https://github.com/anthropics/claude-code/issues/68951).** 
The opt-out PreToolUse pipe gives automatic Bash savings today only if you have no Bash permission rules configured. With any Bash allow, deny, or ask rule it stays out of the way, so your rules apply exactly as native.

## Community

Questions or bug reports → [open a GitHub issue](https://github.com/omar-y-abdi/furl-ctx/issues) (the surest way to reach the maintainer).

**Maintainer note:** Furl is solo-maintained today — one person handles issues, PRs, and security reports, so response times vary with availability. [CONTRIBUTING.md](CONTRIBUTING.md) covers how PRs get reviewed and [SECURITY.md](SECURITY.md) covers the vulnerability-disclosure process; both hold regardless of team size.

## License

Apache 2.0 — see [LICENSE](LICENSE).
