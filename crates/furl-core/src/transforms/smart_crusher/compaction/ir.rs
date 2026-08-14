//! Compaction IR — recursive tree representation for lossless / row-lossy compaction of JSON arrays. The IR is the
//! boundary between [`TabularCompactor`] (which produces it) and [`Formatter`] implementations (which consume it).

use serde_json::Value;

use crate::transforms::smart_crusher::types::DroppedRef;

/// What kind of opaque payload was substituted by CCR.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OpaqueKind {
    /// Looks base64-encoded — long, restricted alphabet.
    Base64Blob,
    /// Long opaque string the classifier couldn't otherwise place.
    LongString,
    /// HTML/XML chunk (detected by `<` density).
    HtmlChunk,
    /// Detected format the classifier knows about by name (e.g. "diff", "code").
    Other(String),
}

impl OpaqueKind {
    /// The KIND token written into the `<<ccr:HASH,KIND,SIZE>>` marker.
    pub fn wire_str(&self) -> &str {
        match self {
            OpaqueKind::Base64Blob => "base64",
            OpaqueKind::LongString => "string",
            OpaqueKind::HtmlChunk => "html",
            OpaqueKind::Other(s) => s.as_str(),
        }
    }
}

/// Reversible per-column encoding, stamped by the compactor when (and only when) the
/// encoded rendering is strictly smaller AND decodes back to the exact original values.
#[derive(Debug, Clone, PartialEq)]
pub enum ColumnEncoding {
    /// The column is an exact arithmetic progression: row `i` holds `base + step * i` (every cell a scalar i64, constant non-zero step).
    ArithInt { base: i64, step: i64 },
    /// Stamp ISO delta encoding only after every strict ISO-8601 value round-trips exactly.
    /// Emit the first timestamp verbatim, then delta seconds and changed timezone spelling.
    IsoDeltaSeconds,
    /// Dictionary-encode low-cardinality strings in first-seen order only when values contain
    /// no newlines and the preamble plus indices is strictly smaller than plain cells.
    DictString { values: Vec<String> },
    /// Float column whose every value renders as a plain decimal with ≤ `scale` fractional digits. Encode/decode are pure string manipulation (no float
    /// arithmetic); the compactor proves the round-trip at stamp time by re-parsing and re-rendering every decoded value against the original rendering.
    DecimalScaled { scale: usize },
    /// Cross-row affix fold. The CSV-schema formatter marks the declaration `name:string^`, emits a `__affix:name=PREFIX,SUFFIX` preamble
    /// line (both CSV-escaped), and renders each cell as only its unique middle; the decoder rebuilds `prefix + middle + suffix`.
    Affix { prefix: String, suffix: String },
    /// Head-dictionary fold. Values split at the last `delim` into a low-cardinality HEAD (declared once, verbatim, first-appearance order, each
    /// including its trailing delimiter) and a unique TAIL. Stamped only after a stamp-time round-trip proof AND a strict byte-saving gate.
    HeadDict { delim: char, heads: Vec<String> },
}

/// One column's metadata in a tabular compaction.
#[derive(Debug, Clone, PartialEq)]
pub struct FieldSpec {
    /// Column name. May be dotted for flattened nested fields,
    /// e.g. `"meta.region"`.
    pub name: String,
    /// Inferred type tag. One of: `"int"`, `"float"`, `"string"`, `"bool"`, `"null"`, `"json"`
    /// (cells render as JSON literals — last-resort), `"ccr"` (cells are CCR pointers).
    pub type_tag: String,
    /// True if at least one row had this field absent or `null`.
    pub nullable: bool,
    /// `Some(v)` when EVERY row holds the identical scalar `v` in this column (constant-column fold).
    pub const_value: Option<Value>,
    /// `Some(enc)` when the column's values are exactly reproducible through a reversible encoding (see [`ColumnEncoding`]).
    /// Stamped only after a stamp-time decode-and-compare proves exact round-trip AND the encoded rendering is strictly smaller.
    pub encoding: Option<ColumnEncoding>,
}

/// Column set for a homogeneous table.
#[derive(Debug, Clone, PartialEq)]
pub struct Schema {
    pub fields: Vec<FieldSpec>,
}

/// One cell in a row. Most cells are scalar; nested/opaque/recursive
/// cells branch the tree.
#[derive(Debug, Clone)]
pub enum CellValue {
    /// Scalar JSON value (number, string, bool, null). Formatter renders
    /// directly per its conventions.
    Scalar(Value),
    /// Recursive sub-compaction. Created for inner arrays, parsed
    /// stringified-JSON, or nested-mixed objects. Formatter recurses.
    Nested(Box<Compaction>),
    /// CCR pointer substituting an opaque/large payload. The original
    /// bytes live in the CCR store keyed by `ccr_hash`.
    OpaqueRef {
        ccr_hash: String,
        byte_size: usize,
        kind: OpaqueKind,
    },
    /// Field is absent in this row.
    Missing,
    /// An array-of-objects cell whose inner compaction DECLINED (`compact` returned
    /// [`Compaction::Untouched`]). Carries the ORIGINAL value so formatters render it verbatim as compact JSON.
    DeclinedJson(Value),
}

/// A row of a tabular compaction. Order and length match the parent
/// table's [`Schema::fields`].
#[derive(Debug, Clone)]
pub struct Row(pub Vec<CellValue>);

impl Row {
    pub fn new(cells: Vec<CellValue>) -> Self {
        Self(cells)
    }
    pub fn len(&self) -> usize {
        self.0.len()
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

/// One bucket of a heterogeneous array, partitioned by a discriminator
/// field's value (e.g. all rows where `type == "user"`).
#[derive(Debug, Clone)]
pub struct Bucket {
    /// The discriminator value that defines this bucket.
    pub key: Value,
    pub schema: Schema,
    pub rows: Vec<Row>,
}

/// Top-level compaction result. Tree-shaped via `Nested` cells. [`Compaction::Table`] is the common case. [`Compaction::Buckets`] only fires for heterogeneous arrays where a
/// discriminator field cleanly partitions rows. [`Compaction::Untouched`] is the fall-through when the compactor declines to operate (e.g. mixed scalars, or fewer than 2 rows).
#[derive(Debug, Clone)]
pub enum Compaction {
    /// Homogeneous tabular form: N rows × C columns.
    Table {
        schema: Schema,
        rows: Vec<Row>,
        /// Row count BEFORE any row-dropping under budget pressure.
        /// `original_count - rows.len()` = rows we had to drop.
        original_count: usize,
    },
    /// Heterogeneous array bucketed by discriminator field.
    Buckets {
        discriminator: String,
        buckets: Vec<Bucket>,
        /// Total rows across all buckets BEFORE row-dropping.
        original_count: usize,
    },
    /// Single CCR pointer — top-level opaque content. Rare; usually
    /// CCR refs live inside table cells, not at the top.
    OpaqueRef {
        ccr_hash: String,
        byte_size: usize,
        kind: OpaqueKind,
    },
    /// Compactor declined to compact; the input passes through unchanged.
    Untouched,
}

impl Compaction {
    pub fn was_compacted(&self) -> bool {
        matches!(
            self,
            Compaction::Table { .. } | Compaction::Buckets { .. } | Compaction::OpaqueRef { .. }
        )
    }

    /// Return true only for shapes the reference decoder can prove lossless; otherwise use recoverable-lossy or untouched output.
    pub fn is_decoder_verifiable(&self) -> bool {
        fn row_has_nested(row: &Row) -> bool {
            // `DeclinedJson` counts as nested here: like `Nested`, its CSV-quoted JSON render decodes to a plain string, so a table carrying
            // one must stay OUT of the lossless tier (COR-13 fail-closed — identical to the pre-PERF-5 `Nested(Untouched)` verdict).
            row.0
                .iter()
                .any(|c| matches!(c, CellValue::Nested(_) | CellValue::DeclinedJson(_)))
        }
        // Reject lossless tables when a `json` column contains a string beginning with `{` or `[`: the decoder
        // would parse it as a container and change its type. Route these shapes to recoverable output instead.
        fn table_has_unquotable_json_string(schema: &Schema, rows: &[Row]) -> bool {
            for (col, field) in schema.fields.iter().enumerate() {
                if field.type_tag != "json" {
                    continue;
                }
                for row in rows {
                    if let Some(CellValue::Scalar(Value::String(s))) = row.0.get(col) {
                        let trimmed = s.trim_start_matches([' ', '\t', '\n', '\r']);
                        if trimmed.starts_with('{') || trimmed.starts_with('[') {
                            return true;
                        }
                    }
                }
            }
            false
        }
        match self {
            Compaction::Table { schema, rows, .. } => {
                !rows.iter().any(row_has_nested) && !table_has_unquotable_json_string(schema, rows)
            }
            Compaction::Buckets { .. } | Compaction::OpaqueRef { .. } | Compaction::Untouched => {
                false
            }
        }
    }

    /// Append one typed [`DroppedRef::Opaque`] to `sink` for every opaque substitution in this tree in render order
    /// (row-major, cell order; byte-identical to the KIND field of the rendered `<<ccr:HASH,KIND,SIZE>>` marker.
    pub fn collect_opaque_refs(&self, sink: &mut Vec<DroppedRef>) {
        fn collect_cell(cell: &CellValue, sink: &mut Vec<DroppedRef>) {
            match cell {
                CellValue::OpaqueRef {
                    ccr_hash,
                    byte_size,
                    kind,
                } => sink.push(DroppedRef::Opaque {
                    hash: ccr_hash.clone(),
                    kind: kind.wire_str().to_string(),
                    byte_size: *byte_size,
                }),
                CellValue::Nested(sub) => sub.collect_opaque_refs(sink),
                CellValue::Scalar(_) | CellValue::Missing | CellValue::DeclinedJson(_) => {}
            }
        }
        match self {
            Compaction::OpaqueRef {
                ccr_hash,
                byte_size,
                kind,
            } => sink.push(DroppedRef::Opaque {
                hash: ccr_hash.clone(),
                kind: kind.wire_str().to_string(),
                byte_size: *byte_size,
            }),
            Compaction::Table { rows, .. } => {
                for row in rows {
                    for cell in &row.0 {
                        collect_cell(cell, sink);
                    }
                }
            }
            Compaction::Buckets { buckets, .. } => {
                for bucket in buckets {
                    for row in &bucket.rows {
                        for cell in &row.0 {
                            collect_cell(cell, sink);
                        }
                    }
                }
            }
            Compaction::Untouched => {}
        }
    }

    /// True if ANY cell in the tree is an [`CellValue::OpaqueRef`] substitution (or the tree itself is a top-level [`Compaction::OpaqueRef`]). Used
    /// by callers that only want a compaction when every original value stays verbatim in the rendered output (pure rearrangement, no substitution).
    pub fn contains_opaque_ref(&self) -> bool {
        fn row_has_opaque(row: &Row) -> bool {
            row.0.iter().any(|c| match c {
                CellValue::OpaqueRef { .. } => true,
                CellValue::Nested(sub) => sub.contains_opaque_ref(),
                CellValue::Scalar(_) | CellValue::Missing | CellValue::DeclinedJson(_) => false,
            })
        }
        match self {
            Compaction::OpaqueRef { .. } => true,
            Compaction::Table { rows, .. } => rows.iter().any(row_has_opaque),
            Compaction::Buckets { buckets, .. } => {
                buckets.iter().any(|b| b.rows.iter().any(row_has_opaque))
            }
            Compaction::Untouched => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn untouched_is_not_compacted() {
        let c = Compaction::Untouched;
        assert!(!c.was_compacted());
    }

    #[test]
    fn cell_missing_distinct_from_scalar_null() {
        let m = CellValue::Missing;
        let n = CellValue::Scalar(Value::Null);
        // Smoke test: just confirm both variants exist and Debug differs.
        assert_ne!(format!("{m:?}"), format!("{n:?}"));
    }

    // ---------- is_decoder_verifiable (COR-13) ----------

    #[test]
    fn flat_scalar_table_is_decoder_verifiable() {
        let c = Compaction::Table {
            schema: Schema { fields: vec![] },
            rows: vec![
                Row::new(vec![CellValue::Scalar(json!(1)), CellValue::Missing]),
                Row::new(vec![
                    CellValue::Scalar(json!("a")),
                    CellValue::Scalar(json!(null)),
                ]),
            ],
            original_count: 2,
        };
        assert!(c.is_decoder_verifiable());
    }

    #[test]
    fn table_with_nested_cell_is_not_decoder_verifiable() {
        let sub = Compaction::Table {
            schema: Schema { fields: vec![] },
            rows: vec![Row::new(vec![])],
            original_count: 1,
        };
        let c = Compaction::Table {
            schema: Schema { fields: vec![] },
            rows: vec![Row::new(vec![
                CellValue::Scalar(json!(1)),
                CellValue::Nested(Box::new(sub)),
            ])],
            original_count: 1,
        };
        // A Nested cell renders as CSV-quoted IR JSON, which the
        // reference decoder decodes to a plain string — unverifiable.
        assert!(!c.is_decoder_verifiable());
    }

    // ---------- collect_opaque_refs (§4.2 R2) ----------

    #[test]
    fn collect_opaque_refs_walks_tables_nested_and_buckets_in_render_order() {
        let opaque = |h: &str, k: OpaqueKind, n: usize| CellValue::OpaqueRef {
            ccr_hash: h.into(),
            byte_size: n,
            kind: k,
        };
        let sub = Compaction::Table {
            schema: Schema { fields: vec![] },
            rows: vec![Row::new(vec![opaque(
                "222222222222",
                OpaqueKind::HtmlChunk,
                512,
            )])],
            original_count: 1,
        };
        let c = Compaction::Buckets {
            discriminator: "type".into(),
            buckets: vec![
                Bucket {
                    key: json!("a"),
                    schema: Schema { fields: vec![] },
                    rows: vec![Row::new(vec![
                        CellValue::Scalar(json!(1)),
                        opaque("111111111111", OpaqueKind::Base64Blob, 2150),
                    ])],
                },
                Bucket {
                    key: json!("b"),
                    schema: Schema { fields: vec![] },
                    rows: vec![Row::new(vec![
                        CellValue::Nested(Box::new(sub)),
                        CellValue::Missing,
                    ])],
                },
            ],
            original_count: 2,
        };
        let mut sink = Vec::new();
        c.collect_opaque_refs(&mut sink);
        assert_eq!(
            sink,
            vec![
                DroppedRef::Opaque {
                    hash: "111111111111".into(),
                    kind: "base64".into(),
                    byte_size: 2150,
                },
                DroppedRef::Opaque {
                    hash: "222222222222".into(),
                    kind: "html".into(),
                    byte_size: 512,
                },
            ]
        );
        // Consistency with the boolean twin: refs exist ⟺ contains says so.
        assert!(c.contains_opaque_ref());
    }

    #[test]
    fn collect_opaque_refs_top_level_and_empty_shapes() {
        let top = Compaction::OpaqueRef {
            ccr_hash: "abc123def456".into(),
            byte_size: 10,
            kind: OpaqueKind::Other("diff".into()),
        };
        let mut sink = Vec::new();
        top.collect_opaque_refs(&mut sink);
        assert_eq!(
            sink,
            vec![DroppedRef::Opaque {
                hash: "abc123def456".into(),
                kind: "diff".into(),
                byte_size: 10,
            }]
        );

        let mut empty_sink = Vec::new();
        Compaction::Untouched.collect_opaque_refs(&mut empty_sink);
        let plain = Compaction::Table {
            schema: Schema { fields: vec![] },
            rows: vec![Row::new(vec![CellValue::Scalar(json!("x"))])],
            original_count: 1,
        };
        plain.collect_opaque_refs(&mut empty_sink);
        assert!(empty_sink.is_empty(), "no opaque cells → no refs");
    }

    #[test]
    fn buckets_opaque_and_untouched_are_not_decoder_verifiable() {
        let buckets = Compaction::Buckets {
            discriminator: "type".into(),
            buckets: vec![],
            original_count: 0,
        };
        let opaque = Compaction::OpaqueRef {
            ccr_hash: "abc123".into(),
            byte_size: 10,
            kind: OpaqueKind::LongString,
        };
        let untouched = Compaction::Untouched;
        assert!(!buckets.is_decoder_verifiable());
        assert!(!opaque.is_decoder_verifiable());
        assert!(!untouched.is_decoder_verifiable());
    }
}
