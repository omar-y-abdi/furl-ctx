//! Formatter trait + the built-in implementations.
//!
//! [`Formatter`] walks a [`Compaction`] tree and renders bytes. It's the
//! pluggable seam where users (or Enterprise plugins) choose how the
//! compacted output looks.
//!
//! # Built-ins
//!
//! - [`JsonFormatter`] — single-line / pretty JSON. Easy to parse,
//!   wider model familiarity, larger byte size. Default for the
//!   debugging path.
//! - [`CsvSchemaFormatter`] — `[N]{cols}` row-count-and-shape
//!   declaration + typed column header + CSV-escaped rows. Steals
//!   TOON's most useful idea (the `[N]{cols}` declaration) without
//!   adopting TOON's bespoke escaping rules — every model has seen
//!   millions of CSV examples in training.
//! - [`MarkdownKvFormatter`] — the same `[N]{cols}` declaration +
//!   one Markdown list item per row with `key: value` lines.
//!   Token-heavier than CSV (field names repeat per row) but
//!   format-comprehension benchmarks favor KV for read-back accuracy.
//!
//! # Nested cells
//!
//! The formatters handle [`CellValue::Nested`] by recursively
//! formatting the sub-compaction and embedding the result. The CSV
//! formatter wraps nested output in CSV-quoted form; the JSON
//! formatter embeds it as a structured JSON object.
//!
//! # Opaque cells
//!
//! [`CellValue::OpaqueRef`] renders as a structured marker the model
//! can recognize: `<<ccr:HASH,KIND,SIZE>>`. This format is fixed across
//! all built-in formatters so downstream consumers can pattern-match
//! markers regardless of which formatter produced them.

use serde_json::{json, Value};

use super::encodings;
use super::ir::{CellValue, ColumnEncoding, Compaction, OpaqueKind, Row, Schema};
use crate::ccr::marker_for_opaque;

// ─────────────────── CSV-schema preamble grammar markers ───────────────────
//
// Line prefixes for the three preamble lines of the CSV-schema rendering.
// Each preamble line declares a column encoding once (dictionary values /
// shared affix / head dictionary) so the rows below stay terse. A plain
// data cell that happens to START with one of these prefixes is CSV-quoted
// by `csv_render_str`, keeping the preamble lines unambiguous.
//
// CONTRACT: these byte strings are the wire format read back by the Python
// reference decoder `furl_ctx/transforms/csv_schema_decoder.py` (the
// `_DICT_PREFIX` / `_AFFIX_PREFIX` / `_HEAD_PREFIX` constants there must be
// byte-for-byte identical). The round-trip is guarded by the 200-case fuzz
// test `tests/test_csv_schema_decoder_roundtrip_fuzz.py`, which drives the
// real formatter → real decoder, so any drift between the two sides fails.
const DICT_PREFIX: &str = "__dict:";
const AFFIX_PREFIX: &str = "__affix:";
const HEAD_PREFIX: &str = "__head:";

// Exact-match reserved cell sentinels (NOT prefixes, unlike the markers
// above — matched like the ditto `=`). `NULL_SENTINEL` renders a JSON
// `null`; `MISSING_SENTINEL` renders an absent key. This keeps `null`,
// a missing key, and the empty string `""` distinct on the lossless CSV
// path (all three previously collapsed to an empty cell). A literal
// STRING cell equal to either sentinel is CSV-quoted by `csv_render_str`,
// so the bare sentinels stay unambiguous. The Python decoder
// `csv_schema_decoder.py` (`_NULL_SENTINEL` / `_MISSING_SENTINEL`) must
// match these byte-for-byte + apply the same escape rule.
const NULL_SENTINEL: &str = "__null__";
const MISSING_SENTINEL: &str = "__missing__";

/// Format a `Compaction` tree into bytes.
pub trait Formatter: Send + Sync {
    /// Stable name for telemetry (e.g. `"json"`, `"csv-schema"`).
    fn name(&self) -> &str;

    /// Render the compaction. Implementations should be deterministic
    /// for stable test parity.
    fn format(&self, c: &Compaction) -> String;

    /// Cheap byte-size estimate. Default impl renders and measures.
    /// Override for cases where rendering is expensive.
    fn estimate_bytes(&self, c: &Compaction) -> usize {
        self.format(c).len()
    }
}

// ─────────────────────────── JSON formatter ───────────────────────────

/// Renders a `Compaction` as structured JSON. Single-line by default
/// for token-tight output; set `pretty = true` for human inspection.
#[derive(Debug, Clone, Default)]
pub struct JsonFormatter {
    pub pretty: bool,
}

impl JsonFormatter {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn pretty(mut self) -> Self {
        self.pretty = true;
        self
    }
}

impl Formatter for JsonFormatter {
    fn name(&self) -> &str {
        "json"
    }

    fn format(&self, c: &Compaction) -> String {
        let v = compaction_to_json(c);
        if self.pretty {
            serde_json::to_string_pretty(&v).unwrap_or_default()
        } else {
            serde_json::to_string(&v).unwrap_or_default()
        }
    }
}

fn compaction_to_json(c: &Compaction) -> Value {
    match c {
        Compaction::Table {
            schema,
            rows,
            original_count,
        } => json!({
            "_compaction": "table",
            "_schema": schema_to_json(schema),
            "_kept": rows.len(),
            "_total": original_count,
            "_rows": rows.iter().map(row_to_json).collect::<Vec<_>>(),
        }),
        Compaction::Buckets {
            discriminator,
            buckets,
            original_count,
        } => json!({
            "_compaction": "buckets",
            "_discriminator": discriminator,
            "_total": original_count,
            "_buckets": buckets
                .iter()
                .map(|b| json!({
                    "_key": b.key.clone(),
                    "_schema": schema_to_json(&b.schema),
                    "_rows": b.rows.iter().map(row_to_json).collect::<Vec<_>>(),
                }))
                .collect::<Vec<_>>(),
        }),
        Compaction::OpaqueRef {
            ccr_hash,
            byte_size,
            kind,
        } => json!({
            "_compaction": "ccr",
            "_hash": ccr_hash,
            "_size": byte_size,
            "_kind": opaque_kind_str(kind),
        }),
        // Payload-free (PERF-5). A declined compaction never ships: every
        // production caller gates on `was_compacted()` before formatting,
        // so this arm renders only in direct-formatter (debug/test) use.
        Compaction::Untouched => Value::Null,
    }
}

fn schema_to_json(s: &Schema) -> Value {
    Value::Array(
        s.fields
            .iter()
            .map(|f| {
                let mut obj = serde_json::Map::new();
                obj.insert("name".into(), Value::String(f.name.clone()));
                obj.insert("type".into(), Value::String(f.type_tag.clone()));
                if f.nullable {
                    obj.insert("nullable".into(), Value::Bool(true));
                }
                Value::Object(obj)
            })
            .collect(),
    )
}

fn row_to_json(row: &Row) -> Value {
    Value::Array(row.0.iter().map(cell_to_json).collect())
}

fn cell_to_json(c: &CellValue) -> Value {
    match c {
        CellValue::Scalar(v) => v.clone(),
        CellValue::Missing => Value::Null,
        CellValue::Nested(sub) => compaction_to_json(sub),
        // Byte-identical to the pre-PERF-5 `Nested(Untouched(v))` render.
        CellValue::DeclinedJson(v) => v.clone(),
        CellValue::OpaqueRef {
            ccr_hash,
            byte_size,
            kind,
        } => json!({
            "_ccr": ccr_hash,
            "_size": byte_size,
            "_kind": opaque_kind_str(kind),
        }),
    }
}

fn opaque_kind_str(k: &OpaqueKind) -> String {
    match k {
        OpaqueKind::Base64Blob => "base64".into(),
        OpaqueKind::LongString => "string".into(),
        OpaqueKind::HtmlChunk => "html".into(),
        OpaqueKind::Other(s) => s.clone(),
    }
}

// ─────────────────────────── CSV+schema formatter ───────────────────────────

/// Renders a `Compaction` as `[N]{col1:type1,col2:type2}` declaration +
/// CSV-escaped rows. Nested cells render as JSON inline; opaque cells
/// render as `<<ccr:...>>` markers.
#[derive(Debug, Clone, Default)]
pub struct CsvSchemaFormatter {
    /// If true, emit a `__total:N` line when rows were dropped under
    /// budget. Costs a few bytes; useful for downstream telemetry.
    pub include_drop_summary: bool,
}

impl CsvSchemaFormatter {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn with_drop_summary(mut self) -> Self {
        self.include_drop_summary = true;
        self
    }
}

impl Formatter for CsvSchemaFormatter {
    fn name(&self) -> &str {
        "csv-schema"
    }

    fn format(&self, c: &Compaction) -> String {
        let mut out = String::new();
        write_compaction(&mut out, c, self);
        out
    }
}

fn write_compaction(out: &mut String, c: &Compaction, fmt: &CsvSchemaFormatter) {
    match c {
        Compaction::Table {
            schema,
            rows,
            original_count,
        } => {
            write_table(out, schema, rows, *original_count, fmt);
        }
        Compaction::Buckets {
            discriminator,
            buckets,
            original_count,
        } => {
            out.push_str("__buckets:");
            out.push_str(discriminator);
            if fmt.include_drop_summary {
                let kept: usize = buckets.iter().map(|b| b.rows.len()).sum();
                if kept < *original_count {
                    out.push_str(&format!(" __dropped:{}", original_count - kept));
                }
            }
            out.push('\n');
            for b in buckets {
                out.push_str(&format!("__key:{}\n", json_scalar_to_csv(&b.key)));
                write_table(out, &b.schema, &b.rows, b.rows.len(), fmt);
            }
        }
        Compaction::OpaqueRef {
            ccr_hash,
            byte_size,
            kind,
        } => {
            out.push_str(&format_ccr_marker(ccr_hash, *byte_size, kind));
        }
        // Payload-free (PERF-5); see `compaction_to_json`. Nothing to
        // render — production callers gate on `was_compacted()`.
        Compaction::Untouched => {}
    }
}

fn write_table(
    out: &mut String,
    schema: &Schema,
    rows: &[Row],
    original_count: usize,
    fmt: &CsvSchemaFormatter,
) {
    // Declaration line: [N]{col:type,col:type,...}
    //
    // Constant-column fold: a column with `const_value = Some(v)`
    // declares `name:type=v` here and is OMITTED from every row below —
    // the value appears verbatim exactly once. Lossless: rows are
    // reconstructible from the declaration + remaining cells.
    //
    // Arithmetic fold: a column stamped `ArithInt { base, step }` is an
    // exact progression `base + step*i`; it declares `name:int=BASE+STEP`
    // here and is OMITTED from every row below. Unambiguous against a
    // constant declaration: an int constant renders as a bare integer,
    // never two integers joined by `+`. Lossless: the decoder
    // regenerates value_i = base + step*i from the row index.
    out.push('[');
    out.push_str(&rows.len().to_string());
    out.push_str("]{");
    let col_decl: Vec<String> = schema
        .fields
        .iter()
        .map(|f| {
            let base = if f.nullable {
                format!("{}:{}?", f.name, f.type_tag)
            } else {
                format!("{}:{}", f.name, f.type_tag)
            };
            match &f.encoding {
                Some(ColumnEncoding::ArithInt { base: b, step }) => {
                    return format!("{base}={b}+{step}");
                }
                // ISO-delta marker: `name:string~`. The `~` suffix tells
                // the decoder this column's first materialized cell is a
                // verbatim ISO timestamp and later cells are
                // `{±delta_seconds}[/tz]` carry-forwards.
                Some(ColumnEncoding::IsoDeltaSeconds) => return format!("{base}~"),
                // Decimal scale marker: `name:float%k` — cells are the
                // integer value × 10^k.
                Some(ColumnEncoding::DecimalScaled { scale }) => {
                    return format!("{base}%{scale}");
                }
                // Affix-fold marker: `name:string^`. The shared prefix
                // and suffix live on the `__affix:name=...` preamble line;
                // rows below carry only the unique middle.
                Some(ColumnEncoding::Affix { .. }) => return format!("{base}^"),
                // Head-dict marker: `name:string@`. The distinct heads
                // live on the `__head:name=...` preamble line; rows carry
                // `<head_index><delim><tail>`.
                Some(ColumnEncoding::HeadDict { .. }) => return format!("{base}@"),
                // Dictionary columns keep a plain declaration — the
                // `__dict:name=...` preamble line is their marker.
                Some(ColumnEncoding::DictString { .. }) | None => {}
            }
            match &f.const_value {
                Some(v) => format!("{base}={}", const_decl_value(v)),
                None => base,
            }
        })
        .collect();
    out.push_str(&col_decl.join(","));
    out.push('}');
    if fmt.include_drop_summary && rows.len() < original_count {
        out.push_str(&format!(" __dropped:{}", original_count - rows.len()));
    }
    out.push('\n');

    // Dictionary lines: each `DictString` column declares its distinct
    // values once (first-appearance order, verbatim, CSV-escaped) on a
    // `__dict:name=v0,v1,...` line; the rows below carry indexes. A
    // plain data cell that happens to START with `__dict:` is CSV-quoted
    // by `csv_render_str`, so these preamble lines stay unambiguous.
    for f in &schema.fields {
        if let Some(ColumnEncoding::DictString { values }) = &f.encoding {
            out.push_str(DICT_PREFIX);
            out.push_str(&f.name);
            out.push('=');
            let segs: Vec<String> = values.iter().map(|v| csv_render_str(v)).collect();
            out.push_str(&segs.join(","));
            out.push('\n');
        }
    }

    // Affix preamble: each `Affix` column declares its shared prefix and
    // suffix once on a `__affix:name=PREFIX,SUFFIX` line (both
    // CSV-escaped so commas/quotes/newlines in the affix stay
    // unambiguous against the comma separator and the row grammar). A
    // plain data cell that happens to START with `__affix:` is CSV-quoted
    // by `csv_render_str`, so these preamble lines stay unambiguous.
    for f in &schema.fields {
        if let Some(ColumnEncoding::Affix { prefix, suffix }) = &f.encoding {
            out.push_str(AFFIX_PREFIX);
            out.push_str(&f.name);
            out.push('=');
            out.push_str(&csv_render_str(prefix));
            out.push(',');
            out.push_str(&csv_render_str(suffix));
            out.push('\n');
        }
    }

    // Head-dict preamble: `__head:name=<DELIM><h0>,<h1>,...`. The first
    // char after `=` is the delimiter; the remaining comma-separated
    // (CSV-escaped) segments are the distinct heads in first-appearance
    // order, each already carrying its trailing delimiter. A plain data
    // cell starting with `__head:` is CSV-quoted by `csv_render_str`.
    for f in &schema.fields {
        if let Some(ColumnEncoding::HeadDict { delim, heads }) = &f.encoding {
            out.push_str(HEAD_PREFIX);
            out.push_str(&f.name);
            out.push('=');
            out.push(*delim);
            let segs: Vec<String> = heads.iter().map(|h| csv_render_str(h)).collect();
            out.push_str(&segs.join(","));
            out.push('\n');
        }
    }

    // Rows. Constant and arithmetic columns are folded into the
    // declaration above. ISO-delta columns render through a streaming
    // per-column encoder (first value verbatim, then second deltas) —
    // the SAME encoder the compactor used to prove the round-trip at
    // stamp time. Dictionary columns render their cell's index.
    //
    // Ditto marks: a cell whose rendering is identical to the SAME
    // column's cell in the previous row renders as a bare `=`
    // (carry-forward). Lossless: the materialized value sits verbatim
    // in the first row of its run; a literal string cell `"="` is
    // CSV-quoted by `csv_render_str` so the bare marker is unambiguous.
    // Cells rendering to 0–1 chars never ditto (no byte saving).
    // Ditto applies AFTER encoding, so repeated identical deltas /
    // indexes compress too; the decoder resolves ditto at the
    // rendered-cell level before decoding.
    let visible_specs: Vec<&super::ir::FieldSpec> =
        schema.fields.iter().filter(|f| row_visible(f)).collect();
    let mut iso_states: Vec<Option<encodings::IsoDeltaState>> = visible_specs
        .iter()
        .map(|f| match f.encoding {
            Some(ColumnEncoding::IsoDeltaSeconds) => Some(encodings::IsoDeltaState::new()),
            _ => None,
        })
        .collect();
    let dict_maps: Vec<Option<std::collections::HashMap<&str, usize>>> = visible_specs
        .iter()
        .map(|f| match &f.encoding {
            Some(ColumnEncoding::DictString { values }) => Some(
                values
                    .iter()
                    .enumerate()
                    .map(|(i, v)| (v.as_str(), i))
                    .collect(),
            ),
            _ => None,
        })
        .collect();
    let mut prev: Vec<Option<String>> = Vec::new();
    for row in rows {
        let mut rendered: Vec<String> = Vec::with_capacity(visible_specs.len());
        let mut slot = 0usize;
        for (c, f) in row.0.iter().zip(schema.fields.iter()) {
            if !row_visible(f) {
                continue;
            }
            let cell = match (&f.encoding, c) {
                (Some(ColumnEncoding::IsoDeltaSeconds), CellValue::Scalar(Value::String(s))) => {
                    match iso_states[slot].as_mut() {
                        Some(state) => state.next_cell(s),
                        None => format_cell(c),
                    }
                }
                (Some(ColumnEncoding::DictString { .. }), CellValue::Scalar(Value::String(s))) => {
                    match dict_maps[slot].as_ref().and_then(|m| m.get(s.as_str())) {
                        Some(idx) => idx.to_string(),
                        // Unreachable for stamped columns (the dict is
                        // built from these exact rows); degrade verbatim.
                        None => format_cell(c),
                    }
                }
                (
                    Some(ColumnEncoding::DecimalScaled { scale }),
                    CellValue::Scalar(Value::Number(n)),
                ) => encodings::encode_decimal_cell(&n.to_string(), *scale)
                    .unwrap_or_else(|| format_cell(c)),
                (
                    Some(ColumnEncoding::Affix { prefix, suffix }),
                    CellValue::Scalar(Value::String(s)),
                ) => match encodings::encode_affix_cell(s, prefix, suffix) {
                    Some(mid) => csv_render_str(mid),
                    // Unreachable for stamped columns (the affix came
                    // from these exact cells); degrade verbatim.
                    None => format_cell(c),
                },
                (
                    Some(ColumnEncoding::HeadDict { delim, heads }),
                    CellValue::Scalar(Value::String(s)),
                ) => match head_cell_for(s, *delim, heads) {
                    Some(cell) => csv_render_str(&cell),
                    // Unreachable for stamped columns; degrade verbatim.
                    None => format_cell(c),
                },
                _ => format_cell(c),
            };
            rendered.push(cell);
            slot += 1;
        }
        if prev.len() != rendered.len() {
            prev = vec![None; rendered.len()];
        }
        let cells: Vec<&str> = rendered
            .iter()
            .zip(prev.iter())
            .map(|(cell, last)| {
                if cell.len() > 1 && last.as_deref() == Some(cell.as_str()) {
                    "="
                } else {
                    cell.as_str()
                }
            })
            .collect();
        out.push_str(&cells.join(","));
        out.push('\n');
        for (slot, cell) in prev.iter_mut().zip(rendered.iter()) {
            *slot = Some(cell.clone());
        }
    }
}

/// Is this column rendered per-row (true) or folded entirely into the
/// declaration line (false — constant fold / arithmetic fold)?
fn row_visible(f: &super::ir::FieldSpec) -> bool {
    f.const_value.is_none() && !matches!(f.encoding, Some(ColumnEncoding::ArithInt { .. }))
}

/// Render a folded constant for the `name:type=value` declaration.
///
/// Same scalar rendering as a row cell, with extra CSV-quoting for
/// strings containing `{` `}` `=` so the declaration's `{...}` grammar
/// and the `=` separator stay unambiguous for read-back.
fn const_decl_value(v: &Value) -> String {
    match v {
        Value::String(s) => {
            if needs_csv_quote(s) || s.contains('{') || s.contains('}') || s.contains('=') {
                csv_quote(s)
            } else {
                s.clone()
            }
        }
        _ => json_scalar_to_csv(v),
    }
}

/// Build a head-dict cell `<head_index><delim><tail>` for `value`,
/// looking the head up in `heads`. `None` when the value lacks the
/// delimiter or its head is not in the dictionary (only possible when
/// rendering a row the stamping never saw) — caller degrades verbatim.
fn head_cell_for(value: &str, delim: char, heads: &[String]) -> Option<String> {
    let (head, tail) = encodings::split_head(value, delim)?;
    let idx = heads.iter().position(|h| h == head)?;
    Some(encodings::encode_head_cell(idx, delim, tail))
}

/// Render a plain string cell with the CSV-schema grammar guards: a
/// literal `=` is quoted (ditto marker), the reserved sentinels and the
/// `__dict:`/`__affix:`/`__head:` preamble prefixes are quoted, and
/// CSV-special chars quote as usual. Shared with the compactor's
/// byte-gate simulation so stamping decisions measure EXACTLY what the
/// formatter ships.
pub(super) fn csv_render_str(s: &str) -> String {
    if s == "="
        || s == NULL_SENTINEL
        || s == MISSING_SENTINEL
        || s.starts_with(DICT_PREFIX)
        || s.starts_with(AFFIX_PREFIX)
        || s.starts_with(HEAD_PREFIX)
        || needs_csv_quote(s)
    {
        csv_quote(s)
    } else {
        s.to_string()
    }
}

/// True when a column name cannot be emitted RAW into the `[N]{...}`
/// declaration (or referenced from a `__dict:`/`__affix:`/`__head:`
/// preamble line) without corrupting the wire grammar. Column names are
/// NEVER quoted on this pre-existing wire format — only cells are — so
/// the compactor DECLINES compaction for such keys (COR-15, fail-closed
/// like every stamp gate) instead of shipping a silently mis-keying
/// table:
/// - `:` mis-splits the `name:type` header (the reference decoder
///   splits on the FIRST colon) and desynchronizes the preamble lines
///   (values lose their affix, arith folds shift by a row);
/// - `=` mis-splits the `name=payload` preamble lines and the
///   `name:type=CONST` declaration;
/// - `,` `{` `}` and CR/LF break the declaration/preamble/row line
///   structure;
/// - `"` flips the decoder's CSV quote state mid-header;
/// - a `__` prefix collides with the reserved marker namespace
///   (`__dict:` / `__affix:` / `__head:` / `__buckets:` / `__key:` /
///   `__dropped:` / `__null__` / `__missing__`).
///
/// Shared with the compactor's table-compaction decision so the gate
/// declines EXACTLY the names this formatter cannot ship safely.
pub(super) fn column_name_breaks_grammar(name: &str) -> bool {
    name.starts_with("__") || name.contains([':', ',', '{', '}', '=', '"', '\n', '\r'])
}

fn format_cell(c: &CellValue) -> String {
    match c {
        CellValue::Missing => MISSING_SENTINEL.to_string(),
        // Grammar guards (ditto `=`, `__dict:` prefix) + CSV quoting.
        CellValue::Scalar(Value::String(s)) => csv_render_str(s),
        CellValue::Scalar(v) => json_scalar_to_csv(v),
        CellValue::Nested(sub) => {
            // Render nested as compact JSON; CSV-quote because it
            // contains commas and structural chars.
            let nested_fmt = JsonFormatter::new();
            csv_quote(&nested_fmt.format(sub))
        }
        // Declined sub-array: verbatim compact JSON, CSV-quoted —
        // byte-identical to the pre-PERF-5 `Nested(Untouched(v))` render
        // (`JsonFormatter::format(Untouched(v))` was `to_string(v)`).
        CellValue::DeclinedJson(v) => csv_quote(&serde_json::to_string(v).unwrap_or_default()),
        CellValue::OpaqueRef {
            ccr_hash,
            byte_size,
            kind,
        } => format_ccr_marker(ccr_hash, *byte_size, kind),
    }
}

fn format_ccr_marker(hash: &str, byte_size: usize, kind: &OpaqueKind) -> String {
    marker_for_opaque(hash, kind.wire_str(), byte_size)
}

fn json_scalar_to_csv(v: &Value) -> String {
    match v {
        Value::Null => NULL_SENTINEL.to_string(),
        Value::Bool(b) => if *b { "true" } else { "false" }.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            if needs_csv_quote(s) {
                csv_quote(s)
            } else {
                s.clone()
            }
        }
        // Object/array fall back to JSON-quoted (rare — usually
        // already promoted to Nested by the compactor).
        _ => csv_quote(&serde_json::to_string(v).unwrap_or_default()),
    }
}

fn needs_csv_quote(s: &str) -> bool {
    s.contains(',') || s.contains('"') || s.contains('\n') || s.contains('\r')
}

fn csv_quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        if c == '"' {
            out.push('"');
            out.push('"');
        } else {
            out.push(c);
        }
    }
    out.push('"');
    out
}

// ─────────────────────────── Markdown-KV formatter ───────────────────────────

/// Renders a `Compaction` as a `[N]{cols}` declaration followed by one
/// Markdown list item per row, each cell on its own `key: value` line.
///
/// Token-heavier than [`CsvSchemaFormatter`] (field names repeat per
/// row), but format-comprehension benchmarks show models retrieve
/// values from Markdown-KV substantially more reliably than from CSV.
/// Offered as an opt-in trade of tokens for read accuracy.
///
/// Rendering rules:
/// - Missing cells are omitted entirely (no `key:` line) — sparse rows
///   cost nothing, unlike CSV's positional empty cells.
/// - Strings that would be ambiguous on a line (contain newlines,
///   leading/trailing whitespace, or are empty) render JSON-quoted;
///   everything else renders raw.
/// - Nested cells render as compact inline JSON, matching
///   [`CsvSchemaFormatter`].
/// - Opaque cells keep the fixed `<<ccr:HASH,KIND,SIZE>>` marker
///   contract shared by all formatters.
#[derive(Debug, Clone, Default)]
pub struct MarkdownKvFormatter {
    /// If true, emit a `__dropped:N` note on the declaration line when
    /// rows were dropped under budget. Mirrors
    /// [`CsvSchemaFormatter::include_drop_summary`].
    pub include_drop_summary: bool,
}

impl MarkdownKvFormatter {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn with_drop_summary(mut self) -> Self {
        self.include_drop_summary = true;
        self
    }
}

impl Formatter for MarkdownKvFormatter {
    fn name(&self) -> &str {
        "markdown-kv"
    }

    fn format(&self, c: &Compaction) -> String {
        let mut out = String::new();
        write_compaction_kv(&mut out, c, self);
        out
    }
}

fn write_compaction_kv(out: &mut String, c: &Compaction, fmt: &MarkdownKvFormatter) {
    match c {
        Compaction::Table {
            schema,
            rows,
            original_count,
        } => {
            write_kv_table(out, schema, rows, *original_count, fmt);
        }
        Compaction::Buckets {
            discriminator,
            buckets,
            original_count,
        } => {
            out.push_str("__buckets:");
            out.push_str(discriminator);
            if fmt.include_drop_summary {
                let kept: usize = buckets.iter().map(|b| b.rows.len()).sum();
                if kept < *original_count {
                    out.push_str(&format!(" __dropped:{}", original_count - kept));
                }
            }
            out.push('\n');
            for b in buckets {
                out.push_str(&format!("__key:{}\n", kv_scalar(&b.key)));
                write_kv_table(out, &b.schema, &b.rows, b.rows.len(), fmt);
            }
        }
        Compaction::OpaqueRef {
            ccr_hash,
            byte_size,
            kind,
        } => {
            out.push_str(&format_ccr_marker(ccr_hash, *byte_size, kind));
        }
        // Payload-free (PERF-5); see `compaction_to_json`.
        Compaction::Untouched => {}
    }
}

fn write_kv_table(
    out: &mut String,
    schema: &Schema,
    rows: &[Row],
    original_count: usize,
    fmt: &MarkdownKvFormatter,
) {
    // Same declaration line as the CSV formatter: keeps row count and
    // typed shape up front where the model (and telemetry) expect it.
    // Unlike CSV (pre-existing exposure, kept byte-identical), KV quotes
    // pathological field names here so the declaration parses the same
    // way as the row lines below.
    out.push('[');
    out.push_str(&rows.len().to_string());
    out.push_str("]{");
    let col_decl: Vec<String> = schema
        .fields
        .iter()
        .map(|f| {
            let name = kv_field_name(&f.name);
            if f.nullable {
                format!("{}:{}?", name, f.type_tag)
            } else {
                format!("{}:{}", name, f.type_tag)
            }
        })
        .collect();
    out.push_str(&col_decl.join(","));
    out.push('}');
    if fmt.include_drop_summary && rows.len() < original_count {
        out.push_str(&format!(" __dropped:{}", original_count - rows.len()));
    }
    out.push('\n');

    for row in rows {
        // Compactor invariant: one cell per schema field. zip() would
        // silently drop extras — fail loudly in debug builds instead.
        debug_assert_eq!(row.0.len(), schema.fields.len());
        let mut wrote_first = false;
        for (field, cell) in schema.fields.iter().zip(row.0.iter()) {
            let rendered = match cell {
                CellValue::Missing => continue,
                CellValue::Scalar(v) => kv_scalar(v),
                CellValue::Nested(sub) => JsonFormatter::new().format(sub),
                // Byte-identical to the old `Nested(Untouched(v))` render.
                CellValue::DeclinedJson(v) => serde_json::to_string(v).unwrap_or_default(),
                CellValue::OpaqueRef {
                    ccr_hash,
                    byte_size,
                    kind,
                } => format_ccr_marker(ccr_hash, *byte_size, kind),
            };
            out.push_str(if wrote_first { "  " } else { "- " });
            out.push_str(&kv_field_name(&field.name));
            out.push_str(": ");
            out.push_str(&rendered);
            out.push('\n');
            wrote_first = true;
        }
        // All-missing row: keep a bare list item so the rendered row
        // count still matches the declaration.
        if !wrote_first {
            out.push_str("-\n");
        }
    }
}

fn kv_scalar(v: &Value) -> String {
    match v {
        Value::Null => "null".to_string(),
        Value::Bool(b) => if *b { "true" } else { "false" }.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            if needs_kv_quote(s) {
                serde_json::to_string(s).unwrap_or_default()
            } else {
                s.clone()
            }
        }
        // Object/array fall back to compact JSON (rare — usually
        // already promoted to Nested by the compactor).
        _ => serde_json::to_string(v).unwrap_or_default(),
    }
}

fn needs_kv_quote(s: &str) -> bool {
    s.is_empty()
        || s.contains('\n')
        || s.contains('\r')
        || s.starts_with(char::is_whitespace)
        || s.ends_with(char::is_whitespace)
}

/// Field names are normally bare identifiers, but nothing upstream
/// enforces that. Quote the pathological ones the same way as values:
/// an embedded newline would inject fake row lines, and `": "` inside
/// a key would split the line at the wrong colon on read-back.
fn kv_field_name(name: &str) -> String {
    if needs_kv_quote(name) || name.contains(": ") {
        serde_json::to_string(name).unwrap_or_default()
    } else {
        name.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transforms::smart_crusher::compaction::compactor::compact;
    use crate::transforms::smart_crusher::compaction::compactor::CompactConfig;
    use serde_json::json;

    fn cfg() -> CompactConfig {
        CompactConfig::default()
    }

    // ── JsonFormatter ──

    /// Per-row-UNIQUE filler with NO shared prefix or suffix across rows,
    /// so an encoding-isolation test's NON-target column triggers neither
    /// the cross-row affix fold NOR the low-cardinality dictionary (both
    /// of which would correctly fire on a column with shared structure or
    /// few distinct values). The leading char rotates through 24 letters
    /// (distinct prefixes) and a rotating trailing char + the unique index
    /// keep suffixes distinct; the embedded `i` makes every value unique.
    fn nonaffix(i: usize) -> String {
        let head = (b'a' + (i % 24) as u8) as char;
        let tail = (b'A' + ((i * 5 + 3) % 24) as u8) as char;
        format!("{head}{i}{tail}")
    }

    #[test]
    fn json_formatter_renders_table() {
        let items = vec![
            json!({"id": 1, "name": "alice"}),
            json!({"id": 2, "name": "bob"}),
        ];
        let c = compact(&items, &cfg());
        let out = JsonFormatter::new().format(&c);
        assert!(out.contains("\"_compaction\":\"table\""), "got: {out}");
        assert!(out.contains("\"_kept\":2"));
        assert!(out.contains("alice"));
    }

    #[test]
    fn json_formatter_renders_untouched_as_null() {
        // PERF-5: `Untouched` is payload-free — production callers gate
        // on `was_compacted()` and never format a declined compaction;
        // direct formatter use renders an explicit JSON null.
        let c = Compaction::Untouched;
        let out = JsonFormatter::new().format(&c);
        assert_eq!(out, "null");
    }

    #[test]
    fn json_formatter_renders_opaque_ref_marker() {
        let mut row = Row::new(vec![CellValue::OpaqueRef {
            ccr_hash: "abc123def456".into(),
            byte_size: 2048,
            kind: OpaqueKind::Base64Blob,
        }]);
        let c = Compaction::Table {
            schema: Schema {
                fields: vec![super::super::ir::FieldSpec {
                    name: "blob".into(),
                    type_tag: "ccr".into(),
                    nullable: false,
                    const_value: None,
                    encoding: None,
                }],
            },
            rows: vec![std::mem::replace(&mut row, Row::new(vec![]))],
            original_count: 1,
        };
        let out = JsonFormatter::new().format(&c);
        assert!(out.contains("\"_ccr\":\"abc123def456\""));
        assert!(out.contains("base64"));
    }

    // ── CsvSchemaFormatter ──

    #[test]
    fn csv_formatter_pure_tabular() {
        let items = vec![
            json!({"id": 1, "name": "alice", "status": "ok"}),
            json!({"id": 2, "name": "bob", "status": "ok"}),
            json!({"id": 3, "name": "carol", "status": "fail"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        // First line: declaration with [3]{...}
        assert!(lines[0].starts_with("[3]{"), "got line[0]: {}", lines[0]);
        assert!(lines[0].contains("id:int"));
        assert!(lines[0].contains("name:string"));
        assert!(lines[0].contains("status:string"));
        assert_eq!(lines.len(), 4);
    }

    #[test]
    fn csv_formatter_quotes_strings_with_commas() {
        let items = vec![
            json!({"id": 1, "name": "alice, the great"}),
            json!({"id": 2, "name": "bob"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(out.contains(r#""alice, the great""#));
    }

    #[test]
    fn csv_formatter_escapes_internal_quotes() {
        let items = vec![
            json!({"id": 1, "msg": "she said \"hi\""}),
            json!({"id": 2, "msg": "ok"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(out.contains(r#""she said ""hi""""#));
    }

    #[test]
    fn csv_formatter_renders_buckets() {
        let items = vec![
            json!({"type": "user", "id": 1, "name": "alice"}),
            json!({"type": "user", "id": 2, "name": "bob"}),
            json!({"type": "order", "id": 99, "total": 50}),
            json!({"type": "order", "id": 100, "total": 75}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(out.starts_with("__buckets:type"));
        assert!(out.contains("__key:order"));
        assert!(out.contains("__key:user"));
    }

    #[test]
    fn csv_formatter_emits_ccr_marker() {
        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let items = vec![
            json!({"id": 1, "blob": big.clone()}),
            json!({"id": 2, "blob": big.clone()}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(out.contains("<<ccr:"), "got: {out}");
        assert!(out.contains(",base64,"));
    }

    #[test]
    fn csv_formatter_nested_cell_inline_json() {
        let items = vec![
            json!({"event": "batch", "payload": r#"[{"x":1},{"x":2},{"x":3}]"#}),
            json!({"event": "batch", "payload": r#"[{"x":4},{"x":5}]"#}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        // Nested compaction is JSON-rendered then CSV-quoted, so a
        // `_compaction":"table"` substring should appear inside quotes.
        assert!(out.contains("_compaction"), "got: {out}");
    }

    #[test]
    fn csv_formatter_drop_summary_opt_in() {
        let mut rows = vec![Row::new(vec![CellValue::Scalar(json!(1))])];
        rows.push(Row::new(vec![CellValue::Scalar(json!(2))]));
        let c = Compaction::Table {
            schema: Schema {
                fields: vec![super::super::ir::FieldSpec {
                    name: "x".into(),
                    type_tag: "int".into(),
                    nullable: false,
                    const_value: None,
                    encoding: None,
                }],
            },
            rows,
            original_count: 5, // 3 dropped
        };
        let with_summary = CsvSchemaFormatter::new().with_drop_summary().format(&c);
        assert!(with_summary.contains("__dropped:3"));
        let without = CsvSchemaFormatter::new().format(&c);
        assert!(!without.contains("__dropped"));
    }

    // ── Constant-column fold (CSV) ──

    #[test]
    fn csv_constant_columns_fold_into_declaration() {
        let items = vec![
            json!({"bytes": 64, "from": "127.0.0.1", "seq": 0, "t": 0.1}),
            json!({"bytes": 64, "from": "127.0.0.1", "seq": 1, "t": 0.2}),
            json!({"bytes": 64, "from": "127.0.0.1", "seq": 2, "t": 0.3}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        // Declaration carries the constants once.
        assert!(lines[0].contains("bytes:int=64"), "got: {}", lines[0]);
        assert!(
            lines[0].contains("from:string=127.0.0.1"),
            "got: {}",
            lines[0]
        );
        // The monotone counter folds as an arithmetic progression.
        assert!(lines[0].contains("seq:int=0+1"), "got: {}", lines[0]);
        // The float column scale-folds (`%1`): cells are value × 10.
        assert!(lines[0].contains("t:float%1"), "got: {}", lines[0]);
        // Rows hold ONLY the remaining variable cells (fields sort
        // alphabetically at equal frequency: bytes,from,seq,t → t after
        // const + arith folds), scale-encoded.
        assert_eq!(lines[1], "1");
        assert_eq!(lines[2], "2");
        assert_eq!(lines[3], "3");
    }

    #[test]
    fn csv_constant_fold_round_trips_losslessly() {
        // A consumer holding only the output reconstructs every row:
        // parse `name:type=value` constants + per-row variable cells.
        let items: Vec<Value> = (0..20)
            .map(|i| json!({"bytes": 64, "from": "127.0.0.1", "seq": i, "ttl": 64}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let mut lines = out.trim_end().lines();
        let decl = lines.next().expect("declaration line");
        let body = decl
            .strip_prefix("[20]{")
            .and_then(|s| s.strip_suffix('}'))
            .expect("decl shape");
        let mut const_cols: Vec<(String, Value)> = Vec::new();
        let mut var_cols: Vec<String> = Vec::new();
        for col in body.split(',') {
            let (name, rest) = col.split_once(':').expect("name:type");
            match rest.split_once('=') {
                Some((_t, raw)) => {
                    let v = serde_json::from_str::<Value>(raw)
                        .unwrap_or_else(|_| Value::String(raw.to_string()));
                    const_cols.push((name.to_string(), v));
                }
                None => var_cols.push(name.to_string()),
            }
        }
        let mut reconstructed: Vec<Value> = Vec::new();
        for line in lines {
            let mut obj = serde_json::Map::new();
            for (name, v) in &const_cols {
                obj.insert(name.clone(), v.clone());
            }
            for (name, raw) in var_cols.iter().zip(line.split(',')) {
                let v = serde_json::from_str::<Value>(raw)
                    .unwrap_or_else(|_| Value::String(raw.to_string()));
                obj.insert(name.clone(), v);
            }
            reconstructed.push(Value::Object(obj));
        }
        assert_eq!(reconstructed, items, "round-trip must be lossless");
    }

    #[test]
    fn csv_constant_string_with_separator_chars_is_quoted() {
        let items = vec![
            json!({"id": 1, "tag": "a=b,{c}"}),
            json!({"id": 2, "tag": "a=b,{c}"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let decl = out.lines().next().unwrap();
        assert!(
            decl.contains(r#"tag:string="a=b,{c}""#),
            "constant with separator chars must be CSV-quoted, got: {decl}"
        );
    }

    // ── Ditto marks (CSV) ──

    #[test]
    fn csv_consecutive_repeats_render_as_ditto() {
        let items = vec![
            json!({"path": "src/a.py", "line": 10, "txt": "alpha"}),
            json!({"path": "src/a.py", "line": 21, "txt": "Bravo9"}),
            json!({"path": "src/b.py", "line": 30, "txt": "kilo!"}),
            json!({"path": "src/b.py", "line": 40, "txt": "oscar"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        // Columns sort alphabetically at equal frequency: line,path,txt.
        // (`line` steps 11,9,10 — non-constant, so no arithmetic fold.)
        assert_eq!(lines[1], "10,src/a.py,alpha");
        assert_eq!(lines[2], "21,=,Bravo9", "repeat path must ditto");
        assert_eq!(lines[3], "30,src/b.py,kilo!", "run break re-materializes");
        assert_eq!(lines[4], "40,=,oscar");
    }

    #[test]
    fn csv_ditto_round_trips_losslessly() {
        // `line` alternates step 4,2 (non-constant) so the arithmetic
        // fold stays out of the way — this test pins DITTO round-trip.
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "path": nonaffix(i / 5),
                    "line": 3 * i + 1 + (i % 2),
                    "code": if i % 5 < 2 { 200 } else { 503 },
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let mut lines = out.trim_end().lines();
        let decl = lines.next().expect("declaration line");
        let body = decl
            .strip_prefix("[30]{")
            .and_then(|s| s.strip_suffix('}'))
            .expect("decl shape");
        let cols: Vec<&str> = body
            .split(',')
            .map(|c| c.split(':').next().unwrap())
            .collect();
        let mut reconstructed: Vec<Value> = Vec::new();
        let mut carry: Vec<Option<Value>> = vec![None; cols.len()];
        for line in lines {
            let mut obj = serde_json::Map::new();
            for (j, (name, raw)) in cols.iter().zip(line.split(',')).enumerate() {
                let v = if raw == "=" {
                    carry[j]
                        .clone()
                        .expect("ditto never appears before a value")
                } else {
                    let v = serde_json::from_str::<Value>(raw)
                        .unwrap_or_else(|_| Value::String(raw.to_string()));
                    carry[j] = Some(v.clone());
                    v
                };
                obj.insert((*name).to_string(), v);
            }
            reconstructed.push(Value::Object(obj));
        }
        assert_eq!(reconstructed, items, "ditto round-trip must be lossless");
    }

    #[test]
    fn csv_literal_equals_cell_is_quoted_not_ditto() {
        let items = vec![
            json!({"id": 1, "op": "="}),
            json!({"id": 2, "op": "="}),
            json!({"id": 3, "op": "+"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        // `id` arith-folds into the declaration; `op` is the only row
        // cell. First materialization is the QUOTED literal; the
        // consecutive repeat dittos the quoted form (carry-forward
        // yields `"="`).
        assert!(lines[0].contains("id:int=1+1"), "got: {}", lines[0]);
        assert_eq!(lines[1], "\"=\"");
        assert_eq!(lines[2], "=");
        assert_eq!(lines[3], "+");
    }

    #[test]
    fn csv_single_char_cells_never_ditto() {
        // "y" repeats consecutively but the column is NOT constant
        // (last row differs, so the const fold stays out of the way).
        let items = vec![
            json!({"id": 1, "flag": "y"}),
            json!({"id": 2, "flag": "y"}),
            json!({"id": 3, "flag": "n"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        // 1-char cells never ditto — `=` would save nothing. (`id`
        // arith-folds into the declaration; `flag` is the only row cell.)
        assert!(out.contains("y\ny\nn\n"), "got: {out}");
    }

    // ── Arithmetic fold (CSV) ──

    #[test]
    fn csv_arith_fold_round_trips_losslessly() {
        // A consumer holding only the output reconstructs every row:
        // `name:int=BASE+STEP` regenerates value_i = BASE + STEP*i.
        let items: Vec<Value> = (0..40)
            .map(|i| json!({"seq": 7 + 3 * i, "t": format!("v{}", i * i)}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let mut lines = out.trim_end().lines();
        let decl = lines.next().expect("declaration line");
        assert!(decl.contains("seq:int=7+3"), "got decl: {decl}");
        let mut reconstructed: Vec<Value> = Vec::new();
        for (i, line) in lines.enumerate() {
            reconstructed.push(json!({"seq": 7 + 3 * (i as i64), "t": line}));
        }
        assert_eq!(reconstructed, items, "arith round-trip must be lossless");
    }

    #[test]
    fn csv_arith_fold_handles_negative_step() {
        let items: Vec<Value> = (0..10)
            .map(|i| json!({"countdown": 100 - 5 * i, "v": format!("x{i}")}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let decl = out.lines().next().unwrap();
        assert!(decl.contains("countdown:int=100+-5"), "got decl: {decl}");
        // Rows carry only `v`.
        assert!(out.contains("\nx0\nx1\n"), "got: {out}");
    }

    #[test]
    fn csv_arith_fold_never_empties_rows() {
        // bytes/ttl are constants, seq is a perfect progression — but
        // folding seq too would leave EMPTY row lines (unreconstructible
        // row count). The last visible column must stay in the rows.
        let items: Vec<Value> = (0..20)
            .map(|i| json!({"bytes": 64, "seq": i, "ttl": 64}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        assert!(
            !lines[0].contains("seq:int=0+1"),
            "seq must NOT fold when it is the last row-visible column: {}",
            lines[0]
        );
        assert_eq!(lines[1..].len(), 20, "every row line present");
        assert_eq!(lines[1], "0");
        assert_eq!(lines[20], "19");
    }

    #[test]
    fn csv_non_constant_step_does_not_fold() {
        let items = vec![
            json!({"n": 1, "v": "a"}),
            json!({"n": 2, "v": "b"}),
            json!({"n": 4, "v": "c"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let decl = out.lines().next().unwrap();
        assert!(!decl.contains("n:int="), "got decl: {decl}");
        assert!(out.contains("1,a\n2,b\n4,c\n"), "got: {out}");
    }

    // ── ISO-delta encoding (CSV) ──

    #[test]
    fn csv_iso_delta_marks_declaration_and_renders_deltas() {
        let items: Vec<Value> = (0..10)
            .map(|i| {
                json!({
                    "date": format!("2026-06-11T21:{:02}:05+02:00", i * 3),
                    "v": nonaffix(i),
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        assert!(lines[0].contains("date:string~"), "got decl: {}", lines[0]);
        // First row carries the verbatim timestamp; later rows carry
        // second deltas (180s apart, same tz → no spelling).
        assert!(
            lines[1].starts_with("2026-06-11T21:00:05+02:00,"),
            "got: {}",
            lines[1]
        );
        assert!(lines[2].starts_with("+180,"), "got: {}", lines[2]);
        assert!(
            lines[3].starts_with("+180,") || lines[3].starts_with("=,"),
            "got: {}",
            lines[3]
        );
    }

    #[test]
    fn csv_iso_delta_round_trips_losslessly() {
        use super::super::encodings::decode_iso_column;
        // Real-shaped commit dates: non-monotone, mixed timezones,
        // distinct deltas (no ditto interference in this fixture).
        let dates = [
            "2026-06-11T21:02:05-07:00",
            "2026-06-11T19:55:13+02:00",
            "2026-06-11T18:55:19+02:00",
            "2026-06-10T19:11:46-07:00",
            "2026-06-10T21:53:18-04:00",
            "2026-06-12T00:00:00Z",
        ];
        let items: Vec<Value> = dates
            .iter()
            .enumerate()
            .map(|(i, d)| json!({"date": *d, "v": nonaffix(i)}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let mut lines = out.trim_end().lines();
        let decl = lines.next().expect("decl");
        assert!(decl.contains("date:string~"), "got decl: {decl}");
        // Extract the first (date) cell of each row and decode the
        // column with the shared decoder — exact reconstruction.
        let cells: Vec<String> = lines
            .map(|l| l.split(',').next().unwrap().to_string())
            .collect();
        let decoded = decode_iso_column(&cells).expect("decode");
        assert_eq!(decoded, dates, "ISO-delta round-trip must be exact");
    }

    #[test]
    fn csv_iso_delta_not_stamped_on_nonconforming_values() {
        // One fractional-second timestamp poisons the ISO-delta path — the
        // column must NOT be stamped `string~`. It may still be folded by
        // the cross-row affix encoding (which is lossless), so we prove the
        // exact values are recoverable rather than asserting verbatim
        // presence. The poisoned value uses an unrelated head so the
        // column shares no affix at all and stays fully plain.
        let dates = [
            "2026-06-11T21:02:05+02:00".to_string(),
            "2025-01-02T08:30:00Z".to_string(),
            "1999-12-31T23:59:59-05:00".to_string(),
            "2026-06-11T21:02:05.123+02:00".to_string(), // fractional: poisons ISO
            "2030-07-04T00:00:00+09:00".to_string(),
        ];
        let items: Vec<Value> = dates
            .iter()
            .enumerate()
            .map(|(i, d)| json!({"date": d, "v": nonaffix(i)}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        // Not ISO-delta encoded (the fractional second poisons it).
        assert!(!out.contains("string~"), "got: {out}");
        // The values share no common affix either, so every timestamp is
        // verbatim in the output.
        for d in &dates {
            assert!(out.contains(d.as_str()), "verbatim {d}, got: {out}");
        }
    }

    // ── Cross-row affix fold (CSV) ──

    #[test]
    fn csv_affix_marks_declaration_and_strips_cells() {
        // Near-unique paths sharing a long root + extension — the affix.
        let items: Vec<Value> = (0..12)
            .map(|i| {
                json!({
                    "path": format!("crates/core/src/mod_{i}_{}.rs", i * 7),
                    "n": nonaffix(i),
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        // The path column is marked `^` and the affix line declares the
        // shared prefix and suffix exactly once.
        assert!(lines[0].contains("path:string^"), "got decl: {}", lines[0]);
        let affix_line = lines
            .iter()
            .find(|l| l.starts_with("__affix:path="))
            .expect("affix preamble line");
        assert!(
            affix_line.contains("crates/core/src/mod_"),
            "shared prefix declared once: {affix_line}"
        );
        assert!(
            affix_line.ends_with(".rs"),
            "shared suffix declared: {affix_line}"
        );
        // The long shared root appears exactly once in the whole output.
        assert_eq!(
            out.matches("crates/core/src/mod_").count(),
            1,
            "affix root must appear exactly once, not per row"
        );
    }

    #[test]
    fn csv_affix_round_trips_losslessly() {
        use super::super::encodings::decode_affix_cell;
        // URL-shaped rows: shared scheme/host prefix, unique sha tail.
        let items: Vec<Value> = (0..20)
            .map(|i| {
                json!({
                    "url": format!("https://api.example.com/v2/items/{:08x}/raw", (i as u64).wrapping_mul(2_654_435_761) & 0xffff_ffff),
                    "k": nonaffix(i),
                })
            })
            .collect();
        let originals: Vec<String> = items
            .iter()
            .map(|v| v["url"].as_str().unwrap().to_string())
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        assert!(lines[0].contains("url:string^"), "got decl: {}", lines[0]);
        // Recover the affix from the preamble line and reconstruct every
        // url cell — must equal the originals exactly.
        let affix_line = lines
            .iter()
            .find(|l| l.starts_with("__affix:url="))
            .expect("affix line");
        let payload = affix_line.strip_prefix("__affix:url=").unwrap();
        // Affix here has no CSV-special chars, so a plain split is exact.
        let (prefix, suffix) = payload.split_once(',').expect("prefix,suffix");
        // Find url's column index from the declaration (columns sort by
        // frequency then alphabetically, so the position is not assumed).
        let decl_body = lines[0]
            .strip_prefix("[20]{")
            .and_then(|s| s.strip_suffix('}'))
            .expect("decl shape");
        let url_col = decl_body
            .split(',')
            .position(|c| c.starts_with("url:"))
            .expect("url column present");
        let recovered: Vec<String> = lines
            .iter()
            .filter(|l| !l.starts_with('[') && !l.starts_with("__"))
            .map(|l| {
                let mid = l.split(',').nth(url_col).unwrap();
                decode_affix_cell(mid, prefix, suffix)
            })
            .collect();
        assert_eq!(recovered, originals, "affix round-trip must be exact");
    }

    // ── Head-dict fold (CSV) ──

    #[test]
    fn csv_head_dict_marks_declaration_and_round_trips() {
        use super::super::encodings::{decode_head_cell, decode_head_value};
        // Paths grouped under a few directories: low-cardinality head,
        // unique tail. The single common affix is short (only the shared
        // root), so head-dict is the bigger fold.
        let dirs = [
            "src/transforms/smart_crusher/",
            "src/cache/store/",
            "src/pipeline/offloads/",
            "lib/util/text/",
        ];
        let items: Vec<Value> = (0..40)
            .map(|i| {
                json!({
                    "path": format!("{}{}.rs", dirs[i % 4], nonaffix(i)),
                    "ln": (i * 7) as i64,
                })
            })
            .collect();
        let originals: Vec<String> = items
            .iter()
            .map(|v| v["path"].as_str().unwrap().to_string())
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        assert!(lines[0].contains("path:string@"), "got decl: {}", lines[0]);
        let head_line = lines
            .iter()
            .find(|l| l.starts_with("__head:path="))
            .expect("head preamble line");
        // Each distinct directory head appears exactly once.
        for d in dirs {
            assert_eq!(out.matches(d).count(), 1, "head {d} must appear once");
        }
        // Reconstruct every path cell from the head dictionary + cells.
        let payload = head_line.strip_prefix("__head:path=").unwrap();
        let delim = payload.chars().next().unwrap();
        let heads: Vec<&str> = payload[delim.len_utf8()..].split(',').collect();
        // `ln` is an exact arithmetic progression (0,7,14,...) so it folds
        // into the declaration and is NOT row-visible; `path` is the only
        // remaining cell per row. Find its position among ROW-VISIBLE
        // columns (those without a `=` const/arith fold in the decl).
        let decl_body = lines[0]
            .strip_prefix("[40]{")
            .and_then(|s| s.strip_suffix('}'))
            .unwrap();
        let visible_cols: Vec<&str> = decl_body.split(',').filter(|c| !c.contains('=')).collect();
        let path_col = visible_cols
            .iter()
            .position(|c| c.starts_with("path:"))
            .unwrap();
        let recovered: Vec<String> = lines
            .iter()
            .filter(|l| !l.starts_with('[') && !l.starts_with("__"))
            .map(|l| {
                let cell = l.split(',').nth(path_col).unwrap();
                let (idx, tail) = decode_head_cell(cell, delim).expect("decode cell");
                decode_head_value(heads[idx], tail)
            })
            .collect();
        assert_eq!(recovered, originals, "head-dict round-trip must be exact");
    }

    // ── Decimal scale-fold (CSV) ──

    #[test]
    fn csv_decimal_scale_round_trips_losslessly() {
        use super::super::encodings::decode_decimal_cell;
        // Real-ping-shaped latencies (varied fractional digits).
        let latencies = [0.053, 0.09, 0.116, 12.5, 0.046, 0.071];
        let items: Vec<Value> = latencies
            .iter()
            .enumerate()
            .map(|(i, t)| json!({"ms": t, "v": nonaffix(i)}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let mut lines = out.trim_end().lines();
        let decl = lines.next().expect("decl");
        assert!(decl.contains("ms:float%3"), "got decl: {decl}");
        let cells: Vec<&str> = lines.map(|l| l.split(',').next().unwrap()).collect();
        assert_eq!(cells[0], "53", "0.053 at scale 3");
        assert_eq!(cells[1], "90", "0.09 pads to 090 -> 90");
        assert_eq!(cells[3], "12500", "12.5 scales by 10^3");
        for (cell, orig) in cells.iter().zip(latencies.iter()) {
            let dec = decode_decimal_cell(cell, 3).expect("decode");
            let back: f64 = dec.parse().expect("parse");
            assert_eq!(back, *orig, "value round-trip {cell} -> {dec}");
        }
    }

    #[test]
    fn csv_decimal_scale_refuses_mixed_and_exponent_columns() {
        // A mixed int/float column gets the `json` tag — must not stamp.
        let items = vec![
            json!({"x": 1, "v": "a"}),
            json!({"x": 2.5, "v": "b"}),
            json!({"x": 3, "v": "c"}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(!out.contains('%'), "mixed column must stay plain: {out}");
        // Exponent renderings refuse too (values stay verbatim).
        let items: Vec<Value> = (1..5)
            .map(|i| json!({"x": 1e300 * i as f64, "v": format!("b{i}")}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(!out.contains('%'), "exponent column must stay plain: {out}");
        assert!(out.contains("1e+300"), "got: {out}");
    }

    // ── Dictionary encoding (CSV) ──

    #[test]
    fn csv_dict_column_emits_preamble_and_indexes() {
        // Low-cardinality author-like column over many rows, repeating
        // NON-consecutively (so ditto can't catch it — the dict case).
        let authors = ["Alice Cooper", "Bob the Builder", "Carol Danvers"];
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "author": authors[i % 3],
                    "msg": nonaffix(i),
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        assert!(
            lines[1].starts_with("__dict:author="),
            "got line[1]: {}",
            lines[1]
        );
        assert!(lines[1].contains("Alice Cooper"), "got: {}", lines[1]);
        // Each distinct value appears exactly ONCE in the whole output.
        for a in authors {
            assert_eq!(out.matches(a).count(), 1, "{a} must appear exactly once");
        }
        // Rows carry indexes (first column: author sorts before msg).
        assert!(lines[2].starts_with("0,"), "got: {}", lines[2]);
        assert!(lines[3].starts_with("1,"), "got: {}", lines[3]);
        assert!(lines[4].starts_with("2,"), "got: {}", lines[4]);
        assert!(lines[5].starts_with("0,"), "got: {}", lines[5]);
    }

    #[test]
    fn csv_dict_round_trips_losslessly() {
        let levels = ["info", "warning", "error", "critical"];
        let items: Vec<Value> = (0..40)
            .map(|i| {
                json!({
                    "level": levels[(i * 7) % 4],
                    "msg": nonaffix(i),
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        let mut lines = out.trim_end().lines();
        let _decl = lines.next().expect("decl");
        let dict_line = lines.next().expect("dict line");
        let payload = dict_line
            .strip_prefix("__dict:level=")
            .expect("dict preamble for level");
        let values: Vec<&str> = payload.split(',').collect();
        // Decode: resolve ditto on the index column, then look up.
        let mut reconstructed: Vec<Value> = Vec::new();
        let mut carry: Option<&str> = None;
        for (i, line) in lines.enumerate() {
            let (idx_cell, msg) = line.split_once(',').expect("two cells");
            let resolved = if idx_cell == "=" {
                carry.expect("ditto after a value")
            } else {
                carry = Some(idx_cell);
                idx_cell
            };
            let level = values[resolved.parse::<usize>().expect("index")];
            reconstructed.push(json!({"level": level, "msg": msg}));
            let _ = i;
        }
        let originals: Vec<Value> = items.clone();
        assert_eq!(reconstructed, originals, "dict round-trip must be exact");
    }

    #[test]
    fn csv_dict_refuses_high_cardinality_and_tiny_columns() {
        // All-distinct column: indexes save nothing — must stay plain.
        let items: Vec<Value> = (0..20)
            .map(|i| json!({"path": format!("src/module_{i}.py"), "n": format!("x{i}")}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(!out.contains("__dict:"), "got: {out}");
        // Short values (1 char): index cells save nothing either.
        let items: Vec<Value> = (0..20)
            .map(|i| json!({"f": if i % 2 == 0 { "y" } else { "n" }, "n": format!("x{i}")}))
            .collect();
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(
            !out.contains("__dict:"),
            "1-char values must not dict: {out}"
        );
    }

    #[test]
    fn csv_data_cell_starting_with_dict_marker_is_quoted() {
        let items = vec![
            json!({"a": "__dict:fake=x,y", "b": 1}),
            json!({"a": "plain", "b": 2}),
            json!({"a": "other", "b": 4}),
        ];
        let c = compact(&items, &cfg());
        let out = CsvSchemaFormatter::new().format(&c);
        assert!(
            out.contains("\"__dict:fake=x,y\""),
            "cell with __dict: prefix must be CSV-quoted: {out}"
        );
    }

    #[test]
    fn csv_grammar_breaking_column_name_declines_compaction() {
        // COR-15: the CSV formatter never quotes COLUMN NAMES (only
        // cells), so the compactor DECLINES these arrays — no `[N]{...}`
        // declaration may ever ship for them. Since PERF-5 the declined
        // `Untouched` is payload-free: the verbatim byte-exact
        // passthrough is the CALLER's contract (every production caller
        // gates on `was_compacted()` and re-uses its own borrow of the
        // array — pinned by the walker/route passthrough tests), and a
        // direct format of a declined compaction renders nothing.
        for key in ["meta:region", "a,b", "x{y"] {
            let items: Vec<Value> = (0..5)
                .map(|i| json!({"id": i, key: format!("srv-{i:03}.internal.example.com")}))
                .collect();
            let c = compact(&items, &cfg());
            assert!(
                !c.was_compacted(),
                "key {key:?} must decline compaction, got {c:?}"
            );
            let out = CsvSchemaFormatter::new().format(&c);
            assert!(
                !out.contains("]{"),
                "no `[N]{{...}}` declaration for key {key:?}: {out}"
            );
            assert!(out.is_empty(), "declined render is empty, got: {out}");
        }
    }

    #[test]
    fn json_formatter_unchanged_by_dict_encoding() {
        let items: Vec<Value> = (0..10)
            .map(|i| json!({"level": if i % 2 == 0 { "information" } else { "warning-level" }, "n": i}))
            .collect();
        let c = compact(&items, &cfg());
        let out = JsonFormatter::new().format(&c);
        assert!(!out.contains("__dict:"), "got: {out}");
        assert!(
            out.matches("information").count() >= 5,
            "verbatim values: {out}"
        );
    }

    #[test]
    fn json_formatter_unchanged_by_iso_delta() {
        let items: Vec<Value> = (0..4)
            .map(|i| json!({"date": format!("2026-06-1{i}T00:00:00Z"), "v": i}))
            .collect();
        let c = compact(&items, &cfg());
        let out = JsonFormatter::new().format(&c);
        for i in 0..4 {
            assert!(
                out.contains(&format!("2026-06-1{i}T00:00:00Z")),
                "JSON formatter must keep verbatim timestamps: {out}"
            );
        }
        assert!(!out.contains("string~"), "got: {out}");
    }

    #[test]
    fn json_formatter_unchanged_by_arith_fold() {
        // The JSON formatter ignores `encoding` — byte-identical output
        // to the pre-encoding engine (rows keep all cells).
        let items: Vec<Value> = (0..5)
            .map(|i| json!({"seq": i, "v": format!("x{i}")}))
            .collect();
        let c = compact(&items, &cfg());
        let out = JsonFormatter::new().format(&c);
        assert!(
            out.contains("\"_rows\":[[0,\"x0\"],[1,\"x1\"],[2,\"x2\"],[3,\"x3\"],[4,\"x4\"]]"),
            "got: {out}"
        );
        assert!(!out.contains("encoding"), "got: {out}");
    }

    #[test]
    fn json_formatter_unchanged_by_constant_fold() {
        // The JSON formatter ignores const_value — byte-identical output
        // to the pre-fold engine (rows keep all cells).
        let items = vec![
            json!({"bytes": 64, "seq": 0}),
            json!({"bytes": 64, "seq": 1}),
        ];
        let c = compact(&items, &cfg());
        let out = JsonFormatter::new().format(&c);
        assert!(out.contains("\"_rows\":[[64,0],[64,1]]"), "got: {out}");
        assert!(!out.contains("const"), "got: {out}");
    }

    #[test]
    fn estimate_matches_format_len() {
        let items = vec![json!({"a": 1}), json!({"a": 2})];
        let c = compact(&items, &cfg());
        let f = CsvSchemaFormatter::new();
        assert_eq!(f.estimate_bytes(&c), f.format(&c).len());
    }

    // ── Cross-formatter property: same input → smaller CSV than JSON ──
    // This is the headline value prop. If it doesn't hold for "obviously
    // tabular" input, the formatter is broken or the fixture is wrong.

    #[test]
    fn csv_smaller_than_json_for_tabular() {
        let items: Vec<Value> = (0..50)
            .map(|i| {
                json!({
                    "id": i,
                    "name": format!("user_{i}"),
                    "email": format!("user_{i}@example.com"),
                    "status": if i % 3 == 0 { "ok" } else { "pending" },
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let json_out = JsonFormatter::new().format(&c);
        let csv_out = CsvSchemaFormatter::new().format(&c);
        // CSV should beat the structured-JSON formatter (both
        // deduplicate the schema, so the win comes from removing
        // structural punctuation only — modest, but real).
        assert!(
            csv_out.len() < json_out.len(),
            "csv {} bytes vs json {} bytes",
            csv_out.len(),
            json_out.len()
        );
    }

    #[test]
    fn csv_substantially_smaller_than_raw_json() {
        // The headline value prop: CSV+schema beats naïve JSON
        // serialization of the same array (where every row repeats
        // every field name) by a wide margin.
        let items: Vec<Value> = (0..50)
            .map(|i| {
                json!({
                    "id": i,
                    "name": format!("user_{i}"),
                    "email": format!("user_{i}@example.com"),
                    "status": if i % 3 == 0 { "ok" } else { "pending" },
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let csv_out = CsvSchemaFormatter::new().format(&c);
        let raw_json = serde_json::to_string(&Value::Array(items.clone())).unwrap();
        assert!(
            csv_out.len() * 10 < raw_json.len() * 7,
            "csv {} bytes vs raw json {} bytes — expected >30% reduction",
            csv_out.len(),
            raw_json.len()
        );
    }

    // ── MarkdownKvFormatter ──

    #[test]
    fn markdown_kv_renders_table() {
        let items = vec![
            json!({"id": 1, "name": "alice"}),
            json!({"id": 2, "name": "bob"}),
        ];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        let lines: Vec<&str> = out.trim_end().lines().collect();
        assert!(lines[0].starts_with("[2]{"), "got line[0]: {}", lines[0]);
        assert!(lines[0].contains("id:int"));
        assert!(out.contains("- id: 1\n  name: alice\n"), "got: {out}");
        assert!(out.contains("- id: 2\n  name: bob\n"), "got: {out}");
    }

    #[test]
    fn markdown_kv_omits_missing_cells() {
        let items = vec![json!({"id": 1, "note": "has note"}), json!({"id": 2})];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        assert!(out.contains("note: has note"), "got: {out}");
        // Row 2 has no `note:` line at all.
        let row2 = out.split("- id: 2").nth(1).expect("row 2 present");
        assert!(!row2.contains("note:"), "got row2 tail: {row2}");
    }

    #[test]
    fn markdown_kv_quotes_ambiguous_strings() {
        let items = vec![
            json!({"id": 1, "msg": "line one\nline two"}),
            json!({"id": 2, "msg": "plain"}),
        ];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        assert!(
            out.contains(r#"msg: "line one\nline two""#),
            "multiline must be JSON-quoted, got: {out}"
        );
        assert!(out.contains("msg: plain\n"), "got: {out}");
    }

    #[test]
    fn markdown_kv_quotes_pathological_field_names() {
        // A newline in a key would inject fake row lines; ": " in a key
        // would split read-back at the wrong colon. Both get JSON-quoted
        // in the declaration and in every row line.
        //
        // The COR-15 gate makes `compact()` decline such keys upstream,
        // so this drives the KV formatter directly with a hand-built
        // Table: `kv_field_name` stays the last line of defense for
        // producers that construct the IR themselves.
        let fields = vec![
            super::super::ir::FieldSpec {
                name: "bad\nkey".into(),
                type_tag: "int".into(),
                nullable: false,
                const_value: None,
                encoding: None,
            },
            super::super::ir::FieldSpec {
                name: "note: extra".into(),
                type_tag: "string".into(),
                nullable: false,
                const_value: None,
                encoding: None,
            },
        ];
        let rows = vec![
            Row::new(vec![
                CellValue::Scalar(json!(1)),
                CellValue::Scalar(json!("x")),
            ]),
            Row::new(vec![
                CellValue::Scalar(json!(2)),
                CellValue::Scalar(json!("y")),
            ]),
        ];
        let c = Compaction::Table {
            schema: Schema { fields },
            rows,
            original_count: 2,
        };
        let out = MarkdownKvFormatter::new().format(&c);
        assert!(!out.contains("bad\nkey"), "raw newline key leaked: {out}");
        assert!(out.contains(r#""bad\nkey""#), "got: {out}");
        assert!(out.contains(r#""note: extra": x"#), "got: {out}");
        let decl = out.lines().next().unwrap();
        assert!(decl.contains(r#""bad\nkey":int"#), "got decl: {decl}");
    }

    #[test]
    fn markdown_kv_plain_strings_unquoted() {
        let items = vec![
            json!({"id": 1, "name": "alice, the \"great\""}),
            json!({"id": 2, "name": "bob"}),
        ];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        // Commas and quotes are fine on a KV line — no CSV-style quoting.
        assert!(out.contains(r#"name: alice, the "great""#), "got: {out}");
    }

    #[test]
    fn markdown_kv_emits_ccr_marker() {
        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let items = vec![
            json!({"id": 1, "blob": big.clone()}),
            json!({"id": 2, "blob": big.clone()}),
        ];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        assert!(out.contains("<<ccr:"), "got: {out}");
        assert!(out.contains(",base64,"));
    }

    #[test]
    fn markdown_kv_renders_buckets() {
        let items = vec![
            json!({"type": "user", "id": 1, "name": "alice"}),
            json!({"type": "user", "id": 2, "name": "bob"}),
            json!({"type": "order", "id": 99, "total": 50}),
            json!({"type": "order", "id": 100, "total": 75}),
        ];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        assert!(out.starts_with("__buckets:type"), "got: {out}");
        assert!(out.contains("__key:order"));
        assert!(out.contains("__key:user"));
        assert!(out.contains("- id:"), "got: {out}");
    }

    #[test]
    fn markdown_kv_drop_summary_opt_in() {
        let rows = vec![
            Row::new(vec![CellValue::Scalar(json!(1))]),
            Row::new(vec![CellValue::Scalar(json!(2))]),
        ];
        let c = Compaction::Table {
            schema: Schema {
                fields: vec![super::super::ir::FieldSpec {
                    name: "x".into(),
                    type_tag: "int".into(),
                    nullable: false,
                    const_value: None,
                    encoding: None,
                }],
            },
            rows,
            original_count: 5, // 3 dropped
        };
        let with_summary = MarkdownKvFormatter::new().with_drop_summary().format(&c);
        assert!(with_summary.contains("__dropped:3"));
        let without = MarkdownKvFormatter::new().format(&c);
        assert!(!without.contains("__dropped"));
    }

    #[test]
    fn markdown_kv_nested_cell_inline_json() {
        let items = vec![
            json!({"event": "batch", "payload": r#"[{"x":1},{"x":2},{"x":3}]"#}),
            json!({"event": "batch", "payload": r#"[{"x":4},{"x":5}]"#}),
        ];
        let c = compact(&items, &cfg());
        let out = MarkdownKvFormatter::new().format(&c);
        assert!(out.contains("_compaction"), "got: {out}");
    }

    #[test]
    fn markdown_kv_estimate_matches_format_len() {
        let items = vec![json!({"a": 1}), json!({"a": 2})];
        let c = compact(&items, &cfg());
        let f = MarkdownKvFormatter::new();
        assert_eq!(f.estimate_bytes(&c), f.format(&c).len());
    }

    #[test]
    fn markdown_kv_smaller_than_raw_json() {
        // KV repeats field names per row, so it loses to CSV on bytes —
        // but it should still beat naïve JSON (quotes + braces + commas).
        let items: Vec<Value> = (0..50)
            .map(|i| {
                json!({
                    "id": i,
                    "name": format!("user_{i}"),
                    "email": format!("user_{i}@example.com"),
                    "status": if i % 3 == 0 { "ok" } else { "pending" },
                })
            })
            .collect();
        let c = compact(&items, &cfg());
        let kv_out = MarkdownKvFormatter::new().format(&c);
        let raw_json = serde_json::to_string(&Value::Array(items.clone())).unwrap();
        assert!(
            kv_out.len() < raw_json.len(),
            "kv {} bytes vs raw json {} bytes",
            kv_out.len(),
            raw_json.len()
        );
    }
}
