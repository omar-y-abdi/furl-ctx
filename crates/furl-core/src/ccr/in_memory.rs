//! Process-local CCR store backed by DashMap. Eviction uses generation-stamped FIFO tokens: stale tokens cannot evict
//! refreshed entries, and the queue compacts when tombstones exceed 2× capacity. Recovery is not durable across restarts.

use std::collections::VecDeque;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Mutex,
};
use std::time::{Duration, Instant};

use dashmap::DashMap;

use crate::ccr::{CcrStore, DEFAULT_CAPACITY, DEFAULT_TTL};

/// Tombstone-compaction multiplier.
const TOMBSTONE_K: usize = 2;

/// In-memory CCR store backed by [`DashMap`] for sharded concurrent access. - **TTL**: 30 minutes by default (session-scale — see `DEFAULT_TTL`).
/// Entries past their TTL are dropped on the next `get` (lazy expiry — no background reaper thread). - **Capacity**: 1000 entries by default.
pub struct InMemoryCcrStore {
    map: DashMap<String, Entry>,
    /// FIFO insertion order with generation tokens. Tokens whose `generation` doesn't match
    /// the live entry's generation are harmless tombstones: the eviction loop skips them.
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
    /// Default: 1000 entries, 30-minute TTL.
    pub fn new() -> Self {
        Self::with_capacity_and_ttl(DEFAULT_CAPACITY, DEFAULT_TTL)
    }

    /// # Panics Panics when `capacity == 0`.
    pub fn with_capacity_and_ttl(capacity: usize, ttl: Duration) -> Self {
        assert!(
            capacity >= 1,
            "InMemoryCcrStore capacity must be >= 1 (a capacity-0 store would still hold one entry)"
        );
        Self {
            map: DashMap::with_capacity(capacity),
            order: Mutex::new(VecDeque::with_capacity(capacity)),
            ttl,
            capacity,
            generation: AtomicU64::new(0),
        }
    }

    /// Sweep the order queue, popping tokens until `map.len() < capacity`. LOCK ORDER: caller must hold the `order` mutex (passed in as `guard`)
    /// BEFORE any DashMap operation. We never hold a DashMap ref-guard across `order.lock()` — that would invert the order and deadlock.
    fn evict_until_under_capacity(&self, guard: &mut VecDeque<(String, u64)>) {
        while self.map.len() >= self.capacity {
            let Some((oldest_key, oldest_gen)) = guard.pop_front() else {
                break;
            };
            // Only remove the entry if the stored generation matches the token's generation. A generation
            // mismatch means this token is a stale tombstone from before an overwrite — skip it.
            self.map
                .remove_if(&oldest_key, |_, entry| entry.generation == oldest_gen);
            // Whether or not we removed: check map.len() again (the while condition). If
            // we skipped a tombstone the count didn't change and we'll try the next token.
        }
    }

    /// Compact the order queue by rebuilding it from live entries sorted by generation ascending. Called
    /// when `order.len() > capacity * TOMBSTONE_K`. Must be called with the order mutex already held.
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
        // Claim a fresh generation *before* touching either the map or the order queue. This is a
        // global counter — each put (insert or refresh) gets a unique, monotonically increasing stamp.
        let gen = self.generation.fetch_add(1, Ordering::Relaxed);

        // Acquire the order mutex before any DashMap shard lock; never hold `RefMut` across `order.lock()`. Same-payload puts refresh
        // generation; conflicting payloads under one hash delete the binding and return a loud miss rather than foreign bytes.
        enum Existing {
            Refreshed,
            Collision,
        }
        let outcome = if let Some(mut existing) = self.map.get_mut(hash) {
            if existing.payload == payload {
                // Idempotent refresh, fully under the shard write-lock.
                existing.inserted = Instant::now();
                existing.generation = gen;
                Some(Existing::Refreshed)
            } else {
                Some(Existing::Collision)
            }
            // RefMut guard drops here, releasing the shard write-lock.
        } else {
            None
        };
        match outcome {
            Some(Existing::Refreshed) => {
                // Push a fresh token for the updated generation so that the OLD token becomes a harmless tombstone
                // (gen-mismatch skip). Lock order: shard already released above, so order→shard is maintained.
                let mut guard = self.order.lock().expect("ccr order mutex poisoned");
                guard.push_back((hash.to_string(), gen));
                if guard.len() > self.capacity * TOMBSTONE_K {
                    self.compact_order_queue(&mut guard);
                }
                return;
            }
            Some(Existing::Collision) => {
                // Drop the ambiguous binding.
                self.map
                    .remove_if(hash, |_, entry| entry.payload != payload);
                tracing::error!(
                    hash = %hash,
                    "CCR hash collision: same key, different payload; dropping the \
                     ambiguous binding so retrieval loud-misses instead of serving \
                     foreign content"
                );
                return;
            }
            None => {}
        }
        // Fall-through: key was absent (new entry) or was concurrently removed between our `get_mut` and now.

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
        // Record in FIFO order. Even if a concurrent insert beat us (prev.is_some()), our `gen`
        // token is fresher and the stale concurrent token will be skipped by the gen-mismatch check.
        guard.push_back((hash.to_string(), gen));

        // Compact if tombstones have accumulated.
        if guard.len() > self.capacity * TOMBSTONE_K {
            self.compact_order_queue(&mut guard);
        }
    }

    fn get(&self, hash: &str) -> Option<String> {
        // Read path shard read-lock, check TTL, clone payload out. No global lock involvement at all distinct hashes hash to distinct shards and
        // never contend. between dropping the read lock and calling `remove`, a concurrent `put()` of the same hash with a fresh timestamp could land.
        if let Some(entry) = self.map.get(hash) {
            if entry.inserted.elapsed() <= self.ttl {
                return Some(entry.payload.clone());
            }
        } else {
            return None;
        }
        // Out-of-band path: the entry exists and looks expired.
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
        // Honest live count (COR-41): skip entries past their TTL that lazy expiry hasn't reaped yet
        self.map
            .iter()
            .filter(|kv| kv.value().inserted.elapsed() <= self.ttl)
            .count()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_ttl_is_session_scale() {
        // Engine P0-3: agentic sessions outlive 5 minutes — an entry that expires mid-session silently converts "lossless + retrieval" into lossy.
        assert_eq!(DEFAULT_TTL, Duration::from_secs(1800));
    }

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
    fn put_same_key_same_payload_refreshes_idempotently() {
        // Content-addressed dedup: re-storing the SAME payload under the same key is the normal idempotent
        // path (generation + timestamp refresh only). It stays resolvable and is NEVER treated as a collision.
        let store = InMemoryCcrStore::new();
        store.put("h", "same-content");
        store.put("h", "same-content");
        assert_eq!(store.get("h"), Some("same-content".to_string()));
        assert_eq!(store.len(), 1);
    }

    #[test]
    fn put_collision_different_payload_drops_binding() {
        // NEW CONTRACT (T3) a same-key / DIFFERENT-payload put is a true hash collision. The store must NOT
        // silently overwrite that let a dropped row recover as ANOTHER row's content (silent corruption).
        let store = InMemoryCcrStore::new();
        store.put("h", "first");
        store.put("h", "second"); // same key, different payload = collision
        assert_eq!(
            store.get("h"),
            None,
            "collision must drop the binding so retrieval loud-misses"
        );
        assert_eq!(
            store.len(),
            0,
            "neither payload is served after a collision"
        );
    }

    #[test]
    fn legacy_twelve_hex_key_round_trips_alongside_wide_key() {
        // Backward compatibility: existing stores hold 12-hex keys emitted before the recovery key was widened to 24 hex.
        let store = InMemoryCcrStore::new();
        store.put("09659eb7ee43", r#"["legacy-row"]"#); // 12-hex legacy
        assert_eq!(
            store.get("09659eb7ee43"),
            Some(r#"["legacy-row"]"#.to_string())
        );
        store.put("09659eb7ee438a05005562f5", r#"["current-row"]"#); // 24-hex current
        assert_eq!(
            store.get("09659eb7ee438a05005562f5"),
            Some(r#"["current-row"]"#.to_string())
        );
        assert_eq!(store.len(), 2, "both widths coexist");
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
    fn len_skips_expired_entries() {
        // COR-41: `CcrStore::len` is documented as the number of LIVE entries, and `get` refuses expired
        // ones — so entries past their TTL that lazy expiry has not reaped yet must not be counted.
        let store = InMemoryCcrStore::with_capacity_and_ttl(10, Duration::from_millis(10));
        store.put("a", "1");
        store.put("b", "2");
        assert_eq!(store.len(), 2);
        std::thread::sleep(Duration::from_millis(25));
        // No get() has touched the entries; the raw map still holds 2.
        assert_eq!(store.len(), 0, "len() must not count expired entries");
    }

    #[test]
    #[should_panic(expected = "capacity")]
    fn capacity_zero_is_rejected() {
        // COR-41: capacity-0 used to hold one entry anyway (the evict-then-insert order
        // leaves the newest put live), silently violating "capacity bounds the live set".
        let _ = InMemoryCcrStore::with_capacity_and_ttl(0, DEFAULT_TTL);
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
    }

    /// Refreshing `a` must invalidate its stale FIFO token. With capacity 2, inserting
    /// `c` skips stale `(a, old_gen)`, evicts live `b`, and keeps refreshed `a`.
    #[test]
    fn aba_refresh_does_not_evict_live_reinserted_entry() {
        let store = InMemoryCcrStore::with_capacity_and_ttl(2, DEFAULT_TTL);
        store.put("a", "a_val");
        store.put("b", "b_val");
        // Refresh "a" with the SAME payload — bumps generation, pushes fresh
        // token (a DIFFERENT payload would be a collision and drop the entry).
        store.put("a", "a_val");
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
            Some("a_val".to_string()),
            "'a' was refreshed (live gen) and must NOT be evicted by stale token"
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

    /// Tombstone-bound test repeatedly refreshing a small set of keys under a larger capacity must keep the order queue bounded.
    /// With capacity=8 and TOMBSTONE_K=2, the queue must never grow beyond 8*2=16 entries. each refresh still pushes an order token
    #[test]
    fn tombstone_accumulation_stays_bounded() {
        let cap = 8usize;
        let store = InMemoryCcrStore::with_capacity_and_ttl(cap, DEFAULT_TTL);
        let keys = ["x0", "x1", "x2", "x3"];
        // Each key keeps ONE stable payload (content-addressed): re-putting it
        // is an idempotent refresh, not a collision.
        let payloads = ["p0", "p1", "p2", "p3"];
        // Initial inserts.
        for (k, p) in keys.iter().zip(payloads.iter()) {
            store.put(k, p);
        }
        // 10_000 same-payload refreshes cycling through the same 4 keys.
        for i in 0..10_000usize {
            let j = i % keys.len();
            store.put(keys[j], payloads[j]);
        }

        // All 4 live keys must still be readable.
        assert_eq!(store.len(), keys.len(), "all 4 live keys must remain");
        for k in &keys {
            assert!(
                store.get(k).is_some(),
                "key '{k}' must be readable after refreshes"
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

    /// Recovery-invariant flavour: insert N > capacity distinct payloads, then verify that exactly the `capacity` most-recently
    /// inserted keys are live and all earlier keys have been evicted (no silent live-entry loss within the retention window).
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
        // Smoke test for the concurrent design — N threads each do P puts and P gets against distinct keys. Every key written must be readable afterwards.
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
        // Regression for the TOCTOU race fixed in the audit-cleanup PR. Two threads
        // contend on the SAME key: - Thread A stores fresh value, then `get` it many times.
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
        // Reader should have observed the live entry the vast majority of the time. Allow
        // some misses on first iterations / TTL transitions but require strong majority.
        assert!(
            hits > 100,
            "reader should mostly observe live entry, hits={hits}"
        );
    }
}
