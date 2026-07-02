<div align="center"><pre>
  ███████╗ ██╗   ██╗ ██████╗  ██╗
  ██╔════╝ ██║   ██║ ██╔══██╗ ██║
  █████╗   ██║   ██║ ██████╔╝ ██║
  ██╔══╝   ██║   ██║ ██╔══██╗ ██║
  ██║      ╚██████╔╝ ██║  ██║ ███████╗
  ╚═╝       ╚═════╝  ╚═╝  ╚═╝ ╚══════╝
              The context compression layer for AI agents
</pre></div>


<p align="center"><strong>60–95% fewer tokens on redundant workloads · library · MCP · local-first · reversible</strong></p>

<p align="center">
  <a href="https://pypi.org/project/furl-ctx/"><img src="https://img.shields.io/pypi/v/furl-ctx.svg" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
</p>

<p align="center">
  <a href="#get-started-60-seconds">Install</a> ·
  <a href="#proof">Proof</a> ·
  <a href="https://discord.gg/yRmaUNpsPJ">Discord</a> ·
  <a href="llms.txt">llms.txt</a>
</p>

<p align="center"><sub>
  <b>AI agents / LLMs:</b> read <a href="llms.txt"><code>/llms.txt</code></a> here for the doc index.
</sub></p>

---

Furl compresses everything your AI agent reads — tool outputs, logs, RAG chunks, files, and conversation history — before it reaches the LLM. Same answers, fraction of the tokens.

<p align="center">
  <img src="FurlDemo-Fast.gif" alt="Furl in action" width="820">
  <br/><sub>Live: 10,144 → 1,260 tokens — same FATAL found.</sub>
</p>

## What it does

- **Library** — `compress(messages)` in Python, inline in any app
- **MCP server** — `furl_compress`, `furl_retrieve`, `furl_stats` for any MCP client
- **Reversible (CCR)** — originals are cached for retrieval on demand

## How it works (30 seconds)

```
 Your agent / app
   (Claude Code, Cursor, Codex, LangChain, Agno, Strands, your own code…)
        │   prompts · tool outputs · logs · RAG results · files
        ▼
    ┌────────────────────────────────────────────────────┐
    │  Furl   (runs locally — your data stays here)  │
    │  ────────────────────────────────────────────────  │
    │  CacheAligner  →  ContentRouter  →  CCR            │
    │                    ├─ SmartCrusher   (JSON)        │
    │                    └─ Search / Log / Diff          │
    │                                                    │
    │  Reversible CCR store  ·  MCP server               │
    └────────────────────────────────────────────────────┘
        │   compressed prompt  +  retrieval tool
        ▼
 LLM provider  (Anthropic · OpenAI · Bedrock · …)
```

- **ContentRouter** — detects content type, selects the right compressor
- **SmartCrusher** — statistical JSON / array compression
- **Search / Log / Diff compressors** — search results, build logs, diffs
- **CacheAligner** — stabilizes prefixes so provider KV caches actually hit
- **CCR** — stores originals locally; LLM calls `furl_retrieve` if it needs them

## Get started (60 seconds)

```bash
# 1 — Install
pip install "furl-ctx[all]"          # everything
# or: pip install "furl-ctx[mcp]"     # just the MCP server
```

```python
# 2 — Compress inline in any Python app
from furl_ctx import compress

result = compress(messages, model="claude-sonnet-4")
# result.messages  → compressed; CCR keeps originals retrievable
```

```bash
# 3 — Or run the MCP server for Claude Code / Cursor / any MCP host
python -m furl_ctx.ccr.mcp_server       # exposes furl_compress / _retrieve / _stats
```

Granular extras: `[mcp]` (MCP server), `[dev]`. Requires **Python 3.10+**.

## Proof

**Token reduction on real captured data** — reproducible; inputs committed under `benchmarks/data/`, captured by `benchmarks/run_bench.py` into [BASELINE.md](benchmarks/BASELINE.md). Every number uses the engine's own tokenizer; needle recall is 100% (a known unique row is always recoverable — visible in the output or via CCR):

| Dataset       | Items | Before | After  | Reduction | Regime      | Info retention |
|---------------|------:|-------:|-------:|----------:|-------------|---------------:|
| code          |     7 | 41,025 |    471 |       99% | lossy (CCR) |           100% |
| disk          |     9 |    694 |    347 |       50% | lossless    |           100% |
| multiturn     |   135 | 14,866 |  2,211 |       85% | lossy (CCR) |           100% |
| logs          |    90 |  8,595 |    619 |       93% | lossy (CCR) |           100% |
| search        |    90 |  4,102 |    318 |       92% | lossy (CCR) |           100% |
| repeated logs |    90 |  3,621 |    120 |       97% | lossy (CCR) |           100% |

<sub>**Regime** — *lossless*: the compressed output is self-contained (zero rows dropped). *lossy (CCR)*: rows are offloaded to the local CCR store and replaced with `<<ccr:HASH>>` markers — smaller output, and **every dropped row is byte-exactly recoverable on demand** (100% info retention, within the configured TTL). `code` (large distinct source files that no compressor can shrink) takes the reversible CCR-offload fallback: an identity preview (paths + first lines) plus a retrieval marker ships in place of the full files.</sub>

These are a single deterministic capture at HEAD (`benchmarks/BASELINE.md`). Across the whole corpus the table sums to **94% fewer tokens** (72,903 → 4,086) at 100% information retention; the **60–95%** headline maps to this table's lossy-CCR range. Full methodology and the 6-seed adversarial sweep live in [BENCHMARKS.md](BENCHMARKS.md).

## When to use · When to skip

**Great fit if you…**
- feed large tool outputs, logs, RAG chunks, or files into an LLM and want fewer tokens
- want compression you can drop into any Python app, or expose to an MCP host
- need reversible compression — originals are retrievable via CCR within the configured TTL

**Skip it if you…**
- only use a single provider's native compaction and don't process large external context
- can't run the compression locally in your own process

<details>
<summary><b>Integrations — drop Furl into any stack</b></summary>

| Your setup     | Hook in with                                  |
|----------------|-----------------------------------------------|
| Any Python app | `compress(messages, model=…)`                 |
| MCP clients    | `python -m furl_ctx.ccr.mcp_server`           |

</details>

<details>
<summary><b>What's inside</b></summary>

- **SmartCrusher** — universal JSON: arrays of dicts, nested objects, mixed types.
- **SearchCompressor / LogCompressor / DiffCompressor** — search results, build logs, and diffs.
- **CrossMessageDeduper** — deduplicates repeated content across conversation turns.
- **CacheAligner** — stabilizes prefixes so Anthropic/OpenAI KV caches actually hit.
- **CCR** — reversible compression; LLM retrieves originals on demand. Large distinct content no compressor can shrink (e.g. source files) takes the reversible CCR offload: an identity preview plus a retrieval marker.

</details>

<details>
<summary><b>Pipeline internals</b></summary>

`compress()` emits three compression lifecycle stages:

`Input Received` → `Input Routed` → `Input Compressed`

- **Transforms** do the work: CacheAligner, CrossMessageDeduper, ContentRouter, SmartCrusher.
- **Pipeline extensions** observe or customize these stages via `on_pipeline_event(...)`; `compress()` passes your `hooks` object as the extension.
- **Compression hooks** sit alongside the lifecycle as an additional extension seam.

</details>

<details>
<summary><b>Prompt caching (<code>cache_control</code>) — the frozen-prefix contract</b></summary>

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

</details>

## Install

```bash
pip install "furl-ctx[all]"          # everything
```

Granular extras: `[mcp]` (MCP server), `[dev]`. Requires **Python 3.10+**.

Using `pipx`? Choose a supported interpreter explicitly:

```bash
pipx install --python python3.13 "furl-ctx[all]"
```

### Corporate / SSL-inspection environments

If `pip install "furl-ctx[all]"` fails with `CERTIFICATE_VERIFY_FAILED`
(`unable to get local issuer certificate`), your network uses **SSL inspection** — a MITM
proxy presenting a company-issued CA. The build backend (`maturin`) downloads `rustup` over a
connection your TLS stack doesn't trust. **Install Rust first** so the build doesn't fetch it:

```bash
# macOS / Linux
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh && rustup default stable
# Windows
winget install Rustlang.Rustup && rustup default stable
```

Restart your shell, then `pip install "furl-ctx[all]"`. A prebuilt wheel avoids the Rust
build entirely where available: `pip install --only-binary furl-ctx furl-ctx`.

One runtime asset is fetched over TLS; if it is blocked, trust your corporate CA via
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`:

- **`openaipublic.blob.core.windows.net`** — tiktoken's BPE encoding files, downloaded once on
  first use and cached locally. Pre-populate the cache and point `TIKTOKEN_CACHE_DIR` at it to
  run fully offline.

## Compared to

Furl runs **locally**, covers **every** content type, and is **reversible**.

|                                                                              | Scope                                          | Deploy                             | Local | Reversible |
|------------------------------------------------------------------------------|------------------------------------------------|------------------------------------|:-----:|:----------:|
| **Furl**                                                                 | All context — tools, RAG, logs, files, history | library · MCP                      | Yes   | Yes        |
| [RTK](https://github.com/rtk-ai/rtk)                                        | CLI command outputs                            | CLI wrapper                        | Yes   | No         |
| [lean-ctx](https://github.com/yvgude/lean-ctx)                               | CLI commands, MCP tools, editor rules          | CLI wrapper · MCP                  | Yes   | No         |
| [Compresr](https://compresr.ai), [Token Co.](https://thetokencompany.ai)    | Text sent to their API                         | Hosted API call                    | No    | No         |
| OpenAI Compaction                                                            | Conversation history                           | Provider-native                    | No    | No         |

> **RTK** ([rtk-ai/rtk](https://github.com/rtk-ai/rtk)) is a complementary CLI-output rewriter — a peer in the table above, **not** bundled with or a dependency of Furl. If you already use it for shell-output rewriting, Furl compresses everything downstream; the two compose cleanly. Credit to the RTK team for a great tool.

## Contributing

```bash
git clone <your-fork-url> && cd furl
pip install -e ".[dev]" && pytest
```

A devcontainer ships in `.devcontainer/`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Community

- **[Discord](https://discord.gg/yRmaUNpsPJ)** — questions, feedback, war stories.

## License

Apache 2.0 — see [LICENSE](LICENSE).
