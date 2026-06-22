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
//! 2. **Embedding** (future commit): sentence-transformer ONNX model
//!    for semantic matching when query and items use different
//!    vocabularies.
//! 3. **Hybrid** (future commit): combines BM25 and embedding signals.
//!
//! Each scorer implements the `RelevanceScorer` trait — same surface
//! as Python's abstract base class.

mod base;
mod bm25;
// `embedding` is compiled only with the `embeddings` feature — it pulls
// `fastembed`/`ort` (ONNX Runtime). With the feature off, `HybridScorer`
// runs BM25-only and the `"embedding"` scorer tier is unavailable.
#[cfg(feature = "embeddings")]
mod embedding;
mod hybrid;

pub use base::{RelevanceScore, RelevanceScorer};
pub use bm25::BM25Scorer;
#[cfg(feature = "embeddings")]
pub use embedding::EmbeddingScorer;
pub use hybrid::HybridScorer;
