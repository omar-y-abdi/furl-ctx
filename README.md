<div align="center"><pre>
  ██╗  ██╗███████╗ █████╗ ██████╗ ██████╗  ██████╗  ██████╗ ███╗   ███╗
  ██║  ██║██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗██╔═══██╗████╗ ████║
  ███████║█████╗  ███████║██║  ██║██████╔╝██║   ██║██║   ██║██╔████╔██║
  ██╔══██║██╔══╝  ██╔══██║██║  ██║██╔══██╗██║   ██║██║   ██║██║╚██╔╝██║
  ██║  ██║███████╗██║  ██║██████╔╝██║  ██║╚██████╔╝╚██████╔╝██║ ╚═╝ ██║
  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝
                  The context compression layer for AI agents
</pre></div>

<p align="center"><strong>60–95% fewer tokens · library · MCP · 6 algorithms · local-first · reversible</strong></p>

<p align="center">
  <a href="https://pypi.org/project/headroom-ai/"><img src="https://img.shields.io/pypi/v/headroom-ai.svg" alt="PyPI"></a>
  <a href="https://huggingface.co/chopratejas/kompress-v2-base"><img src="https://img.shields.io/badge/model-Kompress--v2--base-yellow.svg" alt="Model: Kompress-v2-base"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
</p>

<p align="center">
  <a href="#get-started-60-seconds">Install</a> ·
  <a href="#proof">Proof</a> ·
  <a href="https://discord.gg/yRmaUNpsPJ">Discord</a> ·
  <a href="llms.txt">llms.txt</a> ·
  <a href="ENTERPRISE.md">Enterprise</a>
</p>

<p align="center"><sub>
  <b>AI agents / LLMs:</b> read <a href="llms.txt"><code>/llms.txt</code></a> here for the doc index.
</sub></p>

---

Headroom compresses everything your AI agent reads — tool outputs, logs, RAG chunks, files, and conversation history — before it reaches the LLM. Same answers, fraction of the tokens.

<p align="center">
  <img src="HeadroomDemo-Fast.gif" alt="Headroom in action" width="820">
  <br/><sub>Live: 10,144 → 1,260 tokens — same FATAL found.</sub>
</p>

## What it does

- **Library** — `compress(messages)` in Python, inline in any app
- **MCP server** — `headroom_compress`, `headroom_retrieve`, `headroom_stats` for any MCP client
- **Reversible (CCR)** — originals are cached for retrieval on demand

## How it works (30 seconds)

```
 Your agent / app
   (Claude Code, Cursor, Codex, LangChain, Agno, Strands, your own code…)
        │   prompts · tool outputs · logs · RAG results · files
        ▼
    ┌────────────────────────────────────────────────────┐
    │  Headroom   (runs locally — your data stays here)  │
    │  ────────────────────────────────────────────────  │
    │  CacheAligner  →  ContentRouter  →  CCR            │
    │                    ├─ SmartCrusher   (JSON)        │
    │                    ├─ CodeCompressor (AST)         │
    │                    └─ Kompress-base  (text, HF)    │
    │                                                    │
    │  Reversible CCR store  ·  MCP server               │
    └────────────────────────────────────────────────────┘
        │   compressed prompt  +  retrieval tool
        ▼
 LLM provider  (Anthropic · OpenAI · Bedrock · …)
```

- **ContentRouter** — detects content type, selects the right compressor
- **SmartCrusher / CodeCompressor / Kompress-base** — compress JSON, AST, or prose
- **CacheAligner** — stabilizes prefixes so provider KV caches actually hit
- **CCR** — stores originals locally; LLM calls `headroom_retrieve` if it needs them

→ [Kompress-v2-base model card](https://huggingface.co/chopratejas/kompress-v2-base)

## Get started (60 seconds)

```bash
# 1 — Install
pip install "headroom-ai[all]"          # everything
# or: pip install "headroom-ai[mcp]"     # just the MCP server
```

```python
# 2 — Compress inline in any Python app
from headroom import compress

result = compress(messages, model="claude-sonnet-4")
# result.messages  → compressed; CCR keeps originals retrievable
```

```bash
# 3 — Or run the MCP server for Claude Code / Cursor / any MCP host
python -m headroom.ccr.mcp_server       # exposes headroom_compress / _retrieve / _stats
```

Granular extras: `[mcp]`, `[ml]` (Kompress-base), `[html]`, `[progress]`, `[dev]`. Requires **Python 3.10+**.

## Proof

**Savings on real agent workloads:**

| Workload                      | Before | After  | Savings |
|-------------------------------|-------:|-------:|--------:|
| Code search (100 results)     | 17,765 |  1,408 | **92%** |
| SRE incident debugging        | 65,694 |  5,118 | **92%** |
| GitHub issue triage           | 54,174 | 14,761 | **73%** |
| Codebase exploration          | 78,502 | 41,254 | **47%** |

**Accuracy preserved on standard benchmarks:**

| Benchmark  | Category | N   | Baseline | Headroom | Delta      |
|------------|----------|----:|---------:|---------:|------------|
| GSM8K      | Math     | 100 |    0.870 |    0.870 | **±0.000** |
| TruthfulQA | Factual  | 100 |    0.530 |    0.560 | **+0.030** |
| SQuAD v2   | QA       | 100 |        — |  **97%** | 19% compression |
| BFCL       | Tools    | 100 |        — |  **97%** | 32% compression |

For the full, adversarially-verified methodology and re-runnable token-reduction sweeps, see [BENCHMARKS.md](BENCHMARKS.md).

## When to use · When to skip

**Great fit if you…**
- feed large tool outputs, logs, RAG chunks, or files into an LLM and want fewer tokens
- want compression you can drop into any Python app, or expose to an MCP host
- need reversible compression — originals are retrievable via CCR within the configured TTL

**Skip it if you…**
- only use a single provider's native compaction and don't process large external context
- can't run the compression locally in your own process

<details>
<summary><b>Integrations — drop Headroom into any stack</b></summary>

| Your setup     | Hook in with                                  |
|----------------|-----------------------------------------------|
| Any Python app | `compress(messages, model=…)`                 |
| MCP clients    | `python -m headroom.ccr.mcp_server`           |

</details>

<details>
<summary><b>What's inside</b></summary>

- **SmartCrusher** — universal JSON: arrays of dicts, nested objects, mixed types.
- **CodeCompressor** — AST-aware for Python, JS, Go, Rust, Java, C++.
- **Kompress-base** — our HuggingFace model, trained on agentic traces.
- **CacheAligner** — stabilizes prefixes so Anthropic/OpenAI KV caches actually hit.
- **IntelligentContext** — score-based context fitting with learned importance.
- **CCR** — reversible compression; LLM retrieves originals on demand.

</details>

<details>
<summary><b>Pipeline internals</b></summary>

`compress()` emits three compression lifecycle stages:

`Input Received` → `Input Routed` → `Input Compressed`

- **Transforms** do the work: CacheAligner, ContentRouter, SmartCrusher, CodeCompressor, Kompress-base, IntelligentContext / RollingWindow.
- **Pipeline extensions** observe or customize these stages via `on_pipeline_event(...)`; `compress()` passes your `hooks` object as the extension.
- **Compression hooks** sit alongside the lifecycle as an additional extension seam.

</details>

## Install

```bash
pip install "headroom-ai[all]"          # everything
```

Granular extras: `[mcp]` (MCP server), `[ml]` (Kompress-base), `[html]` (HTML extraction), `[progress]` (download progress bars), `[dev]`. Requires **Python 3.10+**.

Using `pipx`? Choose a supported interpreter explicitly:

```bash
pipx install --python python3.13 "headroom-ai[all]"
```

### Corporate / SSL-inspection environments

If `pip install "headroom-ai[all]"` fails with `CERTIFICATE_VERIFY_FAILED`
(`unable to get local issuer certificate`), your network uses **SSL inspection** — a MITM
proxy presenting a company-issued CA. The build backend (`maturin`) downloads `rustup` over a
connection your TLS stack doesn't trust. **Install Rust first** so the build doesn't fetch it:

```bash
# macOS / Linux
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh && rustup default stable
# Windows
winget install Rustlang.Rustup && rustup default stable
```

Restart your shell, then `pip install "headroom-ai[all]"`. A prebuilt wheel avoids the Rust
build entirely where available: `pip install --only-binary headroom-ai headroom-ai`.

Two runtime assets are fetched over TLS; if they are blocked, trust your corporate CA via
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE`:

- **`cdn.pyke.io`** — the ONNX Runtime for the Rust core. Alternatively pre-provide it with
  `ORT_STRATEGY=system` and `ORT_LIB_LOCATION=/path/to/onnxruntime`.
- **`huggingface.co`** — the `kompress-base` compression model. Pre-download it and run with
  `HF_HUB_OFFLINE=1`, or set `HF_ENDPOINT` to a trusted mirror.

Running with compression disabled requires neither asset.

## Compared to

Headroom runs **locally**, covers **every** content type, and is **reversible**.

|                                                                              | Scope                                          | Deploy                             | Local | Reversible |
|------------------------------------------------------------------------------|------------------------------------------------|------------------------------------|:-----:|:----------:|
| **Headroom**                                                                 | All context — tools, RAG, logs, files, history | library · MCP                      | Yes   | Yes        |
| [RTK](https://github.com/rtk-ai/rtk)                                        | CLI command outputs                            | CLI wrapper                        | Yes   | No         |
| [lean-ctx](https://github.com/yvgude/lean-ctx)                               | CLI commands, MCP tools, editor rules          | CLI wrapper · MCP                  | Yes   | No         |
| [Compresr](https://compresr.ai), [Token Co.](https://thetokencompany.ai)    | Text sent to their API                         | Hosted API call                    | No    | No         |
| OpenAI Compaction                                                            | Conversation history                           | Provider-native                    | No    | No         |

> **Attribution.** Headroom ships with the excellent [RTK](https://github.com/rtk-ai/rtk) binary for shell-output rewriting — `git show --short`, scoped `ls`, summarized installers. Huge thanks to the RTK team; their tool is a first-class part of our stack, and Headroom compresses everything downstream of it.

## Contributing

```bash
git clone <your-fork-url> && cd headroom
pip install -e ".[dev]" && pytest
```

Devcontainers in `.devcontainer/` (default + `memory-stack` with Qdrant & Neo4j). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Community

- **[Discord](https://discord.gg/yRmaUNpsPJ)** — questions, feedback, war stories.
- **[Kompress-v2-base on HuggingFace](https://huggingface.co/chopratejas/kompress-v2-base)** — the model behind our text compression.

## License

Apache 2.0 — see [LICENSE](LICENSE).
