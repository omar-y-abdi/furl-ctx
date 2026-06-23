# Headroom Compression — Next-Steps Action Doc

**Synthesis of 48 measured experiments across 8 rounds. Methodology: every claim below is MEASURED (cited token deltas from the fleet ledger), but ALL measurements are against `benchmarks/run_bench` — the same committed snapshots the 48 agents optimized against. Contract #4 (no overfitting / out-of-sample) is therefore UNVERIFIED for every item. The implementer MUST re-confirm top picks on `verify/run.py` (adversarial 6-seed, cold CCR) before trusting any gain. Baseline verified at HEAD: NO integer-ratio encoding exists in ir.rs/compactor.rs, and NO JSON-array error-gate exemption exists in content_router.py.**

## Summary

The fleet found **one large, generalizing win** and **a handful of small, real column encodings**. Be honest: outside the error-gate fix, the gains are single-digit-token to low-single-digit-pp, and the single highest raw delta is probably a fixture coincidence.

- **The big one:** the error-protection gate misclassifies homogeneous JSON arrays (git logs, search results) as error output and blocks ALL compression. Fixing it: multiturn **4369 → 2009 tokens, +15.9pp**. Three agents (#17, #28, #41) reached the identical result. Python-only, generalizes, low risk.
- **Real but small column encodings (generalize):** MajorityAffix (+2.3pp, #42), percent/unit-suffix strip (+1pp, #8/#11/#39/#40), hex-hash transcoding (−36 tok, #30). Each is a few tokens to ~2pp and touches Rust+Python (parity risk).
- **The trap:** cross-column integer-ratio (`ifree = avail*10`, **+3.2pp**, #13/#14/#15/#20/#23/#27/#32) has the HIGHEST raw delta and converged 7 times — but `ifree=avail*10` is almost certainly a fixture coincidence, not a filesystem invariant. Seven agents hit the **same coincidence**; that is not corroboration of generalization. It is gated and safe (won't regress when absent) but its gain likely vanishes out-of-sample. Ranked **below** smaller generalizing gains.

## Top Recommendation

**Land the error-gate JSON-array bypass FIRST** (#28 strict homogeneous-key-set discriminant, preserving `is_error:true` as an unconditional block, at both gate sites ~2243 and ~2626). It is the only large win, is Python-only (no Rust rebuild, no parity risk), and generalizes. **Load-bearing assumption to state in the PR:** genuine error output (tracebacks/stderr) is never a homogeneous JSON array of dicts.

**Then re-baseline before touching column encodings.** Multiturn gains are NOT additive — error-gate, field-delta (#5), near-csv (#29), and ratio all target the same df/gitlog messages and were each measured against the same 4369 baseline. After the error-gate fix, multiturn drops to 2009 and its composition changes (gitlog now compressed), so every column-encoding candidate's multiturn target shrinks. Re-measure, don't sum.

## Ranked Actions

Ranked by (measured gain × confidence) ÷ effort, with **generalization** as the tiebreaker (per Contract #4).

| Rank | Action | Approach IDs | Measured Evidence | Gain Tier | Risk | Effort |
|------|--------|--------------|-------------------|-----------|------|--------|
| 1 | Error-gate bypass for homogeneous JSON arrays | #28 (#17, #41 corroborate; #33 lossless variant) | multiturn 4369→2009, +15.9pp; needle-recall 100%; 21/21 CCR; 424 tests | **Typical, generalizes** | Low-Med (lossy-to-CCR on protected content) | **Low** (Python-only) |
| 2 | MajorityAffix column encoding | #42 (#12 corroborates) | disk@9 694→331, +2.3pp; 100% recall; 21/21 CCR | **Typical, generalizes** (path prefixes + outliers) | Med (parity, escape collisions) | Med |
| 3 | Percent/unit-suffix strip (inline marker, no preamble) | #40 (#8, #11, #39) | disk@9 694→340, +1pp; 100% recall | **Typical, generalizes** | Low-Med (parity) | Low-Med |
| 4 | Hex-hash transcode (base64url/base32, inline) | #30 (hexhash-b32) | logs@90 619→583, −36 tok; no regressions | **Typical, generalizes** (every git log) | Low | Low-Med |
| 5 | Cross-column integer-ratio (ifree=avail*10) | #15/#32 inline (#13/#14/#20/#23/#27) | disk@9 694→325, +3.2pp (HIGHEST raw) | **CEILING / near-unique data — validate out-of-sample** | Low regression / HIGH gain-illusion | Med |
| 6 | SoA columnar contest (per-table TOKEN gate) | #2 (#38 cautionary) | disk@9 347→334, +1.9pp w/ contest | Marginal, shape-dependent | Med-High (calibrated threshold = overfit risk) | High |

**Note on rank 5:** it sits below smaller gains *despite the highest raw delta* — the honest call under Contract #4. Before writing any code, capture a fresh `df -k` on a different machine and check whether `ifree == avail*K` actually holds. If not, deprioritize. The encoding is safe to ship (gated, round-trip-proven, won't regress when the ratio is absent), but do NOT advertise +3.2pp as a typical gain.

## Dead Ends — Do Not Re-Try

**Unifying principle: every byte-level entropy transform REGRESSED under BPE.** Byte savings ≠ token savings. Non-natural markers (`^N`, `+`, control bytes like `\x1F`) fragment BPE merges and cost more tokens than they save. Winners either use BPE-friendly markers (`$N`, `~/`, inline declaration suffixes) or eliminate content entirely (ratio/constant-fold). Specifically:

- **BWT + MTF + RLE pre-stage** (#10, #34, #46): regresses at the token level on every dataset. The near-unique string columns it would target are already handled by CCR lossy-drop (search@90: 77/90 offloaded) or by DictString/Affix on low-cardinality path columns.
- **`^N` prefix-delta on strings** (#10 prefix-delta-fold-bwt-mtf-rle): byte savings 299 (8.3%) but +20 tokens — the `^N` marker splits `def ` token boundaries.
- **PrefixDict / template mining on whitespace/keywords** (#9, #20 template-mining, #21): gpt-4o already tokenizes Python indentation and `def`/`feat`/`fix` as 1-2 tokens; numeric fill indices are also 1 token → preamble overhead with no per-cell saving. Measured −22 (lines), −15 (CC subjects), −6 (search@90).
- **`+`-sign positive deltas** (numeric column deltas): the `+` is a separate BPE token in gpt-4o → measured −39 (absolute_offset), −84 (line_number).
- **FragDict / cross-row substring dict via preamble** (#7 fired marginally +0.2pp; #43 fragdict-unicode-sigil and CrossColumnSharedDict rejected): the `__frag:COLNAME=` preamble has a poor byte/token ratio; needs ~100-byte net-savings floor to avoid regressions, which no benchmark column clears meaningfully.
- **DictNumeric / ModalBase / head-dict-floor on benchmark data** (#1, #4, #6): byte gate correctly rejects — preamble cost (22-30 tokens) exceeds the 1-2 token/row saving; benchmark columns are either too high-cardinality or already collapsed by ditto.
- **Always-on SoA transpose** (#38): a wash — disk saves ~7 tok but search@90 REGRESSES +8 tok (long heterogeneous `lines` column tokenizes worse as one row). Only viable WITH a per-table token contest (rank 6).
- **Always-on min-base / GCD / hex/octal integer encoding** (#44, #25): small integers already tokenize as 1 token in o200k/cl100k; offsets produce the same digit count; large integers tokenize identically in any base. Zero or negative savings.
- **Token-aware affix length selection** (token_aware_affix_length): genuinely novel but the payoff ceiling is structural (~2-4 tokens total) — ditto + CCR collapse the visible rows before affix fires (search@90: only 3 non-ditto cells survive). Below metric precision, plus parity risk.
- **HeadDict↔Affix re-evaluation** (#24, head-dict-yield-to-affix): #36 squeezed +0.1pp/multiturn (−13 tok) by checking Affix cost inside stamp_head_dict; #24's same idea found DictString stamps benchmark path columns first (≤8 unique values) so the path never competes. Marginal, fragile — not worth it standalone.

## Methodology Note

All deltas cited are **measured** (from the 48-experiment ledger, gpt-4o tiktoken via the engine tokenizer), not estimated. However, the measurement harness (`benchmarks/run_bench`) uses the **committed snapshots the agents optimized against** — so these numbers establish a *ceiling*, not generalization. Contract #4 (no overfitting; gains must hold on fresh out-of-sample data) is **unverified** for every ranked item. Before shipping ANY item as a headline gain, run `verify/run.py` (adversarial 6-seed sweep, cold CCR per subprocess) and `verify/measure.py` (strict byte-exact). Tiers used above: **typical** = recurring structure that generalizes (error-gate, majority-affix, percent-strip, hash-transcode); **ceiling / near-unique-data** = gain depends on a specific fixture coincidence (integer-ratio). Retrieval cost is accounted for: the error-gate full-bypass and any lossy column drops route to CCR per-row chunks (recoverable, needle-recall 100% measured); the lossless-only error-gate variant (#33) keeps all rows visible at a smaller +9.6pp gain — a deliberate visibility-vs-token tradeoff the implementer should choose explicitly.