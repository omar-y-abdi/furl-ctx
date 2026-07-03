//! Recursive JSON walk — `crush` / `smart_crush_content` /
//! `process_value` / `crush_mixed_array` and their `_collecting`
//! variants (ARCH-4: split out of `crusher.rs` as pure moves, zero
//! behavior change).
//!
//! Owns the descent over parsed JSON: array-type dispatch (dict arrays
//! route through `route::crush_array`; string/number/mixed arrays crush
//! in place with the CCR-Dropped sentinel appended via
//! `persist::ccr_dropped_sentinel_collecting`), object key-crush, the
//! walker-equivalent string handling (stringified-JSON recursion +
//! opaque-blob substitution), and the type-grouped mixed-array crusher.

use serde_json::Value;

use super::classifier::{classify_array, ArrayType};
use super::compaction::{
    classify_string, emit_opaque_ccr_marker, has_serde_private_marker, try_parse_json_container,
    CellClass, ClassifyConfig,
};
use super::crusher::{CrushArrayResult, SmartCrusher};
use super::crushers::{compute_k_split, crush_number_array, crush_object, crush_string_array};
use super::persist::ccr_sentinel_map;
use super::route::Routed;
use super::traits::CrushEvent;
use super::types::{CrushResult, DroppedRef};

impl SmartCrusher {
    /// Top-level entry point. Mirrors Python `SmartCrusher.crush`
    /// (line 1581-1603) — used by `ContentRouter` when routing JSON
    /// arrays.
    ///
    /// Parses `content` as JSON, recursively processes it (compressing
    /// arrays at every depth via the appropriate per-type crusher),
    /// then re-serializes with Python-compatible formatting (`, ` and
    /// `: ` separators, ASCII-escaped non-ASCII).
    ///
    /// Returns a `CrushResult` with:
    /// - `compressed`: the re-serialized JSON.
    /// - `original`: the input string (unmodified).
    /// - `was_modified`: whether `compressed` differs from `content`'s
    ///   trimmed form.
    /// - `strategy`: combined strategy info from all crushed arrays
    ///   (or `"passthrough"`).
    pub fn crush(&self, content: &str, query: &str, bias: f64) -> CrushResult {
        let start = std::time::Instant::now();
        // Collect the typed recovery refs alongside the rendered output.
        // `smart_crush_content_collecting` threads a per-call sink through
        // the recursive walk so EVERY reduction — row-drops and opaque
        // substitutions — surfaces here as a [`DroppedRef`], carrying the
        // values the emission sites already computed. The Python shim
        // mirrors these DIRECTLY into the compression_store, so `crush()`
        // recovery no longer depends on scraping `<<ccr:...>>` out of
        // `compressed`. The sink is pure side-output: it does not change
        // which sentinels/markers get embedded, so `compressed` is
        // byte-identical to the pre-typed-field behavior
        // (grammar-characterization + compression-floor untouched by
        // construction).
        let (compressed, was_modified, info, dropped) =
            self.smart_crush_content_collecting(content, query, bias);
        let strategy = if info.is_empty() {
            "passthrough".to_string()
        } else {
            info
        };

        // Fire one event per top-level crush. Cheap when no observers
        // are configured (`for o in &[]` is a single null-pointer
        // check); cheap when only `TracingObserver` is configured if
        // the subscriber filters `debug` out (the default in
        // production). Custom observers — audit logs, Loop training
        // stream, metrics — pay whatever they pay.
        if !self.observers.is_empty() {
            let event = CrushEvent {
                strategy: strategy.clone(),
                input_bytes: content.len(),
                output_bytes: compressed.len(),
                elapsed_ns: start.elapsed().as_nanos() as u64,
                was_modified,
            };
            for observer in &self.observers {
                observer.on_event(&event);
            }
        }

        CrushResult {
            compressed,
            original: content.to_string(),
            was_modified,
            strategy,
            dropped,
        }
    }

    /// `SmartCrusher._smart_crush_content` (Python line 2243-2301).
    /// JSON-parse, recursively process, re-serialize. CCR marker
    /// injection is stubbed (CCR is disabled in this stage).
    ///
    /// Returns `(crushed_content, was_modified, info)`.
    ///
    /// Public wrapper that discards the typed-ref sink. Deprecated in
    /// favor of [`smart_crush_content_typed`](Self::smart_crush_content_typed)
    /// (§4.2 R3/R4) — callers that mirror recovery need the refs; parity
    /// callers that only want the tuple keep this shape.
    pub fn smart_crush_content(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String) {
        let (result, was_modified, info, _dropped) =
            self.smart_crush_content_collecting(content, query_context, bias);
        (result, was_modified, info)
    }

    /// Typed sibling of [`smart_crush_content`](Self::smart_crush_content)
    /// (§4.2 R3/R4): identical first three tuple elements — byte-identical
    /// rendering — plus every [`DroppedRef`] the walk produced (row-drops
    /// AND opaque substitutions, in emission order) so the FFI can hand
    /// recovery to Python typed instead of via the text scrape.
    pub fn smart_crush_content_typed(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String, Vec<DroppedRef>) {
        self.smart_crush_content_collecting(content, query_context, bias)
    }

    /// Collecting variant of [`smart_crush_content`](Self::smart_crush_content):
    /// identical rendering, but also returns every recovery ref the
    /// recursive walk produced as a `Vec<DroppedRef>` (row-drops with
    /// their optional per-blob row-index data, plus opaque
    /// substitutions). Drives `crush()`'s typed-field recovery. The
    /// render path is unchanged — the sink is side-output — so `result`
    /// bytes match `smart_crush_content` exactly.
    fn smart_crush_content_collecting(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String, Vec<DroppedRef>) {
        // COR-44: decline magic-key payloads before calling from_str so
        // serde_json's arbitrary_precision / raw_value promotions never fire.
        // Passthrough identical to the non-JSON branch: original bytes,
        // was_modified=false, no info, no dropped refs.
        if has_serde_private_marker(content) {
            return (content.to_string(), false, String::new(), Vec::new());
        }
        // Parse — non-JSON content passes through unchanged.
        let Ok(parsed) = serde_json::from_str::<Value>(content) else {
            return (content.to_string(), false, String::new(), Vec::new());
        };

        let mut dropped: Vec<DroppedRef> = Vec::new();
        let (crushed, info) =
            self.process_value_collecting(&parsed, 0, query_context, bias, &mut dropped);

        // Re-serialize with Python `safe_json_dumps` formatting:
        // compact `(",", ":")` separators + `ensure_ascii=False`,
        // preserving object-key insertion order. Matches the Python
        // SmartCrusher output bytes exactly.
        let result = crate::util::pyjson::python_safe_json_dumps(&crushed);
        let was_modified = result != content.trim();
        (result, was_modified, info, dropped)
    }

    /// Maximum recursion depth for nested JSON. Mirrors Python's
    /// `_MAX_PROCESS_DEPTH = 50`. Beyond this, values are returned as-is.
    const MAX_PROCESS_DEPTH: usize = 50;

    /// Recursively process a value, crushing arrays where appropriate.
    /// Mirrors Python `_process_value` (line 2307-2398).
    ///
    /// Returns `(processed_value, info_string)`. CCR markers are
    /// stubbed (Python's tuple has a third element for them — Rust's
    /// version omits since we never produce markers in this stage).
    ///
    /// Public wrapper: allocates a throwaway sink and delegates to
    /// [`process_value_collecting`](Self::process_value_collecting). Keeps
    /// the established `(Value, String)` signature for every existing
    /// caller (tests, `process_string`); `crush()` uses the collecting
    /// variant to surface typed row-drop hashes.
    pub fn process_value(
        &self,
        value: &Value,
        depth: usize,
        query_context: &str,
        bias: f64,
    ) -> (Value, String) {
        let mut sink: Vec<DroppedRef> = Vec::new();
        self.process_value_collecting(value, depth, query_context, bias, &mut sink)
    }

    /// Collecting variant of [`process_value`](Self::process_value):
    /// identical processing and identical `(Value, String)` result, but
    /// every row-drop produced at any depth is appended to `dropped` as a
    /// [`DroppedRef`]. The sink is pure side-output — it never influences
    /// which sentinels are embedded into the returned `Value`, so the
    /// rendered bytes are byte-identical to `process_value`'s.
    fn process_value_collecting(
        &self,
        value: &Value,
        depth: usize,
        query_context: &str,
        bias: f64,
        dropped: &mut Vec<DroppedRef>,
    ) -> (Value, String) {
        if depth >= Self::MAX_PROCESS_DEPTH {
            return (value.clone(), String::new());
        }

        let mut info_parts: Vec<String> = Vec::new();

        match value {
            Value::Array(arr) => {
                let n = arr.len();
                if n >= self.config.min_items_to_analyze {
                    let arr_type = classify_array(arr);
                    // Strict lossless-or-passthrough: the non-dict crushers
                    // (string / number / mixed) are sampling drops with a
                    // `<<ccr:HASH>>` recovery sentinel — lossy-recoverable,
                    // so they never run under `lossless_only`. Their match
                    // guards below fail in that mode and the array falls
                    // through to plain recursive descent (nested content is
                    // still processed under the same strict rules). The
                    // DictArray arm stays unguarded: `crush_array` itself
                    // routes lossless-or-passthrough in this mode.
                    match arr_type {
                        ArrayType::DictArray => {
                            let result =
                                match self.crush_array_routed(arr, query_context, bias, true) {
                                    // Unchanged passthrough (skip / at-limit):
                                    // re-wrap our own borrow of the array. The
                                    // old path deep-cloned the items into a
                                    // `CrushArrayResult` that was immediately
                                    // re-wrapped here (PERF-4); output bytes
                                    // are identical — a passthrough's items
                                    // ARE the input.
                                    Routed::Passthrough(info) => {
                                        info_parts.push(format!("{}({}->{})", info, n, n));
                                        return (Value::Array(arr.clone()), info_parts.join(","));
                                    }
                                    Routed::Result(result) => result,
                                };
                            // Lossless path won → substitute the array
                            // with the compacted string in place. This
                            // makes the lossless win visible to the
                            // public `crush()` API: the output JSON
                            // has a string where the array used to be.
                            // The wrapping JSON structure is preserved.
                            if let Some(rendered) = result.compacted {
                                info_parts.push(format!(
                                    "{}({}->len={})",
                                    result.strategy_info,
                                    n,
                                    rendered.len()
                                ));
                                // The compacted render covers TWO cases: a
                                // PURE lossless win (nothing dropped —
                                // `dropped_refs` carries only whatever
                                // opaque substitutions the render bakes in)
                                // AND a LOSSY survivor-compacted drop
                                // (`smart_sample+compact:table` — rows
                                // dropped, the `<<ccr:HASH ...>>` sentinel
                                // baked into `rendered` as its last line,
                                // the row-drop ref in `dropped_refs`). Both
                                // are genuine reductions whose recovery the
                                // Python scrape would otherwise own —
                                // surface them typed, exactly like the
                                // JSON-array arm below.
                                dropped.extend(result.dropped_refs);
                                return (Value::String(rendered), info_parts.join(","));
                            }
                            info_parts.push(format!(
                                "{}({}->{})",
                                result.strategy_info,
                                n,
                                result.items.len()
                            ));
                            // Lossy path with rows dropped → append a
                            // CCR-Dropped sentinel object as the last
                            // element of the kept-items array. This is
                            // the **only** place the LLM sees the
                            // `<<ccr:HASH ...>>` pointer in the prompt.
                            // Without this, the store has the data but
                            // no model can ever ask for it.
                            //
                            // Sentinel shape: `{"_ccr_dropped":
                            // "<<ccr:HASH N_rows_offloaded>>"}` —
                            // preserves "array-of-objects" shape so
                            // downstream consumers iterating with
                            // `x.get(...)` keep working; the well-known
                            // `_ccr_dropped` key signals metadata
                            // unambiguously.
                            let mut items = result.items;
                            if !result.dropped_summary.is_empty() {
                                // The `_ccr_rows` marker names the per-blob
                                // row index so retrieval is proportional
                                // (resolve index, fetch only needed rows);
                                // `_ccr_dropped` keeps the byte-stable
                                // whole-blob pointer.
                                let sentinel = ccr_sentinel_map(
                                    &result.dropped_summary,
                                    result.row_index_marker.as_deref(),
                                );
                                items.push(Value::Object(sentinel));
                            }
                            // Surface the SAME hash + row-index data the
                            // sentinel advertises, typed for direct
                            // mirroring. `dropped_refs` carries the
                            // row-drop ref exactly when rows were dropped
                            // (`persist_dropped` returned `Some`), so this
                            // matches the sentinel condition above.
                            dropped.extend(result.dropped_refs);
                            return (Value::Array(items), info_parts.join(","));
                        }
                        ArrayType::StringArray if !self.config.lossless_only => {
                            let strs: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                            let (crushed, strategy) = crush_string_array(&strs, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed_values: Vec<Value> =
                                crushed.into_iter().map(Value::String).collect();
                            // 1A (non-dict path): persist the full original
                            // + append a CCR-Dropped sentinel whenever rows
                            // were dropped, so every distinct string is
                            // recoverable via `ccr_get(hash)` — never
                            // silently lost. Both the store write and the
                            // sentinel TEXT are unconditional (inside
                            // `persist_dropped`) — `enable_ccr_marker` gates
                            // NEITHER; it is only the router-layer
                            // retrieval-tool advertisement preference
                            // (pinned by `persist.rs`'s
                            // `non_dict_drop_surfaces_pointer_and_persists_even_with_marker_off`).
                            if let Some(sentinel) =
                                self.ccr_dropped_sentinel_collecting(arr, &crushed_values, dropped)
                            {
                                crushed_values.push(sentinel);
                            }
                            return (Value::Array(crushed_values), info_parts.join(","));
                        }
                        ArrayType::NumberArray if !self.config.lossless_only => {
                            let (crushed, strategy) = crush_number_array(arr, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): same guarantee as the
                            // string branch — persist + sentinel on drop.
                            if let Some(sentinel) =
                                self.ccr_dropped_sentinel_collecting(arr, &crushed, dropped)
                            {
                                crushed.push(sentinel);
                            }
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        ArrayType::MixedArray if !self.config.lossless_only => {
                            // Collecting variant: a dict subgroup's
                            // substituted lossless table can bake in
                            // opaque-cell markers — surface those typed
                            // through the same sink (§4.2 R2).
                            let (crushed, strategy) = self.crush_mixed_array_collecting(
                                arr,
                                query_context,
                                bias,
                                dropped,
                            );
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): the mixed crusher drops
                            // str/number subgroup items (and its own
                            // dropped_summary was previously discarded).
                            // Persist the full original + sentinel here so
                            // every dropped item across all subgroups is
                            // recoverable.
                            if let Some(sentinel) =
                                self.ccr_dropped_sentinel_collecting(arr, &crushed, dropped)
                            {
                                crushed.push(sentinel);
                            }
                            return (Value::Array(crushed), info_parts.join(","));
                        }
                        // NestedArray, BoolArray, Empty → fall through
                        // to recursive descent.
                        _ => {}
                    }
                }

                // Below threshold or not crushable → recurse into items.
                let mut processed: Vec<Value> = Vec::with_capacity(n);
                for item in arr {
                    let (p_item, p_info) = self.process_value_collecting(
                        item,
                        depth + 1,
                        query_context,
                        bias,
                        dropped,
                    );
                    processed.push(p_item);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }
                (Value::Array(processed), info_parts.join(","))
            }
            Value::Object(map) => {
                // First pass: recurse into values to compress nested arrays.
                let mut processed = serde_json::Map::new();
                for (k, v) in map {
                    let (p_val, p_info) =
                        self.process_value_collecting(v, depth + 1, query_context, bias, dropped);
                    processed.insert(k.clone(), p_val);
                    if !p_info.is_empty() {
                        info_parts.push(p_info);
                    }
                }

                // Second pass: if the object itself has many keys,
                // compress at the key level. Key-crush DROPS keys with no
                // recovery pointer at all, so it is doubly out under strict
                // lossless-or-passthrough.
                if processed.len() >= self.config.min_items_to_analyze && !self.config.lossless_only
                {
                    let (crushed_dict, strategy) = crush_object(&processed, &self.config, bias);
                    if strategy != "object:passthrough" {
                        info_parts.push(strategy);
                        return (Value::Object(crushed_dict), info_parts.join(","));
                    }
                }

                (Value::Object(processed), info_parts.join(","))
            }
            // Strings: walker-equivalent handling. Delegates to
            // `process_string` which parses stringified-JSON containers
            // (recursing through `process_value`) and CCR-substitutes
            // opaque blobs (with store-write so retrieval works). The
            // collecting variant threads the sink so BOTH a row-drop
            // INSIDE a stringified-JSON sub-array AND the opaque-blob
            // substitution itself surface typed (§4.2 R2 — this
            // deliberately overturns the earlier scrape-by-design
            // decision, per the owner mandate).
            Value::String(s) => {
                self.process_string_collecting(s, depth, query_context, bias, dropped)
            }
            // Other scalars — passthrough.
            _ => (value.clone(), String::new()),
        }
    }

    /// Walker-equivalent string handling. Mirrors `walker::walk_string`
    /// in `compaction/walker.rs` but lives on `SmartCrusher` so the
    /// public `crush()` path picks it up.
    ///
    /// Two cases:
    /// 1. **Stringified-JSON.** Strings that parse to a JSON object or
    ///    array → recurse via `process_value`, then re-emit the result
    ///    as a compact JSON string. The wrapping string is preserved
    ///    (so the parent JSON shape stays a string-typed field), but
    ///    its contents are processed end-to-end.
    /// 2. **Opaque blobs.** Strings classified as
    ///    [`CellClass::Opaque`] (long base64 / HTML / long-text) →
    ///    substitute with a `<<ccr:HASH,KIND,SIZE>>` marker. Same
    ///    format as `compaction::walker::format_ccr_marker` so
    ///    downstream consumers can pattern-match markers regardless
    ///    of which path emitted them.
    ///
    /// Row-drops produced while recursing into a stringified-JSON
    /// container are appended to `dropped`, and so is the opaque blob
    /// substitution itself (case 2) as a typed [`DroppedRef::Opaque`] —
    /// §4.2 R2. (Opaque recovery previously stayed on the Python scrape
    /// "by design"; that decision is deliberately overturned per the
    /// owner mandate — the typed ref carries the same hash/kind the
    /// marker renders, plus the exact byte size.)
    fn process_string_collecting(
        &self,
        s: &str,
        depth: usize,
        query_context: &str,
        bias: f64,
        dropped: &mut Vec<DroppedRef>,
    ) -> (Value, String) {
        // 1. Stringified-JSON: parse, recurse, re-render.
        if let Some(parsed) = try_parse_json_container(s) {
            let (processed, sub_info) =
                self.process_value_collecting(&parsed, depth + 1, query_context, bias, dropped);
            // If recursion produced something different, re-emit.
            // Special case: if the recursion returned a `Value::String`
            // (lossless compaction substituted the array with a
            // rendered CSV+schema string), use that string directly.
            // Re-encoding it as JSON would produce a quoted string
            // literal — double-encoded — which is not what callers
            // expect in the wrapping field.
            if processed != parsed {
                let rendered = match &processed {
                    Value::String(rendered_str) => rendered_str.clone(),
                    _ => serde_json::to_string(&processed).unwrap_or_else(|_| s.to_string()),
                };
                let info = if sub_info.is_empty() {
                    "string_json".to_string()
                } else {
                    format!("string_json[{sub_info}]")
                };
                return (Value::String(rendered), info);
            }
        }

        // 2. Opaque blob: substitute with CCR marker AND stash the
        // original in the store so retrieval works. Hash + format
        // identical to walker.rs via the shared helper — zero drift.
        // Substitution replaces visible bytes with a `<<ccr:` pointer —
        // recoverable, but a visible information reduction — so it is
        // disabled under strict `lossless_only` (the blob passes through
        // verbatim; no store write happens: nothing was hidden).
        if !self.config.lossless_only {
            let cfg = ClassifyConfig::default();
            // `classify_string` takes the borrowed str — no throwaway
            // `Value::String` clone just to classify (PERF-5).
            if let CellClass::Opaque(kind) = classify_string(s, &cfg) {
                let (marker, dropped_ref) =
                    emit_opaque_ccr_marker(s, &kind, self.ccr_store.as_ref());
                // The substitution always ships from here — surface the
                // typed ref alongside the marker text (§4.2 R2).
                dropped.push(dropped_ref);
                let kind_label = opaque_kind_label(&kind);
                return (Value::String(marker), format!("string_ccr:{kind_label}"));
            }
        }

        // 3. Plain string — passthrough.
        (Value::String(s.to_string()), String::new())
    }

    /// Compress a mixed-type array by grouping items by type and
    /// compressing each group with the appropriate handler.
    ///
    /// Direct port of `_crush_mixed_array` (Python line 2914-3013).
    ///
    /// Strategy:
    /// 1. Group by type (dict / str / number / list / null / bool / other).
    /// 2. For groups with >= `min_items_to_analyze` items: apply the
    ///    type-specific compressor.
    /// 3. For small groups: keep all items.
    /// 4. Reassemble in original order.
    ///
    /// Returns `(crushed_items, strategy_string)`.
    ///
    /// Public wrapper: allocates a throwaway sink and delegates to
    /// [`crush_mixed_array_collecting`](Self::crush_mixed_array_collecting)
    /// — same pattern as `process_value` / `smart_crush_content`.
    pub fn crush_mixed_array(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
    ) -> (Vec<Value>, String) {
        let mut sink: Vec<DroppedRef> = Vec::new();
        self.crush_mixed_array_collecting(items, query_context, bias, &mut sink)
    }

    /// Collecting variant of [`crush_mixed_array`](Self::crush_mixed_array):
    /// identical output, but the typed refs of any SHIPPED substituted
    /// render (the dict subgroup's pure-lossless table, COR-28b — which
    /// can bake in opaque-cell markers) are appended to `dropped`
    /// (§4.2 R2). Row-drop refs from the inner dict pipeline are NEVER
    /// surfaced here: that call runs `persist = false` (PersistMode::Skip
    /// — nothing persisted, COR-28), and only its pure-lossless render
    /// (no drops, no sentinel) is ever substituted; opaque-cell store
    /// writes happen eagerly inside `compact()` regardless of the
    /// persist mode, so the opaque refs it carries are backed.
    fn crush_mixed_array_collecting(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
        dropped: &mut Vec<DroppedRef>,
    ) -> (Vec<Value>, String) {
        let n = items.len();
        if n <= 8 {
            return (items.to_vec(), "mixed:passthrough".to_string());
        }

        // Group by type, tracking original indices.
        let mut groups: GroupBuckets = GroupBuckets::default();
        for (i, item) in items.iter().enumerate() {
            groups.push(group_key(item), i, item.clone());
        }

        let mut keep_indices: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
        // Kept positions whose ORIGINAL item is replaced by a rendered
        // string (the dict subgroup's lossless table — COR-28b).
        let mut substitutions: std::collections::BTreeMap<usize, Value> =
            std::collections::BTreeMap::new();
        let mut strategy_parts: Vec<String> = Vec::new();

        for (type_key, indices, values) in groups.into_iter() {
            // Small groups: keep all items.
            if values.len() < self.config.min_items_to_analyze {
                keep_indices.extend(&indices);
                continue;
            }

            match type_key {
                "dict" => {
                    // COR-28: run the shared dict pipeline WITHOUT store
                    // persistence. This arm consumes only the kept-items
                    // set — the caller appends its own whole-mixed-array
                    // sentinel (the recovery pointer rides the OUTER
                    // hash), so an inner persist would write blob +
                    // chunks + index under a hash no surfaced marker
                    // ever names: orphan entries burning COR-4-bounded
                    // capacity.
                    let routed = self.crush_array_routed(&values, query_context, bias, false);
                    let result = match routed {
                        // Passthrough: the old shape returned a full clone
                        // of `values` as the kept set, which the canonical
                        // matching below re-derived as "keep every index".
                        // Skip the clone AND the match (PERF-4) — the
                        // outcome is identical by construction.
                        Routed::Passthrough(_) => {
                            keep_indices.extend(&indices);
                            strategy_parts.push(format!("dict:{}->{}", values.len(), values.len()));
                            continue;
                        }
                        Routed::Result(result) => result,
                    };
                    let CrushArrayResult {
                        items: crushed,
                        strategy_info,
                        compacted,
                        dropped_summary,
                        dropped_refs,
                        ..
                    } = result;
                    // COR-28b (EFF-9): ship a PURE lossless win (nothing
                    // dropped, no sentinel) as ONE rendered table string
                    // at the subgroup's first position — it was
                    // previously discarded, shipping the subgroup
                    // uncompressed while reporting `dict:N->N`. Survivor-
                    // compacted renders (`dropped_summary` non-empty) are
                    // NOT substituted: their baked-in sentinel names the
                    // unpersisted inner hash — a dangling pointer — so
                    // they keep the kept-items path below.
                    if dropped_summary.is_empty() {
                        if let (Some(rendered), Some(&first_idx)) = (compacted, indices.first()) {
                            keep_indices.insert(first_idx);
                            substitutions.insert(first_idx, Value::String(rendered));
                            // The substituted render SHIPS — surface its
                            // typed refs (opaque cells only: a pure
                            // lossless render has no row-drop, and the
                            // opaque originals were written eagerly by
                            // `compact()` regardless of persist mode).
                            dropped.extend(dropped_refs);
                            strategy_parts.push(format!(
                                "dict:{}->{}",
                                values.len(),
                                strategy_info
                            ));
                            continue;
                        }
                    }
                    // Kept-items path: the inner result's renders (and
                    // any refs they carried) are DISCARDED — no inner
                    // marker ships, so no ref may surface (COR-28).
                    // Find which original indices survived by matching
                    // canonical-JSON serialization. Mirrors Python's
                    // `json.dumps(c, sort_keys=True, default=str)`-keyed
                    // set match.
                    let crushed_keys: std::collections::HashSet<String> =
                        crushed.iter().map(canonical_json_for_match).collect();
                    for (i, idx) in indices.iter().enumerate() {
                        if crushed_keys.contains(&canonical_json_for_match(&values[i])) {
                            keep_indices.insert(*idx);
                        }
                    }
                    strategy_parts.push(format!("dict:{}->{}", values.len(), crushed.len()));
                }
                "str" => {
                    let strs: Vec<&str> = values.iter().filter_map(|v| v.as_str()).collect();
                    let (crushed, _) = crush_string_array(&strs, &self.config, bias);
                    let crushed_set: std::collections::HashSet<&str> =
                        crushed.iter().map(|s| s.as_str()).collect();
                    for (i, idx) in indices.iter().enumerate() {
                        if let Some(s) = values[i].as_str() {
                            if crushed_set.contains(s) {
                                keep_indices.insert(*idx);
                            }
                        }
                    }
                    strategy_parts.push(format!("str:{}->{}", values.len(), crushed.len()));
                }
                "number" => {
                    // Python: just adaptive sampling + outlier detection
                    // (no summary prefix). Keeps first/last by index
                    // and items >variance_threshold σ from mean.
                    let item_strings: Vec<String> = values.iter().map(|v| v.to_string()).collect();
                    let item_refs: Vec<&str> = item_strings.iter().map(|s| s.as_str()).collect();
                    let (_kt, kf, kl, _) = compute_k_split(&item_refs, &self.config, bias);

                    let kf = kf.min(values.len());
                    let kl = kl.min(values.len().saturating_sub(kf));
                    let first_idx: Vec<usize> = indices.iter().take(kf).copied().collect();
                    let last_idx: Vec<usize> =
                        indices.iter().rev().take(kl).copied().collect::<Vec<_>>();
                    keep_indices.extend(&first_idx);
                    keep_indices.extend(&last_idx);

                    // Outliers via finite-only stats.
                    let finite: Vec<f64> = values
                        .iter()
                        .filter_map(|v| v.as_f64().filter(|f| f.is_finite()))
                        .collect();
                    if finite.len() > 1 {
                        if let Some(mean_v) = super::stats_math::mean(&finite) {
                            if let Some(std_v) = super::stats_math::sample_stdev(&finite) {
                                if std_v > 0.0 {
                                    let threshold = self.config.variance_threshold * std_v;
                                    for (i, val) in values.iter().enumerate() {
                                        if let Some(num) = val.as_f64().filter(|f| f.is_finite()) {
                                            if (num - mean_v).abs() > threshold {
                                                keep_indices.insert(indices[i]);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    strategy_parts.push(format!("num:{}", values.len()));
                }
                _ => {
                    // list / bool / none / other → keep all items.
                    keep_indices.extend(&indices);
                }
            }
        }

        // Reassemble in original order; a substituted position ships its
        // rendered string (COR-28b) instead of the original item.
        let result: Vec<Value> = keep_indices
            .iter()
            .map(|&i| match substitutions.remove(&i) {
                Some(rendered) => rendered,
                None => items[i].clone(),
            })
            .collect();
        let strategy = format!(
            "mixed:adaptive({}->{},{})",
            n,
            result.len(),
            strategy_parts.join(",")
        );
        (result, strategy)
    }
}

// ---------- helpers ----------

/// Group key that mirrors Python's `_crush_mixed_array` switch on
/// `isinstance`. Note the bool-before-number ordering: in Python, bool
/// is a subclass of int, but JSON treats them as distinct types, so we
/// don't have the Python ordering hazard.
fn group_key(item: &Value) -> &'static str {
    match item {
        Value::Object(_) => "dict",
        Value::String(_) => "str",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::Array(_) => "list",
        Value::Null => "none",
    }
}

/// Group buckets keyed by the type-string. Preserves first-occurrence
/// order across keys so dict/str/number/list/none/bool always come out
/// in the same order — matters because `keep_indices` is built
/// incrementally and Python iterates `groups.items()` (insertion order
/// in 3.7+).
#[derive(Default)]
struct GroupBuckets {
    entries: Vec<(&'static str, Vec<usize>, Vec<Value>)>,
    index_of: std::collections::HashMap<&'static str, usize>,
}

impl GroupBuckets {
    fn push(&mut self, key: &'static str, idx: usize, value: Value) {
        match self.index_of.get(key).copied() {
            Some(i) => {
                self.entries[i].1.push(idx);
                self.entries[i].2.push(value);
            }
            None => {
                self.index_of.insert(key, self.entries.len());
                self.entries.push((key, vec![idx], vec![value]));
            }
        }
    }
}

impl IntoIterator for GroupBuckets {
    type Item = (&'static str, Vec<usize>, Vec<Value>);
    type IntoIter = std::vec::IntoIter<Self::Item>;
    fn into_iter(self) -> Self::IntoIter {
        self.entries.into_iter()
    }
}

/// Serialize a `Value` for membership comparison. Mirrors Python's
/// `json.dumps(c, sort_keys=True, default=str)` used by
/// `_crush_mixed_array` to match crushed dict items back to their
/// original indices. The `default=str` fallback only matters for
/// non-JSON-serializable Python values; in serde_json land everything
/// is already JSON-native, so plain canonical JSON suffices.
fn canonical_json_for_match(value: &Value) -> String {
    crate::util::pyjson::python_json_dumps_sort_keys(value)
}

// ─── Walker-integration helpers (string handling) ──────────────────────
//
// Parse-as-JSON-container, marker formatting, and humanize-bytes used to
// live here as locals. They now live in `compaction::walker` so
// `walker.rs` and `process_value` share one canonical implementation —
// killing the drift risk where the two paths could format markers
// differently. `process_string` now calls `try_parse_json_container` and
// `emit_opaque_ccr_marker` directly. Only `opaque_kind_label` survives
// here because `process_string`'s `string_ccr:<kind>` strategy-info
// label is local to this module's debug-string convention.

fn opaque_kind_label(kind: &super::compaction::OpaqueKind) -> &str {
    use super::compaction::OpaqueKind;
    match kind {
        OpaqueKind::Base64Blob => "base64",
        OpaqueKind::LongString => "string",
        OpaqueKind::HtmlChunk => "html",
        OpaqueKind::Other(s) => s.as_str(),
    }
}

#[cfg(test)]
mod tests {
    use super::super::builder::SmartCrusherBuilder;
    use super::super::config::{RoutingPolicy, SmartCrusherConfig};
    use super::super::crusher::test_support::{crusher, crusher_with_store, lossless_only_crusher};
    use super::*;
    use crate::ccr::CcrStore;
    use serde_json::json;
    use std::collections::HashSet;
    use std::sync::Arc;

    // ---------- crush_mixed_array ----------

    #[test]
    fn crush_mixed_passthrough_at_threshold() {
        let c = crusher();
        let items: Vec<Value> = vec![
            json!(1),
            json!("two"),
            json!({"k": "v"}),
            json!([1, 2]),
            json!(null),
            json!(true),
            json!(3),
            json!("four"),
        ];
        let (result, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert_eq!(result.len(), 8);
        assert_eq!(strat, "mixed:passthrough");
    }

    #[test]
    fn crush_mixed_groups_and_compresses_dicts() {
        let c = crusher();
        // 25 dicts (large group → gets crushed) + 5 strings (small group → all kept).
        let mut items: Vec<Value> = (0..25).map(|i| json!({"id": i, "status": "ok"})).collect();
        for i in 0..5 {
            items.push(json!(format!("string_{}", i)));
        }
        let (result, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(strat.starts_with("mixed:adaptive("));
        // The 5 strings (small group) all survive. (Counted by value:
        // since COR-28b the dict subgroup may ALSO ship as one rendered
        // table string, so a blanket is_string() count is ambiguous.)
        let str_count = result
            .iter()
            .filter(|v| v.as_str().is_some_and(|s| s.starts_with("string_")))
            .count();
        assert_eq!(str_count, 5);
    }

    #[test]
    fn crush_mixed_keeps_lists_and_nulls_unchanged() {
        let c = crusher();
        let mut items: Vec<Value> = vec![json!([1, 2]); 6];
        items.extend(vec![json!(null); 6]);
        items.extend(vec![json!({"k": 1}); 10]);
        let (result, _strat) = c.crush_mixed_array(&items, "", 1.0);
        // Lists and nulls (not dict/str/number) → fall through to "keep all".
        let list_count = result.iter().filter(|v| v.is_array()).count();
        let null_count = result.iter().filter(|v| v.is_null()).count();
        assert_eq!(list_count, 6);
        assert_eq!(null_count, 6);
    }

    #[test]
    fn mixed_dict_arm_persists_nothing_to_the_store() {
        // COR-28(a): the dict subgroup's inner crush must not write
        // blob + chunks + index into the store — the mixed arm consumes
        // only the kept-items set and surfaces NO marker naming the
        // inner hash (the caller appends its own whole-mixed-array
        // sentinel), so every inner write is an orphan entry burning
        // COR-4-bounded capacity. Store must stay EMPTY.
        use crate::ccr::InMemoryCcrStore;
        use std::sync::Arc;
        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        // No compaction stage: the dict subgroup must take the LOSSY
        // path (the persist-writing one), not a lossless render.
        let c = SmartCrusherBuilder::new(SmartCrusherConfig::default())
            .with_default_oss_setup()
            .with_ccr_store(store_dyn)
            .build();
        let mut items: Vec<Value> = (0..25).map(|i| json!({"id": i, "status": "ok"})).collect();
        for i in 0..9 {
            items.push(json!(i));
        }
        let (crushed, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(
            crushed.len() < items.len(),
            "fixture precondition: the dict subgroup must actually drop rows, strat={strat}"
        );
        assert_eq!(
            store.len(),
            0,
            "no surfaced marker names the inner dict-subgroup hash — the \
             mixed arm must not persist (COR-28), strat={strat}"
        );
    }

    #[test]
    fn mixed_dict_subgroup_ships_lossless_render_when_it_wins() {
        // COR-28(b) / EFF-9: a PURE lossless win on the dict subgroup
        // (nothing dropped, no sentinel) used to be thrown away — the
        // subgroup shipped uncompressed while `strategy_parts` reported
        // `dict:25->25`. It must ship as ONE rendered table string at
        // the subgroup's first position instead.
        let config = SmartCrusherConfig {
            // Deterministic: lossless wins whenever its gate clears,
            // independent of tokenizer sizing.
            routing_policy: RoutingPolicy::LosslessFirst,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusher::new(config);
        // Wide, repetitive df-style rows — compacts far past both
        // lossless gates (same shape as the small-array lossless test).
        let mut items: Vec<Value> = (0..25)
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
        for i in 0..4 {
            items.push(json!(format!("trailing_note_{i}")));
        }
        let (crushed, strat) = c.crush_mixed_array(&items, "", 1.0);
        assert!(
            strat.contains("dict:25->lossless:table"),
            "strategy must report the lossless subgroup win, got: {strat}"
        );
        let table = crushed
            .iter()
            .find_map(|v| v.as_str().filter(|s| s.starts_with("[25]{")));
        assert!(
            table.is_some(),
            "dict subgroup must ship as one rendered table string, got: {crushed:?}"
        );
        assert_eq!(
            crushed.iter().filter(|v| v.is_object()).count(),
            0,
            "no raw dict row may remain once the lossless render shipped"
        );
        // The render sits at the subgroup's first original position.
        assert!(
            crushed
                .first()
                .and_then(|v| v.as_str())
                .is_some_and(|s| s.starts_with("[25]{")),
            "the rendered table replaces the subgroup at its first index"
        );
        // The 4 trailing strings (small group) still pass through.
        let trailing = crushed
            .iter()
            .filter(|v| v.as_str().is_some_and(|s| s.starts_with("trailing_note_")))
            .count();
        assert_eq!(trailing, 4);
    }

    // ---------- top-level crush ----------

    #[test]
    fn crush_non_json_passes_through_unchanged() {
        let c = crusher();
        let result = c.crush("not json at all", "", 1.0);
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "not json at all");
        assert_eq!(result.strategy, "passthrough");
    }

    #[test]
    fn crush_scalar_json_passes_through() {
        let c = crusher();
        let result = c.crush("42", "", 1.0);
        // A scalar is not crushable; should round-trip unchanged.
        assert_eq!(result.compressed, "42");
        assert!(!result.was_modified);
    }

    #[test]
    fn crush_small_array_passes_through() {
        let c = crusher();
        // Compact-form input matches the compact serializer output, so
        // the array is not "modified" even though it round-trips
        // through parse → serialize. (The spaced form `[1, 2, 3]`
        // would mark `was_modified=true` because the compact
        // serializer rewrites it to `[1,2,3]`.)
        let result = c.crush(r#"[1,2,3]"#, "", 1.0);
        // Below min_items_to_analyze=5 → no crushing of the structure.
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "[1,2,3]");
    }

    #[test]
    fn crush_dict_array_crushes_when_low_uniqueness() {
        // The public `crush()` API serializes back to JSON; the
        // lossless-path output (a compacted string) is exposed via
        // `crush_array().compacted` rather than being substituted into
        // the JSON re-serialization. So we exercise the lossy path
        // here via `without_compaction()` to validate the original
        // intent: low-uniqueness dicts compress via row-dropping.
        let c = SmartCrusher::without_compaction(SmartCrusherConfig::default());
        let mut input = String::from("[");
        for i in 0..30 {
            if i > 0 {
                input.push(',');
            }
            input.push_str(r#"{"status":"ok"}"#);
        }
        input.push(']');
        let result = c.crush(&input, "", 1.0);
        assert!(
            result.was_modified,
            "30 identical dicts should compress (low_uniqueness_safe_to_sample)"
        );
        assert_ne!(result.strategy, "passthrough");
    }

    #[test]
    fn crush_serializes_with_python_safe_format() {
        let c = crusher();
        // SmartCrusher uses Python's `safe_json_dumps`: compact
        // separators `(",", ":")` + `ensure_ascii=False`, preserving
        // object-key insertion order. A spaced input round-trips to
        // the compact form.
        let input = r#"{"a": 1, "b": 2, "c": 3}"#;
        let result = c.crush(input, "", 1.0);
        assert_eq!(
            result.compressed, r#"{"a":1,"b":2,"c":3}"#,
            "safe_json_dumps emits compact `,` / `:` separators"
        );
    }

    #[test]
    fn crush_recurses_into_nested_arrays() {
        let c = crusher();
        // Top-level dict with a nested array of 30 identical items.
        // The inner array should compress (low_uniqueness path).
        let mut inner = String::from("[");
        for i in 0..30 {
            if i > 0 {
                inner.push(',');
            }
            inner.push_str(r#"{"status":"ok"}"#);
        }
        inner.push(']');
        let input = format!(r#"{{"data": {}}}"#, inner);
        let result = c.crush(&input, "", 1.0);
        assert!(
            result.was_modified,
            "nested compressible array must be crushed even inside a wrapper object"
        );
    }

    // ---------- walker-integration in process_value ----------

    #[test]
    fn process_string_short_string_passthrough() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let (out, info) = c.process_value(&json!("hello world"), 0, "", 1.0);
        assert_eq!(out, json!("hello world"));
        assert!(info.is_empty());
    }

    #[test]
    fn process_string_stringified_json_array_recurses() {
        // A string-typed field whose value is a JSON-encoded array of
        // dicts. process_value should parse it, recurse, and return
        // the processed JSON re-rendered as a string.
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let big_array_json = serde_json::to_string(
            &(0..50)
                .map(|i| json!({"id": i, "level": "info", "msg": "ok"}))
                .collect::<Vec<_>>(),
        )
        .unwrap();
        let doc = json!({"payload": big_array_json.clone()});
        let (out, info) = c.process_value(&doc, 0, "", 1.0);
        // payload still a string-typed field — we preserved the
        // wrapping shape — but its content was processed.
        let payload = out.pointer("/payload").and_then(|v| v.as_str()).unwrap();
        // Either compressed or unchanged; if compressed, info reflects.
        // For 50 items with low-uniqueness, compression should fire.
        // The strategy info should mention string_json processing.
        assert!(
            info.contains("string_json") || payload != big_array_json,
            "expected processing trace; info={info}, len before={}, after={}",
            big_array_json.len(),
            payload.len(),
        );
    }

    #[test]
    fn process_string_opaque_blob_becomes_ccr_marker() {
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let big_b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let doc = json!({"id": 1, "blob": big_b64});
        let (out, _info) = c.process_value(&doc, 0, "", 1.0);
        let blob = out.pointer("/blob").and_then(|v| v.as_str()).unwrap();
        assert!(blob.starts_with("<<ccr:"), "got: {blob}");
        assert!(blob.contains(",base64,"));
    }

    #[test]
    fn process_string_top_level_string_processed() {
        // crush() takes a string; if it doesn't parse as JSON, today's
        // behavior returns it unchanged. But if it's a stringified
        // JSON object/array, it should now get processed.
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        // Non-JSON top-level string — passthrough.
        let plain = "just some plain text";
        let result = c.crush(plain, "", 1.0);
        assert_eq!(result.compressed, plain);
    }

    #[test]
    fn process_string_does_not_alter_short_quoted_strings() {
        // Strings that look JSON-like but are short shouldn't be
        // CCR-substituted.
        let c = SmartCrusher::new(SmartCrusherConfig::default());
        let doc = json!({"msg": "{this looks like json but isnt}"});
        let (out, _) = c.process_value(&doc, 0, "", 1.0);
        assert_eq!(out, doc);
    }

    #[test]
    fn process_string_helper_parses_only_containers() {
        assert!(try_parse_json_container("{\"a\":1}").is_some());
        assert!(try_parse_json_container("[1,2,3]").is_some());
        assert!(try_parse_json_container("123").is_none()); // bare scalar
        assert!(try_parse_json_container("\"hello\"").is_none()); // bare string
        assert!(try_parse_json_container("not json").is_none());
        assert!(try_parse_json_container("{malformed").is_none());
    }

    // ---------- strict lossless-or-passthrough (`lossless_only`) ----------

    #[test]
    fn lossless_only_string_and_number_arrays_pass_through() {
        // The non-dict crushers are sampling drops (lossy-recoverable);
        // strict mode routes their arrays to plain recursive descent.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });

        let strings: Vec<Value> = (0..200)
            .map(|i| Value::String(format!("log-line-{i}-payload")))
            .collect();
        let (out, _info) = c.process_value(&Value::Array(strings.clone()), 0, "", 1.0);
        assert_eq!(
            out.as_array().map(|a| a.len()),
            Some(200),
            "string array must pass through untouched"
        );

        let numbers: Vec<Value> = (0..200).map(|i| json!(i * 7)).collect();
        let (out, _info) = c.process_value(&Value::Array(numbers.clone()), 0, "", 1.0);
        assert_eq!(
            out.as_array().map(|a| a.len()),
            Some(200),
            "number array must pass through untouched"
        );

        let mixed: Vec<Value> = (0..100)
            .flat_map(|i| [Value::String(format!("s{i}")), json!(i)])
            .collect();
        let (out, _info) = c.process_value(&Value::Array(mixed.clone()), 0, "", 1.0);
        assert_eq!(
            out.as_array().map(|a| a.len()),
            Some(200),
            "mixed array must pass through untouched"
        );

        assert_eq!(store.len(), 0, "no drops → no store writes");
    }

    #[test]
    fn lossless_only_disables_opaque_string_substitution() {
        // The walker-equivalent string path normally substitutes long
        // base64 blobs with `<<ccr:HASH,base64,SIZE>>`. Strict mode keeps
        // the blob verbatim (visible bytes are never hidden).
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let big_b64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let doc = json!({"id": 1, "blob": big_b64});

        let (out, _info) = c.process_value(&doc, 0, "", 1.0);

        let blob = out.pointer("/blob").and_then(|v| v.as_str()).unwrap();
        assert_eq!(blob, big_b64, "blob must stay verbatim in strict mode");
        assert_eq!(store.len(), 0);
    }

    #[test]
    fn lossless_only_disables_object_key_crush() {
        // Object key-crush drops keys with no recovery pointer at all —
        // doubly forbidden in strict mode. Every key must survive.
        let (c, _store) = lossless_only_crusher(SmartCrusherConfig {
            min_tokens_to_crush: 1, // make key-crush eager if it were allowed
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let mut obj = serde_json::Map::new();
        for i in 0..40 {
            obj.insert(
                format!("key_{i}"),
                Value::String(format!("value-{i}-with-some-padding-to-cost-tokens")),
            );
        }

        let (out, _info) = c.process_value(&Value::Object(obj.clone()), 0, "", 1.0);

        assert_eq!(
            out.as_object().map(|o| o.len()),
            Some(40),
            "no key may be dropped in strict mode"
        );
    }

    #[test]
    fn lossless_only_end_to_end_crush_output_carries_no_ccr_pointer() {
        // Public `crush()` over a document holding every lossy-tempting
        // shape at once: a droppable dict sub-array, a big string array,
        // and an opaque blob. Strict mode output must contain no `<<ccr:`
        // pointer anywhere and surface no typed row-drop hashes.
        let (c, store) = lossless_only_crusher(SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // lossless never clears → pure passthrough
            lossless_only: true,
            ..SmartCrusherConfig::default()
        });
        let blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".repeat(16);
        let doc = json!({
            "rows": (0..50).map(|_| json!({"status": "ok"})).collect::<Vec<_>>(),
            "lines": (0..100).map(|i| format!("line-{i}")).collect::<Vec<_>>(),
            "attachment": blob,
        });
        let content = serde_json::to_string(&doc).unwrap();

        let result = c.crush(&content, "", 1.0);

        assert!(
            !result.compressed.contains("<<ccr:"),
            "strict-mode crush() output must be pointer-free, got: {}",
            &result.compressed[..result.compressed.len().min(300)]
        );
        assert!(result.dropped.is_empty(), "no typed recovery refs");
        assert!(result.ccr_hashes().is_empty(), "no typed row-drop hashes");
        assert!(result.row_index_markers().is_empty());
        assert_eq!(store.len(), 0, "no store writes in strict mode");
        // Every original row/line survives (parse and count).
        let parsed: Value = serde_json::from_str(&result.compressed).unwrap();
        assert_eq!(
            parsed.pointer("/rows").unwrap().as_array().unwrap().len(),
            50
        );
        assert_eq!(
            parsed.pointer("/lines").unwrap().as_array().unwrap().len(),
            100
        );
        assert_eq!(
            parsed.pointer("/attachment").unwrap().as_str().unwrap(),
            blob
        );
    }

    // ---------- 1A non-dict silent-loss regression (adversarial) ----------
    //
    // The defect these pin: the NON-dict crush paths
    // (`crush_string_array`, `crush_number_array`, `crush_mixed_array`)
    // dropped distinct items with NO store write and NO sentinel — a
    // dropped needle was *silently* lost (markers=[], store empty,
    // `ccr_get` returns nothing). Now `process_value`'s String/Number/
    // Mixed branches route the full original through `persist_dropped`
    // and append a `_ccr_dropped` sentinel, so every distinct dropped
    // item is recoverable via `ccr_get(hash)` — same guarantee the dict
    // path already had.

    /// Recursively collect every `<<ccr:HASH N_rows_offloaded>>` hash
    /// from a crushed JSON tree (string-leaf markers AND `_ccr_dropped`
    /// object sentinels) plus every kept scalar's canonical repr.
    fn collect_scalars_and_hashes(
        node: &Value,
        scalars: &mut HashSet<String>,
        hashes: &mut Vec<String>,
    ) {
        match node {
            Value::Array(a) => {
                for x in a {
                    collect_scalars_and_hashes(x, scalars, hashes);
                }
            }
            Value::Object(map) => {
                if let Some(Value::String(s)) = map.get("_ccr_dropped") {
                    if let Some(h) = extract_ccr_hash(s) {
                        hashes.push(h);
                    }
                    // GRANULAR retrieval: the `_ccr_rows` marker names the
                    // per-blob row index (`{hash}#rows`). The index key is
                    // collected so the caller can resolve it to per-row
                    // hashes and prove each row is individually
                    // addressable + recoverable.
                    if let Some(Value::String(idx)) = map.get("_ccr_rows") {
                        if let Some(h) = extract_ccr_hash(idx) {
                            hashes.push(h);
                        }
                    }
                    return;
                }
                for v in map.values() {
                    collect_scalars_and_hashes(v, scalars, hashes);
                }
            }
            Value::String(s) => {
                if let Some(h) = extract_ccr_hash(s) {
                    hashes.push(h);
                } else {
                    scalars.insert(canonical_json_for_match(node));
                }
            }
            _ => {
                scalars.insert(canonical_json_for_match(node));
            }
        }
    }

    /// Pull the 12-char hash out of a `<<ccr:HASH N_rows_offloaded>>`
    /// marker string, if present.
    fn extract_ccr_hash(s: &str) -> Option<String> {
        let start = s.find("<<ccr:")? + "<<ccr:".len();
        let rest = &s[start..];
        let end = rest.find(' ')?;
        Some(rest[..end].to_string())
    }

    /// Run the full public `crush()` path over `items`, then assert that
    /// EVERY distinct input is recoverable: present in the kept output OR
    /// restorable from the CCR store under an emitted hash. Returns
    /// `(total, recovered, n_markers, store_len)`.
    fn assert_no_silent_loss(
        c: &SmartCrusher,
        store: &crate::ccr::InMemoryCcrStore,
        items: &[Value],
    ) {
        let content = serde_json::to_string(items).unwrap();
        let result = c.crush(&content, "", 1.0);
        let out: Value = serde_json::from_str(&result.compressed).unwrap();

        let mut kept_scalars: HashSet<String> = HashSet::new();
        let mut hashes: Vec<String> = Vec::new();
        collect_scalars_and_hashes(&out, &mut kept_scalars, &mut hashes);

        // A drop must emit at least one marker + populate the store.
        assert!(
            !hashes.is_empty(),
            "expected a <<ccr:..>> sentinel after dropping rows; got none. compressed={}",
            &result.compressed[..result.compressed.len().min(200)]
        );
        assert!(store.len() > 0, "ccr_store must be populated on drop");

        // Resolve every surfaced hash. With the granular model a drop
        // surfaces BOTH the whole-blob pointer (`_ccr_dropped`) and one
        // per-row pointer per original row (`_ccr_rows`). When the array
        // is large enough that the per-row chunks fill the bounded LRU,
        // the (now-redundant) whole-blob entry MAY be evicted — that is
        // acceptable precisely because the granular chunks recover every
        // row on their own. So a hash that fails to resolve is tolerated
        // here; the real invariant — every distinct input recovered — is
        // asserted on the final `recovered` set below.
        let mut recovered: HashSet<String> = kept_scalars;
        let mut n_resolved = 0usize;
        for h in &hashes {
            let Some(payload) = store.get(h) else {
                continue;
            };
            n_resolved += 1;
            let restored: Vec<Value> = serde_json::from_str(&payload).unwrap();
            // A `{hash}#rows` ROW INDEX resolves to a JSON array of per-row
            // hash STRINGS, not rows. Follow each one — the proportional
            // retrieval path: one `ccr_get(row_hash)` per needed row,
            // each returning exactly `[row]`. This is what makes a single
            // needed row cost ONE row, not the whole blob.
            if h.ends_with("#rows") {
                for hv in &restored {
                    if let Value::String(row_hash) = hv {
                        if let Some(row_payload) = store.get(row_hash) {
                            let row_arr: Vec<Value> = serde_json::from_str(&row_payload).unwrap();
                            for x in row_arr {
                                recovered.insert(canonical_json_for_match(&x));
                            }
                        }
                    }
                }
                continue;
            }
            for x in restored {
                recovered.insert(canonical_json_for_match(&x));
            }
        }
        assert!(
            n_resolved > 0,
            "at least one surfaced <<ccr:..>> hash must resolve in the store"
        );

        let distinct_inputs: HashSet<String> = items.iter().map(canonical_json_for_match).collect();
        let lost: Vec<&String> = distinct_inputs.difference(&recovered).collect();
        assert!(
            lost.is_empty(),
            "{} distinct items silently lost (recovered {}/{}); first lost: {:?}",
            lost.len(),
            distinct_inputs.len() - lost.len(),
            distinct_inputs.len(),
            lost.iter().take(3).collect::<Vec<_>>()
        );
    }

    #[test]
    fn string_array_drops_are_ccr_recoverable() {
        // 1000 distinct strings → adversarial counterexample (was 964
        // silently lost).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..1000)
            .map(|i| json!(format!("log-line-entry-number-{i}-payload")))
            .collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn number_array_drops_are_ccr_recoverable() {
        // 1000 distinct numbers (was 985 silently lost).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..1000).map(|i| json!(i)).collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn mixed_array_drops_are_ccr_recoverable() {
        // 700 mixed str+number items (was 679 silently lost).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..700)
            .map(|i| {
                if i % 2 == 0 {
                    json!(format!("event-{i}"))
                } else {
                    json!(i)
                }
            })
            .collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn unicode_string_array_drops_are_ccr_recoverable() {
        // 1000 distinct unicode strings → the canonical bytes + hash
        // round-trip non-ASCII content losslessly.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..1000)
            .map(|i| {
                let cp = char::from_u32(0x4E00 + (i % 2000) as u32).unwrap_or('日');
                json!(format!("café-{i}-日本語-{cp}"))
            })
            .collect();
        assert_no_silent_loss(&c, &store, &items);
    }

    #[test]
    fn dict_array_recovery_still_green_after_refactor() {
        // Control: the dict path (already 1A-covered) must keep
        // recovering 100% after extracting the shared `persist_dropped`
        // helper. Pins that the refactor didn't regress the dict path.
        let (c, store) = crusher_with_store();
        // Low-uniqueness dicts so the analyzer is willing to crush, and
        // a high lossless threshold so the lossy/CCR path fires rather
        // than lossless compaction.
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99,
            ..SmartCrusherConfig::default()
        };
        let store_dyn: std::sync::Arc<dyn CcrStore> =
            std::sync::Arc::clone(&store) as std::sync::Arc<dyn CcrStore>;
        let c2 = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();
        let _ = &c; // silence: reuse store handle from helper
        let items: Vec<Value> = (0..200)
            .map(|i| json!({"status": "ok", "seq": i}))
            .collect();
        assert_no_silent_loss(&c2, &store, &items);
    }

    // ── crush() typed row-drop fields (pass 1a parity) ──────────────────
    //
    // `crush()` now surfaces every row-drop hash + row-index marker TYPED
    // on `CrushResult` so the Python shim mirrors them DIRECTLY instead of
    // scraping `<<ccr:HASH>>` out of the rendered text. These tests pin the
    // contract at the Rust boundary; the Python parity test
    // (`tests/test_crush_typed_hash_parity.py`) pins the end-to-end mirror.

    /// Build a lossy-forced crusher WITH a store so row-drops persist and
    /// row-index markers populate. Mirrors the harness used by the
    /// no-silent-loss tests above.
    fn lossy_crusher_with_store() -> (SmartCrusher, Arc<dyn CcrStore>) {
        use crate::ccr::InMemoryCcrStore;
        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let cfg = SmartCrusherConfig {
            lossless_min_savings_ratio: 0.99, // force lossy row-drop path
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_ccr_store(Arc::clone(&store))
            .build();
        (c, store)
    }

    #[test]
    fn crush_surfaces_typed_row_drop_hash_matching_embedded_marker() {
        let (c, _store) = lossy_crusher_with_store();
        // Single droppable dict array. Sized so the DROPPED count stays
        // within the granular chunk budget (`capacity / 4` = 250 for the
        // default store) — an oversized drop persists the whole-blob
        // only (COR-4) and would surface no row-index marker to pin.
        let items: Vec<Value> = (0..200)
            .map(|i| json!({"id": i, "status": "ok", "svc": "api"}))
            .collect();
        let content = serde_json::to_string(&items).unwrap();
        let r = c.crush(&content, "", 1.0);

        // A drop happened → at least one typed hash, and the SAME hash is
        // embedded in the rendered `<<ccr:HASH N_rows_offloaded>>` marker.
        // Uses the DERIVED back-compat getters — the corpus lock for R1's
        // field→getter promotion (values asserted against the RENDERED
        // text, not against each other).
        assert!(
            !r.ccr_hashes().is_empty(),
            "row drop must surface a typed ccr_hash; strategy={:?}",
            r.strategy
        );
        for h in &r.ccr_hashes() {
            assert!(
                r.compressed.contains(&format!("<<ccr:{h} ")),
                "typed hash {h} must match the embedded row-drop marker"
            );
        }
        // Store-backed → a row-index marker is surfaced for proportional
        // retrieval, and it is embedded too (as the `_ccr_rows` field).
        assert!(
            !r.row_index_markers().is_empty(),
            "store-backed drop must surface a typed row_index_marker"
        );
        for m in &r.row_index_markers() {
            assert!(
                r.compressed.contains(m.as_str()),
                "typed row_index_marker {m} must be embedded in the output"
            );
        }
        // The typed refs' BARE index keys resolve in the store — the
        // datum R5's Python mirror consumes instead of marker text.
        for d in &r.dropped {
            let key = d
                .row_index_key()
                .expect("store-backed row drop carries a row-index key");
            assert!(
                key.ends_with("#rows"),
                "bare key form is HASH#rows, got: {key}"
            );
        }
    }

    #[test]
    fn crush_surfaces_one_typed_hash_per_dropped_subarray() {
        // ★ The multiplicity the singular-spec model would silently lose:
        // an object with TWO independent droppable sub-arrays must yield
        // TWO distinct typed hashes — one per drop — NOT a single hash.
        let (c, _store) = lossy_crusher_with_store();
        let arr_a: Vec<Value> = (0..300)
            .map(|i| json!({"id": i, "kind": "a", "status": "ok"}))
            .collect();
        let arr_b: Vec<Value> = (0..300)
            .map(|i| json!({"ref": i, "kind": "b", "level": "INFO"}))
            .collect();
        let doc = json!({"alpha": arr_a, "beta": arr_b});
        let content = serde_json::to_string(&doc).unwrap();
        let r = c.crush(&content, "", 1.0);

        // Distinct hashes (the two arrays differ) and both ≥ 2.
        let hashes = r.ccr_hashes();
        let distinct: std::collections::HashSet<&String> = hashes.iter().collect();
        assert!(
            distinct.len() >= 2,
            "two droppable sub-arrays must surface ≥2 distinct typed hashes, \
             got {:?} (strategy={:?})",
            hashes,
            r.strategy
        );
        // Every typed hash is embedded in the output as a row-drop marker
        // (parity with the scrape the Python shim used to depend on).
        let out: Value = serde_json::from_str(&r.compressed).unwrap();
        let mut embedded_scalars: HashSet<String> = HashSet::new();
        let mut embedded_hashes: Vec<String> = Vec::new();
        collect_scalars_and_hashes(&out, &mut embedded_scalars, &mut embedded_hashes);
        let embedded: std::collections::HashSet<&String> = embedded_hashes.iter().collect();
        for h in &hashes {
            assert!(
                embedded.contains(h),
                "typed hash {h} must appear in the embedded row-drop markers \
                 (parity with the scrape)"
            );
        }
    }

    // ── COR-44: magic-key guard in smart_crush_content_collecting ──

    #[test]
    fn smart_crush_content_passthrough_on_serde_private_marker() {
        // COR-44: with arbitrary_precision + raw_value enabled, feeding
        // {"$serde_json::private::Number":"123"} to serde_json::from_str
        // would silently return the number literal 123 — mutating the input.
        // The guard must decline parsing and return the original bytes
        // unchanged (was_modified=false, no info, no dropped refs).
        let c = crusher();
        let magic = r#"{"$serde_json::private::Number":"123"}"#;
        let (result, was_modified, info, dropped) =
            c.smart_crush_content_collecting(magic, "", 1.0);
        assert_eq!(
            result, magic,
            "magic-key input must be returned byte-identical"
        );
        assert!(!was_modified, "magic-key input must not be marked modified");
        assert!(info.is_empty(), "no strategy info for declined input");
        assert!(dropped.is_empty(), "no dropped refs for declined input");
    }

    #[test]
    fn smart_crush_content_wrapper_delegates_to_collecting_and_drops_sink() {
        // HONEST SCOPE: `smart_crush_content` IS a wrapper over
        // `smart_crush_content_collecting` (it calls it and discards the
        // 4th element), so this only proves the wrapper forwards render +
        // flags faithfully and that the collecting variant actually
        // collects on a multi-array doc — NOT byte-identity vs the
        // pre-typed-field behavior. The byte-identity-vs-before guarantee
        // is empirical: the floor bench (retain=1.000 on every dataset +
        // needle 100%) and grammar-characterization (marker text frozen).
        let (c, _store) = lossy_crusher_with_store();
        let arr_a: Vec<Value> = (0..300).map(|i| json!({"id": i, "status": "ok"})).collect();
        let arr_b: Vec<Value> = (0..300).map(|i| json!(format!("log-line-{i}"))).collect();
        let doc = json!({"alpha": arr_a, "beta": arr_b, "n": 7});
        let content = serde_json::to_string(&doc).unwrap();

        let (render_wrapper, mod_w, info_w) = c.smart_crush_content(&content, "", 1.0);
        let (render_collect, mod_c, info_c, dropped) =
            c.smart_crush_content_collecting(&content, "", 1.0);

        // Wrapper forwards the render + flags unchanged.
        assert_eq!(render_wrapper, render_collect);
        assert_eq!(mod_w, mod_c);
        assert_eq!(info_w, info_c);
        // And the collecting variant actually collected the drops.
        assert!(
            !dropped.is_empty(),
            "expected the multi-array doc to drop rows and collect hashes"
        );
    }
}
