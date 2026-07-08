//! Dynamic anchor selection for array compression.
//!
//! Direct port of `furl_ctx/transforms/anchor_selector.py`. Used by
//! `smart_crusher::analyzer` (and the not-yet-ported planning layer)
//! to allocate position-based anchor slots — the items that are kept
//! purely for their position in the array, not their relevance score.
//!
//! # What it does
//!
//! Given an array of N items and a target K (max items after compression),
//! decide which K' < K positions to "anchor" (always keep). The choice
//! depends on:
//!
//! 1. **Pattern**: search results favor the front; logs favor the back;
//!    time series want both ends; generic spreads evenly.
//! 2. **Query keywords**: "latest" / "recent" → shift toward back;
//!    "first" / "earliest" → shift toward front.
//! 3. **Information density** (middle region only): compute a [0,1]
//!    score per candidate based on field-value uniqueness, content
//!    length, and structural uniqueness.
//! 4. **Dedup**: identical items hash to the same MD5[:16]; duplicates
//!    are skipped so we don't waste slots.
//!
//! # Hash parity with Python
//!
//! `compute_item_hash` returns `md5(json.dumps(item, sort_keys=True,
//! default=str)).hexdigest()[:16]`. Python's `json.dumps` by default
//! emits `", "` and `": "` separators and ASCII-escapes non-ASCII via
//! `\uXXXX`. The byte-exact serializers live in [`crate::util::pyjson`]
//! (ARCH-8); mismatching the format would silently change which items
//! are considered duplicates, so it's load-bearing for parity fixtures.

use md5::{Digest, Md5};
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet};

use crate::util::pyjson::{python_json_dumps_sort_keys, python_json_dumps_sort_keys_filtered};

// ============================================================================
// Configuration (Python `furl_ctx/config.py:294` AnchorConfig)
// ============================================================================

/// Configuration for dynamic anchor allocation.
///
/// Direct port of Python `AnchorConfig` (`furl_ctx/config.py:294-348`).
/// Defaults must match Python byte-for-byte — they're consulted by
/// every anchor decision and parity fixtures lock the resulting choices.
#[derive(Debug, Clone)]
pub struct AnchorConfig {
    /// Base anchor budget as percentage of `max_items`. Default 0.25.
    pub anchor_budget_pct: f64,
    pub min_anchor_slots: usize,
    pub max_anchor_slots: usize,

    pub default_front_weight: f64,
    pub default_back_weight: f64,
    pub default_middle_weight: f64,

    pub search_front_weight: f64,
    pub search_back_weight: f64,
    pub logs_front_weight: f64,
    pub logs_back_weight: f64,

    /// Query keywords that shift the weight distribution toward the
    /// back of the array (recent items). Lowercase substring match.
    pub recency_keywords: Vec<&'static str>,
    /// Query keywords that shift toward the front (older items).
    pub historical_keywords: Vec<&'static str>,

    pub use_information_density: bool,
    /// Considers `num_slots * candidate_multiplier` candidates when
    /// using density-based selection.
    pub candidate_multiplier: usize,
    pub dedup_identical_items: bool,
}

impl Default for AnchorConfig {
    fn default() -> Self {
        AnchorConfig {
            anchor_budget_pct: 0.25,
            min_anchor_slots: 3,
            max_anchor_slots: 12,
            default_front_weight: 0.5,
            default_back_weight: 0.4,
            default_middle_weight: 0.1,
            search_front_weight: 0.75,
            search_back_weight: 0.15,
            logs_front_weight: 0.15,
            logs_back_weight: 0.75,
            recency_keywords: vec!["latest", "recent", "last", "newest", "current", "now"],
            historical_keywords: vec![
                "first",
                "oldest",
                "earliest",
                "original",
                "initial",
                "beginning",
            ],
            use_information_density: true,
            candidate_multiplier: 3,
            dedup_identical_items: true,
        }
    }
}

// ============================================================================
// Enums (Python `DataPattern`, `AnchorStrategy`)
// ============================================================================

/// Detected data pattern. Drives anchor strategy selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataPattern {
    SearchResults,
    Logs,
    TimeSeries,
    Generic,
}

impl DataPattern {
    /// Mirrors `DataPattern.from_string` in Python — unknown strings
    /// fall through to `Generic`.
    pub fn from_string(s: &str) -> DataPattern {
        match s.to_lowercase().as_str() {
            "search_results" => DataPattern::SearchResults,
            "logs" => DataPattern::Logs,
            "time_series" => DataPattern::TimeSeries,
            "generic" => DataPattern::Generic,
            _ => DataPattern::Generic,
        }
    }

    /// Snake-case label, byte-identical to the strings the analyzer's
    /// `detect_pattern` historically produced (and `from_string`
    /// accepts) — `ArrayAnalysis.detected_pattern` is typed as this enum
    /// (TYPE-1), so any rendered form must round-trip these exact bytes.
    pub fn as_str(self) -> &'static str {
        match self {
            DataPattern::SearchResults => "search_results",
            DataPattern::Logs => "logs",
            DataPattern::TimeSeries => "time_series",
            DataPattern::Generic => "generic",
        }
    }
}

/// Anchor distribution strategy. Determined by pattern via
/// `AnchorSelector::strategy_for_pattern`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnchorStrategy {
    FrontHeavy,
    BackHeavy,
    Balanced,
    Distributed,
}

/// Distribution weights for the front / middle / back regions of the
/// array. Should sum to 1.0; `normalize()` enforces it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AnchorWeights {
    pub front: f64,
    pub middle: f64,
    pub back: f64,
}

impl Default for AnchorWeights {
    fn default() -> Self {
        AnchorWeights {
            front: 0.5,
            middle: 0.1,
            back: 0.4,
        }
    }
}

impl AnchorWeights {
    /// Return a copy with weights normalized to sum to 1.0. If the
    /// total is 0, returns `default()` (the same fallback Python uses).
    pub fn normalize(&self) -> AnchorWeights {
        let total = self.front + self.middle + self.back;
        if total == 0.0 {
            return AnchorWeights::default();
        }
        AnchorWeights {
            front: self.front / total,
            middle: self.middle / total,
            back: self.back / total,
        }
    }
}

// ============================================================================
// Information density scoring
// ============================================================================

/// Region-global aggregates for information scoring, computed ONCE over a
/// candidate region and reused for every item scored against it.
///
/// The three [`calculate_information_score`] factors each derive a
/// region-wide summary from `all_items` (identical for every item in the
/// region) and then compare a single `item` against it. Scoring K
/// candidates against the same region recomputed those summaries K times —
/// each an O(region) pass that re-serialized heavy fields (e.g. a Chrome
/// trace's `args` dict). `RegionProfile` hoists the shared passes out of
/// the per-candidate loop: build once (O(region)), score each candidate in
/// O(item fields). The per-item arithmetic below is byte-for-byte the old
/// per-call code — same counts, same min/max, same threshold sets — so the
/// resulting scores are bit-identical and the candidate sort is unchanged.
struct RegionProfile {
    /// `all_items.len()` — the divisor for value-rareness and the `< 2`
    /// guards. Counts ALL items (not just objects), matching the ports.
    total_items: usize,
    /// Per-field value→count over every object item (value-uniqueness).
    field_value_counts: HashMap<String, HashMap<String, usize>>,
    /// Min/max serialized length over OBJECT items (length-score). `None`
    /// when no object item exists (the `lengths.is_empty()` guard).
    length_bounds: Option<(usize, usize)>,
    /// Object-item count (structural denominator `n`).
    object_count: usize,
    /// Fields present in ≥80% of object items (structural `common`).
    common_fields: HashSet<String>,
    /// Fields present in <20% of object items (structural `rare`).
    rare_fields: HashSet<String>,
}

impl RegionProfile {
    /// Build the aggregates in a single pass over `all_items`. Mirrors the
    /// setup halves of `calculate_value_uniqueness`,
    /// `calculate_length_score`, and `calculate_structural_uniqueness`.
    fn build(all_items: &[Value]) -> Self {
        let total_items = all_items.len();

        // Value-uniqueness field counts + structural field presence +
        // length bounds, all in one walk over the region.
        let mut field_value_counts: HashMap<String, HashMap<String, usize>> = HashMap::new();
        let mut structural_field_counts: HashMap<String, usize> = HashMap::new();
        let mut object_count: usize = 0;
        let mut min_length: Option<usize> = None;
        let mut max_length: Option<usize> = None;

        for other in all_items {
            let Some(obj) = other.as_object() else {
                continue;
            };
            object_count += 1;

            // length-score corpus: serialized length of each object item.
            let len = serde_json::to_string(other).map(|s| s.len()).unwrap_or(0);
            min_length = Some(min_length.map_or(len, |m: usize| m.min(len)));
            max_length = Some(max_length.map_or(len, |m: usize| m.max(len)));

            for (key, value) in obj {
                // value-uniqueness: per-field value tally.
                let value_str = stringify_for_uniqueness(value);
                field_value_counts
                    .entry(key.clone())
                    .or_default()
                    .entry(value_str)
                    .and_modify(|c| *c += 1)
                    .or_insert(1);
                // structural: per-field presence tally.
                *structural_field_counts.entry(key.clone()).or_insert(0) += 1;
            }
        }

        // structural common/rare classification (thresholds on object_count).
        let n_f = object_count as f64;
        let common_fields: HashSet<String> = structural_field_counts
            .iter()
            .filter(|(_, &c)| c as f64 >= n_f * 0.8)
            .map(|(k, _)| (*k).clone())
            .collect();
        let rare_fields: HashSet<String> = structural_field_counts
            .iter()
            .filter(|(_, &c)| (c as f64) < n_f * 0.2)
            .map(|(k, _)| (*k).clone())
            .collect();

        let length_bounds = match (min_length, max_length) {
            (Some(mn), Some(mx)) => Some((mn, mx)),
            _ => None,
        };

        RegionProfile {
            total_items,
            field_value_counts,
            length_bounds,
            object_count,
            common_fields,
            rare_fields,
        }
    }

    /// Per-item value-rareness. Mirrors the scoring half of
    /// `calculate_value_uniqueness`.
    fn value_uniqueness(&self, item: &Value) -> f64 {
        if self.total_items < 2 {
            return 0.5;
        }
        let item_obj = match item.as_object() {
            Some(o) => o,
            None => return 0.5,
        };
        let total_items = self.total_items as f64;
        let mut rareness_scores: Vec<f64> = Vec::new();
        for (key, value) in item_obj {
            let Some(counts) = self.field_value_counts.get(key) else {
                continue;
            };
            let value_str = stringify_for_uniqueness(value);
            let count = counts.get(&value_str).copied().unwrap_or(0);
            if count > 0 {
                let frequency = count as f64 / total_items;
                rareness_scores.push(1.0 - frequency);
            }
        }
        if rareness_scores.is_empty() {
            return 0.5;
        }
        rareness_scores.iter().sum::<f64>() / rareness_scores.len() as f64
    }

    /// Per-item length score. Mirrors the scoring half of
    /// `calculate_length_score`.
    fn length_score(&self, item: &Value) -> f64 {
        if self.total_items < 2 {
            return 0.5;
        }
        let (min_length, max_length) = match self.length_bounds {
            Some(b) => b,
            None => return 0.5,
        };
        if max_length == min_length {
            return 0.5;
        }
        let item_length = serde_json::to_string(item)
            .map(|s| s.len())
            .unwrap_or_else(|_| format!("{}", item).len());
        (item_length as f64 - min_length as f64) / (max_length as f64 - min_length as f64)
    }

    /// Per-item structural uniqueness. Mirrors the scoring half of
    /// `calculate_structural_uniqueness`.
    fn structural_uniqueness(&self, item: &Value) -> f64 {
        if self.object_count < 2 {
            return 0.5;
        }
        let item_fields: HashSet<&str> = item
            .as_object()
            .map(|o| o.keys().map(String::as_str).collect())
            .unwrap_or_default();

        // `has_rare` = |item_fields ∩ rare|; `missing_common` =
        // |common \ item_fields| — same set operations as the port, just
        // with the region sets precomputed as owned `String`s.
        let has_rare = self
            .rare_fields
            .iter()
            .filter(|k| item_fields.contains(k.as_str()))
            .count();
        let missing_common = self
            .common_fields
            .iter()
            .filter(|k| !item_fields.contains(k.as_str()))
            .count();

        let mut uniqueness = 0.0;
        if !self.rare_fields.is_empty() {
            uniqueness += 0.5 * (has_rare as f64 / self.rare_fields.len().max(1) as f64);
        }
        if !self.common_fields.is_empty() {
            uniqueness += 0.5 * (missing_common as f64 / self.common_fields.len().max(1) as f64);
        }
        uniqueness.min(1.0)
    }

    /// Combine the three factors — the hard-coded Python weights.
    fn score(&self, item: &Value) -> f64 {
        if self.total_items == 0 {
            return 0.0;
        }
        let Some(_) = item.as_object() else {
            return 0.0;
        };
        let uniqueness = self.value_uniqueness(item);
        let length = self.length_score(item);
        let structural = self.structural_uniqueness(item);
        let score = uniqueness * 0.4 + length * 0.3 + structural * 0.3;
        score.clamp(0.0, 1.0)
    }
}

/// Information density score for an item, in `[0.0, 1.0]`.
///
/// Combines three factors with hard-coded Python weights:
/// - 0.4: field-value rareness (rare values → higher score).
/// - 0.3: content length (relative to the corpus).
/// - 0.3: structural uniqueness (rare/missing fields).
///
/// Direct port of `calculate_information_score`
/// (Python `anchor_selector.py:132-175`). Thin wrapper over
/// [`RegionProfile`] — builds the region aggregates then scores `item`, so
/// a single-item caller stays byte-identical while the hot region loop
/// ([`AnchorSelector::select_by_density`]) reuses one profile across
/// candidates.
pub fn calculate_information_score(item: &Value, all_items: &[Value]) -> f64 {
    if all_items.is_empty() {
        return 0.0;
    }
    let Some(_) = item.as_object() else {
        return 0.0;
    };
    RegionProfile::build(all_items).score(item)
}

/// Stringification used for uniqueness counting. Python:
///   `json.dumps(value, sort_keys=True) if not isinstance(value, str) else value`
/// Mirror that exactly: bare strings stay bare; everything else uses the
/// Python-compatible sort-keys serializer (`util::pyjson`).
fn stringify_for_uniqueness(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        _ => python_json_dumps_sort_keys(value),
    }
}

// ============================================================================
// Item hashing (with Python-compatible JSON serialization)
// ============================================================================

/// Compute a 16-hex-char MD5 hash of the item's content for dedup.
///
/// Python: `md5(json.dumps(item, sort_keys=True, default=str)).hexdigest()[:16]`.
/// The serialization MUST match Python byte-for-byte — different
/// formatting → different hash → different dedup behavior.
pub fn compute_item_hash(item: &Value) -> String {
    let content = python_json_dumps_sort_keys(item);
    let digest = Md5::digest(content.as_bytes());
    // Per-byte `{:02x}` (mirrors the sha2 sites) instead of
    // `format!("{:x}", digest)`: digest 0.11 returns `hybrid_array::Array`,
    // which does not implement `LowerHex` (the old `GenericArray` did).
    // Byte-identical: 16 hex chars = first 8 digest bytes, and `LowerHex`
    // on the array was zero-padded per-byte hex — exactly `{:02x}`.
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// Field-aware **stable-projection** hash (DESIGN.md Improvement 2).
///
/// Identical to [`compute_item_hash`] EXCEPT that, when `item` is a JSON
/// object, any top-level key in `exclude` is omitted from the serialization
/// before hashing. This is the dedup/cluster/fill grouping hash: by dropping
/// high-cardinality identity columns (timestamps, ids, hashes) two rows that
/// differ ONLY in those columns project to the same bytes and collapse.
///
/// **This is a SEPARATE hash from [`compute_item_hash`].** The full-item
/// canonical hash used for the CCR retrieve key is unchanged — only this
/// projection hash filters keys. When `exclude` is empty the output is
/// byte-identical to [`compute_item_hash`] (same serializer, same key set),
/// so non-identity data is completely unaffected and parity is preserved.
///
/// Exclusion applies only at the top level of an object item — nested objects
/// are serialized whole (an identity column is a top-level field of a row).
pub fn stable_item_hash(item: &Value, exclude: &BTreeSet<String>) -> String {
    // No exclusions, or not an object → identical to the full-item hash.
    if exclude.is_empty() || !item.is_object() {
        return compute_item_hash(item);
    }
    let out = python_json_dumps_sort_keys_filtered(item, exclude);
    let digest = Md5::digest(out.as_bytes());
    // Per-byte `{:02x}` (digest 0.11 `Array` has no `LowerHex`); byte-identical
    // to the old `format!("{:x}", digest)[..16]` — see `compute_item_hash`.
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

// ============================================================================
// AnchorSelector — the main selector
// ============================================================================

/// Dynamic anchor selector. Stateless other than `config`.
pub struct AnchorSelector {
    pub config: AnchorConfig,
}

impl AnchorSelector {
    pub fn new(config: AnchorConfig) -> Self {
        AnchorSelector { config }
    }

    /// Calculate the anchor budget — number of slots to allocate.
    /// Mirrors `calculate_anchor_budget` (Python `anchor_selector.py:364-391`).
    pub fn calculate_anchor_budget(&self, array_size: usize, max_items: usize) -> usize {
        if array_size <= max_items {
            return 0;
        }
        // Python: `int(max_items * pct)` truncates toward zero.
        let raw = (max_items as f64 * self.config.anchor_budget_pct) as usize;
        let mut budget = self.config.min_anchor_slots.max(raw);
        budget = self.config.max_anchor_slots.min(budget);
        budget.min(array_size)
    }

    pub fn strategy_for_pattern(&self, pattern: DataPattern) -> AnchorStrategy {
        match pattern {
            DataPattern::SearchResults => AnchorStrategy::FrontHeavy,
            DataPattern::Logs => AnchorStrategy::BackHeavy,
            DataPattern::TimeSeries => AnchorStrategy::Balanced,
            DataPattern::Generic => AnchorStrategy::Distributed,
        }
    }

    pub fn base_weights_for_strategy(&self, strategy: AnchorStrategy) -> AnchorWeights {
        match strategy {
            AnchorStrategy::FrontHeavy => AnchorWeights {
                front: self.config.search_front_weight,
                middle: 1.0 - self.config.search_front_weight - self.config.search_back_weight,
                back: self.config.search_back_weight,
            },
            AnchorStrategy::BackHeavy => AnchorWeights {
                front: self.config.logs_front_weight,
                middle: 1.0 - self.config.logs_front_weight - self.config.logs_back_weight,
                back: self.config.logs_back_weight,
            },
            AnchorStrategy::Balanced => AnchorWeights {
                front: 0.45,
                middle: 0.1,
                back: 0.45,
            },
            AnchorStrategy::Distributed => AnchorWeights {
                front: self.config.default_front_weight,
                middle: self.config.default_middle_weight,
                back: self.config.default_back_weight,
            },
        }
    }

    /// Adjust weights based on query keywords. `+0.15` toward back on
    /// recency keywords, `+0.15` toward front on historical. Returns
    /// `base_weights` unchanged when no keywords match (or both match —
    /// they cancel out).
    pub fn adjust_weights_for_query(
        &self,
        base: AnchorWeights,
        query: Option<&str>,
    ) -> AnchorWeights {
        let Some(query) = query.filter(|q| !q.is_empty()) else {
            return base;
        };
        let q_lower = query.to_lowercase();
        let has_recency = self
            .config
            .recency_keywords
            .iter()
            .any(|kw| q_lower.contains(kw));
        let has_historical = self
            .config
            .historical_keywords
            .iter()
            .any(|kw| q_lower.contains(kw));

        let shift = 0.15;
        if has_recency && !has_historical {
            AnchorWeights {
                front: 0.1_f64.max(base.front - shift),
                middle: base.middle,
                back: 0.8_f64.min(base.back + shift),
            }
            .normalize()
        } else if has_historical && !has_recency {
            AnchorWeights {
                front: 0.8_f64.min(base.front + shift),
                middle: base.middle,
                back: 0.1_f64.max(base.back - shift),
            }
            .normalize()
        } else {
            base
        }
    }

    /// Main entry: select anchor indices for an array.
    pub fn select_anchors(
        &self,
        items: &[Value],
        max_items: usize,
        pattern: DataPattern,
        query: Option<&str>,
    ) -> BTreeSet<usize> {
        let array_size = items.len();
        if array_size == 0 {
            return BTreeSet::new();
        }
        if array_size <= max_items {
            return (0..array_size).collect();
        }

        let budget = self.calculate_anchor_budget(array_size, max_items);
        if budget == 0 {
            return BTreeSet::new();
        }

        let strategy = self.strategy_for_pattern(pattern);
        let base = self.base_weights_for_strategy(strategy);
        let weights = self.adjust_weights_for_query(base, query).normalize();

        // Slot allocation. Python: max(1, int(budget * weight)).
        let front_slots = 1.max((budget as f64 * weights.front) as usize);
        let mut back_slots = 1.max((budget as f64 * weights.back) as usize);
        let mut middle_slots = budget.saturating_sub(front_slots + back_slots);

        // Ensure we don't exceed budget — reduce middle first, then back.
        let total = front_slots + middle_slots + back_slots;
        if total > budget {
            let mut excess = total - budget;
            let middle_reduction = middle_slots.min(excess);
            middle_slots -= middle_reduction;
            excess -= middle_reduction;
            if excess > 0 {
                back_slots = 1.max(back_slots.saturating_sub(excess));
            }
        }

        let mut anchors: BTreeSet<usize> = BTreeSet::new();
        let mut seen: HashSet<String> = HashSet::new();

        // Front region: [0, min(front_slots*2, array_size/3))
        let front_end = (front_slots * 2).min(array_size / 3);
        let front_anchors = self.select_region(items, 0, front_end, front_slots, &mut seen, false);
        let front_count = front_anchors.len();
        anchors.extend(front_anchors.iter().copied());

        // Back region: [max(array_size - back_slots*2, 2*array_size/3), array_size)
        let back_start = array_size
            .saturating_sub(back_slots * 2)
            .max((2 * array_size) / 3);
        let back_anchors =
            self.select_region(items, back_start, array_size, back_slots, &mut seen, false);
        let back_count = back_anchors.len();
        anchors.extend(back_anchors.iter().copied());

        // Middle region: [front_count, array_size - back_count)
        // Note Python uses `len(front_anchors)` and `len(back_anchors)` — the
        // ACTUAL counts after dedup, not the slot-allocated counts. We mirror.
        if middle_slots > 0 {
            let middle_start = front_count;
            let middle_end = array_size.saturating_sub(back_count);
            if middle_end > middle_start {
                let middle_anchors = self.select_region(
                    items,
                    middle_start,
                    middle_end,
                    middle_slots,
                    &mut seen,
                    self.config.use_information_density,
                );
                anchors.extend(middle_anchors);
            }
        }

        anchors
    }

    fn select_region(
        &self,
        items: &[Value],
        start_idx: usize,
        end_idx: usize,
        num_slots: usize,
        seen: &mut HashSet<String>,
        use_density: bool,
    ) -> BTreeSet<usize> {
        let mut selected = BTreeSet::new();
        if num_slots == 0 || start_idx >= end_idx {
            return selected;
        }
        let region_size = end_idx - start_idx;

        if !use_density {
            if num_slots >= region_size {
                // Take all (with dedup).
                for idx in start_idx..end_idx {
                    if self.should_include(items, idx, seen, false) {
                        selected.insert(idx);
                    }
                }
            } else {
                let step = region_size as f64 / (num_slots + 1) as f64;
                for i in 0..num_slots {
                    let raw_idx = start_idx + ((i + 1) as f64 * step) as usize;
                    let idx = raw_idx.min(end_idx - 1);
                    if self.should_include(items, idx, seen, false) {
                        selected.insert(idx);
                    } else {
                        // Try adjacent indices.
                        for &offset in &[1_isize, -1, 2, -2] {
                            let alt = (idx as isize) + offset;
                            if alt < start_idx as isize || alt >= end_idx as isize {
                                continue;
                            }
                            let alt = alt as usize;
                            if self.should_include(items, alt, seen, false) {
                                selected.insert(alt);
                                break;
                            }
                        }
                    }
                }
            }
        } else {
            selected = self.select_by_density(items, start_idx, end_idx, num_slots, seen);
        }

        selected
    }

    fn select_by_density(
        &self,
        items: &[Value],
        start_idx: usize,
        end_idx: usize,
        num_slots: usize,
        seen: &mut HashSet<String>,
    ) -> BTreeSet<usize> {
        let region_size = end_idx - start_idx;
        let num_candidates = (num_slots * self.config.candidate_multiplier).min(region_size);
        let step = if num_candidates > 0 {
            region_size as f64 / (num_candidates + 1) as f64
        } else {
            1.0
        };

        // Borrow the region directly — scoring only reads, so deep-cloning
        // every `Value` in the region per call was pure allocation
        // overhead.
        let region_items: &[Value] = &items[start_idx..end_idx];
        // The region aggregates (per-field value counts, length bounds,
        // structural common/rare sets) are identical for every candidate in
        // this region — build them ONCE instead of recomputing (and
        // re-serializing the whole region) per candidate. Byte-identical:
        // `RegionProfile::score` reproduces `calculate_information_score`
        // exactly (the non-empty-region / object-item guards below still
        // hold, so the wrapper's early 0.0 returns never applied here).
        let profile = RegionProfile::build(region_items);
        let mut candidates: Vec<(usize, f64)> = Vec::new();

        for i in 0..num_candidates {
            let raw = start_idx + ((i + 1) as f64 * step) as usize;
            let idx = raw.min(end_idx - 1);
            if !self.should_include(items, idx, seen, true) {
                continue;
            }
            let item = &items[idx];
            let score = if item.is_object() {
                profile.score(item)
            } else {
                0.5
            };
            candidates.push((idx, score));
        }

        // Sort by score descending; ties broken by index ascending so
        // results are deterministic (Python's sort is stable, but since
        // we're sorting on tuples (idx, score) the input order matters —
        // we built candidates in increasing-idx order so stable sort
        // yields the same effect).
        candidates.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        let mut selected = BTreeSet::new();
        for (idx, _) in candidates.into_iter().take(num_slots) {
            if self.should_include(items, idx, seen, false) {
                selected.insert(idx);
            }
        }
        selected
    }

    fn should_include(
        &self,
        items: &[Value],
        idx: usize,
        seen: &mut HashSet<String>,
        check_only: bool,
    ) -> bool {
        if !self.config.dedup_identical_items {
            return true;
        }
        if idx >= items.len() {
            return false;
        }
        let item = &items[idx];
        if !item.is_object() {
            return true;
        }
        let h = compute_item_hash(item);
        if seen.contains(&h) {
            return false;
        }
        if !check_only {
            seen.insert(h);
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn cfg() -> AnchorConfig {
        AnchorConfig::default()
    }
    fn selector() -> AnchorSelector {
        AnchorSelector::new(cfg())
    }

    // The Python-json.dumps serializer parity tests moved to
    // `crate::util::pyjson::tests` with the serializers (ARCH-8). The
    // hash tests below still transitively exercise the sort-keys
    // serializer byte-for-byte (the MD5 vectors are Python-computed).

    // ---------- compute_item_hash ----------

    #[test]
    fn compute_item_hash_deterministic() {
        let h1 = compute_item_hash(&json!({"a": 1, "b": 2}));
        let h2 = compute_item_hash(&json!({"b": 2, "a": 1}));
        assert_eq!(h1, h2, "hash is independent of key insertion order");
    }

    #[test]
    fn compute_item_hash_matches_python_basic() {
        // Reference verified via Python:
        //   hashlib.md5(json.dumps({"a":1,"b":2}, sort_keys=True).encode()).hexdigest()[:16]
        //   = "8aacdb17187e6acf"
        assert_eq!(
            compute_item_hash(&json!({"a": 1, "b": 2})),
            "8aacdb17187e6acf"
        );
    }

    #[test]
    fn compute_item_hash_matches_python_with_unicode() {
        // Reference: hashlib.md5(json.dumps({"k":"café"}, sort_keys=True).encode())
        //   .hexdigest()[:16] = "6761da28ed7eb489"
        assert_eq!(compute_item_hash(&json!({"k": "café"})), "6761da28ed7eb489");
    }

    #[test]
    fn compute_item_hash_format_16_hex_chars() {
        let h = compute_item_hash(&json!({"x": 1}));
        assert_eq!(h.len(), 16);
        assert!(
            h.chars().all(|c| c.is_ascii_hexdigit()),
            "hash {} must be hex",
            h
        );
    }

    // ---------- stable_item_hash (field-aware projection) ----------

    #[test]
    fn stable_hash_empty_exclude_equals_full_hash() {
        // CONTRACT: empty exclude-set => byte-identical to compute_item_hash.
        // This is what preserves Python/Rust parity and leaves non-identity
        // data (search, code) completely unaffected.
        let item = json!({"a": 1, "b": "x", "c": [1, 2, 3]});
        let empty = BTreeSet::new();
        assert_eq!(stable_item_hash(&item, &empty), compute_item_hash(&item));
    }

    #[test]
    fn stable_hash_collapses_rows_differing_only_in_excluded_fields() {
        // Two rows identical except the excluded identity columns -> same
        // stable hash. This is the dedup win (DESIGN.md Imp2).
        let a = json!({"ts": "2026-06-12T10:00:00Z", "id": "aaaa", "msg": "disk full"});
        let b = json!({"ts": "2026-06-12T10:00:09Z", "id": "bbbb", "msg": "disk full"});
        let exclude: BTreeSet<String> = ["ts".to_string(), "id".to_string()].into_iter().collect();
        assert_eq!(
            stable_item_hash(&a, &exclude),
            stable_item_hash(&b, &exclude),
            "rows differing only in excluded identity columns must hash equal"
        );
        // ...and they DIFFER without the exclude (every row unique).
        assert_ne!(compute_item_hash(&a), compute_item_hash(&b));
    }

    #[test]
    fn stable_hash_distinguishes_rows_differing_in_content() {
        // Excluding identity does NOT collapse rows with different content.
        let a = json!({"ts": "t0", "msg": "disk full"});
        let b = json!({"ts": "t1", "msg": "out of memory"});
        let exclude: BTreeSet<String> = ["ts".to_string()].into_iter().collect();
        assert_ne!(
            stable_item_hash(&a, &exclude),
            stable_item_hash(&b, &exclude)
        );
    }

    #[test]
    fn stable_hash_excluding_key_equals_full_hash_of_projected_item() {
        // The projection hash over {a,b,c}\{b} must equal the full hash of
        // the literal {a,c} object — i.e. it's a true key projection.
        let item = json!({"a": 1, "b": "drop me", "c": 3});
        let exclude: BTreeSet<String> = ["b".to_string()].into_iter().collect();
        let projected = json!({"a": 1, "c": 3});
        assert_eq!(
            stable_item_hash(&item, &exclude),
            compute_item_hash(&projected)
        );
    }

    #[test]
    fn stable_hash_non_object_ignores_exclude() {
        // Arrays/scalars have no top-level keys to filter -> identical to
        // the full hash regardless of the exclude set.
        let arr = json!([1, 2, 3]);
        let exclude: BTreeSet<String> = ["x".to_string()].into_iter().collect();
        assert_eq!(stable_item_hash(&arr, &exclude), compute_item_hash(&arr));
    }

    // ---------- AnchorWeights ----------

    #[test]
    fn weights_normalize_sums_to_one() {
        let w = AnchorWeights {
            front: 1.0,
            middle: 1.0,
            back: 2.0,
        }
        .normalize();
        assert!((w.front - 0.25).abs() < 1e-9);
        assert!((w.middle - 0.25).abs() < 1e-9);
        assert!((w.back - 0.5).abs() < 1e-9);
    }

    #[test]
    fn weights_normalize_zero_returns_default() {
        let w = AnchorWeights {
            front: 0.0,
            middle: 0.0,
            back: 0.0,
        }
        .normalize();
        assert_eq!(w, AnchorWeights::default());
    }

    // ---------- DataPattern ----------

    #[test]
    fn pattern_from_str_known_values() {
        assert_eq!(
            DataPattern::from_string("search_results"),
            DataPattern::SearchResults
        );
        assert_eq!(DataPattern::from_string("LOGS"), DataPattern::Logs);
        assert_eq!(
            DataPattern::from_string("time_series"),
            DataPattern::TimeSeries
        );
    }

    #[test]
    fn pattern_from_str_unknown_falls_to_generic() {
        assert_eq!(DataPattern::from_string("unknown"), DataPattern::Generic);
    }

    // ---------- calculate_anchor_budget ----------

    #[test]
    fn budget_zero_when_no_compression_needed() {
        assert_eq!(selector().calculate_anchor_budget(10, 10), 0);
        assert_eq!(selector().calculate_anchor_budget(5, 10), 0);
    }

    #[test]
    fn budget_respects_min_floor() {
        // max_items=8 * 0.25 = 2 → max(min=3, 2) = 3.
        assert_eq!(selector().calculate_anchor_budget(100, 8), 3);
    }

    #[test]
    fn budget_respects_max_ceiling() {
        // max_items=100 * 0.25 = 25 → min(max=12, 25) = 12.
        assert_eq!(selector().calculate_anchor_budget(1000, 100), 12);
    }

    #[test]
    fn budget_capped_by_array_size() {
        let c = AnchorConfig {
            min_anchor_slots: 50,
            ..AnchorConfig::default()
        };
        // max_items=100 * 0.25 = 25, max(50,25)=50, min(12,50)=12, min(12, array_size=10)=10.
        let s = AnchorSelector::new(c);
        assert_eq!(s.calculate_anchor_budget(10, 5), 10);
    }

    // ---------- strategy_for_pattern ----------

    #[test]
    fn strategy_mappings() {
        let s = selector();
        assert_eq!(
            s.strategy_for_pattern(DataPattern::SearchResults),
            AnchorStrategy::FrontHeavy
        );
        assert_eq!(
            s.strategy_for_pattern(DataPattern::Logs),
            AnchorStrategy::BackHeavy
        );
        assert_eq!(
            s.strategy_for_pattern(DataPattern::TimeSeries),
            AnchorStrategy::Balanced
        );
        assert_eq!(
            s.strategy_for_pattern(DataPattern::Generic),
            AnchorStrategy::Distributed
        );
    }

    // ---------- adjust_weights_for_query ----------

    #[test]
    fn adjust_weights_recency_shifts_to_back() {
        let s = selector();
        let base = AnchorWeights {
            front: 0.5,
            middle: 0.1,
            back: 0.4,
        };
        let adjusted = s.adjust_weights_for_query(base, Some("show me the latest errors"));
        assert!(
            adjusted.back > base.back,
            "recency keyword 'latest' should boost back: got {}",
            adjusted.back
        );
        assert!(adjusted.front < base.front);
    }

    #[test]
    fn adjust_weights_historical_shifts_to_front() {
        let s = selector();
        let base = AnchorWeights {
            front: 0.5,
            middle: 0.1,
            back: 0.4,
        };
        let adjusted = s.adjust_weights_for_query(base, Some("what was the original cause"));
        assert!(adjusted.front > base.front);
        assert!(adjusted.back < base.back);
    }

    #[test]
    fn adjust_weights_both_keywords_no_change() {
        let s = selector();
        let base = AnchorWeights {
            front: 0.5,
            middle: 0.1,
            back: 0.4,
        };
        let adjusted = s.adjust_weights_for_query(base, Some("first and latest"));
        assert_eq!(adjusted, base);
    }

    #[test]
    fn adjust_weights_no_query_no_change() {
        let s = selector();
        let base = AnchorWeights::default();
        assert_eq!(s.adjust_weights_for_query(base, None), base);
        assert_eq!(s.adjust_weights_for_query(base, Some("")), base);
    }

    // ---------- select_anchors top-level ----------

    #[test]
    fn select_anchors_empty_returns_empty() {
        assert!(selector()
            .select_anchors(&[], 10, DataPattern::Generic, None)
            .is_empty());
    }

    #[test]
    fn select_anchors_no_compression_returns_all() {
        let items: Vec<Value> = (0..5).map(|i| json!({"id": i})).collect();
        let anchors = selector().select_anchors(&items, 10, DataPattern::Generic, None);
        assert_eq!(anchors.len(), 5);
        assert!((0..5).all(|i| anchors.contains(&i)));
    }

    #[test]
    fn select_anchors_includes_first_and_last_for_distributed() {
        let items: Vec<Value> = (0..100).map(|i| json!({"id": i})).collect();
        let anchors = selector().select_anchors(&items, 10, DataPattern::Generic, None);
        // Distributed strategy with default weights should reach near
        // both ends.
        assert!(!anchors.is_empty());
        let max = *anchors.iter().max().unwrap();
        let min = *anchors.iter().min().unwrap();
        assert!(min < 20, "first anchor should be near start, got {}", min);
        assert!(
            max > 80,
            "last anchor should be near end (n=100), got {}",
            max
        );
    }

    #[test]
    fn select_anchors_dedup_identical_items() {
        // 100 items but all identical → most positions hash to the same
        // string → only one anchor per region survives dedup.
        let items: Vec<Value> = (0..100).map(|_| json!({"value": "same"})).collect();
        let anchors = selector().select_anchors(&items, 10, DataPattern::Generic, None);
        // With dedup_identical_items=true, after the first slot in each
        // region claims the hash, subsequent attempts find duplicates.
        // Result should be far fewer than the full budget (12).
        assert!(
            anchors.len() <= 3,
            "duplicate items should dedup: got {} anchors",
            anchors.len()
        );
    }

    // ---------- information density helpers ----------

    #[test]
    fn info_score_zero_for_non_dict() {
        let item = json!("string");
        let all = vec![json!({"a": 1})];
        assert_eq!(calculate_information_score(&item, &all), 0.0);
    }

    #[test]
    fn info_score_in_zero_one_range() {
        let item = json!({"a": 1, "b": 2});
        let all: Vec<Value> = (0..10).map(|i| json!({"a": i})).collect();
        let s = calculate_information_score(&item, &all);
        assert!((0.0..=1.0).contains(&s));
    }

    #[test]
    fn info_score_higher_for_unique_values() {
        // Item with rare value should score higher than item with common.
        let common: Vec<Value> = (0..10).map(|_| json!({"status": "ok"})).collect();
        let mut all = common.clone();
        all.push(json!({"status": "error"}));
        let common_score = calculate_information_score(&common[0], &all);
        let rare_score = calculate_information_score(&all[10], &all);
        assert!(
            rare_score > common_score,
            "rare-value item should score higher: rare={}, common={}",
            rare_score,
            common_score
        );
    }
}
