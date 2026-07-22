//! Field-role classification for the field-aware stable-projection hash
//! (DESIGN.md Improvement 2).
//!
//! The whole-item dedup hash (`compute_item_hash`) hashes every field of a
//! row. One high-cardinality "identity" column — a per-row timestamp, UUID,
//! commit hash, or monotone counter — makes every row's hash unique, which
//! silently defeats dedup, clustering, and fill-diversity (every row looks
//! distinct even when the value-bearing content is identical).
//!
//! This module classifies each field into one of three roles, derived purely
//! from the existing [`FieldStats`] plus a small value sample (no new analysis
//! pass):
//!
//! - [`FieldRole::Constant`]   — one value across the whole array.
//! - [`FieldRole::VaryingIdentity`] — high-cardinality AND shape-matches an
//!   identity pattern (ISO-8601, UUID, hex run, monotone/sequential int, or
//!   high-entropy id-like string). These are the noise fields that force
//!   unique hashes.
//! - [`FieldRole::Content`]    — everything else (value-bearing).
//!
//! The **stable-projection hash** (`stable_item_hash`) serializes only
//! `Constant + Content` fields, excluding `VaryingIdentity`. When no field is
//! classified `VaryingIdentity` the exclude-set is empty and the stable hash
//! is byte-identical to `compute_item_hash` — so non-identity data (e.g. real
//! search results) is completely unaffected and parity is preserved.
//!
//! The separate full-item canonical hash used for the CCR retrieve key
//! (`compute_item_hash`) is **untouched** — see `crusher.rs` / `anchor_selector.rs`.

use std::collections::BTreeSet;

use serde_json::Value;

use super::analyzer::{is_iso_date, is_iso_datetime};
use super::statistics::{calculate_string_entropy, detect_sequential_pattern, is_uuid_format};
use super::types::{FieldStats, FieldType};

/// Unique-ratio at or above which a field is a *candidate* identity column.
///
/// Chosen as 0.9 (DESIGN.md): an identity/noise column is near-unique by
/// definition (timestamps, ids, hashes). 0.9 rather than 1.0 tolerates a
/// handful of collisions (e.g. two events in the same second) without
/// reclassifying the column as content. Mirrors the `unique_ratio < 0.9`
/// hard gate already used by `detect_id_field_statistically`.
pub const IDENTITY_RATIO_THRESHOLD: f64 = 0.9;

/// Fraction of the sampled string values that must shape-match an identity
/// pattern (ISO/UUID/hex) for the field to be ruled VaryingIdentity. 0.8
/// matches the existing UUID gate in `detect_id_field_statistically` — a
/// clear majority must fit the pattern, so a content column that merely
/// contains the odd hash-looking token is not misclassified.
pub const IDENTITY_SHAPE_FRACTION: f64 = 0.8;

/// Average Shannon-entropy (per the project's normalized
/// `calculate_string_entropy`) above which a high-cardinality string column
/// is treated as an opaque identity token even without a recognized shape.
/// 0.7 mirrors the entropy gate in `detect_id_field_statistically`.
pub const IDENTITY_ENTROPY_THRESHOLD: f64 = 0.7;

/// Vowel ratio below which a whitespace-free token counts as non-linguistic
/// (see [`looks_non_linguistic`]). Entropy alone can't tell a random token
/// from a short word — both score near 1.0 — so this corroborates it. Swept
/// 0.10/0.12/0.15/0.18/0.20 against a repo-filename corpus vs. random
/// hex/base62 tokens; 0.10 gave the best precision (1/96 false positives)
/// without losing much recall (285/300 random tokens still caught) — see PR
/// body. ASCII vowels only: non-Latin-script tokens still misclassify, same
/// as before this fix (not a new regression).
pub const IDENTITY_TOKEN_VOWEL_RATIO_THRESHOLD: f64 = 0.10;

/// Fraction of the sample that must look non-linguistic before the entropy
/// fallback trusts itself. Kept separate from [`IDENTITY_SHAPE_FRACTION`]
/// (same value today) since they gate unrelated evidence.
pub const IDENTITY_NONLINGUISTIC_FRACTION: f64 = 0.8;

/// Max values to sample per field when shape-matching. Bounds the cost to a
/// constant per field regardless of array size. 20 mirrors the existing
/// `values[:20]` sampling in `field_detect.rs`.
const SAMPLE_LIMIT: usize = 20;

/// The role a field plays for dedup/cluster/fill grouping.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldRole {
    /// One value across the entire array (`is_constant`). Kept in the stable
    /// projection — a constant doesn't make rows unique, and keeping it means
    /// two rows that differ only in an identity field still hash equal.
    Constant,
    /// High-cardinality + identity-shaped. EXCLUDED from the stable
    /// projection — this is the noise that defeats dedup.
    VaryingIdentity,
    /// Value-bearing field. Kept in the stable projection.
    Content,
}

/// Classify one field from its stats + a value sample.
///
/// `sample` is an order-preserving sample of the field's values (typically
/// the first [`SAMPLE_LIMIT`] across the array). It is only consulted for the
/// shape/entropy tests on high-cardinality fields.
pub fn classify_field(stats: &FieldStats, sample: &[&Value]) -> FieldRole {
    // Constant always wins — a single distinct value can never be identity
    // noise, and keeping it in the projection is what lets two
    // differ-only-in-identity rows collapse.
    if stats.is_constant {
        return FieldRole::Constant;
    }

    // Only near-unique fields can be identity noise.
    if stats.unique_ratio < IDENTITY_RATIO_THRESHOLD {
        return FieldRole::Content;
    }

    if is_identity_shaped(stats, sample) {
        FieldRole::VaryingIdentity
    } else {
        FieldRole::Content
    }
}

/// True when a high-cardinality field shape-matches an identity pattern.
fn is_identity_shaped(stats: &FieldStats, sample: &[&Value]) -> bool {
    match stats.field_type {
        FieldType::String => string_is_identity(sample),
        FieldType::Numeric => numeric_is_identity(stats, sample),
        // Objects/arrays/bools are never identity columns for hashing
        // purposes (bools can't be high-cardinality; nested containers are
        // content).
        _ => false,
    }
}

/// String identity: a clear majority of the sample matches ISO-8601,
/// ISO-date, UUID, or a long hex run; OR the sample is high-entropy
/// token-like AND non-linguistic (see [`looks_non_linguistic`]).
fn string_is_identity(sample: &[&Value]) -> bool {
    let strs: Vec<&str> = sample.iter().filter_map(|v| v.as_str()).collect();
    if strs.is_empty() {
        return false;
    }

    let shaped = strs
        .iter()
        .filter(|s| is_iso_datetime(s) || is_iso_date(s) || is_uuid_format(s) || is_hex_run(s))
        .count();
    if (shaped as f64 / strs.len() as f64) >= IDENTITY_SHAPE_FRACTION {
        return true;
    }

    // Fallback: opaque high-entropy *tokens* (random-ish ids) with no
    // recognized shape. Restricted to token-like strings — every sample
    // value must be a single whitespace-free token. This deliberately
    // EXCLUDES natural-language content (commit subjects, log messages,
    // source lines), which also scores high on normalized per-char
    // entropy but contains spaces and word structure. Without this guard
    // the entropy test misclassifies unique English text as identity and
    // wrongly strips it from the dedup projection.
    let all_tokens = strs
        .iter()
        .all(|s| !s.is_empty() && !s.chars().any(|c| c.is_whitespace()));
    if !all_tokens {
        return false;
    }
    let avg_entropy = strs
        .iter()
        .map(|s| calculate_string_entropy(s))
        .sum::<f64>()
        / strs.len() as f64;
    if avg_entropy <= IDENTITY_ENTROPY_THRESHOLD {
        return false;
    }

    // Entropy alone misreads short words as identity (see IDENTITY_TOKEN_
    // VOWEL_RATIO_THRESHOLD doc); require most of the sample to also look
    // non-linguistic.
    let non_linguistic = strs.iter().filter(|s| looks_non_linguistic(s)).count();
    (non_linguistic as f64 / strs.len() as f64) >= IDENTITY_NONLINGUISTIC_FRACTION
}

/// True when a token doesn't look like an ordinary word: it has a digit,
/// no letters at all, or a vowel ratio below
/// [`IDENTITY_TOKEN_VOWEL_RATIO_THRESHOLD`].
fn looks_non_linguistic(s: &str) -> bool {
    if s.bytes().any(|b| b.is_ascii_digit()) {
        return true;
    }
    let letters: Vec<char> = s.chars().filter(|c| c.is_alphabetic()).collect();
    if letters.is_empty() {
        return true;
    }
    let vowels = letters
        .iter()
        .filter(|c| matches!(c.to_ascii_lowercase(), 'a' | 'e' | 'i' | 'o' | 'u'))
        .count();
    (vowels as f64 / letters.len() as f64) < IDENTITY_TOKEN_VOWEL_RATIO_THRESHOLD
}

/// Numeric identity: a monotone / sequential counter (mirrors the
/// sequential-id branch of `detect_id_field_statistically`).
fn numeric_is_identity(_stats: &FieldStats, sample: &[&Value]) -> bool {
    let owned: Vec<Value> = sample.iter().map(|v| (*v).clone()).collect();
    detect_sequential_pattern(&owned, true)
}

/// A run of at least 8 hex digits (optionally `0x`-prefixed) — commit hashes,
/// object ids, request ids. 8 is the shortest length that reliably indicates
/// an opaque identifier rather than a short content token like a status code.
fn is_hex_run(s: &str) -> bool {
    let body = s
        .strip_prefix("0x")
        .or_else(|| s.strip_prefix("0X"))
        .unwrap_or(s);
    body.len() >= 8 && body.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Build the exclude-set: the names of every field classified as
/// [`FieldRole::VaryingIdentity`] across `items`.
///
/// This is the `exclude_set` threaded into the stable hash for
/// dedup/cluster/fill grouping. Derived once per array from the analysis +
/// the items (for value sampling).
pub fn compute_exclude_set(
    field_stats: &std::collections::BTreeMap<String, FieldStats>,
    items: &[Value],
) -> BTreeSet<String> {
    let mut exclude: BTreeSet<String> = BTreeSet::new();
    for (name, stats) in field_stats {
        // Fast reject before sampling: only near-unique non-constant fields
        // can be identity noise.
        if stats.is_constant || stats.unique_ratio < IDENTITY_RATIO_THRESHOLD {
            continue;
        }
        let sample = sample_field_values(name, items);
        if classify_field(stats, &sample) == FieldRole::VaryingIdentity {
            exclude.insert(name.clone());
        }
    }
    exclude
}

/// Order-preserving sample of a field's non-null values, capped at
/// [`SAMPLE_LIMIT`].
fn sample_field_values<'a>(field: &str, items: &'a [Value]) -> Vec<&'a Value> {
    let mut out: Vec<&Value> = Vec::with_capacity(SAMPLE_LIMIT);
    for item in items {
        if let Some(v) = item.as_object().and_then(|o| o.get(field)) {
            if !v.is_null() {
                out.push(v);
                if out.len() >= SAMPLE_LIMIT {
                    break;
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::collections::BTreeMap;

    fn stats(field_type: FieldType, unique_ratio: f64, is_constant: bool) -> FieldStats {
        FieldStats {
            name: "f".to_string(),
            field_type,
            count: 90,
            unique_count: if is_constant { 1 } else { 81 },
            unique_ratio,
            is_constant,
            constant_value: None,
            min_val: None,
            max_val: None,
            mean_val: None,
            variance: None,
            change_points: Vec::new(),
            avg_length: None,
            top_values: Vec::new(),
        }
    }

    fn refs(vals: &[Value]) -> Vec<&Value> {
        vals.iter().collect()
    }

    #[test]
    fn constant_field_is_constant_role() {
        let s = stats(FieldType::String, 0.011, true);
        assert_eq!(classify_field(&s, &[]), FieldRole::Constant);
    }

    #[test]
    fn low_cardinality_is_content() {
        // author column: 36/90 distinct -> ratio 0.4 -> content.
        let s = stats(FieldType::String, 0.4, false);
        let sample: Vec<Value> = (0..20)
            .map(|i| json!(format!("Author {}", i % 4)))
            .collect();
        assert_eq!(classify_field(&s, &refs(&sample)), FieldRole::Content);
    }

    #[test]
    fn iso_datetime_high_cardinality_is_identity() {
        let s = stats(FieldType::String, 1.0, false);
        let sample: Vec<Value> = (0..20)
            .map(|i| json!(format!("2026-06-12T15:01:{:02}+02:00", i)))
            .collect();
        assert_eq!(
            classify_field(&s, &refs(&sample)),
            FieldRole::VaryingIdentity
        );
    }

    #[test]
    fn uuid_high_cardinality_is_identity() {
        let s = stats(FieldType::String, 1.0, false);
        let sample: Vec<Value> = (0..20)
            .map(|i| json!(format!("550e8400-e29b-41d4-a716-44665544{:04x}", i)))
            .collect();
        assert_eq!(
            classify_field(&s, &refs(&sample)),
            FieldRole::VaryingIdentity
        );
    }

    #[test]
    fn commit_hash_hex_run_is_identity() {
        let s = stats(FieldType::String, 1.0, false);
        let sample: Vec<Value> = vec![
            json!("0795e63ede835e5398f77c72c7f0be8fdb96ab0a"),
            json!("61306fc692468047114064ef5a9c4020439384e7"),
            json!("294212b4aabbccddeeff00112233445566778899"),
        ];
        assert_eq!(
            classify_field(&s, &refs(&sample)),
            FieldRole::VaryingIdentity
        );
    }

    #[test]
    fn unique_english_subject_is_content_not_identity() {
        // A unique-per-row natural-language commit subject must NOT be
        // misclassified as identity — it is the value-bearing content.
        let s = stats(FieldType::String, 1.0, false);
        let sample: Vec<Value> = vec![
            json!("feat(crusher): unconditional CCR persist kill silent loss"),
            json!("docs(engine): Phase-2 DESIGN safe dedup field-aware hash"),
            json!("fix(amputate): guard dead lazy imports in proxy helpers"),
        ];
        assert_eq!(classify_field(&s, &refs(&sample)), FieldRole::Content);
    }

    // Real filenames from this repo: unique, no digits, entropy 0.94-1.00.
    const FILENAME_SAMPLE: [&str; 20] = [
        "utils.py",
        "main.rs",
        "index.ts",
        "router.py",
        "field_role.rs",
        "planning.rs",
        "crusher.rs",
        "walker.rs",
        "builder.rs",
        "config.rs",
        "constraints.rs",
        "observer.rs",
        "traits.rs",
        "pipeline.py",
        "registry.py",
        "compress.py",
        "retrieve.py",
        "tokenizer.py",
        "redaction.py",
        "paths.py",
    ];

    #[test]
    fn single_word_filenames_are_content_not_identity() {
        let s = stats(FieldType::String, 1.0, false);
        let sample: Vec<Value> = FILENAME_SAMPLE.iter().map(|f| json!(f)).collect();
        assert_eq!(classify_field(&s, &refs(&sample)), FieldRole::Content);
    }

    #[test]
    fn opaque_random_tokens_with_digits_are_still_identity() {
        // Regression guard: genuinely opaque tokens must still classify as
        // identity. Python random.seed(123), 12-char ascii_letters+digits
        // x20 — 90% contain a digit.
        let s = stats(FieldType::String, 1.0, false);
        let sample: Vec<Value> = [
            "drfXArg153cy",
            "IJvv2dkivJvS",
            "pka5BXf4Myea",
            "uUCg5cfQjiY6",
            "bs6BKEqE1cXt",
            "vHZEn0MOHKZ9",
            "uaz5XPGBRIOY",
            "QM41FHQAxGc2",
            "WPlU0f6FQqkv",
            "vJz4eUDyKXvb",
            "mLf1Oxa5wozI",
            "GU06dOsF9WOU",
            "oIEljICyWDca",
            "iDmbqZwRr9BO",
            "AagNeGwTffB5",
            "aYIyeISOzbuq",
            "9EcWJvAwbOOk",
            "49AeT3RjxQpV",
            "f04OdWHyItj6",
            "PX6sX1yxiIvH",
        ]
        .iter()
        .map(|t| json!(t))
        .collect();
        assert_eq!(
            classify_field(&s, &refs(&sample)),
            FieldRole::VaryingIdentity
        );
    }

    #[test]
    fn looks_non_linguistic_boundary_cases() {
        assert!(looks_non_linguistic("abc123"), "contains a digit");
        assert!(
            looks_non_linguistic("bcdfg"),
            "no vowels at all -> ratio 0.0"
        );
        assert!(!looks_non_linguistic("urgent"), "ordinary English word");
        assert!(!looks_non_linguistic("main.rs"), "ordinary filename");
        assert!(
            looks_non_linguistic("!!!"),
            "no alphabetic characters at all"
        );
        // 1/10 = 0.10 is not < 0.10; 1/11 ~= 0.0909 is.
        assert!(!looks_non_linguistic("bcdfghjkla"), "ratio at threshold");
        assert!(looks_non_linguistic("bcdfghjklma"), "ratio below threshold");
    }

    #[test]
    fn sequential_numeric_is_identity() {
        let s = stats(FieldType::Numeric, 1.0, false);
        let sample: Vec<Value> = (0..20).map(|i| json!(1000 + i)).collect();
        assert_eq!(
            classify_field(&s, &refs(&sample)),
            FieldRole::VaryingIdentity
        );
    }

    #[test]
    fn compute_exclude_set_picks_identity_fields() {
        let items: Vec<Value> = (0..90)
            .map(|i| {
                json!({
                    "commit": format!("{:040x}", (i as i64) * 0x111111111111_i64),
                    "author": format!("Author {}", i % 4),
                    "date": format!("2026-06-12T15:{:02}:00+02:00", i % 60),
                    "subject": format!("commit number {} did a unique thing here", i),
                })
            })
            .collect();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("commit".into(), stats(FieldType::String, 1.0, false));
        fs.insert("author".into(), stats(FieldType::String, 0.044, false));
        fs.insert("date".into(), stats(FieldType::String, 1.0, false));
        fs.insert("subject".into(), stats(FieldType::String, 1.0, false));

        let exclude = compute_exclude_set(&fs, &items);
        assert!(exclude.contains("commit"), "commit hash should be excluded");
        assert!(exclude.contains("date"), "iso date should be excluded");
        assert!(
            !exclude.contains("subject"),
            "unique english subject is content, not identity"
        );
        assert!(!exclude.contains("author"), "low-cardinality is content");
    }

    #[test]
    fn compute_exclude_set_keeps_near_unique_filename_column() {
        // Audit-log shape: each row names a different file, "status" is
        // constant. Regression: "file" used to be wrongly excluded, so all
        // 20 rows collapsed into one representative, hiding which file
        // each row named.
        let items: Vec<Value> = FILENAME_SAMPLE
            .iter()
            .map(|f| json!({"file": f, "status": "OK"}))
            .collect();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("file".into(), stats(FieldType::String, 1.0, false));
        fs.insert("status".into(), stats(FieldType::String, 0.05, true));

        let exclude = compute_exclude_set(&fs, &items);
        assert!(
            !exclude.contains("file"),
            "distinct filenames are content, not identity noise"
        );
    }

    #[test]
    fn empty_exclude_set_when_no_identity_fields() {
        // Search-shaped data: path/line/lines — none are identity-shaped.
        let items: Vec<Value> = (0..30)
            .map(|i| {
                json!({
                    "path": format!("furl_ctx/mod_{}.py", i % 5),
                    "line_number": i,
                    "lines": format!("def function_{}():", i),
                })
            })
            .collect();
        let mut fs: BTreeMap<String, FieldStats> = BTreeMap::new();
        fs.insert("path".into(), stats(FieldType::String, 0.16, false));
        fs.insert("lines".into(), stats(FieldType::String, 1.0, false));
        // line_number IS sequential -> identity; that's correct and fine.
        let exclude = compute_exclude_set(&fs, &items);
        assert!(!exclude.contains("path"));
        assert!(
            !exclude.contains("lines"),
            "source lines are content, not identity"
        );
    }
}
