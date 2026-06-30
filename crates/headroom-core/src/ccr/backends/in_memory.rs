//! In-memory CCR backend.
//!
//! Process-local store backed by [`DashMap`] (sharded concurrent hash
//! map). Distinct keys never contend on the read path; capacity-bound
//! eviction is the only globally-serialized step.
//!
//! This is the only CCR backend. The engine constructs one at startup and
//! shares it across worker threads behind an `Arc` for the process lifetime;
//! entries are lost on restart (CCR recovery is request-window-scoped — see
//! `CCR-RETENTION.md`).
//!
//! # Eviction scheme: generation-counter FIFO
//!
//! Each entry carries a monotonically increasing `generation: u64` stamped
//! at insert AND re-stamped on overwrite. The order queue holds
//! `(key, generation)` pairs instead of bare key strings.
//!
//! **Why generation counters?** The original FIFO-by-key scheme had three
//! defects:
//!
//! - *ABA stale-token eviction*: overwriting a key refreshed the entry but
//!   left the OLD order token at the front of the queue. A later eviction
//!   would pop that stale token and remove the LIVE, recently-refreshed
//!   entry — silently destroying a still-referenced blob.
//!
//! - *Tombstone / stale-order accumulation*: the order queue retained keys
//!   already removed by TTL or overwrite without bounding its growth. In a
//!   long-running process the queue could grow to O(total_puts) while the live
//!   map stayed bounded — a memory leak independent of capacity.
//!
//! - *Unbacked sentinel*: with `DEFAULT_CAPACITY = 1000`, a call that
//!   emits > 1000 `<<ccr:HASH>>` sentinels could self-evict earlier blobs
//!   mid-call, leaving sentinels that resolve to `None` — silent data loss.
//!
//! The generation-counter scheme fixes the first two: eviction pops
//! `(key, gen)` and only removes the entry if `entry.generation == gen`.
//! A stale token from before an overwrite will find a higher generation
//! and be skipped harmlessly. The tombstone-growth defect is tamed by
//! compacting the order queue whenever it exceeds `capacity * TOMBSTONE_K`
//! (default 2×): we rebuild it from the live entries sorted by generation,
//! discarding every stale token in O(capacity) time.
//!
//! The third defect (unbacked-sentinel / large-call self-eviction) cannot
//! be fully eliminated in an in-memory store with a fixed capacity — for
//! unbounded sessions the production path must use sqlite or redis. The
//! generation scheme makes eviction order well-defined and re-insert-safe;
//! callers relying on the full sentinel window must configure a larger
//! capacity or switch backends.

use std::collections::VecDeque;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Mutex,
};
use std::time::{Duration, Instant};

use dashmap::DashMap;

use crate::ccr::{CcrStore, DEFAULT_CAPACITY, DEFAULT_TTL};

/// Tombstone-compaction multiplier. When `order.len() > capacity *
/// TOMBSTONE_K` we compact the queue by rebuilding it from live entries.
/// A value of 2 means the queue is at most 2× the live entry count before
/// compaction, bounding queue memory proportionally to capacity.
const TOMBSTONE_K: usize = 2;

/// In-memory CCR store backed by [`DashMap`] for sharded concurrent
/// access.
///
/// - **TTL**: 5 minutes by default. Entries past their TTL are dropped
///   on the next `get` (lazy expiry — no background reaper thread).
/// - **Capacity**: 1000 entries by default. When `put` would push us
///   past capacity, the oldest entry (per insertion order) is evicted.
/// - **Concurrency**: gets and puts on distinct keys do not contend.
///   The only serialization point is the insertion-order queue used
///   for capacity eviction; that mutex is held for an O(1) push or a
///   small sweep.
pub struct InMemoryCcrStore {
    map: DashMap<String, Entry>,
    /// FIFO insertion order with generation tokens. Each element is
    /// `(key, generation)`. Tokens whose `generation` doesn't match the
    /// live entry's generation are harmless tombstones: the eviction loop
    /// skips them. When the queue exceeds `capacity * TOMBSTONE_K`, it is
    /// compacted by rebuilding from live entries sorted by generation.
    order: Mutex<VecDeque<(String, u64)>>,
    ttl: Duration,
    capacity: usize,
    /// Monotonically increasing generation counter. Each `put` (insert
    /// *or* overwrite) claims a unique generation via `fetch_add`.
    generation: AtomicU64,
}

#[derive(Clone)]
struct Entry {
    payload: String,
    inserted: Instant,
    /// Generation at which this entry was last stored. Matches the
    /// corresponding `(key, generation)` token in the order queue.
    generation: u64,
}

impl InMemoryCcrStore {
    /// Default: 1000 entries, 5-minute TTL.
    pub fn new() -> Self {
        Self::with_capacity_and_ttl(DEFAULT_CAPACITY, DEFAULT_TTL)
    }

    pub fn with_capacity_and_ttl(capacity: usize, ttl: Duration) -> Self {
        Self {
            map: DashMap::with_capacity(capacity),
            order: Mutex::new(VecDeque::with_capacity(capacity)),
            ttl,
            capacity,
            generation: AtomicU64::new(0),
        }
    }

    /// Sweep the order queue, popping tokens until `map.len() < capacity`.
    ///
    /// Tokens whose key is absent (expired) or whose generation doesn't
    /// match the live entry (ABA stale token) are skipped without changing
    /// `map.len()`. Only a successful `remove_if` (matching generation,
    /// live entry) counts as an eviction that shrinks the live set.
    ///
    /// LOCK ORDER: caller must hold the `order` mutex (passed in as
    /// `guard`) BEFORE any DashMap operation. `remove_if` takes a shard
    /// write lock internally. We never hold a DashMap ref-guard across
    /// `order.lock()` — that would invert the order and deadlock.
    fn evict_until_under_capacity(&self, guard: &mut VecDeque<(String, u64)>) {
        while self.map.len() >= self.capacity {
            let Some((oldest_key, oldest_gen)) = guard.pop_front() else {
                break;
            };
            // Only remove the entry if the stored generation matches the
            // token's generation. A generation mismatch means this token
            // is a stale tombstone from before an overwrite — skip it.
            self.map
                .remove_if(&oldest_key, |_, entry| entry.generation == oldest_gen);
            // Whether or not we removed: check map.len() again (the while
            // condition). If we skipped a tombstone the count didn't
            // change and we'll try the next token.
        }
    }

    /// Compact the order queue by rebuilding it from live entries sorted
    /// by generation ascending. Called when `order.len() > capacity *
    /// TOMBSTONE_K`. Must be called with the order mutex already held.
    fn compact_order_queue(&self, guard: &mut VecDeque<(String, u64)>) {
        // Collect all live (key, generation) pairs from the map.
        let mut live: Vec<(String, u64)> = self
            .map
            .iter()
            .map(|kv| (kv.key().clone(), kv.value().generation))
            .collect();
        // Sort by generation ascending so oldest are at the front.
        live.sort_unstable_by_key(|&(_, gen)| gen);
        *guard = VecDeque::from(live);
    }
}

impl Default for InMemoryCcrStore {
    fn default() -> Self {
        Self::new()
    }
}

impl CcrStore for InMemoryCcrStore {
    fn put(&self, hash: &str, payload: &str) {
        // Claim a fresh generation *before* touching either the map or
        // the order queue. This is a global counter — each put (insert
        // or overwrite) gets a unique, monotonically increasing stamp.
        let gen = self.generation.fetch_add(1, Ordering::Relaxed);

        // Overwrite fast-path: key already exists.
        //
        // IMPORTANT — lock order discipline:
        //   Rule: acquire `order` mutex BEFORE any DashMap shard lock.
        //   So we MUST NOT hold a DashMap `get_mut` RefMut across an
        //   `order.lock()`. The RefMut holds a shard write-lock; locking
        //   `order` while holding it would invert the order (shard→order)
        //   and deadlock with eviction (order→shard).
        //
        //   Solution: use `get_mut` as the test, mutate under its lock,
        //   then DROP the RefMut, and ONLY THEN lock `order`. If `get_mut`
        //   returns `None` (key was removed between intent and attempt —
        //   e.g. a concurrent TTL expiry or capacity eviction), we fall
        //   through to the new-entry path, which always stores the payload.
        //   This mirrors the baseline's fall-through and preserves Contract
        //   #1 (no silent loss): a `put()` call always results in a stored
        //   entry.
        let overwritten = if let Some(mut existing) = self.map.get_mut(hash) {
            // Mutate fully under the shard write-lock.
            existing.payload = payload.to_string();
            existing.inserted = Instant::now();
            existing.generation = gen;
            true
            // RefMut guard drops here, releasing the shard write-lock.
        } else {
            false
        };
        if overwritten {
            // Push a fresh token for the updated generation so that the
            // OLD token becomes a harmless tombstone (gen-mismatch skip).
            // Lock order: shard already released above, so order→shard is
            // maintained.
            let mut guard = self.order.lock().expect("ccr order mutex poisoned");
            guard.push_back((hash.to_string(), gen));
            // Compact if tombstones have accumulated.
            if guard.len() > self.capacity * TOMBSTONE_K {
                self.compact_order_queue(&mut guard);
            }
            return;
        }
        // Fall-through: key was absent (new entry) or was concurrently
        // removed between our `get_mut` and now. Either way, store the
        // payload as a fresh entry so this put is never a no-op.

        // New entry path. Take the order lock first (lock-order rule),
        // then insert into the map.
        let mut guard = self.order.lock().expect("ccr order mutex poisoned");

        // Cap-bound: evict before inserting so the map never exceeds
        // capacity even transiently.
        if self.map.len() >= self.capacity {
            self.evict_until_under_capacity(&mut guard);
        }

        let entry = Entry {
            payload: payload.to_string(),
            inserted: Instant::now(),
            generation: gen,
        };
        self.map.insert(hash.to_string(), entry);
        // Record in FIFO order. Even if a concurrent insert beat us
        // (prev.is_some()), our `gen` token is fresher and the stale
        // concurrent token will be skipped by the gen-mismatch check.
        guard.push_back((hash.to_string(), gen));

        // Compact if tombstones have accumulated.
        if guard.len() > self.capacity * TOMBSTONE_K {
            self.compact_order_queue(&mut guard);
        }
    }

    fn get(&self, hash: &str) -> Option<String> {
        // Read path: shard read-lock, check TTL, clone payload out.
        // No global lock involvement at all — distinct hashes hash to
        // distinct shards and never contend.
        //
        // Lazy expiry uses DashMap's `remove_if` so the check-and-remove
        // is atomic on the shard. An earlier 2-step (drop read lock,
        // then `remove`) had a TOCTOU race: between dropping the read
        // lock and calling `remove`, a concurrent `put()` of the same
        // hash with a fresh timestamp could land — and our `remove`
        // would then wipe that fresh entry. Under multi-worker
        // load this manifested as "I just stored it; why is it gone?"
        // `remove_if` closes the window because the shard write lock
        // is held across both the predicate evaluation and the removal.
        if let Some(entry) = self.map.get(hash) {
            if entry.inserted.elapsed() <= self.ttl {
                return Some(entry.payload.clone());
            }
        } else {
            return None;
        }
        // Out-of-band path: the entry exists and looks expired. Re-check
        // under the shard write lock; if it's still expired, evict.
        // Otherwise (a concurrent `put` refreshed it) leave it alone
        // and re-fetch its payload.
        let was_removed = self
            .map
            .remove_if(hash, |_, entry| entry.inserted.elapsed() > self.ttl)
            .is_some();
        if was_removed {
            None
        } else {
            // Concurrent refresh — return the fresh payload.
            self.map.get(hash).map(|e| e.payload.clone())
        }
    }

    fn len(&self) -> usize {
        self.map.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn put_then_get_returns_payload() {
        let store = InMemoryCcrStore::new();
        store.put("abc123", r#"[{"id":1}]"#);
        assert_eq!(store.get("abc123"), Some(r#"[{"id":1}]"#.to_string()));
    }

    #[test]
    fn missing_hash_returns_none() {
        let store = InMemoryCcrStore::new();
        assert_eq!(store.get("never_stored"), None);
    }

    #[test]
    fn put_overwrites_under_same_hash() {
        let store = InMemoryCcrStore::new();
        store.put("h", "first");
        store.put("h", "second");
        assert_eq!(store.get("h"), Some("second".to_string()));
        assert_eq!(store.len(), 1);
    }

    #[test]
    fn capacity_evicts_oldest() {
        let store = InMemoryCcrStore::with_capacity_and_ttl(2, DEFAULT_TTL);
        store.put("a", "1");
        store.put("b", "2");
        store.put("c", "3");
        assert_eq!(store.len(), 2);
        assert_eq!(store.get("a"), None);
        assert_eq!(store.get("b"), Some("2".to_string()));
        assert_eq!(store.get("c"), Some("3".to_string()));
    }

    #[test]
    fn expired_entries_are_dropped_on_get() {
        let store = InMemoryCcrStore::with_capacity_and_ttl(10, Duration::from_millis(10));
        store.put("a", "1");
        std::thread::sleep(Duration::from_millis(25));
        assert_eq!(store.get("a"), None);
        assert_eq!(store.len(), 0);
    }

    #[test]
    fn store_is_send_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<InMemoryCcrStore>();
    }

    #[test]
    fn trait_object_is_usable() {
        let store: Box<dyn CcrStore> = Box::new(InMemoryCcrStore::new());
        store.put("h", "v");
        assert_eq!(store.get("h"), Some("v".to_string()));
        assert!(!store.is_empty());
    }

    /// ABA test: overwriting a key must NOT allow the stale pre-overwrite
    /// order token to evict the live re-inserted entry.
    ///
    /// Setup: capacity=2.
    ///   1. put("a", ...) → order: [(a,0)]
    ///   2. put("b", ...) → order: [(a,0),(b,1)]
    ///   3. put("a", ...) — overwrite → order: [(a,0),(b,1),(a,2)]; a's
    ///      gen is now 2, so the (a,0) token is a stale tombstone.
    ///   4. put("c", ...) → eviction needed; pops (a,0) — gen mismatch
    ///      (live gen=2 ≠ 0), skip; pops (b,1) — gen match, evict b.
    ///      Now map.len()==1 (<2), insert c. Map: {a,c}.
    ///
    /// Assertion: a is still live (gen-mismatch protected it), b was the
    /// genuinely oldest LIVE entry and was correctly evicted.
    ///
    /// On the baseline FIFO-by-key implementation:
    ///   - put("a") inserts order token "a".
    ///   - put("a") overwrite does NOT push another token (returns early).
    ///   - put("c") triggers eviction; pops "a" (front) → removes live a.
    ///   - Result: a is None (WRONG), b survives (wrong eviction choice).
    #[test]
    fn aba_overwrite_does_not_evict_live_reinserted_entry() {
        let store = InMemoryCcrStore::with_capacity_and_ttl(2, DEFAULT_TTL);
        store.put("a", "first_a");
        store.put("b", "b_val");
        // Overwrite "a" — bumps generation, pushes fresh token.
        store.put("a", "second_a");
        // Adding "c" forces eviction. The stale (a, gen=0) token should
        // be skipped; (b, gen=1) is the oldest live entry and gets evicted.
        store.put("c", "c_val");

        assert_eq!(
            store.len(),
            2,
            "map should have exactly 2 live entries (a and c)"
        );
        assert_eq!(
            store.get("a"),
            Some("second_a".to_string()),
            "'a' was re-inserted (live gen) and must NOT be evicted by stale token"
        );
        assert_eq!(
            store.get("b"),
            None,
            "'b' was the oldest live entry and should have been evicted"
        );
        assert_eq!(
            store.get("c"),
            Some("c_val".to_string()),
            "'c' was just inserted and must be live"
        );
    }

    /// Tombstone-bound test: repeatedly overwriting a small set of keys
    /// under a larger capacity must keep the order queue bounded.
    ///
    /// With capacity=8 and TOMBSTONE_K=2, the queue must never grow
    /// beyond 8*2=16 entries. We perform 10_000 overwrites across 4 keys.
    ///
    /// On the baseline (no compaction), the queue would grow to 10_000 +
    /// initial 4 = 10_004 entries — unbounded memory growth.
    #[test]
    fn tombstone_accumulation_stays_bounded() {
        let cap = 8usize;
        let store = InMemoryCcrStore::with_capacity_and_ttl(cap, DEFAULT_TTL);
        let keys = ["x0", "x1", "x2", "x3"];
        // Initial inserts.
        for k in &keys {
            store.put(k, "init");
        }
        // 10_000 overwrites cycling through the same 4 keys.
        for i in 0..10_000usize {
            let k = keys[i % keys.len()];
            store.put(k, &format!("v{i}"));
        }

        // All 4 live keys must still be readable.
        assert_eq!(store.len(), keys.len(), "all 4 live keys must remain");
        for k in &keys {
            assert!(
                store.get(k).is_some(),
                "key '{k}' must be readable after overwrites"
            );
        }

        // The order queue must be bounded (no unbounded tombstone growth).
        let queue_len = store.order.lock().expect("mutex poisoned in test").len();
        let max_allowed = cap * TOMBSTONE_K;
        assert!(
            queue_len <= max_allowed,
            "order queue length {queue_len} exceeds bound {max_allowed} (cap={cap} × TOMBSTONE_K={TOMBSTONE_K})"
        );
    }

    /// Recovery-invariant flavour: insert N > capacity distinct payloads,
    /// then verify that exactly the `capacity` most-recently inserted keys
    /// are live and all earlier keys have been evicted (no silent live-entry
    /// loss within the retention window).
    ///
    /// This is analogous to the Python `test_ccr_recovery_invariant` check
    /// that no live sentinel resolves to `None`.
    #[test]
    fn most_recent_capacity_entries_survive_eviction() {
        let cap = 10usize;
        let total = 30usize;
        let store = InMemoryCcrStore::with_capacity_and_ttl(cap, DEFAULT_TTL);

        let keys: Vec<String> = (0..total).map(|i| format!("key_{i:04}")).collect();
        let vals: Vec<String> = (0..total).map(|i| format!("payload_{i}")).collect();

        for (k, v) in keys.iter().zip(vals.iter()) {
            store.put(k, v);
        }

        assert_eq!(store.len(), cap, "live count must equal capacity");

        // Evicted keys (older than the last `cap` inserts) must be None.
        for key in keys.iter().take(total - cap) {
            assert_eq!(store.get(key), None, "evicted key '{key}' must be absent");
        }

        // The most-recently inserted `cap` keys must all be present.
        for i in (total - cap)..total {
            assert_eq!(
                store.get(&keys[i]),
                Some(vals[i].clone()),
                "live key '{}' must be present with correct payload",
                keys[i]
            );
        }
    }

    #[test]
    fn concurrent_puts_and_gets_do_not_corrupt() {
        // Smoke test for the concurrent design — N threads each do
        // P puts and P gets against distinct keys. Every key written
        // must be readable afterwards.
        use std::sync::Arc;
        use std::thread;

        let store = Arc::new(InMemoryCcrStore::with_capacity_and_ttl(10_000, DEFAULT_TTL));
        let n_threads = 8;
        let per_thread = 200;

        let mut handles = Vec::new();
        for tid in 0..n_threads {
            let s = store.clone();
            handles.push(thread::spawn(move || {
                for i in 0..per_thread {
                    let key = format!("t{tid}_k{i}");
                    let val = format!("v{tid}_{i}");
                    s.put(&key, &val);
                }
                for i in 0..per_thread {
                    let key = format!("t{tid}_k{i}");
                    let got = s.get(&key);
                    assert_eq!(got, Some(format!("v{tid}_{i}")));
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(store.len(), n_threads * per_thread);
    }

    #[test]
    fn expired_get_does_not_wipe_concurrent_refresh() {
        // Regression for the TOCTOU race fixed in the audit-cleanup PR.
        // Two threads contend on the SAME key:
        //   - Thread A: stores fresh value, then `get` it many times.
        //   - Thread B: keeps re-storing the same key with FRESH
        //     timestamps in a tight loop (simulating a second worker
        //     touching the same payload).
        // With the old 2-step check-then-remove, A's `get` could see
        // an "expired" entry, drop the read lock, and remove B's
        // freshly-inserted entry between drop and remove. With
        // `remove_if`, the predicate runs under the shard write lock,
        // so the race window is closed.
        use std::sync::Arc;
        use std::thread;

        let store = Arc::new(InMemoryCcrStore::with_capacity_and_ttl(
            64,
            Duration::from_millis(20),
        ));
        let key = "shared_key";
        let payload = "fresh";

        // Seed.
        store.put(key, payload);

        let writer = {
            let s = store.clone();
            thread::spawn(move || {
                // 200 fresh re-stores, racing the reader.
                for _ in 0..200 {
                    s.put(key, payload);
                }
            })
        };

        let reader = {
            let s = store.clone();
            thread::spawn(move || {
                let mut hits = 0;
                for _ in 0..200 {
                    if s.get(key).as_deref() == Some(payload) {
                        hits += 1;
                    }
                }
                hits
            })
        };

        writer.join().unwrap();
        let hits = reader.join().unwrap();
        // The entry must be live at the end (writer's last put won).
        assert_eq!(store.get(key).as_deref(), Some(payload));
        // Reader should have observed the live entry the vast majority
        // of the time. Allow some misses on first iterations / TTL
        // transitions but require strong majority.
        assert!(
            hits > 100,
            "reader should mostly observe live entry, hits={hits}"
        );
    }
}
