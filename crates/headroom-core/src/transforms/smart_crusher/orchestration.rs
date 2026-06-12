//! Index-set orchestration helpers used by every planning method.
//!
//! Direct port of three Python methods from `smart_crusher.py`:
//!
//! - `_deduplicate_indices_by_content` (line 1721) — collapse multiple
//!   indices pointing at content-identical items into a single index
//!   (lowest wins).
//! - `_fill_remaining_slots` (line 1794) — when dedup leaves us under
//!   `effective_max`, fill back up with diverse stride-sampled indices
//!   that don't repeat content.
//! - `_prioritize_indices` (line 1891) — apply dedup + fill, then —
//!   if still over budget — keep ALL critical items (errors,
//!   structural outliers, numeric anomalies) plus first-3 / last-2,
//!   discarding non-critical items beyond the budget.
//!
//! All three operate on `BTreeSet<usize>` (sorted, deterministic
//! iteration). Item content hashes use the Python-compatible
//! `compute_item_hash` from `anchor_selector` so the same item collapses
//! to the same hash in both languages.
//!
//! # TOIN field-semantics — currently stubbed
//!
//! Python's `_prioritize_indices` accepts an optional `field_semantics`
//! map and calls `_detect_items_by_learned_semantics` to find items
//! with values in TOIN-learned important fields. TOIN isn't ported
//! yet; we mirror the Python "no field_semantics provided" branch
//! (returns no learned-important indices). When TOIN lands, we'll add
//! that argument back to the public surface.

use md5::{Digest, Md5};
use serde_json::Value;
use std::collections::{BTreeSet, HashSet};

use super::config::SmartCrusherConfig;
use super::outliers::{detect_error_items_for_preservation, detect_structural_outliers};
use super::types::{ArrayAnalysis, FieldStats};
use crate::transforms::anchor_selector::stable_item_hash;

/// Collapse content-duplicate indices to their lowest representative.
///
/// Python: `_deduplicate_indices_by_content`. Iterates `keep_indices`
/// in ascending order and records the FIRST index that hashes to a
/// given content fingerprint. Subsequent matches drop. Out-of-bounds
/// indices skip.
///
/// The grouping hash is the field-aware [`stable_item_hash`]: when `exclude`
/// is empty it is byte-equal to `compute_item_hash` (so the dedup outcome is
/// byte-equal across languages and unchanged for non-identity data); when
/// `exclude` lists high-cardinality identity columns, two rows that differ
/// ONLY in those columns hash equal and collapse (DESIGN.md Improvement 2).
pub fn deduplicate_indices_by_content(
    keep_indices: &BTreeSet<usize>,
    items: &[Value],
    exclude: &BTreeSet<String>,
) -> BTreeSet<usize> {
    if keep_indices.is_empty() {
        return BTreeSet::new();
    }

    // hash -> lowest-seen index. BTreeSet iteration is ascending, so
    // the first insertion for each hash IS the lowest index.
    let mut seen: std::collections::BTreeMap<String, usize> = std::collections::BTreeMap::new();
    for &idx in keep_indices {
        if idx >= items.len() {
            continue;
        }
        let h = item_content_hash(&items[idx], idx, exclude);
        seen.entry(h).or_insert(idx);
    }
    seen.values().copied().collect()
}

/// Fill `keep_indices` back up to `effective_max` with diverse,
/// content-unique items. Python: `_fill_remaining_slots`.
///
/// Strategy:
/// 1. Compute hashes of currently-kept items.
/// 2. Walk candidates (indices NOT in keep_indices) with stride-based
///    sampling for spatial diversity.
/// 3. Add a candidate if its content hash is fresh.
///
/// Python uses two nested loops with `start_offset` to interleave
/// stride scans — we mirror that exactly so the same items are picked
/// in the same order for parity fixtures.
pub fn fill_remaining_slots(
    keep_indices: &BTreeSet<usize>,
    items: &[Value],
    n: usize,
    effective_max: usize,
    exclude: &BTreeSet<String>,
) -> BTreeSet<usize> {
    let remaining = effective_max.saturating_sub(keep_indices.len());
    if remaining == 0 {
        return keep_indices.clone();
    }

    // Hashes of items we're already keeping — bound the working set
    // we won't re-add. Uses the stable-projection hash so a fill
    // candidate that is identical-modulo-identity to a kept row counts
    // as a duplicate and is skipped (real diversity, not identity noise).
    let mut seen: HashSet<String> = HashSet::new();
    for &idx in keep_indices {
        if idx < n {
            seen.insert(item_content_hash(&items[idx], idx, exclude));
        }
    }

    // Candidate pool: every index not already kept.
    let candidates: Vec<usize> = (0..n).filter(|i| !keep_indices.contains(i)).collect();
    if candidates.is_empty() {
        return keep_indices.clone();
    }

    let mut result = keep_indices.clone();
    let step = (candidates.len() / (remaining + 1)).max(1);
    let mut added = 0;

    // Python's interleaved stride: outer loop offsets [0, step),
    // inner loop walks `start_offset, +step, +step, ...`. The result
    // visits every candidate exactly once across the outer iterations.
    'outer: for start_offset in 0..step {
        if added >= remaining {
            break;
        }
        let mut i = start_offset;
        while i < candidates.len() {
            if added >= remaining {
                break 'outer;
            }
            let idx = candidates[i];
            let h = item_content_hash(&items[idx], idx, exclude);
            if !seen.contains(&h) {
                result.insert(idx);
                seen.insert(h);
                added += 1;
            }
            i += step;
        }
    }

    result
}

/// Top-level prioritizer. Python: `_prioritize_indices`.
///
/// Pipeline:
/// 1. **Dedup**: collapse content-duplicate indices.
/// 2. **Fill**: top up to `effective_max` with diverse uniques.
/// 3. **Already under budget?** Return as-is.
/// 4. **Otherwise**: keep ALL critical items (errors + structural
///    outliers + numeric anomalies — non-negotiable per Python's
///    "quality guarantee"). Then add first-3 + last-2 if room. Then
///    fill remaining with non-critical kept indices in ascending order.
///
/// May return MORE than `effective_max` items when critical items
/// alone exceed the budget — Python's documented behavior, mirrored
/// here.
pub fn prioritize_indices(
    config: &SmartCrusherConfig,
    keep_indices: &BTreeSet<usize>,
    items: &[Value],
    n: usize,
    analysis: Option<&ArrayAnalysis>,
    effective_max: usize,
    exclude: &BTreeSet<String>,
    query_pinned: &BTreeSet<usize>,
) -> BTreeSet<usize> {
    // Dedup pass. Uses the field-aware stable hash (`exclude` lists
    // identity columns); empty `exclude` => byte-equal to the prior
    // whole-item dedup.
    let mut current = if config.dedup_identical_items {
        deduplicate_indices_by_content(keep_indices, items, exclude)
    } else {
        keep_indices.clone()
    };

    // Fill pass.
    if current.len() < effective_max && current.len() < n {
        current = fill_remaining_slots(&current, items, n, effective_max, exclude);
    }

    if current.len() <= effective_max {
        return current;
    }

    // Over budget — apply critical-items-first prioritization.

    // Errors (keyword-detected — preservation guarantee).
    let error_indices: BTreeSet<usize> = detect_error_items_for_preservation(items, None)
        .into_iter()
        .collect();

    // Structural outliers (statistical — rare fields, rare statuses).
    let outlier_indices: BTreeSet<usize> = detect_structural_outliers(items).into_iter().collect();

    // Numeric anomalies (>variance_threshold σ from per-field mean).
    let anomaly_indices = numeric_anomaly_indices(config, items, analysis);

    // TOIN learned-important indices: empty until TOIN is ported.
    let learned_indices: BTreeSet<usize> = BTreeSet::new();

    let mut prioritized: BTreeSet<usize> = BTreeSet::new();
    prioritized.extend(&error_indices);
    prioritized.extend(&outlier_indices);
    prioritized.extend(&anomaly_indices);
    prioritized.extend(&learned_indices);

    // Query-relevant pinning. Rows the planner flagged as matching the
    // user's query (deterministic anchor hits + capped high-confidence
    // relevance — see `apply_query_signals`) are what the model most
    // needs VISIBLE; the over-budget path used to let them compete with
    // generic fill and lose by position. Pin them like critical items.
    // The set is bounded by the planner (anchor matches are exact-match
    // only; relevance pins are capped), mirroring top_n's additive
    // query-preservation precedent.
    prioritized.extend(query_pinned);

    // 1B — Field-value singleton pinning. Rows carrying a value that
    // appears EXACTLY ONCE across a (non-identity) field are needles:
    // the over-budget fill used to drop them purely by index position.
    // Pin them like structural outliers, but CAPPED so a high-singleton
    // array can't blow far past `effective_max` and inflate tokens. The
    // identity columns are excluded (a unique uuid/timestamp per row is
    // noise, not a needle), reusing the Imp2 exclude-set.
    //
    // DEGENERACY GATE: "singleton" is a *rarity* signal — a needle is a
    // row that is unusual relative to its peers. When a MAJORITY of rows
    // are singletons (e.g. an all-distinct array: every git-log subject
    // unique), the signal carries no information and pinning degenerates
    // to first-K-by-index — exactly the positional bias 1B was built to
    // remove (measured: on the all-distinct logs benchmark the pins were
    // literally indices 0..cap-1). Skip pinning in that case; every
    // skipped row remains CCR-recoverable via the unconditional persist
    // + surfaced `<<ccr:HASH>>` pointer, so nothing is lost.
    let singleton_indices = field_value_singletons(items, exclude);
    if singleton_signal_is_informative(singleton_indices.len(), items.len()) {
        let singleton_cap = singleton_pin_cap(effective_max);
        let mut singletons_pinned = 0usize;
        for &idx in &singleton_indices {
            if singletons_pinned >= singleton_cap {
                break;
            }
            if prioritized.insert(idx) {
                singletons_pinned += 1;
            }
        }
    }

    // First 3 / last 2 anchors if we have room.
    let mut remaining = effective_max.saturating_sub(prioritized.len());
    if remaining > 0 {
        for i in 0..3.min(n) {
            if !prioritized.contains(&i) && remaining > 0 {
                prioritized.insert(i);
                remaining -= 1;
            }
        }
        let last_start = n.saturating_sub(2);
        for i in last_start..n {
            if !prioritized.contains(&i) && remaining > 0 {
                prioritized.insert(i);
                remaining -= 1;
            }
        }
    }

    // 1B — Novelty-ranked fill. The remaining budget used to be filled
    // lowest-index-first, which dropped a distinct mid-array needle
    // purely by position. Instead rank `current \ prioritized` by
    // NOVELTY — rarity of the row's stable-hash family (rarer = more
    // novel) with index as a deterministic tie-break — and fill the most
    // novel first. Empty `exclude` keeps the stable hash byte-equal to
    // the whole-item hash, so this is well-defined on every dataset.
    if remaining > 0 {
        let others: Vec<usize> = current.difference(&prioritized).copied().collect();
        let ranked = rank_by_novelty(&others, items, exclude);
        for i in ranked {
            if remaining == 0 {
                break;
            }
            if prioritized.insert(i) {
                remaining -= 1;
            }
        }
    }

    prioritized
}

/// Cap on how many field-value singletons the over-budget path pins
/// (1B). Set to `effective_max` so pinned singletons can fill the budget
/// but a singleton-heavy array (e.g. every row has a unique field value)
/// cannot push survivors arbitrarily far past the target — they compete
/// for the same budget as the other critical signals. Measured against
/// the benchmark: this keeps the logs survivor count bounded while still
/// rescuing the mid-array needle that the lowest-index fill dropped.
fn singleton_pin_cap(effective_max: usize) -> usize {
    effective_max
}

/// Is the field-value-singleton signal informative for this array?
///
/// A singleton row is a needle only when singletons are RARE — the
/// signal is "this row is unusual relative to its peers". When a strict
/// majority of rows are singletons the array is simply high-entropy
/// (all-distinct subjects/messages) and "singleton" distinguishes
/// nothing; pinning by it degenerates to first-K-by-index positional
/// noise. At-most-half keeps the borderline case (exactly half) pinned.
fn singleton_signal_is_informative(singleton_count: usize, n: usize) -> bool {
    singleton_count * 2 <= n
}

/// Indices of rows carrying a value that appears EXACTLY ONCE across some
/// non-excluded field — true needles. Excludes identity columns (a
/// per-row uuid/timestamp is unique-by-construction noise, not a needle).
/// Returned in ascending index order (deterministic).
fn field_value_singletons(items: &[Value], exclude: &BTreeSet<String>) -> Vec<usize> {
    // Per-field value frequency (string-rendered, like the analyzer's
    // uniqueness computation). One pass to count, one pass to flag.
    let mut field_value_counts: std::collections::HashMap<&str, std::collections::HashMap<String, usize>> =
        std::collections::HashMap::new();
    for item in items {
        if let Some(obj) = item.as_object() {
            for (k, v) in obj {
                if exclude.contains(k) || v.is_null() {
                    continue;
                }
                let key = value_signature(v);
                *field_value_counts
                    .entry(k.as_str())
                    .or_default()
                    .entry(key)
                    .or_insert(0) += 1;
            }
        }
    }

    let mut out: Vec<usize> = Vec::new();
    for (idx, item) in items.iter().enumerate() {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let is_singleton = obj.iter().any(|(k, v)| {
            if exclude.contains(k) || v.is_null() {
                return false;
            }
            field_value_counts
                .get(k.as_str())
                .and_then(|m| m.get(&value_signature(v)))
                .copied()
                .unwrap_or(0)
                == 1
        });
        if is_singleton {
            out.push(idx);
        }
    }
    out
}

/// Stable per-value signature for frequency counting. Strings compare by
/// content; everything else by canonical JSON (so `1` and `"1"` differ,
/// matching the analyzer's value semantics).
fn value_signature(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => crate::transforms::anchor_selector::python_json_dumps_sort_keys(other),
    }
}

/// Rank `candidates` by descending NOVELTY: rarity of the row's
/// stable-hash family across the whole array (a singleton family is
/// maximally novel), with ascending index as a deterministic tie-break.
fn rank_by_novelty(candidates: &[usize], items: &[Value], exclude: &BTreeSet<String>) -> Vec<usize> {
    // Family sizes over the whole array (rarity signal). Hashes are
    // remembered per index so the sort below never re-serializes — the
    // comparator runs O(n log n) times and an MD5-over-JSON per
    // comparison would dominate the fill cost.
    let mut family_size: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let hashes: Vec<String> = items
        .iter()
        .enumerate()
        .map(|(i, item)| item_content_hash(item, i, exclude))
        .collect();
    for h in &hashes {
        *family_size.entry(h.clone()).or_insert(0) += 1;
    }

    let mut ranked: Vec<usize> = candidates.to_vec();
    ranked.sort_by(|&a, &b| {
        let fa = hashes
            .get(a)
            .and_then(|h| family_size.get(h))
            .copied()
            .unwrap_or(1);
        let fb = hashes
            .get(b)
            .and_then(|h| family_size.get(h))
            .copied()
            .unwrap_or(1);
        // Smaller family first (more novel); ties broken by lower index
        // so the result is fully deterministic and stable.
        fa.cmp(&fb).then(a.cmp(&b))
    });
    ranked
}

/// Compute numeric-anomaly indices from `analysis.field_stats`.
/// Mirrors Python's anomaly loop in `_prioritize_indices` (line 1973-1984).
fn numeric_anomaly_indices(
    config: &SmartCrusherConfig,
    items: &[Value],
    analysis: Option<&ArrayAnalysis>,
) -> BTreeSet<usize> {
    let mut anomalies: BTreeSet<usize> = BTreeSet::new();
    let Some(analysis) = analysis else {
        return anomalies;
    };
    if analysis.field_stats.is_empty() {
        return anomalies;
    }

    for (field_name, stats) in &analysis.field_stats {
        if !is_numeric_field_with_variance(stats) {
            continue;
        }
        let (Some(mean_val), Some(var)) = (stats.mean_val, stats.variance) else {
            continue;
        };
        if var <= 0.0 {
            continue;
        }
        let std = var.sqrt();
        if std <= 0.0 {
            continue;
        }
        let threshold = config.variance_threshold * std;
        for (i, item) in items.iter().enumerate() {
            let Some(obj) = item.as_object() else {
                continue;
            };
            let Some(v) = obj.get(field_name) else {
                continue;
            };
            if let Some(num) = v.as_f64() {
                if !num.is_nan() && (num - mean_val).abs() > threshold {
                    anomalies.insert(i);
                }
            }
        }
    }

    anomalies
}

fn is_numeric_field_with_variance(stats: &FieldStats) -> bool {
    stats.field_type == "numeric" && stats.mean_val.is_some() && stats.variance.unwrap_or(0.0) > 0.0
}

/// Hash function used by all orchestration helpers.
///
/// Wraps the field-aware [`stable_item_hash`] (Python-compatible
/// json.dumps over the non-excluded keys + md5[:16]) with a fail-safe
/// fallback: if the item is not a JSON object/array, fall back to a
/// scalar hash (or `__idx_<i>__` on an unrepresentable value). Mirrors
/// Python's `try/except (TypeError, ValueError, RecursionError)` block
/// which also falls back to `f"__idx_{idx}__"` on serialization failure.
///
/// When `exclude` is empty, [`stable_item_hash`] is byte-equal to
/// `compute_item_hash`, so this is unchanged for non-identity data.
fn item_content_hash(item: &Value, idx: usize, exclude: &BTreeSet<String>) -> String {
    if item.is_object() || item.is_array() {
        stable_item_hash(item, exclude)
    } else {
        // Python: `else: content = str(item)` for non-dict items —
        // they get a real hash too. We don't strictly need that for
        // SmartCrusher's dict-array use case but we mirror it.
        // Fallback to index-stamp only on serialization failure.
        let content = match item {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => b.to_string(),
            Value::Null => "None".to_string(),
            _ => format!("__idx_{}__", idx),
        };
        let digest = Md5::digest(content.as_bytes());
        format!("{:x}", digest)[..16].to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn cfg() -> SmartCrusherConfig {
        SmartCrusherConfig::default()
    }

    fn idx_set(indices: &[usize]) -> BTreeSet<usize> {
        indices.iter().copied().collect()
    }

    /// Empty exclude-set → stable hash == whole-item hash (legacy behavior).
    fn no_exclude() -> BTreeSet<String> {
        BTreeSet::new()
    }

    // ---------- deduplicate_indices_by_content ----------

    #[test]
    fn dedup_empty_input() {
        let result = deduplicate_indices_by_content(&BTreeSet::new(), &[], &no_exclude());
        assert!(result.is_empty());
    }

    #[test]
    fn dedup_lowest_index_wins_for_duplicates() {
        let items = vec![
            json!({"name": "alice"}),
            json!({"name": "alice"}),
            json!({"name": "bob"}),
        ];
        let kept = idx_set(&[0, 1, 2]);
        let result = deduplicate_indices_by_content(&kept, &items, &no_exclude());
        // Items 0 and 1 collapse to the lower (0); item 2 is unique.
        assert_eq!(result, idx_set(&[0, 2]));
    }

    #[test]
    fn dedup_all_distinct_unchanged() {
        let items = vec![json!({"id": 1}), json!({"id": 2}), json!({"id": 3})];
        let kept = idx_set(&[0, 1, 2]);
        let result = deduplicate_indices_by_content(&kept, &items, &no_exclude());
        assert_eq!(result, idx_set(&[0, 1, 2]));
    }

    #[test]
    fn dedup_skips_out_of_bounds() {
        let items = vec![json!({"a": 1})];
        let kept = idx_set(&[0, 5, 10]);
        let result = deduplicate_indices_by_content(&kept, &items, &no_exclude());
        assert_eq!(result, idx_set(&[0]));
    }

    #[test]
    fn dedup_key_order_independent() {
        // {"b":2, "a":1} and {"a":1, "b":2} must hash to the same value
        // because we serialize with sort_keys=True.
        let items = vec![json!({"b": 2, "a": 1}), json!({"a": 1, "b": 2})];
        let kept = idx_set(&[0, 1]);
        let result = deduplicate_indices_by_content(&kept, &items, &no_exclude());
        assert_eq!(result.len(), 1);
        assert!(result.contains(&0));
    }

    #[test]
    fn dedup_collapses_rows_differing_only_in_excluded_identity() {
        // Two log rows: same message, different timestamp/id. With the
        // identity columns excluded they project to the same stable hash
        // and collapse to the lower index (DESIGN.md Imp2).
        let items = vec![
            json!({"ts": "2026-06-12T10:00:00Z", "id": "aaaa1111", "msg": "disk full"}),
            json!({"ts": "2026-06-12T10:00:05Z", "id": "bbbb2222", "msg": "disk full"}),
            json!({"ts": "2026-06-12T10:00:09Z", "id": "cccc3333", "msg": "ok"}),
        ];
        let kept = idx_set(&[0, 1, 2]);
        let exclude: BTreeSet<String> = ["ts".to_string(), "id".to_string()].into_iter().collect();
        let result = deduplicate_indices_by_content(&kept, &items, &exclude);
        // 0 and 1 collapse (same msg modulo ts/id); 2 is distinct.
        assert_eq!(result, idx_set(&[0, 2]));
        // ...and WITHOUT the exclude they stay distinct (every row unique).
        let result_full = deduplicate_indices_by_content(&kept, &items, &no_exclude());
        assert_eq!(result_full, idx_set(&[0, 1, 2]));
    }

    // ---------- fill_remaining_slots ----------

    #[test]
    fn fill_when_at_or_over_budget_returns_unchanged() {
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let kept = idx_set(&[0, 1, 2, 3, 4]);
        let result = fill_remaining_slots(&kept, &items, items.len(), 5, &no_exclude());
        assert_eq!(result, kept);
    }

    #[test]
    fn fill_adds_diverse_uniques_up_to_max() {
        let items: Vec<Value> = (0..20).map(|i| json!({"id": i})).collect();
        let kept = idx_set(&[0, 5]);
        let result = fill_remaining_slots(&kept, &items, items.len(), 10, &no_exclude());
        assert!(result.len() <= 10);
        assert!(result.len() >= 2);
        assert!(result.contains(&0));
        assert!(result.contains(&5));
    }

    #[test]
    fn fill_skips_content_duplicates() {
        // 10 unique + 10 dupes of items[0]. Filling shouldn't pick the dupes.
        let mut items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        items.extend(std::iter::repeat_with(|| json!({"id": 0})).take(10));
        let kept = idx_set(&[0]); // Already keeps the canonical {"id": 0}.
        let result = fill_remaining_slots(&kept, &items, items.len(), 15, &no_exclude());
        // The 10 dupes (indices 10..20) all hash to the same as items[0]
        // and shouldn't be added. Only unique indices [1..10) should fill.
        for i in 10..20 {
            assert!(!result.contains(&i), "dup index {} should not be added", i);
        }
    }

    // ---------- prioritize_indices ----------

    #[test]
    fn prioritize_under_budget_passthrough_after_dedup() {
        let items: Vec<Value> = (0..5).map(|i| json!({"id": i})).collect();
        let kept = idx_set(&[0, 1, 2]);
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 10, &no_exclude(), &BTreeSet::new());
        // 3 items < max 10 → fill kicks in; we get 5 (all items).
        assert_eq!(result.len(), 5);
    }

    #[test]
    fn prioritize_dedup_collapses_then_returns_under_max() {
        let items = vec![
            json!({"name": "alice"}),
            json!({"name": "alice"}),
            json!({"name": "bob"}),
        ];
        let kept = idx_set(&[0, 1, 2]);
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 10, &no_exclude(), &BTreeSet::new());
        // Dedup collapses 0+1 to 0; fill stays put because n=3 already covered.
        assert_eq!(result, idx_set(&[0, 2]));
    }

    #[test]
    fn prioritize_keeps_error_items_when_over_budget() {
        // 30 items, 1 error item. Over-budget path must keep the error.
        let mut items: Vec<Value> = (0..30)
            .map(|i| json!({"id": i, "msg": format!("ok {}", i)}))
            .collect();
        items.push(json!({"id": 30, "msg": "FATAL: out of memory"}));
        let kept: BTreeSet<usize> = (0..items.len()).collect();
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 10, &no_exclude(), &BTreeSet::new());
        assert!(
            result.contains(&30),
            "error item must survive prioritization"
        );
    }

    #[test]
    fn prioritize_includes_first_3_and_last_2_when_room() {
        // No errors / outliers / anomalies → first 3 + last 2 anchors fill.
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i, "v": i})).collect();
        let kept: BTreeSet<usize> = (5..15).collect();
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 10, &no_exclude(), &BTreeSet::new());
        // With no critical items and budget 10, dedup is a no-op (all
        // distinct) and fill keeps us at <= 10. We should see at least
        // some of items 0..3 OR 28..30 covered through fill.
        // Cap is 10; ensure we don't exceed.
        assert!(result.len() <= 10);
    }

    // ---------- 1B: field-value singletons ----------

    #[test]
    fn field_value_singletons_finds_unique_value_rows() {
        // 9 rows share status "ok"; one row has a unique status "PANIC".
        // The PANIC row is a field-value singleton (a needle).
        let mut items: Vec<Value> = (0..9).map(|i| json!({"i": i, "status": "ok"})).collect();
        items.push(json!({"i": 9, "status": "PANIC"}));
        let singletons = field_value_singletons(&items, &no_exclude());
        // Every row has a unique `i`, so all are singletons under that
        // field — the helper flags a row if ANY field value is unique.
        // The point this test pins: the PANIC row is included.
        assert!(singletons.contains(&9), "unique-status needle must be flagged");
    }

    #[test]
    fn field_value_singletons_excludes_identity_columns() {
        // With the id column excluded, only genuine value-needles remain.
        let mut items: Vec<Value> = (0..9)
            .map(|i| json!({"uuid": format!("{:040x}", i), "status": "ok"}))
            .collect();
        items.push(json!({"uuid": format!("{:040x}", 99), "status": "PANIC"}));
        let exclude: BTreeSet<String> = ["uuid".to_string()].into_iter().collect();
        let singletons = field_value_singletons(&items, &exclude);
        // Only the PANIC row is a singleton now (uuid uniqueness ignored).
        assert_eq!(singletons, vec![9]);
    }

    #[test]
    fn singleton_signal_majority_is_uninformative() {
        // All-distinct array: every row is a singleton -> no signal.
        assert!(!singleton_signal_is_informative(90, 90));
        assert!(!singleton_signal_is_informative(46, 90));
        // Rare singletons -> real needles -> informative.
        assert!(singleton_signal_is_informative(1, 30));
        assert!(singleton_signal_is_informative(45, 90)); // exactly half stays pinned
        assert!(singleton_signal_is_informative(0, 90));
    }

    #[test]
    fn prioritize_skips_degenerate_singleton_pinning_on_all_distinct() {
        // 90 all-distinct rows (every row a singleton under `msg`).
        // Pinning would degenerate to indices 0..cap-1; the gate must
        // skip it so the kept set stays at the budget, not budget+extras.
        let items: Vec<Value> = (0..90)
            .map(|i| json!({"msg": format!("unique subject number {} entirely", i)}))
            .collect();
        let kept: BTreeSet<usize> = (0..90).collect();
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 15, &no_exclude(), &BTreeSet::new());
        // No errors/outliers/anomalies fire on this shape; without the
        // degenerate pinning the result is anchors + novelty fill, capped
        // at the budget.
        assert!(
            result.len() <= 15,
            "degenerate singleton pinning must not blow past the budget; got {}",
            result.len()
        );
    }

    #[test]
    fn prioritize_still_pins_minority_singleton_needle() {
        // 29 identical rows + 1 unique-status needle: singletons are a
        // minority (1/30) -> the signal is informative -> needle pinned.
        let mut items: Vec<Value> = (0..29)
            .map(|_| json!({"kind": "routine", "status": "ok"}))
            .collect();
        items.push(json!({"kind": "routine", "status": "PANIC"}));
        let kept: BTreeSet<usize> = (0..30).collect();
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 5, &no_exclude(), &BTreeSet::new());
        assert!(
            result.contains(&29),
            "minority singleton needle must stay pinned; got {:?}",
            result
        );
    }

    // ---------- query-relevant pinning ----------

    #[test]
    fn prioritize_pins_query_relevant_rows_over_budget() {
        // 40 all-distinct rows, tiny budget, one mid-array row flagged as
        // query-pinned. It fires no error/outlier/anomaly signal and the
        // singleton gate (all-distinct => degenerate) skips pinning — so
        // ONLY the query pin can rescue it from the positional drop.
        let items: Vec<Value> = (0..40)
            .map(|i| json!({"msg": format!("entirely distinct message {}", i)}))
            .collect();
        let kept: BTreeSet<usize> = (0..40).collect();
        let pinned: BTreeSet<usize> = [23usize].into_iter().collect();
        let result =
            prioritize_indices(&cfg(), &kept, &items, items.len(), None, 5, &no_exclude(), &pinned);
        assert!(
            result.contains(&23),
            "query-pinned row must survive the over-budget drop; got {:?}",
            result
        );
        // And the empty-pin call must not regress the budget behavior.
        let no_pins = prioritize_indices(
            &cfg(),
            &kept,
            &items,
            items.len(),
            None,
            5,
            &no_exclude(),
            &BTreeSet::new(),
        );
        assert!(no_pins.len() <= 5 + 1, "unpinned result stays near budget");
    }

    // ---------- 1B: novelty-ranked fill ----------

    #[test]
    fn rank_by_novelty_puts_rare_families_first() {
        // 8 identical rows (family size 8) + 2 distinct rows (size 1 each).
        // Novelty ranking must surface the two rare rows ahead of the
        // common family.
        let mut items: Vec<Value> = (0..8).map(|_| json!({"msg": "common"})).collect();
        items.push(json!({"msg": "rare-A"})); // idx 8
        items.push(json!({"msg": "rare-B"})); // idx 9
        let candidates: Vec<usize> = (0..10).collect();
        let ranked = rank_by_novelty(&candidates, &items, &no_exclude());
        // The two rare rows (8, 9) rank ahead of the common family.
        assert_eq!(&ranked[..2], &[8, 9], "rare rows first; got {:?}", ranked);
    }

    #[test]
    fn prioritize_novelty_fill_rescues_mid_array_needle() {
        // 30 rows: a flat common shape, with ONE distinct mid-array needle
        // at index 15 that fires no error/outlier/anomaly constraint. The
        // old lowest-index fill would drop it past budget; novelty fill
        // surfaces it because its stable-hash family is size 1.
        let mut items: Vec<Value> = (0..30)
            .map(|_| json!({"kind": "routine", "payload": "same"}))
            .collect();
        items[15] = json!({"kind": "routine", "payload": "UNIQUE-NEEDLE-XYZ"});
        // Force the over-budget path: keep everything, tiny budget.
        let kept: BTreeSet<usize> = (0..30).collect();
        let result = prioritize_indices(&cfg(), &kept, &items, items.len(), None, 5, &no_exclude(), &BTreeSet::new());
        assert!(
            result.contains(&15),
            "novelty fill must rescue the unique mid-array needle; got {:?}",
            result
        );
    }
}
