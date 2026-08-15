//! Compression transforms Rust ports of `furl_ctx.transforms.*`. information preservation > aggressive compression When in doubt, prefer keeping bytes. anything a transform
//! drops or hides must remain recoverable (CCR store write + surfaced marker), and output grammar is pinned by the in-crate unit tests plus the Python wrapper tests.

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
pub use text_crusher::{TextCrushResult, TextCrusher, TextCrusherConfig, TextCrusherStats};
pub use unidiff_detector::is_diff;
