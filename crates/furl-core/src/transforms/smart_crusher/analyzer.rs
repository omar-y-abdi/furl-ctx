//! Analyzes JSON arrays to compute field statistics, detect patterns/change points, decide crushability, and select
//! a strategy. Rust uses deterministic sorted field order so first-match decisions do not vary with map iteration.

use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

use super::config::SmartCrusherConfig;
use super::field_detect::{detect_id_field_statistically, detect_score_field_statistically};
use super::stats_math::{mean, sample_stdev, sample_variance};
use super::types::{
    ArrayAnalysis, CompressionStrategy, CrushabilityAnalysis, FieldStats, FieldType, SkipReason,
};
use crate::transforms::anchor_selector::DataPattern;

/// Statistical analyzer for compression decisions. Stateless aside from `config`.
/// Construct once per request and call `analyze_array` per array — same API as Python.
pub struct SmartAnalyzer {
    pub config: SmartCrusherConfig,
}

impl SmartAnalyzer {
    pub fn new(config: SmartCrusherConfig) -> Self {
        SmartAnalyzer { config }
    }

    /// Top-level analysis; keep behavior aligned with Python `analyze_array`.
    pub fn analyze_array(&self, items: &[Value]) -> ArrayAnalysis {
        self.analyze_array_with_strings(items, None)
    }

    /// [`analyze_array`](Self::analyze_array) with the caller's pre-computed JSON serializations threaded through
    /// (PERF-3), so the crushability error-keyword scan never re-serializes an array the caller already serialized.
    pub fn analyze_array_with_strings(
        &self,
        items: &[Value],
        item_strings: Option<&[String]>,
    ) -> ArrayAnalysis {
        // Empty / non-dict-first guard: Python returns NONE strategy with
        // empty stats. We mirror exactly.
        let first_is_dict = items.first().map(|v| v.is_object()).unwrap_or(false);
        if !first_is_dict {
            return ArrayAnalysis {
                item_count: items.len(),
                field_stats: BTreeMap::new(),
                detected_pattern: DataPattern::Generic,
                recommended_strategy: CompressionStrategy::None,
                estimated_reduction: 0.0,
                crushability: None,
            };
        }

        // Union of all keys across dict items. Python also unions keys but iterates a set; sorted order is the deterministic choice for both languages.
        let mut all_keys: BTreeSet<String> = BTreeSet::new();
        for item in items {
            if let Some(obj) = item.as_object() {
                for k in obj.keys() {
                    all_keys.insert(k.clone());
                }
            }
        }

        let mut field_stats: BTreeMap<String, FieldStats> = BTreeMap::new();
        for key in &all_keys {
            field_stats.insert(key.clone(), self.analyze_field(key, items));
        }

        let pattern = self.detect_pattern(&field_stats, items);

        let crushability = self.analyze_crushability(items, &field_stats, item_strings);

        let strategy =
            self.select_strategy(&field_stats, pattern, items.len(), Some(&crushability));

        let reduction = if strategy == CompressionStrategy::Skip {
            0.0
        } else {
            self.estimate_reduction(&field_stats, strategy, items.len())
        };

        ArrayAnalysis {
            item_count: items.len(),
            field_stats,
            detected_pattern: pattern,
            recommended_strategy: strategy,
            estimated_reduction: reduction,
            crushability: Some(crushability),
        }
    }

    /// Per-field statistics; keep behavior aligned with Python `_analyze_field`.
    pub fn analyze_field(&self, key: &str, items: &[Value]) -> FieldStats {
        // Collect raw values across dict items.
        let values: Vec<Value> = items
            .iter()
            .filter_map(|i| i.as_object())
            .map(|obj| obj.get(key).cloned().unwrap_or(Value::Null))
            .collect();
        let non_null: Vec<&Value> = values.iter().filter(|v| !v.is_null()).collect();

        if non_null.is_empty() {
            return FieldStats {
                name: key.to_string(),
                field_type: FieldType::Null,
                count: values.len(),
                unique_count: 0,
                unique_ratio: 0.0,
                is_constant: true,
                constant_value: None,
                min_val: None,
                max_val: None,
                mean_val: None,
                variance: None,
                change_points: Vec::new(),
                avg_length: None,
                top_values: Vec::new(),
            };
        }

        let first = non_null[0];
        // Python `isinstance(first, bool)` precedes `int|float` — bool is a subclass of int in Python.
        // We model JSON's bool/number split directly: serde_json::Value::Bool vs Value::Number.
        let field_type = match first {
            Value::Bool(_) => FieldType::Boolean,
            Value::Number(_) => FieldType::Numeric,
            Value::String(_) => FieldType::String,
            Value::Object(_) => FieldType::Object,
            Value::Array(_) => FieldType::Array,
            _ => FieldType::Unknown,
        };

        // Uniqueness: stringify ALL values (including nulls), dedupe, count. Match exactly to keep
        // unique-count parity with fixtures. python_repr handles None as "None", bool as "True"/"False", etc.
        let str_values: Vec<String> = values.iter().map(python_repr).collect();
        let unique_set: BTreeSet<&String> = str_values.iter().collect();
        let unique_count = unique_set.len();
        let unique_ratio = if values.is_empty() {
            0.0
        } else {
            unique_count as f64 / values.len() as f64
        };

        let is_constant = unique_count == 1;
        let constant_value = if is_constant {
            Some(non_null[0].clone())
        } else {
            None
        };

        let mut stats = FieldStats {
            name: key.to_string(),
            field_type,
            count: values.len(),
            unique_count,
            unique_ratio,
            is_constant,
            constant_value,
            min_val: None,
            max_val: None,
            mean_val: None,
            variance: None,
            change_points: Vec::new(),
            avg_length: None,
            top_values: Vec::new(),
        };

        match field_type {
            FieldType::Numeric => {
                // Filter to finite f64 only — Python rejects NaN/Inf via `math.isfinite`. We mirror exactly so the same set of values feeds mean/variance/change-points.
                let nums: Vec<f64> = non_null
                    .iter()
                    .filter_map(|v| v.as_f64().filter(|f| f.is_finite()))
                    .collect();
                if !nums.is_empty() {
                    let min_val = nums.iter().cloned().reduce(f64::min);
                    let max_val = nums.iter().cloned().reduce(f64::max);
                    let mean_val = mean(&nums);
                    // `variance = 0` when n < 2 (Python: `if len(nums) > 1`).
                    let variance = if nums.len() > 1 {
                        sample_variance(&nums)
                    } else {
                        Some(0.0)
                    };
                    // Python wraps the numeric-stats block in `try/except (OverflowError, ValueError)` and resets ALL fields to None on failure.
                    let all_finite = mean_val.map(f64::is_finite).unwrap_or(false)
                        && variance.map(f64::is_finite).unwrap_or(false)
                        && min_val.map(f64::is_finite).unwrap_or(false)
                        && max_val.map(f64::is_finite).unwrap_or(false);
                    if all_finite {
                        stats.min_val = min_val;
                        stats.max_val = max_val;
                        stats.mean_val = mean_val;
                        stats.variance = variance;
                        stats.change_points = self.detect_change_points(&nums, 5);
                    } else {
                        // Python parity: the except block sets `variance = 0` (int literal) but min/max/mean to None.
                        stats.min_val = None;
                        stats.max_val = None;
                        stats.mean_val = None;
                        stats.variance = Some(0.0);
                        stats.change_points = Vec::new();
                    }
                }
            }
            FieldType::String => {
                let strs: Vec<&str> = non_null.iter().filter_map(|v| v.as_str()).collect();
                if !strs.is_empty() {
                    let lens: Vec<f64> = strs.iter().map(|s| s.chars().count() as f64).collect();
                    stats.avg_length = mean(&lens);
                    stats.top_values = top_n_by_count(&strs, 5);
                }
            }
            _ => {}
        }

        stats
    }

    /// Sliding-window change-point detector; keep behavior aligned with Python `_detect_change_points`.
    pub fn detect_change_points(&self, values: &[f64], window: usize) -> Vec<usize> {
        if values.len() < window * 2 {
            return Vec::new();
        }

        let overall_std = match sample_stdev(values) {
            Some(s) if s > 0.0 => s,
            _ => return Vec::new(),
        };

        let threshold = self.config.variance_threshold * overall_std;

        // Python: `for i in range(window, len(values) - window)`.
        let mut change_points: Vec<usize> = Vec::new();
        for i in window..values.len().saturating_sub(window) {
            let before = mean(&values[i - window..i]).unwrap_or(0.0);
            let after = mean(&values[i..i + window]).unwrap_or(0.0);
            if (after - before).abs() > threshold {
                change_points.push(i);
            }
        }

        if change_points.is_empty() {
            return Vec::new();
        }

        // Greedy dedup: keep first, then any cp where `cp - last > window`.
        let mut deduped: Vec<usize> = vec![change_points[0]];
        for &cp in &change_points[1..] {
            let last = *deduped
                .last()
                .expect("seeded with change_points[0], never empty");
            if cp - last > window {
                deduped.push(cp);
            }
        }
        deduped
    }

    /// Pattern classifier aligned with Python `_detect_pattern`. Returns
    /// the typed [`DataPattern`] (TYPE-1); the historical string forms are `as_str()`.
    pub fn detect_pattern(
        &self,
        field_stats: &BTreeMap<String, FieldStats>,
        items: &[Value],
    ) -> DataPattern {
        let has_timestamp = self.detect_temporal_field(field_stats, items);

        let has_numeric_with_variance = field_stats
            .values()
            .filter(|v| v.field_type == FieldType::Numeric)
            .any(|v| v.variance.unwrap_or(0.0) > 0.0);

        if has_timestamp && has_numeric_with_variance {
            return DataPattern::TimeSeries;
        }

        // logs pattern: high-cardinality string (message) + low-cardinality
        // categorical (level/status).
        let mut has_message_like = false;
        let mut has_level_like = false;
        for stats in field_stats.values() {
            if stats.field_type != FieldType::String {
                continue;
            }
            let avg_len = stats.avg_length.unwrap_or(0.0);
            if stats.unique_ratio > 0.5 && avg_len > 20.0 {
                has_message_like = true;
            } else if stats.unique_ratio < 0.1 && (2..=10).contains(&stats.unique_count) {
                has_level_like = true;
            }
        }
        if has_message_like && has_level_like {
            return DataPattern::Logs;
        }

        // search_results: any field with score-like signal at confidence >=0.5.
        for stats in field_stats.values() {
            let (is_score, confidence) = detect_score_field_statistically(stats, items);
            if is_score && confidence >= 0.5 {
                return DataPattern::SearchResults;
            }
        }

        DataPattern::Generic
    }

    /// Temporal-field detector. (Ported from Python's `_detect_temporal_field`, since retired in the excision
    /// — this is the only implementation; the numeric branch is tightened vs the port, see COR-34 below.)
    pub fn detect_temporal_field(
        &self,
        field_stats: &BTreeMap<String, FieldStats>,
        items: &[Value],
    ) -> bool {
        for (name, stats) in field_stats {
            match stats.field_type {
                FieldType::String => {
                    // First 10 values, str-typed only. Python: `items[:10]`.
                    let sample: Vec<&str> = items
                        .iter()
                        .take(10)
                        .filter_map(|i| i.as_object())
                        .filter_map(|o| o.get(name))
                        .filter_map(|v| v.as_str())
                        .collect();
                    if sample.is_empty() {
                        continue;
                    }
                    let iso_count = sample
                        .iter()
                        .filter(|s| is_iso_datetime(s) || is_iso_date(s))
                        .count();
                    if (iso_count as f64 / sample.len() as f64) > 0.5 {
                        return true;
                    }
                }
                FieldType::Numeric => {
                    if let (Some(mn), Some(mx)) = (stats.min_val, stats.max_val) {
                        // Unix epoch range check BOTH ends must sit in the same plausible window (seconds or millis). Checking only the
                        // min let a field spanning min=1.5e9..max=9e17 classify as temporal and flip the strategy to TIME_SERIES (COR-34).
                        let secs = 1_000_000_000.0..=2_000_000_000.0;
                        let millis = 1_000_000_000_000.0..=2_000_000_000_000.0;
                        let unix_seconds = secs.contains(&mn) && secs.contains(&mx);
                        let unix_millis = millis.contains(&mn) && millis.contains(&mx);
                        if unix_seconds || unix_millis {
                            return true;
                        }
                    }
                }
                _ => {}
            }
        }
        false
    }

    /// Crushability decision — the main "is it SAFE?" check.
    pub fn analyze_crushability(
        &self,
        items: &[Value],
        field_stats: &BTreeMap<String, FieldStats>,
        item_strings: Option<&[String]>,
    ) -> CrushabilityAnalysis {
        use super::outliers::{detect_error_items_for_preservation, detect_structural_outliers};

        // 1. PERF: `detect_id_field_statistically` hard-gates `unique_ratio < 0.9` (returns without reading `values`), and beyond
        // that gate only its String branch (first-20 sample) and Numeric branch (full list, sequential scan) read `values` at all
        let mut id_field_name: Option<String> = None;
        let mut id_uniqueness: f64 = 0.0;
        let mut id_confidence: f64 = 0.0;
        for (name, stats) in field_stats {
            if stats.unique_ratio < 0.9 {
                continue;
            }
            let collect_values = |limit: Option<usize>| -> Vec<Value> {
                let iter = items
                    .iter()
                    .filter_map(|i| i.as_object())
                    .map(|o| o.get(name).cloned().unwrap_or(Value::Null));
                match limit {
                    Some(n) => iter.take(n).collect(),
                    None => iter.collect(),
                }
            };
            let (is_id, confidence) = match stats.field_type {
                // String branch reads `values[..20]` only.
                FieldType::String => {
                    let sample = collect_values(Some(20));
                    detect_id_field_statistically(stats, &sample)
                }
                // Numeric branch scans the full list (`detect_sequential_pattern`).
                FieldType::Numeric => {
                    let values = collect_values(None);
                    detect_id_field_statistically(stats, &values)
                }
                // Object / Array / Boolean / Null / Unknown: decided by
                // `unique_ratio` alone — no value read.
                _ => detect_id_field_statistically(stats, &[]),
            };
            if is_id && confidence > id_confidence {
                id_field_name = Some(name.clone());
                id_uniqueness = stats.unique_ratio;
                id_confidence = confidence;
            }
        }
        let has_id_field = id_field_name.is_some() && id_confidence >= 0.7;

        // 2. Score field detection — short-circuit on first match.
        let mut has_score_field = false;
        for stats in field_stats.values() {
            let (is_score, _confidence) = detect_score_field_statistically(stats, items);
            if is_score {
                has_score_field = true;
                break;
            }
        }

        // 3. Structural outliers.
        let outlier_indices = detect_structural_outliers(items);
        let structural_outlier_count = outlier_indices.len();

        // 3b. Error-keyword fallback when no structural signal. Reuses
        // the caller's serializations when provided (PERF-3).
        let error_keyword_indices = detect_error_items_for_preservation(items, item_strings);
        let keyword_error_count = error_keyword_indices.len();
        let has_error_keyword_signal = keyword_error_count > 0 && structural_outlier_count == 0;

        // 4. Numeric anomalies (>variance_threshold σ from mean).
        let mut anomaly_indices: BTreeSet<usize> = BTreeSet::new();
        for stats in field_stats.values() {
            if stats.field_type != FieldType::Numeric {
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
            let threshold = self.config.variance_threshold * std;
            for (i, item) in items.iter().enumerate() {
                let Some(obj) = item.as_object() else {
                    continue;
                };
                let Some(v) = obj.get(&stats.name) else {
                    continue;
                };
                if let Some(num) = v.as_f64() {
                    if !num.is_nan() && (num - mean_val).abs() > threshold {
                        anomaly_indices.insert(i);
                    }
                }
            }
        }
        let anomaly_count = anomaly_indices.len();

        // 5. Average string uniqueness, EXCLUDING the detected ID field.
        let id_name_ref = id_field_name.as_deref();
        let string_ratios: Vec<f64> = field_stats
            .values()
            .filter(|s| s.field_type == FieldType::String && Some(s.name.as_str()) != id_name_ref)
            .map(|s| s.unique_ratio)
            .collect();
        let avg_string_uniqueness = if string_ratios.is_empty() {
            0.0
        } else {
            mean(&string_ratios).unwrap_or(0.0)
        };

        let non_id_numeric_ratios: Vec<f64> = field_stats
            .values()
            .filter(|s| s.field_type == FieldType::Numeric && Some(s.name.as_str()) != id_name_ref)
            .map(|s| s.unique_ratio)
            .collect();
        let avg_non_id_numeric_uniqueness = if non_id_numeric_ratios.is_empty() {
            0.0
        } else {
            mean(&non_id_numeric_ratios).unwrap_or(0.0)
        };

        let max_uniqueness = avg_string_uniqueness.max(id_uniqueness).max(0.0);
        let non_id_content_uniqueness = avg_string_uniqueness.max(avg_non_id_numeric_uniqueness);

        // 6. Change points.
        let has_change_points = field_stats
            .values()
            .filter(|s| s.field_type == FieldType::Numeric)
            .any(|s| !s.change_points.is_empty());

        let has_any_signal = has_score_field
            || structural_outlier_count > 0
            || has_error_keyword_signal
            || anomaly_count > 0
            || has_change_points;

        // Decision tree — order matters; mirrors Python case-by-case.
        let make = |crushable: bool,
                    confidence: f64,
                    reason: SkipReason,
                    has_any_signal: bool|
         -> CrushabilityAnalysis {
            CrushabilityAnalysis {
                crushable,
                confidence,
                reason,
                has_any_signal,
                // Memoized for the over-budget prioritizer (PERF-3) — both detections already ran above to derive the counts; carrying the indices avoids a re-scan.
                structural_outlier_indices: outlier_indices.clone(),
                error_keyword_indices: error_keyword_indices.clone(),
            }
        };

        // Case 0: repetitive content with unique IDs.
        if non_id_content_uniqueness < 0.1 && has_id_field {
            // `repetitive_content` is itself a signal, so this arm is
            // always `has_any_signal = true`.
            return make(true, 0.85, SkipReason::RepetitiveContentWithIds, true);
        }

        // Case 1: low uniqueness.
        if max_uniqueness < 0.3 {
            return make(
                true,
                0.9,
                SkipReason::LowUniquenessSafeToSample,
                has_any_signal,
            );
        }

        // Case 2: high uniqueness + ID field + NO signal = DON'T CRUSH.
        if has_id_field && max_uniqueness > 0.8 && !has_any_signal {
            return make(
                false,
                0.85,
                SkipReason::UniqueEntitiesNoSignal,
                has_any_signal,
            );
        }

        // Case 3: high uniqueness + has signal = crush.
        if max_uniqueness > 0.8 && has_any_signal {
            return make(
                true,
                0.7,
                SkipReason::UniqueEntitiesWithSignal,
                has_any_signal,
            );
        }

        // Case 4: medium uniqueness + no signal = don't crush.
        if !has_any_signal {
            return make(
                false,
                0.6,
                SkipReason::MediumUniquenessNoSignal,
                has_any_signal,
            );
        }

        // Case 5: medium uniqueness + has signal = crush with caution.
        make(
            true,
            0.5,
            SkipReason::MediumUniquenessWithSignal,
            has_any_signal,
        )
    }

    /// Strategy selector aligned with Python `_select_strategy`.
    pub fn select_strategy(
        &self,
        field_stats: &BTreeMap<String, FieldStats>,
        pattern: DataPattern,
        item_count: usize,
        crushability: Option<&CrushabilityAnalysis>,
    ) -> CompressionStrategy {
        if item_count < self.config.min_items_to_analyze {
            return CompressionStrategy::None;
        }

        if let Some(c) = crushability {
            if !c.crushable {
                return CompressionStrategy::Skip;
            }
        }

        if pattern == DataPattern::TimeSeries {
            let has_change_points = field_stats
                .values()
                .filter(|f| f.field_type == FieldType::Numeric)
                .any(|f| !f.change_points.is_empty());
            if has_change_points {
                return CompressionStrategy::TimeSeries;
            }
        }

        if pattern == DataPattern::Logs {
            // Python: `next((v for k, v in field_stats.items() if "message" in k.lower()), None)` We mirror
            // — first BTreeMap iteration order match wins. With sorted iteration, this is deterministic.
            let message_field = field_stats
                .iter()
                .find(|(k, _)| k.to_lowercase().contains("message"))
                .map(|(_, v)| v);
            if let Some(mf) = message_field {
                if mf.unique_ratio < 0.5 {
                    return CompressionStrategy::ClusterSample;
                }
            }
        }

        if pattern == DataPattern::SearchResults {
            return CompressionStrategy::TopN;
        }

        CompressionStrategy::SmartSample
    }

    /// Reduction estimator aligned with Python `_estimate_reduction`; returns a value in `[0, 0.95]`.
    pub fn estimate_reduction(
        &self,
        field_stats: &BTreeMap<String, FieldStats>,
        strategy: CompressionStrategy,
        _item_count: usize,
    ) -> f64 {
        if strategy == CompressionStrategy::None {
            return 0.0;
        }

        // Python divides by `len(field_stats)` unconditionally. We mirror by returning 0.0
        // — analyze_array's empty-input guard prevents this path from ever being reached .
        if field_stats.is_empty() {
            return 0.0;
        }

        let constant_count = field_stats.values().filter(|v| v.is_constant).count();
        let constant_ratio = constant_count as f64 / field_stats.len() as f64;

        let base = match strategy {
            CompressionStrategy::TimeSeries => 0.7,
            CompressionStrategy::ClusterSample => 0.8,
            CompressionStrategy::TopN => 0.6,
            CompressionStrategy::SmartSample => 0.5,
            _ => 0.3,
        };

        (base + constant_ratio * 0.2).min(0.95)
    }
}

// ---------- helpers ----------

/// Python-equivalent `str(v)` for `serde_json::Value`. We approximate via JSON for parity-locked counts; the only case
/// this can drift is if a field carries nested dicts with mixed types in its values, which is rare for crushable arrays.
fn python_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        // Nested values aren't typically the unique-count drivers, so we
        // stringify with JSON. Used only for cardinality, not surfaced.
        _ => v.to_string(),
    }
}

/// Counter.most_common(n) equivalent. Returns up to `n` (value, count) pairs sorted by count descending; ties broken
/// by FIRST OCCURRENCE order (mirrors Python's `Counter.most_common` via dict insertion order + `heapq.nlargest`).
fn top_n_by_count(strs: &[&str], n: usize) -> Vec<(String, usize)> {
    use std::collections::HashMap;

    let mut order: Vec<&str> = Vec::new();
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for &s in strs {
        if !counts.contains_key(s) {
            order.push(s);
        }
        *counts.entry(s).or_insert(0) += 1;
    }

    // Stable sort by count desc preserves first-occurrence tie order.
    let mut pairs: Vec<(&&str, usize)> = order.iter().map(|k| (k, counts[k])).collect();
    pairs.sort_by_key(|b| std::cmp::Reverse(b.1));

    pairs
        .into_iter()
        .take(n)
        .map(|(k, c)| ((*k).to_string(), c))
        .collect()
}

// Match the Python ISO-8601 prefixes for full timestamps and dates using direct
// character checks instead of per-call regex compilation; behavior must stay equivalent.
pub(crate) fn is_iso_datetime(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() < 19 {
        return false;
    }
    is_digit(b[0])
        && is_digit(b[1])
        && is_digit(b[2])
        && is_digit(b[3])
        && b[4] == b'-'
        && is_digit(b[5])
        && is_digit(b[6])
        && b[7] == b'-'
        && is_digit(b[8])
        && is_digit(b[9])
        && (b[10] == b'T' || b[10] == b' ')
        && is_digit(b[11])
        && is_digit(b[12])
        && b[13] == b':'
        && is_digit(b[14])
        && is_digit(b[15])
        && b[16] == b':'
        && is_digit(b[17])
        && is_digit(b[18])
}

pub(crate) fn is_iso_date(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() != 10 {
        return false;
    }
    is_digit(b[0])
        && is_digit(b[1])
        && is_digit(b[2])
        && is_digit(b[3])
        && b[4] == b'-'
        && is_digit(b[5])
        && is_digit(b[6])
        && b[7] == b'-'
        && is_digit(b[8])
        && is_digit(b[9])
}

#[inline]
fn is_digit(b: u8) -> bool {
    b.is_ascii_digit()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn is_iso_datetime_rejects_every_corrupted_position() {
        // `is_iso_datetime` is a positional structural check (`DDDD-DD-DD` + `T`|` ` + `DD:DD:DD`). cargo-mutants flagged EVERY `&&` junction (lines 800-817)
        // as a surviving `&&`→`||` mutant, plus `is_digit`→`true` (:839), because no test fed a string that is valid everywhere EXCEPT one position.
        let base = "2024-01-15T12:30:45";
        assert!(is_iso_datetime(base), "valid ISO datetime must pass");
        assert!(
            is_iso_datetime("2024-01-15 12:30:45"),
            "space at [10] is a valid separator"
        );
        assert!(
            is_iso_datetime("2024-01-15T12:30:45.123Z"),
            "trailing fraction/zone past index 18 is ignored (len>=19)"
        );
        for k in 0..base.len() {
            let mut bytes = base.as_bytes().to_vec();
            bytes[k] = b'X';
            let corrupted = String::from_utf8(bytes).unwrap();
            assert!(
                !is_iso_datetime(&corrupted),
                "a structurally-invalid byte at position {k} must be rejected, \
                 but was accepted: {corrupted:?}"
            );
        }
        assert!(
            !is_iso_datetime("2024-01-15T12:30:4"),
            "len 18 < 19 must be rejected (the length guard)"
        );
    }

    #[test]
    fn is_iso_date_rejects_every_corrupted_position() {
        // Same kill for the date-only validator (`DDDD-DD-DD`, exactly 10
        // chars): the `&&` junctions at 825-834 all survived as `&&`→`||`.
        let base = "2024-01-15";
        assert!(is_iso_date(base), "valid ISO date must pass");
        for k in 0..base.len() {
            let mut bytes = base.as_bytes().to_vec();
            bytes[k] = b'X';
            let corrupted = String::from_utf8(bytes).unwrap();
            assert!(
                !is_iso_date(&corrupted),
                "a structurally-invalid byte at position {k} must be rejected, \
                 but was accepted: {corrupted:?}"
            );
        }
        assert!(!is_iso_date("2024-01-150"), "len 11 != 10 must be rejected");
        assert!(!is_iso_date("2024-01-1"), "len 9 != 10 must be rejected");
    }

    fn analyzer() -> SmartAnalyzer {
        SmartAnalyzer::new(SmartCrusherConfig::default())
    }

    // ---------- analyze_array ----------

    #[test]
    fn empty_array_returns_none_strategy() {
        let a = analyzer().analyze_array(&[]);
        assert_eq!(a.item_count, 0);
        assert!(a.field_stats.is_empty());
        assert_eq!(a.detected_pattern, DataPattern::Generic);
        assert_eq!(a.recommended_strategy, CompressionStrategy::None);
        assert_eq!(a.estimated_reduction, 0.0);
        assert!(a.crushability.is_none());
    }

    #[test]
    fn non_dict_first_returns_none_strategy() {
        let items = vec![json!("hello"), json!("world")];
        let a = analyzer().analyze_array(&items);
        assert_eq!(a.item_count, 2);
        assert_eq!(a.recommended_strategy, CompressionStrategy::None);
    }

    #[test]
    fn small_array_below_threshold_returns_none() {
        // 4 items < min_items_to_analyze=5
        let items: Vec<Value> = (0..4).map(|i| json!({"id": i, "v": i})).collect();
        let a = analyzer().analyze_array(&items);
        assert_eq!(a.recommended_strategy, CompressionStrategy::None);
    }

    // ---------- analyze_field ----------

    #[test]
    fn analyze_field_all_null_yields_null_type_constant() {
        let items: Vec<Value> = (0..5).map(|_| json!({"x": null})).collect();
        let s = analyzer().analyze_field("x", &items);
        assert_eq!(s.field_type, FieldType::Null);
        assert!(s.is_constant);
        assert_eq!(s.unique_count, 0);
        assert_eq!(s.count, 5);
    }

    #[test]
    fn analyze_field_numeric_basic_stats() {
        let items: Vec<Value> = (1..=10).map(|i| json!({"n": i})).collect();
        let s = analyzer().analyze_field("n", &items);
        assert_eq!(s.field_type, FieldType::Numeric);
        assert_eq!(s.min_val, Some(1.0));
        assert_eq!(s.max_val, Some(10.0));
        assert_eq!(s.mean_val, Some(5.5));
        // Python: statistics.variance(1..=10) = 9.166666...
        let v = s.variance.expect("variance present");
        assert!((v - 9.166666666666666).abs() < 1e-9);
    }

    #[test]
    fn analyze_field_numeric_overflow_resets_all_stats_to_none() {
        // Python parity: when stats computation overflows, the `try/except (OverflowError, ValueError)` block resets ALL numeric
        // fields to None. We mirror by checking finiteness across the bundle and dropping the whole numeric stats group on failure.
        let huge = 1e200;
        // Two extreme opposite values: variance overflows.
        let items = vec![json!({"n": huge}), json!({"n": -huge})];
        let s = analyzer().analyze_field("n", &items);
        assert_eq!(s.field_type, FieldType::Numeric);
        // Per Python: min/max/mean reset to None; variance = 0 (int);
        // change_points empty.
        assert_eq!(s.min_val, None);
        assert_eq!(s.max_val, None);
        assert_eq!(s.mean_val, None);
        assert_eq!(s.variance, Some(0.0));
        assert!(s.change_points.is_empty());
        // Non-numeric stats (count, unique, is_constant) should still hold.
        assert_eq!(s.count, 2);
        assert_eq!(s.unique_count, 2);
    }

    #[test]
    fn analyze_field_numeric_filters_nan_and_inf() {
        // Tricky: serde_json doesn't allow NaN/Inf in JSON, so we build a Number directly. Use `json!` with regular
        // ints/floats only — we just verify the finite-only path doesn't crash on a single value (variance=0 then).
        let items: Vec<Value> = vec![json!({"n": 42.0}), json!({"n": 42.0})];
        let s = analyzer().analyze_field("n", &items);
        assert_eq!(s.variance, Some(0.0));
    }

    #[test]
    fn analyze_field_string_avg_length_and_top_values() {
        let items = vec![
            json!({"s": "ok"}),
            json!({"s": "ok"}),
            json!({"s": "warn"}),
            json!({"s": "fail"}),
            json!({"s": "ok"}),
        ];
        let s = analyzer().analyze_field("s", &items);
        assert_eq!(s.field_type, FieldType::String);
        // mean(2,2,4,4,2) = 2.8
        assert_eq!(s.avg_length, Some(2.8));
        // most_common: ok=3, warn=1, fail=1 (tie order: first-occurrence)
        assert_eq!(s.top_values[0], ("ok".to_string(), 3));
        assert_eq!(s.top_values[1].1, 1);
        assert_eq!(s.top_values[2].1, 1);
    }

    #[test]
    fn analyze_field_constant_detected() {
        let items: Vec<Value> = (0..10).map(|_| json!({"flag": true})).collect();
        let s = analyzer().analyze_field("flag", &items);
        assert!(s.is_constant);
        assert_eq!(s.constant_value, Some(json!(true)));
    }

    // ---------- detect_change_points ----------

    #[test]
    fn change_points_too_few_values_empty() {
        let cps = analyzer().detect_change_points(&[1.0, 2.0, 3.0], 5);
        assert!(cps.is_empty());
    }

    #[test]
    fn change_points_constant_values_empty() {
        // stdev=0 → early return.
        let cps = analyzer().detect_change_points(&[5.0; 20], 5);
        assert!(cps.is_empty());
    }

    #[test]
    fn change_points_step_function_detected() {
        // Three-segment: 30×0, 30×100, 30×0. For a pure two-segment step, diff = |b-a| ≈ 2σ exactly, so the strict `> threshold` check would miss.
        let mut v: Vec<f64> = Vec::with_capacity(90);
        v.extend(vec![0.0; 30]);
        v.extend(vec![100.0; 30]);
        v.extend(vec![0.0; 30]);
        let cps = analyzer().detect_change_points(&v, 5);
        assert!(
            cps.contains(&30) || cps.contains(&60),
            "expected change point at i=30 or i=60, got {:?}",
            cps
        );
    }

    // ---------- detect_pattern ----------

    #[test]
    fn pattern_logs_message_and_level() {
        // 30 items, 2 distinct levels → unique_ratio = 2/30 ≈ 0.067 < 0.1 ✓.
        // Long unique messages → unique_ratio = 1.0 > 0.5 and avg_length > 20.
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "msg": format!("Some long unique log message body text #{}", i),
                    "level": if i % 2 == 0 { "INFO" } else { "ERROR" },
                })
            })
            .collect();
        let mut field_stats: BTreeMap<String, FieldStats> = BTreeMap::new();
        let a = analyzer();
        for k in ["msg", "level"] {
            field_stats.insert(k.to_string(), a.analyze_field(k, &items));
        }
        let p = a.detect_pattern(&field_stats, &items);
        assert_eq!(p, DataPattern::Logs);
    }

    #[test]
    fn pattern_generic_when_nothing_matches() {
        let items: Vec<Value> = (0..10).map(|i| json!({"a": i, "b": i * 2})).collect();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        let a = analyzer();
        for k in ["a", "b"] {
            fs.insert(k.to_string(), a.analyze_field(k, &items));
        }
        let p = a.detect_pattern(&fs, &items);
        // No timestamps, no logs shape, no obvious score → generic.
        assert_eq!(p, DataPattern::Generic);
    }

    // ---------- detect_temporal_field ----------

    #[test]
    fn temporal_iso_date() {
        let items: Vec<Value> = (1..=10)
            .map(|i| json!({"d": format!("2025-01-{:02}", i)}))
            .collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("d".to_string(), a.analyze_field("d", &items));
        assert!(a.detect_temporal_field(&fs, &items));
    }

    #[test]
    fn temporal_iso_datetime() {
        let items: Vec<Value> = (1..=10)
            .map(|i| json!({"t": format!("2025-01-{:02}T12:00:00Z", i)}))
            .collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("t".to_string(), a.analyze_field("t", &items));
        assert!(a.detect_temporal_field(&fs, &items));
    }

    #[test]
    fn temporal_unix_seconds_range() {
        // Timestamps in the 2024-2025 range.
        let items: Vec<Value> = (0..10)
            .map(|i| json!({"ts": 1_700_000_000_i64 + i * 86400}))
            .collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("ts".to_string(), a.analyze_field("ts", &items));
        assert!(a.detect_temporal_field(&fs, &items));
    }

    #[test]
    fn temporal_unix_millis_range() {
        // Millisecond timestamps in the 2023-2024 range — both ends
        // inside the plausible epoch-millis window.
        let items: Vec<Value> = (0..10)
            .map(|i| json!({"ts": 1_700_000_000_000_i64 + i * 86_400_000}))
            .collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("ts".to_string(), a.analyze_field("ts", &items));
        assert!(a.detect_temporal_field(&fs, &items));
    }

    #[test]
    fn temporal_absurd_max_not_detected() {
        // COR-34: a min alone inside the plausible epoch-seconds window must not classify the field as temporal when the max is
        // absurd — a numeric field spanning 1.5e9..9e17 is not a timestamp column, and flipping it to TIME_SERIES misplans the array.
        let mut items: Vec<Value> = (0..9)
            .map(|i| json!({"ts": 1_500_000_000_i64 + i}))
            .collect();
        items.push(json!({"ts": 900_000_000_000_000_000_i64}));
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("ts".to_string(), a.analyze_field("ts", &items));
        assert!(
            !a.detect_temporal_field(&fs, &items),
            "min=1.5e9 with max=9e17 must not be temporal"
        );
    }

    #[test]
    fn temporal_normal_numbers_not_detected() {
        let items: Vec<Value> = (1..=10).map(|i| json!({"n": i})).collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("n".to_string(), a.analyze_field("n", &items));
        assert!(!a.detect_temporal_field(&fs, &items));
    }

    // ---------- analyze_crushability ----------

    #[test]
    fn crushability_low_uniqueness_safe_to_sample() {
        // 30 items, all 'status':'ok' — high redundancy.
        let items: Vec<Value> = (0..30).map(|_| json!({"status": "ok"})).collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("status".to_string(), a.analyze_field("status", &items));
        let c = a.analyze_crushability(&items, &fs, None);
        assert!(c.crushable);
        // Only "status" string field with unique_ratio=1/30=0.033 → max
        // uniqueness ≈ 0.033 < 0.3 → low_uniqueness path.
        assert_eq!(c.reason, SkipReason::LowUniquenessSafeToSample);
    }

    #[test]
    fn crushability_unique_entities_no_signal_skips() {
        // Sequential IDs, distinct names, no errors, no change points.
        // Max uniqueness > 0.8, has_id_field=true, no signals → skip.
        let items: Vec<Value> = (0..20)
            .map(|i| json!({"id": i, "name": format!("user_{}", i)}))
            .collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        for k in ["id", "name"] {
            fs.insert(k.to_string(), a.analyze_field(k, &items));
        }
        let c = a.analyze_crushability(&items, &fs, None);
        assert!(!c.crushable);
        assert_eq!(c.reason, SkipReason::UniqueEntitiesNoSignal);
    }

    #[test]
    fn crushability_repetitive_content_with_ids_crushes() {
        // Unique ID + constant content field → repetitive_content path.
        let items: Vec<Value> = (0..20).map(|i| json!({"id": i, "status": "ok"})).collect();
        let a = analyzer();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        for k in ["id", "status"] {
            fs.insert(k.to_string(), a.analyze_field(k, &items));
        }
        let c = a.analyze_crushability(&items, &fs, None);
        assert!(c.crushable);
        assert_eq!(c.reason, SkipReason::RepetitiveContentWithIds);
    }

    // ---------- select_strategy ----------

    #[test]
    fn select_strategy_below_min_returns_none() {
        let fs = BTreeMap::new();
        let s = analyzer().select_strategy(&fs, DataPattern::Generic, 3, None);
        assert_eq!(s, CompressionStrategy::None);
    }

    #[test]
    fn select_strategy_skip_when_not_crushable() {
        let fs = BTreeMap::new();
        let crush = CrushabilityAnalysis::skip(SkipReason::MediumUniquenessNoSignal, 0.9);
        let s = analyzer().select_strategy(&fs, DataPattern::Generic, 100, Some(&crush));
        assert_eq!(s, CompressionStrategy::Skip);
    }

    #[test]
    fn select_strategy_search_results_returns_top_n() {
        let fs = BTreeMap::new();
        let s = analyzer().select_strategy(&fs, DataPattern::SearchResults, 100, None);
        assert_eq!(s, CompressionStrategy::TopN);
    }

    #[test]
    fn select_strategy_generic_returns_smart_sample() {
        let fs = BTreeMap::new();
        let s = analyzer().select_strategy(&fs, DataPattern::Generic, 100, None);
        assert_eq!(s, CompressionStrategy::SmartSample);
    }

    // ---------- estimate_reduction ----------

    #[test]
    fn estimate_reduction_none_returns_zero() {
        let fs = BTreeMap::new();
        let r = analyzer().estimate_reduction(&fs, CompressionStrategy::None, 100);
        assert_eq!(r, 0.0);
    }

    #[test]
    fn estimate_reduction_caps_at_0_95() {
        // All-constant field stats → constant_ratio=1.0 → base+0.2 = 1.0,
        // capped at 0.95.
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        for k in ["a", "b"] {
            fs.insert(
                k.to_string(),
                FieldStats {
                    name: k.to_string(),
                    field_type: FieldType::String,
                    count: 10,
                    unique_count: 1,
                    unique_ratio: 0.1,
                    is_constant: true,
                    constant_value: Some(json!("v")),
                    min_val: None,
                    max_val: None,
                    mean_val: None,
                    variance: None,
                    change_points: Vec::new(),
                    avg_length: None,
                    top_values: Vec::new(),
                },
            );
        }
        let r = analyzer().estimate_reduction(&fs, CompressionStrategy::ClusterSample, 10);
        assert_eq!(r, 0.95);
    }

    #[test]
    fn estimate_reduction_smart_sample_no_constants() {
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert(
            "id".to_string(),
            FieldStats {
                name: "id".to_string(),
                field_type: FieldType::Numeric,
                count: 100,
                unique_count: 100,
                unique_ratio: 1.0,
                is_constant: false,
                constant_value: None,
                min_val: Some(0.0),
                max_val: Some(99.0),
                mean_val: Some(49.5),
                variance: Some(841.66),
                change_points: Vec::new(),
                avg_length: None,
                top_values: Vec::new(),
            },
        );
        let r = analyzer().estimate_reduction(&fs, CompressionStrategy::SmartSample, 100);
        // base 0.5 + constant_ratio 0 * 0.2 = 0.5
        assert_eq!(r, 0.5);
    }

    // ---------- helpers ----------

    #[test]
    fn iso_datetime_pattern_matches() {
        assert!(is_iso_datetime("2025-01-15T12:00:00"));
        assert!(is_iso_datetime("2025-01-15 12:00:00"));
        assert!(is_iso_datetime("2025-01-15T12:00:00.123Z"));
        assert!(!is_iso_datetime("2025-01-15"));
        assert!(!is_iso_datetime("not a date"));
    }

    #[test]
    fn iso_date_pattern_matches() {
        assert!(is_iso_date("2025-01-15"));
        assert!(!is_iso_date("2025-01-15T12:00:00"));
        assert!(!is_iso_date("2025/01/15"));
    }

    #[test]
    fn python_repr_basics() {
        assert_eq!(python_repr(&Value::Null), "None");
        assert_eq!(python_repr(&json!(true)), "True");
        assert_eq!(python_repr(&json!(false)), "False");
        assert_eq!(python_repr(&json!(42)), "42");
        assert_eq!(python_repr(&json!("hello")), "hello");
    }

    #[test]
    fn top_n_first_occurrence_tie_break() {
        // a appears first, b second, both count 2.
        let strs = vec!["a", "b", "a", "b", "c"];
        let top = top_n_by_count(&strs, 5);
        assert_eq!(top[0].0, "a");
        assert_eq!(top[1].0, "b");
        assert_eq!(top[2].0, "c");
    }
}
