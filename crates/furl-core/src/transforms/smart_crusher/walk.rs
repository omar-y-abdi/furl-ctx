//! Recursive JSON walk

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
use super::types::{CrushResult, DroppedRef};

impl SmartCrusher {
    /// Top-level entry point. Mirrors Python `SmartCrusher.crush` (line 1581-1603) — used by `ContentRouter` when routing JSON arrays. Parses `content` as JSON, recursively processes it
    /// (compressing arrays at every depth via the appropriate per-type crusher), then re-serializes with Python-compatible formatting (`, ` and `: ` separators, ASCII-escaped non-ASCII).
    pub fn crush(&self, content: &str, query: &str, bias: f64) -> CrushResult {
        // Collect the typed recovery refs alongside the rendered output. `smart_crush_content_collecting` threads
        // a per-call sink through the recursive walk so EVERY reduction row-drops and opaque substitutions
        let (compressed, was_modified, info, dropped) =
            self.smart_crush_content_collecting(content, query, bias);
        let strategy = if info.is_empty() {
            "passthrough".to_string()
        } else {
            info
        };

        CrushResult {
            compressed,
            original: content.to_string(),
            was_modified,
            strategy,
            dropped,
        }
    }

    /// `SmartCrusher._smart_crush_content` (Python line 2243-2301). CCR marker injection is stubbed (CCR is disabled in this stage). Deprecated in favor of
    /// [`smart_crush_content_typed`](Self::smart_crush_content_typed) (§4.2 R3/R4) — callers that mirror recovery need the refs; parity callers that only want the tuple keep this shape.
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

    /// Typed sibling of [`smart_crush_content`](Self::smart_crush_content) (§4.2 R3/R4): identical first three tuple elements.
    pub fn smart_crush_content_typed(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String, Vec<DroppedRef>) {
        self.smart_crush_content_collecting(content, query_context, bias)
    }

    /// Return the same rendered result as `smart_crush_content` plus every recursive
    /// row-drop or opaque recovery ref. The collection sink cannot change rendered bytes.
    fn smart_crush_content_collecting(
        &self,
        content: &str,
        query_context: &str,
        bias: f64,
    ) -> (String, bool, String, Vec<DroppedRef>) {
        // COR-44: decline magic-key payloads before calling from_str so serde_json's arbitrary_precision / raw_value promotions
        // never fire. Passthrough identical to the non-JSON branch: original bytes, was_modified=false, no info, no dropped refs.
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

        // Re-serialize with Python `safe_json_dumps` formatting: compact `(",", ":")` separators + `ensure_ascii=False`,
        // preserving object-key insertion order. Matches the Python SmartCrusher output bytes exactly.
        let result = crate::util::pyjson::python_safe_json_dumps(&crushed);
        let was_modified = result != content.trim();
        (result, was_modified, info, dropped)
    }

    /// Maximum recursion depth for nested JSON. Mirrors Python's
    /// `_MAX_PROCESS_DEPTH = 50`. Beyond this, values are returned as-is.
    const MAX_PROCESS_DEPTH: usize = 50;

    /// Recursively process a value, crushing arrays where appropriate. Rust's version omits since we never produce markers in this stage).
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

    /// Return the same `(Value, strategy)` as `process_value` while collecting row-drop
    /// refs from any depth. The sink does not influence sentinels or output bytes.
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
                    // Strict lossless-or-passthrough: the non-dict crushers (string / number / mixed) are sampling drops
                    // with a `<<ccr:HASH>>` recovery sentinel — lossy-recoverable, so they never run under `lossless_only`.
                    match arr_type {
                        ArrayType::DictArray => {
                            let result =
                                match self.crush_array_routed(arr, query_context, bias, true) {
                                    // Unchanged passthrough (skip / at-limit): re-wrap our own borrow of the array.
                                    Routed::Passthrough(info) => {
                                        info_parts.push(format!("{}({}->{})", info, n, n));
                                        return (Value::Array(arr.clone()), info_parts.join(","));
                                    }
                                    Routed::Result(result) => result,
                                };
                            // Lossless path won → substitute the array with the compacted string in place. The wrapping JSON structure is preserved.
                            if let Some(rendered) = result.compacted {
                                info_parts.push(format!(
                                    "{}({}->len={})",
                                    result.strategy_info,
                                    n,
                                    rendered.len()
                                ));
                                // The compacted render covers TWO cases: a PURE lossless win (nothing dropped `dropped_refs` carries only whatever opaque substitutions the render bakes in) AND a LOSSY
                                // survivor-compacted drop (`smart_sample+compact:table` — rows dropped, the `<<ccr:HASH ...>>` sentinel baked into `rendered` as its last line, the row-drop ref in `dropped_refs`).
                                dropped.extend(result.dropped_refs);
                                return (Value::String(rendered), info_parts.join(","));
                            }
                            info_parts.push(format!(
                                "{}({}->{})",
                                result.strategy_info,
                                n,
                                result.items.len()
                            ));
                            // Lossy path with rows dropped → append a CCR-Dropped sentinel object as the last element of the kept-items array. This is the **only** place the LLM sees the
                            // `<<ccr:HASH ...>>` pointer in the prompt. Sentinel shape preserves "array-of-objects" shape so downstream consumers iterating with `x.get(...)` keep working;
                            let mut items = result.items;
                            if !result.dropped_summary.is_empty() {
                                // `_ccr_dropped` carries the byte-stable whole-blob recovery pointer; row-level recovery is served from that parent.
                                let sentinel = ccr_sentinel_map(&result.dropped_summary);
                                items.push(Value::Object(sentinel));
                            }
                            // Surface the SAME hash + row-index data the sentinel advertises, typed for direct mirroring.
                            dropped.extend(result.dropped_refs);
                            return (Value::Array(items), info_parts.join(","));
                        }
                        ArrayType::StringArray if !self.config.lossless_only => {
                            let strs: Vec<&str> = arr.iter().filter_map(|v| v.as_str()).collect();
                            let (crushed, strategy) = crush_string_array(&strs, &self.config, bias);
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed_values: Vec<Value> =
                                crushed.into_iter().map(Value::String).collect();
                            // 1A (non-dict path): persist the full original + append a CCR-Dropped sentinel whenever rows were dropped, so every distinct string is recoverable via `ccr_get(hash)` — never silently lost.
                            // `advertise_retrieval_tool` gates NEITHER; it is only the router-layer retrieval-tool advertisement preference (pinned by `persist.rs`'s `non_dict_drop_surfaces_pointer_and_persists_even_with_marker_off`).
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
                            // Collecting variant: a dict subgroup's substituted lossless table can bake in
                            // opaque-cell markers — surface those typed through the same sink (§4.2 R2).
                            let (crushed, strategy) = self.crush_mixed_array_collecting(
                                arr,
                                query_context,
                                bias,
                                dropped,
                            );
                            info_parts.push(format!("{}({}->{})", strategy, n, crushed.len()));
                            let mut crushed = crushed;
                            // 1A (non-dict path): the mixed crusher drops str/number subgroup items (and its own dropped_summary was discarded).
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

                // Second pass: if the object itself has many keys, compress at the key level.
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
            // Strings: walker-equivalent handling. The collecting variant threads the sink so BOTH a row-drop INSIDE a stringified-JSON sub-array AND the
            // opaque-blob substitution itself surface typed (§4.2 R2 — this deliberately overturns the earlier scrape-by-design decision, per the owner mandate).
            Value::String(s) => {
                self.process_string_collecting(s, depth, query_context, bias, dropped)
            }
            // Other scalars — passthrough.
            _ => (value.clone(), String::new()),
        }
    }

    /// For stringified JSON, recurse while preserving the outer string type. For opaque strings, emit the
    /// standard CCR marker and a typed opaque recovery reference carrying hash, kind, and exact byte size.
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

        // Use the shared Rust hash/marker format exactly. Opaque substitution is recoverable but hides
        // visible bytes, so strict `lossless_only` leaves the blob verbatim and performs no store write.
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

    /// Compress a mixed-type array by grouping items by type and compressing each group with the appropriate handler.
    pub fn crush_mixed_array(
        &self,
        items: &[Value],
        query_context: &str,
        bias: f64,
    ) -> (Vec<Value>, String) {
        let mut sink: Vec<DroppedRef> = Vec::new();
        self.crush_mixed_array_collecting(items, query_context, bias, &mut sink)
    }

    /// Collecting variant of [`crush_mixed_array`](Self::crush_mixed_array): identical output, but the typed refs of any SHIPPED substituted
    /// render (the dict subgroup's pure-lossless table, COR-28b — which can bake in opaque-cell markers) are appended to `dropped` (§4.2 R2).
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
                    // Run the shared dict pipeline without persistence because the caller appends the mixed-array sentinel using
                    // the outer hash. Inner persistence would create blob/chunk/index entries that no surfaced marker names.
                    let routed = self.crush_array_routed(&values, query_context, bias, false);
                    let result = match routed {
                        // Passthrough: the old shape returned a full clone of `values` as the kept set, which the canonical matching below re-derived as "keep every index".
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
                    // COR-28b (EFF-9): ship a PURE lossless win (nothing dropped, no sentinel) as ONE rendered table string at the
                    // subgroup's first position — it was discarded, shipping the subgroup uncompressed while reporting `dict:N->N`.
                    if dropped_summary.is_empty() {
                        if let (Some(rendered), Some(&first_idx)) = (compacted, indices.first()) {
                            keep_indices.insert(first_idx);
                            substitutions.insert(first_idx, Value::String(rendered));
                            // The substituted render SHIPS — surface its typed refs (opaque cells only: a pure lossless render has
                            // no row-drop, and the opaque originals were written eagerly by `compact()` regardless of persist mode).
                            dropped.extend(dropped_refs);
                            strategy_parts.push(format!(
                                "dict:{}->{}",
                                values.len(),
                                strategy_info
                            ));
                            continue;
                        }
                    }
                    // Kept-items path: the inner result's renders (and any refs they carried) are DISCARDED — no inner marker ships, so no ref may surface (COR-28).
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
                    // Python: just adaptive sampling + outlier detection (no summary prefix). Keeps first/last by index and items >variance_threshold σ from mean.
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

/// Group key that mirrors Python's `_crush_mixed_array` switch on `isinstance`.
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

/// Group buckets keyed by the type-string. Preserves first-occurrence order across keys so dict/str/number/list/none/bool always come out in
/// the same order — matters because `keep_indices` is built incrementally and Python iterates `groups.items()` (insertion order in 3.7+).
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

/// Serialize a `Value` for membership comparison. The `default=str` fallback only matters for non-JSON-serializable
/// Python values; in serde_json land everything is already JSON-native, so plain canonical JSON suffices.
fn canonical_json_for_match(value: &Value) -> String {
    crate::util::pyjson::python_json_dumps_sort_keys(value)
}

// ─── Walker-integration helpers (string handling) ────────────────────── Parse-as-JSON-container, marker formatting, and humanize-bytes used to live here as locals. They now live
// in `compaction::walker` so `the Rust module` and `process_value` share one canonical implementation — killing the drift risk where the two paths could format markers differently.

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
        // The 5 strings (small group) all survive.
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
        // COR-28(a): the dict subgroup's inner crush must not write blob + chunks + index into the store. Store must stay EMPTY.
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
        // COR-28(b) / EFF-9: a PURE lossless win on the dict subgroup (nothing dropped, no sentinel) used to be thrown away — the subgroup shipped
        // uncompressed while `strategy_parts` reported `dict:25->25`. It must ship as ONE rendered table string at the subgroup's first position instead.
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
        // Compact-form input matches the compact serializer output, so the array is not "modified" even though it round-trips through parse → serialize.
        let result = c.crush(r#"[1,2,3]"#, "", 1.0);
        // Below min_items_to_analyze=5 → no crushing of the structure.
        assert!(!result.was_modified);
        assert_eq!(result.compressed, "[1,2,3]");
    }

    #[test]
    fn crush_dict_array_crushes_when_low_uniqueness() {
        // The public `crush()` API serializes back to JSON; the lossless-path output (a compacted string) is
        // exposed via `crush_array().compacted` rather than being substituted into the JSON re-serialization.
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
        // SmartCrusher uses Python's `safe_json_dumps`: compact separators `(",", ":")` + `ensure_ascii=False`,
        // preserving object-key insertion order. A spaced input round-trips to the compact form.
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
        // A string-typed field whose value is a JSON-encoded array of dicts. process_value
        // should parse it, recurse, and return the processed JSON re-rendered as a string.
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
        // Either compressed or unchanged; if compressed, info reflects. For 50 items with low-uniqueness,
        // compression should fire. The strategy info should mention string_json processing.
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
        // crush() takes a string; if it doesn't parse as JSON, today's behavior returns it
        // unchanged. But if it's a stringified JSON object/array, it should now get processed.
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
        // The walker-equivalent string path normally substitutes long base64 blobs with
        // `<<ccr:HASH,base64,SIZE>>`. Strict mode keeps the blob verbatim (visible bytes are never hidden).
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
        // Public `crush()` over a document holding every lossy-tempting shape at once: a droppable dict sub-array, a big string array, and an opaque blob.
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

    // 1A non-dict silent-loss regression (adversarial) ---------- The defect these pin: the NON-dict crush paths (`crush_string_array`, `crush_number_array`, `crush_mixed_array`)
    // dropped distinct items with NO store write and NO sentinel — a dropped needle was *silently* lost (markers=[], store empty, `ccr_get` returns nothing).

    /// Recursively collect every `<<ccr:HASH N_rows_offloaded>>` hash from a crushed JSON tree
    /// (string-leaf markers AND `_ccr_dropped` object sentinels) plus every kept scalar's canonical repr.
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

    /// Run the full public `crush()` path over `items`, then assert that EVERY distinct input is
    /// recoverable: present in the kept output OR restorable from the CCR store under an emitted hash.
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

        // Resolve every surfaced whole-blob hash. A drop surfaces the whole-blob recovery pointer (`_ccr_dropped`); `ccr_get(hash)` returns the full offloaded
        // array, from which every dropped row is recovered. The store holds one entry per drop (the whole-blob), so the recovery pointer always resolves.
        let mut recovered: HashSet<String> = kept_scalars;
        let mut n_resolved = 0usize;
        for h in &hashes {
            let Some(payload) = store.get(h) else {
                continue;
            };
            n_resolved += 1;
            let restored: Vec<Value> = serde_json::from_str(&payload).unwrap();
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
        // Control: the dict path (already 1A-covered) must keep recovering 100% after extracting
        // the shared `persist_dropped` helper. Pins that the refactor didn't regress the dict path.
        let (c, store) = crusher_with_store();
        // Low-uniqueness dicts so the analyzer is willing to crush, and a high lossless threshold so the lossy/CCR path fires rather than lossless compaction.
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

    // ── crush() typed row-drop fields (pass 1a parity) ────────────────── `crush()` surfaces every row-drop hash TYPED on
    // `CrushResult` so the Python shim mirrors them DIRECTLY instead of scraping `<<ccr:HASH>>` out of the rendered text.

    /// Build a lossy-forced crusher WITH a store so row-drops persist.
    /// Mirrors the harness used by the no-silent-loss tests above.
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
        // A single droppable dict array.
        let items: Vec<Value> = (0..200)
            .map(|i| json!({"id": i, "status": "ok", "svc": "api"}))
            .collect();
        let content = serde_json::to_string(&items).unwrap();
        let r = c.crush(&content, "", 1.0);

        // A drop happened → at least one typed hash, and the SAME hash is embedded in the rendered `<<ccr:HASH N_rows_offloaded>>` marker.
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
        // No granular `#rows` row-index marker is embedded — row-level recovery is served from the
        // whole-blob parent, not a per-blob index the model's retrieve path could never resolve.
        assert!(
            !r.compressed.contains("#rows"),
            "no granular row-index marker must be embedded, got: {}",
            &r.compressed[..r.compressed.len().min(200)]
        );
    }

    #[test]
    fn crush_surfaces_one_typed_hash_per_dropped_subarray() {
        // ★ The multiplicity the singular-spec model would silently lose: an object with TWO independent
        // droppable sub-arrays must yield TWO distinct typed hashes — one per drop — NOT a single hash.
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
        // COR-44: with arbitrary_precision + raw_value enabled, feeding {"$serde_json::private::Number":"123"}
        // to serde_json::from_str would silently return the number literal 123 — mutating the input.
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
        // HONEST SCOPE so this only proves the wrapper forwards render + flags faithfully and that the collecting
        // variant actually collects on a multi-array doc The byte-identity-vs-before guarantee is empirical
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
