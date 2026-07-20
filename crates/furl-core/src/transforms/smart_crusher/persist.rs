//! CCR persistence — `persist_dropped`, the `DroppedPersist`/`CcrWrite`
//! plumbing, the CCR-Dropped sentinel builders, and `hash_canonical`
//! (ARCH-4: split out of `crusher.rs` as pure moves, zero behavior
//! change). The invariant-bearing module: whenever rows are dropped the
//! full original is hashed, persisted per [`PersistMode`], and surfaced
//! behind a `<<ccr:HASH>>` recovery pointer — never silently lost.
//!
//! The shared hash/persist primitives consolidated by ARCH-5 stay in
//! `crate::ccr::persist`; this module owns the smart-crusher-local
//! persist logic built on top of them.

use std::sync::Arc;

use serde_json::Value;

use super::crusher::SmartCrusher;
use super::types::DroppedRef;
use crate::ccr::persist::row_index_key;
use crate::ccr::{marker_for_row_index, marker_for_rows_offloaded, CcrStore};

/// Build the `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>",
/// "_ccr_rows": "<<ccr:HASH#rows N_chunks>>"}` sentinel object.
///
/// `_ccr_dropped` carries the byte-stable whole-blob recovery pointer
/// (unchanged — the single-blob retrieve path + parity hash still hold).
/// `_ccr_rows`, when present, is ONE compact marker naming the per-blob
/// row index so retrieval is PROPORTIONAL: the consumer resolves the
/// index, then fetches only the row(s) it needs (each `ccr_get(row_hash)`
/// returns just that single row) instead of the whole offloaded payload.
/// It is a single short string — surfacing it costs ~no prompt tokens
/// and does not perturb the lossy-vs-lossless token routing. `_ccr_rows`
/// is omitted when absent (no store configured), keeping the no-store
/// sentinel byte-identical to the historical shape.
pub(super) fn ccr_sentinel_map(
    dropped_summary: &str,
    row_index_marker: Option<&str>,
) -> serde_json::Map<String, Value> {
    let mut sentinel = serde_json::Map::new();
    sentinel.insert(
        "_ccr_dropped".to_string(),
        Value::String(dropped_summary.to_string()),
    );
    if let Some(idx) = row_index_marker {
        sentinel.insert("_ccr_rows".to_string(), Value::String(idx.to_string()));
    }
    sentinel
}

/// Build the full sentinel `Value::Object` from a [`DroppedPersist`].
fn build_ccr_sentinel(persisted: &DroppedPersist) -> Value {
    Value::Object(ccr_sentinel_map(
        &persisted.marker,
        persisted.row_index_marker().as_deref(),
    ))
}

/// One deferred CCR store write (`key` → `payload`). Captured by
/// [`SmartCrusher::persist_dropped`] under [`PersistMode::Collect`] in
/// commit order (granular chunks → row index → whole-blob, the same
/// eviction-friendly order the direct writes use) and replayed by
/// [`SmartCrusher::commit_ccr_writes`] iff the lossy render ships.
pub(super) struct CcrWrite {
    key: String,
    payload: String,
}

/// How [`SmartCrusher::persist_dropped`] treats the CCR store writes for
/// a drop. The hash, whole-blob marker and granular row-index marker are
/// computed IDENTICALLY in every mode (COR-28 byte-parity: routing built
/// on them cannot shift) — the modes differ ONLY in what happens to the
/// store writes.
#[derive(Clone, Copy)]
pub(super) enum PersistMode {
    /// Write through to the configured store immediately. For paths
    /// whose render ALWAYS ships (the non-dict string/number/mixed
    /// sentinel path) — there is no later routing decision to wait for.
    Commit,
    /// Compute the writes but DEFER them: they are returned on
    /// [`DroppedPersist::pending_writes`] and committed by the routing
    /// layer only if the lossy render is actually chosen to ship. This
    /// is the P0-4 fix: committing at build time left orphan
    /// blob + chunks + index entries behind whenever the LOSSLESS render
    /// won the MinTokens/LosslessFirst arbitration — wasted
    /// COR-4-bounded capacity under hashes no surfaced marker names.
    Collect,
    /// Never write (COR-28, mixed dict arm): the caller surfaces no
    /// marker naming this hash, so any write would be an orphan.
    Skip,
}

/// Result of [`SmartCrusher::persist_dropped`] — the CCR hash that keys
/// the stored full-original array plus the prompt-visible recovery
/// pointer text. `Some(_)` only when rows were actually dropped. Under
/// [`PersistMode::Commit`] the store write (when a store is configured)
/// has already happened by the time this is returned; under
/// [`PersistMode::Collect`] the writes ride in `pending_writes` for the
/// caller to commit iff the render ships.
pub(super) struct DroppedPersist {
    /// 24-hex (96-bit) SHA-256 prefix of the canonical full-original array.
    /// Always returned when something was dropped — callers may mirror
    /// or retrieve it.
    pub(super) hash: String,
    /// `<<ccr:HASH N_rows_offloaded>>` recovery pointer. ALWAYS
    /// non-empty when rows were dropped (Defect 1): the pointer is the
    /// recovery key, not a UX flag, so it is surfaced unconditionally on
    /// every drop. The store write backing a SHIPPED render is likewise
    /// unconditional (immediate under `Commit`, at ship-time under
    /// `Collect`).
    pub(super) marker: String,
    /// Number of granular per-row chunks the store-side row index holds
    /// (exactly the in-range dropped rows — COR-20). `None` when no
    /// store is configured to chunk into or the drop exceeded the COR-4
    /// granular budget (the array hash + whole-blob `marker` still cover
    /// recovery in that case). The single datum behind BOTH the rendered
    /// `<<ccr:{hash}#rows {n}_chunks>>` marker (derived via
    /// [`DroppedPersist::row_index_marker`]) and the typed
    /// [`DroppedRef::RowDrop`] the FFI carries — one source, no drift.
    row_index_chunks: Option<usize>,
    /// Deferred store writes, populated ONLY under
    /// [`PersistMode::Collect`] (empty under `Commit` — already written —
    /// and `Skip` — never written). Commit order is preserved: granular
    /// chunks first, then the row index, the whole-blob LAST, so the
    /// bounded-store eviction rationale documented in `persist_dropped`
    /// holds identically for deferred replay.
    pub(super) pending_writes: Vec<CcrWrite>,
}

impl DroppedPersist {
    /// Compact GRANULAR-retrieval marker, e.g.
    /// `<<ccr:9f3a2b#rows 50_chunks>>`. ONE short string (not a list), so
    /// surfacing it costs ~no prompt tokens and never flips the
    /// lossy-vs-lossless routing decision. It names the per-blob ROW
    /// INDEX entry (`{hash}#rows`) the store holds: a JSON array of the
    /// per-row hashes. The retrieval layer resolves it to fetch only the
    /// needed row(s) — `ccr_get(row_hash)` returns a 1-element array
    /// holding exactly that row — instead of paying for the whole blob.
    /// Derived from `row_index_chunks` through the pinned grammar owner
    /// (`ccr::markers`), byte-identical to the pre-derivation field.
    pub(super) fn row_index_marker(&self) -> Option<String> {
        self.row_index_chunks
            .map(|n| marker_for_row_index(&self.hash, n))
    }

    /// The typed carrier for this drop (§4.2) — the exact values the
    /// rendered sentinel advertises, surfaced for direct mirroring.
    pub(super) fn dropped_ref(&self) -> DroppedRef {
        DroppedRef::RowDrop {
            hash: self.hash.clone(),
            row_index_chunks: self.row_index_chunks,
        }
    }
}

impl SmartCrusher {
    /// Replay deferred CCR writes captured under [`PersistMode::Collect`],
    /// in capture order (granular chunks → row index → whole-blob — the
    /// same eviction-friendly order `persist_dropped` writes directly).
    /// Called by the routing layer EXACTLY when the lossy render ships;
    /// a discarded candidate's writes are simply dropped, so the store
    /// never carries entries no surfaced marker names (P0-4).
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

    /// Shared CCR persist + sentinel logic — the single source of truth
    /// for sub-step 1A's "kill silent loss" guarantee across **every**
    /// lossy array path (dict / string / number / mixed).
    ///
    /// When `dropped_count > 0`, serialize the FULL `original_items`
    /// array exactly once into canonical JSON, hash those bytes, and
    /// **unconditionally** write `(hash → canonical)` to the configured
    /// CCR store (if any), then **unconditionally** build the
    /// `<<ccr:HASH N_rows_offloaded>>` recovery pointer. The store write
    /// and pointer together are the cornerstone of the no-data-loss
    /// guarantee: a dropped needle is always retrievable via
    /// `ccr_get(hash)` AND nameable from the output via the pointer —
    /// never silently lost. Neither is gated by `advertise_retrieval_tool`
    /// (Defect 1): you cannot drop a distinct item without surfacing a
    /// recovery pointer to it.
    ///
    /// Returns `None` when nothing was dropped (no hash, no marker, no
    /// store write). Centralizing this here keeps the canonicalization
    /// and hash scheme byte-identical across all callers — the dict path
    /// behavior is unchanged, and the non-dict paths now inherit the
    /// exact same contract.
    ///
    /// `dropped_count` drives the whole-blob marker text (the byte-stable
    /// `{n}_rows_offloaded` arithmetic every caller already computed as
    /// `original - kept`); `dropped_indices` names exactly WHICH original
    /// rows left the visible output and therefore get granular per-row
    /// chunks. The two agree on every path; they can diverge only when a
    /// crusher synthesizes non-original output items (mixed-path
    /// summaries), in which case the index simply covers every original
    /// row not visible verbatim — a safe superset for recovery.
    ///
    /// `mode` selects what happens to the store writes ([`PersistMode`]):
    /// `Commit` writes through immediately (always-ships paths);
    /// `Collect` (P0-4, the dict arbitration path) defers them into
    /// [`DroppedPersist::pending_writes`] for commit-on-ship; `Skip`
    /// (COR-28, via `crush_array_inner`) performs NO store write — the
    /// caller surfaces no marker naming this hash, so any write would be
    /// an orphan entry burning COR-4-bounded capacity. In EVERY mode the
    /// hash, marker and granular-index decision are computed EXACTLY the
    /// same — the returned markers are byte-identical, so token routing
    /// built on them cannot shift. The COR-4 store-flood gate still
    /// decides `row_index_marker` the same way in all modes.
    pub(super) fn persist_dropped(
        &self,
        original_items: &[Value],
        dropped_count: usize,
        dropped_indices: &[usize],
        mode: PersistMode,
    ) -> Option<DroppedPersist> {
        if dropped_count == 0 {
            return None;
        }

        // Deferred-write sink (populated only under `Collect`). Routing a
        // write: through to the store (Commit), into the sink (Collect),
        // or nowhere (Skip). Total over `PersistMode` so a new mode is a
        // compile error here, not a silent write-nothing.
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

        // Serialize the original array exactly ONCE. The hash is taken
        // over those bytes, and (if a store is configured) the same
        // bytes get stored — no redundant clone or re-serialize.
        let canonical = canonical_array_json(original_items);
        let hash = hash_canonical(&canonical);

        // ── GRANULAR per-row persist (proportional retrieval) ──
        //
        // Held-out audit (verify/heldout/REPORT.md, leniency #2): the
        // single whole-blob offload makes the FIRST needed row cost the
        // WHOLE payload — a single `<<ccr:HASH>>` retrieve returns every
        // dropped row at once, so effective savings can go NEGATIVE the
        // moment the model retrieves anything (logs@90 high: +55.7% @25%
        // → −10.3% worst-case). Fix: ALSO stash each DROPPED row under
        // its own canonical 1-element hash so retrieving ONE row fetches
        // exactly one row, not the whole blob.
        //
        // Only the DROPPED rows are chunked (COR-4). Kept rows are
        // already visible in the output — a per-row chunk for them buys
        // nothing, and writing one store entry per ORIGINAL row let a
        // single large array (or two arrays in one document) flood the
        // bounded FIFO store and evict whole-blob entries the SAME
        // document's markers still referenced — a dangling `<<ccr:HASH>>`
        // is silent loss. Each per-row entry is keyed by `hash_canonical`
        // over the 1-element canonical array, so `ccr_get(row_hash)`
        // returns exactly `[row]`.
        //
        // ── Store-flood gate (COR-4, oversized drops) ──
        // Even dropped-only chunking can flood the store: a drop bigger
        // than the store's capacity would self-evict its own earliest
        // chunks. When the chunk count exceeds `capacity /
        // GRANULAR_CHUNK_CAPACITY_DIVISOR`, skip granular chunking
        // entirely and persist the whole-blob only (`row_index_marker:
        // None` — an already-supported sentinel shape). Proportional
        // retrieval degrades to whole-blob retrieval for oversized
        // arrays; recovery never degrades.
        //
        // The per-row hashes are NOT inlined into the prompt (that would
        // add O(n) pointer strings, bloat the lossy render, and flip the
        // min-tokens routing toward lossless). Instead they go into a
        // store-side ROW INDEX under `{hash}#rows` — a JSON array of the
        // row hashes — and the prompt carries ONE compact marker naming
        // that index. Retrieval: resolve the index (cheap), then fetch
        // only the needed rows. Prompt cost ~flat; retrieval proportional.
        //
        // ── Write ORDER matters under a bounded LRU ──
        // The per-row chunks + index are written FIRST, the whole-blob
        // LAST. Per-row chunks are redundant with the whole-blob (both
        // recover the same rows), so when the store is capacity-bound the
        // FIFO eviction sheds the redundant chunks before the whole-blob
        // — keeping the byte-stable single-blob recovery fallback alive
        // even if proportional retrieval degrades. Recovery is therefore
        // never worse than the pre-change single-blob guarantee.
        let mut row_index_chunks: Option<usize> = None;
        if let Some(store) = &self.ccr_store {
            let granular_budget = store.capacity() / GRANULAR_CHUNK_CAPACITY_DIVISOR;
            if !dropped_indices.is_empty() && dropped_indices.len() <= granular_budget {
                // In-range dropped rows are the chunkable set (out-of-
                // range indices: nothing to chunk). Resolved up front so
                // the marker's chunk count stays identical whether or
                // not the writes below happen (COR-28 persist-skip).
                let chunk_rows: Vec<&Value> = dropped_indices
                    .iter()
                    .filter_map(|&idx| original_items.get(idx))
                    .collect();
                if !matches!(mode, PersistMode::Skip) {
                    let mut row_hashes: Vec<String> = Vec::with_capacity(chunk_rows.len());
                    for item in &chunk_rows {
                        let row_canonical = canonical_array_json(std::slice::from_ref(*item));
                        let row_hash = hash_canonical(&row_canonical);
                        route_write(mode, store, &mut pending_writes, &row_hash, row_canonical);
                        row_hashes.push(row_hash);
                    }
                    // Row index: `{hash}#rows` → ["rowhash0", "rowhash1", ...].
                    // Stored as a JSON array of strings so the retrieval layer can
                    // parse it and address each dropped row independently. The
                    // marker advertises `chunk_rows.len()` — EXACTLY the number
                    // of chunks the index holds (COR-20: it previously claimed
                    // `dropped_count` chunks over an every-original-row index).
                    // Key construction shared with the typed carrier's
                    // `DroppedRef::row_index_key` accessor (one owner).
                    let index_key = row_index_key(&hash);
                    let index_payload = serde_json::to_string(&row_hashes).unwrap_or_default();
                    route_write(mode, store, &mut pending_writes, &index_key, index_payload);
                }
                row_index_chunks = Some(chunk_rows.len());
            }
        }

        // ── Whole-blob persist (1A) — written LAST ──
        // The byte-stable recovery key the invariant + parity depend on.
        // Unconditional on every persisting path (immediate under Commit,
        // deferred-to-ship under Collect); skipped ONLY for the
        // surfaced-nowhere mixed-arm inner call (COR-28, Skip).
        if let Some(store) = &self.ccr_store {
            route_write(mode, store, &mut pending_writes, &hash, canonical);
        }

        // ── Unconditional recovery pointer (Defect 1) ──
        //
        // The `<<ccr:HASH N_rows_offloaded>>` marker is the RECOVERY
        // KEY, not a UX nicety: it is the only way a consumer holding
        // just the output can name the hash and pull the dropped rows
        // back. It MUST be surfaced whenever data is dropped, regardless
        // of `advertise_retrieval_tool`. The recovery invariant ("a dropped
        // item is recoverable from the output alone") cannot hold if the
        // pointer is suppressed while the rows are still dropped.
        //
        // `advertise_retrieval_tool` historically gated this text; that
        // conflated the *data-loss recovery pointer* with the heavier
        // *retrieval-tool injection* (advertising `furl_retrieve`
        // into the request), which is owned by the router layer
        // (`CCRConfig.inject_tool` / `inject_retrieval_marker`), NOT by
        // the crusher. The crusher's job is to never drop a distinct
        // item without leaving a pointer to it; that pointer is now
        // emitted unconditionally on every drop.
        let marker = marker_for_rows_offloaded(&hash, dropped_count);

        Some(DroppedPersist {
            hash,
            marker,
            row_index_chunks,
            pending_writes,
        })
    }

    /// Build the `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>"}`
    /// sentinel object for a non-dict array drop, persisting the full
    /// original via [`SmartCrusher::persist_dropped`] first.
    ///
    /// Returns `None` ONLY when nothing was dropped. Whenever rows were
    /// dropped, the sentinel is always produced (Defect 1): the
    /// recovery pointer is non-optional, so the output always carries a
    /// `<<ccr:HASH>>` the consumer can resolve via `ccr_get(hash)` —
    /// never a silent drop. This mirrors the dict path, where
    /// `dropped_summary` is now likewise always non-empty on a drop.
    ///
    /// Pushes the typed [`DroppedRef::RowDrop`] onto `dropped` so the
    /// non-dict (string/number/mixed) row-drop is surfaced for direct
    /// mirroring — parity with the dict path. Side effect only: the
    /// returned `Value` is byte-identical to the pre-collection behavior.
    ///
    /// `kept_items` is the crushed output BEFORE the sentinel is pushed.
    /// The whole-blob marker keeps the callers' historical arithmetic
    /// (`original - kept`); the dropped-row set for granular chunking is
    /// derived by multiset diff, since the non-dict crushers return kept
    /// VALUES, not kept indices (COR-4: kept rows are never chunked).
    pub(super) fn ccr_dropped_sentinel_collecting(
        &self,
        original_items: &[Value],
        kept_items: &[Value],
        dropped: &mut Vec<DroppedRef>,
    ) -> Option<Value> {
        let dropped_count = original_items.len().saturating_sub(kept_items.len());
        let dropped_indices = dropped_indices_by_multiset_diff(original_items, kept_items);
        // Commit mode: the non-dict paths never arbitrate against a
        // lossless candidate — this render always ships, so the write
        // happens now (no deferral needed).
        let persisted = self.persist_dropped(
            original_items,
            dropped_count,
            &dropped_indices,
            PersistMode::Commit,
        )?;
        dropped.push(persisted.dropped_ref());
        Some(build_ccr_sentinel(&persisted))
    }
}

/// Store-flood gate for granular per-row chunking (COR-4). One drop
/// writes `chunks + index + whole-blob` entries into a bounded FIFO
/// store; capping the chunk count at `capacity / 4` leaves room for
/// several drops in one document (each ≤ ~capacity/4 + 2 entries)
/// before anything a live marker references can be evicted. Drops
/// bigger than the budget persist the whole-blob only.
const GRANULAR_CHUNK_CAPACITY_DIVISOR: usize = 4;

/// Serialize `[v0, v1, ...]` once into the canonical JSON form used by
/// the CCR retrieval contract. `serde_json` writes a slice of `Value` as
/// the same bytes it would write for `Value::Array(items.to_vec())`, so
/// we skip the array-wrapper allocation and the deep tree clone it
/// requires. Used by both the hash (input) and the store payload (write).
pub(super) fn canonical_array_json(items: &[Value]) -> String {
    serde_json::to_string(items).unwrap_or_default()
}

/// Complement of `keep_indices` over `0..len`: the original-array
/// indices of the rows a plan DROPS, ascending. Out-of-range keep
/// indices are ignored (mirroring `execute_plan`'s bounds filter) and
/// duplicates collapse via the mask, so the result length always equals
/// `len - |kept ∩ 0..len|`. Feeds `persist_dropped`'s dropped-rows-only
/// granular chunking on the dict path (COR-4).
pub(super) fn dropped_indices_from_kept(keep_indices: &[usize], len: usize) -> Vec<usize> {
    let mut kept = vec![false; len];
    for &idx in keep_indices {
        if idx < len {
            kept[idx] = true;
        }
    }
    kept.iter()
        .enumerate()
        .filter_map(|(i, &k)| (!k).then_some(i))
        .collect()
}

/// Original-array indices whose rows do NOT appear in `kept`, ascending
/// (multiset semantics: each kept occurrence consumes ONE matching
/// original). Matching is on the same per-row canonical bytes the
/// granular chunks are keyed by (`canonical_array_json` of the
/// 1-element slice), so "kept" means byte-identical to a visible output
/// row. A synthesized kept item (e.g. a mixed-path summary) matches no
/// original and consumes nothing — the result can only ever
/// OVER-approximate the dropped set, never miss a genuinely dropped
/// row. Feeds `persist_dropped` on the string/number/mixed paths, whose
/// crushers return kept VALUES rather than kept indices (COR-4).
fn dropped_indices_by_multiset_diff(original: &[Value], kept: &[Value]) -> Vec<usize> {
    use std::collections::HashMap;
    let mut kept_counts: HashMap<String, usize> = HashMap::with_capacity(kept.len());
    for item in kept {
        *kept_counts
            .entry(canonical_array_json(std::slice::from_ref(item)))
            .or_insert(0) += 1;
    }
    original
        .iter()
        .enumerate()
        .filter_map(|(i, item)| {
            let key = canonical_array_json(std::slice::from_ref(item));
            match kept_counts.get_mut(&key) {
                Some(count) if *count > 0 => {
                    *count -= 1;
                    None
                }
                _ => Some(i),
            }
        })
        .collect()
}

/// 24-hex (96-bit) SHA-256 prefix of an already-serialized canonical JSON
/// string. Caller is responsible for producing the canonical form via
/// [`canonical_array_json`] (or another byte-equal serializer) — the
/// hash is over the bytes, so a stable serializer is the contract.
/// Algorithm + width consolidated in `ccr::persist` (ARCH-5); this domain
/// alias stays so the row-drop call sites and the parity pins keep their
/// vocabulary.
fn hash_canonical(canonical: &str) -> String {
    crate::ccr::persist::sha256_recovery_key(canonical.as_bytes())
}

// `hash_array_for_ccr` (a test-only `canonical_array_json + hash_canonical`
// convenience) lived here previously but had no callers — clippy flagged
// it as dead code. Reintroduce as a test fixture if a future test wants
// the one-liner; production callsites inline both steps so the canonical
// bytes can be reused for the store payload.

#[cfg(test)]
mod tests {
    use super::super::builder::SmartCrusherBuilder;
    use super::super::config::SmartCrusherConfig;
    use super::super::crusher::test_support::crusher_with_store;
    use super::*;
    use serde_json::json;

    #[test]
    fn persist_dropped_hash_is_byte_identical_to_inline_dict_scheme() {
        // The shared helper must produce the SAME hash the dict path
        // produced inline before the refactor: SHA-256(canonical) → 12
        // hex chars over `canonical_array_json(items)`. Pin it so the
        // CCR retrieve contract is provably unchanged.
        let (c, _store) = crusher_with_store();
        let items: Vec<Value> = (0..30).map(|i| json!({"id": i})).collect();
        let persisted = c
            .persist_dropped(&items, 5, &[25, 26, 27, 28, 29], PersistMode::Commit)
            .expect("dropped_count>0 → Some");
        let expected = hash_canonical(&canonical_array_json(&items));
        assert_eq!(persisted.hash, expected, "hash scheme must be unchanged");
        assert_eq!(persisted.hash.len(), 24);
        assert!(persisted
            .marker
            .contains(&format!("<<ccr:{expected} 5_rows_offloaded>>")));
        // Zero dropped → None (no hash, no marker, no store write).
        assert!(c
            .persist_dropped(&items, 0, &[], PersistMode::Commit)
            .is_none());
    }

    // ---------- COR-4 / COR-20: dropped-rows-only granular persist ----------

    #[test]
    fn persist_dropped_chunks_only_dropped_rows_and_marker_counts_them() {
        // COR-4: kept rows must NEVER be written to the store — one
        // granular chunk per DROPPED row, nothing else. COR-20: the
        // `_ccr_rows` marker advertises EXACTLY the number of chunks the
        // index holds (it previously claimed `dropped_count` chunks over
        // an every-original-row index — "keep 7 of 60" rendered
        // `53_chunks` over a 60-entry index, a model-visible lie).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        // Keep rows 0..7, drop rows 7, 8, 9.
        let dropped_indices = [7usize, 8, 9];
        let persisted = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Commit)
            .expect("drop → Some");

        // Marker advertises exactly the chunks the index holds (COR-20).
        let idx_marker = persisted
            .row_index_marker()
            .expect("granular index fires for a small drop");
        assert!(
            idx_marker.contains("#rows 3_chunks>>"),
            "marker must advertise the 3 dropped-row chunks, got: {idx_marker}"
        );

        // The index holds one hash per DROPPED row, in original order,
        // each resolving to exactly `[row]`.
        let index_raw = store
            .get(&format!("{}#rows", persisted.hash))
            .expect("row index stored");
        let row_hashes: Vec<String> = serde_json::from_str(&index_raw).unwrap();
        assert_eq!(row_hashes.len(), 3, "index holds dropped rows only");
        for (rh, &orig_idx) in row_hashes.iter().zip(dropped_indices.iter()) {
            let payload = store.get(rh).expect("dropped-row chunk resolves");
            assert_eq!(
                payload,
                canonical_array_json(std::slice::from_ref(&items[orig_idx]))
            );
        }

        // Kept rows are NOT in the store under their per-row hashes.
        for kept in &items[..7] {
            let kept_canonical = canonical_array_json(std::slice::from_ref(kept));
            let kept_hash = hash_canonical(&kept_canonical);
            assert!(
                store.get(&kept_hash).is_none(),
                "kept row {kept} must never be written to the store (COR-4)"
            );
        }

        // Store contents: 3 chunks + 1 index + 1 whole-blob, nothing more.
        assert_eq!(store.len(), 5, "3 chunks + index + whole-blob exactly");
    }

    #[test]
    fn persist_dropped_skips_granular_chunking_for_oversized_drops() {
        // COR-4 store-flood gate: when the dropped-row count exceeds
        // `store.capacity() / 4`, granular chunking stands down entirely
        // — whole-blob only, `row_index_marker: None` — so a single big
        // array can never self-evict its own chunks (and two big arrays
        // in one document can never evict each other's whole-blobs).
        let (c, store) = crusher_with_store();
        let budget = store.capacity() / GRANULAR_CHUNK_CAPACITY_DIVISOR;

        // One past the budget → granular skipped, whole-blob only.
        let n_over = budget + 8;
        let items: Vec<Value> = (0..n_over).map(|i| json!({"id": i})).collect();
        let dropped_indices: Vec<usize> = (0..=budget).collect(); // budget+1 dropped
        let persisted = c
            .persist_dropped(
                &items,
                dropped_indices.len(),
                &dropped_indices,
                PersistMode::Commit,
            )
            .expect("drop → Some");
        assert!(
            persisted.row_index_marker().is_none(),
            "oversized drop must not advertise a granular index"
        );
        assert_eq!(
            store.len(),
            1,
            "oversized drop persists the whole-blob ONLY (no chunk flood)"
        );
        let payload = store.get(&persisted.hash).expect("whole-blob resolves");
        assert_eq!(payload, canonical_array_json(&items));

        // Exactly AT the budget → granular still fires (inclusive bound).
        let (c2, store2) = crusher_with_store();
        let at_budget: Vec<usize> = (0..budget).collect();
        let persisted2 = c2
            .persist_dropped(&items, at_budget.len(), &at_budget, PersistMode::Commit)
            .expect("drop → Some");
        assert!(
            persisted2.row_index_marker().is_some(),
            "a drop at the budget keeps proportional retrieval"
        );
        assert_eq!(
            store2.len(),
            budget + 2,
            "chunks + index + whole-blob at the budget"
        );
    }

    #[test]
    fn persist_dropped_skip_mode_is_marker_identical_and_writes_nothing() {
        // COR-28 persist-skip contract: `PersistMode::Skip` must return a
        // BYTE-IDENTICAL DroppedPersist (hash, whole-blob marker, and
        // granular row-index marker — the inputs to MinTokens routing)
        // while writing NOTHING to the store (and deferring nothing —
        // there is no later commit for the mixed arm). If the marker text
        // could shift, the mixed arm's inner routing would shift with it
        // and the skip would no longer be behavior-invisible.
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let dropped_indices = [7usize, 8, 9];

        let skipped = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Skip)
            .expect("drop → Some regardless of persist mode");
        assert_eq!(store.len(), 0, "Skip mode must write nothing");
        assert!(
            skipped.pending_writes.is_empty(),
            "Skip mode must defer nothing either (no later commit exists)"
        );

        let persisted = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Commit)
            .expect("drop → Some");
        assert_eq!(skipped.hash, persisted.hash);
        assert_eq!(skipped.marker, persisted.marker);
        assert_eq!(skipped.row_index_marker(), persisted.row_index_marker());
        assert!(
            persisted.pending_writes.is_empty(),
            "Commit mode writes through — nothing rides back deferred"
        );
        assert_eq!(
            store.len(),
            5,
            "Commit mode still writes 3 chunks + index + whole-blob"
        );
    }

    #[test]
    fn persist_dropped_collect_mode_defers_writes_and_replay_matches_commit() {
        // P0-4 Collect contract: hash + markers byte-identical to Commit
        // (routing built on them cannot shift), NOTHING written until the
        // caller commits — and replaying the deferred writes reproduces
        // Commit-mode store state exactly (same keys, same payloads, same
        // chunks → index → whole-blob order).
        let (c, store) = crusher_with_store();
        let items: Vec<Value> = (0..10).map(|i| json!({"id": i})).collect();
        let dropped_indices = [7usize, 8, 9];

        let collected = c
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Collect)
            .expect("drop → Some");
        assert_eq!(store.len(), 0, "Collect must write nothing before commit");
        assert_eq!(
            collected.pending_writes.len(),
            5,
            "3 chunks + index + whole-blob ride back deferred"
        );

        let (c2, store2) = crusher_with_store();
        let committed = c2
            .persist_dropped(&items, 3, &dropped_indices, PersistMode::Commit)
            .expect("drop → Some");
        assert_eq!(collected.hash, committed.hash, "hash is mode-independent");
        assert_eq!(collected.marker, committed.marker);
        assert_eq!(collected.row_index_marker(), committed.row_index_marker());

        // The whole-blob write is LAST (bounded-store eviction rationale
        // — redundant chunks shed before the recovery backstop).
        assert_eq!(
            collected.pending_writes.last().map(|w| w.key.as_str()),
            Some(committed.hash.as_str()),
            "whole-blob must be the final deferred write"
        );

        // Replaying the deferred writes reproduces Commit-mode state.
        c.commit_ccr_writes(collected.pending_writes);
        assert_eq!(store.len(), store2.len());
        assert_eq!(store.len(), 5, "3 chunks + index + whole-blob exactly");
        assert_eq!(
            store.get(&committed.hash),
            store2.get(&committed.hash),
            "whole-blob payload identical across modes"
        );
        let index_key = format!("{}#rows", committed.hash);
        assert_eq!(
            store.get(&index_key),
            store2.get(&index_key),
            "row index identical across modes"
        );
        let row_hashes: Vec<String> =
            serde_json::from_str(&store.get(&index_key).expect("index present")).unwrap();
        for rh in &row_hashes {
            assert_eq!(
                store.get(rh),
                store2.get(rh),
                "per-row chunk {rh} identical across modes"
            );
        }
    }

    #[test]
    fn dropped_indices_helpers_cover_complement_and_multiset_diff() {
        // Dict path: complement of the plan's keep set, out-of-range
        // keeps ignored, duplicates collapsed.
        assert_eq!(dropped_indices_from_kept(&[0, 2, 4], 5), vec![1, 3]);
        assert_eq!(dropped_indices_from_kept(&[], 3), vec![0, 1, 2]);
        assert_eq!(dropped_indices_from_kept(&[0, 0, 99], 3), vec![1, 2]);
        assert_eq!(
            dropped_indices_from_kept(&[0, 1, 2], 3),
            Vec::<usize>::new()
        );

        // Non-dict paths: multiset diff on values. Duplicates consume
        // one-for-one; synthesized kept items consume nothing.
        let original = vec![json!("a"), json!("a"), json!("b"), json!("c")];
        let kept = vec![json!("a"), json!("c"), json!("<summary>")];
        assert_eq!(
            dropped_indices_by_multiset_diff(&original, &kept),
            vec![1, 2],
            "one 'a' kept consumes index 0; 'b' and the second 'a' dropped"
        );
        assert_eq!(
            dropped_indices_by_multiset_diff(&original, &original),
            Vec::<usize>::new()
        );
    }

    #[test]
    fn hash_canonical_pinned_vectors() {
        // Rule-2 pin (anti parallel-mutation blindness): fixed SHA-256[:24]
        // literals over the EXACT canonical bytes `serde_json::to_string`
        // emits. A truncation (`take(12)`→`take(11)`), a hex-format change, or
        // a hasher swap FLIPS a literal here — unlike the sibling
        // `persist_dropped_hash_is_byte_identical_to_inline_dict_scheme`,
        // which RECOMPUTES `expected` and would survive every such mutation.
        // Literals produced once in Python and pinned identically on the
        // Python side (tests/test_ccr_hash_parity_vectors.py) — the two
        // pins together are the Py↔Rust parity lock for the CCR recovery key.
        // The first 12 hex chars are the historical 48-bit key, extended to
        // 24 hex / 96 bits (CCR_KEY_HEX_WIDTH):
        //   python3 -c "import hashlib; print(hashlib.sha256(C.encode()).hexdigest()[:24])"
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
        // The canonical serializer must emit EXACTLY those bytes, so each
        // literal above is the hash of what the array drop path actually
        // hashes (closes the loop: a serializer change would flip this).
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
        // TEST-33: the WIRE-FORM half of the Py↔Rust parity lock. This
        // canonical is LITERAL-PRESERVING for decimal tokens (serde_json
        // `arbitrary_precision`: `1.50` stays `1.50`, digits beyond f64
        // precision survive) and normalizes ONLY the exponent spelling
        // (`1E5` → `1e+5`: lowercase `e`, explicit sign — serde's number
        // scanner, not a float round-trip). The documented Python
        // reference (`json.loads` → `json.dumps`) round-trips through
        // float and computes a DIFFERENT key for every numeric vector
        // here (`1.50`→`1.5`, `1E5`→`100000.0`, `1e400`→`Infinity`) — it
        // is valid for Python-normal-form inputs only. Pinned identically
        // (from the canonical TEXT, not parsed values) on the Python side
        // (tests/test_ccr_hash_parity_vectors.py::_WIRE_VECTORS):
        //   python3 -c "import hashlib; print(hashlib.sha256(C.encode()).hexdigest()[:24])"
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
            // Non-ASCII and control-char forms — these AGREE with the
            // Python reference (same hashes pinned in its `_VECTORS`);
            // included so the lock covers the full canonical grammar,
            // not just ASCII scalars.
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
            // The production path hashes `canonical_array_json(parsed
            // rows)` — pin that parsing the wire text re-serializes to
            // EXACTLY the canonical bytes above (`preserve_order` +
            // `arbitrary_precision`). A serde feature-flag regression or
            // a serializer swap flips this before it can corrupt a key.
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
        // Defect 1: parity with the dict path. With
        // `advertise_retrieval_tool=false`, the non-dict string path STILL
        // surfaces the `<<ccr:HASH>>` recovery pointer in the output AND
        // writes the store. The pointer is the recovery key; suppressing
        // it while still dropping rows is the silent loss the invariant
        // forbids. The hash carried by the pointer keys the canonical
        // original in the store → fully recoverable from the output.
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
