//! Model-name → tokenizer dispatch.
//!
//! Mirrors `MODEL_PATTERNS` in `headroom/tokenizers/registry.py`. Two
//! backends in priority order:
//!
//! 1. **Tiktoken** — OpenAI / o-series via `tiktoken-rs`. Byte-identical to
//!    Python `tiktoken`.
//! 2. **Estimation** — `chars / cpt` fallback for Anthropic Claude (3.5),
//!    Gemini / Cohere / Command (4.0), and everything else (4.0).

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
/// Patterns and ordering match `headroom.tokenizers.registry.MODEL_PATTERNS`
/// for the families this stage supports. Anything outside the OpenAI BPE
/// family lands in `Estimation`. Use [`get_tokenizer`] for the real
/// dispatch.
pub fn detect_backend(model: &str) -> Backend {
    let m = model.to_ascii_lowercase();

    // OpenAI BPE-tokenized families (gpt-3.5/4/4o + o1/o3 reasoning + embeddings + legacy davinci/curie/babbage/ada + code-).
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
        for m in [
            "claude-haiku-4-5-20251001",
            "claude-3-opus",
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
    fn case_insensitive() {
        assert_eq!(detect_backend("GPT-4o"), Backend::Tiktoken);
        assert_eq!(detect_backend("Claude-haiku"), Backend::Estimation);
    }

    #[test]
    fn estimator_density_per_family() {
        // Round-trip through the public dispatch and check that the chosen
        // estimator behaves with the right density. We can't introspect the
        // trait object's chars_per_token directly, so we use a known-length
        // string and back-compute.
        let claude = get_tokenizer("claude-3-opus");
        // 3.5 chars/token: 35 chars -> 10 tokens.
        assert_eq!(claude.count_text(&"a".repeat(35)), 10);

        let gemini = get_tokenizer("gemini-1.5-pro");
        // 4.0 chars/token: 40 chars -> 10 tokens.
        assert_eq!(gemini.count_text(&"a".repeat(40)), 10);
    }

    #[test]
    fn non_openai_models_dispatch_to_estimation_backend() {
        // The full dispatch (not just detect_backend) must hand
        // non-OpenAI families the estimator.
        let t = get_tokenizer("command-r-plus");
        assert_eq!(t.backend(), Backend::Estimation);
        let t2 = get_tokenizer("claude-3-opus");
        assert_eq!(t2.backend(), Backend::Estimation);
    }
}
