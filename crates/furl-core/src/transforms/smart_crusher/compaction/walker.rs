//! `DocumentCompactor` — recursive walker that finds compactable spots anywhere in a JSON document and replaces
//! them in place. . The wrapping object/array structure is preserved exactly — only bulky leaves get replaced.

use std::sync::Arc;

use serde_json::{Map, Value};

use super::classifier::{classify_string, CellClass};
use super::compactor::{compact, CompactConfig};
use super::formatter::{CsvSchemaFormatter, Formatter};
use super::ir::OpaqueKind;
use crate::ccr::{marker_for_opaque, CcrStore};
use crate::transforms::smart_crusher::types::DroppedRef;

/// Walks any JSON value and applies lossless compaction in place.
pub struct DocumentCompactor {
    pub config: CompactConfig,
    pub formatter: Box<dyn Formatter>,
}

impl Default for DocumentCompactor {
    fn default() -> Self {
        Self {
            config: CompactConfig::default(),
            formatter: Box::new(CsvSchemaFormatter::new()),
        }
    }
}

impl DocumentCompactor {
    pub fn new() -> Self {
        Self::default()
    }

    /// Wire a CCR store so opaque-blob substitutions
    pub fn with_ccr_store(mut self, store: Arc<dyn CcrStore>) -> Self {
        self.config.ccr_store = Some(store);
        self
    }

    /// Walk and compact. Wrapper that discards the typed refs — callers that mirror recovery should
    /// use [`Self::compact_collecting`] instead of re-parsing markers out of the returned value.
    pub fn compact(&self, doc: Value) -> Value {
        let mut sink: Vec<DroppedRef> = Vec::new();
        self.compact_collecting(doc, &mut sink)
    }

    /// `compact_collecting` returns the same value as `compact` while appending shipped opaque
    /// substitutions to `sink` as typed recovery refs. The side-output never changes rendering.
    pub fn compact_collecting(&self, doc: Value, sink: &mut Vec<DroppedRef>) -> Value {
        walk(doc, self, sink)
    }
}

fn walk(v: Value, ctx: &DocumentCompactor, sink: &mut Vec<DroppedRef>) -> Value {
    match v {
        Value::Object(map) => walk_object(map, ctx, sink),
        Value::Array(items) => walk_array(items, ctx, sink),
        Value::String(s) => walk_string(s, ctx, sink),
        scalar => scalar,
    }
}

fn walk_object(
    map: Map<String, Value>,
    ctx: &DocumentCompactor,
    sink: &mut Vec<DroppedRef>,
) -> Value {
    Value::Object(
        map.into_iter()
            .map(|(k, v)| (k, walk(v, ctx, sink)))
            .collect(),
    )
}

fn walk_array(items: Vec<Value>, ctx: &DocumentCompactor, sink: &mut Vec<DroppedRef>) -> Value {
    // Recurse into items FIRST so inner sub-tables / opaque markers are already in their compacted form when the outer compact runs.
    let inner: Vec<Value> = items.into_iter().map(|i| walk(i, ctx, sink)).collect();

    // Then try the array as a whole.
    let c = compact(&inner, &ctx.config);
    if c.was_compacted() {
        // This render SHIPS (it replaces the array) — surface every opaque cell it baked in, typed.
        c.collect_opaque_refs(sink);
        Value::String(ctx.formatter.format(&c))
    } else {
        Value::Array(inner)
    }
}

fn walk_string(s: String, ctx: &DocumentCompactor, sink: &mut Vec<DroppedRef>) -> Value {
    // Stringified-JSON: parse, recurse, replace.
    if let Some(parsed) = try_parse_json_container(&s) {
        // Recurse into a LOCAL sink: the no-op guard below may discard the recursed value, and refs must be surfaced
        // only for substitutions that actually SHIP (a discarded recursion's markers appear nowhere in the output).
        let mut local: Vec<DroppedRef> = Vec::new();
        let recursed = walk(parsed.clone(), ctx, &mut local);
        // No-op guard (COR-45) Re-emitting through serde would silently minify Python `json.dumps`-spaced JSON,
        // collapse duplicate keys ({"a":1,"a":2} → {"a":2}), decode \uXXXX escapes and rewrite exponent forms
        if recursed == parsed {
            return Value::String(s);
        }
        sink.extend(local);
        return match recursed {
            // Sub-table won — already a rendered string.
            Value::String(rendered) => Value::String(rendered),
            // Sub-recursion compacted something deeper; re-emit.
            other => Value::String(serde_json::to_string(&other).unwrap_or(s)),
        };
    }

    // Long opaque blob: substitute with CCR marker (and stash the original in the store if one is configured, so retrieval works).
    if let CellClass::Opaque(kind) = classify_string(&s, &ctx.config.classify) {
        let (marker, dropped_ref) =
            emit_opaque_ccr_marker(&s, &kind, ctx.config.ccr_store.as_ref());
        sink.push(dropped_ref);
        return Value::String(marker);
    }

    Value::String(s)
}

/// Guard against serde_json internal magic keys (COR-44). Any parse entry point that calls `serde_json::from_str` on adversarial or
/// tool-echoed content containing these markers would silently receive a mutated `Value` — shipping altered data and poisoning CCR recovery.
pub fn has_serde_private_marker(s: &str) -> bool {
    s.contains("$serde_json::private::")
}

/// Parse a string as JSON IF it looks like a container (starts with `{` or `[`) AND parses cleanly to Object/Array. Declines
/// (returns `None`) when the input contains a serde_json internal magic key — see [`has_serde_private_marker`] (COR-44).
pub fn try_parse_json_container(s: &str) -> Option<Value> {
    // COR-44: decline before calling from_str so the magic-key promotion never fires.
    if has_serde_private_marker(s) {
        return None;
    }
    let trimmed = s.trim_start();
    if !matches!(trimmed.chars().next(), Some('{') | Some('[')) {
        return None;
    }
    serde_json::from_str::<Value>(s)
        .ok()
        .filter(|v| matches!(v, Value::Object(_) | Value::Array(_)))
}

/// Emit an opaque-blob CCR marker AND (optionally) stash the original in the store so retrieval works. The hash is computed
/// identically regardless of store presence — same input → same marker — so the runtime contract is stable across configurations.
pub fn emit_opaque_ccr_marker(
    payload: &str,
    kind: &OpaqueKind,
    store: Option<&Arc<dyn CcrStore>>,
) -> (String, DroppedRef) {
    // 24-hex (96-bit) SHA-256 prefix via the consolidated `ccr::persist`
    // implementation (ARCH-5) — same key with or without a store.
    let hash = crate::ccr::persist::sha256_recovery_key(payload.as_bytes());
    if let Some(s) = store {
        s.put(&hash, payload);
    }
    let marker = marker_for_opaque(&hash, kind.wire_str(), payload.len());
    let dropped_ref = DroppedRef::Opaque {
        hash,
        kind: kind.wire_str().to_string(),
        byte_size: payload.len(),
    };
    (marker, dropped_ref)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn dc() -> DocumentCompactor {
        DocumentCompactor::new()
    }

    #[test]
    fn top_level_array_of_objects_is_compacted() {
        let doc = json!([
            {"id": 1, "name": "alice"},
            {"id": 2, "name": "bob"},
            {"id": 3, "name": "carol"},
        ]);
        let out = dc().compact(doc);
        match out {
            Value::String(s) => {
                assert!(s.starts_with("[3]{"), "got: {s}");
                assert!(s.contains("name:string"));
            }
            other => panic!("expected String, got {other:?}"),
        }
    }

    #[test]
    fn nested_array_in_object_field_is_compacted_in_place() {
        let doc = json!({
            "user": "alice",
            "events": [
                {"id": 1, "action": "click"},
                {"id": 2, "action": "hover"},
                {"id": 3, "action": "submit"},
            ],
        });
        let out = dc().compact(doc);
        let obj = out.as_object().expect("object preserved");
        assert_eq!(obj.get("user").and_then(|v| v.as_str()), Some("alice"));
        let events = obj.get("events").and_then(|v| v.as_str()).expect("string");
        assert!(events.starts_with("[3]{"), "got: {events}");
    }

    #[test]
    fn deeply_nested_arrays_compact_at_every_level() {
        let doc = json!({
            "outer": {
                "middle": {
                    "rows": [
                        {"a": 1, "b": "x"},
                        {"a": 2, "b": "y"},
                    ],
                },
            },
        });
        let out = dc().compact(doc);
        let inner = out
            .pointer("/outer/middle/rows")
            .and_then(|v| v.as_str())
            .expect("rows compacted to string");
        assert!(inner.starts_with("[2]{"), "got: {inner}");
    }

    #[test]
    fn stringified_json_in_field_is_parsed_and_compacted() {
        let inner = r#"[{"x":1},{"x":2},{"x":3}]"#;
        let doc = json!({
            "id": "abc",
            "payload": inner,
        });
        let out = dc().compact(doc);
        let payload = out
            .pointer("/payload")
            .and_then(|v| v.as_str())
            .expect("payload compacted");
        assert!(payload.starts_with("[3]{"), "got: {payload}");
    }

    #[test]
    fn long_opaque_string_at_top_level_becomes_ccr_marker() {
        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let out = dc().compact(Value::String(big));
        match out {
            Value::String(s) => assert!(
                s.starts_with("<<ccr:") && s.contains(",base64,"),
                "got: {s}"
            ),
            other => panic!("expected String, got {other:?}"),
        }
    }

    #[test]
    fn long_opaque_string_inside_object_field_becomes_ccr_marker() {
        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let doc = json!({"id": 1, "blob": big});
        let out = dc().compact(doc);
        let blob = out.pointer("/blob").and_then(|v| v.as_str()).unwrap();
        assert!(blob.starts_with("<<ccr:"), "got: {blob}");
    }

    #[test]
    fn pure_scalar_object_unchanged() {
        let doc = json!({"a": 1, "b": "short", "c": true, "d": null});
        let out = dc().compact(doc.clone());
        assert_eq!(out, doc);
    }

    #[test]
    fn mixed_doc_only_compactable_parts_change() {
        let doc = json!({
            "user_id": 42,
            "tag": "active",
            "events": [
                {"id": 1, "kind": "x"},
                {"id": 2, "kind": "y"},
            ],
            "config": {"region": "us", "tier": "gold"},
        });
        let out = dc().compact(doc);
        // user_id and tag preserved as scalars.
        assert_eq!(out.pointer("/user_id"), Some(&json!(42)));
        assert_eq!(out.pointer("/tag"), Some(&json!("active")));
        // config preserved as object (not an array, can't tabulate).
        assert!(out
            .pointer("/config")
            .map(|v| v.is_object())
            .unwrap_or(false));
        // events compacted to a string.
        assert!(out
            .pointer("/events")
            .and_then(|v| v.as_str())
            .unwrap()
            .starts_with("[2]{"));
    }

    #[test]
    fn cascading_recursion_outer_table_sees_inner_compacted_string() {
        // Each row has a stringified-JSON `payload`. After the walker recurses into items, each payload is a rendered sub-table string.
        let doc = json!([
            {"id": 1, "payload": r#"[{"x":1},{"x":2},{"x":3}]"#},
            {"id": 2, "payload": r#"[{"x":4},{"x":5}]"#},
        ]);
        let out = dc().compact(doc);
        match out {
            Value::String(s) => {
                assert!(s.starts_with("[2]{"), "outer table: {s}");
                // The inner-rendered sub-tables show up CSV-quoted in
                // the payload column.
                assert!(s.contains("[3]{") || s.contains("\"[3]{"));
            }
            other => panic!("expected String, got {other:?}"),
        }
    }

    #[test]
    fn array_of_scalars_left_alone() {
        // Compactor declines non-object arrays → walker returns the
        // recursed array unchanged.
        let doc = json!([1, 2, 3, "four", 5.0]);
        let out = dc().compact(doc.clone());
        assert_eq!(out, doc);
    }

    #[test]
    fn empty_object_unchanged() {
        let doc = json!({});
        assert_eq!(dc().compact(doc.clone()), doc);
    }

    #[test]
    fn empty_array_unchanged() {
        let doc = json!([]);
        assert_eq!(dc().compact(doc.clone()), doc);
    }

    #[test]
    fn malformed_stringified_json_left_alone() {
        let doc = json!({"payload": "{not valid json"});
        let out = dc().compact(doc.clone());
        assert_eq!(out, doc);
    }

    // ── COR-45: no-op recursion must not rewrite the string leaf ──

    #[test]
    fn noop_stringified_json_leaf_survives_byte_identical() {
        // A string leaf holding Python `json.dumps`-spaced JSON that the recursion cannot compact (below every threshold) must come back BYTE-IDENTICAL.
        let leaf = r#"{"a": 1, "b": "x"}"#;
        let doc = json!({"payload": leaf});
        let out = dc().compact(doc);
        assert_eq!(
            out.pointer("/payload").and_then(|v| v.as_str()),
            Some(leaf),
            "a no-op walk must return the original string verbatim"
        );
    }

    #[test]
    fn noop_guard_preserves_duplicate_keys_escapes_and_exponents() {
        // COR-45's worst cases: serde re-serialization collapses duplicate keys ({"a":1,"a":2} → {"a":2}), decodes
        // \uXXXX escapes, and rewrites exponent forms — all silent data mutation when the walk compacted nothing.
        for leaf in [
            r#"{"a":1,"a":2}"#,     // duplicate keys must not collapse
            r#"{"s":"caf\u00e9"}"#, // \uXXXX escapes must not decode
            r#"{"n":1e5}"#,         // exponent form must not rewrite
            r#"[1, 2, 3]"#,         // json.dumps-spaced scalar array
        ] {
            let doc = json!({"payload": leaf});
            let out = dc().compact(doc);
            assert_eq!(
                out.pointer("/payload").and_then(|v| v.as_str()),
                Some(leaf),
                "no-op leaf must survive byte-identical: {leaf}"
            );
        }
    }

    // ── COR-44: magic-key guard in try_parse_json_container + walk_string ──

    #[test]
    fn try_parse_json_container_declines_serde_number_magic_key() {
        // COR-44: with arbitrary_precision enabled, serde_json treats {"$serde_json::private::Number":"123"} as the number literal 123.
        // The guard must return None before calling from_str so the promotion never fires — the string leaf passes through unchanged.
        let magic = r#"{"$serde_json::private::Number":"123"}"#;
        assert!(
            try_parse_json_container(magic).is_none(),
            "magic-key payload must be declined by try_parse_json_container"
        );
    }

    #[test]
    fn walk_string_magic_key_leaf_returned_byte_identical() {
        // COR-44: when a string leaf contains a serde_json magic key, the walker must return the original
        // string byte-identical rather than parsing (which would silently mutate it via the promotion).
        let magic = r#"{"$serde_json::private::Number":"456"}"#;
        let doc = json!({"payload": magic});
        let out = dc().compact(doc);
        assert_eq!(
            out.pointer("/payload").and_then(|v| v.as_str()),
            Some(magic),
            "magic-key string leaf must come back byte-identical"
        );
    }

    #[test]
    fn try_parse_json_container_declines_raw_value_magic_key() {
        // COR-44: the RawValue variant of the magic key.
        let magic = r#"{"$serde_json::private::RawValue":"true"}"#;
        assert!(
            try_parse_json_container(magic).is_none(),
            "raw_value magic-key payload must be declined by try_parse_json_container"
        );
    }

    // ── COR-19: with_ccr_store wires the SINGLE config.ccr_store field ──

    #[test]
    fn with_ccr_store_populates_config_store_for_the_array_compaction_path() {
        // Any opaque cell reaching that path emitted a `<<ccr:HASH,...>>` marker with NO stored original (a dangling marker = silent loss).
        // Pre-fix `config.ccr_store` was `None`, so the blob was never stored and `get(hash)` MISSES; post-fix it resolves byte-exact.
        use crate::ccr::InMemoryCcrStore;
        use crate::transforms::smart_crusher::compaction::compactor::compact;
        use std::sync::Arc;

        let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
        let dc = DocumentCompactor::new().with_ccr_store(Arc::clone(&store));

        // A long base64-ish blob classifies Opaque in a table cell.
        let big = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=".repeat(8);
        let rows = vec![
            json!({"id": 1, "blob": big.clone()}),
            json!({"id": 2, "blob": big.clone()}),
        ];

        // The array-compaction path the fix wires: `compact` reads
        // `config.ccr_store` inside `cell_from_value`'s opaque branch.
        let c = compact(&rows, &dc.config);
        assert!(c.was_compacted(), "the two-row table must compact");
        let rendered = dc.formatter.format(&c);
        assert!(
            rendered.contains("<<ccr:"),
            "the opaque blob must render as a CCR marker: {rendered}"
        );

        // The store must now hold the original under the marker hash — the whole point of COR-19. Pull the hash from the marker and recover byte-exact.
        let start = rendered.find("<<ccr:").expect("marker present") + "<<ccr:".len();
        let rest = &rendered[start..];
        let end = rest.find(',').expect("opaque marker has a comma");
        let hash = &rest[..end];
        let recovered = store
            .get(hash)
            .expect("COR-19: config.ccr_store must hold the opaque original (dangling marker bug)");
        assert_eq!(recovered, big, "recovered blob must be byte-exact");
    }
}
