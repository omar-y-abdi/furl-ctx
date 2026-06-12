//! TabularCompactor — array → [`Compaction`] IR.
//!
//! # Pipeline
//!
//! ```text
//! &[Value]  →  detect uniformity  →  build schema  →  build rows
//!                    │
//!                    ├─ heterogeneous? → bucket by discriminator
//!                    │                    (Compaction::Buckets)
//!                    │
//!                    └─ homogeneous → flatten nested-uniform columns
//!                                        (Compaction::Table)
//! ```
//!
//! # Decision rules
//!
//! - **Untouched fall-through.** Items < 2, non-object items, or a key
//!   distribution too uneven for tabular form → return [`Compaction::Untouched`]
//!   so the existing lossy path takes over.
//! - **Schema = union of all keys**, sorted by descending frequency then
//!   alphabetically. Sparse fields keep their slot — cells in rows that
//!   lack the field render as [`CellValue::Missing`].
//! - **Heterogeneous case.** When < 50% of keys appear in >= 80% of rows,
//!   look for a discriminator (a string field present in every row whose
//!   value distribution partitions cleanly). If found, emit
//!   [`Compaction::Buckets`]; else [`Compaction::Untouched`].
//! - **Nested-uniform flatten.** A field that's an object in every row
//!   with the same inner key set, where flattening doesn't blow up the
//!   column count by more than `max_flatten_inner_keys`, gets promoted
//!   into dotted columns (`meta.region`, `meta.tier`).
//! - **Stringified-JSON.** Cells that classify as
//!   [`CellClass::StringifiedJson`] become [`CellValue::Nested`] when the
//!   parsed value is an array of objects (recursive table); otherwise
//!   [`CellValue::Scalar`] of the parsed value (saves escaping cost).
//! - **Opaque blob.** [`CellClass::Opaque`] cells become
//!   [`CellValue::OpaqueRef`] keyed by a 12-char SHA-256 prefix.
//!
//! [`CellClass`]: super::classifier::CellClass
//! [`CellClass::StringifiedJson`]: super::classifier::CellClass::StringifiedJson
//! [`CellClass::Opaque`]: super::classifier::CellClass::Opaque

use std::collections::BTreeMap;
use std::sync::Arc;

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::classifier::{classify_cell, CellClass, ClassifyConfig};
use super::ir::{Bucket, CellValue, ColumnEncoding, Compaction, FieldSpec, Row, Schema};
use crate::ccr::CcrStore;

/// Config for the compactor.
///
/// `Clone`/`Default`/manual `Debug`. The optional `ccr_store` cannot
/// derive `Debug` (`dyn CcrStore` isn't `Debug`), so `Debug` is
/// hand-written below to print only the store's presence.
#[derive(Clone)]
pub struct CompactConfig {
    pub classify: ClassifyConfig,

    /// Minimum item count to attempt tabular compaction. Below this,
    /// return [`Compaction::Untouched`]. Default: 2.
    pub min_items: usize,

    /// A field is "core" if it appears in at least this fraction of
    /// rows. Schemas with too few core fields trigger heterogeneous
    /// (bucket) handling. Default: 0.8.
    pub core_field_fraction: f64,

    /// Heterogeneity threshold: when fewer than this fraction of all
    /// observed keys are core, treat the array as heterogeneous and
    /// look for a discriminator. Default: 0.5.
    pub heterogeneous_core_ratio: f64,

    /// Cap on inner-key count for nested-uniform flattening. Larger
    /// inner schemas stay nested rather than exploding column count.
    /// Default: 6.
    pub max_flatten_inner_keys: usize,

    /// Minimum bucket count before considering a candidate discriminator
    /// "useful". Default: 2.
    pub min_buckets: usize,

    /// Maximum bucket count — too many buckets means the discriminator
    /// is too granular (e.g. an ID column). Default: 8.
    pub max_buckets: usize,

    /// Optional CCR store. When set, an opaque-blob cell substituted with
    /// an `<<ccr:HASH,...>>` marker ALSO stashes the original bytes under
    /// that hash (Defect 2): the marker becomes a recovery pointer, not a
    /// silent loss. Without a store the marker still renders (same hash),
    /// but the original is unretrievable — which is exactly the silent
    /// loss the public path must avoid, so the production
    /// `CompactionStage` always wires one in. `None` keeps the compactor
    /// a pure function for the many tests + the parity formatters that
    /// only inspect the IR.
    pub ccr_store: Option<Arc<dyn CcrStore>>,
}

impl std::fmt::Debug for CompactConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CompactConfig")
            .field("classify", &self.classify)
            .field("min_items", &self.min_items)
            .field("core_field_fraction", &self.core_field_fraction)
            .field("heterogeneous_core_ratio", &self.heterogeneous_core_ratio)
            .field("max_flatten_inner_keys", &self.max_flatten_inner_keys)
            .field("min_buckets", &self.min_buckets)
            .field("max_buckets", &self.max_buckets)
            .field("ccr_store", &self.ccr_store.is_some())
            .finish()
    }
}

impl Default for CompactConfig {
    fn default() -> Self {
        Self {
            classify: ClassifyConfig::default(),
            min_items: 2,
            core_field_fraction: 0.8,
            heterogeneous_core_ratio: 0.6,
            max_flatten_inner_keys: 6,
            min_buckets: 2,
            max_buckets: 8,
            ccr_store: None,
        }
    }
}

/// Top-level compaction entry point.
pub fn compact(items: &[Value], cfg: &CompactConfig) -> Compaction {
    if items.len() < cfg.min_items {
        return Compaction::Untouched(Value::Array(items.to_vec()));
    }
    if !items.iter().all(|v| matches!(v, Value::Object(_))) {
        return Compaction::Untouched(Value::Array(items.to_vec()));
    }

    let key_freqs = compute_key_freqs(items);
    let total = items.len();
    let core_threshold = (total as f64 * cfg.core_field_fraction).ceil() as usize;
    let core_count = key_freqs.values().filter(|&&f| f >= core_threshold).count();
    let total_keys = key_freqs.len();

    let core_ratio = if total_keys == 0 {
        1.0
    } else {
        core_count as f64 / total_keys as f64
    };

    if core_ratio < cfg.heterogeneous_core_ratio {
        if let Some(disc) = detect_discriminator(items, &key_freqs, cfg) {
            return bucket_by(items, &disc, cfg);
        }
        // No clean discriminator — fall through to a sparse Table
        // rather than refusing. A sparse table is still better than
        // letting the lossy path drop fields wholesale.
    }

    build_homogeneous_table(items, &key_freqs, cfg)
}

fn compute_key_freqs(items: &[Value]) -> BTreeMap<String, usize> {
    let mut freqs: BTreeMap<String, usize> = BTreeMap::new();
    for item in items {
        if let Value::Object(map) = item {
            for k in map.keys() {
                *freqs.entry(k.clone()).or_insert(0) += 1;
            }
        }
    }
    freqs
}

fn build_homogeneous_table(
    items: &[Value],
    key_freqs: &BTreeMap<String, usize>,
    cfg: &CompactConfig,
) -> Compaction {
    // Order: descending frequency, then alphabetical for stability.
    let mut keys: Vec<(&String, &usize)> = key_freqs.iter().collect();
    keys.sort_by(|a, b| b.1.cmp(a.1).then_with(|| a.0.cmp(b.0)));
    let ordered_keys: Vec<String> = keys.into_iter().map(|(k, _)| k.clone()).collect();

    let total = items.len();
    let mut field_specs: Vec<FieldSpec> = ordered_keys
        .iter()
        .map(|k| FieldSpec {
            name: k.clone(),
            type_tag: infer_type_tag(items, k),
            nullable: key_freqs[k] < total
                || items
                    .iter()
                    .filter_map(|v| v.as_object())
                    .any(|o| matches!(o.get(k), Some(Value::Null))),
            const_value: None,
            encoding: None,
        })
        .collect();

    let mut rows: Vec<Row> = items
        .iter()
        .map(|item| build_row(item, &ordered_keys, cfg))
        .collect();

    flatten_uniform_nested(&mut field_specs, &mut rows, cfg);
    stamp_constant_columns(&mut field_specs, &rows);
    stamp_arith_int_columns(&mut field_specs, &rows);
    stamp_iso_delta_columns(&mut field_specs, &rows);
    stamp_dict_string_columns(&mut field_specs, &rows);

    Compaction::Table {
        schema: Schema {
            fields: field_specs,
        },
        rows,
        original_count: items.len(),
    }
}

fn build_row(item: &Value, ordered_keys: &[String], cfg: &CompactConfig) -> Row {
    let obj = match item.as_object() {
        Some(o) => o,
        None => return Row::new(vec![]),
    };
    let cells: Vec<CellValue> = ordered_keys
        .iter()
        .map(|k| match obj.get(k) {
            None => CellValue::Missing,
            Some(v) => cell_from_value(v, cfg),
        })
        .collect();
    Row::new(cells)
}

fn cell_from_value(v: &Value, cfg: &CompactConfig) -> CellValue {
    match classify_cell(v, &cfg.classify) {
        CellClass::Scalar => CellValue::Scalar(v.clone()),
        CellClass::JsonObject => CellValue::Scalar(v.clone()), // flatten pass may promote
        CellClass::JsonArray => {
            // Recurse if the inner array is array-of-objects; else scalar.
            if let Value::Array(items) = v {
                if items.iter().all(|i| matches!(i, Value::Object(_))) && items.len() >= 2 {
                    return CellValue::Nested(Box::new(compact(items, cfg)));
                }
            }
            CellValue::Scalar(v.clone())
        }
        CellClass::StringifiedJson(parsed) => {
            // If the parsed JSON is an array of objects, recurse; else
            // store the parsed value as a Scalar (un-escapes for free).
            if let Value::Array(items) = &parsed {
                if items.iter().all(|i| matches!(i, Value::Object(_))) && items.len() >= 2 {
                    return CellValue::Nested(Box::new(compact(items, cfg)));
                }
            }
            CellValue::Scalar(parsed)
        }
        CellClass::Opaque(kind) => {
            let s = match v {
                Value::String(s) => s,
                _ => return CellValue::Scalar(v.clone()),
            };
            let bytes = s.as_bytes();
            let ccr_hash = hash_opaque(bytes);
            // Defect 2: persist the original blob under the SAME hash the
            // rendered `<<ccr:HASH,...>>` marker will carry, so a
            // consumer holding only the output can recover it via
            // `ccr_get(hash)`. Without this, the lossless:table path
            // substitutes the blob with a marker but never stores the
            // original → silent loss. The store write is unconditional
            // when a store is configured and idempotent (same hash →
            // same bytes); `None` keeps the compactor pure for tests.
            if let Some(store) = &cfg.ccr_store {
                store.put(&ccr_hash, s);
            }
            CellValue::OpaqueRef {
                ccr_hash,
                byte_size: bytes.len(),
                kind,
            }
        }
    }
}

/// Promote fields whose every row holds an object with the same key
/// set into dotted columns. Bounded by `cfg.max_flatten_inner_keys` so
/// a 50-key inner schema doesn't blow up the table width.
fn flatten_uniform_nested(specs: &mut Vec<FieldSpec>, rows: &mut [Row], cfg: &CompactConfig) {
    let mut i = 0;
    while i < specs.len() {
        let inner_keys = match uniform_object_keys(specs, rows, i) {
            Some(keys) if !keys.is_empty() && keys.len() <= cfg.max_flatten_inner_keys => keys,
            _ => {
                i += 1;
                continue;
            }
        };

        let parent_name = specs[i].name.clone();
        let new_specs: Vec<FieldSpec> = inner_keys
            .iter()
            .map(|k| FieldSpec {
                name: format!("{parent_name}.{k}"),
                type_tag: "string".into(),
                nullable: false,
                const_value: None,
                encoding: None,
            })
            .collect();
        let n_new = new_specs.len();

        // Splice into specs: replace specs[i] with new_specs.
        specs.splice(i..i + 1, new_specs);

        // Rewrite each row: replace row.0[i] with N expanded cells.
        for row in rows.iter_mut() {
            let original = row.0.remove(i);
            let inner_obj: Option<serde_json::Map<String, Value>> = match original {
                CellValue::Scalar(Value::Object(map)) => Some(map),
                CellValue::Missing => None,
                _ => unreachable!(
                    "uniform_object_keys guarantees every cell is Scalar(Object) or Missing"
                ),
            };
            let expanded: Vec<CellValue> = inner_keys
                .iter()
                .map(|k| match &inner_obj {
                    None => CellValue::Missing,
                    Some(map) => match map.get(k) {
                        None => CellValue::Missing,
                        Some(v) => CellValue::Scalar(v.clone()),
                    },
                })
                .collect();
            for (offset, cell) in expanded.into_iter().enumerate() {
                row.0.insert(i + offset, cell);
            }
        }

        // Refine type tags + nullability from data.
        for offset in 0..n_new {
            let col_idx = i + offset;
            let mut nullable = false;
            let inferred = infer_type_tag_from_cells(rows, col_idx, &mut nullable);
            specs[col_idx].type_tag = inferred;
            specs[col_idx].nullable = nullable;
        }

        i += n_new;
    }
}

/// Stamp [`FieldSpec::const_value`] on every column whose cells are the
/// SAME scalar in every row (constant-column fold, DESIGN-style RLE for
/// constant columns).
///
/// Fold conditions (all must hold):
/// - ≥ 2 rows (a 1-row "constant" saves nothing);
/// - every cell is `CellValue::Scalar` (no `Missing` / `Nested` /
///   `OpaqueRef`) and all values are equal;
/// - the value is not `Null` and not the empty string — both render as
///   an empty CSV cell, which would make the `name:type=` declaration
///   ambiguous with "no constant".
///
/// The rows keep their cells (IR stays lossless / formatter-agnostic);
/// only formatters that understand `const_value` change their output.
fn stamp_constant_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    if rows.len() < 2 {
        return;
    }
    for (col, spec) in specs.iter_mut().enumerate() {
        let mut first: Option<&Value> = None;
        let mut constant = true;
        for row in rows {
            match row.0.get(col) {
                Some(CellValue::Scalar(v)) => match first {
                    None => first = Some(v),
                    Some(seen) if seen == v => {}
                    Some(_) => {
                        constant = false;
                        break;
                    }
                },
                _ => {
                    constant = false;
                    break;
                }
            }
        }
        let foldable = match first {
            Some(Value::Null) | None => false,
            Some(Value::String(s)) if s.is_empty() => false,
            Some(_) => constant,
        };
        if foldable {
            spec.const_value = first.cloned();
        }
    }
}

/// Stamp [`ColumnEncoding::ArithInt`] on every integer column that is an
/// EXACT arithmetic progression (`value_i == base + step * i`, constant
/// non-zero step). The CSV-schema formatter folds such a column into the
/// declaration (`name:int=BASE+STEP`) and omits it from the rows; the
/// decoder regenerates the exact values from the row index — pure
/// integer math, exact reconstruction by construction (the detection IS
/// the round-trip proof: every cell is checked against `base + step*i`).
///
/// Stamp conditions (all must hold):
/// - ≥ 3 rows (a 2-row "progression" is a coincidence with negligible
///   saving);
/// - every cell is `Scalar` of an i64 (no `Missing`/`Null`/float — the
///   column must be non-nullable by data, not just by schema);
/// - the step is constant and non-zero (a zero step is a constant
///   column — `stamp_constant_columns` already owns that fold);
/// - no overflow anywhere in `base + step * i` (checked arithmetic);
/// - at least one OTHER row-visible column remains (rows must not
///   render as empty lines);
/// - strict byte saving: the per-row cells + their commas outweigh the
///   `=BASE+STEP` declaration suffix.
fn stamp_arith_int_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    if rows.len() < 3 {
        return;
    }
    for col in 0..specs.len() {
        if specs[col].const_value.is_some() || specs[col].encoding.is_some() {
            continue;
        }
        let visible = specs
            .iter()
            .filter(|f| f.const_value.is_none() && f.encoding.is_none())
            .count();
        if visible < 2 {
            return; // folding any further column would empty the rows
        }
        if let Some((base, step)) = detect_arith_progression(rows, col) {
            let cell_bytes: usize = rows
                .iter()
                .map(|r| match r.0.get(col) {
                    Some(CellValue::Scalar(Value::Number(n))) => n.to_string().len(),
                    _ => 0,
                })
                .sum();
            let saved = cell_bytes + rows.len(); // cells + one comma per row
            let decl_extra = format!("={base}+{step}").len();
            if saved > decl_extra {
                specs[col].encoding = Some(ColumnEncoding::ArithInt { base, step });
            }
        }
    }
}

/// Stamp [`ColumnEncoding::IsoDeltaSeconds`] on every string column
/// whose EVERY value is a strict-shape ISO-8601 timestamp and whose
/// delta rendering is strictly smaller than the plain rendering.
///
/// The round-trip is PROVEN at stamp time: the column is encoded with
/// the same streaming encoder the formatter uses, decoded back, and
/// compared against every original string — only an exact match stamps.
/// Byte costs are simulated WITH ditto marks (the formatter applies
/// ditto after encoding), so the gate measures real rendered bytes.
fn stamp_iso_delta_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    use super::encodings::{decode_iso_column, encode_iso_column};

    if rows.len() < 3 {
        return;
    }
    for (col, spec) in specs.iter_mut().enumerate() {
        if spec.const_value.is_some() || spec.encoding.is_some() {
            continue;
        }
        let mut values: Vec<&str> = Vec::with_capacity(rows.len());
        let mut all_strings = true;
        for row in rows {
            match row.0.get(col) {
                Some(CellValue::Scalar(Value::String(s))) => values.push(s.as_str()),
                _ => {
                    all_strings = false;
                    break;
                }
            }
        }
        if !all_strings {
            continue;
        }
        let Some(encoded) = encode_iso_column(&values) else {
            continue;
        };
        // Prove the exact round-trip before stamping.
        match decode_iso_column(&encoded) {
            Some(decoded) if decoded == values => {}
            _ => continue,
        }
        let plain = ditto_rendered_cost(values.iter().copied());
        let enc = ditto_rendered_cost(encoded.iter().map(|s| s.as_str()));
        if enc < plain {
            spec.encoding = Some(ColumnEncoding::IsoDeltaSeconds);
        }
    }
}

/// Stamp [`ColumnEncoding::DictString`] on every low-cardinality string
/// column where a `__dict:name=v0,v1,...` line plus per-row index cells
/// render strictly smaller than the plain cells. Every distinct value
/// appears verbatim exactly once (first-appearance order) in the
/// dictionary line; reconstruction is a total index lookup, proven at
/// stamp time by decoding the index cells back and comparing.
///
/// Gates: ≥ 3 rows; every cell a scalar string; 2 ≤ distinct < rows;
/// no value contains a newline (line-grammar integrity); strict byte
/// saving measured WITH ditto on both sides using the exact formatter
/// quoting (`csv_render_str`).
fn stamp_dict_string_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    use super::formatter::csv_render_str;

    if rows.len() < 3 {
        return;
    }
    for (col, spec) in specs.iter_mut().enumerate() {
        if spec.const_value.is_some() || spec.encoding.is_some() {
            continue;
        }
        let mut values: Vec<&str> = Vec::with_capacity(rows.len());
        let mut all_strings = true;
        for row in rows {
            match row.0.get(col) {
                Some(CellValue::Scalar(Value::String(s))) => values.push(s.as_str()),
                _ => {
                    all_strings = false;
                    break;
                }
            }
        }
        if !all_strings {
            continue;
        }
        let mut seen: std::collections::HashSet<&str> = std::collections::HashSet::new();
        let mut order: Vec<&str> = Vec::new();
        for v in &values {
            if seen.insert(v) {
                order.push(v);
            }
        }
        let k = order.len();
        if k < 2 || k >= values.len() {
            continue;
        }
        if order.iter().any(|v| v.contains('\n') || v.contains('\r')) {
            continue;
        }
        let index_of: std::collections::HashMap<&str, usize> =
            order.iter().enumerate().map(|(i, v)| (*v, i)).collect();
        let idx_cells: Vec<String> = values.iter().map(|v| index_of[v].to_string()).collect();
        // Prove the exact round-trip before stamping: decode every
        // index cell back through the dictionary and compare.
        let decoded: Option<Vec<&str>> = idx_cells
            .iter()
            .map(|c| c.parse::<usize>().ok().and_then(|i| order.get(i).copied()))
            .collect();
        if decoded.as_deref() != Some(values.as_slice()) {
            continue;
        }
        let plain_cells: Vec<String> = values.iter().map(|v| csv_render_str(v)).collect();
        let plain = ditto_rendered_cost(plain_cells.iter().map(|s| s.as_str()));
        let idx_cost = ditto_rendered_cost(idx_cells.iter().map(|s| s.as_str()));
        let dict_line = "__dict:".len()
            + spec.name.len()
            + 1 // '='
            + order.iter().map(|v| csv_render_str(v).len()).sum::<usize>()
            + k.saturating_sub(1) // commas
            + 1; // newline
        if dict_line + idx_cost < plain {
            spec.encoding = Some(ColumnEncoding::DictString {
                values: order.into_iter().map(|v| v.to_string()).collect(),
            });
        }
    }
}

/// Total rendered bytes of a column's cells as the CSV formatter would
/// ship them: a cell identical to the previous one (and longer than one
/// char) costs 1 byte (`=` ditto), otherwise its own length. ISO and
/// delta cells never need CSV quoting (no commas/quotes/newlines).
fn ditto_rendered_cost<'a>(cells: impl Iterator<Item = &'a str>) -> usize {
    let mut prev: Option<&str> = None;
    let mut total = 0usize;
    for cell in cells {
        if cell.len() > 1 && prev == Some(cell) {
            total += 1;
        } else {
            total += cell.len();
        }
        prev = Some(cell);
    }
    total
}

/// `Some((base, step))` when every cell in `col` is a scalar i64 forming
/// the exact progression `base + step * i` with constant non-zero step.
fn detect_arith_progression(rows: &[Row], col: usize) -> Option<(i64, i64)> {
    let mut values: Vec<i64> = Vec::with_capacity(rows.len());
    for row in rows {
        match row.0.get(col) {
            Some(CellValue::Scalar(Value::Number(n))) => values.push(n.as_i64()?),
            _ => return None,
        }
    }
    let base = *values.first()?;
    let second = *values.get(1)?;
    let step = second.checked_sub(base)?;
    if step == 0 {
        return None;
    }
    let mut expected = base;
    for v in &values[1..] {
        expected = expected.checked_add(step)?;
        if *v != expected {
            return None;
        }
    }
    Some((base, step))
}

fn infer_type_tag_from_cells(rows: &[Row], col: usize, nullable: &mut bool) -> String {
    let mut tag = "string";
    let mut saw_value = false;
    for row in rows {
        if let Some(cell) = row.0.get(col) {
            match cell {
                CellValue::Missing => *nullable = true,
                CellValue::Scalar(Value::Null) => *nullable = true,
                CellValue::Scalar(v) => {
                    if !saw_value {
                        tag = type_tag_for(v);
                        saw_value = true;
                    } else if type_tag_for(v) != tag {
                        tag = "json";
                    }
                }
                _ => tag = "json",
            }
        }
    }
    tag.to_string()
}

/// Returns Some(inner_keys) if every row's cell at `col` is an object
/// with the same key set (or Missing). None otherwise.
fn uniform_object_keys(specs: &[FieldSpec], rows: &[Row], col: usize) -> Option<Vec<String>> {
    if specs[col].name.contains('.') {
        // Already a flattened column.
        return None;
    }
    let mut canonical: Option<Vec<String>> = None;
    let mut saw_object = false;
    for row in rows {
        let cell = row.0.get(col)?;
        match cell {
            CellValue::Missing => continue,
            CellValue::Scalar(Value::Object(map)) => {
                let keys: Vec<String> = map.keys().cloned().collect();
                saw_object = true;
                match &canonical {
                    None => canonical = Some(keys),
                    Some(existing) => {
                        if existing != &keys {
                            return None;
                        }
                    }
                }
            }
            _ => return None,
        }
    }
    if !saw_object {
        return None;
    }
    canonical
}

fn infer_type_tag(items: &[Value], key: &str) -> String {
    let mut tag: Option<&'static str> = None;
    for it in items {
        if let Some(v) = it.as_object().and_then(|m| m.get(key)) {
            if matches!(v, Value::Null) {
                continue;
            }
            let t = type_tag_for(v);
            match tag {
                None => tag = Some(t),
                Some(existing) if existing != t => {
                    tag = Some("json");
                    break;
                }
                _ => {}
            }
        }
    }
    tag.unwrap_or("string").to_string()
}

fn type_tag_for(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_i64() || n.is_u64() => "int",
        Value::Number(_) => "float",
        Value::String(_) => "string",
        Value::Object(_) | Value::Array(_) => "json",
    }
}

fn hash_opaque(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    let digest = h.finalize();
    // 12-char hex prefix — collision-resistant enough for a single
    // payload in flight, short enough to keep the marker compact.
    let hex: String = digest.iter().take(6).map(|b| format!("{b:02x}")).collect();
    hex
}

// ─────────────────────────── heterogeneous bucketing ───────────────────────────

/// Find a discriminator field — string-typed, present in every row,
/// with a value distribution that partitions cleanly into 2..=max_buckets
/// non-trivial buckets.
fn detect_discriminator(
    items: &[Value],
    key_freqs: &BTreeMap<String, usize>,
    cfg: &CompactConfig,
) -> Option<String> {
    let total = items.len();
    let mut best: Option<(String, usize)> = None; // (key, bucket_count)

    for (k, &freq) in key_freqs {
        if freq < total {
            continue; // must be present in every row
        }
        // Collect values; require all strings.
        let mut values: Vec<&str> = Vec::with_capacity(total);
        let mut all_strings = true;
        for item in items {
            match item.as_object().and_then(|m| m.get(k)) {
                Some(Value::String(s)) => values.push(s.as_str()),
                _ => {
                    all_strings = false;
                    break;
                }
            }
        }
        if !all_strings {
            continue;
        }
        let mut distinct: std::collections::HashSet<&str> = std::collections::HashSet::new();
        for v in &values {
            distinct.insert(*v);
        }
        let n = distinct.len();
        if n < cfg.min_buckets || n > cfg.max_buckets {
            continue;
        }
        // Reject discriminators that are essentially unique (1 row per
        // bucket — that's an ID, not a category).
        if n as f64 / total as f64 > 0.7 {
            continue;
        }
        let score = n; // prefer more buckets up to max
        match &best {
            None => best = Some((k.clone(), score)),
            Some((_, s)) if score > *s => best = Some((k.clone(), score)),
            _ => {}
        }
    }
    best.map(|(k, _)| k)
}

fn bucket_by(items: &[Value], discriminator: &str, cfg: &CompactConfig) -> Compaction {
    let mut groups: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for item in items {
        let key = item
            .as_object()
            .and_then(|m| m.get(discriminator))
            .and_then(|v| v.as_str())
            .unwrap_or("__missing__")
            .to_string();
        groups.entry(key).or_default().push(item.clone());
    }
    let buckets: Vec<Bucket> = groups
        .into_iter()
        .map(|(key, group_items)| {
            let inner = compact(&group_items, cfg);
            match inner {
                Compaction::Table { schema, rows, .. } => Bucket {
                    key: Value::String(key),
                    schema,
                    rows,
                },
                _ => {
                    // Sub-compaction declined — fall back to a degenerate
                    // single-column "value" table holding the raw items.
                    Bucket {
                        key: Value::String(key),
                        schema: Schema {
                            fields: vec![FieldSpec {
                                name: "value".into(),
                                type_tag: "json".into(),
                                nullable: false,
                                const_value: None,
                                encoding: None,
                            }],
                        },
                        rows: group_items
                            .into_iter()
                            .map(|v| Row::new(vec![CellValue::Scalar(v)]))
                            .collect(),
                    }
                }
            }
        })
        .collect();
    Compaction::Buckets {
        discriminator: discriminator.to_string(),
        buckets,
        original_count: items.len(),
    }
}

#[cfg(test)]
mod tests {
    use super::super::ir::OpaqueKind;
    use super::*;
    use serde_json::json;

    fn cfg() -> CompactConfig {
        CompactConfig::default()
    }

    #[test]
    fn empty_or_single_is_untouched() {
        let items: Vec<Value> = vec![];
        assert!(matches!(compact(&items, &cfg()), Compaction::Untouched(_)));
        let items = vec![json!({"a": 1})];
        assert!(matches!(compact(&items, &cfg()), Compaction::Untouched(_)));
    }

    #[test]
    fn non_object_array_is_untouched() {
        let items = vec![json!(1), json!(2), json!(3)];
        assert!(matches!(compact(&items, &cfg()), Compaction::Untouched(_)));
    }

    #[test]
    fn pure_tabular_produces_table() {
        let items = vec![
            json!({"id": 1, "name": "alice", "status": "ok"}),
            json!({"id": 2, "name": "bob", "status": "ok"}),
            json!({"id": 3, "name": "carol", "status": "fail"}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table {
                schema,
                rows,
                original_count,
            } => {
                assert_eq!(original_count, 3);
                assert_eq!(rows.len(), 3);
                let names = schema.field_names();
                assert!(names.contains(&"id"));
                assert!(names.contains(&"name"));
                assert!(names.contains(&"status"));
                // Type inference
                let id_spec = schema.fields.iter().find(|f| f.name == "id").unwrap();
                assert_eq!(id_spec.type_tag, "int");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn nested_uniform_is_flattened() {
        let items = vec![
            json!({"id": 1, "meta": {"region": "us", "tier": "gold"}}),
            json!({"id": 2, "meta": {"region": "eu", "tier": "silver"}}),
            json!({"id": 3, "meta": {"region": "us", "tier": "bronze"}}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, rows, .. } => {
                let names = schema.field_names();
                assert!(names.contains(&"meta.region"), "got {names:?}");
                assert!(names.contains(&"meta.tier"), "got {names:?}");
                assert!(!names.contains(&"meta"));
                assert_eq!(rows[0].len(), schema.fields.len());
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn nested_mixed_keys_stay_nested() {
        let items = vec![
            json!({"id": 1, "meta": {"region": "us"}}),
            json!({"id": 2, "meta": {"region": "eu", "tier": "silver"}}),
            json!({"id": 3, "meta": {"tier": "bronze"}}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                // No flatten — all-different key sets per row
                assert!(names.contains(&"meta"));
                assert!(!names.iter().any(|n| n.starts_with("meta.")));
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn stringified_json_array_recurses() {
        let items = vec![
            json!({"event": "batch", "payload": r#"[{"x":1},{"x":2},{"x":3}]"#}),
            json!({"event": "batch", "payload": r#"[{"x":4},{"x":5}]"#}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { rows, .. } => {
                // payload column should be Nested(Compaction::Table).
                let payload_idx = 1; // depends on order; check both
                let cell0 = &rows[0].0[0];
                let cell1 = &rows[0].0[1];
                let nested_count = [cell0, cell1]
                    .iter()
                    .filter(|c| matches!(***c, CellValue::Nested(_)))
                    .count();
                let _ = payload_idx;
                assert_eq!(nested_count, 1, "expected exactly one Nested cell");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn opaque_cell_becomes_ccr_ref() {
        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let items = vec![
            json!({"id": 1, "blob": big.clone()}),
            json!({"id": 2, "blob": big.clone()}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { rows, schema, .. } => {
                let blob_idx = schema
                    .fields
                    .iter()
                    .position(|f| f.name == "blob")
                    .expect("blob col");
                match &rows[0].0[blob_idx] {
                    CellValue::OpaqueRef {
                        ccr_hash,
                        byte_size,
                        kind,
                    } => {
                        assert!(!ccr_hash.is_empty());
                        assert_eq!(*byte_size, big.len());
                        assert_eq!(*kind, OpaqueKind::Base64Blob);
                    }
                    other => panic!("expected OpaqueRef, got {other:?}"),
                }
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn opaque_blob_original_is_persisted_under_marker_hash() {
        // Defect 2: when a CCR store is wired into the compact config,
        // an opaque-blob substitution MUST persist the original bytes
        // under the SAME hash the `OpaqueRef` / rendered marker carries,
        // so a consumer holding only the output can recover it. Without
        // a store wired in (the pure-function default) nothing is
        // persisted — that path is for tests/parity formatters only.
        use crate::ccr::InMemoryCcrStore;

        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let other = "ZYXWVUTSRQPONMLKJIHGFEDCBA9876543210zyxwvutsrqponmlkjihgfedcba+/=".repeat(8);
        let items = vec![
            json!({"id": 1, "blob": big.clone()}),
            json!({"id": 2, "blob": other.clone()}),
        ];

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let config = CompactConfig {
            ccr_store: Some(Arc::clone(&store)),
            ..Default::default()
        };

        let hash_big = hash_opaque(big.as_bytes());
        let hash_other = hash_opaque(other.as_bytes());

        // Pure default (no store) → nothing persisted.
        let pure_store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let _ = compact(&items, &cfg());
        assert!(
            pure_store.get(&hash_big).is_none(),
            "pure compact must not write to an unrelated store"
        );
        assert_eq!(store.len(), 0, "no compaction has run with the store yet");

        // With a store wired in → every distinct blob is recoverable
        // under the marker hash.
        let c = compact(&items, &config);
        assert!(c.was_compacted());
        assert_eq!(
            store.get(&hash_big).as_deref(),
            Some(big.as_str()),
            "first blob must be retrievable under its marker hash"
        );
        assert_eq!(
            store.get(&hash_other).as_deref(),
            Some(other.as_str()),
            "second (distinct) blob must be retrievable under its marker hash"
        );
        assert_eq!(store.len(), 2, "both distinct blobs persisted");
    }

    #[test]
    fn compaction_stage_with_store_persists_opaque_originals_end_to_end() {
        // End-to-end through the real production seam: a CSV-schema
        // CompactionStage with a CCR store wired in renders `<<ccr:...>>`
        // markers AND persists the originals. This is the seam the
        // public `compress()` path uses (Defect 2 fix point).
        use super::super::CompactionStage;
        use crate::ccr::InMemoryCcrStore;

        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let items = vec![
            json!({"id": 1, "tag": "x", "blob": big.clone()}),
            json!({"id": 2, "tag": "x", "blob": big.clone()}),
        ];
        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let stage = CompactionStage::default_csv_schema().with_ccr_store(Arc::clone(&store));

        let (c, rendered) = stage.run(&items);
        assert!(c.was_compacted());
        assert!(
            rendered.contains("<<ccr:"),
            "marker must render: {rendered}"
        );

        let hash_big = hash_opaque(big.as_bytes());
        assert!(
            rendered.contains(&hash_big),
            "rendered marker must carry the persisted hash"
        );
        assert_eq!(
            store.get(&hash_big).as_deref(),
            Some(big.as_str()),
            "original blob recoverable from the store keyed by the marker hash"
        );
    }

    #[test]
    fn heterogeneous_array_buckets_by_discriminator() {
        let items = vec![
            json!({"type": "user", "id": 1, "name": "alice"}),
            json!({"type": "user", "id": 2, "name": "bob"}),
            json!({"type": "order", "id": 99, "total": 50}),
            json!({"type": "order", "id": 100, "total": 75}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Buckets {
                discriminator,
                buckets,
                original_count,
            } => {
                assert_eq!(discriminator, "type");
                assert_eq!(buckets.len(), 2);
                assert_eq!(original_count, 4);
                let total_rows: usize = buckets.iter().map(|b| b.rows.len()).sum();
                assert_eq!(total_rows, 4);
            }
            other => panic!("expected Buckets, got {other:?}"),
        }
    }

    #[test]
    fn id_like_field_not_chosen_as_discriminator() {
        // Every "id" is unique → reject as discriminator.
        let items = vec![
            json!({"id": "a1", "kind": "x"}),
            json!({"id": "a2", "kind": "x"}),
            json!({"id": "a3", "kind": "y"}),
            json!({"id": "a4", "kind": "y"}),
        ];
        // Schema is well-defined (homogeneous) so we won't even enter
        // the discriminator path. But verify directly.
        let mut freqs = BTreeMap::new();
        freqs.insert("id".to_string(), 4);
        freqs.insert("kind".to_string(), 4);
        let disc = detect_discriminator(&items, &freqs, &cfg());
        assert_eq!(disc.as_deref(), Some("kind"));
    }

    #[test]
    fn stable_field_ordering() {
        // Frequency descending then alphabetical.
        let items = vec![
            json!({"common": 1, "z_rare": 1}),
            json!({"common": 2, "a_rare": 1}),
            json!({"common": 3}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                assert_eq!(schema.fields[0].name, "common");
                // Two rare fields with same freq: alphabetical
                assert_eq!(schema.fields[1].name, "a_rare");
                assert_eq!(schema.fields[2].name, "z_rare");
            }
            other => panic!("got {other:?}"),
        }
    }

    #[test]
    fn nullable_field_marked() {
        let items = vec![
            json!({"id": 1, "tag": "a"}),
            json!({"id": 2}),
            json!({"id": 3, "tag": null}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let tag = schema.fields.iter().find(|f| f.name == "tag").unwrap();
                assert!(tag.nullable);
                let id = schema.fields.iter().find(|f| f.name == "id").unwrap();
                assert!(!id.nullable);
            }
            other => panic!("got {other:?}"),
        }
    }

    #[test]
    fn constant_columns_are_stamped() {
        let items = vec![
            json!({"bytes": 64, "from": "127.0.0.1", "seq": 0, "t": 0.1}),
            json!({"bytes": 64, "from": "127.0.0.1", "seq": 1, "t": 0.2}),
            json!({"bytes": 64, "from": "127.0.0.1", "seq": 2, "t": 0.1}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, rows, .. } => {
                let by_name = |n: &str| {
                    schema
                        .fields
                        .iter()
                        .find(|f| f.name == n)
                        .unwrap_or_else(|| panic!("missing field {n}"))
                };
                assert_eq!(by_name("bytes").const_value, Some(json!(64)));
                assert_eq!(by_name("from").const_value, Some(json!("127.0.0.1")));
                assert_eq!(by_name("seq").const_value, None);
                assert_eq!(by_name("t").const_value, None);
                // IR rows still carry the full cells (formatter-agnostic).
                assert_eq!(rows[0].len(), schema.fields.len());
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn null_and_empty_constants_are_not_folded() {
        let items = vec![
            json!({"id": 1, "n": null, "e": ""}),
            json!({"id": 2, "n": null, "e": ""}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                for f in &schema.fields {
                    assert_eq!(f.const_value, None, "field {} must not fold", f.name);
                }
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn arith_progression_is_stamped_with_exact_base_and_step() {
        let items: Vec<Value> = (0..10)
            .map(|i| json!({"seq": 5 + 2 * i, "v": format!("x{i}")}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, rows, .. } => {
                let seq = schema.fields.iter().find(|f| f.name == "seq").unwrap();
                assert_eq!(
                    seq.encoding,
                    Some(ColumnEncoding::ArithInt { base: 5, step: 2 })
                );
                // IR rows still carry the full cells (formatter-agnostic).
                assert_eq!(rows[0].len(), schema.fields.len());
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn arith_not_stamped_on_two_rows_or_nonconstant_step() {
        let two = vec![json!({"n": 1, "v": "a"}), json!({"n": 2, "v": "b"})];
        match compact(&two, &cfg()) {
            Compaction::Table { schema, .. } => {
                let n = schema.fields.iter().find(|f| f.name == "n").unwrap();
                assert_eq!(n.encoding, None, "2-row progression must not stamp");
            }
            other => panic!("expected Table, got {other:?}"),
        }
        let jagged = vec![
            json!({"n": 1, "v": "a"}),
            json!({"n": 2, "v": "b"}),
            json!({"n": 4, "v": "c"}),
        ];
        match compact(&jagged, &cfg()) {
            Compaction::Table { schema, .. } => {
                let n = schema.fields.iter().find(|f| f.name == "n").unwrap();
                assert_eq!(n.encoding, None, "non-constant step must not stamp");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn arith_not_stamped_when_it_would_empty_rows() {
        // Constants fold bytes/ttl; folding seq too would leave empty
        // row lines — the gate keeps the last visible column.
        let items: Vec<Value> = (0..20)
            .map(|i| json!({"bytes": 64, "seq": i, "ttl": 64}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let seq = schema.fields.iter().find(|f| f.name == "seq").unwrap();
                assert_eq!(seq.encoding, None);
                assert_eq!(seq.const_value, None);
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn arith_not_stamped_on_float_or_nullable_columns() {
        let floats: Vec<Value> = (0..5)
            .map(|i| json!({"x": i as f64 + 0.5, "v": format!("a{i}")}))
            .collect();
        match compact(&floats, &cfg()) {
            Compaction::Table { schema, .. } => {
                let x = schema.fields.iter().find(|f| f.name == "x").unwrap();
                assert_eq!(x.encoding, None, "float column must not stamp");
            }
            other => panic!("expected Table, got {other:?}"),
        }
        let sparse = vec![
            json!({"n": 0, "v": "a"}),
            json!({"n": 1, "v": "b"}),
            json!({"v": "c"}),
        ];
        match compact(&sparse, &cfg()) {
            Compaction::Table { schema, .. } => {
                let n = schema.fields.iter().find(|f| f.name == "n").unwrap();
                assert_eq!(n.encoding, None, "missing cell must block the fold");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn arith_detection_is_i64_safe() {
        // Values at the i64 ceiling fold without overflow (checked
        // arithmetic), and u64-beyond-i64 values never stamp.
        let at_ceiling: Vec<Value> = (0..3i64)
            .map(|i| json!({"n": i64::MAX - 2 + i, "v": format!("x{i}")}))
            .collect();
        match compact(&at_ceiling, &cfg()) {
            Compaction::Table { schema, .. } => {
                let n = schema.fields.iter().find(|f| f.name == "n").unwrap();
                assert_eq!(
                    n.encoding,
                    Some(ColumnEncoding::ArithInt {
                        base: i64::MAX - 2,
                        step: 1
                    })
                );
            }
            other => panic!("expected Table, got {other:?}"),
        }
        let beyond = vec![
            json!({"n": u64::MAX - 2, "v": "a"}),
            json!({"n": u64::MAX - 1, "v": "b"}),
            json!({"n": u64::MAX, "v": "c"}),
        ];
        match compact(&beyond, &cfg()) {
            Compaction::Table { schema, .. } => {
                let n = schema.fields.iter().find(|f| f.name == "n").unwrap();
                assert_eq!(n.encoding, None, "u64-beyond-i64 must not stamp");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn iso_delta_is_stamped_on_strict_timestamp_columns() {
        let items: Vec<Value> = (0..6)
            .map(|i| {
                json!({
                    "date": format!("2026-06-1{}T0{}:00:00+02:00", i % 3 + 1, i),
                    "v": format!("x{i}"),
                })
            })
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let date = schema.fields.iter().find(|f| f.name == "date").unwrap();
                assert_eq!(date.encoding, Some(ColumnEncoding::IsoDeltaSeconds));
                let v = schema.fields.iter().find(|f| f.name == "v").unwrap();
                assert_eq!(v.encoding, None);
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn iso_delta_never_stamps_constant_or_short_columns() {
        // A constant timestamp column is owned by the const fold.
        let constant: Vec<Value> = (0..5)
            .map(|i| json!({"date": "2026-06-11T21:02:05Z", "v": format!("x{i}")}))
            .collect();
        match compact(&constant, &cfg()) {
            Compaction::Table { schema, .. } => {
                let date = schema.fields.iter().find(|f| f.name == "date").unwrap();
                assert!(date.const_value.is_some());
                assert_eq!(date.encoding, None);
            }
            other => panic!("expected Table, got {other:?}"),
        }
        // Two rows: below the minimum.
        let short = vec![
            json!({"date": "2026-06-11T21:02:05Z", "v": "a"}),
            json!({"date": "2026-06-11T22:02:05Z", "v": "b"}),
        ];
        match compact(&short, &cfg()) {
            Compaction::Table { schema, .. } => {
                let date = schema.fields.iter().find(|f| f.name == "date").unwrap();
                assert_eq!(date.encoding, None);
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn missing_cell_blocks_constant_fold() {
        // Same value where present, but absent in one row → not constant.
        let items = vec![
            json!({"id": 1, "tag": "x"}),
            json!({"id": 2, "tag": "x"}),
            json!({"id": 3}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let tag = schema.fields.iter().find(|f| f.name == "tag").unwrap();
                assert_eq!(tag.const_value, None);
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn hash_opaque_stable_and_short() {
        let h1 = hash_opaque(b"hello world");
        let h2 = hash_opaque(b"hello world");
        let h3 = hash_opaque(b"different");
        assert_eq!(h1, h2);
        assert_ne!(h1, h3);
        assert_eq!(h1.len(), 12);
    }
}
