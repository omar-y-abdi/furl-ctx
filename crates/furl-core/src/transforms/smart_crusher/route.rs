//! Array routing `crush_array` / `crush_array_lossy`, the lossless byte-savings floors, the CCR-backed keep budget, and the
//! `MinTokens`/`LosslessFirst` arbitration (ARCH-4: split out of `the Rust module` as pure moves, zero behavior change).

use serde_json::{Number, Value};

use super::compaction::{ColumnEncoding, Compaction};
use super::config::RoutingPolicy;
use super::crusher::{CrushArrayResult, SmartCrusher};
use super::field_role::compute_exclude_set;
use super::persist::{ccr_sentinel_map, CcrWrite, PersistMode};
use super::types::{ArrayAnalysis, CompressionStrategy, DroppedRef};
use crate::transforms::adaptive_sizer::compute_optimal_k;

/// Result of the lossy-recoverable render attempt in [`SmartCrusher::crush_array_lossy`]. `pending_ccr_writes` carries the deferred store writes backing its markers ([`PersistMode::Collect`]);
/// the routing layer commits them IFF this render ships — a discarded candidate's writes are dropped with it, so the store never holds entries no surfaced marker names (P0-4).
enum LossyOutcome {
    Crushed {
        result: CrushArrayResult,
        pending_ccr_writes: Vec<CcrWrite>,
    },
    Skip(String),
}

/// Routing outcome of [`SmartCrusher::crush_array_routed`] (PERF-4).
pub(super) enum Routed {
    /// The array ships unchanged. Carries the strategy string (`"none:adaptive_at_limit"`
    /// / `"skip:<reason>"` / `"skip:lossless_only"` / `""` for a reason-less skip).
    Passthrough(String),
    /// A real render shipped: lossless table, survivor-compacted table,
    /// or lossy row-drop.
    Result(CrushArrayResult),
}

/// A lossless render that cleared its gates, BEFORE materialization (PERF-4): carries everything except the `items` clone, which is
/// deferred until the route decision actually picks this candidate — a discarded candidate never pays the full-array deep clone.
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
            dropped_refs: self.dropped_refs,
        }
    }
}

/// Render the raw array exactly as the walker ships a passthrough — element-wise through the python-safe writer, byte-identical to `python_safe_json_dumps(&Value::Array(items.to_vec()))`
/// without the deep clone (PERF-4 style). This is the token baseline the small-zone lossy candidate must strictly beat when no lossless candidate exists.
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
        dropped_refs: Vec::new(),
    }
}

impl SmartCrusher {
    /// Compress an array of dict items.
    pub fn crush_array(&self, items: &[Value], query_context: &str, bias: f64) -> CrushArrayResult {
        match self.crush_array_routed(items, query_context, bias, true) {
            // Public contract: a passthrough result mirrors the unchanged input in `items`. Internal callers use the enum directly and skip this clone (PERF-4).
            Routed::Passthrough(strategy_info) => passthrough_result(items, strategy_info),
            Routed::Result(result) => result,
        }
    }

    /// `persist=false` computes the same candidate and routing metadata without store writes. Callers may use
    /// only pointer-free lossless output; never surface hashes or dropped summaries that were not persisted.
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

        // Tier-1 boundary: the array fits inside the adaptive budget. this zone was LOSSLESS-OR-PASSTHROUGH only; EFF-3 adds the lossy-recoverable
        // candidate (CCR-store-backed, default MinTokens policy only) so small arrays — the COMMON case for real tool output — can offload too.
        if items.len() <= adaptive_k {
            return self.small_array_route(
                items,
                &item_strings,
                adaptive_k,
                query_context,
                persist,
            );
        }

        // ── Lossless candidate ── Run the compaction stage ONCE if present. The lossless render keeps every row (nothing
        // dropped); it is a valid candidate only when it actually compacted into a decoder-verifiable shape (COR-13: a flat `Table`
        let (lossless_candidate, lossless_uses_opaque) =
            if let Some(stage) = self.compaction.as_ref() {
                let (c, rendered) = stage.run(items);
                // Read `contains_opaque_ref` before `c` is potentially moved.
                let uses_opaque = c.contains_opaque_ref();
                // Strict mode tightens the lossless claim to "reconstructible from the visible output ALONE": an opaque-substituted render hides blob
                // bytes behind a `<<ccr:` pointer (recoverable, but a visible information reduction), so it is NOT a candidate under `lossless_only`.
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
                        // This render CAN carry opaque substitutions (decoder-verifiability excludes only Nested cells) — collect them typed (§4.2 R2).
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

        // ── Strict lossless-or-passthrough (`lossless_only`) ── The lossy-recoverable candidate is NEVER BUILT in this mode: no rows are dropped, no
        // `<<ccr:HASH>>` sentinel is minted, and no CCR store write happens (`crush_array_lossy` is not invoked, so there are no deferred writes to leak either).
        if self.config.lossless_only {
            return match lossless_candidate {
                Some(lossless) => Routed::Result(lossless.into_result(items)),
                None => Routed::Passthrough("skip:lossless_only".to_string()),
            };
        }

        // Build a CCR-backed row-drop candidate only when the full original remains recoverable. If lossless compaction
        // already uses opaque substitution, prefer it because it keeps each row's non-opaque fields visible.

        // P0-4: build the lossy candidate with its CCR store writes DEFERRED (collect-only). Committing at build time orphaned the blob + chunks + index in the store
        // whenever the routing below chose the lossless render — wasted COR-4-bounded capacity and misleading store stats under hashes no surfaced marker names.
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

        // Route between the recoverable renders ── When BOTH a lossless render and a lossy DROP render exist they are each 100% recoverable lossless shows every
        // row; lossy surfaces a `<<ccr:HASH>>` pointer to the CCR-stored originals. Under `MinTokens` (the default) ship the fewer-TOKEN render (bytes mislead
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
                    // The lossless candidate's render IS its final model-visible string — count it directly (PERF-4: no materialized result, no clone, same count).
                    let lossless_tokens = self.tokenizer.count_text(&lossless.rendered);
                    let lossy_tokens = self.render_token_count(&lossy);
                    // Lossy wins only when STRICTLY fewer tokens; ties (and lossless-fewer) → lossless: more rows visible at no extra token cost.
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
            // Lossless render valid but the array isn't droppable (Skip): ship lossless — it shows every
            // row losslessly. (A non- droppable array should never drop, and lossless never drops.)
            (Some(lossless), LossyOutcome::Skip(_)) => Routed::Result(lossless.into_result(items)),
            // Only the lossy DROP render is valid → ship it. Its recovery entries are committed on the
            // way out (same unconditional guarantee as before the deferral; only the timing moved).
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

    /// For small arrays, compare lossless and CCR-backed lossy candidates under `MinTokens`. Lossy requires a store, an
    /// actual drop, and fewer tokens than the alternative; strict/lossless-first policies retain passthrough semantics.
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

        // Strict lossless-or-passthrough: the lossy candidate is NEVER BUILT (no drops, no markers, no store
        // writes) — same rule as the big-array path, same passthrough strategy string this zone always used.
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
            // `!lossless_uses_opaque` mirrors the big path: when the compactor wants opaque-blob
            // substitution for this array (a render the small zone REJECTS as a candidate.
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
                // Lossy wins only when STRICTLY fewer tokens; ties (and lossless-fewer) → lossless:
                // more rows visible at no extra token cost. Same rule as the big-array race.
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
                // No lossless candidate: the alternative is the RAW passthrough, so the drop render must
                // strictly beat THAT — near the keep floor a sentinel can cost more than the rows it hides.
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

    /// Small-array lossless compaction requires decoder-verifiable flat tables, no opaque substitutions,
    /// a minimum absolute byte saving, and the normal savings-ratio gate. Otherwise return passthrough.
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
            // The `!contains_opaque_ref` gate above means this collects nothing today; collecting
            // anyway keeps the typed carrier correct by construction if the gate ever changes.
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

    /// Build the lossy-recoverable render of `items` (row-drop + CCR sentinel). there is no DROP render in that case. Factored
    /// out of `crush_array` so the routing layer can size this candidate against the lossless one before deciding which to ship.
    fn crush_array_lossy(
        &self,
        items: &[Value],
        query_context: &str,
        item_strings: &[String],
        adaptive_k: usize,
        // When false, the entropy-floor crushability override stands down (a better-suited lossless render
        // — e.g. opaque-blob substitution — exists for this array, so we must not hijack it into a drop).
        allow_skip_override: bool,
        // How the drop's store writes are handled (hash + markers are computed identically in every mode — routing stays byte-identical).
        // `Collect` defers them into the returned outcome for commit-on-ship (P0-4); `Skip` (COR-28, mixed dict arm) never writes.
        persist_mode: PersistMode,
    ) -> LossyOutcome {
        // CCR-BACKED AGGRESSIVE BUDGET when a CCR store is configured, every dropped row is guaranteed recoverable (unconditional persist + surfaced `<<ccr:HASH>>`
        // pointer the invariant the adversarial loop locked). errors, outliers, anomalies, query-relevant rows (all pinned beyond budget by `prioritize_indices`)
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

        // With CCR backing, override only no-signal crushability skips: distinct sampleable rows remain recoverable,
        // so random anomaly presence must not decide whether a candidate exists. Structural skips remain fail-closed.
        if analysis.recommended_strategy == CompressionStrategy::Skip
            && allow_skip_override
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

        // Crushability gate: not safe to crush → no DROP candidate.
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

        // When identity-only variants collapse under the stable-projection hash, annotate the
        // kept representative with `_dup_count`; emit it only when rows were actually dropped.
        if dropped_count > 0 {
            let exclude = compute_exclude_set(&analysis.field_stats, items);
            if !exclude.is_empty() {
                annotate_dup_counts(&mut result, items, &exclude);
            }
        }

        // CCR persistence + marker emission. **The store write is the cornerstone of CCR's no-data-loss guarantee:** whenever rows are dropped
        // we hash the full original and stash it in the configured store so a dropped needle is *always* recoverable — never silently lost.
        let (ccr_hash, dropped_summary, row_drop_refs, pending_ccr_writes) =
            match self.persist_dropped(items, dropped_count, persist_mode) {
                Some(persisted) => {
                    // The typed carrier for this drop — same hash the
                    // sentinel advertises (§4.2).
                    let refs = vec![persisted.dropped_ref()];
                    (
                        Some(persisted.hash),
                        persisted.marker,
                        refs,
                        persisted.pending_writes,
                    )
                }
                None => (None, String::new(), Vec::new(), Vec::new()),
            };

        // Survivor compaction this step only decides how those rows are RENDERED. When the compaction stage can render the survivors as a CSV-schema table
        // that is meaningfully smaller than the JSON array form, ship that rendering with the `{"_ccr_dropped": ...}` sentinel appended as a final line.
        if !dropped_summary.is_empty() {
            if let Some(stage) = &self.compaction {
                let (mut c, _) = stage.run(&result);
                // F4: the survivor compaction only saw the KEPT rows, so its `original_count` is the survivor count. Restore the TRUE original total (`items.len()`)
                // so the inline `[kept/total]` header lets a consumer recover the original row count from the text itself, not only from the offload marker.
                set_table_original_count(&mut c, items.len());
                // review F1b: the survivor render's per-column constant folds and dictionary encodings are computed over the SURVIVOR
                // subset only. Left alone they would assert, over the whole array, a fact that only holds for the shown rows.
                let _ = demote_subset_only_encodings(&mut c, items);
                // The header now carries the true total, so render from the
                // adjusted compaction.
                let rendered = stage.formatter.format(&c);
                if c.is_decoder_verifiable() && !c.contains_opaque_ref() {
                    let sentinel = ccr_sentinel_map(&dropped_summary);
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
                        // Appended AFTER the survivor-vs-JSON savings-floor gate (`compact_len` above excludes it) so it never changes THAT gate's decision. It IS part
                        // of the final shipped render, though so its token weight DOES count in the outer lossy-vs-lossless `MinTokens` race (via `render_token_count`)
                        let table_body = rendered.trim_end_matches('\n');
                        let rendered_with_sentinel = match numeric_stats_line(&c, items) {
                            Some(stats_line) => {
                                format!("{table_body}\n{stats_line}\n{sentinel_line}")
                            }
                            None => format!("{table_body}\n{sentinel_line}"),
                        };
                        // Survivor renders are gated opaque-free (`!contains_opaque_ref` above) so this collects nothing today — kept for correctness under gate changes.
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
                dropped_refs: row_drop_refs,
            },
            pending_ccr_writes,
        }
    }

    /// Count the tokens of the FINAL model-visible string a `CrushArrayResult` renders to — the exact text `process_value` substitutes
    /// for this array. Used by the `MinTokens` routing policy to size the lossless vs lossy-recoverable candidates against each other.
    fn render_token_count(&self, result: &CrushArrayResult) -> usize {
        let rendered = self.render_result_string(result);
        self.tokenizer.count_text(&rendered)
    }

    /// Render a `CrushArrayResult` to the string `process_value` substitutes for the array (see [`SmartCrusher::render_token_count`]). Composes the `[item0,item1,...,sentinel?]` render element-wise — byte-identical
    /// to `python_safe_json_dumps(&Value::Array(...))` (the serializer is context-free; `,` separators are appended here) — without deep-cloning the items into a temporary array just to count tokens (PERF-4).
    fn render_result_string(&self, result: &CrushArrayResult) -> String {
        use crate::util::pyjson::write_python_safe_json;

        if let Some(s) = &result.compacted {
            return s.clone();
        }
        let sentinel = if result.dropped_summary.is_empty() {
            None
        } else {
            Some(Value::Object(ccr_sentinel_map(&result.dropped_summary)))
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

/// Drives the T5 display fix: an identity column is safe to SHOW on the representative
/// only when every object member of the collapsed family carries it with the SAME value.
struct ColVariance {
    /// First value seen for the column, or `None` until a member carries it.
    first: Option<Value>,
    /// A later member carried a different value.
    varies: bool,
    /// Object members of the family that carried the column.
    present: usize,
}

impl ColVariance {
    fn new() -> Self {
        Self {
            first: None,
            varies: false,
            present: 0,
        }
    }

    /// Fold one member's value for this column into the tally.
    fn observe(&mut self, value: &Value) {
        self.present += 1;
        match &self.first {
            None => self.first = Some(value.clone()),
            Some(seen) if seen != value => self.varies = true,
            Some(_) => {}
        }
    }

    /// The column holds one identical value on EVERY object member of the family, so displaying that concrete
    /// value on the representative is truthful (a genuine duplicate) rather than a fabricated recurrence.
    fn is_uniform(&self, family_object_members: usize) -> bool {
        self.first.is_some() && !self.varies && self.present == family_object_members
    }
}

/// Annotate only the first kept representative of each stable-hash duplicate family with `_dup_count`. Replace
/// identity fields that vary across the family with `<varies>` so one concrete ID is not misrepresented as repeated.
fn annotate_dup_counts(
    kept: &mut [Value],
    all_items: &[Value],
    exclude: &std::collections::BTreeSet<String>,
) {
    use crate::transforms::anchor_selector::stable_item_hash;
    use std::collections::HashMap;

    /// Sentinel shown for an identity column whose value is NOT constant
    /// across its collapsed family (see the function doc; T5).
    const VARIES_SENTINEL: &str = "<varies>";

    // One pass over the WHOLE original array builds, per family hash: * `family_size`.
    let mut family_size: HashMap<String, usize> = HashMap::new();
    let mut object_members: HashMap<String, usize> = HashMap::new();
    let mut col_variance: HashMap<String, HashMap<String, ColVariance>> = HashMap::new();

    for item in all_items {
        if let Some(obj) = item.as_object() {
            let h = stable_item_hash(item, exclude);
            *family_size.entry(h.clone()).or_insert(0) += 1;
            *object_members.entry(h.clone()).or_insert(0) += 1;
            let cols = col_variance.entry(h).or_default();
            for name in exclude {
                if let Some(value) = obj.get(name.as_str()) {
                    cols.entry(name.clone())
                        .or_insert_with(ColVariance::new)
                        .observe(value);
                }
            }
        } else if item.is_array() {
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
        if count <= 1 || !stamped.insert(h.clone()) {
            continue;
        }
        let members = object_members.get(&h).copied().unwrap_or(0);
        let family_cols = col_variance.get(&h);
        if let Some(obj) = row.as_object_mut() {
            // Blank every excluded identity column that VARIES across the family; a column
            // constant across the family keeps its value (genuine-duplicate preservation). T5.
            for name in exclude {
                if !obj.contains_key(name.as_str()) {
                    continue;
                }
                let uniform = family_cols
                    .and_then(|cols| cols.get(name))
                    .is_some_and(|cv| cv.is_uniform(members));
                if !uniform {
                    obj.insert(name.clone(), Value::from(VARIES_SENTINEL));
                }
            }
            // Record the family size. Don't clobber a real `_dup_count`
            // field the caller already had (extremely unlikely; defensive).
            obj.entry("_dup_count")
                .or_insert_with(|| Value::from(count));
        }
    }
}

/// Minimum ABSOLUTE byte saving required before a small array (`len <= adaptive_k`, the tier-1
/// passthrough zone) ships the lossless compacted rendering instead of passing through.
const SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES: usize = 256;

/// Whether an absolute byte saving clears the small-array lossless floor (`>= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES`, inclusive).
#[inline]
fn clears_small_array_lossless_floor(saved: usize) -> bool {
    saved >= SMALL_ARRAY_LOSSLESS_MIN_SAVED_BYTES
}

/// Divisor applied to `adaptive_k` for the lossy keep budget when a CCR store guarantees recovery of every dropped row.
const CCR_BACKED_KEEP_DIVISOR: usize = 2;

/// Floor for the CCR-backed keep budget. `min_items_to_analyze` (5) is the engine's own
/// notion of "too small to even analyze" — the visible sample never shrinks below it.
const CCR_BACKED_KEEP_FLOOR: usize = 5;

/// Minimum ABSOLUTE byte saving required before the lossy path ships its survivors as a CSV-schema rendering instead of a JSON array.
const LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES: usize = 64;

/// Whether an absolute byte saving clears the lossy-survivor render floor (`>= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES`, inclusive). Extracted for
/// the same reason as the small-array floor helper: the inclusive boundary (63/64/65) is unit-testable here without a pipeline-byte-exact fixture.
#[inline]
fn clears_lossy_survivor_floor(saved: usize) -> bool {
    saved >= LOSSY_SURVIVOR_RENDER_MIN_SAVED_BYTES
}

/// Only typed no-signal skip reasons are eligible for the CCR override. Structural skips remain
/// ineligible, and new skip variants default to fail-closed through the exhaustive typed match.
fn skip_reason_is_no_signal(analysis: &ArrayAnalysis) -> bool {
    analysis
        .crushability
        .as_ref()
        .is_some_and(|c| c.reason.is_no_signal())
}

/// Lossy keep budget when every dropped row is CCR-recoverable. `adaptive_k / 2`, floored at [`CCR_BACKED_KEEP_FLOOR`], never above `adaptive_k` itself.
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

/// The survivor render compacts only the KEPT rows, so the IR count is the survivor count; the row-drop path
/// knows the real total (`items.len()`) and restores it so the inline `[kept/total]` header carries it.
fn set_table_original_count(c: &mut Compaction, total: usize) {
    if let Compaction::Table { original_count, .. } = c {
        *original_count = total;
    }
}

/// Emit `__stats:col=min/max/sum/count` for non-constant numeric columns over all original rows. Skip
/// universal constants because the header value plus total row count already determines their aggregates.
fn numeric_stats_line(c: &Compaction, items: &[Value]) -> Option<String> {
    let schema = match c {
        Compaction::Table { schema, .. } => schema,
        _ => return None,
    };
    let mut segments: Vec<String> = Vec::new();
    for f in &schema.fields {
        if f.type_tag != "int" && f.type_tag != "float" {
            continue;
        }
        // Dead-weight trim: a constant-folded column already carries `=V` in the header, so its min/max/sum/count
        // are all derivable from V and the original total — emitting a stats segment for it is pure waste.
        if f.const_value.is_some() {
            continue;
        }
        if let Some(stats) = column_numeric_stats(items, &f.name) {
            segments.push(format!("{}={stats}", f.name));
        }
    }
    if segments.is_empty() {
        None
    } else {
        Some(format!("__stats:{}", segments.join(",")))
    }
}

/// Accumulate `min/max/sum/count` over every numeric value at column `name` across the FULL `items`
/// array. Integer columns sum exactly via `i128`; a float anywhere switches the sum to `f64`.
fn column_numeric_stats(items: &[Value], name: &str) -> Option<String> {
    let mut count: u64 = 0;
    let mut min: Option<Number> = None;
    let mut max: Option<Number> = None;
    let mut min_f = f64::INFINITY;
    let mut max_f = f64::NEG_INFINITY;
    let mut int_sum: i128 = 0;
    let mut float_sum: f64 = 0.0;
    let mut all_int = true;
    for item in items {
        let Some(Value::Number(n)) = resolve_flattened(item, name) else {
            continue;
        };
        let Some(x) = n.as_f64() else { continue };
        count += 1;
        if x < min_f {
            min_f = x;
            min = Some(n.clone());
        }
        if x > max_f {
            max_f = x;
            max = Some(n.clone());
        }
        match n.as_i64() {
            Some(i) => int_sum += i128::from(i),
            None => {
                all_int = false;
                float_sum += x;
            }
        }
    }
    if count == 0 {
        return None;
    }
    let sum = if all_int {
        int_sum.to_string()
    } else {
        (int_sum as f64 + float_sum).to_string()
    };
    let min_s = min.map(|n| n.to_string()).unwrap_or_default();
    let max_s = max.map(|n| n.to_string()).unwrap_or_default();
    Some(format!("{min_s}/{max_s}/{sum}/{count}"))
}

#[cfg(test)]
mod numeric_stats_tests {
    use super::*;

    #[test]
    fn column_numeric_stats_min_max_sum_count_and_dotted() {
        let items: Vec<Value> = (0..5)
            .map(|i| serde_json::json!({"n": i, "nested": {"deep": i * 10}, "s": "x"}))
            .collect();
        // Int column 0..4 → min/max/sum/count.
        assert_eq!(
            column_numeric_stats(&items, "n").as_deref(),
            Some("0/4/10/5")
        );
        // Dotted (flattened) path resolves the real nested value: 0,10,20,30,40.
        assert_eq!(
            column_numeric_stats(&items, "nested.deep").as_deref(),
            Some("0/40/100/5")
        );
        // Non-numeric and absent columns contribute no stats.
        assert_eq!(column_numeric_stats(&items, "s"), None);
        assert_eq!(column_numeric_stats(&items, "missing"), None);
    }

    #[test]
    fn numeric_stats_line_skips_constant_columns() {
        use super::super::compaction::ir::{CellValue, FieldSpec, Row, Schema};
        use serde_json::json;

        let field = |name: &str, tag: &str, konst: Option<Value>| FieldSpec {
            name: name.into(),
            type_tag: tag.into(),
            nullable: false,
            const_value: konst,
            encoding: None,
        };
        // The stats line should include only varying numeric column `v`; constant `k=64` is derivable from the header and row count, while `s` is non-numeric.
        let items: Vec<Value> = (0..3)
            .map(|i| json!({"k": 64, "v": (i + 1) * 10, "s": "x"}))
            .collect();
        let table = Compaction::Table {
            schema: Schema {
                fields: vec![
                    field("k", "int", Some(json!(64))),
                    field("v", "int", None),
                    field("s", "string", None),
                ],
            },
            rows: vec![Row::new(vec![
                CellValue::Scalar(json!(64)),
                CellValue::Scalar(json!(10)),
                CellValue::Scalar(json!("x")),
            ])],
            original_count: 3,
        };
        let line =
            numeric_stats_line(&table, &items).expect("a non-constant numeric column remains");
        assert_eq!(line, "__stats:v=10/30/60/3");
        assert!(
            !line.contains("k="),
            "constant column must be omitted: {line}"
        );

        // 2b: a table whose ONLY numeric column is constant yields NO stats line.
        let all_const = Compaction::Table {
            schema: Schema {
                fields: vec![
                    field("k", "int", Some(json!(64))),
                    field("s", "string", None),
                ],
            },
            rows: vec![Row::new(vec![
                CellValue::Scalar(json!(64)),
                CellValue::Scalar(json!("x")),
            ])],
            original_count: 3,
        };
        assert_eq!(numeric_stats_line(&all_const, &items), None);
    }
}

/// Before survivor rendering, clear subset-only constant/dictionary/affix/head encodings unless they hold over
/// every original row. Positional encodings remain; demotion is lossless because survivor cells are still present.
fn demote_subset_only_encodings(c: &mut Compaction, all_rows: &[Value]) -> bool {
    use super::compaction::encodings::{encode_affix_cell, split_head};
    use std::collections::HashSet;

    let Compaction::Table { schema, .. } = c else {
        return false;
    };
    let mut changed = false;
    for spec in schema.fields.iter_mut() {
        // review RF2: a flattened nested column carries a DOTTED name (`meta.region`) whose value lives at `row["meta"]["region"]`, not under a literal `"meta.region"`
        // key — a plain `row.get(name)` was always None there, OVER-demoting genuinely-universal nested consts and UNDER-demoting subset-only nested dicts.
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

        // Category-claim encodings each assert the WHOLE column ranges over a fixed set stamped from the SURVIVOR subset, so on the
        // offload path any of them can present a subset fact as universal (review F1b/RF3). Each is cleared unless it holds over ALL rows.
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
                // `encode_affix_cell` is the exact strip-and-check the compactor proved the round-trip with: it returns `Some` iff the cell
                // carries both affixes without overlap, so an affix-free or overlap-length string fails here just as it failed to stamp.
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
                        // `split_head` is the compactor's own last-delimiter split; a cell without the
                        // delimiter, or whose head is not in the declared set, is not covered by the fold.
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

/// Otherwise the name is a flattening path (`parent.inner`) traversed segment by segment
/// through nested objects, exactly the structure the compactor folded into the dotted column.
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

/// Approximate byte size of `[v0, v1, ...]` JSON serialization, given each item's already-serialized
/// form. Adds 2 for outer brackets and 1 per inter-item comma. Used by the lossless savings-ratio check.
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
        // 8 rows whose columnar table DECISIVELY beats the lossy render only `filesystem` varies it
        // shipped lossless ONLY because the old `_ccr_rows` granular marker padded the lossy candidate.
        let c = crusher();
        let items: Vec<Value> = (0..8)
            .map(|i| {
                json!({
                    "filesystem": format!("/dev/disk1s{i}"),
                    "kilobytes_total": 971350180,
                    "kilobytes_used": 543210,
                    "capacity_percent": "85%",
                    "mounted_on": "/Volumes/Data",
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

        // A survivor render (kept rows all `payout_request`, status all `ok`) whose header would assert those as universal + a genuinely-constant `region`.
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

        // A survivor render of a FLATTENED nested object: the compactor split `meta` into dotted columns
        // `meta.region` (const "us") and `meta.status` (dict ["ok"]) because the kept rows all shared them.
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

        // The FULL array is UN-flattened: values live under a nested `meta`. region is genuinely universal ("us"); status also takes "fail" in an offloaded row.
        let all_rows = vec![
            json!({"meta": {"region": "us", "status": "ok"}}),
            json!({"meta": {"region": "us", "status": "fail"}}),
        ];

        assert!(demote_subset_only_encodings(&mut c, &all_rows));
        let Compaction::Table { schema, .. } = &c else {
            panic!("still a table");
        };
        let field = |n: &str| schema.fields.iter().find(|f| f.name == n).unwrap();
        // Pre-fix a dotted-name lookup was always None, so a universal nested const was OVER-demoted
        // and a subset-only nested dict UNDER-demoted; resolve_flattened fixes BOTH directions.
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

        // Survivor render whose header would claim, from the kept rows alone: a shared endpoint
        // prefix, a shared token affix, an `/api/` route head, and a `logs/` dir head.
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

        // The FULL array: an offloaded `/api/purchase/9` breaks the endpoint prefix and a `/web/health`
        // breaks the `/api/` route head; the token affix and the `logs/` dir head hold over every row.
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

    // The two tests above pin the SMALL_ARRAY_LOSSLESS floor DIRECTIONALLY (well-above ships lossless,
    // well-below stays passthrough). The two below pin the EXACT inclusive boundary of each absolute-saved gate.
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
        // A small array whose cells would be CCR-substituted (file contents!) must NOT take
        // the small-array lossless path — the model needs those values visible verbatim.
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

    /// The COR-13 nested-cell fixture: long constant columns clear both byte-savings
    /// gates, so ONLY the decoder-coverage gate keeps the shape out of the lossless tier.
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
        // COR-13 fail-closed: an array-of-objects cell becomes `CellValue::Nested`, whose CSV-quoted IR-JSON rendering the reference decoder
        // cannot invert — the small-array lossless zone must DECLINE it (verbatim passthrough), never ship it as "lossless"-verified.
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
        // Same COR-13 shape WITH a store: the EFF-3 small-zone lossy candidate may legitimately drop rows (recoverably!), but the lossless claim
        // must still be declined — no `lossless:*` strategy and no compacted render (nested cells also fail the survivor-render decoder gate).
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
        // COR-13 fail-closed: a heterogeneous array with a clean string discriminator compacts to `Compaction::Buckets`,
        // whose `__buckets:` grammar the reference decoder cannot decode — the lossless accept gates must DECLINE it.
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
        // No compaction stage → no lossless candidate can exist, so the small zone routes lossy-vs-passthrough only.
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

    // EFF-3: small-array lossy-recoverable candidate ---------- The tier-1 zone (`items.len() <= adaptive_k`) shipped lossless-or-passthrough ONLY — small
    // arrays never produced a lossy-recoverable candidate, capping disk@9-style tool output at the lossless ceiling (~50%) while size-90 twins reached 91%+.

    /// One high-entropy small-zone row: distinct at BOTH ends and hex-dominated in the middle, defeating every lossless fold
    /// (constant/arith/iso/decimal/dict/head-dict/affix). Heavy enough (~60 tokens/row) that dropping one row decisively out-earns the ~40-token sentinel.
    fn high_entropy_small_row(i: usize) -> Value {
        // Full-width odd multipliers: every hex segment fills its width with a DISTINCT leading digit (no shared zero-padding
        // for the `__affix` fold to harvest — that fold alone once pushed this fixture over the relaxed ratio gate).
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
        // 8 high-entropy rows: the lossless render can't fold genuine entropy (fails the 0.30 gate), so pre-EFF-3 this passed
        // through untouched. With every drop CCR-backed, the small zone now drops to the keep budget and wins the MinTokens race.
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
        // No granular `#rows` index is written — the store holds one
        // entry (the whole-blob) per drop.
        assert!(
            store.get(&format!("{h}#rows")).is_none(),
            "no granular row index is written"
        );
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
        // A deterministic query anchor ("req-7f3a", quoted) names exactly one row.
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
        // No CCR store → a small-zone drop would be UNRECOVERABLE, so the lossy candidate
        // must not exist: both high-entropy and low-uniqueness shapes pass through untouched.
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

        // Low-uniqueness twin: crushable WITHOUT the entropy-floor override — the store
        // gate alone must keep it whole (the lossless render may ship; that drops nothing).
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
        // 6 tiny rows: the drop candidate exists (6 > keep floor 5) but dropping ONE ~5-token
        // row cannot pay for the ~40-token sentinel — MinTokens must decline it and pass through.
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
        // Constant-heavy tabular rows: the lossless render folds the heavy constant columns once and ditto-marks
        // the rest, beating the drop render (which pays a ~40-token sentinel to hide 3 low-residue rows).
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
        // 30 unique dict items with ID-like fields → the analyzer's crushability gate labels this `unique_entities_no_signal`. so the entropy-floor override
        // re-derives a real strategy and crushes DETERMINISTIC and aggressive, with every dropped row recoverable via the surfaced `<<ccr:HASH>>` pointer.
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
        // Same near-unique no-signal shape, but NO CCR store: a drop here would be UNRECOVERABLE, so the
        // override must NOT fire — the analyzer's skip stands (legacy / parity mode, zero silent loss).
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
        // COR-33 (representative half): when SEVERAL members of the same stable-projection family
        // stay visible, only the FIRST kept member (the representative) may carry `_dup_count`.
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
        // COR-33 (no-drop half): `_dup_count` exists to record rows the plan COLLAPSED.
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

    // ---------- T5: varying-identity blanking on the representative ----------

    #[test]
    fn dup_count_representative_blanks_varying_identity_columns_e2e() {
        // T5 (furl-ctx pre-mortem audit): when rows collapse under the stable-projection hash because they differ ONLY in VaryingIdentity columns (hex id / ISO
        // timestamp / monotone counter) while their content is constant, the kept REPRESENTATIVE must NOT keep row-0's concrete identity values beside `_dup_count:N`.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        //
        let items: Vec<Value> = (0..120)
            .map(|i| {
                let (event, detail) = if i < 60 {
                    ("heartbeat", "ok".to_string())
                } else {
                    ("request", format!("served shard {}", i % 7))
                };
                json!({
                    "req_id": format!("{i:040x}"),
                    "ts": format!("2026-06-12T10:{:02}:{:02}Z", i / 60, i % 60),
                    "seq": i,
                    "host": format!("web-{}", i % 3),
                    "event": event,
                    "detail": detail,
                    "status": "ok",
                })
            })
            .collect();
        let result = c.crush_array(&items, "", 1.0);

        let reps: Vec<&Value> = result
            .items
            .iter()
            .filter(|r| r.is_object() && r.get("_dup_count").is_some())
            .collect();
        assert!(
            !reps.is_empty(),
            "T5 precondition: the collapse-and-stamp path must fire \
             (strategy={}); items={:#?}",
            result.strategy_info,
            result.items
        );
        for rep in &reps {
            let dup = rep
                .get("_dup_count")
                .and_then(|v| v.as_u64())
                .expect("_dup_count is a number");
            assert!(dup > 1, "a stamped representative records N>1: {rep:?}");
            for col in ["req_id", "ts", "seq"] {
                assert_eq!(
                    rep.get(col),
                    Some(&json!("<varies>")),
                    "identity column `{col}` on a stamped representative must be \
                     `<varies>`, not a concrete value implying it recurred {dup} \
                     times: {rep:#?}"
                );
            }
            assert_eq!(
                rep.get("status"),
                Some(&json!("ok")),
                "constant content stays verbatim — only identity display changes: {rep:?}"
            );
        }
    }

    #[test]
    fn annotate_dup_counts_blanks_only_columns_that_vary_within_the_family() {
        // The blanking is SELECTIVE. Within a collapsed family an excluded column is replaced
        // with `<varies>` ONLY when its value actually differs across the family's members.
        let all: Vec<Value> = vec![
            json!({"req_id": format!("{:040x}", 1), "region": "us", "op": "login"}),
            json!({"req_id": format!("{:040x}", 2), "region": "us", "op": "login"}),
            json!({"req_id": format!("{:040x}", 9), "region": "eu", "op": "logout"}),
        ];
        let mut kept = vec![all[0].clone()];
        let exclude: std::collections::BTreeSet<String> =
            ["req_id".to_string(), "region".to_string()]
                .into_iter()
                .collect();
        annotate_dup_counts(&mut kept, &all, &exclude);
        let rep = kept[0].as_object().expect("representative stays an object");
        assert_eq!(
            rep.get("_dup_count"),
            Some(&json!(2)),
            "the `login` family has two members"
        );
        assert_eq!(
            rep.get("req_id"),
            Some(&json!("<varies>")),
            "req_id differs across the family -> `<varies>`"
        );
        assert_eq!(
            rep.get("region"),
            Some(&json!("us")),
            "region is constant across the family -> kept verbatim (genuine-duplicate preservation)"
        );
        assert_eq!(
            rep.get("op"),
            Some(&json!("login")),
            "content field stays verbatim"
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
        // 50 uniform tabular dicts → CSV+schema compaction shrinks the input well above the 0.30 gate so the LOSSLESS render is
        // a valid candidate. Under `LosslessFirst` it MUST ship (all rows visible, nothing dropped) whenever it clears the gate
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
        // Force the threshold high enough that even tabular savings can't satisfy it → lossy path runs → CCR-Dropped fires.
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
        assert_eq!(h.len(), 24);
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
        // The CCR-Dropped restoration applies regardless of whether lossless was attempted — without_compaction also gets the ccr_hash on row drops.
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
        // Defect 1 (kill silent loss, completed). With `advertise_retrieval_tool=false` the engine STILL surfaces the `<<ccr:HASH>>` recovery pointer in
        // `dropped_summary` AND writes the store. `dropped_summary.is_empty()` — which encoded exactly the silent loss the recovery invariant forbids on the public path.
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
        // `lossy_falls_through_when_savings_below_threshold`): low uniqueness, lossless gate forced unreachable.
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
        // Cleanly tabular input still compacts LOSSLESSLY in strict mode — the mode forbids lossy candidates,
        // not the verified lossless tier. The render must carry every row and no `<<ccr:` pointer.
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
        // Long base64 columns normally make the compactor substitute cells with `<<ccr:HASH,base64,SIZE>>` (opaque refs) — a
        // recoverable render that still hides visible bytes. Strict mode must neither ship such a render NOR write the store.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(64);
        let items: Vec<Value> = (0..30)
            .map(|i| json!({"path": format!("src/f{i}.py"), "content": blob.clone()}))
            .collect();

        // Counterfactual precondition: under the DEFAULT config this very fixture ships an opaque-substituted render (pointers
        // in the output) — proving the strict-mode decline below is the work of the new gates, not an accident of the fixture.
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
        // With substitution off, the CONSTANT blob column folds into the declaration (`content:string=<blob>` — verbatim, exactly once): a legitimately PURE lossless
        // render that may still win the savings gate. Whichever way the gate lands, the strict-mode output must carry the blob bytes verbatim and no pointer.
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
        // DISTINCT per-row blobs (distinct at BOTH ends, so neither the constant fold nor the affix/head-dict encoders apply):
        // the blobs-verbatim render cannot clear the savings gate, and the opaque-substituted render is disabled in strict mode.
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
        // Belt-and-braces layer: the ROUTING gate (`opaque_ok` in `crush_array_inner`) must decline an opaque-bearing render
        // even when a hand-composed crusher pairs `lossless_only` with a stage that still substitutes (e.g. via `from_parts`.
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
        // With a CCR store every dropped row is recoverable, so the lossy keep budget halves; without a store the
        // legacy full `adaptive_k` budget applies (a tighter budget there would drop unrecoverable rows for nothing).
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

    /// One realistic git-log-shaped row: identity columns (40-hex commit, ISO date), a low-cardinality
    /// author, and a genuinely varied unique subject built from rotating conventional-commit vocabulary.
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
        // When the lossy path drops rows AND the survivors render as a smaller CSV-schema table, the
        // output is the rendering with the `{"_ccr_dropped": ...}` sentinel appended as the final line.
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
        // High-entropy distinct rows (git-log shaped): hex/ISO identity columns, repeating author, genuinely varied unique subjects (uniformly-templated
        // subjects trip the `skip:unique_entities_no_signal` crushability gate and never reach the lossy path — mirroring how real logs behave).
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

    /// Build a default-config crusher (MinTokens) plus a LosslessFirst twin, both sharing one in-memory
    /// CCR store, so a routing test can compare the two policies and still recover any dropped rows.
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
        // Logs-shaped: per-row entropy (40-hex commit + distinct subject) shipped 90× makes the lossless render
        // token-expensive; dropping to a small visible sample + a `<<ccr:HASH>>` sentinel is far fewer tokens.
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
        // A low-cardinality tabular array whose every row collapses under dedup: the lossy path
        // keeps the same content the lossless table shows, so the lossless render is ≤ tokens.
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
        // The core invariant: under MinTokens the shipped render is never MORE tokens than the
        // lossless render would have been — for any droppable array where both candidates exist.
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

    // P0-4 When the LOSSLESS render won and shipped, those writes stayed behind as orphans
    // entries no surfaced marker names, burning COR-4-bounded capacity and inflating store stats.

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
        // Low-uniqueness (analyzer willing to crush → a real lossy DROP candidate is built) AND cleanly
        // tabular (lossless clears the 0.30 gate) → both candidates exist; LosslessFirst ships lossless.
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
        // The pinned MinTokens lossless-win shape (see `min_tokens_ships_lossless_when_it_is_fewer_tokens`): identical
        // low-cardinality rows dedup so hard that the lossless table is ≤ tokens vs the drop render — ties go to lossless.
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
        // The P0-4 deferral must NOT weaken the recovery invariant: when the lossy render SHIPS out of the arbitration arm (both candidates existed,
        // lossy strictly fewer tokens), its store writes are committed exactly as before. Only the write TIMING moved (build → ship decision).
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
        // A row-drop commits EXACTLY ONE store entry — the whole-blob. No
        // granular `#rows` index or per-row chunks are written.
        assert!(
            store.get(&format!("{h}#rows")).is_none(),
            "no granular row index is committed"
        );
        assert_eq!(
            store.len(),
            1,
            "the whole-blob is the only committed entry — nothing more"
        );
    }

    // ---------- U8: single compaction pass on large-array hot path ----------

    /// A [`Formatter`] spy that counts how many times `format` is called. Each call to `CompactionStage::run`
    /// calls `format` exactly once, so this is a direct proxy for the number of `stage.run(items)` calls.
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

    /// RED test (TDD step 1) once for lossless_candidate (line 796) and a second redundant time for lossless_uses_opaque (line 843). Only the
    /// lossless_candidate call and the now-redundant lossless_uses_opaque call are in-scope. Before fix: 2 calls (lossless_candidate + lossless_uses_opaque).
    #[test]
    fn crush_array_large_compactable_invokes_compaction_stage_exactly_once() {
        // 30 unique-entity rows (no CCR store → lossy skips, no drops → survivor compaction
        // doesn't fire). Uniform tabular shape → compacts well so lossless_candidate is not None.
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

        // Lossless wins → nothing dropped → survivor compaction (line 1058) never runs. Only the lossless_candidate + optional lossless_uses_opaque calls count.
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

    /// Behavioral parity: lossless-wins case — the chosen render must be byte-identical before and after the fix.
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
