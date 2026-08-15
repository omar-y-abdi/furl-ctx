//! Native token counting for Rust transforms avoids Python FFI. Tiktoken handles OpenAI/o-series;
//! unsupported vendor tokenizers use the same calibrated character estimator as Python.

mod estimator;
mod registry;
mod tiktoken_impl;

pub use estimator::EstimatingCounter;
pub use registry::{detect_backend, get_tokenizer, Backend};
pub use tiktoken_impl::{TiktokenCounter, TiktokenError};

/// Counts tokens.
pub trait Tokenizer: Send + Sync + std::fmt::Debug {
    /// Number of tokens that this tokenizer assigns to `text`.
    fn count_text(&self, text: &str) -> usize;

    /// Which backend produced the count. Useful for logs and metrics.
    fn backend(&self) -> Backend;
}
