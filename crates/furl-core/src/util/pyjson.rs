//! Python-`json.dumps`-parity serializers (ARCH-8).
//!
//! One writer family, exactly reproducing CPython's `json.dumps` byte
//! output for the three formatting profiles Furl depends on:
//!
//! - [`python_json_dumps_sort_keys`] — `json.dumps(v, sort_keys=True)`:
//!   `", "` / `": "` separators, sorted keys, `ensure_ascii=True`.
//!   Feeds the MD5 item hashes (`anchor_selector::compute_item_hash` /
//!   `stable_item_hash`) — a one-byte drift silently changes dedup.
//! - [`python_json_dumps`] — `json.dumps(v)`: insertion-order keys
//!   (serde_json `preserve_order`), `ensure_ascii=True`.
//! - [`python_safe_json_dumps`] / [`write_python_safe_json`] — Python
//!   `safe_json_dumps`: compact `(",", ":")` separators,
//!   `ensure_ascii=False`. This is the format `SmartCrusher` uses to
//!   re-serialize crushed output, so the Rust bytes match Python's
//!   exactly (output parity pinned by the crusher grammar tests).
//!
//! These serializers are load-bearing for BOTH canonical
//! serialization/matching in the crusher (match-back, rendering,
//! value signatures in orchestration) and the anchor-selection dedup
//! hashes. They live here — not in any one consumer — because they are
//! a cross-cutting wire-format concern; the hash functions that
//! consume them stay with their owners (`anchor_selector` for the MD5
//! item hashes, `ccr::persist` for the CCR keys, which use serde_json's
//! `canonical_array_json` instead).

use serde_json::Value;
use std::collections::BTreeSet;

/// Python json.dumps formatting flags used by the writer below.
#[derive(Clone, Copy)]
struct JsonFmt {
    /// `sort_keys=True` → alphabetical object key order.
    sort_keys: bool,
    /// Compact separators `(",", ":")`. False → Python default `(", ", ": ")`.
    compact: bool,
    /// `ensure_ascii=True` → non-ASCII becomes `\uXXXX`. False → emit UTF-8.
    ensure_ascii: bool,
}

/// Python `json.dumps(value, sort_keys=True)` — exact format parity.
///
/// Differences from `serde_json::to_string`:
/// 1. Separators: `, ` and `: ` (with spaces, not compact).
/// 2. Object keys are sorted alphabetically.
/// 3. Non-ASCII strings are escaped to `\uXXXX` (Python default
///    `ensure_ascii=True`).
/// 4. Numbers serialize the same as serde_json for finite f64; serde_json
///    refuses NaN/Inf which JSON forbids — Python's json.dumps also
///    refuses by default but `default=str` would coerce them. For
///    compute_item_hash inputs (already-parsed JSON) NaN/Inf are
///    impossible so we don't handle them here.
pub fn python_json_dumps_sort_keys(value: &Value) -> String {
    let mut out = String::new();
    write_python_json_inner(
        value,
        &mut out,
        JsonFmt {
            sort_keys: true,
            compact: false,
            ensure_ascii: true,
        },
    );
    out
}

/// Python `json.dumps(value)` — exact format parity, preserving
/// object-key insertion order (matches the JSON parser's order via
/// serde_json's `preserve_order` feature).
///
/// Bytes differ from `to_string` because of the `, ` / `: ` separators
/// and `\uXXXX` non-ASCII escapes — both Python defaults.
pub fn python_json_dumps(value: &Value) -> String {
    let mut out = String::new();
    write_python_json_inner(
        value,
        &mut out,
        JsonFmt {
            sort_keys: false,
            compact: false,
            ensure_ascii: true,
        },
    );
    out
}

/// Python `safe_json_dumps(value)` — compact separators `(",", ":")` +
/// `ensure_ascii=False`, preserving object-key insertion order. This is
/// the format `SmartCrusher._smart_crush_content` uses to re-serialize
/// crushed output, so the Rust output bytes match Python's exactly.
pub fn python_safe_json_dumps(value: &Value) -> String {
    let mut out = String::new();
    write_python_safe_json(value, &mut out);
    out
}

/// Streaming form of [`python_safe_json_dumps`]: renders `value` into
/// `out` (identical bytes). The serializer is context-free, so callers
/// can compose an array render element-wise — `[a,b,c]` equals the
/// elements rendered here joined by `,` inside brackets — without
/// materializing a temporary `Value::Array` (PERF-4).
pub(crate) fn write_python_safe_json(value: &Value, out: &mut String) {
    write_python_json_inner(
        value,
        out,
        JsonFmt {
            sort_keys: false,
            compact: true,
            ensure_ascii: false,
        },
    );
}

/// `json.dumps(value, sort_keys=True)` with top-level keys in `exclude`
/// omitted — the projection serialization behind
/// `anchor_selector::stable_item_hash`. For the TOP-LEVEL object only,
/// keys present in `exclude` are skipped; nested values use the
/// unfiltered writer so the bytes match [`python_json_dumps_sort_keys`]
/// for everything that survives. Non-objects serialize whole (no
/// top-level keys to filter).
pub(crate) fn python_json_dumps_sort_keys_filtered(
    value: &Value,
    exclude: &BTreeSet<String>,
) -> String {
    let fmt = JsonFmt {
        sort_keys: true,
        compact: false,
        ensure_ascii: true,
    };
    let mut out = String::new();
    match value {
        Value::Object(map) => {
            let item_sep = if fmt.compact { "," } else { ", " };
            let kv_sep = if fmt.compact { ":" } else { ": " };
            out.push('{');
            let mut keys: Vec<&String> = map.keys().filter(|k| !exclude.contains(*k)).collect();
            if fmt.sort_keys {
                keys.sort();
            }
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_python_json_string(key, &mut out, fmt.ensure_ascii);
                out.push_str(kv_sep);
                write_python_json_inner(&map[key.as_str()], &mut out, fmt);
            }
            out.push('}');
        }
        // Non-objects are serialized whole (no top-level keys to filter).
        other => write_python_json_inner(other, &mut out, fmt),
    }
    out
}

fn write_python_json_inner(value: &Value, out: &mut String, fmt: JsonFmt) {
    let item_sep = if fmt.compact { "," } else { ", " };
    let kv_sep = if fmt.compact { ":" } else { ": " };
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_python_json_string(s, out, fmt.ensure_ascii),
        Value::Array(arr) => {
            out.push('[');
            for (i, v) in arr.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_python_json_inner(v, out, fmt);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            if fmt.sort_keys {
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for (i, key) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push_str(item_sep);
                    }
                    write_python_json_string(key, out, fmt.ensure_ascii);
                    out.push_str(kv_sep);
                    write_python_json_inner(&map[key.as_str()], out, fmt);
                }
            } else {
                for (i, (key, val)) in map.iter().enumerate() {
                    if i > 0 {
                        out.push_str(item_sep);
                    }
                    write_python_json_string(key, out, fmt.ensure_ascii);
                    out.push_str(kv_sep);
                    write_python_json_inner(val, out, fmt);
                }
            }
            out.push('}');
        }
    }
}

/// Encode a string value Python-style.
///
/// `ensure_ascii=true`:
/// - Backslash, quote, control chars → standard escapes (`\\`, `\"`,
///   `\n`, etc.).
/// - Non-ASCII codepoints → `\uXXXX` (surrogate-paired for codepoints
///   above 0xFFFF).
///
/// `ensure_ascii=false`:
/// - Same standard escapes for backslash/quote/controls.
/// - Non-ASCII codepoints emit literal UTF-8 bytes.
fn write_python_json_string(s: &str, out: &mut String, ensure_ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{09}' => out.push_str("\\t"),
            '\u{0A}' => out.push_str("\\n"),
            '\u{0C}' => out.push_str("\\f"),
            '\u{0D}' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) <= 0x7E => out.push(c),
            c if !ensure_ascii => {
                // ensure_ascii=False: emit raw UTF-8 like Python does.
                out.push(c);
            }
            c => {
                // ensure_ascii=True: encode as \uXXXX, surrogate pair
                // for codepoints above 0xFFFF.
                let cp = c as u32;
                if cp <= 0xFFFF {
                    out.push_str(&format!("\\u{:04x}", cp));
                } else {
                    let cp = cp - 0x10000;
                    let hi = 0xD800 + (cp >> 10);
                    let lo = 0xDC00 + (cp & 0x3FF);
                    out.push_str(&format!("\\u{:04x}\\u{:04x}", hi, lo));
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // ---------- python_json_dumps_sort_keys (parity with Python) ----------

    #[test]
    fn json_dumps_basic() {
        // Python: json.dumps({"b": 1, "a": 2}, sort_keys=True) = '{"a": 2, "b": 1}'
        let v = json!({"b": 1, "a": 2});
        assert_eq!(python_json_dumps_sort_keys(&v), r#"{"a": 2, "b": 1}"#);
    }

    #[test]
    fn json_dumps_array_uses_space_separator() {
        // Python: json.dumps([1, 2, 3]) = '[1, 2, 3]'
        let v = json!([1, 2, 3]);
        assert_eq!(python_json_dumps_sort_keys(&v), "[1, 2, 3]");
    }

    #[test]
    fn json_dumps_nested_sort_keys_recursive() {
        let v = json!({"outer": {"z": 1, "a": 2}});
        assert_eq!(
            python_json_dumps_sort_keys(&v),
            r#"{"outer": {"a": 2, "z": 1}}"#
        );
    }

    #[test]
    fn json_dumps_string_escapes() {
        let v = json!({"k": "hello\nworld"});
        assert_eq!(python_json_dumps_sort_keys(&v), r#"{"k": "hello\nworld"}"#);
    }

    #[test]
    fn json_dumps_non_ascii_escaped() {
        // Python ensure_ascii=True: 'café' → '\\u00e9' for é.
        // Reference verified via: json.dumps({"k": "café"}, sort_keys=True)
        let v = json!({"k": "café"});
        assert_eq!(python_json_dumps_sort_keys(&v), "{\"k\": \"caf\\u00e9\"}");
    }

    #[test]
    fn json_dumps_emoji_uses_surrogate_pair() {
        // Codepoint U+1F600 (😀) → \\ud83d\\ude00 surrogate pair.
        // Reference: json.dumps({"k": "😀"}, sort_keys=True)
        // = '{"k": "\\ud83d\\ude00"}'
        let v = json!({"k": "😀"});
        assert_eq!(
            python_json_dumps_sort_keys(&v),
            "{\"k\": \"\\ud83d\\ude00\"}"
        );
    }

    #[test]
    fn json_dumps_null_bool() {
        let v = json!({"a": null, "b": true, "c": false});
        assert_eq!(
            python_json_dumps_sort_keys(&v),
            r#"{"a": null, "b": true, "c": false}"#
        );
    }

    // ---------- python_safe_json_dumps ----------

    #[test]
    fn safe_json_dumps_is_compact_and_utf8() {
        // Python safe_json_dumps: separators (",", ":"), ensure_ascii=False.
        let v = json!({"b": 1, "a": "café", "c": [1, 2]});
        assert_eq!(
            python_safe_json_dumps(&v),
            r#"{"b":1,"a":"café","c":[1,2]}"#
        );
    }

    #[test]
    fn write_python_safe_json_streams_identical_bytes() {
        let v = json!({"k": [true, null, "x"]});
        let mut streamed = String::new();
        write_python_safe_json(&v, &mut streamed);
        assert_eq!(streamed, python_safe_json_dumps(&v));
    }

    // ---------- python_json_dumps (insertion order) ----------

    #[test]
    fn json_dumps_preserves_insertion_order() {
        // serde_json preserve_order keeps parse order; python_json_dumps
        // must NOT sort.
        let v: Value = serde_json::from_str(r#"{"b": 1, "a": 2}"#).unwrap();
        assert_eq!(python_json_dumps(&v), r#"{"b": 1, "a": 2}"#);
    }

    // ---------- filtered projection serialization ----------

    #[test]
    fn filtered_top_level_key_is_omitted() {
        let v = json!({"a": 1, "b": "drop me", "c": 3});
        let exclude: BTreeSet<String> = ["b".to_string()].into_iter().collect();
        assert_eq!(
            python_json_dumps_sort_keys_filtered(&v, &exclude),
            r#"{"a": 1, "c": 3}"#
        );
    }

    #[test]
    fn filtered_empty_exclude_matches_unfiltered() {
        let v = json!({"a": 1, "b": {"z": 1, "a": 2}});
        let empty = BTreeSet::new();
        assert_eq!(
            python_json_dumps_sort_keys_filtered(&v, &empty),
            python_json_dumps_sort_keys(&v)
        );
    }

    #[test]
    fn filtered_nested_keys_are_not_filtered() {
        // Exclusion applies only at the top level — nested objects keep
        // the excluded key name.
        let v = json!({"ts": 1, "nested": {"ts": 2}});
        let exclude: BTreeSet<String> = ["ts".to_string()].into_iter().collect();
        assert_eq!(
            python_json_dumps_sort_keys_filtered(&v, &exclude),
            r#"{"nested": {"ts": 2}}"#
        );
    }

    #[test]
    fn filtered_non_object_ignores_exclude() {
        let v = json!([1, 2, 3]);
        let exclude: BTreeSet<String> = ["x".to_string()].into_iter().collect();
        assert_eq!(
            python_json_dumps_sort_keys_filtered(&v, &exclude),
            python_json_dumps_sort_keys(&v)
        );
    }
}
