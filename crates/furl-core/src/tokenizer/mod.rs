//! Token counting for Furl transforms.
//!
//! Mirrors the public surface of the Python `furl_ctx.tokenizers` package:
//! a `Tokenizer` trait, a tiktoken-backed counter for OpenAI / o-series models
//! (via the `tiktoken-rs` crate, which uses the same BPE data files as Python's
//! `tiktoken` and therefore returns byte-identical token IDs), and an estimation
//! fallback for everything else.
//!
//! # Why this exists
//! Counting tokens currently round-trips into Python's `tiktoken` (itself a
//! Rust extension under the hood). For Rust transforms running on the
//! hot path, counting natively avoids the Python-Rust FFI cost and keeps the
//! Rust binary self-contained.
//!
//! # What this is NOT
//! - Not a real Anthropic Claude tokenizer (Anthropic doesn't publish theirs;
//!   estimation matches what the Python implementation does).
//! - SentencePiece — Gemini's tokenizer is SP-based but Google doesn't
//!   publish the model. Falls through to estimation.
//!
//! # What is here
//! - [`TiktokenCounter`] — OpenAI / o-series, byte-equal to Python `tiktoken`.
//! - [`EstimatingCounter`] — last-resort `chars / cpt` fallback for Anthropic
//!   and Gemini, calibrated to match the Python implementation.

mod estimator;
mod registry;
mod tiktoken_impl;

pub use estimator::EstimatingCounter;
pub use registry::{detect_backend, get_tokenizer, Backend};
pub use tiktoken_impl::{TiktokenCounter, TiktokenError};

/// Counts tokens. Implementations must be thread-safe (`Send + Sync`).
///
/// # Conventions (preserved across all built-in implementations)
/// - `count_text("")` returns `0`.
/// - Counts are deterministic for a given input and instance.
/// - For non-empty input, counts are `>= 1`.
pub trait Tokenizer: Send + Sync + std::fmt::Debug {
    /// Number of tokens that this tokenizer assigns to `text`.
    fn count_text(&self, text: &str) -> usize;

    /// Which backend produced the count. Useful for logs and metrics.
    fn backend(&self) -> Backend;
}
