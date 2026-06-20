# RUST FAT GROUND-TRUTH (grep-based, 2026-06-20) — branch verify/phase2-audit-report @ ce658efe
KEY QUESTION: which crates/ code is reachable ONLY via the archived/deferred proxy path (live_zone), hence dead now?

## pyo3 bridge surface (every #[pyfunction] exported from headroom-py)
rg: error parsing flag -E: grep config error: unknown encoding: live_zone|compress_|crush|retrieve|tokeni|detect|relevance

## for each live_zone/proxy-ish pyo3 fn: which PYTHON files call it (live vs proxy/deferred)?
-- compress_anthropic_live_zone --
    NO python caller
-- compress_openai_responses_live_zone --
    NO python caller
-- compress_openai_live_zone --
    NO python caller

## live_zone.rs internal deps — which Rust modules does it pull in (its dead subtree if live_zone dead)?
104:use super::content_detector::{detect_content_type, ContentType};
105:use super::diff_compressor::{DiffCompressor, DiffCompressorConfig};
106:use super::log_compressor::{LogCompressor, LogCompressorConfig};
107:use super::search_compressor::{SearchCompressor, SearchCompressorConfig};
108:use super::smart_crusher::{SmartCrusher, SmartCrusherConfig};
109:use crate::ccr::{compute_key, marker_for, CcrStore};
110:use crate::tokenizer::get_tokenizer;

## which Rust compressors are referenced OUTSIDE live_zone (i.e. also live via SmartCrusher path)?
  log_compressor     referenced-outside-live_zone: crates/headroom-core/src/transforms/mod.rs crates/headroom-core/src/transforms/diff_compressor.rs crates/headroom-core/src/transforms/pipeline/offloads/log_offload.rs 
  diff_compressor    referenced-outside-live_zone: crates/headroom-core/src/transforms/mod.rs crates/headroom-core/src/transforms/pipeline/offloads/diff_offload.rs 
  search_compressor  referenced-outside-live_zone: crates/headroom-core/src/transforms/mod.rs crates/headroom-core/src/transforms/pipeline/offloads/search_offload.rs 
  anchor_selector    referenced-outside-live_zone: crates/headroom-core/src/transforms/smart_crusher/builder.rs crates/headroom-core/src/transforms/smart_crusher/orchestration.rs crates/headroom-core/src/transforms/smart_crusher/planning.rs 
  tag_protector      referenced-outside-live_zone: crates/headroom-core/src/transforms/mod.rs 

## #[allow(dead_code)] / #[allow(unused)] masks
crates/headroom-core/src/transforms/live_zone.rs:1299:    #[allow(dead_code)]

## Cargo features + are they enabled in headroom-py?
75:magika = { version = "1", optional = true }
123:redis = { version = "0.27", optional = true, default-features = false }
154:[features]
163:redis = ["dep:redis"]
167:embeddings = ["dep:fastembed"]
172:magika = ["dep:magika"]
headroom-py default features:
23:# Disable the default test harness — Rust `cargo test` can't run a cdylib that
30:[features]
31:default = []

## crates/ LOC by file (top 20)
    3149 crates/headroom-core/src/transforms/smart_crusher/crusher.rs
    2899 crates/headroom-core/src/transforms/live_zone.rs
    1942 crates/headroom-core/src/transforms/smart_crusher/compaction/formatter.rs
    1685 crates/headroom-core/src/transforms/diff_compressor.rs
    1683 crates/headroom-core/src/transforms/smart_crusher/compaction/compactor.rs
    1657 crates/headroom-py/src/lib.rs
    1451 crates/headroom-core/src/transforms/log_compressor.rs
    1322 crates/headroom-core/src/transforms/anchor_selector.rs
    1272 crates/headroom-core/src/transforms/tag_protector.rs
    1262 crates/headroom-core/src/transforms/smart_crusher/analyzer.rs
     975 crates/headroom-core/src/transforms/smart_crusher/planning.rs
     877 crates/headroom-core/src/transforms/search_compressor.rs
     865 crates/headroom-core/src/transforms/smart_crusher/crushers.rs
     848 crates/headroom-core/src/transforms/pipeline/orchestrator.rs
     844 crates/headroom-core/src/transforms/smart_crusher/orchestration.rs
     769 crates/headroom-core/src/transforms/content_detector.rs
     748 crates/headroom-core/src/transforms/smart_crusher/compaction/encodings.rs
     632 crates/headroom-core/src/transforms/adaptive_sizer.rs
     584 crates/headroom-core/src/ccr/backends/in_memory.rs
     545 crates/headroom-core/src/transforms/pipeline/reformats/log_template.rs

## CORRECTED: full pyo3 bridge surface (#[pyfunction]s) + which have LIVE python callers
  compress_openai_responses_live_zone        LIVE-caller: NONE (dead or proxy-only)
  content_has_error_indicators               LIVE-caller: headroom/transforms/error_detection.py 
  detect_content_type                        LIVE-caller: headroom/transforms/__init__.py headroom/transforms/content_router.py 
  detect_log_format                          LIVE-caller: headroom/transforms/log_compressor.py 
  hello                                      LIVE-caller: headroom/models/ml_models.py 
  is_html_tag                                LIVE-caller: headroom/transforms/tag_protector.py 
  is_json_array_of_dicts                     LIVE-caller: headroom/transforms/content_detector.py 
  keyword_registry_snapshot                  LIVE-caller: headroom/transforms/error_detection.py 
  known_html_tag_names                       LIVE-caller: headroom/transforms/tag_protector.py 
  parse_search_lines                         LIVE-caller: headroom/transforms/search_compressor.py 
  protect_tags                               LIVE-caller: headroom/transforms/content_router.py headroom/transforms/tag_protector.py 
  restore_tags                               LIVE-caller: headroom/transforms/content_router.py headroom/transforms/tag_protector.py 
  score_line                                 LIVE-caller: headroom/transforms/error_detection.py 

## is the Rust CompressionPipeline/offloads reachable from a LIVE pyo3 fn, or only live_zone?
-- who uses pipeline::orchestrator / CompressionPipeline --
crates/headroom-core/src/transforms/mod.rs
crates/headroom-core/src/transforms/pipeline/mod.rs
crates/headroom-core/src/transforms/pipeline/orchestrator.rs
-- offloads/ referenced by what --
crates/headroom-core/src/transforms/pipeline/orchestrator.rs
crates/headroom-core/src/transforms/pipeline/mod.rs
crates/headroom-core/src/transforms/pipeline/config.rs
