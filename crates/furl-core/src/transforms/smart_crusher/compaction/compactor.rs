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
//!
//!   HONEST CONTRACT (COR-14): the wire grammar records nothing about the
//!   flatten, so the reference decoder reconstructs such rows with dotted
//!   TOP-LEVEL keys (`{"meta.region": ...}`), not the original nesting.
//!   Reconstruction is **value-exact under dotted keys** — every value is
//!   exact, the nesting shape is not restored. `csv_schema_decoder.py`
//!   documents the same caveat on the consumer side, and
//!   `verify/independent_recheck.py` compares both sides un-flattened.
//!   Shapes where even that equivalence would mis-bind a value — see
//!   [`flatten_breaks_dotted_equivalence`] — never flatten (fail closed).
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
    /// look for a discriminator. Default: 0.6.
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

    /// Whether opaque-classified cells are substituted with
    /// `<<ccr:HASH,...>>` pointers (default `true`). The substitution
    /// pair — store write + `OpaqueRef` cell — happens EAGERLY during
    /// `compact()`, before any caller accept/decline decision, so
    /// callers that must never hide visible bytes cannot merely reject
    /// the rendered output: the write would already have happened. Set
    /// `false` (the crusher's `lossless_only` strict mode does, via
    /// `SmartCrusherBuilder::build`) to keep opaque cells as verbatim
    /// `Scalar`s — no pointer, no store write; the render either wins
    /// as a pure rearrangement or fails the savings gate.
    pub substitute_opaque: bool,
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
            .field("substitute_opaque", &self.substitute_opaque)
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
            substitute_opaque: true,
        }
    }
}

/// Top-level compaction entry point.
pub fn compact(items: &[Value], cfg: &CompactConfig) -> Compaction {
    use super::formatter::column_name_breaks_grammar;

    if items.len() < cfg.min_items {
        return Compaction::Untouched;
    }
    if !items.iter().all(|v| matches!(v, Value::Object(_))) {
        return Compaction::Untouched;
    }

    let key_freqs = compute_key_freqs(items);

    // COR-15 fail-closed gate: column names ship RAW in the `[N]{...}`
    // declaration and the preamble lines (nothing quotes them), so a key
    // like `meta:region` silently mis-keys every decoded row AND
    // desynchronizes the `__affix:` preamble (values lose their affix,
    // arith folds shift by a row). Decline compaction — the array keeps
    // its verbatim JSON shape and merely skips the lossless tier.
    if key_freqs.keys().any(|k| column_name_breaks_grammar(k)) {
        return Compaction::Untouched;
    }
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
    stamp_decimal_scaled_columns(&mut field_specs, &rows);
    stamp_dict_string_columns(&mut field_specs, &rows);
    stamp_head_dict_columns(&mut field_specs, &rows);
    stamp_affix_columns(&mut field_specs, &rows);

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
                    let sub = compact(items, cfg);
                    if sub.was_compacted() {
                        return CellValue::Nested(Box::new(sub));
                    }
                    // Inner compaction declined (`Untouched` is
                    // payload-free, PERF-5): carry the original value —
                    // renders byte-identically to the old
                    // `Nested(Untouched(...))` and stays out of the
                    // decoder-verifiable tier.
                    return CellValue::DeclinedJson(v.clone());
                }
            }
            CellValue::Scalar(v.clone())
        }
        CellClass::StringifiedJson(_) => {
            // T2 fidelity: a value that ORIGINATED as a string is kept as the
            // EXACT original string bytes — never deserialized. Parsing it
            // (the old behaviour) let `flatten_uniform_nested` promote
            // object-strings into dotted columns so the original string field
            // vanished, and re-serialized array-strings dropped their interior
            // whitespace — both silent, markerless corruption on the lossless
            // path. `classify_string` still parses to gate CCR/opaque routing,
            // but the cell it yields here is the verbatim source string.
            //
            // A container-string that lands in a type-mixed (`json`-tagged)
            // column is then declined from the lossless tier by
            // `Compaction::is_decoder_verifiable`: a quoted container-string
            // is indistinguishable from a real container cell to the reference
            // decoder, so the array routes to the recoverable tier instead.
            CellValue::Scalar(v.clone())
        }
        CellClass::Opaque(kind) => {
            // Strict lossless-or-passthrough callers disable substitution
            // entirely: the cell stays a verbatim Scalar, no pointer is
            // minted, and — critically — no store write happens (the
            // Defect-2 write below is EAGER; a caller-side render
            // rejection would come too late to prevent it).
            if !cfg.substitute_opaque {
                return CellValue::Scalar(v.clone());
            }
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
///
/// COR-14 contract note: the flatten is NOT recorded in the wire grammar,
/// so the decoder cannot un-flatten — decoded rows carry the dotted
/// column names as top-level keys. "Lossless" for a flattened table means
/// value-exact under dotted keys, not shape-exact reconstruction. Shapes
/// where even THAT equivalence would break — an empty parent name, inner
/// keys with empty dot-segments, prefix-overlapping inner keys, or a
/// sibling column on a strict prefix of a synthesized dotted path — skip
/// the flatten entirely ([`flatten_breaks_dotted_equivalence`]) and stay
/// nested-object cells, which decode byte-exact.
fn flatten_uniform_nested(specs: &mut Vec<FieldSpec>, rows: &mut [Row], cfg: &CompactConfig) {
    use super::formatter::column_name_breaks_grammar;

    let mut i = 0;
    while i < specs.len() {
        let inner_keys = match uniform_object_keys(specs, rows, i) {
            // COR-15: a flattened `parent.inner` column name ships RAW in
            // the declaration too — an inner key carrying grammar chars
            // would corrupt it the same way, so the column stays a nested
            // object cell (CSV-quoted JSON) instead of flattening.
            Some(keys)
                if !keys.is_empty()
                    && keys.len() <= cfg.max_flatten_inner_keys
                    && !keys.iter().any(|k| column_name_breaks_grammar(k)) =>
            {
                keys
            }
            _ => {
                i += 1;
                continue;
            }
        };

        let parent_name = specs[i].name.clone();

        // T12 fail-closed: synthesized `parent.key` columns ship as RAW names
        // in the declaration with no free-name check. If any collides with an
        // existing column — a literal top-level `parent.key`, or a sibling
        // already flattened to the same dotted name — flattening would emit
        // two identically named columns and the reference decoder would
        // silently OVERWRITE one value (last write wins). Skip the flatten for
        // this column; it stays a nested-object cell (CSV-quoted JSON, decoded
        // back to the object) so both distinct values survive.
        let collides = inner_keys.iter().any(|k| {
            let synthesized = format!("{parent_name}.{k}");
            specs
                .iter()
                .enumerate()
                .any(|(idx, s)| idx != i && s.name == synthesized)
        });
        if collides {
            i += 1;
            continue;
        }

        // COR-14 fail-closed: even with no name collision, a synthesized
        // dotted name can still be AMBIGUOUS about the original nesting
        // (`{"a": {"b.c": 1}}` and `{"a.b": {"c": 1}}` both synthesize
        // `a.b.c`). Where that ambiguity can bind a value to the wrong
        // path, skip the flatten; the column stays a nested-object cell
        // and decodes byte-exact.
        if flatten_breaks_dotted_equivalence(&parent_name, &inner_keys, specs) {
            i += 1;
            continue;
        }

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

/// COR-14 ambiguity guard: true when flattening `parent` would break
/// value-exactness under dotted-key canonicalization, the documented
/// equivalence for flattened tables (`verify/independent_recheck.
/// _unflatten_dotted`: non-dotted keys fold first, then dotted keys in
/// sorted order, and a key stays literal when nesting would clobber
/// existing data).
///
/// A dot-free, non-empty parent with dot-free inner keys is always safe:
/// the leaf slot `out[parent][k]` cannot be reached or blocked by any
/// other column once the synthesized-name collision check has passed, so
/// original and decoded fold identically regardless of siblings. Four
/// shapes break that symmetry, each reproduced as a live silent-corruption
/// round trip before this guard existed:
///
/// - **empty parent name**: `{"": {"k": 1}}` synthesizes `.k`, whose
///   leading empty segment the canonicalizer keeps LITERAL at top level,
///   while the original folds `k` inside the `""` subtree;
/// - **empty inner key / empty dot-segment** (`""`, `"b."`): same
///   literal-vs-nested split (`{"p": {"": 1}}` decoded as `{"p.": 1}`);
/// - **prefix-overlapping inner keys** (`{"b", "b.c"}`): whether the
///   `b.c` leaf nests under sibling value `b` depends on that value's own
///   keys row by row — data-dependent, so fail closed;
/// - **sibling column on a strict prefix of a synthesized dotted path**
///   (column `a.b` beside parent `a` with inner `b.c`): the original
///   folds `b.c` inside the parent's sandboxed subtree, the decoded folds
///   it in the global namespace where the sibling blocks the path,
///   silently swapping which value owns `a.b.c`.
///
/// Siblings that EXTEND a synthesized path (`a.b.c.d`) or sit on a
/// disjoint branch (`a.j`) fold identically on both sides and keep the
/// flatten, so metrics-style dotted inner keys (`{"m": {"cpu.usage": 1}}`
/// alone) still compress.
fn flatten_breaks_dotted_equivalence(
    parent: &str,
    inner_keys: &[String],
    specs: &[FieldSpec],
) -> bool {
    if parent.is_empty() {
        return true;
    }
    if inner_keys
        .iter()
        .any(|k| k.is_empty() || k.split('.').any(|seg| seg.is_empty()))
    {
        return true;
    }
    if !inner_keys.iter().any(|k| k.contains('.')) {
        return false;
    }
    let paths: Vec<Vec<&str>> = inner_keys.iter().map(|k| k.split('.').collect()).collect();
    let prefix_overlap = paths.iter().enumerate().any(|(x, px)| {
        paths
            .iter()
            .enumerate()
            .any(|(y, py)| x != y && py.len() > px.len() && py[..px.len()] == px[..])
    });
    if prefix_overlap {
        return true;
    }
    paths.iter().filter(|p| p.len() > 1).any(|path| {
        (1..path.len()).any(|j| {
            let prefix_name = format!("{parent}.{}", path[..j].join("."));
            specs.iter().any(|s| s.name == prefix_name)
        })
    })
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

/// Stamp [`ColumnEncoding::DecimalScaled`] on every float column whose
/// values all render as plain decimals (`-?\d+\.\d{1,6}`, no exponent).
/// Cells become the integer value × 10^scale via PURE STRING
/// MANIPULATION — no float arithmetic — and the round-trip is proven at
/// stamp time: each encoded cell is decoded back to a decimal string,
/// parsed as f64, re-rendered, and compared to the original rendering.
/// Strict byte gate WITH ditto plus the `%scale` declaration suffix.
fn stamp_decimal_scaled_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    use super::encodings::{decimal_frac_digits, decode_decimal_cell, encode_decimal_cell};

    if rows.len() < 3 {
        return;
    }
    for (col, spec) in specs.iter_mut().enumerate() {
        if spec.const_value.is_some() || spec.encoding.is_some() {
            continue;
        }
        // Collect every cell's serde rendering; bail on anything that
        // is not a plain-decimal float.
        let mut rendered: Vec<String> = Vec::with_capacity(rows.len());
        let mut scale = 0usize;
        let mut eligible = true;
        for row in rows {
            match row.0.get(col) {
                Some(CellValue::Scalar(Value::Number(n))) if !n.is_i64() && !n.is_u64() => {
                    let r = n.to_string();
                    match decimal_frac_digits(&r) {
                        Some(k) => scale = scale.max(k),
                        None => {
                            eligible = false;
                            break;
                        }
                    }
                    rendered.push(r);
                }
                _ => {
                    eligible = false;
                    break;
                }
            }
        }
        if !eligible || scale == 0 {
            continue;
        }
        let encoded: Option<Vec<String>> = rendered
            .iter()
            .map(|r| encode_decimal_cell(r, scale))
            .collect();
        let Some(encoded) = encoded else { continue };
        // Prove the round-trip: decode -> parse f64 -> re-render must
        // equal the original serde rendering for EVERY cell.
        let round_trip_ok = encoded.iter().zip(rendered.iter()).all(|(cell, orig)| {
            decode_decimal_cell(cell, scale)
                .and_then(|dec| dec.parse::<f64>().ok())
                .and_then(serde_json::Number::from_f64)
                .map(|n| n.to_string() == *orig)
                .unwrap_or(false)
        });
        if !round_trip_ok {
            continue;
        }
        let plain = ditto_rendered_cost(rendered.iter().map(|s| s.as_str()));
        let enc = ditto_rendered_cost(encoded.iter().map(|s| s.as_str()));
        let decl_extra = 1 + scale.to_string().len(); // "%k"
        if enc + decl_extra < plain {
            spec.encoding = Some(ColumnEncoding::DecimalScaled { scale });
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

/// Stamp [`ColumnEncoding::HeadDict`] on a plain string column whose
/// values split at a delimiter into a LOW-cardinality head and a unique
/// tail (paths grouped under a few directories, namespaced keys under a
/// few prefixes, dotted module names under a few packages). The distinct
/// heads are declared once; each row carries `<head_index><delim><tail>`.
///
/// For each delimiter in [`HEAD_DELIMS`] priority order, the column is
/// split at the LAST occurrence; the first delimiter that (a) splits
/// EVERY cell, (b) yields 2..(rows) distinct heads, and (c) renders
/// strictly smaller wins. The round-trip is proven at stamp time:
/// every cell is split, indexed, re-encoded, decoded, and rejoined back
/// to the original.
///
/// Runs BEFORE the affix stamp: when the leading segment is
/// low-cardinality this is the larger fold; affix then catches any
/// whole-column shared affix head-dict did not claim.
fn stamp_head_dict_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    use super::encodings::{
        decode_head_cell, decode_head_value, encode_head_cell, split_head, HEAD_DELIMS,
    };
    use super::formatter::csv_render_str;

    // Row-count floor: a head dictionary's one-time cost only reliably
    // amortizes — in BYTES and (crucially) in tokens — when many rows
    // reuse the few heads. On small arrays the `<idx><delim>` cells can be
    // byte-smaller yet tokenize LARGER (the index+delimiter fragments
    // familiar path tokens), so the per-column byte gate is not sufficient.
    // A 16-row floor keeps head-dict in the regime where the win is robust.
    const MIN_HEAD_DICT_ROWS: usize = 16;
    if rows.len() < MIN_HEAD_DICT_ROWS {
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

        let plain_cells: Vec<String> = values.iter().map(|v| csv_render_str(v)).collect();
        let plain = ditto_rendered_cost(plain_cells.iter().map(|s| s.as_str()));

        for &delim in &HEAD_DELIMS {
            // Split every cell; bail on the first that lacks the delimiter.
            let mut splits: Vec<(&str, &str)> = Vec::with_capacity(values.len());
            let mut ok = true;
            for v in &values {
                match split_head(v, delim) {
                    Some(pair) => splits.push(pair),
                    None => {
                        ok = false;
                        break;
                    }
                }
            }
            if !ok {
                continue;
            }
            // Distinct heads, first-appearance order.
            let mut seen: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
            let mut order: Vec<&str> = Vec::new();
            for (head, _) in &splits {
                if !seen.contains_key(head) {
                    seen.insert(head, order.len());
                    order.push(head);
                }
            }
            let k = order.len();
            if k < 2 || k >= values.len() {
                continue; // no head sharing, or every head distinct
            }
            // A head containing a newline would break the single-line
            // preamble grammar.
            if order.iter().any(|h| h.contains('\n') || h.contains('\r')) {
                continue;
            }
            // Encode every cell and PROVE the exact round-trip.
            let mut cells: Vec<String> = Vec::with_capacity(values.len());
            let mut round_trip_ok = true;
            for ((head, tail), orig) in splits.iter().zip(values.iter()) {
                let idx = seen[head];
                let cell = encode_head_cell(idx, delim, tail);
                match decode_head_cell(&cell, delim) {
                    Some((didx, dtail))
                        if didx == idx
                            && didx < order.len()
                            && decode_head_value(order[didx], dtail) == **orig => {}
                    _ => {
                        round_trip_ok = false;
                        break;
                    }
                }
                cells.push(cell);
            }
            if !round_trip_ok {
                continue;
            }
            // Byte gate WITH ditto + the one-time `__head:` line.
            let enc_cells: Vec<String> = cells.iter().map(|c| csv_render_str(c)).collect();
            let enc = ditto_rendered_cost(enc_cells.iter().map(|s| s.as_str()));
            let head_line = "__head:".len()
                + spec.name.len()
                + 1 // '='
                + delim.len_utf8()
                + order.iter().map(|h| csv_render_str(h).len()).sum::<usize>()
                + k.saturating_sub(1) // commas
                + 1 // newline
                + 1; // the `@` declaration marker
            if head_line + enc < plain {
                spec.encoding = Some(ColumnEncoding::HeadDict {
                    delim,
                    heads: order.into_iter().map(|h| h.to_string()).collect(),
                });
                break; // first winning delimiter claims the column
            }
        }
    }
}

/// Stamp [`ColumnEncoding::Affix`] on every plain string column whose
/// every cell shares a common byte prefix and/or suffix (the structure
/// that repeats across near-unique rows: shared path roots, URL roots,
/// fixed key/template heads, file extensions). The affix is declared
/// once on a `__affix:name=PREFIX,SUFFIX` preamble line and each row
/// carries only its unique middle; reconstruction is pure byte
/// concatenation (`prefix + middle + suffix`), proven at stamp time by
/// stripping and rejoining every cell and comparing to the original.
///
/// Runs AFTER the dictionary stamp, so low-cardinality columns are
/// already claimed by `DictString` (which is a strictly bigger win for
/// few distinct values); affix catches the HIGH-cardinality near-unique
/// columns the dictionary cannot fold.
///
/// Gates (all must hold):
/// - ≥ 3 rows;
/// - every cell a scalar string (no Missing/Nested/numeric);
/// - the shared affix is non-empty (prefix or suffix);
/// - the affix length is ≥ 2 bytes (a 1-byte affix barely pays for the
///   `^` marker + the `__affix:` line);
/// - the exact round-trip holds for every cell;
/// - strict byte saving: the affix line + stripped+ditto cells render
///   smaller than the plain+ditto cells.
fn stamp_affix_columns(specs: &mut [FieldSpec], rows: &[Row]) {
    use super::encodings::{common_affix, decode_affix_cell, encode_affix_cell};
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
        let (prefix, suffix) = common_affix(&values);
        if prefix.len() + suffix.len() < 2 {
            continue;
        }
        // A prefix/suffix containing a newline would break the
        // single-line `__affix:` preamble grammar; skip (CSV-quoting
        // can't carry a literal newline on one line).
        if prefix.contains('\n')
            || prefix.contains('\r')
            || suffix.contains('\n')
            || suffix.contains('\r')
        {
            continue;
        }
        // Strip every cell and PROVE the exact round-trip.
        let middles: Option<Vec<&str>> = values
            .iter()
            .map(|v| encode_affix_cell(v, prefix, suffix))
            .collect();
        let Some(middles) = middles else { continue };
        let round_trip_ok = middles
            .iter()
            .zip(values.iter())
            .all(|(mid, orig)| decode_affix_cell(mid, prefix, suffix) == **orig);
        if !round_trip_ok {
            continue;
        }
        // Byte gate: plain cells vs stripped middles, both WITH ditto and
        // the exact formatter quoting, plus the one-time `__affix:` line.
        let plain_cells: Vec<String> = values.iter().map(|v| csv_render_str(v)).collect();
        let mid_cells: Vec<String> = middles.iter().map(|m| csv_render_str(m)).collect();
        let plain = ditto_rendered_cost(plain_cells.iter().map(|s| s.as_str()));
        let stripped = ditto_rendered_cost(mid_cells.iter().map(|s| s.as_str()));
        let affix_line = "__affix:".len()
            + spec.name.len()
            + 1 // '='
            + csv_render_str(prefix).len()
            + 1 // ',' between prefix and suffix
            + csv_render_str(suffix).len()
            + 1 // newline
            + 1; // the `^` marker on the declaration
        if affix_line + stripped < plain {
            spec.encoding = Some(ColumnEncoding::Affix {
                prefix: prefix.to_string(),
                suffix: suffix.to_string(),
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
    // 24-hex (96-bit) SHA-256 prefix — collision-resistant well past this
    // store's request-window population, short enough to keep the marker
    // compact. Algorithm consolidated in `ccr::persist` (ARCH-5); this domain
    // alias stays so call sites and tests keep their vocabulary.
    crate::ccr::persist::sha256_recovery_key(bytes)
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
        assert!(matches!(compact(&items, &cfg()), Compaction::Untouched));
        let items = vec![json!({"a": 1})];
        assert!(matches!(compact(&items, &cfg()), Compaction::Untouched));
    }

    #[test]
    fn non_object_array_is_untouched() {
        let items = vec![json!(1), json!(2), json!(3)];
        assert!(matches!(compact(&items, &cfg()), Compaction::Untouched));
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
    fn stringified_json_object_stays_string_scalar_not_flattened() {
        // T2: a value that ORIGINATED as a JSON-object string is kept verbatim
        // as a Scalar(String), never parsed + flattened into dotted columns
        // (which dropped the original `payload` field entirely).
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "payload": format!("{{\"a\": {i}}}")}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, rows, .. } => {
                let names = schema.field_names();
                assert!(
                    names.contains(&"payload"),
                    "payload column vanished: {names:?}"
                );
                assert!(
                    !names.iter().any(|n| n.starts_with("payload.")),
                    "object-string was flattened into dotted columns: {names:?}"
                );
                let col = schema
                    .fields
                    .iter()
                    .position(|f| f.name == "payload")
                    .unwrap();
                match &rows[0].0[col] {
                    CellValue::Scalar(Value::String(s)) => assert_eq!(s, "{\"a\": 0}"),
                    other => panic!("expected verbatim Scalar(String), got {other:?}"),
                }
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn stringified_json_array_keeps_exact_bytes() {
        // T2: an array-string keeps its interior whitespace (no parse +
        // compact re-serialization that dropped the spaces).
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "arr": format!("[1, 2, {i}]")}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, rows, .. } => {
                let col = schema.fields.iter().position(|f| f.name == "arr").unwrap();
                match &rows[0].0[col] {
                    CellValue::Scalar(Value::String(s)) => assert_eq!(s, "[1, 2, 0]"),
                    other => panic!("expected verbatim Scalar(String), got {other:?}"),
                }
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn literal_dotted_key_collision_skips_flatten() {
        // T12: a literal top-level `m.k` beside a nested `{"m": {"k": ..}}`
        // must NOT synthesize a colliding `m.k` column. The nested `m` stays
        // an object column; the literal `m.k` stays its own column, so both
        // distinct values survive instead of one silently overwriting the
        // other on decode.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "m.k": format!("lit-{i}"), "m": {"k": 1000 + i}}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                let dotted = names.iter().filter(|n| **n == "m.k").count();
                assert_eq!(dotted, 1, "duplicate m.k columns synthesized: {names:?}");
                assert!(
                    names.contains(&"m"),
                    "nested `m` object column dropped: {names:?}"
                );
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn dotted_inner_with_prefix_sibling_object_skips_flatten() {
        // COR-14: `a` with inner `b.c` synthesizes `a.b.c` while sibling
        // column `a.b` sits on the strict prefix `a.b` — flattening bound
        // 900+i to the wrong owner of `a.b.c` under the documented
        // dotted-key equivalence. Both columns must stay nested cells.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "a.b": {"c": i}, "a": {"b.c": 900 + i}}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(
                    !names.contains(&"a.b.c"),
                    "ambiguous a.b.c column synthesized: {names:?}"
                );
                assert!(names.contains(&"a"), "nested `a` dropped: {names:?}");
                assert!(names.contains(&"a.b"), "literal `a.b` dropped: {names:?}");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn dotted_inner_with_prefix_sibling_scalar_skips_flatten() {
        // COR-14: same prefix interference with a SCALAR sibling `a.b` —
        // the decoded fold nests `a.b` first and keeps `a.b.c` literal,
        // while the original folds `b.c` inside `a`'s subtree.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "a": {"b.c": i}, "a.b": format!("s{i}")}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(
                    !names.contains(&"a.b.c"),
                    "ambiguous a.b.c column synthesized: {names:?}"
                );
                assert!(names.contains(&"a"), "nested `a` dropped: {names:?}");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn prefix_overlapping_inner_keys_skip_flatten() {
        // COR-14: inners {`b`, `b.c`} — whether the `b.c` leaf nests under
        // sibling value `b` depends on that value's own keys per row, so
        // the flatten fails closed and `p` stays a nested cell.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "p": {"b": {"c": i}, "b.c": 900 + i}}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(
                    !names.iter().any(|n| n.starts_with("p.")),
                    "prefix-overlapping inners flattened: {names:?}"
                );
                assert!(names.contains(&"p"), "nested `p` dropped: {names:?}");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn empty_segment_inner_keys_skip_flatten() {
        // COR-14: `""` and `"b."` synthesize `p.` / `p.b.` whose empty
        // segments the canonicalizer keeps LITERAL at top level while the
        // original folds them inside `p` — guaranteed divergence.
        for inner in [json!({"": 1, "q": 2}), json!({"b.": 1, "q": 2})] {
            let items: Vec<Value> = (0..4)
                .map(|i| json!({"id": i, "p": inner.clone()}))
                .collect();
            match compact(&items, &cfg()) {
                Compaction::Table { schema, .. } => {
                    let names = schema.field_names();
                    assert!(
                        !names.iter().any(|n| n.starts_with("p.")),
                        "empty-segment inner flattened: {names:?}"
                    );
                    assert!(names.contains(&"p"), "nested `p` dropped: {names:?}");
                }
                other => panic!("expected Table, got {other:?}"),
            }
        }
    }

    #[test]
    fn empty_parent_name_skips_flatten() {
        // COR-14: parent `""` is dot-free but synthesizes `.k`, whose
        // leading empty segment stays literal under the canonicalization
        // while the original nests `k` under `""`.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "": {"k": 900 + i}}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(
                    !names.contains(&".k"),
                    "empty parent flattened to `.k`: {names:?}"
                );
                assert!(names.contains(&""), "empty-name column dropped: {names:?}");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn benign_dotted_inner_keys_still_flatten() {
        // COR-14 guard must NOT over-decline: metrics-style dotted inner
        // keys with no prefix interference fold identically on both sides
        // of the equivalence, so the flatten (and its compression) stays.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "m": {"cpu.usage": i, "mem.rss": 2 * i}}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(names.contains(&"m.cpu.usage"), "got {names:?}");
                assert!(names.contains(&"m.mem.rss"), "got {names:?}");
                assert!(!names.contains(&"m"), "flatten declined: {names:?}");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn leaf_extension_and_disjoint_siblings_keep_flatten() {
        // COR-14 guard must NOT over-decline: a sibling EXTENDING the
        // synthesized path (`a.b.c.d`) or on a disjoint branch (`a.j`)
        // folds identically on both sides — only strict-prefix siblings
        // block the flatten.
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"id": i, "a": {"b.c": i}, "a.b.c.d": 900 + i, "a.j": 30 + i}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(names.contains(&"a.b.c"), "flatten declined: {names:?}");
                assert!(names.contains(&"a.b.c.d"), "got {names:?}");
                assert!(names.contains(&"a.j"), "got {names:?}");
                assert!(!names.contains(&"a"), "flatten declined: {names:?}");
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn json_column_container_string_declines_lossless() {
        // T1/T2: a `json`-tagged (type-mixed) column holding a container-
        // looking STRING cannot ship losslessly — a quoted container-string
        // is indistinguishable from a real container to the reference
        // decoder, so the table declines (`is_decoder_verifiable` == false).
        let items: Vec<Value> = (0..6)
            .map(|i| {
                if i % 2 == 0 {
                    json!({"id": i, "cfg": "{\"a\": 1}"}) // container STRING
                } else {
                    json!({"id": i, "cfg": i}) // real int -> mixes to `json`
                }
            })
            .collect();
        let c = compact(&items, &cfg());
        match &c {
            Compaction::Table { schema, .. } => {
                let spec = schema.fields.iter().find(|f| f.name == "cfg").unwrap();
                assert_eq!(spec.type_tag, "json", "expected a json-tagged mixed column");
            }
            other => panic!("expected Table, got {other:?}"),
        }
        assert!(
            !c.is_decoder_verifiable(),
            "a json column with a container-string must decline the lossless tier"
        );
    }

    #[test]
    fn json_column_scalar_string_stays_verifiable() {
        // Contrast: scalar-looking strings ("200") in a json column ARE
        // quoted by the formatter and round-trip, so the table stays
        // decoder-verifiable — only container-strings decline.
        let items: Vec<Value> = (0..6)
            .map(|i| {
                if i % 2 == 0 {
                    json!({"id": i, "code": "200"}) // scalar-looking STRING
                } else {
                    json!({"id": i, "code": 500}) // real int
                }
            })
            .collect();
        let c = compact(&items, &cfg());
        match &c {
            Compaction::Table { schema, .. } => {
                let spec = schema.fields.iter().find(|f| f.name == "code").unwrap();
                assert_eq!(spec.type_tag, "json");
            }
            other => panic!("expected Table, got {other:?}"),
        }
        assert!(
            c.is_decoder_verifiable(),
            "a json column with only scalar-looking strings must stay verifiable"
        );
    }

    #[test]
    fn json_column_serde_rejected_container_string_still_declines() {
        // The decline gate keys on cell SHAPE, not a serde re-parse. Python
        // `json.loads` accepts NaN / Infinity and deep nesting that
        // `serde_json` rejects, and it also skips leading JSON whitespace, so a
        // container-SHAPED string that serde would fail must still decline, else
        // it ships quoted and the reference decoder parses it as a container
        // (silent loss). Includes a leading-whitespace variant for the trim.
        let deep = "[".repeat(128) + &"]".repeat(128);
        for bad in [
            "[NaN]",
            "[Infinity]",
            "{\"x\": NaN}",
            " [NaN]",
            "\t[1]",
            deep.as_str(),
        ] {
            let items: Vec<Value> = (0..6)
                .map(|i| {
                    if i % 2 == 0 {
                        json!({ "id": i, "cfg": bad })
                    } else {
                        json!({ "id": i, "cfg": i })
                    }
                })
                .collect();
            let c = compact(&items, &cfg());
            assert!(
                !c.is_decoder_verifiable(),
                "container-shaped string {bad:?} must decline the lossless tier"
            );
        }
    }

    #[test]
    fn grammar_breaking_key_declines_compaction() {
        // COR-15: column names ship RAW in the `[N]{...}` declaration and
        // the `__dict:`/`__affix:`/`__head:` preamble lines — nothing
        // quotes them. A key carrying a grammar char (or the reserved
        // `__` marker prefix) must DECLINE compaction (Untouched, array
        // verbatim), never ship a silently mis-keying table.
        for key in [
            "meta:region",
            "a,b",
            "x{y",
            "x}y",
            "a=b",
            "he\"llo",
            "a\nb",
            "a\rb",
            "__dict",
        ] {
            let items: Vec<Value> = (0..5)
                .map(|i| json!({"id": i, key: format!("val-{i}"), "s": "ok"}))
                .collect();
            match compact(&items, &cfg()) {
                // Payload-free decline (PERF-5): the caller re-uses its
                // own borrow, so "verbatim" is guaranteed by construction.
                Compaction::Untouched => {}
                other => panic!("key {key:?} must decline compaction, got {other:?}"),
            }
        }
    }

    #[test]
    fn grammar_breaking_inner_key_does_not_flatten() {
        // COR-15 flatten guard: a nested-uniform object whose INNER key
        // would flatten into a grammar-breaking dotted column name
        // (`cfg.k:v`) must stay a nested object cell (CSV-quoted JSON)
        // instead of flattening into a corrupting declaration.
        let items: Vec<Value> = (0..5)
            .map(|i| json!({"id": i, "cfg": {"k:v": format!("val-{i}"), "plain": "p"}}))
            .collect();
        match compact(&items, &cfg()) {
            Compaction::Table { schema, .. } => {
                let names = schema.field_names();
                assert!(names.contains(&"cfg"), "cfg must stay nested: {names:?}");
                assert!(
                    names.iter().all(|n| !n.contains(':')),
                    "no grammar-breaking flattened name may ship: {names:?}"
                );
            }
            other => panic!("expected Table, got {other:?}"),
        }
    }

    #[test]
    fn stringified_json_array_stays_verbatim_string() {
        // T2: a value that ORIGINATED as a stringified-JSON array is kept as
        // the EXACT original string, never parsed into a recursive sub-table.
        // The old recurse-into-Nested behaviour dropped the string's interior
        // bytes and hid the original behind an un-decodable Nested cell on the
        // lossless path. Genuine (non-string) arrays still recurse — see
        // `formatter::tests::csv_formatter_nested_cell_inline_json`.
        let items = vec![
            json!({"event": "batch", "payload": r#"[{"x":1},{"x":2},{"x":3}]"#}),
            json!({"event": "batch", "payload": r#"[{"x":4},{"x":5}]"#}),
        ];
        match compact(&items, &cfg()) {
            Compaction::Table { schema, rows, .. } => {
                for row in &rows {
                    assert!(
                        row.0.iter().all(|c| !matches!(c, CellValue::Nested(_))),
                        "stringified array must not recurse into a Nested cell"
                    );
                }
                let col = schema
                    .fields
                    .iter()
                    .position(|f| f.name == "payload")
                    .unwrap();
                match &rows[0].0[col] {
                    CellValue::Scalar(Value::String(s)) => {
                        assert_eq!(s, r#"[{"x":1},{"x":2},{"x":3}]"#)
                    }
                    other => panic!("expected verbatim Scalar(String), got {other:?}"),
                }
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
                assert!(
                    !matches!(x.encoding, Some(ColumnEncoding::ArithInt { .. })),
                    "float column must not stamp ARITH (decimal scale-fold may apply): {:?}",
                    x.encoding
                );
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
        assert_eq!(h1.len(), 24);
    }
}
