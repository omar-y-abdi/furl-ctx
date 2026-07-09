//! Compression transforms — Rust ports of `furl_ctx.transforms.*`.
//!
//! # Guiding principle: information preservation > aggressive compression
//!
//! When in doubt, prefer keeping bytes. These Rust implementations are
//! canonical — the `furl_ctx.transforms.*` Python modules are thin shims
//! wrapping this crate (the pure-Python originals and their parity
//! fixtures are retired). The spec is the recovery contract the test
//! suites exercise: anything a transform drops or hides must remain
//! recoverable (CCR store write + surfaced marker), and output grammar
//! is pinned by the in-crate unit tests plus the Python wrapper tests.
//!
//! Observability is the escape hatch: every transform returns a sidecar
//! `Stats` struct with the granular metrics Python doesn't emit (e.g. which
//! files were dropped, how many context lines were trimmed, per-file hunk
//! drop counts). These flow through `tracing` spans for OTel scraping in
//! prod and are returned alongside the parity-equal output for tests.

pub mod adaptive_sizer;
pub mod anchor_selector;
pub mod detection;
pub mod diff_compressor;
pub mod log_compressor;
pub mod search_compressor;
pub mod smart_crusher;
pub mod tag_protector;
pub mod text_crusher;
pub mod unidiff_detector;

pub use detection::{detect, ContentType, DetectionResult};
pub use diff_compressor::{
    DiffCompressionResult, DiffCompressor, DiffCompressorConfig, DiffCompressorStats,
};
pub use log_compressor::{
    LogCompressionResult, LogCompressor, LogCompressorConfig, LogCompressorStats, LogFormat,
    LogLevel,
};
pub use search_compressor::{
    SearchCompressionResult, SearchCompressor, SearchCompressorConfig, SearchCompressorStats,
};
pub use tag_protector::{is_known_html_tag, protect_tags, restore_tags};
pub use text_crusher::{TextCrushResult, TextCrusher, TextCrusherConfig, TextCrusherStats};
pub use unidiff_detector::is_diff;
