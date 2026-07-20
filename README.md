<div align="center"><pre>
███████╗██╗   ██╗██████╗ ██╗
██╔════╝██║   ██║██╔══██╗██║
█████╗  ██║   ██║██████╔╝██║
██╔══╝  ██║   ██║██╔══██╗██║
██║     ╚██████╔╝██║  ██║███████╗
╚═╝      ╚═════╝ ╚═╝  ╚═╝╚══════╝
 The context compression layer for AI agents
  <img src="typing.svg" width="42" />
</pre></div>

# furl-ctx

**Reversible context compression for AI agents.** furl-ctx shrinks large tool outputs, logs, web fetches, and RAG chunks before they fill your agent's context window, and keeps every original byte retrievable on demand. Think prompt compression and context pruning for token optimization, without losing data. CCR, short for Compress-Cache-Retrieve, is the core: compression where every dropped byte stays retrievable.


<p align="center"><strong>0–54% token savings on real high-entropy content · reaching 95% on repetitive logs/fixtures (<a href="#proof">honest read</a>)</strong></p>
<p align="center"><strong>Claude Code plugin · MCP server usable by any MCP host · Reversible compression</strong></p>

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

> **What works today:** furl-ctx is context compression for AI agents that reduces Claude Code token usage while every original byte stays retrievable, and install is two commands. Automatic hands-off compression works on Claude Code 2.1.163 and newer: the PostToolUse hook mirrors each replacement to the tool's output shape, so the harness honors it, verified live by both external audits on 2.1.212. This shape-mirroring was built in response to upstream issue [#68951](https://github.com/anthropics/claude-code/issues/68951), where an unmirrored replacement was dropped. The manual MCP tools furl_compress, furl_retrieve, and furl_search work on every version, and the opt-out PreToolUse pipe adds automatic Bash savings when no Bash permission rules are configured. furl-ctx never touches your Read, Grep, or Glob file reads by design. See [LIBRARY.md](LIBRARY.md) for the canonical harness status.

**Keep finding yourself waiting on the next usage limit reset?** 

**Answer:** Stop making your AI agent read everything.

furl-ctx works as a context compression and retrieval layer for AI agents. It shrinks large tool outputs, logs, web fetches, and RAG chunks before they consume your agent's context window, and keeps the original data available for exact retrieval when needed.

# Quick install

**Prerequisite:** [`uv`](https://docs.astral.sh/uv/) on your PATH (same as the official [serena](https://github.com/oraios/serena) plugin).

Then two commands inside Claude Code:

```
/plugin marketplace add omar-y-abdi/furl-ctx
/plugin install furl@furl
```

That's it — this installs the compression hook, the MCP tools, and the skill. No `pip install`, no setup: Furl fetches itself on first use.

## Furl also works as a Python library

The PyPI package is `furl-ctx`. Do not run `pip install furl`, which installs an unrelated URL-manipulation library.

The same engine drops into any Python app or MCP host:

```python
from furl_ctx import compress

messages = [{"role": "tool", "content": "..."}]
result = compress(messages, model="claude-sonnet-4")
# result.messages → compressed when content is large enough; CCR keeps originals retrievable
```

Install, usage, pipeline internals, prompt-caching contract, and the full `FURL_*` config reference live in [LIBRARY.md](LIBRARY.md).

# How it works
**furl-ctx filters out unwanted noise** while the agent searches for the sections it needs, so input token usage drops while the answer stays the same. 

**What works today** is an on-demand toolkit for Furl:
Your agent calls the MCP tools directly
- `furl_compress` — compress large payloads into an agent-readable summary
- `furl_retrieve` — recover exact original content by pattern, field, or line range
- `furl_search` — locate relevant sections inside compressed data
- `furl_list` — inspect stored payloads
- `furl_stats` — view compression results
- `furl_purge` — remove stored payloads

Instead of pushing thousands of irrelevant lines into the model, Furl gives the agent a compressed view of the data. If it later needs something that was omitted, it explicitly retrieves just that portion—by pattern, field, or line range—without materializing the entire payload again.

Unlike token compressors or summarizers, Furl never throws data away. Compression is **reversible**: every original text payload remains byte-exact and retrievable.

**Compression savings vary by data type:**

- 0–54% on high-entropy content
- up to 95% on repetitive logs and fixtures

**Where furl-ctx saves little or nothing.** Repetitive text with no newlines compresses at roughly 0 percent, because the engine is line and structure oriented. Single-line high-entropy content is near 0 percent. Code and file reads are 0 percent by design, because Read, Grep, and Glob are never touched. So a coding session's expected savings come only from large structured tool outputs, for example JSON, logs, and search results from Bash, WebFetch, and sub-agent tasks.

**Retrieval model:** Furl is pull-based, not push-based.

Dropped content does not automatically reappear. The compressed representation intentionally removes those sections from the model-visible context. If the agent needs a specific omitted item by pattern, field, or line range, it retrieves it explicitly. The data is never lost, every retrieval is byte-exact and done by the agent. 

**Tradeoff is visibility:**

A unique anomaly hidden inside repetitive data will not appear in the compressed summary unless the agent already knows to search for it. Furl preserves data availability, not automatic anomaly discovery.

**Furl compresses what is already in context, not files on disk.** It shrinks a payload your agent has already read into its context window. It cannot reach into a large file on disk to pull out the part that matters, and it cannot take a file path and return compressed output. For a genuinely large file, the first and biggest reduction comes from pre-filtering with tools like grep, awk, sed, or jq to extract the relevant slice; Furl then compresses that slice further and keeps every dropped byte retrievable. Treat the two as layers: pre-filter megabytes down to a focused excerpt, then let Furl compress the excerpt. Furl is a strong second layer on top of pre-filtering, not a replacement for it.

**Why "Furl"?**

To furl a sail is to roll it up and keep it out of the way until needed.
Furl does the same for context: it rolls large amounts of information out of the active window while keeping it ready to unfurl when retrieval is required.

Furl is a hard fork of [Headroom](https://github.com/headroomlabs-ai/headroom)'s compression engine, stripped and rebuilt around the reversible-compression core. About a third of the engine still has traces of Headroom (see [NOTICE](NOTICE)).

## **What you get**

- **Auto-compression hook** — shrinks large `Bash` / `WebFetch` / `WebSearch` / `Task` (sub-agent) outputs before they enter context. Fail-open: never breaks a tool call. **It does *not* touch your `Read` / `Grep` / `Glob` file reads — by design**, so a later `Edit` still sees exact file bytes; those reads (often a coding agent's largest context cost) pass through uncompressed ([why](#proof)). One honest limit: when an output is so large that Claude Code itself persists it to a file and hands the model only a file reference, there is no inline output for the hook to compress.
- **Harness status:** On Claude Code 2.1.163 and newer, the PostToolUse hook mirrors its replacement to the tool's output shape, so the harness honors it and automatic compression reaches the model. This shape-mirroring answers upstream issue [anthropics/claude-code#68951](https://github.com/anthropics/claude-code/issues/68951), where an unmirrored replacement was dropped. The manual tools `furl_compress`, `furl_retrieve`, and `furl_search` work on every version, and the on-by-default PreToolUse pipe adds Bash savings when no Bash permission rules exist. Disable that pipe with `FURL_PRETOOL_PIPE=0`. See [LIBRARY.md](LIBRARY.md) for the canonical harness status.
- **Signal-aware offload + sliceable retrieval** — a payload too big to compress inline (e.g. a 33 MB trace) comes back as a structured summary (schema, per-field value histograms, example rows) instead of a truncated head/tail, and the agent pulls a narrow slice on demand — `retrieve(hash, select_field="name", select_equals="DroppedFrame")` or a numeric range — without materializing the whole thing.
- **MCP tools** — `furl_compress`, `furl_retrieve`, `furl_stats`, `furl_purge` (erase stored originals), `furl_search` (find by content substring), `furl_list` (list stored entries). A seventh tool, `furl_read`, exists but is off by default — enable with `FURL_MCP_READ=1` (see [LIBRARY.md](LIBRARY.md)).
- **Skill** — explains the `<<ccr:HASH>>` retrieval flow and how to tune or disable it.

Tuning, disabling with `FURL_HOOK_ENABLED=0`, and the full reference live in [`plugins/furl/README.md`](plugins/furl/README.md). Retrieval TTL differs by surface:

| Surface | Retrieval TTL |
|---|---|
| Library | 30 minutes |
| `furl` CLI | 24 hours |
| Claude Code plugin | 24 hours |
| Bare MCP server | 1 hour session, plus 30 minutes for dropped-row originals |

The plugin sets `FURL_CCR_TTL_SECONDS=86400`, which governs both the hook's offloads and the MCP tools' stores; the full 24 hour window needs that env set, as the plugin ships it.

**A note on version numbers:** the Claude Code plugin versions independently from the `furl-ctx` engine it pins — a plugin release doesn't always mean an engine release, and vice versa. `/plugin` shows the plugin version; GitHub Releases and `CHANGELOG.md` track the engine version; the SessionStart banner shows both together (`furl <plugin> · engine furl-ctx <engine>`), which is the quickest way to see both numbers at once.

# Proof

Token reduction on real captured data — a dated snapshot (inputs committed under `benchmarks/data/` for auditability; a re-run measures the current engine, so absolute counts can drift from this table — the honest-read band below is the authoritative check). 
Every number uses the engine's own tokenizer and measures `compress()` directly — independent of the PostToolUse hook-delivery issue noted above; needle recall is 100% (a known unique row is always recoverable, in the output or via CCR). 
This table is measured with the `gpt-4o` model string (real tiktoken BPE, see [BENCHMARKS.md](BENCHMARKS.md)). `compress()`'s own default model is `claude-sonnet-4-5-20250929` — the shape Claude Code and the plugin actually call with — and claude-* routes through the exact same o200k_base encoding as gpt-4o, since Anthropic's own tokenizer is not publicly available. That makes this table's shape representative of what a real Claude Code run sees internally, but the counts themselves are a documented PROXY for Anthropic's tokenizer, not real Anthropic billing tokens: per Anthropic's own developer guidance, tiktoken undercounts Claude tokens by roughly 15-20% on typical text and by more on code or non-English text. Read every "token savings" percentage you see from a claude-* call, here or in your own agent, as an approximation on that basis — not an exact Anthropic token count.

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

**Automatic, hands-off compression works on Claude Code 2.1.163 and newer**, because the PostToolUse hook mirrors its replacement to the tool's output shape and the harness honors it. Upstream issue [#68951](https://github.com/anthropics/claude-code/issues/68951) is the reason the mirror was built, not an open blocker. 
The opt-out PreToolUse pipe gives automatic Bash savings today only if you have no Bash permission rules configured. With any Bash allow, deny, or ask rule it stays out of the way, so your rules apply exactly as native.

## Community

Questions or bug reports → [open a GitHub issue](https://github.com/omar-y-abdi/furl-ctx/issues) (the surest way to reach the maintainer).

**Maintainer note:** Furl is solo-maintained today — one person handles issues, PRs, and security reports, so response times vary with availability. [CONTRIBUTING.md](CONTRIBUTING.md) covers how PRs get reviewed and [SECURITY.md](SECURITY.md) covers the vulnerability-disclosure process; both hold regardless of team size.

## License

Apache 2.0 — see [LICENSE](LICENSE).
