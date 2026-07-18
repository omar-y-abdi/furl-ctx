//! Array routing — `crush_array` / `crush_array_lossy`, the lossless
//! byte-savings floors, the CCR-backed keep budget, and the
//! `MinTokens`/`LosslessFirst` arbitration (ARCH-4: split out of
//! `crusher.rs` as pure moves, zero behavior change).
//!
//! Builds the lossless candidate (compaction stage + savings gates) and
//! the lossy-recoverable candidate (row-drop + deferred CCR store
//! writes, P0-4), sizes them against each other, and ships the winner —
//! committing the lossy candidate's deferred writes iff it ships.

use serde_json::Value;

use super::compaction::{ColumnEncoding, Compaction};
use super::config::RoutingPolicy;
use super::crusher::{CrushArrayResult, SmartCrusher};
use super::field_role::compute_exclude_set;
use super::persist::{ccr_sentinel_map, dropped_indices_from_kept, CcrWrite, PersistMode};
use super::types::{ArrayAnalysis, CompressionStrategy, DroppedRef};
use crate::transforms::adaptive_sizer::compute_optimal_k;

/// Result of the lossy-recoverable render attempt in
/// [`SmartCrusher::crush_array_lossy`].
///
/// The routing layer needs to tell two cases apart:
/// - **Crushed** — a real DROP render exists (a surfaced `<<ccr:HASH>>`
///   pointer naming the offloadable rows). This is the candidate the
///   `MinTokens` policy sizes against the lossless render.
///   `pending_ccr_writes` carries the deferred store writes backing its
///   markers ([`PersistMode::Collect`]); the routing layer commits them
///   IFF this render ships — a discarded candidate's writes are dropped
///   with it, so the store never holds entries no surfaced marker names
///   (P0-4). Empty under [`PersistMode::Skip`] (mixed dict arm).
/// - **Skip** — the analyzer refused to crush the array (e.g. all-unique
///   entities with no signal). There is NO drop alternative; only the
///   `skip:<reason>` strategy string is carried (PERF-4: no full-array
///   clone for a candidate that ships only when there's also no lossless
///   render — and even then the caller re-uses its own borrow).
enum LossyOutcome {
    Crushed {
        result: CrushArrayResult,
        pending_ccr_writes: Vec<CcrWrite>,
    },
    Skip(String),
}

/// Routing outcome of [`SmartCrusher::crush_array_routed`] (PERF-4).
///
/// The pre-enum shape cloned the FULL input array into a
/// `CrushArrayResult` on every passthrough/skip path — immediately
/// re-wrapped (or discarded) by the caller. The enum makes "nothing
/// changed" a first-class outcome so no kept-items Vec is materialized
/// for it; internal callers (`process_value`'s DictArray arm, the mixed
/// dict arm) re-use their own borrow of the array instead.
pub(super) enum Routed {
    /// The array ships unchanged. Carries the strategy string
    /// (`"none:adaptive_at_limit"` / `"skip:<reason>"` /
    /// `"skip:lossless_only"` / `""` for a reason-less skip).
    Passthrough(String),
    /// A real render shipped: lossless table, survivor-compacted table,
    /// or lossy row-drop.
    Result(CrushArrayResult),
}

/// A lossless render that cleared its gates, BEFORE materialization
/// (PERF-4): carries everything except the `items` clone, which is
/// deferred until the route decision actually picks this candidate —
/// a discarded candidate never pays the full-array deep clone.
struct LosslessCandidate {
    rendered: String,
    kind: &'static str,
    dropped_refs: Vec<DroppedRef>,
}

impl LosslessCandidate {
    /// Materialize the shipped form. The `items` clone happens HERE —
    /// exactly once, on the winning candidate only.
    fn into_result(self, items: &[Value]) -> CrushArrayResult {
        CrushArrayResult {
            items: items.to_vec(), // nothing dropped
            strategy_info: format!("lossless:{}", self.kind),
            ccr_hash: None,
            dropped_summary: String::new(),
            compacted: Some(self.rendered),
            compaction_kind: Some(self.kind),
            row_index_marker: None,
            dropped_refs: self.dropped_refs,
        }
    }
}

/// Render the raw array exactly as the walker ships a passthrough —
/// element-wise through the python-safe writer, byte-identical to
/// `python_safe_json_dumps(&Value::Array(items.to_vec()))` without the
/// deep clone (PERF-4 style). This is the token baseline the small-zone
/// lossy candidate must strictly beat when no lossless candidate exists.
fn render_array_string(items: &[Value]) -> String {
    use crate::util::pyjson::write_python_safe_json;
    let mut out = String::new();
    out.push('[');
    for (i, v) in items.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        write_python_safe_json(v, &mut out);
    }
    out.push(']');
    out
}

/// Materialize a passthrough `CrushArrayResult` (public `crush_array`
/// contract: `items` mirrors the unchanged input).
fn passthrough_result(items: &[Value], strategy_info: String) -> CrushArrayResult {
    CrushArrayResult {
        items: items.to_vec(),
        strategy_info,
        ccr_hash: None,
        dropped_summary: String::new(),
        compacted: None,
        compaction_kind: None,
        row_index_marker: None,
        dropped_refs: Vec::new(),
    }
}

impl SmartCrusher {
    /// Compress an array of dict items.
    ///
    /// Direct port of `_crush_array` (Python line 2400-2687) with the
    /// retired optional subsystems (learning / feedback / telemetry)
    /// in their disabled behavior and CCR wired live. See module-level
    /// docs for the rationale.
    ///
    /// # Pipeline
    ///
    /// 1. Compute `item_strings` once (used as input to adaptive
    ///    sizing and downstream relevance scoring).
    /// 2. `compute_optimal_k` → `adaptive_k`.
    /// 3. If `n <= adaptive_k`, route the tier-1 zone
    ///    (`small_array_route`): lossless candidate vs the EFF-3
    ///    CCR-backed lossy candidate vs passthrough.
    /// 4. `analyzer.analyze_array(items)` → `analysis`.
    /// 5. If `analysis.recommended_strategy == Skip`, return passthrough
    ///    with a `skip:<reason>` strategy string.
    /// 6. `planner.create_plan(analysis, items, query_context, ...)`.
    /// 7. `execute_plan(plan, items)` → result.
    /// 8. Strategy info = `analysis.recommended_strategy.as_str()`.
    pub fn crush_array(&self, items: &[Value], query_context: &str, bias: f64) -> CrushArrayResult {
        match self.crush_array_routed(items, query_context, bias, true) {
            // Public contract: a passthrough result mirrors the unchanged
            // input in `items`. Internal callers use the enum directly
            // and skip this clone (PERF-4).
            Routed::Passthrough(strategy_info) => passthrough_result(items, strategy_info),
            Routed::Result(result) => result,
        }
    }

    /// [`SmartCrusher::crush_array`] with the CCR store writes
    /// switchable (COR-28) and the passthrough outcome unmaterialized
    /// (PERF-4 — see [`Routed`]).
    ///
    /// `persist = false` — used by the mixed-array dict arm — runs the
    /// exact same pipeline and returns a byte-identical result: the
    /// hash, marker text and therefore the `MinTokens` routing decision
    /// are all computed as usual; ONLY the store writes are skipped.
    /// That caller consumes just the kept-items set (plus a PURE
    /// lossless `compacted` render) and surfaces no marker naming the
    /// inner hash — its caller appends a whole-mixed-array sentinel of
    /// its own — so a persisted blob + chunks + index would be orphan
    /// entries nothing can ever retrieve, burning the COR-4-bounded
    /// store capacity.
    ///
    /// Contract for `persist = false` callers: NEVER surface
    /// `dropped_summary` / `ccr_hash` / a sentinel-bearing survivor
    /// `compacted` render from the result — a surfaced pointer to an
    /// unpersisted hash would dangle (and trip the Python mirror's
    /// COR-5 fail-open). The pure lossless `compacted` render
    /// (`dropped_summary` empty) carries no pointer and is safe to ship.
    pub(super) fn crush_array_routed(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
        persist: bool,
    ) -> Routed {
        let item_strings: Vec<String> = items
            .iter()
            .map(|i| serde_json::to_string(i).unwrap_or_default())
            .collect();
        let item_str_refs: Vec<&str> = item_strings.iter().map(|s| s.as_str()).collect();

        let max_k = if self.config.max_items_after_crush > 0 {
            Some(self.config.max_items_after_crush)
        } else {
            None
        };
        let adaptive_k = compute_optimal_k(&item_str_refs, bias, 3, max_k);

        // Tier-1 boundary: the array fits inside the adaptive budget.
        // Historically this zone was LOSSLESS-OR-PASSTHROUGH only; EFF-3
        // adds the lossy-recoverable candidate (CCR-store-backed, default
        // MinTokens policy only) so small arrays — the COMMON case for
        // real tool output — can offload too. See `small_array_route`.
        if items.len() <= adaptive_k {
            return self.small_array_route(
                items,
                &item_strings,
                adaptive_k,
                query_context,
                persist,
            );
        }

        // ── Lossless candidate ──
        //
        // Run the compaction stage ONCE if present. The lossless render keeps
        // every row (nothing dropped); it is a valid candidate only when
        // it actually compacted into a decoder-verifiable shape (COR-13:
        // a flat `Table` — `Buckets`/`Nested` renders are unverifiable by
        // the reference decoder and DECLINE, fail-closed) and clears the
        // byte-savings gate — below that gate the rendering is not worth
        // shipping over either the raw array or the lossy view, so it is
        // not a real alternative.
        //
        // The single run also supplies `lossless_uses_opaque` (computed from
        // the same `Compaction` value below) so we do NOT call stage.run a
        // second time — that was the redundant hot-path double-compaction
        // eliminated by U8.
        let (lossless_candidate, lossless_uses_opaque) =
            if let Some(stage) = self.compaction.as_ref() {
                let (c, rendered) = stage.run(items);
                // Read `contains_opaque_ref` before `c` is potentially moved.
                let uses_opaque = c.contains_opaque_ref();
                // Strict mode tightens the lossless claim to "reconstructible
                // from the visible output ALONE": an opaque-substituted render
                // hides blob bytes behind a `<<ccr:` pointer (recoverable, but
                // a visible information reduction), so it is NOT a candidate
                // under `lossless_only` — same rule the small-array zone
                // already applies unconditionally.
                let opaque_ok = !(self.config.lossless_only && uses_opaque);
                let candidate = if c.is_decoder_verifiable() && opaque_ok {
                    let input_bytes = estimate_array_bytes(&item_strings);
                    let savings_ratio = if input_bytes > 0 {
                        1.0 - (rendered.len() as f64 / input_bytes as f64)
                    } else {
                        0.0
                    };
                    if savings_ratio >= self.config.lossless_min_savings_ratio {
                        let kind = compaction_kind_str(&c);
                        // This render CAN carry opaque substitutions
                        // (decoder-verifiability excludes only Nested
                        // cells) — collect them typed (§4.2 R2). The refs
                        // ride the candidate: they ship iff it ships, and
                        // a discarded candidate's refs drop with it —
                        // exactly like its pending store writes. The
                        // `items` clone is deferred until the candidate
                        // WINS (PERF-4) — see [`LosslessCandidate`].
                        let mut dropped_refs: Vec<DroppedRef> = Vec::new();
                        c.collect_opaque_refs(&mut dropped_refs);
                        Some(LosslessCandidate {
                            rendered,
                            kind,
                            dropped_refs,
                        })
                    } else {
                        None
                    }
                } else {
                    None
                };
                (candidate, uses_opaque)
            } else {
                (None, false)
            };

        // ── Strict lossless-or-passthrough (`lossless_only`) ──
        //
        // The lossy-recoverable candidate is NEVER BUILT in this mode: no
        // rows are dropped, no `<<ccr:HASH>>` sentinel is minted, and no
        // CCR store write happens (`crush_array_lossy` is not invoked, so
        // there are no deferred writes to leak either). Ship the proven-
        // lossless render when it cleared its gates; otherwise pass every
        // row through untouched.
        if self.config.lossless_only {
            return match lossless_candidate {
                Some(lossless) => Routed::Result(lossless.into_result(items)),
                None => Routed::Passthrough("skip:lossless_only".to_string()),
            };
        }

        // ── Lossy-recoverable candidate ──
        //
        // Compress inline + cache the full original via CCR. The runtime
        // caller stashes the full input keyed by `ccr_hash` so a retrieval
        // tool can serve dropped rows back to the LLM on demand. **No data
        // is lost** — "lossy" means "compressed view inline; full payload
        // retrievable via CCR cache." When the array is not safe to crush
        // (the analyzer's `Skip` gate) there is NO lossy alternative — the
        // outcome carries the `skip:<reason>` passthrough so the routing
        // layer can ship it when there's also no lossless render.
        // Does the lossless compaction render rely on opaque-blob
        // substitution (heavy base64/hex fields replaced by an
        // `<<ccr:HASH,KIND,SIZE>>` pointer while EVERY row stays visible)?
        // That path is the dedicated, better-suited treatment for blob-
        // bearing rows: it preserves each row's light fields (id/name/…)
        // inline instead of dropping whole rows. When it applies, the
        // entropy-floor override must stand down so it does not hijack
        // blob data into a row-drop render — both are recoverable, but the
        // opaque-substitution view keeps strictly more visible per row.
        //
        // `lossless_uses_opaque` is derived from the SAME Compaction value
        // produced by the single stage.run call above (U8 dedup).

        // P0-4: build the lossy candidate with its CCR store writes
        // DEFERRED (collect-only). Committing at build time orphaned the
        // blob + chunks + index in the store whenever the routing below
        // chose the lossless render — wasted COR-4-bounded capacity and
        // misleading store stats under hashes no surfaced marker names.
        // The deferred writes are committed exactly when the lossy render
        // ships (the two ship-lossy arms below), so persistence for
        // SHIPPED lossy output remains UNCONDITIONAL — the recovery
        // invariant is timing-shifted, never weakened. Hash/marker
        // computation is mode-independent, so routing cannot shift.
        // `persist = false` (COR-28, mixed dict arm) still means NO
        // writes ever: Skip mode.
        let persist_mode = if persist {
            PersistMode::Collect
        } else {
            PersistMode::Skip
        };
        let lossy = self.crush_array_lossy(
            items,
            query_context,
            &item_strings,
            adaptive_k,
            !lossless_uses_opaque,
            persist_mode,
        );

        // ── Route between the recoverable renders ──
        //
        // When BOTH a lossless render and a lossy DROP render exist they
        // are each 100% recoverable: lossless shows every row; lossy
        // surfaces a `<<ccr:HASH>>` pointer to the CCR-stored originals.
        // So the choice is a pure size decision with no information loss.
        // Under `MinTokens` (the default) ship the fewer-TOKEN render
        // (bytes mislead — hex vs base64); ties prefer lossless (more rows
        // visible). Under `LosslessFirst` keep the legacy behavior:
        // lossless wins whenever it cleared its gate.
        match (lossless_candidate, lossy) {
            (
                Some(lossless),
                LossyOutcome::Crushed {
                    result: lossy,
                    pending_ccr_writes,
                },
            ) => match self.config.routing_policy {
                // Lossless ships → the lossy candidate is discarded and
                // its deferred writes drop with it (no orphans, P0-4).
                RoutingPolicy::LosslessFirst => Routed::Result(lossless.into_result(items)),
                RoutingPolicy::MinTokens => {
                    // The lossless candidate's render IS its final
                    // model-visible string — count it directly (PERF-4:
                    // no materialized result, no clone, same count).
                    let lossless_tokens = self.tokenizer.count_text(&lossless.rendered);
                    let lossy_tokens = self.render_token_count(&lossy);
                    // Lossy wins only when STRICTLY fewer tokens; ties (and
                    // lossless-fewer) → lossless: more rows visible at no
                    // extra token cost.
                    if lossy_tokens < lossless_tokens {
                        // Lossy SHIPS → commit its recovery entries now
                        // (unconditional persist for shipped output).
                        self.commit_ccr_writes(pending_ccr_writes);
                        Routed::Result(lossy)
                    } else {
                        // Lossless ships → discarded candidate's deferred
                        // writes are dropped (no orphans, P0-4).
                        Routed::Result(lossless.into_result(items))
                    }
                }
            },
            // Lossless render valid but the array isn't droppable (Skip):
            // ship lossless — it shows every row losslessly. (A non-
            // droppable array should never drop, and lossless never drops.)
            (Some(lossless), LossyOutcome::Skip(_)) => Routed::Result(lossless.into_result(items)),
            // Only the lossy DROP render is valid → ship it. Its recovery
            // entries are committed on the way out (same unconditional
            // guarantee as before the deferral; only the timing moved).
            (
                None,
                LossyOutcome::Crushed {
                    result: lossy,
                    pending_ccr_writes,
                },
            ) => {
                self.commit_ccr_writes(pending_ccr_writes);
                Routed::Result(lossy)
            }
            // No lossless render and the array isn't droppable → the
            // `skip:<reason>` passthrough (preserves pre-routing behavior).
            (None, LossyOutcome::Skip(reason)) => Routed::Passthrough(reason),
        }
    }

    /// Route an array inside the tier-1 zone (`items.len() <= adaptive_k`).
    ///
    /// Historically this zone was LOSSLESS-OR-PASSTHROUGH: a cleanly-
    /// tabular small array (df/ps-style tool output) ships the verified
    /// lossless render, everything else passes through — small arrays
    /// NEVER produced a lossy-recoverable candidate, capping the most
    /// common real tool-output size at the lossless ceiling (EFF-3:
    /// disk@9 landed 50% where its size-90 twin reached 91%+).
    ///
    /// The zone now builds up to TWO candidates and arbitrates:
    ///
    /// - **Lossless** — same four gates as before, extracted verbatim
    ///   into [`SmartCrusher::small_array_lossless_candidate`].
    /// - **Lossy-recoverable** (EFF-3) — the same row-drop candidate the
    ///   big-array path builds, eligible ONLY when every guarantee
    ///   holds:
    ///   - `lossless_only` is off (strict mode never builds lossy);
    ///   - the policy is `MinTokens` (`LosslessFirst` keeps the legacy
    ///     lossless-or-passthrough zone — the lossless round-trip suite
    ///     asserts that shape directly);
    ///   - a CCR store is configured — a small-zone drop MUST be
    ///     recoverable. Unlike the big path there is NO storeless legacy
    ///     mode here: pre-EFF-3 this zone never dropped, so a storeless
    ///     drop would be a brand-new unrecoverable loss;
    ///   - a drop is actually possible under the CCR-backed keep budget
    ///     (floor 5): `items.len() > ccr_backed_keep_budget(adaptive_k)`.
    ///
    ///   Query pins and critical rows (errors / outliers / anomalies)
    ///   are respected by construction — the candidate is built by the
    ///   same `crush_array_lossy` planner path whose
    ///   `prioritize_indices` pins them beyond budget.
    ///
    /// Arbitration (MinTokens semantics, ties → more rows visible):
    /// - both candidates → lossy ships IFF strictly fewer tokens than
    ///   the lossless render;
    /// - lossy only → lossy ships IFF strictly fewer tokens than the RAW
    ///   passthrough render (the sentinel must pay for itself — a
    ///   token-inflating drop never ships);
    /// - lossless only → lossless ships (pre-EFF-3 contract, unchanged);
    /// - neither → passthrough (`"none:adaptive_at_limit"`).
    ///
    /// P0-4 semantics carry over: the lossy candidate's store writes are
    /// deferred ([`PersistMode::Collect`]) and committed IFF it ships —
    /// a discarded candidate leaves no orphan entries. `persist = false`
    /// (COR-28 mixed dict arm) still means [`PersistMode::Skip`]: no
    /// writes ever, routing byte-identical.
    fn small_array_route(
        &self,
        items: &[Value],
        item_strings: &[String],
        adaptive_k: usize,
        query_context: &str,
        persist: bool,
    ) -> Routed {
        let (lossless_candidate, lossless_uses_opaque) =
            self.small_array_lossless_candidate(items, item_strings);

        // Strict lossless-or-passthrough: the lossy candidate is NEVER
        // BUILT (no drops, no markers, no store writes) — same rule as
        // the big-array path, same passthrough strategy string this
        // zone always used.
        if self.config.lossless_only {
            return match lossless_candidate {
                Some(lossless) => Routed::Result(lossless.into_result(items)),
                None => Routed::Passthrough("none:adaptive_at_limit".to_string()),
            };
        }

        let lossy_eligible = self.config.routing_policy == RoutingPolicy::MinTokens
            && self.ccr_store.is_some()
            && items.len() > ccr_backed_keep_budget(adaptive_k);
        let lossy = if lossy_eligible {
            let persist_mode = if persist {
                PersistMode::Collect
            } else {
                PersistMode::Skip
            };
            // `!lossless_uses_opaque` mirrors the big path: when the
            // compactor wants opaque-blob substitution for this array
            // (a render the small zone REJECTS as a candidate — values
            // must stay verbatim), the entropy-floor override stands
            // down so blob-bearing rows are not hijacked into a
            // row-drop render instead.
            Some(self.crush_array_lossy(
                items,
                query_context,
                item_strings,
                adaptive_k,
                !lossless_uses_opaque,
                persist_mode,
            ))
        } else {
            None
        };

        match (lossless_candidate, lossy) {
            (
                Some(lossless),
                Some(LossyOutcome::Crushed {
                    result: lossy,
                    pending_ccr_writes,
                }),
            ) => {
                let lossless_tokens = self.tokenizer.count_text(&lossless.rendered);
                let lossy_tokens = self.render_token_count(&lossy);
                // Lossy wins only when STRICTLY fewer tokens; ties (and
                // lossless-fewer) → lossless: more rows visible at no
                // extra token cost. Same rule as the big-array race.
                if lossy_tokens < lossless_tokens {
                    self.commit_ccr_writes(pending_ccr_writes);
                    Routed::Result(lossy)
                } else {
                    Routed::Result(lossless.into_result(items))
                }
            }
            // Lossless cleared its gates and no drop render exists (or
            // the analyzer refused) → ship lossless, pre-EFF-3 contract.
            (Some(lossless), _) => Routed::Result(lossless.into_result(items)),
            (
                None,
                Some(LossyOutcome::Crushed {
                    result: lossy,
                    pending_ccr_writes,
                }),
            ) => {
                // No lossless candidate: the alternative is the RAW
                // passthrough, so the drop render must strictly beat
                // THAT — near the keep floor a sentinel can cost more
                // than the rows it hides.
                let passthrough_tokens = self.tokenizer.count_text(&render_array_string(items));
                let lossy_tokens = self.render_token_count(&lossy);
                if lossy_tokens < passthrough_tokens {
                    self.commit_ccr_writes(pending_ccr_writes);
                    Routed::Result(lossy)
                } else {
                    Routed::Passthrough("none:adaptive_at_limit".to_string())
                }
            }
            (None, Some(LossyOutcome::Skip(_)) | None) => {
                Routed::Passthrough("none:adaptive_at_limit".to_string())
            }
        }
    }

    /// The small-array LOSSLESS attempt (pre-EFF-3 gate logic, extracted
    /// verbatim). Returns the candidate plus whether the compacted form
    /// relies on `OpaqueRef` substitution (the big path's
    /// `lossless_uses_opaque`, consumed by the override stand-down).
    ///
    /// Four gates protect the passthrough default beyond the big-array
    /// ratio check:
    /// - decoder-verifiable shape only (COR-13, fail-closed): the
    ///   lossless claim is "exact reconstruction through the reference
    ///   decoder", which today covers flat `Table`s only — `Buckets`
    ///   renders and `Nested` cells DECLINE until the decoder covers
    ///   them;
    /// - no `OpaqueRef` substitution anywhere — on a small array every
    ///   value must stay verbatim in the visible output (substituting a
    ///   file's content with a CCR pointer would hide exactly what the
    ///   model was asked to read);
    /// - absolute saving ≥ `SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES` — the
    ///   schema line must pay for itself; toy arrays stay passthrough;
    /// - the same `lossless_min_savings_ratio` gate as the big-array
    ///   attempt. (EFF-4 measured relaxing this gate to 0.15 for the
    ///   small zone: exactly neutral on every benchmark dataset — the
    ///   EFF-3 lossy candidate out-arbitrates mid-window lossless
    ///   renders under `MinTokens` — so the relaxation was reverted.)
    fn small_array_lossless_candidate(
        &self,
        items: &[Value],
        item_strings: &[String],
    ) -> (Option<LosslessCandidate>, bool) {
        if items.len() < 2 {
            return (None, false);
        }
        let Some(stage) = self.compaction.as_ref() else {
            return (None, false);
        };
        let (c, rendered) = stage.run(items);
        let uses_opaque = c.contains_opaque_ref();
        if !c.is_decoder_verifiable() || uses_opaque {
            return (None, uses_opaque);
        }
        let input_bytes = estimate_array_bytes(item_strings);
        let saved = input_bytes.saturating_sub(rendered.len());
        let savings_ratio = if input_bytes > 0 {
            saved as f64 / input_bytes as f64
        } else {
            0.0
        };
        if clears_small_array_lossless_floor(saved)
            && savings_ratio >= self.config.lossless_min_savings_ratio
        {
            let kind = compaction_kind_str(&c);
            // The `!contains_opaque_ref` gate above means this collects
            // nothing today; collecting anyway keeps the typed carrier
            // correct by construction if the gate ever changes.
            let mut dropped_refs: Vec<DroppedRef> = Vec::new();
            c.collect_opaque_refs(&mut dropped_refs);
            (
                Some(LosslessCandidate {
                    rendered,
                    kind,
                    dropped_refs,
                }),
                uses_opaque,
            )
        } else {
            (None, uses_opaque)
        }
    }

    /// Build the lossy-recoverable render of `items` (row-drop + CCR
    /// sentinel). Returns [`LossyOutcome::Skip`] (carrying the
    /// `skip:<reason>` passthrough) when the array is not safe to crush
    /// (the analyzer's `Skip` gate) — there is no DROP render in that
    /// case. Otherwise returns [`LossyOutcome::Crushed`] with the
    /// row-dropped render plus its deferred store writes: a chosen lossy
    /// render is ALWAYS recoverable — the routing layer commits the
    /// writes iff this render ships (P0-4).
    ///
    /// Factored out of `crush_array` so the routing layer can size this
    /// candidate against the lossless one before deciding which to ship.
    /// The rendered bytes are byte-identical to the pre-routing lossy
    /// path — only the place it is *called from* (and, since P0-4, the
    /// store-write timing) changed.
    fn crush_array_lossy(
        &self,
        items: &[Value],
        query_context: &str,
        item_strings: &[String],
        adaptive_k: usize,
        // When false, the entropy-floor crushability override stands down
        // (a better-suited lossless render — e.g. opaque-blob substitution
        // — exists for this array, so we must not hijack it into a drop).
        allow_skip_override: bool,
        // How the drop's store writes are handled (hash + markers are
        // computed identically in every mode — routing stays
        // byte-identical). `Collect` defers them into the returned
        // outcome for commit-on-ship (P0-4); `Skip` (COR-28, mixed dict
        // arm) never writes. See `crush_array_inner` for the contract.
        persist_mode: PersistMode,
    ) -> LossyOutcome {
        // CCR-BACKED AGGRESSIVE BUDGET: when a CCR store is configured,
        // every dropped row is guaranteed recoverable (unconditional
        // persist + surfaced `<<ccr:HASH>>` pointer — the invariant the
        // adversarial loop locked). Under that guarantee the visible
        // sample only has to carry the *signal* — errors, outliers,
        // anomalies, query-relevant rows (all pinned beyond budget by
        // `prioritize_indices`) — not a generic cross-section, so the
        // keep budget is halved. Without a store the drop would be
        // unrecoverable and the budget stays at the full `adaptive_k`
        // (legacy / parity mode).
        let effective_max_items = if self.ccr_store.is_some() {
            ccr_backed_keep_budget(adaptive_k)
        } else {
            adaptive_k
        };
        // Threads the already-computed serializations into the analyzer's
        // crushability error-keyword scan (PERF-3) — no re-serialization.
        let mut analysis = self
            .analyzer
            .analyze_array_with_strings(items, Some(item_strings));

        // ── CCR-backed crushability override (entropy floor) ──
        //
        // The analyzer's crushability gate refuses to crush near-unique,
        // high-uniqueness arrays UNLESS a "signal" (a numeric anomaly, a
        // change point, an error keyword) happens to be present — see
        // `analyze_crushability` cases 2 & 4 (`unique_entities_no_signal`,
        // `medium_uniqueness_no_signal`). That gate was written for the
        // PERMANENT-LOSS world: with no signal telling us which distinct
        // row matters, dropping any of them risked losing it forever, so
        // the safe choice was to keep them all visible.
        //
        // Under the CCR recovery invariant that premise is gone. When a
        // store is configured every dropped row is persisted + surfaced
        // via a `<<ccr:HASH>>` pointer, so a smaller visible sample loses
        // NO information — the rest is retrievable from the output alone.
        // "We don't know which row matters" therefore no longer argues for
        // keeping everything visible; it argues for a bounded recoverable
        // sample. Worse, the gate is NON-DETERMINISTIC on exactly this
        // data: whether a uniformly-random integer column produces a >2σ
        // anomaly is a per-seed coin-flip, so the SAME near-unique shape
        // flips between "skip → ship all rows" (~34% reduction) and
        // "crush → drop+recover" (~94%) across seeds. That is the
        // erratic 24-94% scatter the verifier measured.
        //
        // Fix: when a CCR store guarantees recovery, do NOT skip on a
        // no-signal reason — re-derive the real pattern strategy (the one
        // `select_strategy` would pick if `crushable` were true) and crush.
        // This is deterministic (independent of the noise signals) and
        // aggressive at the entropy floor. The result still flows through
        // `MinTokens` routing below, so the lossy render only SHIPS when it
        // is actually fewer tokens — the override just makes the
        // recoverable candidate EXIST every time, not at the mercy of a
        // random anomaly. STRUCTURAL skips (mixed types, too-few-items,
        // non-dict) are NOT overridden: those arrays are genuinely
        // un-sampleable, not merely signal-free.
        if analysis.recommended_strategy == CompressionStrategy::Skip
            && allow_skip_override
            && self.config.crush_unique_entities_when_recoverable
            && self.ccr_store.is_some()
            && skip_reason_is_no_signal(&analysis)
        {
            let strategy = self.analyzer.select_strategy(
                &analysis.field_stats,
                analysis.detected_pattern,
                items.len(),
                None, // bypass the crushability veto: recovery is CCR-backed
            );
            if strategy != CompressionStrategy::Skip {
                analysis.recommended_strategy = strategy;
            }
        }

        // Crushability gate: not safe to crush → no DROP candidate. Carry
        // the `skip:<reason>` strategy string so the caller can ship a
        // passthrough when there's also no lossless render (PERF-4: the
        // unchanged items are never cloned here).
        if analysis.recommended_strategy == CompressionStrategy::Skip {
            let reason = match &analysis.crushability {
                Some(c) => format!("skip:{}", c.reason),
                None => String::new(),
            };
            return LossyOutcome::Skip(reason);
        }

        let plan = self.planner().create_plan(
            &analysis,
            items,
            query_context,
            None, // preserve_fields — no production caller supplies these
            Some(effective_max_items),
            Some(item_strings),
        );
        let mut result = self.execute_plan(&plan, items);
        // Computed BEFORE annotation (which only adds keys to kept rows,
        // never changes the row count) so it can gate the stamping below.
        let dropped_count = items.len().saturating_sub(result.len());

        // Field-aware multiplicity (DESIGN.md Imp2). When rows that are
        // identical-except-identity collapse under the stable-projection
        // hash, the kept representative carries a `_dup_count` so the
        // model knows N rows existed. This fires ONLY when the plan
        // actually dropped rows (COR-33: on a no-drop plan every original
        // row is already visible, so stamping is token inflation with
        // zero compression) AND real duplication is present (group size
        // > 1); for all-distinct data (e.g. unique-subject git logs,
        // search results) every group is size 1, no key is added, and
        // the output bytes are unchanged.
        if dropped_count > 0 {
            let exclude = compute_exclude_set(&analysis.field_stats, items);
            if !exclude.is_empty() {
                annotate_dup_counts(&mut result, items, &exclude);
            }
        }

        // CCR persistence + marker emission. **The store write is the
        // cornerstone of CCR's no-data-loss guarantee:** whenever rows
        // are dropped we hash the full original and stash it in the
        // configured store so a dropped needle is *always* recoverable
        // — never silently lost. The plan's `keep_indices` name the
        // surviving rows, so their complement is EXACTLY the dropped
        // set — threaded through so only dropped rows get granular
        // chunks (COR-4: kept rows must never flood the bounded store).
        let dropped_indices = dropped_indices_from_kept(&plan.keep_indices, items.len());
        let (ccr_hash, dropped_summary, row_index_marker, row_drop_refs, pending_ccr_writes) =
            match self.persist_dropped(items, dropped_count, &dropped_indices, persist_mode) {
                Some(persisted) => {
                    let row_index_marker = persisted.row_index_marker();
                    // The typed carrier for this drop — same hash + chunk
                    // count the sentinel advertises (§4.2).
                    let refs = vec![persisted.dropped_ref()];
                    (
                        Some(persisted.hash),
                        persisted.marker,
                        row_index_marker,
                        refs,
                        persisted.pending_writes,
                    )
                }
                None => (None, String::new(), None, Vec::new(), Vec::new()),
            };

        // ── Survivor compaction: lossless re-encoding of the kept rows ──
        //
        // The lossy selection above decides WHICH rows stay visible; this
        // step only decides how those rows are RENDERED. When the
        // compaction stage can render the survivors as a CSV-schema
        // table that is meaningfully smaller than the JSON array form,
        // ship that rendering with the `{"_ccr_dropped": ...}` sentinel
        // appended as a final line. Every kept value stays verbatim in
        // the output and the recovery pointer still names the full
        // original. Gated on:
        // - decoder-verifiable shape only (COR-13, fail-closed): the
        //   survivor render must be provable by the same reference
        //   decoder as the lossless tier — flat `Table` only,
        //   `Buckets`/`Nested` renders decline to the plain JSON form;
        // - no `OpaqueRef` substitution (survivor values must stay
        //   verbatim — same rule as the small-array lossless zone);
        // - absolute saving ≥ `LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`
        //   vs the exact bytes the JSON form would ship.
        if !dropped_summary.is_empty() {
            if let Some(stage) = &self.compaction {
                let (mut c, mut rendered) = stage.run(&result);
                // review F1b: the survivor render's per-column constant folds
                // and dictionary encodings are computed over the SURVIVOR subset
                // only. Left alone they would assert, over the whole array, a
                // fact that only holds for the shown rows — a false-universal
                // `col:type=value` constant (e.g. every event is a
                // `payout_request` when only the payout_request rows survived)
                // or a `__dict:` that enumerates only the categories present in
                // the survivors while others live solely in the offloaded rows.
                // Demote any such encoding that does NOT hold over ALL rows
                // (checked against the full `items`), then re-render. Lossless:
                // the IR rows keep every survivor cell, so a demoted column just
                // renders per-row. Byte-identical when nothing is demoted.
                if demote_subset_only_encodings(&mut c, items) {
                    rendered = stage.formatter.format(&c);
                }
                if c.is_decoder_verifiable() && !c.contains_opaque_ref() {
                    let sentinel = ccr_sentinel_map(&dropped_summary, row_index_marker.as_deref());
                    let sentinel_line = crate::util::pyjson::python_safe_json_dumps(
                        &Value::Object(sentinel.clone()),
                    );
                    let mut json_form_items = result.clone();
                    json_form_items.push(Value::Object(sentinel));
                    let json_form =
                        crate::util::pyjson::python_safe_json_dumps(&Value::Array(json_form_items));
                    let compact_len = rendered.len() + 1 + sentinel_line.len();
                    if clears_lossy_survivor_floor(json_form.len().saturating_sub(compact_len)) {
                        let kind = compaction_kind_str(&c);
                        let rendered_with_sentinel =
                            format!("{}\n{sentinel_line}", rendered.trim_end_matches('\n'));
                        // Survivor renders are gated opaque-free
                        // (`!contains_opaque_ref` above) so this collects
                        // nothing today — kept for correctness under gate
                        // changes. Render order: opaque cells first, the
                        // sentinel (row-drop) is the render's last line.
                        let mut dropped_refs: Vec<DroppedRef> = Vec::new();
                        c.collect_opaque_refs(&mut dropped_refs);
                        dropped_refs.extend(row_drop_refs);
                        return LossyOutcome::Crushed {
                            result: CrushArrayResult {
                                items: result,
                                strategy_info: format!(
                                    "{}+compact:{kind}",
                                    analysis.recommended_strategy.as_str()
                                ),
                                ccr_hash,
                                dropped_summary,
                                compacted: Some(rendered_with_sentinel),
                                compaction_kind: Some(kind),
                                row_index_marker,
                                dropped_refs,
                            },
                            pending_ccr_writes,
                        };
                    }
                }
            }
        }

        LossyOutcome::Crushed {
            result: CrushArrayResult {
                items: result,
                strategy_info: analysis.recommended_strategy.as_str().to_string(),
                ccr_hash,
                dropped_summary,
                compacted: None,
                compaction_kind: None,
                row_index_marker,
                dropped_refs: row_drop_refs,
            },
            pending_ccr_writes,
        }
    }

    /// Count the tokens of the FINAL model-visible string a
    /// `CrushArrayResult` renders to — the exact text `process_value`
    /// substitutes for this array. Used by the `MinTokens` routing policy
    /// to size the lossless vs lossy-recoverable candidates against each
    /// other. The two renders are:
    ///
    /// - `compacted = Some(s)` → the string `s` (lossless table, or lossy
    ///   survivor-compacted table whose last line is the sentinel).
    /// - `compacted = None` → the JSON array `[..items, {"_ccr_dropped":
    ///   marker}]` exactly as `process_value` emits it (the sentinel is
    ///   only appended when something was dropped).
    ///
    /// This mirrors `process_value`'s `DictArray` substitution so the
    /// token count reflects what the model actually sees, not an
    /// approximation.
    fn render_token_count(&self, result: &CrushArrayResult) -> usize {
        let rendered = self.render_result_string(result);
        self.tokenizer.count_text(&rendered)
    }

    /// Render a `CrushArrayResult` to the string `process_value`
    /// substitutes for the array (see [`SmartCrusher::render_token_count`]).
    ///
    /// Composes the `[item0,item1,...,sentinel?]` render element-wise —
    /// byte-identical to `python_safe_json_dumps(&Value::Array(...))`
    /// (the serializer is context-free; `,` separators are appended
    /// here) — without deep-cloning the items into a temporary array
    /// just to count tokens (PERF-4).
    fn render_result_string(&self, result: &CrushArrayResult) -> String {
        use crate::util::pyjson::write_python_safe_json;

        if let Some(s) = &result.compacted {
            return s.clone();
        }
        let sentinel = if result.dropped_summary.is_empty() {
            None
        } else {
            Some(Value::Object(ccr_sentinel_map(
                &result.dropped_summary,
                result.row_index_marker.as_deref(),
            )))
        };
        let mut out = String::new();
        out.push('[');
        for (i, v) in result.items.iter().chain(sentinel.iter()).enumerate() {
            if i > 0 {
                out.push(',');
            }
            write_python_safe_json(v, &mut out);
        }
        out.push(']');
        out
    }
}

/// Stamp `_dup_count` on the kept REPRESENTATIVE of each
/// stable-projection-hash family (over ALL original `items`, with
/// `exclude` identity columns filtered) that has more than one member
/// (DESIGN.md Imp2).
///
/// `_dup_count = N` records that N original rows shared this row's
/// value-bearing content (differing only in excluded identity columns).
/// Only the FIRST kept member of a family — its representative — is
/// stamped (COR-33): when several members of the same family stay
/// visible, stamping each of them made N rows each claim N duplicates,
/// reading like N² originals — token inflation, not information. Rows
/// in a singleton family are left untouched, so all-distinct input is
/// byte-for-byte unchanged. The representative keeps its own real
/// varying values; the dropped duplicates remain CCR-recoverable from
/// the full-original store entry.
fn annotate_dup_counts(
    kept: &mut [Value],
    all_items: &[Value],
    exclude: &std::collections::BTreeSet<String>,
) {
    use crate::transforms::anchor_selector::stable_item_hash;

    // Family sizes over the WHOLE original array.
    let mut family_size: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    for item in all_items {
        if item.is_object() || item.is_array() {
            *family_size
                .entry(stable_item_hash(item, exclude))
                .or_insert(0) += 1;
        }
    }

    // Families whose representative has already been stamped — later
    // kept members of the same family stay untouched (COR-33).
    let mut stamped: std::collections::HashSet<String> = std::collections::HashSet::new();
    for row in kept.iter_mut() {
        if !row.is_object() {
            continue;
        }
        let h = stable_item_hash(row, exclude);
        let count = family_size.get(&h).copied().unwrap_or(1);
        if count > 1 && stamped.insert(h) {
            if let Some(obj) = row.as_object_mut() {
                // Don't clobber a real `_dup_count` field the caller
                // already had (extremely unlikely; defensive).
                obj.entry("_dup_count")
                    .or_insert_with(|| Value::from(count));
            }
        }
    }
}

/// Minimum ABSOLUTE byte saving required before a small array
/// (`len <= adaptive_k`, the tier-1 passthrough zone) ships the
/// lossless compacted rendering instead of passing through. The
/// big-array path uses the ratio gate alone; small arrays additionally
/// need the `[N]{cols}` schema line to pay for itself — re-encoding a
/// 3-row toy array to save a dozen bytes is churn, not compression.
const SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES: usize = 256;

/// Whether an absolute byte saving clears the small-array lossless floor
/// (`>= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`, inclusive). Extracted from the
/// inline gate so the boundary is unit-testable directly (255/256/257) without
/// crafting a renderer-byte-exact fixture through the whole crush pipeline — a
/// directional fixture (well-above / well-below) would survive a `>=`→`>`
/// operator mutation; the boundary test kills it.
#[inline]
fn clears_small_array_lossless_floor(saved: usize) -> bool {
    saved >= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES
}

/// Divisor applied to `adaptive_k` for the lossy keep budget when a CCR
/// store guarantees recovery of every dropped row. 2 (halving) keeps a
/// meaningful visible sample while the critical signals (errors /
/// outliers / anomalies / query pins) remain exempt from the budget.
const CCR_BACKED_KEEP_DIVISOR: usize = 2;

/// Floor for the CCR-backed keep budget. `min_items_to_analyze` (5) is
/// the engine's own notion of "too small to even analyze" — the visible
/// sample never shrinks below it.
const CCR_BACKED_KEEP_FLOOR: usize = 5;

/// Minimum ABSOLUTE byte saving required before the lossy path ships
/// its survivors as a CSV-schema rendering instead of a JSON array.
/// Same churn-protection rationale as the small-array gate: re-encoding
/// a handful of rows to save a few bytes is noise, not compression.
const LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES: usize = 64;

/// Whether an absolute byte saving clears the lossy-survivor render floor
/// (`>= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`, inclusive). Extracted for the
/// same reason as the small-array floor helper: the inclusive boundary
/// (63/64/65) is unit-testable here without a pipeline-byte-exact fixture.
#[inline]
fn clears_lossy_survivor_floor(saved: usize) -> bool {
    saved >= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES
}

/// Is the analyzer's `Skip` a "no SIGNAL on distinct data" skip (as
/// opposed to a STRUCTURAL skip)?
///
/// The crushability gate produces two flavors of `Skip`:
/// - **no-signal skips** — the data IS sampleable (rows are well-formed
///   dicts with comparable fields) but the analyzer found no anomaly /
///   change-point / error-keyword to anchor the sample on, so under the
///   permanent-loss assumption it refused to drop. Reasons:
///   [`SkipReason::UniqueEntitiesNoSignal`],
///   [`SkipReason::MediumUniquenessNoSignal`].
/// - **structural skips** — the array is genuinely un-sampleable
///   (too few items, non-dict items, mixed value types). Those carry
///   different reasons and MUST keep skipping even with CCR backing.
///
/// Only the no-signal flavor is eligible for the CCR-backed override:
/// recovery removes the loss risk that justified the veto, and the data
/// is structurally fine to sample. The eligibility now lives on the
/// typed [`SkipReason::is_no_signal`] (TYPE-1) — its match is
/// exhaustive, so a NEW reason variant is excluded by default
/// (fail-closed), enforced by the compiler instead of a string match.
fn skip_reason_is_no_signal(analysis: &ArrayAnalysis) -> bool {
    analysis
        .crushability
        .as_ref()
        .is_some_and(|c| c.reason.is_no_signal())
}

/// Lossy keep budget when every dropped row is CCR-recoverable.
/// `adaptive_k / 2`, floored at [`CCR_BACKED_KEEP_FLOOR`], never above
/// `adaptive_k` itself.
fn ccr_backed_keep_budget(adaptive_k: usize) -> usize {
    (adaptive_k / CCR_BACKED_KEEP_DIVISOR)
        .max(CCR_BACKED_KEEP_FLOOR)
        .min(adaptive_k)
}

#[cfg(test)]
mod ccr_budget_tests {
    use super::*;

    #[test]
    fn budget_halves_with_floor_and_cap() {
        assert_eq!(ccr_backed_keep_budget(15), 7); // default max_items_after_crush
        assert_eq!(ccr_backed_keep_budget(20), 10);
        assert_eq!(ccr_backed_keep_budget(10), 5); // floor met exactly
        assert_eq!(ccr_backed_keep_budget(8), 5); // floored at 5
        assert_eq!(ccr_backed_keep_budget(4), 4); // never above adaptive_k
        assert_eq!(ccr_backed_keep_budget(3), 3);
    }
}

/// Maps a `Compaction` to a stable kind tag exposed via `CrushArrayResult`.
fn compaction_kind_str(c: &Compaction) -> &'static str {
    match c {
        Compaction::Table { .. } => "table",
        Compaction::Buckets { .. } => "buckets",
        Compaction::OpaqueRef { .. } => "ccr",
        Compaction::Untouched => "untouched",
    }
}

/// Demote survivor-render column encodings that hold ONLY over the shown
/// rows, checked against the FULL `all_rows` array (review F1b / RF2 / RF3).
///
/// The survivor compaction stamps its constant folds and category encodings
/// from the kept subset alone, so on the offload path they can present a subset
/// fact as universal. For every column of a flat [`Compaction::Table`] this
/// clears any such claim that does not hold over EVERY row in `all_rows`:
/// - a `const_value` unless every row holds that exact value;
/// - a `DictString` unless its value list already covers every string the
///   column takes;
/// - an `Affix` unless every string cell carries the declared prefix and suffix
///   (review RF3);
/// - a `HeadDict` unless every string cell's last-delimiter head is in the
///   declared head set (review RF3).
///
/// The positional encodings (`ArithInt` / `IsoDeltaSeconds` / `DecimalScaled`)
/// describe the shown rows by index and make no category claim, so they are
/// left untouched. Column values are resolved with [`resolve_flattened`] so a
/// flattened nested (dotted) column is checked against the real nested value,
/// not a literal-key miss (review RF2).
///
/// Demotion is lossless: the IR rows keep every survivor cell, so a cleared
/// column simply renders per-row. Returns whether anything changed, so the
/// caller re-renders only then and the offload output stays byte-identical
/// whenever every survivor encoding was already universal.
fn demote_subset_only_encodings(c: &mut Compaction, all_rows: &[Value]) -> bool {
    use super::compaction::encodings::{encode_affix_cell, split_head};
    use std::collections::HashSet;

    let Compaction::Table { schema, .. } = c else {
        return false;
    };
    let mut changed = false;
    for spec in schema.fields.iter_mut() {
        // review RF2: a flattened nested column carries a DOTTED name
        // (`meta.region`) whose value lives at `row["meta"]["region"]`, not under
        // a literal `"meta.region"` key — a plain `row.get(name)` was always
        // None there, OVER-demoting genuinely-universal nested consts and
        // UNDER-demoting subset-only nested dicts. `resolve_flattened` resolves
        // the name the same way the compactor flattened it.
        let demote_const = match &spec.const_value {
            Some(v) => !all_rows
                .iter()
                .all(|row| resolve_flattened(row, &spec.name) == Some(v)),
            None => false,
        };
        if demote_const {
            spec.const_value = None;
            changed = true;
        }

        // Category-claim encodings each assert the WHOLE column ranges over a
        // fixed set stamped from the SURVIVOR subset, so on the offload path any
        // of them can present a subset fact as universal (review F1b/RF3): a
        // `__dict:` missing categories, a `__affix:` whose prefix/suffix is false
        // for offloaded rows, a `__head:` whose heads do not cover them. Each is
        // cleared unless it holds over ALL rows. The positional encodings
        // (ArithInt / IsoDeltaSeconds / DecimalScaled) describe the shown rows by
        // index, make no category claim, and are left untouched. A non-string /
        // absent / null cell cannot be represented by any of these string
        // encodings, so it never makes one incomplete (mirrors the dict arm).
        let demote_encoding = match &spec.encoding {
            Some(ColumnEncoding::DictString { values }) => {
                let known: HashSet<&str> = values.iter().map(String::as_str).collect();
                !all_rows
                    .iter()
                    .all(|row| match resolve_flattened(row, &spec.name) {
                        Some(Value::String(s)) => known.contains(s.as_str()),
                        _ => true,
                    })
            }
            Some(ColumnEncoding::Affix { prefix, suffix }) => {
                // `encode_affix_cell` is the exact strip-and-check the compactor
                // proved the round-trip with: it returns `Some` iff the cell
                // carries both affixes without overlap, so an affix-free or
                // overlap-length string fails here just as it failed to stamp.
                !all_rows
                    .iter()
                    .all(|row| match resolve_flattened(row, &spec.name) {
                        Some(Value::String(s)) => encode_affix_cell(s, prefix, suffix).is_some(),
                        _ => true,
                    })
            }
            Some(ColumnEncoding::HeadDict { delim, heads }) => {
                let known: HashSet<&str> = heads.iter().map(String::as_str).collect();
                !all_rows
                    .iter()
                    .all(|row| match resolve_flattened(row, &spec.name) {
                        // `split_head` is the compactor's own last-delimiter
                        // split; a cell without the delimiter, or whose head is
                        // not in the declared set, is not covered by the fold.
                        Some(Value::String(s)) => match split_head(s, *delim) {
                            Some((head, _tail)) => known.contains(head),
                            None => false,
                        },
                        _ => true,
                    })
            }
            _ => false,
        };
        if demote_encoding {
            spec.encoding = None;
            changed = true;
        }
    }
    changed
}

/// Resolve a (possibly dotted, flattened) column name against an ORIGINAL
/// un-flattened row, mirroring the compactor's `flatten_uniform_nested`.
///
/// A literal top-level key wins first — this covers every non-dotted column and
/// any key that literally contains a dot. Otherwise the name is a flattening
/// path (`parent.inner`) traversed segment by segment through nested objects,
/// exactly the structure the compactor folded into the dotted column. Returns
/// `None` when any segment is absent or a non-object (the compactor's `Missing`
/// cell), which the demote check reads as "not this value" (a constant) or "not
/// represented" (a string encoding).
fn resolve_flattened<'a>(row: &'a Value, name: &str) -> Option<&'a Value> {
    if let Some(v) = row.get(name) {
        return Some(v);
    }
    if !name.contains('.') {
        return None;
    }
    let mut cur = row;
    for seg in name.split('.') {
        cur = cur.get(seg)?;
    }
    Some(cur)
}

/// Approximate byte size of `[v0, v1, ...]` JSON serialization, given
/// each item's already-serialized form. Adds 2 for outer brackets and
/// 1 per inter-item comma. Used by the lossless savings-ratio check.
fn estimate_array_bytes(item_strings: &[String]) -> usize {
    let payload: usize = item_strings.iter().map(|s| s.len()).sum();
    let separators = item_strings.len().saturating_sub(1);
    payload + separators + 2
}

#[cfg(test)]
mod tests {
    use super::super::builder::SmartCrusherBuilder;
    use super::super::config::SmartCrusherConfig;
    use super::super::crusher::test_support::{crusher, crusher_with_store, lossless_only_crusher};
    use super::super::persist::canonical_array_json;
    use super::*;
    use crate::ccr::CcrStore;
    use serde_json::json;
    use std::sync::Arc;

    // ---------- crush_array ----------

    #[test]
    fn crush_array_passthrough_when_below_adaptive_k() {
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.items.len(), 3);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.ccr_hash.is_none());
    }

    #[test]
    fn small_array_ships_lossless_when_savings_substantial() {
        // 8 rows — `compute_optimal_k` returns n for n <= 8, so this is
        // guaranteed inside the tier-1 passthrough zone (the SMALL
        // path, not the big-array lossless attempt). Enough repeated-
        // key overhead that the CSV rendering saves ≥ 256 bytes AND
        // ≥ the ratio gate → lossless ships, nothing dropped.
        let c = crusher();
        let items: Vec<Value> = (0..8)
            .map(|i| {
                json!({
                    "filesystem": format!("/dev/disk1s{i}"),
                    "kilobytes_total": 971350180,
                    "kilobytes_used": 543210 + i,
                    "capacity_percent": "85%",
                    "mounted_on": format!("/Volumes/vol_{i}"),
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.items.len(), 8, "nothing may be dropped");
        assert!(
            result.strategy_info.starts_with("lossless:table"),
            "got: {}",
            result.strategy_info
        );
        let compacted = result.compacted.expect("compacted must be set");
        assert!(compacted.starts_with("[8]{"), "got: {compacted}");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
    }

    #[test]
    fn demote_clears_subset_only_constant_and_dict_keeps_universal_review_f1b() {
        use super::super::compaction::{CellValue, FieldSpec, Row, Schema};

        // A survivor render (kept rows all `payout_request`, status all `ok`)
        // whose header would assert those as universal + a genuinely-constant
        // `region`.
        let mut c = Compaction::Table {
            schema: Schema {
                fields: vec![
                    FieldSpec {
                        name: "event_type".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: Some(json!("payout_request")),
                        encoding: None,
                    },
                    FieldSpec {
                        name: "status".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::DictString {
                            values: vec!["ok".into()],
                        }),
                    },
                    FieldSpec {
                        name: "region".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: Some(json!("us")),
                        encoding: None,
                    },
                ],
            },
            rows: vec![Row(vec![
                CellValue::Scalar(json!("payout_request")),
                CellValue::Scalar(json!("ok")),
                CellValue::Scalar(json!("us")),
            ])],
            original_count: 2,
        };

        // The FULL array disagrees: event_type also has `purchase`, status also
        // has `fail`; region is genuinely constant `us`.
        let all_rows = vec![
            json!({"event_type": "purchase", "status": "fail", "region": "us"}),
            json!({"event_type": "payout_request", "status": "ok", "region": "us"}),
        ];

        assert!(demote_subset_only_encodings(&mut c, &all_rows));
        let Compaction::Table { schema, .. } = &c else {
            panic!("still a table");
        };
        let field = |n: &str| schema.fields.iter().find(|f| f.name == n).unwrap();
        assert!(
            field("event_type").const_value.is_none(),
            "false-universal constant must be demoted",
        );
        assert!(
            field("status").encoding.is_none(),
            "category-incomplete dict must be demoted",
        );
        assert_eq!(
            field("region").const_value,
            Some(json!("us")),
            "a genuinely universal constant must be kept",
        );
    }

    #[test]
    fn demote_is_noop_when_encodings_hold_over_all_rows_review_f1b() {
        use super::super::compaction::{CellValue, FieldSpec, Row, Schema};

        let mut c = Compaction::Table {
            schema: Schema {
                fields: vec![
                    FieldSpec {
                        name: "kind".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: Some(json!("tick")),
                        encoding: None,
                    },
                    FieldSpec {
                        name: "lvl".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::DictString {
                            values: vec!["a".into(), "b".into()],
                        }),
                    },
                ],
            },
            rows: vec![Row(vec![
                CellValue::Scalar(json!("tick")),
                CellValue::Scalar(json!("a")),
            ])],
            original_count: 3,
        };
        // Every row agrees with the survivor encodings — nothing to demote, so
        // the offload output stays byte-identical.
        let all_rows = vec![
            json!({"kind": "tick", "lvl": "a"}),
            json!({"kind": "tick", "lvl": "b"}),
            json!({"kind": "tick", "lvl": "a"}),
        ];
        assert!(!demote_subset_only_encodings(&mut c, &all_rows));
        let Compaction::Table { schema, .. } = &c else {
            panic!("still a table");
        };
        assert_eq!(schema.fields[0].const_value, Some(json!("tick")));
        assert!(matches!(
            schema.fields[1].encoding,
            Some(ColumnEncoding::DictString { .. })
        ));
    }

    #[test]
    fn demote_resolves_flattened_nested_columns_review_rf2() {
        use super::super::compaction::{CellValue, FieldSpec, Row, Schema};

        // A survivor render of a FLATTENED nested object: the compactor split
        // `meta` into dotted columns `meta.region` (const "us") and
        // `meta.status` (dict ["ok"]) because the kept rows all shared them.
        let mut c = Compaction::Table {
            schema: Schema {
                fields: vec![
                    FieldSpec {
                        name: "meta.region".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: Some(json!("us")),
                        encoding: None,
                    },
                    FieldSpec {
                        name: "meta.status".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::DictString {
                            values: vec!["ok".into()],
                        }),
                    },
                ],
            },
            rows: vec![Row(vec![
                CellValue::Scalar(json!("us")),
                CellValue::Scalar(json!("ok")),
            ])],
            original_count: 2,
        };

        // The FULL array is UN-flattened: values live under a nested `meta`.
        // region is genuinely universal ("us"); status also takes "fail" in an
        // offloaded row.
        let all_rows = vec![
            json!({"meta": {"region": "us", "status": "ok"}}),
            json!({"meta": {"region": "us", "status": "fail"}}),
        ];

        assert!(demote_subset_only_encodings(&mut c, &all_rows));
        let Compaction::Table { schema, .. } = &c else {
            panic!("still a table");
        };
        let field = |n: &str| schema.fields.iter().find(|f| f.name == n).unwrap();
        // Pre-fix a dotted-name lookup was always None, so a universal nested
        // const was OVER-demoted and a subset-only nested dict UNDER-demoted;
        // resolve_flattened fixes BOTH directions.
        assert_eq!(
            field("meta.region").const_value,
            Some(json!("us")),
            "a genuinely-universal NESTED constant must be kept",
        );
        assert!(
            field("meta.status").encoding.is_none(),
            "a subset-only NESTED dict must be demoted",
        );
    }

    #[test]
    fn demote_clears_subset_only_affix_and_head_review_rf3() {
        use super::super::compaction::{CellValue, FieldSpec, Row, Schema};

        // Survivor render whose header would claim, from the kept rows alone: a
        // shared endpoint prefix, a shared token affix, an `/api/` route head,
        // and a `logs/` dir head.
        let mut c = Compaction::Table {
            schema: Schema {
                fields: vec![
                    FieldSpec {
                        name: "endpoint".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::Affix {
                            prefix: "/api/payout/".into(),
                            suffix: String::new(),
                        }),
                    },
                    FieldSpec {
                        name: "token".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::Affix {
                            prefix: "req-".into(),
                            suffix: "-v1".into(),
                        }),
                    },
                    FieldSpec {
                        name: "route".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::HeadDict {
                            delim: '/',
                            heads: vec!["/api/".into()],
                        }),
                    },
                    FieldSpec {
                        name: "dir".into(),
                        type_tag: "string".into(),
                        nullable: false,
                        const_value: None,
                        encoding: Some(ColumnEncoding::HeadDict {
                            delim: '/',
                            heads: vec!["logs/".into()],
                        }),
                    },
                ],
            },
            rows: vec![Row(vec![
                CellValue::Scalar(json!("/api/payout/1")),
                CellValue::Scalar(json!("req-a-v1")),
                CellValue::Scalar(json!("/api/one")),
                CellValue::Scalar(json!("logs/a")),
            ])],
            original_count: 3,
        };

        // The FULL array: an offloaded `/api/purchase/9` breaks the endpoint
        // prefix and a `/web/health` breaks the `/api/` route head; the token
        // affix and the `logs/` dir head hold over every row.
        let all_rows = vec![
            json!({"endpoint": "/api/payout/1", "token": "req-a-v1", "route": "/api/one", "dir": "logs/a"}),
            json!({"endpoint": "/api/payout/2", "token": "req-b-v1", "route": "/api/two", "dir": "logs/b"}),
            json!({"endpoint": "/api/purchase/9", "token": "req-c-v1", "route": "/web/health", "dir": "logs/c"}),
        ];

        assert!(demote_subset_only_encodings(&mut c, &all_rows));
        let Compaction::Table { schema, .. } = &c else {
            panic!("still a table");
        };
        let field = |n: &str| schema.fields.iter().find(|f| f.name == n).unwrap();
        assert!(
            field("endpoint").encoding.is_none(),
            "a false-universal affix prefix must be demoted",
        );
        assert!(
            field("route").encoding.is_none(),
            "a head set missing an offloaded row's head must be demoted",
        );
        assert!(
            matches!(field("token").encoding, Some(ColumnEncoding::Affix { .. })),
            "an affix that holds over ALL rows must be kept",
        );
        assert!(
            matches!(field("dir").encoding, Some(ColumnEncoding::HeadDict { .. })),
            "a head set covering ALL rows must be kept",
        );
    }

    #[test]
    fn small_toy_array_stays_passthrough_below_absolute_floor() {
        // 3 tiny rows save well above the RATIO gate but only ~a dozen
        // absolute bytes — the schema line doesn't pay for itself.
        let c = crusher();
        let items: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.compacted.is_none());
    }

    // The two tests above pin the SMALL_ARRAY_LOSSLESS floor DIRECTIONALLY
    // (well-above ships lossless, well-below stays passthrough). The two below
    // pin the EXACT inclusive boundary of each absolute-saved gate. Because
    // `saved = estimate_array_bytes - rendered.len()` is a coupled function of
    // the compaction renderer's output, a byte-exact pipeline fixture would be
    // brittle; testing the extracted predicate isolates the `>=` boundary
    // cleanly and kills a `>=`→`>` operator mutation a directional fixture
    // would survive.
    #[test]
    fn small_array_lossless_floor_boundary_is_inclusive_256() {
        assert!(!clears_small_array_lossless_floor(255));
        assert!(clears_small_array_lossless_floor(256));
        assert!(clears_small_array_lossless_floor(257));
    }

    #[test]
    fn lossy_survivor_floor_boundary_is_inclusive_64() {
        assert!(!clears_lossy_survivor_floor(63));
        assert!(clears_lossy_survivor_floor(64));
        assert!(clears_lossy_survivor_floor(65));
    }

    #[test]
    fn small_array_with_opaque_cells_stays_passthrough() {
        // A small array whose cells would be CCR-substituted (file
        // contents!) must NOT take the small-array lossless path — the
        // model needs those values visible verbatim.
        let c = crusher();
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.compacted.is_none());
        assert_eq!(result.items.len(), 4);
    }

    /// The COR-13 nested-cell fixture: long constant columns clear both
    /// byte-savings gates, so ONLY the decoder-coverage gate keeps the
    /// shape out of the lossless tier.
    fn nested_cell_items() -> Vec<Value> {
        (0..6)
            .map(|i| {
                json!({
                    "id": i,
                    "service": "auth-service-primary-eu-central-1.internal.example.com",
                    "status": "ok-and-healthy-and-ready",
                    "region": "eu-central-1-availability-zone-a",
                    "deployment": "blue-green-rollout-2026-06-15T00:00:00Z-primary",
                    "children": [{"k": i}, {"k": i + 1}],
                })
            })
            .collect()
    }

    #[test]
    fn small_array_with_nested_cells_stays_passthrough_without_store() {
        // COR-13 fail-closed: an array-of-objects cell becomes
        // `CellValue::Nested`, whose CSV-quoted IR-JSON rendering the
        // reference decoder cannot invert — the small-array lossless
        // zone must DECLINE it (verbatim passthrough), never ship it as
        // "lossless"-verified. Storeless crusher: the EFF-3 lossy
        // candidate is ineligible too, so the decline lands on plain
        // passthrough — pinning the lossless gate in isolation.
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_default_compaction()
            .build();
        assert!(c.ccr_store.is_none(), "fixture: no store");
        let items = nested_cell_items();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(
            result.strategy_info, "none:adaptive_at_limit",
            "a Nested-cell table must not ship under the lossless claim"
        );
        assert!(result.compacted.is_none());
        assert_eq!(result.items.len(), 6, "nothing may be dropped");
    }

    #[test]
    fn small_array_with_nested_cells_never_claims_lossless_with_store() {
        // Same COR-13 shape WITH a store: the EFF-3 small-zone lossy
        // candidate may legitimately drop rows (recoverably!), but the
        // lossless claim must still be declined — no `lossless:*`
        // strategy and no compacted render (nested cells also fail the
        // survivor-render decoder gate). Any drop must stay CCR-backed.
        let (c, store) = crusher_with_store();
        let items = nested_cell_items();
        let result = c.crush_array(&items, "", 1.0);
        assert!(
            !result.strategy_info.starts_with("lossless:"),
            "a Nested-cell table must never ship under the lossless claim, got {}",
            result.strategy_info
        );
        assert!(
            result.compacted.is_none(),
            "nested cells fail the decoder gate for every compacted render"
        );
        if result.items.len() < items.len() {
            let h = result.ccr_hash.as_ref().expect("hash on drop");
            assert!(result.dropped_summary.contains("<<ccr:"));
            let recovered = store.get(h).expect("dropped payload retrievable");
            assert_eq!(recovered, canonical_array_json(&items));
        } else {
            assert!(result.ccr_hash.is_none());
            assert_eq!(store.len(), 0, "no drop → no store writes (P0-4)");
        }
    }

    #[test]
    fn heterogeneous_buckets_array_never_ships_lossless() {
        // COR-13 fail-closed: a heterogeneous array with a clean string
        // discriminator compacts to `Compaction::Buckets`, whose
        // `__buckets:` grammar the reference decoder cannot decode —
        // the lossless accept gates must DECLINE it. The corpus routes
        // through the big-array candidate gate (60 rows) so the lossy
        // path stays available; whatever wins, no `lossless:` strategy
        // and no `__buckets:` render may ship.
        let c = crusher();
        let items: Vec<Value> = (0..60_i64)
            .map(|i| {
                if i % 2 == 0 {
                    json!({
                        "kind": "user",
                        "name": format!("user-{i:03}"),
                        "email": format!("user{i}@example.com"),
                        "role": if i % 4 == 0 { "admin" } else { "member" },
                    })
                } else {
                    json!({
                        "kind": "metric",
                        "ts": 1_700_000_000_i64 + i,
                        "value": i * 3,
                        "unit": "ms",
                    })
                }
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(
            !result.strategy_info.starts_with("lossless:"),
            "Buckets must be declined from the lossless tier (COR-13); got: {}",
            result.strategy_info
        );
        if let Some(compacted) = &result.compacted {
            assert!(
                !compacted.starts_with("__buckets:"),
                "an unverifiable __buckets: render shipped: {compacted}"
            );
        }
    }

    #[test]
    fn small_array_without_compaction_stage_never_ships_compacted() {
        // No compaction stage → no lossless candidate can exist, so the
        // small zone routes lossy-vs-passthrough only. Whatever ships,
        // `compacted` stays None (there is nothing to render it with)
        // and any drop is CCR-backed (`without_compaction` installs the
        // default store).
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..8)
            .map(|i| {
                json!({
                    "filesystem": format!("/dev/disk1s{i}"),
                    "kilobytes_total": 971350180,
                    "kilobytes_used": 543210 + i,
                    "capacity_percent": "85%",
                    "mounted_on": format!("/Volumes/vol_{i}"),
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
        assert!(
            !result.strategy_info.starts_with("lossless:"),
            "no stage → no lossless claim, got {}",
            result.strategy_info
        );
        if result.items.len() < items.len() {
            // EFF-3: the small zone may now drop here — recoverably.
            assert!(result.ccr_hash.is_some(), "drops must be CCR-backed");
            assert!(result.dropped_summary.contains("<<ccr:"));
        } else {
            assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        }
    }

    // ---------- EFF-3: small-array lossy-recoverable candidate ----------
    //
    // The tier-1 zone (`items.len() <= adaptive_k`) historically shipped
    // lossless-or-passthrough ONLY — small arrays never produced a
    // lossy-recoverable candidate, capping disk@9-style tool output at
    // the lossless ceiling (~50%) while size-90 twins reached 91%+.
    // These tests pin the EFF-3 contract: WITH a CCR store, under the
    // default MinTokens policy, the small zone also builds the drop
    // candidate (keep-budget floor 5, query pins / critical rows
    // respected) and ships it iff strictly fewer tokens; `lossless_only`
    // still suppresses it entirely; without a store nothing ever drops.

    /// One high-entropy small-zone row: distinct at BOTH ends and
    /// hex-dominated in the middle, defeating every lossless fold
    /// (constant/arith/iso/decimal/dict/head-dict/affix) — only the two
    /// short keys fold (~10% savings, far under the ratio gate with
    /// margin even for future relaxations) — while each cell stays
    /// below the 256-byte opaque-substitution threshold (an opaque
    /// render would stand the entropy-floor override down). Heavy
    /// enough (~60 tokens/row) that dropping one row decisively
    /// out-earns the ~40-token sentinel. 8 of these are GUARANTEED
    /// tier-1 (`compute_optimal_k` returns `n` for `n <= 8`, so
    /// `items.len() <= adaptive_k` always).
    fn high_entropy_small_row(i: usize) -> Value {
        // Full-width odd multipliers: every hex segment fills its width
        // with a DISTINCT leading digit (no shared zero-padding for the
        // `__affix` fold to harvest — that fold alone once pushed this
        // fixture over the relaxed ratio gate).
        json!({
            "trace_id": format!(
                "{:032x}",
                (i as u128 + 7).wrapping_mul(0x9E37_79B9_7F4A_7C15_F39C_C060_5CED_C835)
            ),
            "message": format!(
                "{:x} rebalance {:x} attempt-{} lease {:016x} epoch {:x} fence {:08x} seq-{}",
                (i as u128 + 5).wrapping_mul(0xC2B2_AE3D_27D4_EB4F_1656_67B1_2525_2521),
                (i as u128 + 17).wrapping_mul(0x2545_F491_4F6C_DD1D_8446_F35B_7A3B_9525),
                i * 7 + 13,
                (i as u64 + 3).wrapping_mul(6_364_136_223_846_793_005),
                (i as u64 + 11).wrapping_mul(2_862_933_555_777_941_757),
                (i as u32 + 29).wrapping_mul(2_654_435_761),
                i * 4096 + 512
            ),
        })
    }

    #[test]
    fn small_array_with_store_emits_lossy_candidate_and_recovers_byte_exact() {
        // 8 high-entropy rows: the lossless render can't fold genuine
        // entropy (fails the 0.30 gate), so pre-EFF-3 this passed
        // through untouched. With every drop CCR-backed, the small zone
        // now drops to the keep budget and wins the MinTokens race.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..8).map(high_entropy_small_row).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert!(
            result.items.len() < items.len(),
            "small-zone lossy candidate must ship for high-entropy rows, \
             got {} of {} (strategy {})",
            result.items.len(),
            items.len(),
            result.strategy_info
        );
        assert!(
            result.items.len() >= 5,
            "keep-budget floor is 5, got {}",
            result.items.len()
        );
        // Recovery invariant: pointer surfaced + whole original stored.
        let h = result.ccr_hash.as_ref().expect("hash on drop");
        assert!(
            result.dropped_summary.contains(&format!("<<ccr:{h}")),
            "sentinel must carry the recovery pointer, got: {}",
            result.dropped_summary
        );
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(
            recovered,
            canonical_array_json(&items),
            "whole-blob recovery must be byte-exact"
        );
        // Granular row chunks resolve byte-exact too.
        let dropped = items.len() - result.items.len();
        let index_raw = store
            .get(&format!("{h}#rows"))
            .expect("row index committed on ship");
        let row_hashes: Vec<String> = serde_json::from_str(&index_raw).unwrap();
        assert_eq!(row_hashes.len(), dropped, "one chunk per dropped row");
        for rh in &row_hashes {
            let chunk = store.get(rh).expect("row chunk retrievable");
            let arr: Vec<Value> = serde_json::from_str(&chunk).unwrap();
            assert_eq!(arr.len(), 1, "each chunk holds exactly one row");
            assert!(
                items.contains(&arr[0]),
                "chunk must be byte-exact one original row"
            );
        }
        // MinTokens honored: strictly fewer tokens than the raw array.
        let shipped_tokens = c.render_token_count(&result);
        let raw = crate::util::pyjson::python_safe_json_dumps(&Value::Array(items.clone()));
        let raw_tokens = c.tokenizer.count_text(&raw);
        assert!(
            shipped_tokens < raw_tokens,
            "shipped render must be strictly fewer tokens (shipped={shipped_tokens}, raw={raw_tokens})"
        );
    }

    #[test]
    fn small_array_lossless_only_suppresses_lossy_candidate() {
        // Strict mode: the small-zone lossy candidate must never be
        // BUILT — no drops, no markers of any shape, no store writes.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let items: Vec<Value> = (0..8).map(high_entropy_small_row).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 8, "no row may be dropped");
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert!(result.compacted.is_none());
        assert_eq!(store.len(), 0, "strict mode must not write the store");
    }

    #[test]
    fn small_array_query_pinned_rows_survive_lossy_candidate() {
        // A deterministic query anchor ("req-7f3a", quoted) names exactly
        // one row. Whatever render ships out of the small zone, that row
        // must stay visible — query pins are exempt from the keep budget.
        let (c, _store) = crusher_with_store();
        let mut items: Vec<Value> = (0..8).map(high_entropy_small_row).collect();
        items[3] = json!({
            "trace_id": format!("{:032x}", 424_242u128),
            "message": "003-worker checkout handler saw req-7f3a retry storm and shed load",
        });

        let result = c.crush_array(&items, "why did \"req-7f3a\" fail", 1.0);

        // Precondition: the lossy candidate actually shipped (a drop
        // happened) — otherwise survival is vacuous.
        assert!(
            result.ccr_hash.is_some() && result.items.len() < items.len(),
            "fixture must take the small-zone drop path, got {} of {} (strategy {})",
            result.items.len(),
            items.len(),
            result.strategy_info
        );
        let rendered = {
            // The final model-visible string, whatever form shipped.
            let mut out = String::new();
            match &result.compacted {
                Some(s) => out.push_str(s),
                None => {
                    out = crate::util::pyjson::python_safe_json_dumps(&Value::Array(
                        result.items.clone(),
                    ))
                }
            }
            out
        };
        assert!(
            rendered.contains("req-7f3a"),
            "query-pinned row must survive the small-zone drop, got: {rendered}"
        );
    }

    #[test]
    fn small_array_without_store_stays_passthrough() {
        // No CCR store → a small-zone drop would be UNRECOVERABLE, so
        // the lossy candidate must not exist: both high-entropy and
        // low-uniqueness shapes pass through untouched.
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_default_compaction()
            .build();
        assert!(c.ccr_store.is_none(), "this crusher must have no store");

        let unique: Vec<Value> = (0..8).map(high_entropy_small_row).collect();
        let r_unique = c.crush_array(&unique, "", 1.0);
        assert_eq!(r_unique.items.len(), 8, "no unrecoverable drop");
        assert_eq!(r_unique.strategy_info, "none:adaptive_at_limit");
        assert!(r_unique.ccr_hash.is_none());

        // Low-uniqueness twin: crushable WITHOUT the entropy-floor
        // override — the store gate alone must keep it whole (the
        // lossless render may ship; that drops nothing).
        let dupes: Vec<Value> = (0..8)
            .map(|_| json!({"status": "ok", "note": "identical row"}))
            .collect();
        let r_dupes = c.crush_array(&dupes, "", 1.0);
        assert_eq!(r_dupes.items.len(), 8, "no unrecoverable drop");
        assert!(r_dupes.ccr_hash.is_none());
        assert!(r_dupes.dropped_summary.is_empty());
    }

    #[test]
    fn small_array_lossless_first_policy_keeps_legacy_passthrough() {
        // LosslessFirst is the legacy policy: the small zone stays
        // lossless-or-passthrough — no lossy candidate, no arbitration.
        use crate::ccr::InMemoryCcrStore;
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let c = SmartCrusherBuilder::new(SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..SmartCrusherConfig::default()
        })
        .with_default_oss_setup()
        .with_default_compaction()
        .with_ccr_store(store_dyn)
        .build();
        let items: Vec<Value> = (0..8).map(high_entropy_small_row).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 8, "legacy small zone never drops");
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.ccr_hash.is_none());
        assert_eq!(store.len(), 0, "no store writes on the legacy path");
    }

    #[test]
    fn small_array_lossy_declined_when_not_fewer_tokens() {
        // 6 tiny rows: the drop candidate exists (6 > keep floor 5) but
        // dropping ONE ~5-token row cannot pay for the ~40-token
        // sentinel — MinTokens must decline it and pass through.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..6).map(|i| json!({"id": i})).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(
            result.items.len(),
            6,
            "a token-inflating drop must not ship (strategy {})",
            result.strategy_info
        );
        assert_eq!(result.strategy_info, "none:adaptive_at_limit");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(
            store.len(),
            0,
            "declined candidate's deferred writes must be dropped (P0-4)"
        );
    }

    #[test]
    fn small_array_lossless_win_leaves_no_orphan_store_writes() {
        // Constant-heavy tabular rows: the lossless render folds the
        // heavy constant columns once and ditto-marks the rest, beating
        // the drop render (which pays a ~40-token sentinel to hide 3
        // low-residue rows). The discarded lossy candidate's deferred
        // writes must drop with it (P0-4 Collect semantics) — the store
        // stays empty on a lossless win.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..8)
            .map(|i| {
                json!({
                    "host": format!("web-{i:02}"),
                    "region": "eu-central-1a",
                    "image": "registry.example.com/platform/api-gateway:2026-06-15-rc4-9f31c2",
                    "status": "healthy",
                    "port": 8080,
                })
            })
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        assert!(
            result.strategy_info.starts_with("lossless:table"),
            "precondition: lossless must win this shape, got {}",
            result.strategy_info
        );
        assert_eq!(result.items.len(), 8, "lossless drops nothing");
        assert!(result.ccr_hash.is_none());
        assert_eq!(
            store.len(),
            0,
            "discarded small-zone lossy candidate must not commit store writes"
        );
    }

    #[test]
    fn crush_array_no_signal_with_ccr_store_crushes_recoverably() {
        // 30 unique dict items with ID-like fields → the analyzer's
        // crushability gate labels this `unique_entities_no_signal`.
        // Pre-fix that SKIPPED (returned all 30 rows). With a CCR store
        // configured (the `without_compaction` constructor installs the
        // default store) recovery is guaranteed, so the entropy-floor
        // override re-derives a real strategy and crushes — DETERMINISTIC
        // and aggressive, with every dropped row recoverable via the
        // surfaced `<<ccr:HASH>>` pointer. This is the routing fix that
        // collapses the 24-94% scatter on near-unique data.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        // Aggressively crushed: far fewer survivors than the input.
        assert!(
            result.items.len() < items.len(),
            "expected a crush, got {} of {} rows",
            result.items.len(),
            items.len()
        );
        // Recovery invariant: a drop happened, so a CCR pointer is
        // surfaced and the store holds the full original (never silent).
        assert!(
            result.ccr_hash.is_some(),
            "dropped rows must carry a CCR recovery pointer"
        );
        assert!(
            !result.dropped_summary.is_empty(),
            "the `<<ccr:HASH>>` sentinel must be surfaced in the output"
        );
        assert!(
            !result.strategy_info.starts_with("skip:"),
            "no-signal + CCR store must crush, not skip; got {}",
            result.strategy_info
        );
    }

    #[test]
    fn crush_array_no_signal_without_ccr_store_still_skips() {
        // Same near-unique no-signal shape, but NO CCR store: a drop here
        // would be UNRECOVERABLE, so the override must NOT fire — the
        // analyzer's skip stands (legacy / parity mode, zero silent loss).
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .build(); // no `.with_default_ccr_store()`
        assert!(c.ccr_store.is_none(), "this crusher must have no store");
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        // Without recovery backing, the no-signal skip is preserved.
        assert_eq!(result.items.len(), 30);
        assert!(
            result.strategy_info.starts_with("skip:"),
            "expected skip:... without a store, got {}",
            result.strategy_info
        );
    }

    #[test]
    fn crush_array_low_uniqueness_compresses() {
        // 30 items with status=ok across all → low_uniqueness path
        // (crushable, smart_sample strategy).
        let c = crusher();
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.items.len() <= 30, "should not exceed original count");
    }

    #[test]
    fn crush_array_keeps_error_items() {
        let c = crusher();
        let mut items: Vec<Value> = (0..30).map(|i| json!({"id": i, "status": "ok"})).collect();
        items.push(json!({"id": 30, "status": "error", "msg": "FATAL"}));
        let result = c.crush_array(&items, "", 1.0);
        // Whatever path is taken, the error item should survive.
        assert!(
            result
                .items
                .iter()
                .any(|item| { item.get("status").and_then(|v| v.as_str()) == Some("error") }),
            "error item must survive crush_array"
        );
    }

    // ---------- COR-33: `_dup_count` stamping ----------

    #[test]
    fn annotate_dup_counts_stamps_only_the_family_representative() {
        // COR-33 (representative half): when SEVERAL members of the same
        // stable-projection family stay visible, only the FIRST kept
        // member (the representative) may carry `_dup_count`. Stamping
        // every visible copy made N rows each claim N duplicates —
        // reading like N² rows — pure token inflation.
        let all: Vec<Value> = (0..4)
            .map(|i| json!({"req_id": format!("{i:040x}"), "msg": "dup"}))
            .collect();
        let mut kept = vec![all[0].clone(), all[1].clone()];
        let exclude: std::collections::BTreeSet<String> =
            std::iter::once("req_id".to_string()).collect();
        annotate_dup_counts(&mut kept, &all, &exclude);
        assert_eq!(
            kept[0].get("_dup_count"),
            Some(&json!(4)),
            "the representative records the family size"
        );
        assert_eq!(
            kept[1].get("_dup_count"),
            None,
            "non-representative visible copies must NOT be stamped (COR-33)"
        );
    }

    #[test]
    fn dup_count_not_stamped_when_plan_drops_nothing() {
        // COR-33 (no-drop half): `_dup_count` exists to record rows the
        // plan COLLAPSED. When the plan dropped nothing every original
        // row is already visible, so stamping is token inflation with
        // zero compression. Fixture: 30 all-error rows (the error
        // constraint pins every row through the over-budget path) in 3
        // identical-except-identity families, with whole-item dedup
        // disabled so the duplicates stay visible.
        let config = SmartCrusherConfig {
            dedup_identical_items: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusher::without_compaction(config);
        let msgs = ["disk full", "auth expired", "cache miss"];
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "req_id": format!("{i:040x}"),
                    "status": "error",
                    "msg": msgs[i % 3],
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        assert_eq!(
            result.items.len(),
            30,
            "fixture precondition: all-error rows must produce a no-drop plan, got strategy {}",
            result.strategy_info
        );
        assert!(
            result.items.iter().all(|r| r.get("_dup_count").is_none()),
            "a no-drop plan must not stamp `_dup_count` (COR-33); got {:?}",
            result.items
        );
    }

    // ---------- lossless-first default with threshold + CCR-Dropped ----------

    #[test]
    fn without_compaction_yields_none_compacted_field() {
        // The opt-out constructor preserves the lossy-only path.
        // No lossless attempt → compacted/compaction_kind always None.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
    }

    #[test]
    fn lossless_wins_when_savings_above_threshold() {
        // 50 uniform tabular dicts → CSV+schema compaction shrinks the
        // input well above the 0.30 gate, so the LOSSLESS render is a
        // valid candidate. Under `LosslessFirst` it MUST ship (all rows
        // visible, nothing dropped) whenever it clears the gate — that is
        // exactly what this policy guarantees and what this test pins.
        //
        // (The DEFAULT `MinTokens` policy may instead ship the equally-
        // recoverable lossy survivor render when it is fewer tokens —
        // both views are 100% recoverable, so that is a pure size win, not
        // information loss. The MinTokens routing race is covered by the
        // dedicated routing tests below; here we assert the lossless
        // render itself is built and chosen by the policy that owns it.)
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "name": format!("u_{i}"), "status": "ok"}))
            .collect();
        let result = c.crush_array(&items, "", 1.0);
        let compacted = result.compacted.expect("compacted should be set");
        assert!(compacted.starts_with("[50]{"), "got: {compacted}");
        assert_eq!(result.compaction_kind, Some("table"));
        assert!(
            result.strategy_info.starts_with("lossless:table"),
            "got: {}",
            result.strategy_info
        );
        // Lossless = nothing dropped → no CCR retrieval needed.
        assert!(result.ccr_hash.is_none());
        // items preserved (full original).
        assert_eq!(result.items.len(), 50);
    }

    #[test]
    fn lossy_falls_through_when_savings_below_threshold() {
        // Force the threshold high enough that even tabular savings
        // can't satisfy it → lossy path runs → CCR-Dropped fires.
        // Use low-uniqueness items so the analyzer is willing to
        // crush (unique id+name per row would trip the
        // "unique_entities_no_signal" skip gate instead).
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        // Lossless declined → no compacted output.
        assert!(result.compacted.is_none());
        // Lossy ran → rows dropped.
        assert!(
            result.items.len() < 50,
            "expected lossy drop, got {} items",
            result.items.len()
        );
        // CCR hash populated for retrieval.
        let h = result.ccr_hash.expect("ccr_hash populated on drop");
        assert_eq!(h.len(), 12);
        // Marker visible in dropped_summary.
        assert!(
            result.dropped_summary.contains(&format!("<<ccr:{h}")),
            "got: {}",
            result.dropped_summary
        );
        assert!(result.dropped_summary.contains("rows_offloaded"));
    }

    #[test]
    fn ccr_hash_is_deterministic() {
        // Same input → same hash, so the runtime cache key is stable.
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i, "tag": "ok"})).collect();
        let r1 = c.crush_array(&items, "", 1.0);
        let r2 = c.crush_array(&items, "", 1.0);
        assert_eq!(r1.ccr_hash, r2.ccr_hash);
        assert!(r1.ccr_hash.is_some());
    }

    #[test]
    fn ccr_hash_changes_with_input() {
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..Default::default()
        };
        let c = SmartCrusher::new(cfg);
        let a: Vec<Value> = (0..30).map(|i| json!({"id": i})).collect();
        let b: Vec<Value> = (100..130).map(|i| json!({"id": i})).collect();
        let ra = c.crush_array(&a, "", 1.0);
        let rb = c.crush_array(&b, "", 1.0);
        assert_ne!(ra.ccr_hash, rb.ccr_hash);
    }

    #[test]
    fn lossy_without_compaction_still_emits_ccr_hash() {
        // The CCR-Dropped restoration applies regardless of whether
        // lossless was attempted — without_compaction also gets the
        // ccr_hash on row drops. TEST-17: the drop is asserted as a
        // PRECONDITION — the old `if dropped { assert }` gate made this
        // test vacuously green whenever the fixture stopped dropping.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(
            result.items.len() < items.len(),
            "fixture must drop rows (kept {} of {}) — without a drop this \
             test proves nothing",
            result.items.len(),
            items.len()
        );
        assert!(result.ccr_hash.is_some());
        assert!(!result.dropped_summary.is_empty());
    }

    #[test]
    fn passthrough_paths_do_not_emit_ccr_hash() {
        // Tier-1 boundary (items.len() <= adaptive_k): nothing
        // dropped, no CCR. Skip path: same.
        let c = crusher();
        let small: Vec<Value> = (0..3).map(|i| json!({"id": i})).collect();
        let r = c.crush_array(&small, "", 1.0);
        assert!(r.ccr_hash.is_none());
        assert_eq!(r.dropped_summary, "");
    }

    #[test]
    fn compaction_skips_non_object_array() {
        // Compactor returns Untouched for non-object arrays → no
        // compacted field populated, no kind tag.
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_default_compaction()
            .build();
        let items: Vec<Value> = (0..30).map(|i| json!(i)).collect();
        let result = c.crush_array(&items, "", 1.0);
        assert!(result.compacted.is_none());
        assert!(result.compaction_kind.is_none());
    }

    // ---------- recovery-pointer invariant (Defect 1) ----------

    #[test]
    fn advertise_retrieval_tool_false_still_surfaces_recovery_pointer() {
        // Defect 1 (kill silent loss, completed). With
        // `advertise_retrieval_tool=false` the engine STILL surfaces the
        // `<<ccr:HASH>>` recovery pointer in `dropped_summary` AND
        // writes the store. The recovery pointer is the retrieval key,
        // not a UX nicety: you cannot drop a distinct item without
        // leaving a pointer to it. Suppressing the pointer while still
        // dropping rows was the silent-loss bug this test now guards
        // against.
        //
        // (Previously this test asserted the OLD behavior —
        // `dropped_summary.is_empty()` — which encoded exactly the
        // silent loss the recovery invariant forbids on the public
        // path. Fixture flipped to assert the invariant.)
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            advertise_retrieval_tool: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_ccr_store(Arc::clone(&store))
            .build();
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);
        let store_len_after = store.len();

        // Rows were dropped (we built 50, kept fewer).
        assert!(result.items.len() < items.len(), "lossy path didn't fire");
        // The recovery pointer IS surfaced even with the marker flag off.
        assert!(
            result.dropped_summary.contains("<<ccr:"),
            "dropped_summary must carry the recovery pointer even with \
             advertise_retrieval_tool=false (Defect 1), got: {:?}",
            result.dropped_summary
        );
        assert!(result.dropped_summary.contains("rows_offloaded"));
        // The hash is returned so callers can mirror/retrieve.
        let h = result
            .ccr_hash
            .as_ref()
            .expect("ccr_hash should be returned on a drop");
        // The pointer text references the same hash.
        assert!(
            result.dropped_summary.contains(h.as_str()),
            "the pointer must reference the returned hash"
        );
        // ...and the store DID grow — persistence is unconditional.
        assert!(
            store_len_after > store_len_before,
            "ccr_store must grow on a drop (kill silent loss)"
        );
        // The dropped payload is recoverable: the canonical original
        // array round-trips out of the store under the returned hash.
        let recovered = store.get(h).expect("dropped payload must be retrievable");
        let canonical = canonical_array_json(&items);
        assert_eq!(
            recovered, canonical,
            "recovered payload must equal the canonical original array"
        );
    }

    // ---------- strict lossless-or-passthrough (`lossless_only`) ----------

    #[test]
    fn lossless_only_droppable_array_passes_through_no_markers_no_store_writes() {
        // The shape that DOES lossy-drop under defaults (see
        // `lossy_falls_through_when_savings_below_threshold`): low
        // uniqueness, lossless gate forced unreachable. Under
        // `lossless_only` the lossy candidate must never be BUILT —
        // every row passes through, no `<<ccr:` pointer of any shape is
        // minted, and the store stays empty.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // lossless never clears
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 50, "no row may be dropped");
        assert_eq!(result.strategy_info, "skip:lossless_only");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert!(result.compacted.is_none());
        assert_eq!(store.len(), 0, "strict mode must not write the CCR store");
    }

    #[test]
    fn lossless_only_ships_proven_lossless_render_without_markers() {
        // Cleanly tabular input still compacts LOSSLESSLY in strict mode
        // — the mode forbids lossy candidates, not the verified lossless
        // tier. The render must carry every row and no `<<ccr:` pointer.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "name": format!("u_{i}"), "status": "ok"}))
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        let compacted = result.compacted.expect("lossless render should ship");
        assert!(compacted.starts_with("[50]{"), "got: {compacted}");
        assert!(
            !compacted.contains("<<ccr:"),
            "strict-mode lossless render must be pointer-free, got: {compacted}"
        );
        assert!(result.strategy_info.starts_with("lossless:table"));
        assert_eq!(result.items.len(), 50);
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(store.len(), 0, "a pure lossless win writes nothing");
    }

    #[test]
    fn lossless_only_rejects_opaque_bearing_lossless_render() {
        // Long base64 columns normally make the compactor substitute
        // cells with `<<ccr:HASH,base64,SIZE>>` (opaque refs) — a
        // recoverable render that still hides visible bytes. Strict mode
        // must neither ship such a render NOR write the store: the
        // builder disables the stage's `substitute_opaque` (the Defect-2
        // store write is EAGER inside `compact()`, so a routing-layer
        // rejection alone would come after the write), and the blobs-
        // verbatim render then fails the savings gate → passthrough.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();

        // Counterfactual precondition: under the DEFAULT config this very
        // fixture ships an opaque-substituted render (pointers in the
        // output) — proving the strict-mode decline below is the work of
        // the new gates, not an accident of the fixture.
        let (default_c, _s) = lossless_only_crusher(SmartCrusherConfig::default());
        let default_result = default_c.crush_array(&items, "", 1.0);
        assert!(
            default_result
                .compacted
                .as_deref()
                .is_some_and(|r| r.contains("<<ccr:")),
            "fixture precondition: default config must ship an opaque render, got {}",
            default_result.strategy_info
        );

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 30, "nothing may be dropped");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(
            store.len(),
            0,
            "the eager Defect-2 opaque write must not fire in strict mode"
        );
        // With substitution off, the CONSTANT blob column folds into the
        // declaration (`content:string=<blob>` — verbatim, exactly once):
        // a legitimately PURE lossless render that may still win the
        // savings gate. Whichever way the gate lands, the strict-mode
        // output must carry the blob bytes verbatim and no pointer.
        match &result.compacted {
            Some(render) => {
                assert!(
                    !render.contains("<<ccr:"),
                    "strict-mode render must be pointer-free"
                );
                assert!(
                    render.contains(&blob),
                    "the blob must appear verbatim in the render"
                );
                assert!(result.strategy_info.starts_with("lossless:"));
            }
            None => {
                let rendered = serde_json::to_string(&result.items).unwrap();
                assert!(!rendered.contains("<<ccr:"), "no pointer may be minted");
                assert!(rendered.contains(&blob), "blobs must stay verbatim");
            }
        }
    }

    #[test]
    fn lossless_only_distinct_blob_rows_pass_through_untouched() {
        // DISTINCT per-row blobs (distinct at BOTH ends, so neither the
        // constant fold nor the affix/head-dict encoders apply): the
        // blobs-verbatim render cannot clear the savings gate, and the
        // opaque-substituted render is disabled in strict mode — so the
        // array must pass through untouched with an empty store.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let base = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "path": format!("src/f{i}.py"),
                    "content": format!("{i:04}{}{i:04}", base.repeat(64)),
                })
            })
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        assert_eq!(result.items.len(), 30, "nothing may be dropped");
        assert!(result.compacted.is_none(), "no render can clear the gate");
        assert!(result.ccr_hash.is_none());
        assert!(result.dropped_summary.is_empty());
        assert_eq!(store.len(), 0, "strict mode must not write the store");
        let rendered = serde_json::to_string(&result.items).unwrap();
        assert!(!rendered.contains("<<ccr:"), "no pointer may be minted");
    }

    #[test]
    fn lossless_only_routing_gate_declines_opaque_render_even_if_stage_substitutes() {
        // Belt-and-braces layer: the ROUTING gate (`opaque_ok` in
        // `crush_array_inner`) must decline an opaque-bearing render even
        // when a hand-composed crusher pairs `lossless_only` with a stage
        // that still substitutes (e.g. via `from_parts` — the production
        // builder always disables `substitute_opaque`, but the strict-mode
        // output invariant must not depend on wiring).
        let (mut c, _store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        // Adversarial wiring: re-enable substitution behind the mode's back.
        c.compaction
            .as_mut()
            .expect("builder installs a stage")
            .config
            .substitute_opaque = true;
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();

        let result = c.crush_array(&items, "", 1.0);

        assert!(
            result.compacted.is_none(),
            "routing gate must decline the opaque render in strict mode"
        );
        assert_eq!(result.items.len(), 30, "nothing may be dropped");
        assert!(result.ccr_hash.is_none());
        let rendered = serde_json::to_string(&result.items).unwrap();
        assert!(!rendered.contains("<<ccr:"), "no pointer may ship");
    }

    #[test]
    fn ccr_backed_store_tightens_lossy_budget_vs_storeless() {
        // With a CCR store every dropped row is recoverable, so the
        // lossy keep budget halves; without a store the legacy full
        // `adaptive_k` budget applies (a tighter budget there would
        // drop unrecoverable rows for nothing).
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let mk_cfg = || SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..SmartCrusherConfig::default()
        };
        let items: Vec<Value> = (0..60)
            .map(|i| json!({"msg": format!("entirely distinct message number {}", i)}))
            .collect();

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let with_store = SmartCrusherBuilder::new(mk_cfg())
            .with_ccr_store(Arc::clone(&store))
            .build();
        let without_store = SmartCrusherBuilder::new(mk_cfg()).build();

        let r_store = with_store.crush_array(&items, "", 1.0);
        let r_legacy = without_store.crush_array(&items, "", 1.0);

        assert!(
            r_store.items.len() < r_legacy.items.len(),
            "store-backed budget must keep fewer rows ({} vs {})",
            r_store.items.len(),
            r_legacy.items.len()
        );
        // Everything dropped under the tightened budget is recoverable.
        let h = r_store.ccr_hash.as_ref().expect("hash on drop");
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    /// One realistic git-log-shaped row: identity columns (40-hex commit,
    /// ISO date), a low-cardinality author, and a genuinely varied unique
    /// subject built from rotating conventional-commit vocabulary.
    fn log_shaped_row(i: usize) -> Value {
        const PREFIXES: [&str; 8] = [
            "feat", "fix", "docs", "chore", "refactor", "test", "perf", "ci",
        ];
        const AREAS: [&str; 10] = [
            "crusher",
            "proxy",
            "ccr",
            "router",
            "bench",
            "tokenizer",
            "store",
            "pipeline",
            "compaction",
            "relevance",
        ];
        const VERBS: [&str; 10] = [
            "add", "remove", "rework", "guard", "pin", "extend", "isolate", "deflake", "speed up",
            "harden",
        ];
        const THINGS: [&str; 15] = [
            "the lossy budget",
            "novelty fill",
            "sentinel emission",
            "marker parsing",
            "store mirroring",
            "field-role gates",
            "ditto marks",
            "schema folding",
            "query anchors",
            "drop accounting",
            "TTL handling",
            "thread-local state",
            "import guards",
            "error surfaces",
            "byte parity",
        ];
        json!({
            "commit": format!("{:040x}", (i as u128 * 2_654_435_761 + 12_345)),
            "author": format!("Author {}", i % 7),
            "date": format!(
                "2026-{:02}-{:02}T{:02}:{:02}:00+02:00",
                (i % 12) + 1,
                (i % 28) + 1,
                i % 24,
                (i * 13) % 60
            ),
            "subject": format!(
                "{}({}): {} {} #{}",
                PREFIXES[i % 8],
                AREAS[i % 10],
                VERBS[i % 10],
                THINGS[i % 15],
                i + 100
            ),
        })
    }

    #[test]
    fn lossy_survivor_compaction_ships_table_with_sentinel_line() {
        // When the lossy path drops rows AND the survivors render as a
        // smaller CSV-schema table, the output is the rendering with the
        // `{"_ccr_dropped": ...}` sentinel appended as the final line.
        // Every survivor value stays verbatim; the dropped rows stay
        // recoverable under the surfaced hash.
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force the lossy path
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_compaction()
            .with_ccr_store(Arc::clone(&store))
            .build();
        // High-entropy distinct rows (git-log shaped): hex/ISO identity
        // columns, repeating author, genuinely varied unique subjects
        // (uniformly-templated subjects trip the
        // `skip:unique_entities_no_signal` crushability gate and never
        // reach the lossy path — mirroring how real logs behave).
        let items: Vec<Value> = (0..60).map(log_shaped_row).collect();

        let result = c.crush_array(&items, "", 1.0);
        assert!(result.items.len() < items.len(), "lossy path didn't fire");

        let rendered = result
            .compacted
            .as_ref()
            .expect("survivor compaction should win on key-heavy log rows");
        // Sentinel is the final line and carries the recovery pointer.
        let last_line = rendered.lines().last().expect("non-empty rendering");
        assert!(
            last_line.starts_with("{\"_ccr_dropped\":"),
            "sentinel must be the final line, got: {last_line:?}"
        );
        assert!(last_line.contains("<<ccr:"), "sentinel carries the pointer");
        // Every survivor's subject is verbatim in the rendering.
        for row in &result.items {
            let subject = row["subject"].as_str().unwrap();
            assert!(
                rendered.contains(subject),
                "survivor value must stay verbatim: {subject}"
            );
        }
        // Dropped rows recoverable under the surfaced hash.
        let h = result.ccr_hash.as_ref().expect("hash on drop");
        assert!(last_line.contains(h.as_str()), "pointer names the hash");
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    #[test]
    fn advertise_retrieval_tool_true_is_default_behavior() {
        // Default config still emits markers + stores when rows drop.
        // Sanity: the gate is opt-out, not opt-in.
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..SmartCrusherConfig::default()
        };
        // Default: advertise_retrieval_tool = true.
        assert!(cfg.advertise_retrieval_tool);
        let c = SmartCrusherBuilder::new(cfg)
            .with_ccr_store(Arc::clone(&store))
            .build();
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();

        let store_len_before = store.len();
        let result = c.crush_array(&items, "", 1.0);
        let store_len_after = store.len();

        assert!(result.items.len() < items.len(), "lossy path didn't fire");
        assert!(result.ccr_hash.is_some(), "default should produce a hash");
        assert!(
            result.dropped_summary.contains("<<ccr:"),
            "default should produce a marker: {:?}",
            result.dropped_summary
        );
        assert!(
            store_len_after > store_len_before,
            "default should write to ccr_store"
        );
    }

    // ---------- Phase 7: route-by-min-tokens ----------

    /// Build a default-config crusher (MinTokens) plus a LosslessFirst
    /// twin, both sharing one in-memory CCR store, so a routing test can
    /// compare the two policies and still recover any dropped rows.
    fn min_tokens_and_lossless_first() -> (
        SmartCrusher,
        SmartCrusher,
        std::sync::Arc<crate::ccr::InMemoryCcrStore>,
    ) {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let mk = |policy: RoutingPolicy| {
            SmartCrusherBuilder::new(SmartCrusherConfig {
                routing_policy: policy,
                ..SmartCrusherConfig::default()
            })
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(Arc::clone(&store_dyn))
            .build()
        };
        (
            mk(RoutingPolicy::MinTokens),
            mk(RoutingPolicy::LosslessFirst),
            store,
        )
    }

    #[test]
    fn min_tokens_ships_lossy_for_logs_shaped_data() {
        // Logs-shaped: per-row entropy (40-hex commit + distinct subject)
        // shipped 90× makes the lossless render token-expensive; dropping
        // to a small visible sample + a `<<ccr:HASH>>` sentinel is far
        // fewer tokens. MinTokens must pick the lossy DROP render — and
        // the dropped rows must remain recoverable from the store.
        let (min_tokens, lossless_first, store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..90).map(log_shaped_row).collect();

        let r_min = min_tokens.crush_array(&items, "", 1.0);
        let r_loss = lossless_first.crush_array(&items, "", 1.0);

        // MinTokens drops (lossy chosen): a hash is surfaced.
        assert!(
            r_min.ccr_hash.is_some(),
            "MinTokens must ship the lossy DROP render for logs-shaped data; got strategy {:?}",
            r_min.strategy_info
        );
        assert!(
            r_min.items.len() < items.len(),
            "lossy must actually drop rows"
        );

        // The chosen lossy render is fewer tokens than the lossless one.
        let lossy_tokens = min_tokens.render_token_count(&r_min);
        let lossless_tokens = lossless_first.render_token_count(&r_loss);
        assert!(
            lossy_tokens < lossless_tokens,
            "lossy must be strictly fewer tokens (lossy={lossy_tokens}, lossless={lossless_tokens})"
        );

        // LosslessFirst ships the lossless render for the same data.
        assert!(
            r_loss.ccr_hash.is_none() && r_loss.compacted.is_some(),
            "LosslessFirst must ship the lossless render; got strategy {:?}",
            r_loss.strategy_info
        );

        // Recovery proof: every dropped row is retrievable from the store
        // under the surfaced hash (the chosen lossy render loses nothing).
        let h = r_min.ccr_hash.as_ref().unwrap();
        let recovered = store.get(h).expect("dropped payload retrievable");
        assert_eq!(recovered, canonical_array_json(&items));
    }

    #[test]
    fn min_tokens_ships_lossless_when_it_is_fewer_tokens() {
        // A low-cardinality tabular array whose every row collapses under
        // dedup: the lossy path keeps the same content the lossless table
        // shows, so the lossless render is ≤ tokens. Under MinTokens the
        // tie-or-fewer goes to lossless (more rows visible). Nothing is
        // dropped → the output is recoverable inline, no CCR needed.
        let (min_tokens, lossless_first, _store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..12).map(|_| json!({"a": 1, "b": 2})).collect();

        let r_min = min_tokens.crush_array(&items, "", 1.0);
        let r_loss = lossless_first.crush_array(&items, "", 1.0);

        // MinTokens ships lossless: nothing dropped, compacted populated.
        assert!(
            r_min.ccr_hash.is_none() && r_min.compacted.is_some(),
            "MinTokens must ship the lossless render when it is ≤ tokens; got strategy {:?}",
            r_min.strategy_info
        );
        assert_eq!(r_min.items.len(), items.len(), "lossless drops nothing");

        // LosslessFirst ships lossless too (same render for this shape).
        assert!(
            r_loss.ccr_hash.is_none() && r_loss.compacted.is_some(),
            "LosslessFirst must ship lossless here; got strategy {:?}",
            r_loss.strategy_info
        );
        // The chosen render is identical across policies in this case.
        assert_eq!(r_min.compacted, r_loss.compacted);
    }

    #[test]
    fn min_tokens_never_ships_more_tokens_than_lossless() {
        // The core invariant: under MinTokens the shipped render is never
        // MORE tokens than the lossless render would have been — for any
        // droppable array where both candidates exist. (Lossy wins only
        // when STRICTLY fewer; ties go to lossless.) Pin it on the
        // logs-shaped family where lossy genuinely wins.
        let (min_tokens, lossless_first, _store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..90).map(log_shaped_row).collect();

        let r_min = min_tokens.crush_array(&items, "", 1.0);
        let r_loss = lossless_first.crush_array(&items, "", 1.0);

        let min_tokens_count = min_tokens.render_token_count(&r_min);
        let lossless_tokens = lossless_first.render_token_count(&r_loss);
        assert!(
            min_tokens_count <= lossless_tokens,
            "MinTokens must never ship more tokens than lossless \
             (chosen={min_tokens_count}, lossless={lossless_tokens})"
        );
    }

    // ---------- P0-4: no orphan store-writes for a non-shipped lossy candidate ----------
    //
    // The defect these pin: `crush_array_inner` builds the lossy candidate
    // for MinTokens/LosslessFirst arbitration, and `persist_dropped` used
    // to COMMIT its store writes (whole-blob + granular chunks + row
    // index) at build time — before the routing decision. When the
    // LOSSLESS render won and shipped, those writes stayed behind as
    // orphans: entries no surfaced marker names, burning COR-4-bounded
    // capacity and inflating store stats. The fix defers the candidate's
    // writes (collect-only) and commits them exactly when the lossy
    // render ships — persistence for SHIPPED lossy output stays
    // unconditional (the recovery invariant is timing-shifted, never
    // weakened), and hashes/markers are computed identically either way.

    #[test]
    fn lossless_first_win_writes_nothing_for_discarded_lossy_candidate() {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        // Low-uniqueness (analyzer willing to crush → a real lossy DROP
        // candidate is built) AND cleanly tabular (lossless clears the
        // 0.30 gate) → both candidates exist; LosslessFirst ships lossless.
        let items: Vec<Value> = (0..50).map(|i| json!({"status": "ok", "seq": i})).collect();

        let result = c.crush_array(&items, "", 1.0);

        // Precondition (asserted, not if-guarded): lossless shipped.
        assert!(
            result.compacted.is_some() && result.ccr_hash.is_none(),
            "precondition: lossless render must ship under LosslessFirst, got {}",
            result.strategy_info
        );
        assert_eq!(result.items.len(), items.len(), "lossless drops nothing");
        // The discarded lossy candidate must leave NO entries behind.
        assert_eq!(
            store.len(),
            0,
            "discarded lossy candidate must not commit store writes (orphan entries)"
        );
    }

    #[test]
    fn min_tokens_lossless_win_writes_nothing_for_discarded_lossy_candidate() {
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::SmartCrusherBuilder;
        use std::sync::Arc;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        // Default policy IS MinTokens; spelled out because the test is
        // specifically about the MinTokens arbitration arm.
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::MinTokens,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        // The pinned MinTokens lossless-win shape (see
        // `min_tokens_ships_lossless_when_it_is_fewer_tokens`): identical
        // low-cardinality rows dedup so hard that the lossless table is
        // ≤ tokens vs the drop render — ties go to lossless. The lossy
        // candidate is still BUILT for arbitration; it must not write.
        let items: Vec<Value> = (0..12).map(|_| json!({"a": 1, "b": 2})).collect();

        let result = c.crush_array(&items, "", 1.0);

        assert!(
            result.compacted.is_some() && result.ccr_hash.is_none(),
            "precondition: lossless render must win under MinTokens here, got {}",
            result.strategy_info
        );
        assert_eq!(
            store.len(),
            0,
            "MinTokens lossless win must leave no orphan lossy store writes"
        );
    }

    #[test]
    fn min_tokens_lossy_win_still_commits_store_writes_unconditionally() {
        // The P0-4 deferral must NOT weaken the recovery invariant: when
        // the lossy render SHIPS out of the arbitration arm (both
        // candidates existed, lossy strictly fewer tokens), its store
        // writes are committed exactly as before — whole-blob under the
        // surfaced hash, plus the granular row index when in budget. Only
        // the write TIMING moved (build → ship decision).
        let (min_tokens, _lossless_first, store) = min_tokens_and_lossless_first();
        let items: Vec<Value> = (0..90).map(log_shaped_row).collect();

        let r = min_tokens.crush_array(&items, "", 1.0);

        let h = r
            .ccr_hash
            .as_ref()
            .expect("lossy is the pinned winner for logs-shaped data");
        let dropped = items.len() - r.items.len();
        assert!(dropped > 0, "lossy must actually drop rows");
        // Whole-blob committed under the surfaced hash.
        assert_eq!(
            store.get(h).as_deref(),
            Some(canonical_array_json(&items).as_str()),
            "shipped lossy render must persist the whole-blob (unconditional)"
        );
        // Granular index + one chunk per dropped row committed too (this
        // drop is well inside the capacity/4 budget).
        let idx_marker = r
            .row_index_marker
            .as_ref()
            .expect("store-backed in-budget drop advertises the row index");
        let index_raw = store
            .get(&format!("{h}#rows"))
            .expect("row index must be committed when the lossy render ships");
        let row_hashes: Vec<String> = serde_json::from_str(&index_raw).unwrap();
        assert_eq!(row_hashes.len(), dropped, "one chunk per dropped row");
        assert!(
            idx_marker.contains(&format!("#rows {}_chunks>>", row_hashes.len())),
            "marker advertises exactly the committed chunk count, got: {idx_marker}"
        );
        assert_eq!(
            store.len(),
            dropped + 2,
            "chunks + index + whole-blob — nothing more, nothing less"
        );
    }

    // ---------- U8: single compaction pass on large-array hot path ----------

    /// A [`Formatter`] spy that counts how many times `format` is called.
    /// Each call to `CompactionStage::run` calls `format` exactly once, so
    /// this is a direct proxy for the number of `stage.run(items)` calls.
    struct CountingFormatter {
        inner: Box<dyn super::super::compaction::Formatter>,
        count: Arc<std::sync::atomic::AtomicUsize>,
    }

    impl super::super::compaction::Formatter for CountingFormatter {
        fn name(&self) -> &str {
            self.inner.name()
        }
        fn format(&self, c: &super::super::compaction::Compaction) -> String {
            self.count
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            self.inner.format(c)
        }
    }

    /// Build a [`SmartCrusher`] wired with a [`CountingFormatter`] and
    /// return the call-count handle alongside the crusher.
    fn crusher_with_counting_compaction(
        cfg: SmartCrusherConfig,
    ) -> (SmartCrusher, Arc<std::sync::atomic::AtomicUsize>) {
        use super::super::compaction::{CompactConfig, CompactionStage, CsvSchemaFormatter};
        let counter = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let stage = CompactionStage {
            config: CompactConfig::default(),
            formatter: Box::new(CountingFormatter {
                inner: Box::new(CsvSchemaFormatter::new()),
                count: Arc::clone(&counter),
            }),
        };
        let crusher = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_compaction(stage)
            .build();
        (crusher, counter)
    }

    /// RED test (TDD step 1): before the fix, crush_array calls stage.run
    /// TWICE unnecessarily on a large compactable array — once for
    /// lossless_candidate (line 796) and a second redundant time for
    /// lossless_uses_opaque (line 843).  After the fix the second call is
    /// eliminated and the compaction result is reused.
    ///
    /// Shape: unique-entity rows (no CCR store) → lossy path returns Skip
    /// (no rows dropped → `dropped_summary` is empty → the survivor-
    /// compaction branch inside crush_array_lossy does NOT fire).  Only the
    /// lossless_candidate call and the now-redundant lossless_uses_opaque call
    /// are in-scope.
    ///
    /// Before fix: 2 calls (lossless_candidate + lossless_uses_opaque).
    /// After fix:  1 call  (lossless_candidate only; result reused for opaque).
    #[test]
    fn crush_array_large_compactable_invokes_compaction_stage_exactly_once() {
        // 30 unique-entity rows (no CCR store → lossy skips, no drops →
        // survivor compaction doesn't fire).  Uniform tabular shape →
        // compacts well so lossless_candidate is not None.
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "user": format!("u_{i}"), "status": "ok"}))
            .collect();
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            lossless_min_savings_ratio: 0.0, // always accept lossless render
            ..Default::default()
        };
        let (crusher, counter) = crusher_with_counting_compaction(cfg);

        // Sanity: no CCR store on this crusher (survivor compaction guard).
        assert!(crusher.ccr_store.is_none());

        let result = crusher.crush_array(&items, "", 1.0);

        // Lossless wins → nothing dropped → survivor compaction (line 1058)
        // never runs.  Only the lossless_candidate + optional lossless_uses_opaque
        // calls count.
        assert!(
            result.compacted.is_some(),
            "lossless render must win in this test setup (strategy: {})",
            result.strategy_info
        );
        assert_eq!(result.items.len(), 30, "lossless drops nothing");

        let calls = counter.load(std::sync::atomic::Ordering::Relaxed);
        assert_eq!(
            calls, 1,
            "crush_array must invoke the compaction stage EXACTLY once on the \
             large-array hot path when lossless wins (got {calls} calls — the \
             redundant lossless_uses_opaque call must be eliminated)"
        );
    }

    /// Behavioral parity: lossless-wins case — the chosen render must be
    /// byte-identical before and after the fix. We capture output from the
    /// reference (standard) crusher and the counting crusher (same stage
    /// logic, just with the spy) to confirm the refactor does not alter the
    /// lossless output.
    #[test]
    fn crush_array_lossless_output_unchanged_after_dedup() {
        let items: Vec<Value> = (0..50)
            .map(|i| json!({"id": i, "status": "ok", "region": "us-east-1"}))
            .collect();
        let cfg = SmartCrusherConfig {
            routing_policy: RoutingPolicy::LosslessFirst,
            ..Default::default()
        };
        // Reference: normal crusher.
        let ref_crusher = SmartCrusher::new(cfg.clone());
        let ref_result = ref_crusher.crush_array(&items, "", 1.0);

        // Under test: counting spy (same compaction logic).
        let (spy_crusher, _counter) = crusher_with_counting_compaction(cfg);
        let spy_result = spy_crusher.crush_array(&items, "", 1.0);

        assert_eq!(
            ref_result.strategy_info, spy_result.strategy_info,
            "strategy_info must match"
        );
        assert_eq!(
            ref_result.compacted, spy_result.compacted,
            "compacted output must be byte-identical"
        );
        assert_eq!(
            ref_result.ccr_hash, spy_result.ccr_hash,
            "ccr_hash must match"
        );
        assert_eq!(
            ref_result.items.len(),
            spy_result.items.len(),
            "item count must match"
        );
    }

    /// Behavioral parity: lossy-wins case — when compaction savings are
    /// below threshold the lossy path fires; output must be unaffected.
    #[test]
    fn crush_array_lossy_output_unchanged_after_dedup() {
        let items: Vec<Value> = (0..50).map(|_| json!({"status": "ok"})).collect();
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy path
            ..Default::default()
        };
        let ref_crusher = SmartCrusher::new(cfg.clone());
        let ref_result = ref_crusher.crush_array(&items, "", 1.0);

        let (spy_crusher, _counter) = crusher_with_counting_compaction(cfg);
        let spy_result = spy_crusher.crush_array(&items, "", 1.0);

        assert_eq!(
            ref_result.strategy_info, spy_result.strategy_info,
            "strategy_info must match on lossy path"
        );
        assert_eq!(
            ref_result.ccr_hash, spy_result.ccr_hash,
            "ccr_hash must match on lossy path"
        );
        assert_eq!(
            ref_result.items.len(),
            spy_result.items.len(),
            "item count must match on lossy path"
        );
    }
}
