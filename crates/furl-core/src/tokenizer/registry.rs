//! Model-name → tokenizer dispatch.
//!
//! Mirrors `MODEL_PATTERNS` in `furl_ctx/tokenizers/registry.py` — the
//! pattern → backend mapping is the same on both sides (post-excision
//! both registries are tiktoken + estimation only; the HF and
//! mistral-common backends are gone). Two backends in priority order:
//!
//! 1. **Tiktoken** — OpenAI / o-series via `tiktoken-rs`. Byte-identical to
//!    Python `tiktoken` (pinned by `tests/tokenizer_python_parity.rs`,
//!    TEST-8).
//! 2. **Estimation** — `chars / cpt` fallback for Gemini / Cohere / Command
//!    (4.0) and everything else (4.0). Claude-* now uses tiktoken o200k_base
//!    (Q1) and is byte-identical to gpt-4o on both sides of the FFI.
//!
//! # Known cross-language divergences (ARCH-6)
//!
//! For the families above the two sides agree (TEST-8 pins them). Two
//! deliberate divergences remain — the same model name can count
//! differently across the FFI:
//!
//! 1. **Unknown-model estimation**: Python's default
//!    `EstimatingTokenCounter()` AUTO-detects a density (4.0 text /
//!    3.5 code / 3.2 JSON, sampled prefix) and adds URL/UUID overhead;
//!    this registry uses a FIXED 4.0 for unknown models. Code/JSON-ish
//!    content on an unrecognized model counts higher here than in
//!    Python.
//! 2. **Legacy OpenAI encoding corners**: unknown members of the legacy
//!    completion families (e.g. `davinci-002`, `babbage-002`) fall to
//!    Python's `DEFAULT_ENCODING` (`cl100k_base`) but match the
//!    `davinci`/`babbage` prefixes here (`r50k_base`). The exact names
//!    in Python's `MODEL_TO_ENCODING` agree on both sides.
//!
//! **No longer a divergence (Q1):** `claude-*` was previously estimation
//! (3.5 cpt) on both sides; it is now tiktoken o200k_base on both sides —
//! byte-identical counts across the FFI (same corpus, same BPE tables).

use super::{EstimatingCounter, TiktokenCounter, Tokenizer};

/// Which family of tokenizer was selected for a model.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    /// Real BPE via `tiktoken-rs`. Byte-identical to Python `tiktoken`.
    Tiktoken,
    /// Character-density estimation (chars/token formula).
    Estimation,
}

/// Pick a backend purely from the model name.
///
/// Patterns and ordering match `furl_ctx.tokenizers.registry.MODEL_PATTERNS`
/// for the families this stage supports. Anything outside the OpenAI BPE
/// family lands in `Estimation`. Use [`get_tokenizer`] for the real
/// dispatch.
pub fn detect_backend(model: &str) -> Backend {
    let m = model.to_ascii_lowercase();

    // OpenAI BPE-tokenized families (gpt-3.5/4/4o + o1/o3 reasoning + embeddings + legacy davinci/curie/babbage/ada + code-)
    // plus Claude (o200k_base, Q1 — same encoding as gpt-4o, byte-identical counts).
    if m.starts_with("gpt-4o")
        || m.starts_with("gpt-4")
        || m.starts_with("gpt-3.5")
        || m.starts_with("o1")
        || m.starts_with("o3")
        || m.starts_with("text-embedding")
        || m.starts_with("text-davinci")
        || m.starts_with("davinci")
        || m.starts_with("curie")
        || m.starts_with("babbage")
        || m.starts_with("ada")
        || m.starts_with("code-")
        || m.starts_with("claude-")
    {
        return Backend::Tiktoken;
    }

    Backend::Estimation
}

/// Return a tokenizer for `model`. Resolution order:
/// 1. Tiktoken for OpenAI / o-series families.
/// 2. Estimation, with density calibrated per family (Claude → 3.5, Gemini /
///    Cohere / Command → 4.0, otherwise 4.0).
pub fn get_tokenizer(model: &str) -> Box<dyn Tokenizer> {
    match detect_backend(model) {
        Backend::Tiktoken => match TiktokenCounter::for_model(model) {
            Ok(t) => Box::new(t),
            Err(_) => Box::new(default_estimator_for(model)),
        },
        Backend::Estimation => Box::new(default_estimator_for(model)),
    }
}

fn default_estimator_for(model: &str) -> EstimatingCounter {
    // Note: claude-* reaches here only if TiktokenCounter::for_model fails
    // (which is unreachable in practice — o200k_base is compiled in). The
    // 3.5 branch is kept as a cold-path mirror of Python's ImportError
    // fallback (EstimatingTokenCounter(chars_per_token=3.5)) so the two
    // sides degrade identically if the BPE table ever fails to init.
    let m = model.to_ascii_lowercase();
    if m.starts_with("claude-") {
        EstimatingCounter::new(3.5)
    } else if m.starts_with("gemini") || m.starts_with("palm") || m.starts_with("command") {
        EstimatingCounter::new(4.0)
    } else {
        EstimatingCounter::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn openai_models_pick_tiktoken() {
        for m in [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
            "o1-preview",
            "o3-mini",
            "text-embedding-3-small",
            "text-davinci-003",
            "davinci",
            "babbage-002",
            "code-davinci-002",
        ] {
            assert_eq!(detect_backend(m), Backend::Tiktoken, "{m}");
        }
    }

    #[test]
    fn non_openai_models_fall_through_to_estimation() {
        // claude-* now uses Tiktoken (o200k_base, Q1) — excluded here.
        for m in [
            "gemini-1.5-pro",
            "command-r-plus",
            "llama-3-70b",
            "mistral-large",
            "qwen-72b",
            "made-up-model-name",
        ] {
            assert_eq!(detect_backend(m), Backend::Estimation, "{m}");
        }
    }

    #[test]
    fn claude_models_pick_tiktoken() {
        // claude-* routes to tiktoken o200k_base (Q1).
        for m in [
            "claude-3-opus",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ] {
            assert_eq!(detect_backend(m), Backend::Tiktoken, "{m}");
        }
    }

    #[test]
    fn case_insensitive() {
        assert_eq!(detect_backend("GPT-4o"), Backend::Tiktoken);
        // claude-* now uses Tiktoken (o200k_base, Q1).
        assert_eq!(detect_backend("Claude-haiku"), Backend::Tiktoken);
    }

    #[test]
    fn estimator_density_per_family() {
        // Round-trip through the public dispatch and check that the chosen
        // estimator behaves with the right density. We can't introspect the
        // trait object's chars_per_token directly, so we use a known-length
        // string and back-compute.

        // claude-* now uses tiktoken o200k_base (Q1) — verify via backend()
        // and a known corpus count. 35 repeated 'a's → 5 tokens in o200k_base
        // (BPE merges runs; confirmed with Python tiktoken 0.x).
        let claude = get_tokenizer("claude-3-opus");
        assert_eq!(claude.backend(), Backend::Tiktoken);
        assert_eq!(claude.count_text(&"a".repeat(35)), 5);

        let gemini = get_tokenizer("gemini-1.5-pro");
        // 4.0 chars/token: 40 chars -> 10 tokens.
        assert_eq!(gemini.count_text(&"a".repeat(40)), 10);
    }

    #[test]
    fn non_openai_models_dispatch_to_estimation_backend() {
        // The full dispatch (not just detect_backend) must hand
        // non-tiktoken families the estimator.
        // (claude-* is now Tiktoken, verified by estimator_density_per_family.)
        let t = get_tokenizer("command-r-plus");
        assert_eq!(t.backend(), Backend::Estimation);
    }
}
