//! Compaction IR — recursive tree representation for lossless / row-lossy
//! compaction of JSON arrays.
//!
//! The IR is the boundary between [`TabularCompactor`] (which produces it)
//! and [`Formatter`] implementations (which consume it). Renderer-agnostic.
//!
//! # Recursive structure
//!
//! A `Compaction::Table` has rows of [`CellValue`]s, and a `CellValue` may
//! itself hold a nested `Compaction`. This enables multi-level compression:
//! an array whose rows hold stringified-JSON gets recursively compacted
//! into a sub-table; an opaque blob gets CCR-substituted; a heterogeneous
//! array gets bucketed by discriminator.
//!
//! [`TabularCompactor`]: super::compactor::TabularCompactor
//! [`Formatter`]: super::formatter::Formatter

use serde_json::Value;

/// What kind of opaque payload was substituted by CCR.
///
/// Carried for telemetry and so formatters can render a one-line hint
/// next to the CCR pointer (e.g. `<<ccr:abc123 base64,2.1KB>>`) without
/// re-parsing the original bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OpaqueKind {
    /// Looks base64-encoded — long, restricted alphabet.
    Base64Blob,
    /// Long opaque string the classifier couldn't otherwise place.
    LongString,
    /// HTML/XML chunk (detected by `<` density).
    HtmlChunk,
    /// Detected format the classifier knows about by name (e.g. "diff",
    /// "code"). Routing of these into the right transform is deferred
    /// to a later PR; for now they're treated as `LongString`.
    Other(String),
}

impl OpaqueKind {
    /// The KIND token written into the `<<ccr:HASH,KIND,SIZE>>` marker.
    /// Defined once here so every opaque producer (walker live
    /// substitution + CSV/KV formatters) maps the enum to the same wire
    /// string — no per-site `match` to drift out of sync.
    pub fn wire_str(&self) -> &str {
        match self {
            OpaqueKind::Base64Blob => "base64",
            OpaqueKind::LongString => "string",
            OpaqueKind::HtmlChunk => "html",
            OpaqueKind::Other(s) => s.as_str(),
        }
    }
}

/// Reversible per-column encoding, stamped by the compactor when (and
/// only when) the encoded rendering is strictly smaller AND decodes
/// back to the exact original values.
///
/// Like [`FieldSpec::const_value`], encodings are advisory: the IR rows
/// keep their full original cells, and only formatters that understand
/// an encoding exploit it (today: the CSV-schema formatter). Formatters
/// that ignore this field (JSON, Markdown-KV) render byte-identical
/// output to the pre-encoding engine.
#[derive(Debug, Clone, PartialEq)]
pub enum ColumnEncoding {
    /// The column is an exact arithmetic progression: row `i` holds
    /// `base + step * i` (every cell a scalar i64, constant non-zero
    /// step). The CSV-schema formatter declares `name:int=BASE+STEP`
    /// once and omits the column from rows; the decoder regenerates
    /// the exact values from the row index. Pure integer math — exact
    /// reconstruction by construction.
    ArithInt { base: i64, step: i64 },
    /// Every value is a strict-shape ISO-8601 timestamp
    /// (`YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)`). The CSV-schema formatter
    /// marks the declaration `name:string~`, renders the first value
    /// verbatim and each subsequent cell as `{±delta_seconds}[/tz]`
    /// (tz spelling only when it changes). Stamped only after the
    /// compactor PROVES the exact round-trip at stamp time
    /// (encode → decode → compare against every original string).
    IsoDeltaSeconds,
    /// Low-cardinality string column. `values` holds every distinct
    /// value in first-appearance order, each verbatim exactly once; the
    /// CSV-schema formatter emits a `__dict:name=v0,v1,...` line after
    /// the declaration and renders each cell as its dictionary index.
    /// Stamped only when 2 ≤ |values| < rows, no value contains a
    /// newline (line-grammar integrity), and the dictionary line plus
    /// index cells are strictly smaller than the plain cells.
    DictString { values: Vec<String> },
    /// Float column whose every value renders as a plain decimal with
    /// ≤ `scale` fractional digits. The CSV-schema formatter declares
    /// `name:float%scale` and renders each cell as the integer value ×
    /// 10^scale (`0.053` → `53` at scale 3). Encode/decode are pure
    /// string manipulation (no float arithmetic); the compactor proves
    /// the round-trip at stamp time by re-parsing and re-rendering
    /// every decoded value against the original rendering.
    DecimalScaled { scale: usize },
    /// Cross-row affix fold. Every value in the column shares the byte
    /// `prefix` and `suffix` (either may be empty, never both). The
    /// CSV-schema formatter marks the declaration `name:string^`, emits
    /// a `__affix:name=PREFIX,SUFFIX` preamble line (both CSV-escaped),
    /// and renders each cell as only its unique middle; the decoder
    /// rebuilds `prefix + middle + suffix`. Pure byte concatenation —
    /// exact reconstruction by construction. Stamped only after the
    /// compactor PROVES the round-trip at stamp time AND the affix line
    /// plus stripped cells render strictly smaller than the plain cells.
    Affix { prefix: String, suffix: String },
    /// Head-dictionary fold. Values split at the last `delim` into a
    /// low-cardinality HEAD (declared once, verbatim, first-appearance
    /// order, each including its trailing delimiter) and a unique TAIL.
    /// The CSV-schema formatter marks the declaration `name:string@`,
    /// emits a `__head:name=<DELIM><h0>,<h1>,...` preamble line, and
    /// renders each cell as `<head_index><delim><tail>`; the decoder
    /// rebuilds `head[index] + tail`. Stamped only after a stamp-time
    /// round-trip proof AND a strict byte-saving gate.
    HeadDict { delim: char, heads: Vec<String> },
}

/// One column's metadata in a tabular compaction.
#[derive(Debug, Clone, PartialEq)]
pub struct FieldSpec {
    /// Column name. May be dotted for flattened nested fields,
    /// e.g. `"meta.region"`.
    pub name: String,
    /// Inferred type tag. One of: `"int"`, `"float"`, `"string"`,
    /// `"bool"`, `"null"`, `"json"` (cells render as JSON literals —
    /// last-resort), `"ccr"` (cells are CCR pointers).
    pub type_tag: String,
    /// True if at least one row had this field absent or `null`.
    pub nullable: bool,
    /// `Some(v)` when EVERY row holds the identical scalar `v` in this
    /// column (constant-column fold). The value lives here once instead
    /// of repeating per row; formatters MAY exploit it (the CSV-schema
    /// formatter declares `name:type=value` and omits the column from
    /// rows). Rows in the IR still carry the full cells, so formatters
    /// that ignore this field (JSON, Markdown-KV) render byte-identical
    /// output to the pre-fold engine. Lossless by construction: the
    /// constant is verbatim in the declaration and every row is
    /// reconstructible from header + row cells alone.
    pub const_value: Option<Value>,
    /// `Some(enc)` when the column's values are exactly reproducible
    /// through a reversible encoding (see [`ColumnEncoding`]). Stamped
    /// only after a stamp-time decode-and-compare proves exact
    /// round-trip AND the encoded rendering is strictly smaller.
    /// Mutually exclusive with `const_value`. Rows in the IR still
    /// carry the full cells, so encoding-unaware formatters are
    /// byte-identical to the pre-encoding engine.
    pub encoding: Option<ColumnEncoding>,
}

/// Column set for a homogeneous table.
#[derive(Debug, Clone, PartialEq)]
pub struct Schema {
    pub fields: Vec<FieldSpec>,
}

impl Schema {
    pub fn field_names(&self) -> Vec<&str> {
        self.fields.iter().map(|f| f.name.as_str()).collect()
    }
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
    /// Field is absent in this row. Distinct from `Scalar(Value::Null)`
    /// — `Missing` means the original object had no such key, while
    /// `Scalar(Value::Null)` means the key existed and was null.
    Missing,
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

/// Top-level compaction result. Tree-shaped via `Nested` cells.
///
/// [`Compaction::Table`] is the common case. [`Compaction::Buckets`]
/// only fires for heterogeneous arrays where a discriminator field
/// cleanly partitions rows. [`Compaction::Untouched`] is the
/// fall-through when the compactor declines to operate (e.g. mixed
/// scalars, or fewer than 2 rows).
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
    /// Compactor declined to compact; pass-through original value.
    /// The crusher will fall back to the existing lossy path.
    Untouched(Value),
}

impl Compaction {
    /// Total kept rows in this compaction (sum across buckets if
    /// applicable). 0 for `OpaqueRef` and `Untouched`.
    pub fn kept_row_count(&self) -> usize {
        match self {
            Compaction::Table { rows, .. } => rows.len(),
            Compaction::Buckets { buckets, .. } => buckets.iter().map(|b| b.rows.len()).sum(),
            Compaction::OpaqueRef { .. } | Compaction::Untouched(_) => 0,
        }
    }

    /// Original (pre-drop) row count. 0 for `OpaqueRef` and `Untouched`.
    pub fn original_row_count(&self) -> usize {
        match self {
            Compaction::Table { original_count, .. } => *original_count,
            Compaction::Buckets { original_count, .. } => *original_count,
            Compaction::OpaqueRef { .. } | Compaction::Untouched(_) => 0,
        }
    }

    pub fn was_compacted(&self) -> bool {
        matches!(
            self,
            Compaction::Table { .. } | Compaction::Buckets { .. } | Compaction::OpaqueRef { .. }
        )
    }

    /// True if ANY cell in the tree is an [`CellValue::OpaqueRef`]
    /// substitution (or the tree itself is a top-level
    /// [`Compaction::OpaqueRef`]). Used by callers that only want a
    /// compaction when every original value stays verbatim in the
    /// rendered output (pure rearrangement, no substitution).
    pub fn contains_opaque_ref(&self) -> bool {
        fn row_has_opaque(row: &Row) -> bool {
            row.0.iter().any(|c| match c {
                CellValue::OpaqueRef { .. } => true,
                CellValue::Nested(sub) => sub.contains_opaque_ref(),
                CellValue::Scalar(_) | CellValue::Missing => false,
            })
        }
        match self {
            Compaction::OpaqueRef { .. } => true,
            Compaction::Table { rows, .. } => rows.iter().any(row_has_opaque),
            Compaction::Buckets { buckets, .. } => {
                buckets.iter().any(|b| b.rows.iter().any(row_has_opaque))
            }
            Compaction::Untouched(_) => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn schema_field_names_returns_in_order() {
        let s = Schema {
            fields: vec![
                FieldSpec {
                    name: "id".into(),
                    type_tag: "int".into(),
                    nullable: false,
                    const_value: None,
                    encoding: None,
                },
                FieldSpec {
                    name: "name".into(),
                    type_tag: "string".into(),
                    nullable: false,
                    const_value: None,
                    encoding: None,
                },
            ],
        };
        assert_eq!(s.field_names(), vec!["id", "name"]);
    }

    #[test]
    fn untouched_is_not_compacted() {
        let c = Compaction::Untouched(json!([1, 2, 3]));
        assert!(!c.was_compacted());
        assert_eq!(c.kept_row_count(), 0);
        assert_eq!(c.original_row_count(), 0);
    }

    #[test]
    fn table_row_counts() {
        let c = Compaction::Table {
            schema: Schema { fields: vec![] },
            rows: vec![Row::new(vec![]), Row::new(vec![])],
            original_count: 5,
        };
        assert!(c.was_compacted());
        assert_eq!(c.kept_row_count(), 2);
        assert_eq!(c.original_row_count(), 5);
    }

    #[test]
    fn buckets_aggregate_row_counts() {
        let c = Compaction::Buckets {
            discriminator: "type".into(),
            buckets: vec![
                Bucket {
                    key: json!("user"),
                    schema: Schema { fields: vec![] },
                    rows: vec![Row::new(vec![]), Row::new(vec![])],
                },
                Bucket {
                    key: json!("order"),
                    schema: Schema { fields: vec![] },
                    rows: vec![Row::new(vec![])],
                },
            ],
            original_count: 10,
        };
        assert_eq!(c.kept_row_count(), 3);
        assert_eq!(c.original_row_count(), 10);
    }

    #[test]
    fn cell_missing_distinct_from_scalar_null() {
        let m = CellValue::Missing;
        let n = CellValue::Scalar(Value::Null);
        // Smoke test: just confirm both variants exist and Debug differs.
        assert_ne!(format!("{m:?}"), format!("{n:?}"));
    }
}
