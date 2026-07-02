# Independent Verification Report — Furl Compression Claims

**Role:** Phase-2 independent verifier (adversarial). Assume reported numbers are
inflated until proven on freshly-generated, out-of-sample, real-data inputs.
**Date:** 2026-06-13 (UTC). **Engine commit under test:** `3a507010` (run), audited
at HEAD `d760d4a4` (the commit that added this harness).
**Engine source touched:** NONE. `git diff crates/ furl_ctx/` is empty; the
harness commit `d760d4a4` modified zero engine files. All work is confined to
`verify/`.

---

## 1. Verdict (bottom line)

**The harness is legitimate and its numbers replicate.** The Phase-1 verifier did
NOT cheat: it really reconstructs from the compressed output + CCR and sha256-
compares to the original, uses committed DEFAULT params (`RoutingPolicy::MinTokens`),
runs cold CCR per case in fresh subprocesses, sweeps 6 fixed seeds with mean±range
(not best-of-N), and uses genuinely real external data. I re-ran a spot subset and
got **byte-for-byte identical** numbers.

**The dev claims replicate ONLY at low/medium entropy. They DEGRADE — sometimes
catastrophically — on high-entropy, near-unique rows:**

- `search@90` high-entropy: **36.1%** vs claimed **92.7%** — a **-56.6pp** collapse
  (and unstable: one seed compressed only 24.5%).
- `logs@90` high-entropy: **80.2%** vs claimed **93.0%** — **-12.8pp**.
- `disk@9` high-entropy: **43.3%** vs claimed **50%** — **-6.7pp** (already only 59.7%
  at medium).
- `multiturn` (medium): **55.0%** vs claimed **70.8%** — **-15.8pp** (NOT auto-flagged;
  see harness gap below).
- `repeated_logs@90`: **95.9%** vs claimed **97.1%** — **-1.2pp** (minor).

**Lossless claim ("100% recoverable") HOLDS in every single case I tested**, including
my own stricter reconstruction that refuses the harness's substring fallback. Every
case is byte-exact (sha256 of reconstruction == sha256 of original), 0 hash failures,
0 silent needle loss.

One **real engine bug** is correctly surfaced by the harness: a result-cache vs
CCR-store TTL divergence that can leave a signalled `<<ccr:HASH>>` pointer unbacked
(silent data loss in a long-running server). Reproduced, 3/3 trials.

---

## 2. Results table (fresh, out-of-sample, 6 seeds each, mean ± range)

Token reduction is from the engine's own `compress()` with default params and the
real gpt-4o tiktoken tokenizer. "Lossless?" = sha256(reconstruction)==sha256(original)
across all 6 seeds. Effective savings include round-trip retrieval overhead (retrieve
call tokens + re-injected content tokens).

| type / case | dev claim | fresh mean ± range (by tier) | lossless? | eff@25% | eff@50% | needle survival | roundtrip hash OK |
|---|---|---|---|---|---|---|---|
| **logs@90** | 93.0% | low 95.9% / med **94.6%** / high **80.2%** (76.4-83.7) | YES (6/6) | 70.8% | 47.1% | 18 vis / 18 sig / **0 silent** / 18 recov | YES |
| **repeated_logs@90** | 97.1% | **95.9%** all tiers (95.8-96.1) | YES (6/6) | 72.2% | 48.7% | 18 / 18 / 0 / 18 | YES |
| **search@90** | 92.7% | low 95.6% / med **94.0%** / high **36.1%** (24.5-93.7) | YES (6/6) | med 65.9% | med 38.2% | med 4 vis/18 sig/**0 silent**/18 recov; high 16 vis/3 sig/0 silent | YES |
| **disk@9** | 50% lossless | low 70.2% / med **59.7%** / high **43.3%** (42.6-44.7) | YES (6/6) | = raw (no drop) | = raw | 18 vis / 0 sig / **0 silent** | YES |
| **code@7** | 0% | low **66.0%** (dedup!) / med **0.0%** / high **0.0%** | YES (6/6) | n/a | n/a | n/a | YES |
| **multiturn@90** | 70.8% (@135) | low 77.5% / med **55.0%** / high **33.8%** | YES (6/6) | = raw | = raw | n/a (cache-prefix safe) | YES |

Sizes 90 and 900 were both swept; the 900 sizes generally compress BETTER (more rows
to fold), e.g. multiturn@900 medium = 87.5%, so the degradations above are the
small-payload / high-entropy worst cases, which is the right thing to stress.

---

## 3. Per-claim verdict

| Dev claim | Replicates? | Detail |
|---|---|---|
| logs@90 = 93.0% | **PARTIAL** | med 94.6% (beats claim); high **80.2%** (degrades -12.8pp) |
| search@90 = 92.7% | **PARTIAL / FAILS at high** | med 94.0% (beats claim); high **36.1%** (-56.6pp, unstable) |
| repeated_logs@90 = 97.1% | **NEAR** | 95.9% (-1.2pp), tier-invariant (forced-low shape) |
| multiturn = 70.8% | **DEGRADES** | med 55.0% (-15.8pp); only low(77.5%)/900(87.5%) beat it |
| disk@9 = 50% lossless | **DEGRADES on reduction, HOLDS on lossless** | med 59.7%/high 43.3%; lossless YES |
| code@7 = 0% | **HOLDS at med/high**; low dedups to 66% | 0% passthrough confirmed for unique blobs |
| "100% recoverable" (all) | **HOLDS** | byte-exact in 6/6 seeds for every case; 0 hash failures |

---

## 4. Harness audit — what I checked and found

### 4.1 Reconstruction path — REAL, not short-circuited
The harness reconstructs the original item multiset from the compressed output
ALONE: visible JSON rows + `decode_csv_schema_rows` (the documented decoder) +
CCR-retrieved blobs (parsing the `<<ccr:HASH>>` sentinel and calling
`compression_store.retrieve`). It then sha256s the reconstructed multiset against
the original and sets `byte_exact` only on identity (`measure.py:269`).

**Leniency risk I investigated:** `hash_compare_structured` has a last-resort
`_present_in_text` scalar-substring fallback (`measure.py:263`) that could mark an
item "reconstructed" without true CCR retrieval. **I wrote an independent stricter
recheck** (`verify/independent_recheck.py`) that DROPS that fallback — reconstructing
only from visible + decoded + CCR, then sha256-comparing. Result: **strict_byte_exact
== harness byte_exact in 12/12 cross-checked cases (4 families x 3 tiers), zero
mismatches.** Where rows are dropped (e.g. search low/med, logs med: 1 CCR sentinel,
86-90 rows offloaded), the strict reconstruction reproduces ALL rows via genuine CCR
retrieval — the round-trip is real, the sha256 matches. The substring fallback is
NOT load-bearing.

### 4.2 Cold CCR / fixed seeds / no best-of-N — CONFIRMED
- Each case runs in a fresh subprocess (`worker.py`) -> fresh pipeline singleton ->
  cold Rust CCR store; `reset_compression_store()` clears the Python store
  (`measure.py:531`). This is the correct way to cold-start both CCR layers.
- 6 fixed deterministic seeds `[1000,1137,1274,1411,1548,1685]`, mean+min/max+stdev
  aggregated. No best-of-N anywhere.
- **Reproduced exactly:** my fresh re-run of search@90 medium gave
  `[0.9375, 0.9401, 0.9416, 0.9391, 0.9402, 0.9385]` — **byte-identical to the
  committed `raw_results.json`.**

### 4.3 Default params — CONFIRMED
No `config`/kwargs passed to `compress()`. `RoutingPolicy::MinTokens` is the committed
default (`crates/furl-core/src/transforms/smart_crusher/config.rs:183`).
`default_params_confirmed: true` for all 36 groups. No special-casing/branching on the
new data was found in the harness.

### 4.4 Real external data — CONFIRMED REAL
`verify/data/` holds genuine captures: real `sindresorhus/slugify` source + git log
(pinned commit `7c318bd1aa4b...` matches the cited clone), real ripgrep JSON, real
GitHub commits API dump (71 commits, real shas), real `is-plain-obj` source, a real
`package-lock.json` (271 packages, a DIFFERENT project), and a real macOS install.log.
No fallback to project `benchmarks/` fixtures — the generators are net-new and seed
only from these captures, synthesizing fresh per-row-unique fields (uuids, sha1s, ISO
timestamps). Out-of-sample as required.

### 4.5 Needle test — CLEAN (0 silent loss)
Across all needle cases: **0 silent losses.** Dropped needles are always SIGNALLED via
a `{"_ccr_dropped":"<<ccr:HASH>>"}` sentinel the model would see AND recoverable from
CCR. At high entropy fewer needles are dropped (more stay visible). No unsignalled
drops anywhere.

### 4.6 Multiturn cache-prefix safety — SAFE
For every multiturn case: `preserved_in_order: true`, `index0_intact: true`, no drops,
no reorders. The cache_control-bearing prefix (system + leading turns) is never
dropped or reordered. Prompt-cache safety holds. `cache_prefix_violations: 0`.

### 4.7 Result-cache vs CCR-store divergence (silent-loss probe) — REAL BUG, REPRODUCED
The harness includes an in-process probe documenting a genuine engine failure mode:
the router's Tier-2 result cache (`compression_cache.py`, 300s TTL) serves the crushed
output WITH its `<<ccr:HASH>>` sentinel on a content cache-hit WITHOUT re-running the
CCR mirror, while the CCR store (`compression_store.py`, `DEFAULT_CCR_TTL_SECONDS=300`)
has an INDEPENDENT lifetime. When the CCR entry expires but the result cache still
serves the identical output, the sentinel points to a non-existent entry — a
SIGNALLED but UNRECOVERABLE drop. **Reproduced 3/3 seeds:** first compress backs the
drop; after simulated CCR expiry the second compress (cache hit, same bytes) leaves
the sentinel unbacked. Both TTLs verified in engine source. *Caveat:* the probe
simulates expiry via `reset_compression_store()` rather than waiting 300s real-time —
a sharper trigger than production timing, but the divergence mechanism is real and
source-traced.

---

## 5. Cheats / bugs found

| # | Finding | Severity | Notes |
|---|---|---|---|
| 1 | `multiturn@135` dev-claim key never matches any generated `case_id` (`multiturn@90`/`@900`), so the multiturn degradation (55% vs 70.8%) is **silently never flagged** in the `degradations` list. | **medium** | NOT a correctness cheat — the raw numbers ARE recorded honestly in `groups`; only the auto-degradation detector misses it. Surfaced here explicitly. |
| 2 | `_present_in_text` scalar-substring fallback in `hash_compare_structured` could in principle inflate recovery without CCR. | **low** | Investigated with an independent strict recheck (no fallback): 12/12 cases agree, fallback is not load-bearing. No actual inflation observed. |
| 3 | Silent-loss probe uses `reset_compression_store()` to simulate the 300s CCR TTL expiry rather than real elapsed time. | **low** | The divergence (result-cache hit skips CCR mirror; independent TTLs) is source-real; the trigger is accelerated, not fabricated. |
| 4 | Degradation detector compares only against the MEDIUM tier's `case_id` size; the worst (high-entropy) tier is recorded but not the basis for the `degradations` flag. | **low** | All tiers ARE in `groups`; this report reads the high tier directly. |

No engine source was modified. No warm-cache leakage. No best-of-N. No fixture
fallback. No hardcoding to the new data.

---

## 6. Spot re-check summary (what I re-ran)

1. **search@90 high entropy, seed 1000, needles** -> 24.6% reduction, byte_exact True,
   all needles visible, 0 silent loss. (Confirms the high-entropy collapse.)
2. **search@90 all 6 seeds, medium** -> `[0.9375...0.9385]`, **byte-identical to
   committed raw_results**. (Confirms determinism, no cherry-picking.)
3. **Independent strict reconstruction** (`independent_recheck.py`, no substring
   fallback) across 4 families x 3 tiers -> **strict_byte_exact == harness byte_exact,
   0 mismatches**. (Confirms the lossless claim is real, CCR round-trip genuine.)
4. **Silent-loss probe** -> reproduced 3/3 trials. (Confirms the engine bug.)
5. **Multiturn cache-prefix** -> preserved, index0 intact, no reorder/drop.
6. **code@7** -> 0% at med/high (passthrough confirmed); 66% at low (engine dedups
   identical blobs — claim's "0%" is an under-statement when content is foldable).

**After the spot re-check I trust the harness's numbers.** It is an honest, strict,
reproducible verifier. Its only weakness is a cosmetic degradation-flagging gap
(finding #1), which hides — but does not falsify — the multiturn shortfall. The
substantive story is correct: **lossless holds everywhere; the headline reduction
percentages are real at low/medium entropy and degrade hard on high-entropy /
near-unique real data, exactly where a compressor is supposed to.**
