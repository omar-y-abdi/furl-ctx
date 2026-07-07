//! TEST-8 (ARCH-6): cross-language tokenizer parity pins.
//!
//! The same corpus + expected counts are pinned in
//! `tests/test_tokenizer_rust_parity.py` on the Python side. The
//! expected values were computed ONCE with Python `tiktoken` (the
//! reference implementation) and with the shared estimation formula
//! `max(1, int(chars / cpt + 0.5))`; each side asserts against the same
//! constants, so a drift on either side of the FFI fails its suite —
//! the two counts are transitively pinned to agree.
//!
//! Families covered (the ones that SHOULD agree — see the divergence
//! notes in `src/tokenizer/registry.rs` for the ones that don't):
//! - OpenAI tiktoken: `gpt-4o` (o200k_base) and `gpt-4` (cl100k_base).
//! - Anthropic tiktoken: `claude-*` (o200k_base) — byte-identical to
//!   gpt-4o counts (Q1, same encoding on both sides).
//! - Gemini/Cohere estimation at 4.0 chars/token.

use furl_core::tokenizer::get_tokenizer;

/// Plain ASCII with digits and punctuation.
const ASCII: &str = "The quick brown fox jumps over the lazy dog. 0123456789 taxes=42%";

/// CJK across three scripts (31 Unicode scalars).
const CJK: &str = "东京タワーは高いです。北京烤鸭很好吃。한국어 텍스트도 있다.";

/// Emoji incl. a ZWJ family sequence (60 Unicode scalars).
const EMOJI: &str =
    "Deploy status: ✅ rocket 🚀 then 👨\u{200d}👩\u{200d}👧\u{200d}👦 family emoji with ZWJ";

/// Code snippet (131 chars).
const CODE: &str = "def count(xs):\n    return {k: len(v) for k, v in xs.items()}  # O(n)\nif __name__ == \"__main__\":\n    print(count({\"a\": [1, 2, 3]}))\n";

/// Deterministic ~100KB log-shaped blob — identical construction on the
/// Python side (1819 lines, 100,045 chars, ASCII-only so bytes == chars).
fn blob_100kb() -> String {
    let mut blob = String::new();
    let mut i: usize = 0;
    while blob.len() < 100_000 {
        blob.push_str(&format!(
            "Line {i:04}: status=ok latency_ms={:03} path=/api/v1/items\n",
            i % 250
        ));
        i += 1;
    }
    blob
}

/// (corpus item, gpt-4o/o200k count, gpt-4/cl100k count, est@3.5, est@4.0)
/// — reference counts computed with Python tiktoken 0.x / the estimation
/// formula; pinned verbatim in the Python twin test.
fn corpus() -> Vec<(&'static str, String, usize, usize, usize, usize)> {
    vec![
        ("ascii", ASCII.to_string(), 19, 19, 19, 16),
        ("cjk", CJK.to_string(), 24, 34, 9, 8),
        ("emoji", EMOJI.to_string(), 25, 33, 17, 15),
        ("code", CODE.to_string(), 49, 49, 37, 33),
        ("blob_100kb", blob_100kb(), 34561, 34561, 28584, 25011),
    ]
}

#[test]
fn blob_construction_matches_python_twin() {
    // If this changes, the pinned counts below are for a different blob —
    // the Python twin asserts the same two numbers.
    let blob = blob_100kb();
    assert_eq!(blob.chars().count(), 100_045);
    assert_eq!(blob.lines().count(), 1_819);
}

#[test]
fn tiktoken_counts_match_python_reference() {
    let gpt4o = get_tokenizer("gpt-4o");
    let gpt4 = get_tokenizer("gpt-4");
    let claude = get_tokenizer("claude-sonnet-4-6");
    for (name, text, want_4o, want_4, _, _) in corpus() {
        assert_eq!(gpt4o.count_text(&text), want_4o, "gpt-4o/o200k: {name}");
        assert_eq!(gpt4.count_text(&text), want_4, "gpt-4/cl100k: {name}");
        // claude-* uses o200k_base (Q1) — byte-identical to gpt-4o.
        assert_eq!(claude.count_text(&text), want_4o, "claude/o200k: {name}");
    }
}

#[test]
fn estimation_counts_match_python_formula() {
    // Gemini/Cohere → 4.0 chars/token. Python's fixed-ratio
    // EstimatingTokenCounter computes `max(1, int(chars / cpt + 0.5))` —
    // same constants pinned there.
    // (claude-* now uses tiktoken o200k_base (Q1) and is covered by
    // tiktoken_counts_match_python_reference above.)
    let gemini = get_tokenizer("gemini-1.5-pro");
    let cohere = get_tokenizer("command-r-plus");
    for (name, text, _, _, _, want_40) in corpus() {
        assert_eq!(gemini.count_text(&text), want_40, "gemini est@4.0: {name}");
        assert_eq!(cohere.count_text(&text), want_40, "cohere est@4.0: {name}");
    }
}
