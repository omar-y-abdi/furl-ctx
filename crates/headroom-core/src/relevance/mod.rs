//! Relevance scoring — Rust port of `headroom/relevance/`.
//!
//! Used by SmartCrusher's planning layer to decide which items in a tool
//! output match the user's query (the user's recent prompts plus the
//! assistant's tool-call argument JSON, joined). Items above a relevance
//! threshold are pinned into `keep_indices`.
//!
//! # Scorer ladder
//!
//! 1. **BM25** (`bm25`): keyword overlap with TF-IDF + length
//!    normalization. No ML deps. Excellent for exact-match cases (UUIDs,
//!    field=value filters). Tool-call arguments are usually literal
//!    keywords that appear verbatim in the response, so BM25 catches
//!    most cases.
//! 2. **Hybrid** (`hybrid`): wraps BM25 with a match boost so
//!    single-term matches clear the relevance threshold. BM25-only (the
//!    embedding-fusion path was removed with the `embeddings` feature).
//!
//! Each scorer implements the `RelevanceScorer` trait — same surface
//! as Python's abstract base class.

mod base;
mod bm25;
mod hybrid;

pub use base::{RelevanceScore, RelevanceScorer};
pub use bm25::BM25Scorer;
pub use hybrid::HybridScorer;
