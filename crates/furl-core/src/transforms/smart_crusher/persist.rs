//! CCR persistence `persist_dropped`, the `DroppedPersist`/`CcrWrite` plumbing, the CCR-Dropped sentinel builders, and `hash_canonical` (ARCH-4 The invariant-bearing
//! module whenever rows are dropped the full original is hashed, persisted per [`PersistMode`], and surfaced behind a `<<ccr:HASH>>` recovery pointer

use std::sync::Arc;

use serde_json::Value;

use super::crusher::SmartCrusher;
use super::types::DroppedRef;
use crate::ccr::{marker_for_rows_offloaded, CcrStore};

/// Build the `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>"}` sentinel object. `_ccr_dropped` carries the byte-stable whole-blob recovery pointer a bare `ccr_get(HASH)` returns the full
/// offloaded array, and the MCP `furl_retrieve` surface narrows it with `query=` / `select_field=`. surfacing it costs ~no prompt tokens and does not perturb the lossy-vs-lossless token routing.
pub(super) fn ccr_sentinel_map(dropped_summary: &str) -> serde_json::Map<String, Value> {
    let mut sentinel = serde_json::Map::new();
    sentinel.insert(
        "_ccr_dropped".to_string(),
        Value::String(dropped_summary.to_string()),
    );
    sentinel
}

/// Build the full sentinel `Value::Object` from a [`DroppedPersist`].
fn build_ccr_sentinel(persisted: &DroppedPersist) -> Value {
    Value::Object(ccr_sentinel_map(&persisted.marker))
}

/// One deferred CCR store write (`key` → `payload`).
pub(super) struct CcrWrite {
    key: String,
    payload: String,
}

/// How [`SmartCrusher::persist_dropped`] treats the CCR store writes for a drop.
#[derive(Clone, Copy)]
pub(super) enum PersistMode {
    /// Write through to the configured store immediately. For paths whose render ALWAYS ships (the
    /// non-dict string/number/mixed sentinel path) — there is no later routing decision to wait for.
    Commit,
    /// `Collect` returns pending CCR writes and commits them only if the lossy render
    /// ships. This prevents orphan store entries when lossless arbitration wins.
    Collect,
    /// Never write (COR-28, mixed dict arm): the caller surfaces no
    /// marker naming this hash, so any write would be an orphan.
    Skip,
}

/// `DroppedPersist` carries the full-original hash, recovery pointer, and optional pending
/// writes. `Commit` is already persisted; `Collect` must commit only if the render ships.
pub(super) struct DroppedPersist {
    /// 24-hex (96-bit) SHA-256 prefix of the canonical full-original array. Always returned when something was dropped — callers may mirror or retrieve it.
    pub(super) hash: String,
    /// `<<ccr:HASH N_rows_offloaded>>` recovery pointer. ALWAYS non-empty when rows were dropped (Defect
    /// 1): the pointer is the recovery key, not a UX flag, so it is surfaced unconditionally on every drop.
    pub(super) marker: String,
    /// Deferred store writes, populated ONLY under [`PersistMode::Collect`] (empty under `Commit` — already
    /// written — and `Skip` — never written). A row-drop defers exactly one write: the whole-blob original.
    pub(super) pending_writes: Vec<CcrWrite>,
}

impl DroppedPersist {
    /// The typed carrier for this drop (§4.2) — the exact values the
    /// rendered sentinel advertises, surfaced for direct mirroring.
    pub(super) fn dropped_ref(&self) -> DroppedRef {
        DroppedRef::RowDrop {
            hash: self.hash.clone(),
        }
    }
}

impl SmartCrusher {
    /// Replay deferred CCR writes captured under [`PersistMode::Collect`], in capture order — the same order `persist_dropped` writes them directly.
    pub(super) fn commit_ccr_writes(&self, pending: Vec<CcrWrite>) {
        if pending.is_empty() {
            return;
        }
        if let Some(store) = &self.ccr_store {
            for write in &pending {
                store.put(&write.key, &write.payload);
            }
        }
    }

    /// When rows are dropped, hash canonical full input and surface a CCR recovery marker. `Commit` writes
    /// immediately, `Collect` defers until the candidate ships, and `Skip` must never surface an unbacked marker.
    pub(super) fn persist_dropped(
        &self,
        original_items: &[Value],
        dropped_count: usize,
        mode: PersistMode,
    ) -> Option<DroppedPersist> {
        if dropped_count == 0 {
            return None;
        }

        // Deferred-write sink (populated only under `Collect`). Total over `PersistMode` so a new mode is a compile error here, not a silent write-nothing.
        let mut pending_writes: Vec<CcrWrite> = Vec::new();
        fn route_write(
            mode: PersistMode,
            store: &Arc<dyn CcrStore>,
            pending: &mut Vec<CcrWrite>,
            key: &str,
            payload: String,
        ) {
            match mode {
                PersistMode::Commit => store.put(key, &payload),
                PersistMode::Collect => pending.push(CcrWrite {
                    key: key.to_string(),
                    payload,
                }),
                PersistMode::Skip => {}
            }
        }

        // Serialize the original array exactly ONCE. The hash is taken over those bytes, and (if
        // a store is configured) the same bytes get stored — no redundant clone or re-serialize.
        let canonical = canonical_array_json(original_items);
        let hash = hash_canonical(&canonical);

        // Whole-blob persist (1A) ── A row-drop persists exactly ONE store entry the full original array under `hash`, the byte-stable recovery key the invariant
        // + parity depend on. a bare `ccr_get(hash)` returns the full array, and the MCP `furl_retrieve` surface narrows it with `query=` / `select_field=`.
        if let Some(store) = &self.ccr_store {
            route_write(mode, store, &mut pending_writes, &hash, canonical);
        }

        // Unconditional recovery pointer (Defect 1) ── The `<<ccr:HASH N_rows_offloaded>>` marker is the RECOVERY KEY, not a
        // UX nicety it is the only way a consumer holding just the output can name the hash and pull the dropped rows back.
        let marker = marker_for_rows_offloaded(&hash, dropped_count);

        Some(DroppedPersist {
            hash,
            marker,
            pending_writes,
        })
    }

    /// Build the `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>"}` sentinel object for a non-dict array drop, persisting the full original via [`SmartCrusher::persist_dropped`]
    /// first. Returns `None` ONLY when nothing was dropped. so the output always carries a `<<ccr:HASH>>` the consumer can resolve via `ccr_get(hash)` never a silent drop.
    pub(super) fn ccr_dropped_sentinel_collecting(
        &self,
        original_items: &[Value],
        kept_items: &[Value],
        dropped: &mut Vec<DroppedRef>,
    ) -> Option<Value> {
        let dropped_count = original_items.len().saturating_sub(kept_items.len());
        // Commit mode: the non-dict paths never arbitrate against a lossless candidate
        // — this render always ships, so the write happens now (no deferral needed).
        let persisted = self.persist_dropped(original_items, dropped_count, PersistMode::Commit)?;
        dropped.push(persisted.dropped_ref());
        Some(build_ccr_sentinel(&persisted))
    }
}

/// Serialize the value slice directly to canonical array JSON. It is byte-equal to
/// serializing a cloned `Value::Array` while avoiding a wrapper allocation and deep clone.
pub(super) fn canonical_array_json(items: &[Value]) -> String {
    serde_json::to_string(items).unwrap_or_default()
}

/// 24-hex (96-bit) SHA-256 prefix of an already-serialized canonical JSON string. Caller is responsible for producing the canonical form
/// via [`canonical_array_json`] (or another byte-equal serializer) — the hash is over the bytes, so a stable serializer is the contract.
fn hash_canonical(canonical: &str) -> String {
    crate::ccr::persist::sha256_recovery_key(canonical.as_bytes())
}

// `hash_array_for_ccr` (a test-only `canonical_array_json + hash_canonical` convenience) lived here but had no callers — clippy flagged it as dead code.

#[cfg(test)]
mod tests {
    use super::super::builder::SmartCrusherBuilder;
    use super::super::config::SmartCrusherConfig;
    use super::super::crusher::test_support::crusher_with_store;
    use super::*;
    use serde_json::json;

    #[test]
    fn persist_dropped_hash_is_byte_identical_to_inline_dict_scheme() {
        // The shared helper must produce the SAME hash the dict path produced inline before
        // the refactor: SHA-256(canonical) → 12 hex chars over `canonical_array_json(items)`.
        let (c, _store) = crusher_with_store();
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i})).collect();
        let persisted = c
            .persist_dropped(&items, 5, PersistMode::Commit)
            .expect("dropped_count>0 → Some");
        let expected = hash_canonical(&canonical_array_json(&items));
        assert_eq!(persisted.hash, expected, "hash scheme must be unchanged");
        assert_eq!(persisted.hash.len(), 24);
        assert!(persisted
            .marker
            .contains(&format!("<<ccr:{expected} 5_rows_offloaded>>")));
        // Zero dropped → None (no hash, no marker, no store write).
        assert!(c.persist_dropped(&items, 0, PersistMode::Commit).is_none());
    }

    // ---------- A row-drop persists exactly one entry: the whole-blob ----------

    #[test]
    fn persist_dropped_writes_only_the_whole_blob() {
        // Design A / #168: per-row chunks are byte-duplicates of rows the whole-blob already
        // holds — storing one entry per dropped row floods the bounded FIFO store (the F8 defect).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let persisted = c
            .persist_dropped(&items, 3, PersistMode::Commit)
            .expect("drop → Some");

        assert_eq!(store.len(), 1, "a drop persists the whole-blob ONLY");
        let payload = store.get(&persisted.hash).expect("whole-blob resolves");
        assert_eq!(payload, canonical_array_json(&items));

        // No `{hash}#rows` index and no per-row chunk entries exist.
        assert!(
            store.get(&format!("{}#rows", persisted.hash)).is_none(),
            "no granular row index is written"
        );
        for row in &items {
            let row_hash = hash_canonical(&canonical_array_json(std::slice::from_ref(row)));
            assert!(
                store.get(&row_hash).is_none(),
                "no per-row chunk is written for {row}"
            );
        }
    }

    #[test]
    fn persist_dropped_skip_mode_is_marker_identical_and_writes_nothing() {
        // COR-28 persist-skip contract: `PersistMode::Skip` must return a BYTE-IDENTICAL DroppedPersist (hash + whole-blob marker — the inputs
        // to MinTokens routing) while writing NOTHING to the store (and deferring nothing — there is no later commit for the mixed arm).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();

        let skipped = c
            .persist_dropped(&items, 3, PersistMode::Skip)
            .expect("drop → Some regardless of persist mode");
        assert_eq!(store.len(), 0, "Skip mode must write nothing");
        assert!(
            skipped.pending_writes.is_empty(),
            "Skip mode must defer nothing either (no later commit exists)"
        );

        let persisted = c
            .persist_dropped(&items, 3, PersistMode::Commit)
            .expect("drop → Some");
        assert_eq!(skipped.hash, persisted.hash);
        assert_eq!(skipped.marker, persisted.marker);
        assert!(
            persisted.pending_writes.is_empty(),
            "Commit mode writes through — nothing rides back deferred"
        );
        assert_eq!(store.len(), 1, "Commit mode writes the whole-blob");
    }

    #[test]
    fn persist_dropped_collect_mode_defers_writes_and_replay_matches_commit() {
        // P0-4 Collect contract: hash + marker byte-identical to Commit (routing built on them cannot shift), NOTHING written until
        // the caller commits — and replaying the deferred write reproduces Commit-mode store state exactly (the one whole-blob entry).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();

        let collected = c
            .persist_dropped(&items, 3, PersistMode::Collect)
            .expect("drop → Some");
        assert_eq!(store.len(), 0, "Collect must write nothing before commit");
        assert_eq!(
            collected.pending_writes.len(),
            1,
            "the whole-blob rides back deferred"
        );

        let (c2, store2) = crusher_with_store();
        let committed = c2
            .persist_dropped(&items, 3, PersistMode::Commit)
            .expect("drop → Some");
        assert_eq!(collected.hash, committed.hash, "hash is mode-independent");
        assert_eq!(collected.marker, committed.marker);
        assert_eq!(
            collected.pending_writes.last().map(|w| w.key.as_str()),
            Some(committed.hash.as_str()),
            "the deferred write is the whole-blob"
        );

        // Replaying the deferred write reproduces Commit-mode state.
        c.commit_ccr_writes(collected.pending_writes);
        assert_eq!(store.len(), store2.len());
        assert_eq!(store.len(), 1, "the whole-blob entry exactly");
        assert_eq!(
            store.get(&committed.hash),
            store2.get(&committed.hash),
            "whole-blob payload identical across modes"
        );
    }

    #[test]
    fn hash_canonical_pinned_vectors() {
        // Rule-2 pin (anti parallel-mutation blindness): fixed SHA-256[:24] literals over the EXACT canonical bytes `serde_json::to_string` emits. Literals produced
        // once in Python and pinned identically there; together the tests lock the Py↔Rust CCR recovery key.
        assert_eq!(hash_canonical("[]"), "4f53cda18c2baa0c0354bb5f");
        assert_eq!(
            hash_canonical(r#"["alpha","beta","gamma"]"#),
            "a3e185260009ab5be7bb16f3"
        );
        assert_eq!(hash_canonical("[1,2,3,4,5]"), "f5baf0e4336fd53b4c82b453");
        assert_eq!(
            hash_canonical(r#"[{"id":1},{"id":2},{"id":3}]"#),
            "d99179347cb13877fc9057e0"
        );
        // The canonical serializer must emit EXACTLY those bytes, so each literal above is the hash of
        // what the array drop path actually hashes (closes the loop: a serializer change would flip this).
        assert_eq!(canonical_array_json(&[]), "[]");
        assert_eq!(
            canonical_array_json(&[json!("alpha"), json!("beta"), json!("gamma")]),
            r#"["alpha","beta","gamma"]"#
        );
        assert_eq!(
            canonical_array_json(&[json!({"id": 1}), json!({"id": 2}), json!({"id": 3})]),
            r#"[{"id":1},{"id":2},{"id":3}]"#
        );
    }

    #[test]
    fn hash_canonical_wire_form_pinned_vectors() {
        // TEST-33: the WIRE-FORM half of the Py↔Rust parity lock. This canonical is LITERAL-PRESERVING for decimal tokens (serde_json `arbitrary_precision`: `1.50` stays `1.50`, digits
        // beyond f64 precision survive) and normalizes ONLY the exponent spelling (`1E5` → `1e+5`: lowercase `e`, explicit sign — serde's number scanner, not a float round-trip).
        let wire_vectors: [(&str, &str, &str); 6] = [
            // (wire input, serde canonical, pinned SHA-256[:24])
            (
                r#"[{"price":1.50}]"#,
                r#"[{"price":1.50}]"#,
                "86cf954ca9f301c4cf6f9832", // trailing zero preserved verbatim
            ),
            ("[1E5]", "[1e+5]", "5c20cc153829a59a47596031"), // exponent spelling normalized
            ("[1e400]", "[1e+400]", "7e9854d86909950904d96294"), // overflows f64; token kept
            (
                "[2.5000000000000000000000000001]",
                "[2.5000000000000000000000000001]",
                "44a8948fa037883453d1adec", // beyond f64 precision, preserved verbatim
            ),
            // Non-ASCII and control-char forms — these AGREE with the Python reference (same hashes pinned in
            // its `_VECTORS`); included so the lock covers the full canonical grammar, not just ASCII scalars.
            (
                r#"["café","日本語","naïve"]"#,
                r#"["café","日本語","naïve"]"#,
                "3a6991f2cdbff9637f9d8ec2",
            ),
            (
                r#"["line1\nline2","tab\there","bell\u0007"]"#,
                r#"["line1\nline2","tab\there","bell\u0007"]"#,
                "333b058285a5aa142b93c6bd",
            ),
        ];
        for (input, canonical, expected) in wire_vectors {
            // The production path hashes `canonical_array_json(parsed rows)` — pin that parsing the wire text
            // re-serializes to EXACTLY the canonical bytes above (`preserve_order` + `arbitrary_precision`).
            let items: Vec<Value> =
                serde_json::from_str(input).expect("wire vector must parse as a JSON array");
            assert_eq!(
                canonical_array_json(&items),
                canonical,
                "canonical for wire input {input:?}"
            );
            assert_eq!(
                hash_canonical(canonical),
                expected,
                "hash for {canonical:?}"
            );
        }
    }

    #[test]
    fn non_dict_drop_surfaces_pointer_and_persists_even_with_marker_off() {
        // Defect 1: parity with the dict path. With `advertise_retrieval_tool=false`, the non-dict string path STILL surfaces the `<<ccr:HASH>>` recovery pointer
        // in the output AND writes the store. The pointer is the recovery key; suppressing it while still dropping rows is the silent loss the invariant forbids.
        use crate::ccr::InMemoryCcrStore;
        use std::sync::Arc;

        let store = Arc::new(InMemoryCcrStore::new());
        let store_dyn: Arc<dyn CcrStore> = Arc::clone(&store) as Arc<dyn CcrStore>;
        let cfg = SmartCrusherConfig {
            advertise_retrieval_tool: false,
            ..SmartCrusherConfig::default()
        };
        let c = SmartCrusherBuilder::new(cfg)
            .with_default_oss_setup()
            .with_default_compaction()
            .with_ccr_store(store_dyn)
            .build();

        let items: Vec<Value> = (0..200)
            .map(|i| json!(format!("distinct-string-{i}")))
            .collect();
        let content = serde_json::to_string(&items).unwrap();

        let store_before = store.len();
        let result = c.crush(&content, "", 1.0);
        let store_after = store.len();

        // The recovery pointer IS in the output even with the flag off.
        assert!(
            result.compressed.contains("<<ccr:"),
            "recovery pointer must be surfaced even with advertise_retrieval_tool=false \
             (Defect 1), got: {}",
            &result.compressed[..result.compressed.len().min(200)]
        );
        // The store grew — persistence is unconditional.
        assert!(
            store_after > store_before,
            "ccr_store must grow on a drop (kill silent loss)"
        );
        // The pointer carries the hash that keys the canonical original.
        let expected_hash = hash_canonical(&canonical_array_json(&items));
        assert!(
            result.compressed.contains(&expected_hash),
            "the surfaced pointer must reference the canonical hash"
        );
        let recovered = store
            .get(&expected_hash)
            .expect("non-dict drop must be retrievable by hash");
        assert_eq!(recovered, canonical_array_json(&items));
    }
}
