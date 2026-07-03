//! §4.2 R2 property test: the typed [`DroppedRef`] carrier covers the
//! text scrape on every fixture — the adversarial proof the Python
//! scrape can be retired against.
//!
//! For each fixture the rendered output is scraped exactly the way the
//! Python mirror scrapes it (comma-shape opaque markers, space-shape
//! row-drop markers, `#rows` index keys) and compared against the typed
//! sets carried on `CrushResult::dropped`. The retirement-safety
//! property is DIRECTIONAL:
//!
//! 1. `scraped ∖ planted  ⊆  collected` — nothing the scrape would
//!    mirror is missing from the typed carrier (retiring the scrape
//!    loses nothing);
//! 2. every collected opaque hash appears in the rendered text and
//!    resolves in the store to a payload of EXACTLY `byte_size` bytes —
//!    no phantom refs;
//! 3. payloads containing LITERAL `<<ccr:...>>` text (the scrape's
//!    false-positive class) are scraped but never collected;
//! 4. row-drop hashes and `#rows` index keys match EXACTLY in both
//!    directions (those markers are never encoding-folded).
//!
//! Strict `collected == scraped` equality deliberately does NOT hold in
//! general — and that is a FINDING, not a test weakness: when a
//! compacted table's marker column shares its `<<ccr:` prefix across
//! rows, the Affix column-encoding folds the prefix into a one-line
//! `__affix:` preamble and renders each cell as only the hash middle.
//! The raw-text scrape finds NO `<<ccr:` marker in that render, so the
//! Python mirror TODAY silently fails to mirror those opaque originals
//! (they are recoverable only through the Rust store + affix-aware
//! decoding). The typed path collects them correctly — typed coverage
//! is a strict superset of the scrape. `affix_folded_markers_are_typed_
//! but_invisible_to_the_scrape` pins the discovery.

use std::collections::BTreeSet;
use std::sync::Arc;

use furl_core::ccr::{CcrStore, InMemoryCcrStore};
use furl_core::transforms::smart_crusher::compaction::{CompactionStage, DocumentCompactor};
use furl_core::transforms::smart_crusher::{DroppedRef, SmartCrusher, SmartCrusherConfig};
use serde_json::{json, Value};

// ─── Test-side scrape (mirrors furl_ctx/transforms/smart_crusher.py) ──────

/// Humanized SIZE field, mirroring `ccr::markers::humanize_bytes` (the
/// marker's lossy rendering the scrape can recover; the typed ref
/// carries the exact byte count).
fn humanize(n: usize) -> String {
    if n < 1024 {
        return format!("{n}B");
    }
    let kb = n as f64 / 1024.0;
    if kb < 1024.0 {
        return format!("{kb:.1}KB");
    }
    format!("{:.1}MB", kb / 1024.0)
}

fn is_hex(c: char) -> bool {
    c.is_ascii_digit() || ('a'..='f').contains(&c)
}

/// Scrape every OPAQUE marker `<<ccr:HASH,KIND,SIZE>>` out of rendered
/// text — the comma shape, exactly like Python's
/// `_collect_opaque_ccr_hashes_from_string` (plus KIND/SIZE capture so
/// the test can pin them against the typed ref).
fn scrape_opaque(text: &str) -> BTreeSet<(String, String, String)> {
    let mut out = BTreeSet::new();
    let mut idx = 0;
    while let Some(start) = text[idx..].find("<<ccr:") {
        let cursor = idx + start + "<<ccr:".len();
        let rest = &text[cursor..];
        let hash_len = rest.chars().take_while(|&c| is_hex(c)).count();
        if hash_len == 0 {
            idx = cursor;
            continue;
        }
        let after = &rest[hash_len..];
        // Opaque shape iff the delimiter after the hash is a comma.
        if let Some(stripped) = after.strip_prefix(',') {
            if let Some(end) = stripped.find(">>") {
                let body = &stripped[..end];
                if let Some((kind, size)) = body.split_once(',') {
                    out.insert((
                        rest[..hash_len].to_string(),
                        kind.to_string(),
                        size.to_string(),
                    ));
                }
            }
        }
        idx = cursor + hash_len;
    }
    out
}

/// Scrape row-drop hashes (`<<ccr:HASH N_rows_offloaded>>`, space shape,
/// no `#rows` suffix) out of rendered text.
fn scrape_row_drop(text: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let mut idx = 0;
    while let Some(start) = text[idx..].find("<<ccr:") {
        let cursor = idx + start + "<<ccr:".len();
        let rest = &text[cursor..];
        let hash_len = rest.chars().take_while(|&c| is_hex(c)).count();
        if hash_len == 0 {
            idx = cursor;
            continue;
        }
        let after = &rest[hash_len..];
        if after.starts_with(' ') && after.contains("_rows_offloaded>>") {
            out.insert(rest[..hash_len].to_string());
        }
        idx = cursor + hash_len;
    }
    out
}

/// Scrape granular index keys (`<<ccr:HASH#rows N_chunks>>` → `HASH#rows`).
fn scrape_index_keys(text: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let mut idx = 0;
    while let Some(start) = text[idx..].find("<<ccr:") {
        let cursor = idx + start + "<<ccr:".len();
        let rest = &text[cursor..];
        let hash_len = rest.chars().take_while(|&c| is_hex(c)).count();
        if hash_len == 0 {
            idx = cursor;
            continue;
        }
        if rest[hash_len..].starts_with("#rows ") {
            out.insert(format!("{}#rows", &rest[..hash_len]));
        }
        idx = cursor + hash_len;
    }
    out
}

// ─── Typed-side projections ───────────────────────────────────────────────

fn typed_opaque(dropped: &[DroppedRef]) -> BTreeSet<(String, String, String)> {
    dropped
        .iter()
        .filter_map(|d| match d {
            DroppedRef::Opaque {
                hash,
                kind,
                byte_size,
            } => Some((hash.clone(), kind.clone(), humanize(*byte_size))),
            DroppedRef::RowDrop { .. } => None,
        })
        .collect()
}

fn typed_row_drop(dropped: &[DroppedRef]) -> BTreeSet<String> {
    dropped
        .iter()
        .filter_map(|d| match d {
            DroppedRef::RowDrop { hash, .. } => Some(hash.clone()),
            DroppedRef::Opaque { .. } => None,
        })
        .collect()
}

fn typed_index_keys(dropped: &[DroppedRef]) -> BTreeSet<String> {
    dropped.iter().filter_map(|d| d.row_index_key()).collect()
}

// ─── Fixtures ─────────────────────────────────────────────────────────────

fn blob(seed: usize, repeats: usize) -> String {
    let base = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";
    format!("{seed}:{}", base.repeat(repeats))
}

const PLANTED: &str = "<<ccr:aaaaaaaaaaaa,base64,2.0KB>>";

/// `(name, JSON content)` pairs covering every opaque emission plane:
/// process_string live substitution, compaction-cell substitution inside
/// lossless candidates, stringified-JSON recursion, the mixed dict arm,
/// combined row-drop + opaque outputs — and the literal-marker
/// false-positive class.
fn fixtures() -> Vec<(&'static str, String)> {
    let dict_rows: Vec<Value> = (0..60)
        .map(|i| {
            json!({
                "id": format!("{i:04}"),
                "svc": if i % 2 == 0 { "api" } else { "worker" },
                "lvl": if i % 2 == 0 { "INFO" } else { "WARN" },
                "msg": format!("req {i} done"),
            })
        })
        .collect();
    vec![
        (
            "top-level-opaque-string-field",
            json!({"summary": "ok", "payload": blob(3, 8)}).to_string(),
        ),
        (
            "opaque-rows-varied-blobs",
            Value::Array(
                (0..12)
                    .map(|i| json!({"id": i, "name": format!("row{i}"), "blob": blob(i, 8)}))
                    .collect::<Vec<_>>(),
            )
            .to_string(),
        ),
        (
            "stringified-json-with-inner-blobs",
            json!({
                "payload": Value::Array(
                    (0..12)
                        .map(|i| json!({"id": i, "blob": blob(i + 100, 8)}))
                        .collect::<Vec<_>>(),
                )
                .to_string()
            })
            .to_string(),
        ),
        (
            "mixed-array-with-blob-dicts",
            Value::Array(
                (0..12)
                    .map(|i| json!({"id": i, "name": format!("r{i}"), "blob": blob(i + 200, 8)}))
                    .chain((0..6).map(|i| json!(format!("note-{i}"))))
                    .chain((0..6).map(|i| json!(i)))
                    .collect::<Vec<_>>(),
            )
            .to_string(),
        ),
        ("row-drop-only", Value::Array(dict_rows.clone()).to_string()),
        (
            "row-drop-plus-opaque-doc",
            json!({"rows": dict_rows, "payload": blob(9, 8)}).to_string(),
        ),
        (
            "literal-marker-false-positive",
            Value::Array(
                (0..40)
                    .map(|i| json!({"id": i, "note": format!("saw {PLANTED} in output")}))
                    .collect::<Vec<_>>(),
            )
            .to_string(),
        ),
        (
            "literal-marker-passthrough",
            json!({"note": format!("quoting {PLANTED} verbatim"), "n": 1}).to_string(),
        ),
    ]
}

// ─── The property ─────────────────────────────────────────────────────────

/// Assert the directional retirement-safety property over one rendered
/// output + its typed refs (see module docs for the four clauses).
fn assert_typed_covers_scrape(
    name: &str,
    rendered: &str,
    dropped: &[DroppedRef],
    store: &dyn CcrStore,
) {
    let scraped_opaque = scrape_opaque(rendered);
    let collected_opaque = typed_opaque(dropped);
    // The scrape's false positives are markers PLANTED in the payload —
    // text the engine never emitted. The typed path is immune by
    // construction.
    let planted: BTreeSet<(String, String, String)> = scraped_opaque
        .iter()
        .filter(|(h, _, _)| h == "aaaaaaaaaaaa")
        .cloned()
        .collect();
    let scraped_minus_planted: BTreeSet<_> = scraped_opaque.difference(&planted).cloned().collect();

    // (1) Retirement safety: everything the scrape would mirror is
    // covered typed.
    let scrape_only: Vec<_> = scraped_minus_planted
        .difference(&collected_opaque)
        .collect();
    assert!(
        scrape_only.is_empty(),
        "[{name}] scrape-only opaque refs would be LOST by retiring the \
         scrape: {scrape_only:?}; rendered head: {}",
        &rendered[..rendered.len().min(200)]
    );

    // (2) No phantom refs: every collected hash is visible in the
    // rendered text (verbatim marker, or the bare hash middle of an
    // encoding-folded cell — every column encoding keeps the original
    // value bytes somewhere in the render: affix middles, __dict
    // entries, head/tail splits) and resolves to exactly `byte_size`
    // bytes.
    for d in dropped {
        if let DroppedRef::Opaque {
            hash, byte_size, ..
        } = d
        {
            assert!(
                rendered.contains(hash.as_str()),
                "[{name}] collected opaque hash {hash} must be visible in \
                 the rendered output"
            );
            let payload = store
                .get(hash)
                .unwrap_or_else(|| panic!("[{name}] opaque hash {hash} must resolve"));
            assert_eq!(
                payload.len(),
                *byte_size,
                "[{name}] typed byte_size must be the exact payload length"
            );
        }
    }

    // (3) The planted literal is never collected.
    assert!(
        !collected_opaque.iter().any(|(h, _, _)| h == "aaaaaaaaaaaa"),
        "[{name}] the typed path must NOT carry the planted literal"
    );
}

#[test]
fn typed_refs_cover_scrape_on_every_fixture() {
    for (name, content) in fixtures() {
        let crusher = SmartCrusher::new(SmartCrusherConfig::default());
        let r = crusher.crush(&content, "", 1.0);
        let store = crusher.ccr_store().expect("default crusher has a store");

        assert_typed_covers_scrape(name, &r.compressed, &r.dropped, store.as_ref());

        if content.contains(PLANTED) {
            assert!(
                scrape_opaque(&r.compressed)
                    .iter()
                    .any(|(h, _, _)| h == "aaaaaaaaaaaa"),
                "[{name}] the planted literal must survive into the output \
                 for the false-positive class to be exercised"
            );
        }

        // (4) Row-drop + index parity, EXACT in both directions (those
        // marker shapes are never encoding-folded: they live in JSON
        // sentinel strings / plain sentinel lines).
        assert_eq!(
            typed_row_drop(&r.dropped),
            scrape_row_drop(&r.compressed),
            "[{name}] typed row-drop hashes must equal the scraped set"
        );
        assert_eq!(
            typed_index_keys(&r.dropped),
            scrape_index_keys(&r.compressed),
            "[{name}] typed row-index keys must equal the scraped set"
        );
    }
}

#[test]
fn verbatim_markers_scrape_equals_typed_exactly() {
    // A fixture pinned to render markers VERBATIM (each blob is an
    // object FIELD, so it flows through `process_string`'s live
    // substitution — no table render, no encoding fold): here the
    // scrape sees everything and strict equality holds — the historical
    // `collected == scraped` property, preserved where the render
    // doesn't fold.
    let content = json!({
        "first": blob(1, 8),
        "second": blob(2, 9),
        "third": blob(3, 10),
    })
    .to_string();
    let crusher = SmartCrusher::new(SmartCrusherConfig::default());
    let r = crusher.crush(&content, "", 1.0);

    let collected = typed_opaque(&r.dropped);
    assert!(
        !collected.is_empty(),
        "fixture must exercise opaque substitution; strategy={}",
        r.strategy
    );
    assert_eq!(
        collected,
        scrape_opaque(&r.compressed),
        "verbatim-marker render: typed and scraped sets must be identical"
    );
}

#[test]
fn affix_folded_markers_are_typed_but_invisible_to_the_scrape() {
    // ★ The discovery this suite pins: uniform-size blobs produce
    // same-length markers sharing prefix `<<ccr:` AND suffix
    // `,base64,523B>>`, so the CSV-schema Affix encoding folds both into
    // a `__affix:` preamble and renders each cell as the bare hash
    // middle. The raw-text scrape — the Python mirror's ONLY discovery
    // mechanism before §4.2 — finds NO opaque marker in that render:
    // those originals were silently unmirrorable. The typed walk
    // collects all of them (strictly better coverage).
    let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
    let dc = DocumentCompactor::new().with_ccr_store(Arc::clone(&store));
    let doc = json!({
        "rows": (0..12).map(|i| json!({"id": i, "blob": blob(i + 50, 8)})).collect::<Vec<_>>(),
    });

    let mut sink: Vec<DroppedRef> = Vec::new();
    let out = dc.compact_collecting(doc, &mut sink);
    let rendered = serde_json::to_string(&out).expect("walk output serializes");

    assert_eq!(sink.len(), 12, "one typed ref per substituted blob");
    assert!(
        scrape_opaque(&rendered).is_empty(),
        "the affix fold must hide every verbatim marker from the scrape \
         (if this starts failing the render stopped folding — the \
         discovery no longer reproduces): {rendered}"
    );
    // The typed refs still resolve — recovery is intact through the
    // typed path even though the scrape is blind here.
    for d in &sink {
        if let DroppedRef::Opaque {
            hash, byte_size, ..
        } = d
        {
            assert!(
                rendered.contains(hash.as_str()),
                "folded cell must still render the hash middle"
            );
            assert_eq!(store.get(hash).map(|p| p.len()), Some(*byte_size));
        }
    }
}

#[test]
fn document_compactor_collecting_refs_cover_scrape() {
    let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
    let dc = DocumentCompactor::new().with_ccr_store(Arc::clone(&store));
    let doc = json!({
        "summary": "ok",
        "payload": blob(1, 8),
        "rows": (0..12).map(|i| json!({"id": i, "blob": blob(i + 50, 8)})).collect::<Vec<_>>(),
        "quoted": format!("saw {PLANTED} in logs"),
    });

    let mut sink: Vec<DroppedRef> = Vec::new();
    let out = dc.compact_collecting(doc.clone(), &mut sink);
    let rendered = serde_json::to_string(&out).expect("walk output serializes");

    assert_typed_covers_scrape("walker-doc", &rendered, &sink, store.as_ref());
    assert!(
        sink.len() >= 13,
        "fixture must exercise both the walk_string substitution (1) and \
         the compacted-cell substitutions (12), got {}",
        sink.len()
    );
    // Wrapper equivalence: `compact` output is byte-identical (the sink
    // is pure side-output).
    let dc2 = DocumentCompactor::new().with_ccr_store(Arc::clone(&store));
    assert_eq!(
        serde_json::to_string(&dc2.compact(doc)).unwrap(),
        rendered,
        "compact() and compact_collecting() must render identically"
    );
}

#[test]
fn compaction_stage_run_refs_equal_scrape_of_render() {
    let store: Arc<dyn CcrStore> = Arc::new(InMemoryCcrStore::new());
    let stage = CompactionStage::default_csv_schema().with_ccr_store(Arc::clone(&store));
    let items: Vec<Value> = (0..12)
        .map(|i| json!({"id": i, "name": format!("row{i}"), "blob": blob(i + 300, 8)}))
        .collect();

    let (c, rendered) = stage.run(&items);
    let mut sink: Vec<DroppedRef> = Vec::new();
    c.collect_opaque_refs(&mut sink);

    assert_eq!(
        typed_opaque(&sink),
        scrape_opaque(&rendered),
        "IR-walk refs must equal the scrape of the stage's own render"
    );
    assert!(!sink.is_empty(), "fixture must produce opaque cells");
}
