//! Adaptive compression sizing via information saturation detection.
//!
//! This Rust implementation is canonical (the pure-Python
//! `adaptive_sizer.py` original is retired — no cross-language pin
//! remains). Used by `smart_crusher`'s array crushers and the log/search
//! compressors to decide *how many* items to keep — statistically, by
//! detecting the "knee point" of an information saturation curve.
//!
//! # Algorithm overview
//!
//! Three-tier decision:
//! 1. **Fast path**: trivial cases (`n <= 8` → keep all) and near-total
//!    redundancy (≤3 unique-by-simhash → keep that count).
//! 2. **Standard**: Kneedle on cumulative unique-bigram coverage curve.
//!    Coverage stops growing → that's the knee → return that count.
//! 3. **Validation**: zlib-ratio sanity check. If keeping `k` items
//!    produces a much-more-redundant subset than the full set, bump
//!    `k` by 20%.
//!
//! # Implementation notes
//!
//! - `simhash` hashes character 4-grams (codepoint windows) with
//!   [`FxHasher`] and aggregates bits via weighted voting. The original
//!   used one full **MD5 digest per window** — a 10k-line log paid
//!   ~770k MD5 calls inside `compute_optimal_k` (PERF-6). A similarity
//!   fingerprint needs a fast, deterministic, well-mixed 64-bit hash,
//!   not a cryptographic one. Fingerprint VALUES differ from the MD5
//!   era; the clustering semantics (Hamming-distance grouping) are
//!   unchanged and the outputs are pinned by the characterization
//!   tests below + the benchmark ratio floor.
//! - `compute_unique_bigram_curve` operates on whitespace-split words,
//!   deduped as `(u64, u64)` word-hash pairs (PERF-6 — the old
//!   `(String, String)` set allocated two Strings per bigram).
//!   Single-word items emit `(word, "")`; empty items emit `("", "")`.
//! - `find_knee` requires `> 0.05` deviation from the diagonal in
//!   normalized space; threshold is strict (`<` returns None).
//! - `validate_with_zlib` mirrors `zlib.compress(..., level=1)` via
//!   `flate2` (miniz_oxide backend); the 15% ratio-diff threshold
//!   absorbs per-byte encoder drift.

use flate2::write::ZlibEncoder;
use flate2::Compression;
use rustc_hash::{FxHashSet, FxHasher};
use std::hash::Hasher;
use std::io::Write;

/// Compute the optimal number of items to keep via information saturation.
///
/// Direct port of `compute_optimal_k` (Python `adaptive_sizer.py:27-106`).
///
/// # Arguments
///
/// - `items`: string representations of items in importance order.
/// - `bias`: multiplier on the knee point (>1 = keep more, <1 = compress
///   harder).
/// - `min_k`: lower bound on the return value.
/// - `max_k`: upper bound; `None` means "no cap" (i.e. up to `items.len()`).
pub fn compute_optimal_k(items: &[&str], bias: f64, min_k: usize, max_k: Option<usize>) -> usize {
    let n = items.len();
    let effective_max = max_k.unwrap_or(n);

    // Tier 1: fast path.
    if n <= 8 {
        return n.min(effective_max);
    }

    // Near-total redundancy: at most 3 unique groups → keep that many.
    let unique_count = count_unique_simhash(items, 3);
    if unique_count <= 3 {
        let k = min_k.max(unique_count);
        return k.min(effective_max);
    }

    // Tier 2: Kneedle on bigram-coverage curve.
    let curve = compute_unique_bigram_curve(items);
    let mut knee = find_knee(&curve);

    // Diversity ratio: fraction of items that are genuinely unique.
    let diversity_ratio = unique_count as f64 / n as f64;

    knee = match knee {
        None => {
            // No saturation found — scale keep-fraction with diversity.
            // diversity ~1.0 → keep 100%; ~0.0 → keep 30%.
            let keep_fraction = 0.3 + 0.7 * diversity_ratio;
            Some(min_k.max((n as f64 * keep_fraction) as usize))
        }
        Some(k) if diversity_ratio > 0.7 => {
            // Knee found, but high diversity — apply diversity floor so
            // we don't drop below `n * (0.3 + 0.7 * diversity)`.
            let floor = min_k.max((n as f64 * (0.3 + 0.7 * diversity_ratio)) as usize);
            Some(k.max(floor))
        }
        some => some,
    };

    let knee = knee.unwrap_or(min_k); // defensive — knee path always sets Some above

    // Apply bias multiplier. Python: `int(knee * bias)`.
    let mut k = min_k.max((knee as f64 * bias) as usize);
    k = k.min(effective_max);

    // Tier 3: zlib-ratio validation.
    k = validate_with_zlib(items, k, effective_max, 0.15);

    // Final clamp.
    min_k.max(k.min(effective_max))
}

/// Find the knee in a monotonically-increasing curve (Kneedle).
///
/// Direct port of `find_knee` (Python `adaptive_sizer.py:109-154`).
/// Returns the 1-indexed count `knee_idx + 1` so the caller can use it
/// directly as a "keep this many" value.
pub fn find_knee(curve: &[usize]) -> Option<usize> {
    let n = curve.len();
    if n < 3 {
        return None;
    }

    let x_min: usize = 0;
    let x_max: usize = n - 1;
    let y_min = curve[0] as f64;
    let y_max = curve[n - 1] as f64;

    if (y_max - y_min).abs() < f64::EPSILON {
        // Flat curve — all items are identical.
        // Python returns the literal `1`.
        return Some(1);
    }

    let x_range = (x_max - x_min) as f64;
    let y_range = y_max - y_min;

    let mut max_diff: f64 = -1.0;
    let mut knee_idx: Option<usize> = None;

    for (i, &y) in curve.iter().enumerate() {
        let x_norm = (i - x_min) as f64 / x_range;
        let y_norm = (y as f64 - y_min) / y_range;
        let diff = y_norm - x_norm;
        if diff > max_diff {
            max_diff = diff;
            knee_idx = Some(i);
        }
    }

    if max_diff < 0.05 {
        return None;
    }

    knee_idx.map(|i| i + 1)
}

/// Cumulative unique-bigram coverage curve.
///
/// Each item contributes its word-level bigrams; single-word items
/// contribute `(word, "")`, empty items `("", "")`. The curve at index
/// `k` is the running count of unique bigrams after seeing
/// `items[0..=k]`.
///
/// Bigrams dedupe as `(u64, u64)` FxHash word pairs (PERF-6): the old
/// `HashSet<(String, String)>` allocated two owned Strings per bigram.
/// Set cardinality is identical up to 64-bit hash collisions —
/// negligible against the knee detector's coarse geometry.
pub fn compute_unique_bigram_curve(items: &[&str]) -> Vec<usize> {
    let mut seen: FxHashSet<(u64, u64)> = FxHashSet::default();
    let mut curve: Vec<usize> = Vec::with_capacity(items.len());
    let empty_hash = hash_word("");

    for item in items {
        let lower = item.to_lowercase();
        let mut words = lower.split_whitespace();
        match words.next() {
            // Empty item: synthesize `("", "")`.
            None => {
                seen.insert((empty_hash, empty_hash));
            }
            Some(first) => {
                let mut prev = hash_word(first);
                let mut saw_pair = false;
                for w in words {
                    let cur = hash_word(w);
                    seen.insert((prev, cur));
                    prev = cur;
                    saw_pair = true;
                }
                // Single word: synthesize `(word, "")`.
                if !saw_pair {
                    seen.insert((prev, empty_hash));
                }
            }
        }
        curve.push(seen.len());
    }

    curve
}

/// 64-bit SimHash fingerprint of a text string.
///
/// Algorithm:
/// 1. Iterate character 4-grams (sliding codepoint window). For input
///    shorter than 4 chars, the loop runs once with the entire string
///    as the only "gram". Empty input still iterates once with `""`.
/// 2. Hash each gram to a `u64` with [`FxHasher`] over its UTF-8 bytes
///    (PERF-6 — the MD5-per-window original was Python-parity ballast;
///    a similarity fingerprint needs speed + determinism + bit mixing,
///    not collision resistance). No per-window String is allocated:
///    the window encodes into a 16-byte stack buffer.
/// 3. For each bit position 0..64, increment a vote counter when the
///    bit is set, decrement when clear.
/// 4. Final fingerprint: bit `j` is set iff `votes[j] > 0` (strict).
pub fn simhash(text: &str) -> u64 {
    let lower = text.to_lowercase();
    let chars: Vec<char> = lower.chars().collect();
    let n = chars.len();

    // `max(1, n - 3)` windows: for n<=3 a single iteration over the
    // whole (short) string; for n>=4 one window per starting index.
    let iter_count = if n <= 3 { 1 } else { n - 3 };

    let mut votes: [i32; 64] = [0; 64];
    // A 4-char window is at most 16 UTF-8 bytes.
    let mut buf = [0u8; 16];

    for i in 0..iter_count {
        // 4-character window starting at char index i, encoded into the
        // stack buffer (for short input this is the whole string).
        let mut len = 0usize;
        for &c in chars.iter().skip(i).take(4) {
            len += c.encode_utf8(&mut buf[len..]).len();
        }

        let mut hasher = FxHasher::default();
        hasher.write(&buf[..len]);
        let h = hasher.finish();

        for (j, vote) in votes.iter_mut().enumerate() {
            if (h >> j) & 1 == 1 {
                *vote += 1;
            } else {
                *vote -= 1;
            }
        }
    }

    let mut fingerprint: u64 = 0;
    for (j, &v) in votes.iter().enumerate() {
        if v > 0 {
            fingerprint |= 1 << j;
        }
    }
    fingerprint
}

/// FxHash of a word's bytes — the bigram-set element (PERF-6).
#[inline]
fn hash_word(w: &str) -> u64 {
    let mut hasher = FxHasher::default();
    hasher.write(w.as_bytes());
    hasher.finish()
}

/// Hamming distance between two 64-bit SimHash fingerprints.
#[inline]
pub fn hamming_distance(a: u64, b: u64) -> u32 {
    (a ^ b).count_ones()
}

/// Count items with distinct content via SimHash + greedy clustering.
///
/// Direct port of `count_unique_simhash` (Python `adaptive_sizer.py:222-252`).
/// Two items cluster together when their fingerprints are within
/// `threshold` Hamming distance.
pub fn count_unique_simhash(items: &[&str], threshold: u32) -> usize {
    if items.is_empty() {
        return 0;
    }

    let fingerprints: Vec<u64> = items.iter().map(|s| simhash(s)).collect();
    let mut clusters: Vec<u64> = Vec::new();

    for &fp in &fingerprints {
        let mut matched = false;
        for &rep in &clusters {
            if hamming_distance(fp, rep) <= threshold {
                matched = true;
                break;
            }
        }
        if !matched {
            clusters.push(fp);
        }
    }

    clusters.len()
}

/// zlib-based compression-ratio validation of the chosen `k`.
///
/// Direct port of `_validate_with_zlib` (Python `adaptive_sizer.py:255-308`).
/// If the subset `items[..k]` compresses *much* better than the full
/// set, the subset is missing diversity → bump `k` by 20%.
///
/// `tolerance` is the maximum allowed ratio difference (Python default
/// 0.15 = 15%).
pub fn validate_with_zlib(items: &[&str], k: usize, max_k: usize, tolerance: f64) -> usize {
    if k >= items.len() || k >= max_k {
        return k;
    }

    let full_text = items.join("\n");
    let subset_text = items[..k].join("\n");

    // Skip validation for very small content (zlib overhead dominates).
    if full_text.len() < 200 {
        return k;
    }

    let full_compressed = zlib_compressed_len(full_text.as_bytes());
    let subset_compressed = zlib_compressed_len(subset_text.as_bytes());

    let full_ratio = if !full_text.is_empty() {
        full_compressed as f64 / full_text.len() as f64
    } else {
        1.0
    };
    let subset_ratio = if !subset_text.is_empty() {
        subset_compressed as f64 / subset_text.len() as f64
    } else {
        1.0
    };

    let ratio_diff = (full_ratio - subset_ratio).abs();

    if ratio_diff > tolerance {
        // Subset compresses much better than full → bump k by 20%.
        let adjusted = ((k as f64) * 1.2) as usize;
        return adjusted.min(max_k);
    }

    k
}

/// Compress `bytes` with zlib level=1 and return the output length.
///
/// Wraps `flate2::ZlibEncoder` at `Compression::fast()` (level 1).
/// Mirrors Python's `len(zlib.compress(data, level=1))`. miniz_oxide
/// (default flate2 backend) produces DEFLATE streams of similar length
/// to CPython's libz at level 1 — small per-byte drift is absorbed by
/// the 15% ratio-diff tolerance in `validate_with_zlib`.
fn zlib_compressed_len(bytes: &[u8]) -> usize {
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
    // Writes are infallible for an in-memory Vec.
    encoder.write_all(bytes).expect("in-memory write");
    let compressed = encoder.finish().expect("flush");
    compressed.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---------- simhash (characterization of the FxHash fingerprint) ----------
    //
    // PERF-6: the fingerprints below were regenerated when the per-gram
    // hash moved MD5 → FxHash (the MD5 choice was Python-parity; the
    // Python original is retired). The constants pin THIS
    // implementation's determinism — cross-platform, cross-release —
    // not an external reference. Clustering semantics are covered by
    // the count_unique_simhash / compute_optimal_k tests, and end-to-end
    // quality by the benchmark ratio floor (non-regression gated).

    #[test]
    fn simhash_empty_string() {
        // Single iteration over the empty gram.
        assert_eq!(simhash(""), 0xf456d26876d72d91);
    }

    #[test]
    fn simhash_single_char() {
        assert_eq!(simhash("a"), 0xbd8e8067b4be2f50);
    }

    #[test]
    fn simhash_short_strings() {
        // For n <= 3, single iteration: fp = FxHash of the whole string.
        assert_eq!(simhash("ab"), 0x9de70eed801ef68e);
        assert_eq!(simhash("abc"), 0xe7cfbde76661213f);
    }

    #[test]
    fn simhash_n_eq_4_single_iteration() {
        // n=4: max(1, 4-3)=1, single iteration on full string.
        assert_eq!(simhash("abcd"), 0x129d90d0160ec9ca);
    }

    #[test]
    fn simhash_multi_window() {
        // n>=5 → bit voting from multiple grams.
        assert_eq!(simhash("hello"), 0xe42508110121c8a0);
        assert_eq!(simhash("hello world"), 0xb423895069a0a8ac);
    }

    #[test]
    fn simhash_unicode_codepoint_iteration() {
        // "café" is 4 codepoints — a single window over the full string,
        // hashing its UTF-8 bytes (5 bytes: é is 2).
        assert_eq!(simhash("café"), 0xfc2e6df1bc20cdf8);
    }

    #[test]
    fn simhash_lowercases_input() {
        assert_eq!(simhash("ABC"), simhash("abc"));
        assert_eq!(simhash("Hello"), simhash("hello"));
    }

    #[test]
    fn simhash_longer_text() {
        assert_eq!(simhash("The quick brown fox jumps"), 0x6ac81d5154b171cd);
    }

    // ---------- hamming_distance ----------

    #[test]
    fn hamming_distance_zero_identical() {
        assert_eq!(hamming_distance(0, 0), 0);
        assert_eq!(hamming_distance(0xff, 0xff), 0);
    }

    #[test]
    fn hamming_distance_basic() {
        assert_eq!(hamming_distance(0b0000, 0b1111), 4);
        assert_eq!(hamming_distance(0b1010, 0b0101), 4);
        assert_eq!(hamming_distance(0b1100, 0b1010), 2);
    }

    #[test]
    fn hamming_distance_full_64_bits() {
        assert_eq!(hamming_distance(u64::MAX, 0), 64);
    }

    // ---------- count_unique_simhash ----------

    #[test]
    fn count_unique_simhash_empty() {
        assert_eq!(count_unique_simhash(&[], 3), 0);
    }

    #[test]
    fn count_unique_simhash_all_identical() {
        let items = ["abc", "abc", "abc"];
        assert_eq!(count_unique_simhash(&items, 3), 1);
    }

    #[test]
    fn count_unique_simhash_diverse_items() {
        // Three sentences with very different bigram coverage — should
        // simhash to fingerprints with Hamming > 3.
        let items = [
            "the cat sat on the mat",
            "the dog ran in the park",
            "a fish swam in the sea",
        ];
        assert_eq!(count_unique_simhash(&items, 3), 3);
    }

    #[test]
    fn count_unique_simhash_threshold_groups_near_dupes() {
        // Same fingerprint distance — well under threshold.
        let items = ["abc", "abc"];
        assert_eq!(count_unique_simhash(&items, 0), 1);
    }

    // ---------- compute_unique_bigram_curve ----------

    #[test]
    fn bigram_curve_distinct_words() {
        // ["the cat", "the dog", "a fish"] → [1, 2, 3]
        let items = ["the cat", "the dog", "a fish"];
        assert_eq!(compute_unique_bigram_curve(&items), vec![1, 2, 3]);
    }

    #[test]
    fn bigram_curve_single_word_dedup() {
        // ["hello", "world", "hello"] → [1, 2, 2]  (third "hello" dupes)
        let items = ["hello", "world", "hello"];
        assert_eq!(compute_unique_bigram_curve(&items), vec![1, 2, 2]);
    }

    #[test]
    fn bigram_curve_empty_string_contributes_one() {
        // ["", "a", "a b"] → [1, 2, 3]
        // "" → ("", "")
        // "a" → ("a", "")
        // "a b" → ("a", "b")
        let items = ["", "a", "a b"];
        assert_eq!(compute_unique_bigram_curve(&items), vec![1, 2, 3]);
    }

    #[test]
    fn bigram_curve_lowercases_for_dedup() {
        // "Hello" and "hello" should produce the same bigram.
        let items = ["Hello", "hello"];
        assert_eq!(compute_unique_bigram_curve(&items), vec![1, 1]);
    }

    // ---------- find_knee ----------

    #[test]
    fn find_knee_too_short_is_none() {
        assert_eq!(find_knee(&[]), None);
        assert_eq!(find_knee(&[1]), None);
        assert_eq!(find_knee(&[1, 2]), None);
    }

    #[test]
    fn find_knee_flat_curve_returns_one() {
        // y_max == y_min → return 1 (Python literal).
        assert_eq!(find_knee(&[5, 5, 5, 5, 5]), Some(1));
    }

    #[test]
    fn find_knee_concave_curve() {
        // Reference computed via Python: [1,5,8,9,10,10,10,10,10] → 3
        assert_eq!(find_knee(&[1, 5, 8, 9, 10, 10, 10, 10, 10]), Some(3));
    }

    #[test]
    fn find_knee_linear_no_clear_knee() {
        // Diagonal curve → max_diff = 0 < 0.05 → None.
        assert_eq!(find_knee(&[1, 2, 3, 4, 5, 6, 7, 8, 9]), None);
    }

    // ---------- validate_with_zlib ----------

    #[test]
    fn validate_zlib_passthrough_when_k_at_max() {
        // k >= len(items) → no adjustment.
        let items = ["a", "b", "c"];
        assert_eq!(validate_with_zlib(&items, 3, 10, 0.15), 3);
    }

    #[test]
    fn validate_zlib_passthrough_when_total_too_small() {
        // total bytes < 200 → skip validation (per Python).
        let items: [&str; 5] = ["short"; 5];
        assert_eq!(validate_with_zlib(&items, 2, 100, 0.15), 2);
    }

    #[test]
    fn validate_zlib_bumps_k_when_subset_undercompresses() {
        // Counterintuitive: 20 identical lines and 5 identical lines have
        // the same content redundancy, but zlib at level=1 compresses
        // longer redundant text more efficiently per byte. The validator
        // sees a ratio_diff > 0.15 between full and subset → bumps k by
        // 20%. Verified against Python: returns 6 for k=5.
        let items: [&str; 20] = ["the quick brown fox jumps over the lazy dog"; 20];
        let result = validate_with_zlib(&items, 5, 100, 0.15);
        assert_eq!(result, 6, "expected 1.2× bump from 5 to 6");
    }

    #[test]
    fn validate_zlib_passthrough_when_subset_representative() {
        // 20 diverse items with similar per-item compressibility — full
        // and subset get similar ratios → no bump.
        let many: Vec<String> = (0..20)
            .map(|i| {
                format!(
                    "entry id={} payload=item value with content for item number {}",
                    i, i
                )
            })
            .collect();
        let items: Vec<&str> = many.iter().map(|s| s.as_str()).collect();
        let result = validate_with_zlib(&items, 10, 100, 0.15);
        // With 10 of 20 diverse items, ratio_diff should stay under 0.15.
        // Pin to the equality observed; if zlib backend changes shift it,
        // we'll see a clean signal here.
        assert_eq!(result, 10, "expected passthrough for representative subset");
    }

    // ---------- compute_optimal_k (parity with Python) ----------

    #[test]
    fn compute_optimal_k_n_le_8_returns_n() {
        // n<=8, max_k=None → returns n (no cap).
        let items = ["a", "b", "c", "d", "e"];
        assert_eq!(compute_optimal_k(&items, 1.0, 3, None), 5);
    }

    #[test]
    fn compute_optimal_k_n_le_8_max_k_none_returns_n() {
        // Fast path, no cap: max_k=None must return n.
        let items = ["a", "b", "c", "d", "e"];
        assert_eq!(compute_optimal_k(&items, 1.0, 1, None), 5);
    }

    #[test]
    fn compute_optimal_k_n_le_8_max_k_greater_than_n_returns_n() {
        // max_k > n: cap doesn't bite, still return n.
        let items = ["a", "b", "c", "d", "e"];
        assert_eq!(compute_optimal_k(&items, 1.0, 1, Some(10)), 5);
    }

    #[test]
    fn compute_optimal_k_n_le_8_respects_max_k_when_less_than_n() {
        // Fast path MUST apply max_k cap: n=5, max_k=2 → must return 2, not 5.
        let items = ["a", "b", "c", "d", "e"];
        assert_eq!(compute_optimal_k(&items, 1.0, 1, Some(2)), 2);
    }

    #[test]
    fn compute_optimal_k_low_diversity_returns_unique_count() {
        // 10 identical → unique=1 → max(min_k=3, 1) = 3.
        let items: [&str; 10] = ["abc"; 10];
        assert_eq!(compute_optimal_k(&items, 1.0, 3, None), 3);
    }

    #[test]
    fn compute_optimal_k_all_unique_keeps_all() {
        // 20 distinct items, no knee, diversity_ratio=1.0 → keep ~100% → 20.
        let items: Vec<String> = (0..20)
            .map(|i| format!("unique item number {} with some long content", i))
            .collect();
        let refs: Vec<&str> = items.iter().map(|s| s.as_str()).collect();
        assert_eq!(compute_optimal_k(&refs, 1.0, 3, None), 20);
    }

    #[test]
    fn compute_optimal_k_respects_max_k() {
        let items: Vec<String> = (0..20).map(|i| format!("item {}", i)).collect();
        let refs: Vec<&str> = items.iter().map(|s| s.as_str()).collect();
        let k = compute_optimal_k(&refs, 1.0, 3, Some(10));
        assert!(k <= 10, "k={} should be ≤ max_k=10", k);
    }

    #[test]
    fn compute_optimal_k_respects_min_k() {
        // Force a path that would return fewer than min_k by pinning
        // tons of identical items + high min_k.
        let items: [&str; 20] = ["abc"; 20];
        let k = compute_optimal_k(&items, 1.0, 5, None);
        assert_eq!(k, 5);
    }

    #[test]
    fn compute_optimal_k_bias_keeps_more() {
        // Higher bias should give >= the unbiased k.
        let items: Vec<String> = (0..30).map(|i| format!("item content {}", i)).collect();
        let refs: Vec<&str> = items.iter().map(|s| s.as_str()).collect();
        let k_low = compute_optimal_k(&refs, 0.7, 3, None);
        let k_mid = compute_optimal_k(&refs, 1.0, 3, None);
        let k_high = compute_optimal_k(&refs, 1.5, 3, None);
        assert!(
            k_low <= k_mid,
            "bias 0.7 → {} should be ≤ bias 1.0 → {}",
            k_low,
            k_mid
        );
        assert!(
            k_mid <= k_high,
            "bias 1.0 → {} should be ≤ bias 1.5 → {}",
            k_mid,
            k_high
        );
    }
}
