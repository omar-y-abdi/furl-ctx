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


<p align="center"><strong>Typically 0–54% fewer tokens on real high-entropy content · up to 95% on repetitive logs/fixtures (<a href="#proof">honest read</a>) · a Claude Code plugin · local-first · reversible</strong></p>

<p align="center">
  <a href="https://github.com/omar-y-abdi/furl/releases/latest"><img src="https://img.shields.io/github/v/release/omar-y-abdi/furl?sort=semver&color=blue" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#proof">Proof</a> ·
  <a href="LIBRARY.md">Library</a> ·
  <a href="#community">Community</a>
</p>

---

Furl compresses everything your Claude Code agent reads — tool outputs, logs, web fetches, RAG chunks — before it reaches the model. Same answers, a fraction of the tokens. Every dropped byte stays retrievable on demand.

The name is nautical: to *furl* is to roll up a sail — Furl rolls long context up out of the model's way and keeps it on a line, ready to *unfurl* (retrieve) the instant you need it.

<p align="center">
  <img src="FurlDemo-Fast.gif" alt="Furl in action" width="820">
</p>

## Install

Two commands inside Claude Code:

```
/plugin marketplace add omar-y-abdi/furl
/plugin install furl@furl
```

That's it — this installs the compression hook, the MCP tools, and the skill. No `pip install`, no setup: Furl fetches itself on first use. Requires [`uv`](https://docs.astral.sh/uv/) on your PATH (same as the official [serena](https://github.com/oraios/serena) plugin).

**What you get**

- **Auto-compression hook** — shrinks large `Bash` / `WebFetch` / `WebSearch` / `Task` (sub-agent) outputs before they enter context. Fail-open: never breaks a tool call. **It does *not* touch your `Read` / `Grep` / `Glob` file reads — by design**, so a later `Edit` still sees exact file bytes; those reads (often a coding agent's largest context cost) pass through uncompressed ([why](#proof)).
- **Signal-aware offload + sliceable retrieval** — a payload too big to compress inline (e.g. a 33 MB trace) comes back as a structured summary (schema, per-field value histograms, example rows) instead of a truncated head/tail, and the agent pulls a narrow slice on demand — `retrieve(hash, select_field="name", select_equals="DroppedFrame")` or a numeric range — without materializing the whole thing.
- **MCP tools** — `furl_compress`, `furl_retrieve`, `furl_stats`, `furl_purge` (erase stored originals), `furl_search` (find by content substring), `furl_list` (list stored entries). A seventh tool, `furl_read`, exists but is off by default — enable with `FURL_MCP_READ=1` (see [LIBRARY.md](LIBRARY.md)).
- **Skill** — explains the `<<ccr:HASH>>` retrieval flow and how to tune or disable it.

Tuning, disabling (`FURL_HOOK_ENABLED=0`), and the full reference: [`plugins/furl/README.md`](plugins/furl/README.md). Retrieval TTL differs by surface: the library defaults to 30 minutes, while this Claude Code plugin ships a 24 h window (`FURL_CCR_TTL_SECONDS=86400`) via its MCP env.

## Proof

Token reduction on real captured data — reproducible, inputs committed under `benchmarks/data/`. Every number uses the engine's own tokenizer; needle recall is 100% (a known unique row is always recoverable, in the output or via CCR). Read every figure below as a **best-case ceiling**, not a typical — the honest read follows.

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

**Honest read:** the numbers above are best-case, low-entropy *ceilings* measured on the dev fixtures — two independent, out-of-sample audits show they degrade by 6–43pp on fresh high-entropy / near-unique / realistic data (exactly where real logs and listings live). On genuinely high-entropy content, honest lossless savings sit in the **0–54% band**, not 60–95% (code 0%, search 40%, repeated_logs 54%); read every figure here as a ceiling, not a typical, and see the tier-aware breakdown in [BENCHMARKS.md](BENCHMARKS.md).

The `code` row's 99% is CCR-offload of a large non-file-read tool output (e.g. `Bash` dumping source text); an agent's own `Read`/`Grep`/`Glob` file access bypasses the compression hook by design and passes through unchanged, at 0%.

## Also a Python library

The same engine drops into any Python app or MCP host: `from furl_ctx import compress`. Install, usage, pipeline internals, prompt-caching contract, and the full `FURL_*` config reference live in [LIBRARY.md](LIBRARY.md).

## Community

Questions or bug reports → [open a GitHub issue](https://github.com/omar-y-abdi/furl/issues) (the surest way to reach us). For chat, there's a [Discord](https://discord.gg/yRmaUNpsPJ).

## License

Apache 2.0 — see [LICENSE](LICENSE).
