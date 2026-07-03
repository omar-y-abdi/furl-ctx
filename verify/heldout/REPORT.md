# Held-out Verification — Phase 2 Audit & Verdict (SECOND run)

> **DATED HISTORICAL AUDIT (2026-06-13) — read before citing (TEST-15,
> stamped 2026-07-03).** Verification *record* of the tree as of engine
> commit `e1ce1cb8` / harness commit `50b43d52`, kept verbatim for
> provenance (the Follow-up section below already tracks the two resolved
> leniencies). One claim needs correcting from today's tree:
> **`verify/heldout/raw_results.json` is NOT committed** — the held-out
> sweep regenerates it per run; the "committed red%" columns and the
> degradations-array citations below refer to the file produced by the
> audited run, which the auditor held locally. The numbers are
> *re-runnable* (pinned data + fixed seeds), not verifiable from the
> repository alone.

**Auditor:** independent Phase-2 verifier (adversarial). **Date:** 2026-06-13 (UTC).
**Engine commit under test:** `e1ce1cb8` (round-3: entropy-floor crushability
override + head-dictionary fold + cross-row affix fold + result-cache recompute
fix). Harness committed in `50b43d52`.
**Data:** out-of-sample, real external — `expressjs/express` (pinned
`dae209ae…`), `chalk/chalk` (pinned `aa06bb5a…`), GitHub commits API (express,
100), npm registry (express, 288 versions), `npm/cli` `package-lock.json` (1,196
packages), and a real local `/var/log/install.log`. Disjoint from the first run
(`slugify`/`is-plain-obj`) and from the round-3 training data. Seeds `2000 +
211·i`, 6 seeds/case, mean ± min/max. Tokenizer: real gpt-4o tiktoken via the
engine. Compression + reconstruction via the engine's own public surface only.

---

## Follow-up (post-audit): both leniencies RESOLVED

The two engine-favorable leniencies this report flagged have since been
fixed; this report's tables below describe the **as-found** state and are
kept for provenance. Current state:

1. **Scalar-presence fallback removed — strict by default.** The
   `_present_in_text` fallback in `hash_compare_structured` (and the
   conversation path) is gone (`verify/measure.py`,
   `verify/heldout/measure.py`): a row counts as recovered ONLY via a
   documented surface (visible / `decode_csv_schema_rows` / CCR retrieve),
   so a non-round-tripping item now FAILS. Re-ran the full held-out sweep
   under the strict measure: **0 hash failures, 0 silent loss, 0
   cache-prefix violations** — the lossless contract was never resting on
   the fallback.
2. **Proportional retrieval model replaced with REAL granular cost.**
   Frontier A's granular per-row offload (commit `cbf16a85`) makes
   retrieval proportional; `effective_savings` now charges only the `k`
   largest ACTUAL per-row chunks (resolved via the engine's own
   `ccr_get`), not a slice of one blob. The negative-savings collapse this
   report found (logs@90 high `+55.7% @25% → −10.3%` whole-blob) no longer
   occurs: granular stays **positive at every fraction** (logs@90 high
   `+62% @25% / +44% @50%`). The honest, tier-aware summary now lives in
   `BENCHMARKS.md`.

---

## Bottom line

The reported numbers **replicate at low/medium entropy and on the structured
families, and are genuinely lossless** — but the headline percentages **degrade
hard on fresh high-entropy / near-unique / genuine-entropy data**, exactly where
real-world logs/listings live. The compression is real and the recovery is real
(byte-exact under a STRICT decoder+CCR reconstruction that refuses the harness's
lenient fallback). The dev headline figures are best-case-tier numbers, not
worst-case. No silent data loss, no fake structural folds on random data, no
engine tampering, default params throughout.

The harness is **legitimate but lenient in two engine-favorable ways** (scalar
fallback in the lossless check, and a proportional retrieval-cost model that
understates single-blob CCR retrieval). Neither leniency manufactures a passing
number against the engine — both, if anything, flatter it. After de-cheating the
lossless check with my own strict recheck, every spot case still passes.

---

## Results table (representative tiers, @90/@9, 6 seeds)

| type (tier) | dev claim | fresh mean ± range | lossless? | eff @25% / @50% retrieval | needle survival | round-trip hash OK |
|---|---|---|---|---|---|---|
| logs@90 (medium) | 93.0% | **94.6%** [94.5–94.6] | YES | 70.6% / 46.8% | 18 vis / 18 sig / 0 silent | YES (6/6) |
| logs@90 (high) | 93.0% | **82.4%** [75.4–91.2] | YES | 58.7% / 35.1% | 18 vis / 18 sig / 0 silent | YES (6/6) |
| logs@90 (genuine) | 93.0% | **80.5%** [77.0–84.0] | YES | 57.2% / 33.8% | 18 vis / 18 sig / 0 silent | YES (6/6) |
| search@90 (medium) | 92.7% | **93.4%** [92.9–94.0] | YES | 66.6% / 39.8% | 0 vis / 18 sig / 0 silent | YES (6/6) |
| search@90 (high) | 92.7% | **93.8%** [93.6–93.9] | YES | 67.6% / 41.7% | 6 vis / 18 sig / 0 silent | YES (6/6) |
| repeated_logs@90 | 97.1% | **96.9%** [96.9–97.0] | YES | 73.0% / 49.1% | 18 vis / 18 sig / 0 silent | YES (6/6) |
| multiturn@90 (low) | 70.8% | **80.2%** [80.2–80.2] | YES | 59.6% / 39.0% | n/a (prefix-safe) | YES (6/6) |
| multiturn@90 (medium) | 70.8% | **39.0%** [39.0–39.1] | YES | 39.0% / 39.0% (no drop) | n/a (prefix-safe) | YES (6/6) |
| multiturn@90 (high) | 70.8% | **28.3%** [22.9–32.5] | YES | 28.3% / 28.3% (no drop) | n/a (prefix-safe) | YES (6/6) |
| disk@9 (medium) | 50% | **61.1%** [60.9–61.3] | YES | 61.1% / 61.1% (no drop) | 18 vis / 0 sig / 0 silent | YES (6/6) |
| disk@9 (high) | 50% | **44.1%** [44.0–44.3] | YES | 44.1% / 44.1% (no drop) | 18 vis / 0 sig / 0 silent | YES (6/6) |
| disk@9 (genuine) | 50% | **40.6%** [39.9–41.3] | YES | 40.6% / 40.6% (no drop) | 18 vis / 0 sig / 0 silent | YES (6/6) |
| code@7 (medium+) | 0% | **0.0%** (passthrough) | YES | 0% / 0% | n/a | YES (6/6) |

Bold = mean fresh token reduction. "no drop" = at this small size nothing is
offloaded, so retrieval cost is zero and effective = raw. Needle survival is
summed over 6 seeds × 3 needles. **All 60 group-cases are byte-exact across all
6 seeds; 0 hash failures, 0 silent-loss findings, 0 cache-prefix violations.**

---

## What replicates

- **search@90** holds the claim across every tier, including high-entropy:
  92.9–94.0%. The round-3 reliability claim (search@90 high: erratic 24–94% →
  reliable 93.6% ±0.1pp) **replicates on held-out data**: fresh mean 93.77%,
  spread **0.32pp** (93.58–93.91), all byte-exact. Not overfit to slugify.
- **logs@90 high → round-3 82.4% reduction claim replicates exactly** (fresh
  82.4%, delta −0.0pp), though the spread is wide (75.4–91.2%).
- **logs@90 medium** beats claim (94.6% ≥ 93.0%).
- **repeated_logs@90** matches (96.9% vs 97.1%, within 0.2pp).
- **code@7 passthrough** replicates: identical copies dedup (low tier), but any
  per-row perturbation → 0% (passthrough), matching the "code@7 0%" claim.
- **Lossless contract holds everywhere.** Independent STRICT recheck
  (`strict_recheck.py`, no scalar fallback) on logs@90 high, logs@90 genuine,
  search@90 high (+needles), disk@90 genuine: all `strict_byte_exact=true`,
  `n_missing=0`, reconstructed purely from visible rows + the documented
  `decode_csv_schema_rows` + CCR `<<ccr:HASH>>` retrieve.
- **Anti-cheat negative control passes:** on pure-random sha1 rows (no shared
  structure) the affix/head/dict folds **never fire** (0/6 seeds) — only
  row-dedup/CCR offload runs. No fake structural gain on genuine entropy. The
  affix/head/dict folds **do** fire on the structured tiers (struct: affix 6/6
  for logs+search; head/dict 4/6 for disk; dict 6/6 for multiturn) and decline
  or near-decline on genuine. Confirmed and reproduced.
- **Silent-loss (TTL/result-cache) bug stays fixed:** divergence probe
  reproduced on 3 seeds — second compress after simulated CCR expiry stays
  backed (`all_second_backed=True`, `any_silent_loss=False`).

## What DEGRADES below the dev claim (the result)

| case | dev claim | fresh mean | delta |
|---|---|---|---|
| logs@90 high | 93.0% | 82.4% | **−10.6pp** |
| logs@90 genuine | 93.0% | 80.5% | **−12.5pp** |
| disk@9 high | 50% | 44.1% | **−5.9pp** |
| disk@9 genuine | 50% | 40.6% | **−9.4pp** |
| multiturn@90 medium | 70.8% | 39.0% | **−31.8pp** |
| multiturn@90 high | 70.8% | 28.3% | **−42.5pp** |

- **logs**: the 93% headline is a low/medium-entropy figure. On near-unique
  rows (fresh uuid message + random sha commit + per-row author/service) it
  falls to ~80–82%. Still substantial, but ~11–13pp below the claim, and the
  high tier is volatile (75–91% across seeds).
- **disk@9**: the small-size 50% claim does not survive near-unique names — at
  size 9 nothing is offloaded, so reduction is pure per-row structural folding,
  which earns ~40–44% on high/genuine. (At @90 disk recovers to ~92–95%
  because CCR offload kicks in.)
- **multiturn@90**: the 70.8% headline only holds at LOW entropy/size. At
  realistic medium/high entropy and 90 items the conversation is below the
  crush threshold per-turn, so little drops → 28–39%. It only reaches the
  headline at 900 items (84–97%). The dev figure is size/entropy-cherry-picked.

The degradations array in `raw_results.json` independently flags 4 of these
(`logs@90 high/genuine`, `disk@9 high/genuine`); the multiturn medium/high
shortfalls are visible in the per-tier groups (multiturn is not in the
single-tier degradation keying because its claim tier is ambiguous).

---

## Harness audit (did it cheat?)

| Check | Verdict |
|---|---|
| Reconstructs from compressed output + CCR, sha256 vs ORIGINAL? | **Yes, genuinely.** Confirmed by my independent strict recheck that refuses the lenient fallback. |
| Self-comparison / short-circuit? | **No.** Original multiset hashed from `case.items`; reconstruction built only from output-derived sigs (visible/decoded/CCR), with a presence fallback (see leniency #1). |
| Cold CCR per case? | **Yes.** Each case in a fresh subprocess → fresh Rust+Python CCR. Controls call `reset_compression_store()` per seed. Verified. |
| ≥5 seeds, mean±range, no best-of-N? | **Yes.** 6 seeds, mean/min/max/stdev reported; no max selection anywhere. |
| Default params (MinTokens)? | **Yes.** No `config`/kwargs passed. Engine default is `smart_crusher_routing_policy = "min-tokens"` (content_router.py:522). Independently confirmed. |
| Engine source touched? | **No.** `git diff/status crates/ furl_ctx/` is empty. |
| Real external data, not fixtures? | **Yes.** Real express git log (dependabot commits/dates/40-char hashes), real rg JSONL, real npm/cli lockfile, GitHub commits API, npm registry, real macOS install log. None under `benchmarks/` or `verify/`. CI-log fallback to local macOS log is disclosed in SOURCES.md. |
| Spot-recheck reproduces? | **Yes, bit-identical** (see below). |

### Harness leniencies found (engine-favorable; do not inflate the verdict)

1. **Scalar-presence fallback in `hash_compare_structured` (`_present_in_text`)
   — severity: medium.** An item can count as "reconstructed" if all its scalar
   values appear verbatim *anywhere* in the output, even scattered, without the
   decoder/CCR round-tripping it. This could mask a non-recoverable case as
   byte-exact. **Mitigation/test:** I wrote `strict_recheck.py` that removes
   this fallback entirely and reconstructs ONLY from visible rows +
   `decode_csv_schema_rows` + CCR retrieve. On every spot case it still reports
   `strict_byte_exact=true, n_missing=0` — so the fallback is **not load-bearing**
   for the cases tested; the recovery is real.
2. **Proportional retrieval-cost model understates single-blob CCR retrieval —
   severity: medium.** `effective_savings` charges `r · total_offloaded_tokens`,
   but the engine offloads ALL dropped rows into ONE blob; a single
   `<<ccr:HASH>>` retrieve returns the WHOLE blob. So the FIRST needed row
   already costs the full offloaded payload. Worst-case (any 1 retrieve = full
   blob), effective savings collapse far below the table: e.g. **logs@90 high
   drops from +55.7% @25% to −10.3%** (you spend more than the uncompressed
   original); search@90 high → +5.6%; disk@90 genuine → +8.2%. The @25/@50
   columns are therefore **optimistic** — read them as "savings if retrieval
   were finely sliceable," which it is not. This favors the engine, so it does
   not soften any degradation finding.

Neither leniency fabricates a number against the engine, and the lossless claim
survives the strict recheck.

---

## Spot re-check (re-run by me, this session)

Re-ran 4 cases through the harness's own worker AND through my independent
strict reconstruction:

| case (seed) | committed red% | my re-run red% | match | strict byte-exact |
|---|---|---|---|---|
| logs@90 high (2000) | 76.5% | 76.5% | identical | true (90/90, 1 CCR blob) |
| search@90 high (2000, +needles) | 93.7% | 93.7% | identical | true (93/93) |
| logs@90 genuine (2422) | 82.3% | 82.3% | identical | true (90/90) |
| disk@90 genuine (2633) | 94.7% | 94.7% | identical | true (90/90) |

Drop counts, effective-savings, and needle survival all reproduced
bit-identically. The no-structure control and the silent-loss divergence probe
also reproduced (no fake fold; no silent loss). **The numbers are real and
re-runnable.** Trust the result: the compression and lossless recovery are
genuine; the headline percentages are upper-tier figures that degrade by
~6–43pp on fresh high/near-unique/realistic-entropy data.

---

## Files

- Harness (Phase 1): `verify/heldout/{run,measure,worker,encprobe,generators}.py`,
  `verify/heldout/SOURCES.md`, `verify/heldout/data/*`;
  `verify/heldout/raw_results.json` is produced per run (not committed).
- Phase-2 strict recheck (mine, de-cheats the lossless check):
  `verify/heldout/strict_recheck.py`.
- This report: `verify/heldout/REPORT.md`.
