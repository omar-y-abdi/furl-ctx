//! Search-results compressor — Rust port of
//! `furl_ctx.transforms.search_compressor`.
//!
//! Compresses grep / ripgrep / ag output (one of the most common tool
//! outputs in coding tasks). Typical compression: 5-10×.
//!
//! # Input format
//!
//! Standard `grep -n` style:
//! ```text
//! src/utils.py:42:def process_data(items):
//! src/utils.py:43:    """Process items with validation."""
//! src/models.py:15:class DataProcessor:
//! ```
//!
//! Ripgrep with `-C` context (mixes `:` and `-` separators):
//! ```text
//! src/main.py-40-some context before
//! src/main.py:42:def process_data(items):
//! src/main.py-43-some context after
//! ```
//!
//! # Compression pipeline
//!
//! 1. Parse into `{file: [(line, content), ...]}` structure.
//! 2. Score each match on relevance (context-word overlap +
//!    [`crate::signals::LineImportanceDetector`] priority signals +
//!    config-supplied keywords).
//! 3. Sort files by total match score; cap to `max_files`.
//! 4. Run [`crate::transforms::adaptive_sizer::compute_optimal_k`] over
//!    the global match list with `bias` to land an adaptive total.
//! 5. Per-file selection: always-keep first/last (configurable), fill
//!    remaining slots by score, sort survivors back to line order.
//! 6. Format `file:line:content` lines + `[... and N more matches in
//!    file]` summaries. With `group_by_file` (default off) the file
//!    path is emitted once as a `file:` header and match lines nest
//!    under it as `  line:content` — better token economics when many
//!    matches concentrate in few files (the common rg shape).
//! 7. Optional CCR storage when `min_matches_for_ccr` cleared and
//!    compression ratio < 0.8 — appends standard CCR marker.
//!
//! # Bug fixes vs Python (2026-04-29)
//!
//! Python's `_GREP_PATTERN`/`_RG_CONTEXT_PATTERN` regexes mis-handled
//! two real-world inputs. The hand-rolled parser here fixes both:
//!
//! - **Windows paths.** `^([^:]+):(\d+):(.*)$` captured only the drive
//!   letter for `C:\Users\foo\bar.py:42:line`, then the `\d+` group
//!   failed (next char is `\`). Result: every Windows-formatted line
//!   silently dropped from `file_matches`. Rust parser detects
//!   `[A-Za-z]:[\\/]` drive-prefix and starts the line-number scan
//!   *after* the drive colon.
//! - **Filenames containing `-`.** `_RG_CONTEXT_PATTERN`'s
//!   `[^:-]+` excluded dashes from the path, so legitimate names like
//!   `pre-commit-config.yaml-42-line` parsed wrong. Rust parser
//!   anchors on the *line-number marker* (`<sep>\d+<sep>`) found
//!   earliest in the line; everything before is the path, everything
//!   after is the content.
//!
//! Two further hardening changes:
//!
//! - **CCR storage failures are loud.** Python silently swallowed all
//!   exceptions from the store. Rust returns `Result` and surfaces
//!   storage errors via `tracing::warn!` so operations can investigate.
//! - **Per-file dedup is `O(n log n)`.** Python checks `match not in
//!   file_selected` linearly inside a loop (worst-case O(n²) for big
//!   files). Rust uses a `BTreeSet<(line_number, content_hash)>` so the
//!   membership check is logarithmic.

use std::collections::{BTreeMap, BTreeSet};

use crate::ccr::persist::{key_and_mark, persist_and_mark, MarkerBacking};
use crate::ccr::CcrStore;
use crate::ccr::RetrieveUnit;
use crate::signals::{ImportanceContext, LineImportanceDetector};
use crate::transforms::adaptive_sizer::compute_optimal_k;

// ─── Types ──────────────────────────────────────────────────────────────

/// Single search match — a single grep-style hit.
#[derive(Debug, Clone, PartialEq)]
pub struct SearchMatch {
    pub file: String,
    pub line_number: u64,
    pub content: String,
    /// Relevance score in [0.0, 1.0]; populated by [`SearchCompressor::score_matches`].
    pub score: f32,
}

impl SearchMatch {
    pub fn new(file: impl Into<String>, line_number: u64, content: impl Into<String>) -> Self {
        Self {
            file: file.into(),
            line_number,
            content: content.into(),
            score: 0.0,
        }
    }
}

/// All matches grouped under a single file.
#[derive(Debug, Clone, Default)]
pub struct FileMatches {
    pub file: String,
    pub matches: Vec<SearchMatch>,
}

impl FileMatches {
    pub fn new(file: impl Into<String>) -> Self {
        Self {
            file: file.into(),
            matches: Vec::new(),
        }
    }

    pub fn first(&self) -> Option<&SearchMatch> {
        self.matches.first()
    }

    pub fn last(&self) -> Option<&SearchMatch> {
        self.matches.last()
    }

    pub fn total_score(&self) -> f32 {
        self.matches.iter().map(|m| m.score).sum()
    }
}

/// Compressor configuration. Defaults match Python `SearchCompressorConfig`.
#[derive(Debug, Clone)]
pub struct SearchCompressorConfig {
    pub max_matches_per_file: usize,
    pub always_keep_first: bool,
    pub always_keep_last: bool,
    pub max_total_matches: usize,
    pub max_files: usize,
    pub context_keywords: Vec<String>,
    pub boost_errors: bool,
    pub enable_ccr: bool,
    pub min_matches_for_ccr: usize,
    /// Compression ratio threshold for CCR storage. Python defaults to
    /// 0.8 — only persist when compression saved at least 20%. Promoted
    /// to a config field here (Python had it inline) so a future
    /// pipeline can tune per-content-type.
    pub min_compression_ratio_for_ccr: f64,
    /// Render matches grouped under one header line per file (upstream's
    /// `group_by_file` mode, restored per ENGINE-COMPARISON P2-15):
    ///
    /// ```text
    /// src/main.py:
    ///   42:def main():
    ///   43:    pass
    ///   [... and 3 more matches in src/main.py]
    /// ```
    ///
    /// The file path is emitted once instead of per match — better token
    /// economics for the common rg shape (many matches across few files).
    /// Selection, caps, stats attribution (COR-41 3-way scheme) and CCR
    /// persistence are all unchanged; only the rendering differs.
    /// Default: `false` — flat `file:line:content` output, byte-identical
    /// to the pre-existing behavior.
    pub group_by_file: bool,
}

impl Default for SearchCompressorConfig {
    fn default() -> Self {
        Self {
            max_matches_per_file: 5,
            always_keep_first: true,
            always_keep_last: true,
            max_total_matches: 30,
            max_files: 15,
            context_keywords: Vec::new(),
            boost_errors: true,
            enable_ccr: true,
            min_matches_for_ccr: 10,
            min_compression_ratio_for_ccr: 0.8,
            group_by_file: false,
        }
    }
}

/// Compression result. `compressed` carries the formatted output (with
/// optional CCR marker appended); `summaries` maps file paths to the
/// `[... and N more matches in foo.py]` line that landed in that file's
/// section.
#[derive(Debug, Clone)]
pub struct SearchCompressionResult {
    pub compressed: String,
    pub original: String,
    pub original_match_count: usize,
    pub compressed_match_count: usize,
    pub files_affected: usize,
    pub compression_ratio: f64,
    pub cache_key: Option<String>,
    pub summaries: BTreeMap<String, String>,
}

impl SearchCompressionResult {
    /// Estimate tokens saved (rough: 1 token per 4 chars), matching Python.
    pub fn tokens_saved_estimate(&self) -> i64 {
        let chars_saved = self.original.len() as i64 - self.compressed.len() as i64;
        chars_saved.max(0) / 4
    }

    pub fn matches_omitted(&self) -> usize {
        self.original_match_count
            .saturating_sub(self.compressed_match_count)
    }
}

/// Sidecar diagnostics not returned by the parity-equal API. Captures
/// per-stage drop counts so OTel can see what the compressor actually
/// did beyond the bytes Python emits.
#[derive(Debug, Clone, Default)]
pub struct SearchCompressorStats {
    pub lines_scanned: usize,
    pub lines_unparsed: usize,
    pub files_dropped: usize,
    pub matches_dropped_by_per_file_cap: usize,
    pub matches_dropped_by_global_cap: usize,
    /// Duplicate match instances collapsed by the intra-file dedup
    /// (same line number + content). Mutually exclusive with the two
    /// cap counters (COR-41).
    pub matches_dropped_by_dedup: usize,
    pub ccr_emitted: bool,
    pub ccr_skip_reason: Option<&'static str>,
}

// ─── Compressor ─────────────────────────────────────────────────────────

/// Top-level compressor. Holds an importance detector (from the signals
/// trait family) so the priority-pattern scoring is pluggable. Defaults
/// to a [`crate::signals::KeywordDetector`].
pub struct SearchCompressor {
    config: SearchCompressorConfig,
    importance: Box<dyn LineImportanceDetector>,
}

impl SearchCompressor {
    pub fn new(config: SearchCompressorConfig) -> Self {
        Self {
            config,
            importance: Box::new(crate::signals::KeywordDetector::new()),
        }
    }

    /// Construct with a custom [`LineImportanceDetector`] (e.g. an ML
    /// head composed with the keyword detector).
    pub fn with_detector<D: LineImportanceDetector + 'static>(
        config: SearchCompressorConfig,
        detector: D,
    ) -> Self {
        Self {
            config,
            importance: Box::new(detector),
        }
    }

    pub fn config(&self) -> &SearchCompressorConfig {
        &self.config
    }

    /// Compress without persisting CCR. Returns the parity-equal result
    /// plus sidecar stats.
    pub fn compress(
        &self,
        content: &str,
        context: &str,
        bias: f64,
    ) -> (SearchCompressionResult, SearchCompressorStats) {
        self.compress_with_store(content, context, bias, None)
    }

    /// Compress with the CCR `cache_key` computed but NOTHING persisted
    /// (PERF-8). Byte-identical output (compressed bytes, marker,
    /// `cache_key`, stats) to `compress_with_store(.., Some(store))` —
    /// only the store write is skipped. For callers that own persistence
    /// themselves: the PyO3 bridge's Python shim re-persists the
    /// original into the production `CompressionStore` under this exact
    /// key and vetoes the compression if that write fails.
    pub fn compress_key_only(
        &self,
        content: &str,
        context: &str,
        bias: f64,
    ) -> (SearchCompressionResult, SearchCompressorStats) {
        self.compress_inner(content, context, bias, MarkerBacking::KeyOnly)
    }

    /// Compress with optional CCR persistence. `store` is consulted only
    /// if `config.enable_ccr` is true and the compression cleared the
    /// thresholds; storage failures emit `tracing::warn!` rather than
    /// being silently swallowed.
    pub fn compress_with_store(
        &self,
        content: &str,
        context: &str,
        bias: f64,
        store: Option<&dyn CcrStore>,
    ) -> (SearchCompressionResult, SearchCompressorStats) {
        let backing = match store {
            Some(s) => MarkerBacking::Store(s),
            None => MarkerBacking::Disabled,
        };
        self.compress_inner(content, context, bias, backing)
    }

    fn compress_inner(
        &self,
        content: &str,
        context: &str,
        bias: f64,
        backing: MarkerBacking<'_>,
    ) -> (SearchCompressionResult, SearchCompressorStats) {
        let mut stats = SearchCompressorStats::default();
        let parsed = self.parse_search_results(content, &mut stats);

        if parsed.is_empty() {
            return (
                SearchCompressionResult {
                    compressed: content.to_string(),
                    original: content.to_string(),
                    original_match_count: 0,
                    compressed_match_count: 0,
                    files_affected: 0,
                    compression_ratio: 1.0,
                    cache_key: None,
                    summaries: BTreeMap::new(),
                },
                stats,
            );
        }

        let original_count: usize = parsed.values().map(|fm| fm.matches.len()).sum();

        let mut scored = parsed;
        self.score_matches(&mut scored, context);

        let selected = self.select_matches(&scored, bias, &mut stats);

        let (compressed_body, summaries) = self.format_output(&selected, &scored);
        let compressed_count: usize = selected.values().map(|fm| fm.matches.len()).sum();
        let ratio = compressed_body.len() as f64 / content.len().max(1) as f64;

        let mut compressed = compressed_body;
        let mut cache_key = None;
        if self.config.enable_ccr {
            if original_count < self.config.min_matches_for_ccr {
                stats.ccr_skip_reason = Some("below min_matches_for_ccr");
            } else if ratio >= self.config.min_compression_ratio_for_ccr {
                stats.ccr_skip_reason = Some("compression ratio too high");
            } else {
                // Shared key→[put]→marker tail (`ccr::persist`, ARCH-5);
                // the leading `\n` is composed there so the marker
                // grammar in `ccr::markers` stays newline-free. Key and
                // marker bytes are identical across backings (PERF-8) —
                // only whether the original is persisted differs.
                let keyed = match backing {
                    MarkerBacking::Store(store) => Some(persist_and_mark(
                        store,
                        content,
                        original_count,
                        compressed_count,
                        RetrieveUnit::Matches,
                    )),
                    MarkerBacking::KeyOnly => Some(key_and_mark(
                        content,
                        original_count,
                        compressed_count,
                        RetrieveUnit::Matches,
                    )),
                    MarkerBacking::Disabled => None,
                };
                match keyed {
                    Some((key, marker)) => {
                        compressed.push_str(&marker);
                        cache_key = Some(key);
                        stats.ccr_emitted = true;
                    }
                    None => {
                        stats.ccr_skip_reason = Some("no store provided");
                    }
                }
            }
        } else {
            stats.ccr_skip_reason = Some("ccr disabled in config");
        }

        let result = SearchCompressionResult {
            compressed,
            original: content.to_string(),
            original_match_count: original_count,
            compressed_match_count: compressed_count,
            files_affected: scored.len(),
            compression_ratio: ratio,
            cache_key,
            summaries,
        };
        (result, stats)
    }

    // ─── Stage helpers (also used by tests + Python adapter) ───────────

    pub fn parse_search_results(
        &self,
        content: &str,
        stats: &mut SearchCompressorStats,
    ) -> BTreeMap<String, FileMatches> {
        let mut out: BTreeMap<String, FileMatches> = BTreeMap::new();
        for raw in content.split('\n') {
            let line = raw.trim();
            if line.is_empty() {
                continue;
            }
            stats.lines_scanned += 1;
            match parse_match_line(line) {
                Some((file, line_no, body)) => {
                    out.entry(file.to_string())
                        .or_insert_with(|| FileMatches::new(file))
                        .matches
                        .push(SearchMatch::new(file, line_no, body));
                }
                None => stats.lines_unparsed += 1,
            }
        }
        out
    }

    pub fn score_matches(&self, files: &mut BTreeMap<String, FileMatches>, context: &str) {
        let context_lower = context.to_ascii_lowercase();
        let context_words: Vec<&str> = context_lower
            .split_whitespace()
            .filter(|w| w.len() > 2)
            .collect();

        for fm in files.values_mut() {
            for m in &mut fm.matches {
                let mut score: f32 = 0.0;
                let content_lower = m.content.to_ascii_lowercase();

                for w in &context_words {
                    if content_lower.contains(w) {
                        score += 0.3;
                    }
                }

                if self.config.boost_errors {
                    let signal = self.importance.score(&m.content, ImportanceContext::Search);
                    if let Some(category) = signal.category {
                        // Python's loop boosts by 0.5 - i*0.1 over priority
                        // patterns; map our trait categories to the same
                        // ordering: Error first (0.5), Warning (0.4),
                        // Importance (0.3).
                        let bump = match category {
                            crate::signals::ImportanceCategory::Error => 0.5,
                            crate::signals::ImportanceCategory::Warning => 0.4,
                            crate::signals::ImportanceCategory::Importance => 0.3,
                            // Categories below aren't part of
                            // PRIORITY_PATTERNS_SEARCH; preserve Python's
                            // behavior of not boosting for them.
                            crate::signals::ImportanceCategory::Security
                            | crate::signals::ImportanceCategory::Markdown => 0.0,
                        };
                        score += bump;
                    }
                }

                for kw in &self.config.context_keywords {
                    if content_lower.contains(&kw.to_ascii_lowercase()) {
                        score += 0.4;
                    }
                }

                m.score = score.min(1.0);
            }
        }
    }

    pub fn select_matches(
        &self,
        files: &BTreeMap<String, FileMatches>,
        bias: f64,
        stats: &mut SearchCompressorStats,
    ) -> BTreeMap<String, FileMatches> {
        // Python `_select_matches` sorts files by total match score
        // descending. `BTreeMap` iterates in key order, so we collect
        // and sort explicitly.
        let mut by_score: Vec<(&String, &FileMatches)> = files.iter().collect();
        by_score.sort_by(|a, b| {
            b.1.total_score()
                .partial_cmp(&a.1.total_score())
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        if by_score.len() > self.config.max_files {
            stats.files_dropped += by_score.len() - self.config.max_files;
            by_score.truncate(self.config.max_files);
        }

        // Feed the adaptive sizer the match CONTENT slices directly
        // (PERF-6). The old shape materialized every match as a fresh
        // `format!("{file}:{line}:{content}")` String purely to size the
        // set — per-line-unique `file:line:` prefixes glued to the first
        // content word inflated bigram uniqueness with zero information,
        // and the allocs dominated select_matches on big result sets.
        // The saturation signal lives in the content; the k this yields
        // is gated by the benchmark ratio floor (non-regression).
        let all_refs: Vec<&str> = by_score
            .iter()
            .flat_map(|(_file, fm)| fm.matches.iter().map(|m| m.content.as_str()))
            .collect();
        let adaptive_total =
            compute_optimal_k(&all_refs, bias, 5, Some(self.config.max_total_matches));

        let mut selected: BTreeMap<String, FileMatches> = BTreeMap::new();
        let mut total_selected: usize = 0;

        for (file, fm) in by_score {
            if total_selected >= adaptive_total {
                stats.matches_dropped_by_global_cap += fm.matches.len();
                continue;
            }

            // Sort by score desc, ties broken by line number asc for
            // determinism (Python's `sorted` is stable; order in is
            // line-asc by construction so highest-score-first picks the
            // earliest line on ties).
            let mut sorted = fm.matches.clone();
            sorted.sort_by(|a, b| {
                b.score
                    .partial_cmp(&a.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| a.line_number.cmp(&b.line_number))
            });

            let mut file_selected: Vec<SearchMatch> = Vec::new();
            // BTreeSet for O(log n) "already in selection" check (Python
            // uses linear `not in` — quadratic for big files).
            let mut seen: BTreeSet<(u64, u64)> = BTreeSet::new();

            let remaining_global = adaptive_total.saturating_sub(total_selected);
            let remaining_cap = self.config.max_matches_per_file.min(remaining_global);

            let push_unique = |m: &SearchMatch,
                               file_selected: &mut Vec<SearchMatch>,
                               seen: &mut BTreeSet<(u64, u64)>| {
                let key = (m.line_number, hash_u64(&m.content));
                if seen.insert(key) {
                    file_selected.push(m.clone());
                    true
                } else {
                    false
                }
            };

            if self.config.always_keep_first {
                if let Some(first) = fm.first() {
                    if file_selected.len() < remaining_cap {
                        push_unique(first, &mut file_selected, &mut seen);
                    }
                }
            }

            if self.config.always_keep_last && fm.matches.len() > 1 {
                if let Some(last) = fm.last() {
                    if file_selected.len() < remaining_cap {
                        push_unique(last, &mut file_selected, &mut seen);
                    }
                }
            }

            for m in &sorted {
                if file_selected.len() >= remaining_cap {
                    break;
                }
                push_unique(m, &mut file_selected, &mut seen);
            }

            // Restore line order for output.
            file_selected.sort_by_key(|m| m.line_number);

            // Attribute every truncated match to its actual cause
            // (COR-41): duplicate instances collapsed by the intra-file
            // dedup; unique matches beyond the per-file cap (dropped
            // regardless of budget); and unique matches that fit the
            // per-file cap but not the remaining global budget. The three
            // buckets sum to `fm.matches.len() - file_selected.len()`.
            let unique_total = fm
                .matches
                .iter()
                .map(|m| (m.line_number, hash_u64(&m.content)))
                .collect::<BTreeSet<_>>()
                .len();
            stats.matches_dropped_by_dedup += fm.matches.len() - unique_total;
            stats.matches_dropped_by_per_file_cap +=
                unique_total.saturating_sub(self.config.max_matches_per_file);
            stats.matches_dropped_by_global_cap += unique_total
                .min(self.config.max_matches_per_file)
                .saturating_sub(remaining_global);

            total_selected += file_selected.len();
            selected.insert(
                file.clone(),
                FileMatches {
                    file: file.clone(),
                    matches: file_selected,
                },
            );
        }

        selected
    }

    /// Render the selected matches. Two modes:
    ///
    /// - Flat (default): grep-shaped `file:line:content` per match —
    ///   byte-identical to the original output contract.
    /// - Grouped (`config.group_by_file`): one `file:` header per file,
    ///   match lines nested under it as `  line:content` (P2-15). The
    ///   per-file omission summary nests too. Same selection, same caps,
    ///   same summaries map keys — only the rendering differs.
    pub fn format_output(
        &self,
        selected: &BTreeMap<String, FileMatches>,
        original: &BTreeMap<String, FileMatches>,
    ) -> (String, BTreeMap<String, String>) {
        let mut lines: Vec<String> = Vec::new();
        let mut summaries: BTreeMap<String, String> = BTreeMap::new();
        let grouped = self.config.group_by_file;

        for (file, fm) in selected {
            if grouped {
                lines.push(format!("{}:", file));
            }
            for m in &fm.matches {
                if grouped {
                    lines.push(format!("  {}:{}", m.line_number, m.content));
                } else {
                    lines.push(format!("{}:{}:{}", m.file, m.line_number, m.content));
                }
            }
            if let Some(orig_fm) = original.get(file) {
                if orig_fm.matches.len() > fm.matches.len() {
                    let omitted = orig_fm.matches.len() - fm.matches.len();
                    let summary = if grouped {
                        format!("  [... and {} more matches in {}]", omitted, file)
                    } else {
                        format!("[... and {} more matches in {}]", omitted, file)
                    };
                    lines.push(summary.clone());
                    summaries.insert(file.clone(), summary);
                }
            }
        }

        (lines.join("\n"), summaries)
    }
}

// ─── Parser ─────────────────────────────────────────────────────────────

/// Parse one grep/ripgrep-style line into `(file, line_number, content)`.
///
/// Strategy:
/// 1. If the line starts with a Windows drive prefix (`C:\` or `C:/`),
///    record the drive letter + colon as the path's required prefix and
///    start the line-number scan after the drive colon.
/// 2. Colon pass: find the leftmost `:<digits>:` marker. grep and
///    ripgrep match lines are always `path:N:content`, so this form
///    wins even when the filename contains a `-<digits>-` run
///    (fixed_in_cor26: `utils-2-final.py:42:content` used to parse as
///    file `utils`, line 2, content `final.py:42:content`).
/// 3. Dash pass (ripgrep context lines, `path-N-content`): find the
///    leftmost `-<digits>-` marker; runs only when no colon marker
///    exists. Match and context lines may still be intermingled in one
///    stream — each individual line is one of the two pure forms.
///    Mixed separators (`:N-` / `-N:`) no longer parse; neither tool
///    emits them.
///
/// Returns `None` for lines that don't match either shape. Caller
/// treats those as un-parseable and drops them.
fn parse_match_line(line: &str) -> Option<(&str, u64, &str)> {
    let bytes = line.as_bytes();
    // Windows drive prefix: starts with [A-Za-z]:[\\/]
    let scan_start = if bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && (bytes[2] == b'\\' || bytes[2] == b'/')
    {
        // Skip past the drive colon so it isn't misread as the
        // line-number-marker separator.
        2
    } else {
        0
    };

    // Reject zero-length paths up front: a line that *starts* with a
    // `<sep><digits><sep>` marker (any separator pair) has no file part.
    // Matches the old leftmost-scan behavior, which aborted on these.
    if scan_start == 0 && matches!(bytes.first(), Some(b':') | Some(b'-')) {
        let mut j = 1;
        while j < bytes.len() && bytes[j].is_ascii_digit() {
            j += 1;
        }
        if j > 1 && j < bytes.len() && (bytes[j] == b':' || bytes[j] == b'-') {
            return None;
        }
    }

    find_marker(line, scan_start, b':').or_else(|| find_marker(line, scan_start, b'-'))
}

/// Find the leftmost `<sep><digits><sep>` marker where BOTH separators
/// are `sep`, splitting the line into `(path, line_number, content)`.
///
/// The byte immediately before the first separator must not itself be a
/// separator. That collapses adjacent-separator runs (`::`, `:-`, `--`)
/// so a line like `src/file.py:-1:invalid` doesn't parse the `-` as the
/// marker's first separator and `1` as the line number; the negative
/// sign belongs to the content, not the marker, so the line is rejected
/// as un-parseable.
fn find_marker(line: &str, scan_start: usize, sep: u8) -> Option<(&str, u64, &str)> {
    let bytes = line.as_bytes();
    let mut i = scan_start;
    while i < bytes.len() {
        if bytes[i] == sep {
            if i > 0 && (bytes[i - 1] == b':' || bytes[i - 1] == b'-') {
                i += 1;
                continue;
            }
            // Try this as the first separator. Walk through digits.
            let digits_start = i + 1;
            let mut j = digits_start;
            while j < bytes.len() && bytes[j].is_ascii_digit() {
                j += 1;
            }
            if j > digits_start && j < bytes.len() && bytes[j] == sep {
                // Zero-length path (line starts with the separator);
                // the caller's up-front guard rejects these before the
                // passes run, kept here so the helper stands alone.
                if i == 0 {
                    return None;
                }
                let file = &line[..i];
                let line_no = std::str::from_utf8(&bytes[digits_start..j])
                    .ok()
                    .and_then(|s| s.parse::<u64>().ok())?;
                let content = &line[j + 1..];
                return Some((file, line_no, content));
            }
        }
        i += 1;
    }
    None
}

// ─── Internals ──────────────────────────────────────────────────────────

fn hash_u64(s: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    s.hash(&mut h);
    h.finish()
}

// `md5_hex_24` (the CCR cache key) lives in `crate::ccr::persist` —
// one shared implementation for the diff/log/search/text family
// (ARCH-5); the tail above rides it via `persist_and_mark`.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ccr::InMemoryCcrStore;

    fn parse_line(line: &str) -> Option<(String, u64, String)> {
        parse_match_line(line).map(|(f, n, c)| (f.to_string(), n, c.to_string()))
    }

    #[test]
    fn parses_standard_grep_line() {
        assert_eq!(
            parse_line("src/main.py:42:def main():"),
            Some(("src/main.py".into(), 42, "def main():".into()))
        );
    }

    #[test]
    fn parses_ripgrep_context_line() {
        assert_eq!(
            parse_line("src/main.py-43-context after match"),
            Some(("src/main.py".into(), 43, "context after match".into()))
        );
    }

    #[test]
    fn fixed_in_3e2_handles_windows_path_with_backslash() {
        // Pre-3e2 Python regex misread the drive colon as the
        // line-number-marker separator and silently dropped this line.
        assert_eq!(
            parse_line(r"C:\Users\foo\bar.py:42:def main():"),
            Some((r"C:\Users\foo\bar.py".into(), 42, "def main():".into()))
        );
    }

    #[test]
    fn fixed_in_3e2_handles_windows_path_with_forward_slash() {
        // Ripgrep on Windows often emits forward-slash paths.
        assert_eq!(
            parse_line("C:/Users/foo/bar.py:42:def main():"),
            Some(("C:/Users/foo/bar.py".into(), 42, "def main():".into()))
        );
    }

    #[test]
    fn fixed_in_3e2_handles_dashes_in_filename_with_ripgrep_context() {
        // Pre-3e2 `_RG_CONTEXT_PATTERN`'s `[^:-]+` excluded dashes from
        // the path, so this line was either misparsed (path truncated
        // at the first dash) or fell through.
        assert_eq!(
            parse_line("pre-commit-config.yaml-42-fail_fast: true"),
            Some((
                "pre-commit-config.yaml".into(),
                42,
                "fail_fast: true".into()
            ))
        );
    }

    #[test]
    fn fixed_in_cor26_digit_run_between_dashes_in_filename() {
        // `foo-2-bar.py:42:x` used to parse as file `foo`, line 2,
        // content `bar.py:42:x`: the leftmost any-separator scan matched
        // the `-2-` run inside the filename before the real `:42:`
        // marker. grep/rg match lines are always `path:N:content`, so
        // the colon-only form is tried first.
        assert_eq!(
            parse_line("foo-2-bar.py:42:x"),
            Some(("foo-2-bar.py".into(), 42, "x".into()))
        );
        // Same shape on a longer realistic name.
        assert_eq!(
            parse_line("utils-2-final.py:42:content"),
            Some(("utils-2-final.py".into(), 42, "content".into()))
        );
    }

    #[test]
    fn fixed_in_cor26_mixed_separators_no_longer_parse() {
        // grep emits `path:N:content`; rg context lines are
        // `path-N-content`. Neither tool mixes separators within one
        // line, so the old any-pair acceptance (`file.py:42-text`) only
        // ever fired on coincidental shapes. Both passes now require a
        // matching pair.
        assert!(parse_line("file.py:42-text").is_none());
        assert!(parse_line("file.py-42:text").is_none());
    }

    #[test]
    fn preserves_colons_in_match_content() {
        // Standard grep behavior: stop at the second separator, the rest
        // is content, even when content contains colons.
        assert_eq!(
            parse_line(r#"config.py:10:DATABASE_URL = "postgres://user:pass@host:5432/db""#),
            Some((
                "config.py".into(),
                10,
                r#"DATABASE_URL = "postgres://user:pass@host:5432/db""#.into()
            ))
        );
    }

    #[test]
    fn rejects_lines_without_line_number_marker() {
        assert!(parse_line("just a normal line of prose").is_none());
        assert!(parse_line("file.py:not-a-number:something").is_none());
        // Empty/zero-length path rejected:
        assert!(parse_line(":42:something").is_none());
    }

    #[test]
    fn rejects_negative_line_numbers() {
        // The `-` is part of the content, not a separator. Pre-3e2 the
        // adjacent-separator collapse rule didn't exist; a stray fix
        // could have re-introduced this regression.
        assert!(parse_line("src/file.py:-1:invalid").is_none());
        // Equivalent form with the dash adjacent to the dash separator.
        assert!(parse_line("src/file.py--1-invalid").is_none());
    }

    #[test]
    fn parser_groups_by_file_and_counts() {
        let compressor = SearchCompressor::new(SearchCompressorConfig::default());
        let content = "\
src/main.py:42:def main():
src/main.py:43:    pass
src/utils.py:15:def util():
just prose, no marker
src/main.py-44-context line";
        let mut stats = SearchCompressorStats::default();
        let parsed = compressor.parse_search_results(content, &mut stats);
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed["src/main.py"].matches.len(), 3);
        assert_eq!(parsed["src/utils.py"].matches.len(), 1);
        assert_eq!(stats.lines_unparsed, 1);
        assert_eq!(stats.lines_scanned, 5);
    }

    #[test]
    fn scoring_boosts_error_lines_in_search_context() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            context_keywords: vec!["auth".into()],
            ..Default::default()
        });
        let mut files = BTreeMap::new();
        let mut fm = FileMatches::new("src/auth.py");
        fm.matches
            .push(SearchMatch::new("src/auth.py", 10, "ERROR auth failed"));
        fm.matches
            .push(SearchMatch::new("src/auth.py", 11, "plain auth line"));
        files.insert("src/auth.py".into(), fm);

        compressor.score_matches(&mut files, "find auth error");
        let scored = &files["src/auth.py"].matches;
        // ERROR + auth-keyword + context-word "error" + context-word
        // "auth" all hit; clamped to 1.0.
        assert_eq!(scored[0].score, 1.0);
        // Plain line gets only context-word + keyword boosts (no error).
        assert!(scored[1].score > 0.0 && scored[1].score < 1.0);
    }

    #[test]
    fn select_respects_per_file_cap_and_global_cap() {
        // Note: compute_optimal_k enforces a hard `min_k=5` floor (matches
        // Python `_select_matches`), so `max_total_matches` is a soft cap
        // that bites only above that floor. Configure 6 here to exercise
        // the cap path.
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            max_matches_per_file: 2,
            max_total_matches: 6,
            max_files: 2,
            always_keep_first: true,
            always_keep_last: true,
            ..Default::default()
        });
        let mut files = BTreeMap::new();
        for (file, n) in [("a.py", 5), ("b.py", 4), ("c.py", 3)] {
            let mut fm = FileMatches::new(file);
            for i in 0..n {
                fm.matches
                    .push(SearchMatch::new(file, i + 1, format!("line {}", i + 1)));
            }
            files.insert(file.into(), fm);
        }

        let mut stats = SearchCompressorStats::default();
        let selected = compressor.select_matches(&files, 1.0, &mut stats);

        // max_files=2 caps surviving files; one of three is dropped.
        assert_eq!(selected.len(), 2);
        assert!(stats.files_dropped >= 1);
        // Each surviving file is capped at max_matches_per_file=2.
        for fm in selected.values() {
            assert!(fm.matches.len() <= 2);
            // Survivors output in line order.
            assert!(fm
                .matches
                .windows(2)
                .all(|w| w[0].line_number < w[1].line_number));
        }
    }

    #[test]
    fn truncation_stats_attribute_global_cap_inside_processed_file() {
        // COR-41: matches truncated inside a PROCESSED file because the
        // global budget ran out used to be booked under
        // `matches_dropped_by_per_file_cap`. Here the per-file cap (10)
        // never binds — the global cap (5) does.
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            max_matches_per_file: 10,
            max_total_matches: 5,
            ..Default::default()
        });
        let mut files = BTreeMap::new();
        for file in ["a.py", "b.py"] {
            let mut fm = FileMatches::new(file);
            for i in 0..4 {
                fm.matches.push(SearchMatch::new(
                    file,
                    i + 1,
                    format!("{} line {}", file, i + 1),
                ));
            }
            files.insert(file.to_string(), fm);
        }

        let mut stats = SearchCompressorStats::default();
        let selected = compressor.select_matches(&files, 1.0, &mut stats);

        // 8 total matches, global budget 5: a.py ships all 4, b.py ships
        // 1 and truncates 3 — because of the GLOBAL cap.
        let total: usize = selected.values().map(|fm| fm.matches.len()).sum();
        assert_eq!(total, 5);
        assert_eq!(
            stats.matches_dropped_by_per_file_cap, 0,
            "per-file cap never bound; nothing may be booked under it"
        );
        assert_eq!(
            stats.matches_dropped_by_global_cap, 3,
            "global-budget truncation inside a processed file must be booked to the global cap"
        );
        assert_eq!(stats.matches_dropped_by_dedup, 0);
    }

    #[test]
    fn truncation_stats_attribute_intra_file_dedup() {
        // COR-41: duplicates collapsed by the intra-file dedup are not
        // per-file-cap drops (the file is far under both caps here).
        let compressor = SearchCompressor::new(SearchCompressorConfig::default());
        let mut fm = FileMatches::new("d.py");
        fm.matches.push(SearchMatch::new("d.py", 1, "dup line"));
        fm.matches.push(SearchMatch::new("d.py", 2, "unique line"));
        fm.matches.push(SearchMatch::new("d.py", 1, "dup line"));
        let mut files = BTreeMap::new();
        files.insert("d.py".to_string(), fm);

        let mut stats = SearchCompressorStats::default();
        let selected = compressor.select_matches(&files, 1.0, &mut stats);

        assert_eq!(selected["d.py"].matches.len(), 2);
        assert_eq!(stats.matches_dropped_by_per_file_cap, 0);
        assert_eq!(stats.matches_dropped_by_global_cap, 0);
        assert_eq!(
            stats.matches_dropped_by_dedup, 1,
            "the collapsed duplicate instance is a dedup drop"
        );
    }

    #[test]
    fn empty_input_returns_unchanged() {
        let compressor = SearchCompressor::new(SearchCompressorConfig::default());
        let (result, _) = compressor.compress("plain text only", "", 1.0);
        assert_eq!(result.original_match_count, 0);
        assert_eq!(result.compressed, "plain text only");
        assert_eq!(result.compression_ratio, 1.0);
    }

    #[test]
    fn key_only_mode_is_byte_identical_to_store_mode() {
        // PERF-8 pin: `compress_key_only` must produce byte-equal output
        // (compressed bytes incl. marker, cache_key, counts) to
        // `compress_with_store(.., Some(store))` — the ONLY difference
        // is that nothing is persisted.
        use crate::ccr::InMemoryCcrStore;
        let content: String = (1..=200)
            .map(|i| format!("src/file.py:{i}:line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let compressor = SearchCompressor::new(SearchCompressorConfig::default());
        let store = InMemoryCcrStore::new();
        let (with_store, ws_stats) =
            compressor.compress_with_store(&content, "", 1.0, Some(&store));
        let (key_only, ko_stats) = compressor.compress_key_only(&content, "", 1.0);
        assert_eq!(
            with_store.cache_key, key_only.cache_key,
            "cache_key must be byte-equal"
        );
        assert!(with_store.cache_key.is_some(), "fixture must trigger CCR");
        assert_eq!(with_store.compressed, key_only.compressed);
        assert_eq!(
            with_store.compressed_match_count,
            key_only.compressed_match_count
        );
        assert_eq!(ws_stats.ccr_emitted, ko_stats.ccr_emitted);
        assert_eq!(ws_stats.ccr_skip_reason, ko_stats.ccr_skip_reason);
        assert_eq!(store.len(), 1, "store mode persisted exactly the original");
    }

    #[test]
    fn ccr_marker_emitted_when_thresholds_clear() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            max_matches_per_file: 2,
            max_total_matches: 4,
            min_matches_for_ccr: 5,
            min_compression_ratio_for_ccr: 0.95, // very permissive for the test
            ..Default::default()
        });
        let mut content = String::new();
        for i in 1..=12 {
            content.push_str(&format!("src/main.py:{}:line content {}\n", i, i));
        }
        let store = InMemoryCcrStore::new();
        let (result, stats) = compressor.compress_with_store(&content, "", 1.0, Some(&store));
        assert!(result.cache_key.is_some());
        assert!(stats.ccr_emitted);
        assert!(result.compressed.contains("[12 matches compressed to"));
        // Round-trip via the store.
        let key = result.cache_key.as_ref().unwrap();
        assert_eq!(store.get(key).unwrap(), content);
    }

    #[test]
    fn ccr_skipped_when_below_min_matches() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            min_matches_for_ccr: 100,
            ..Default::default()
        });
        let content = "src/main.py:1:hi\nsrc/main.py:2:bye\n";
        let store = InMemoryCcrStore::new();
        let (_, stats) = compressor.compress_with_store(content, "", 1.0, Some(&store));
        assert!(!stats.ccr_emitted);
        assert_eq!(stats.ccr_skip_reason, Some("below min_matches_for_ccr"));
        assert_eq!(store.len(), 0);
    }

    // ─── group_by_file mode (P2-15) ─────────────────────────────────────

    #[test]
    fn group_by_file_defaults_off_and_flat_output_is_byte_identical() {
        // The flag must default OFF, and the flat rendering must stay the
        // exact grep shape: when nothing is dropped the output reproduces
        // the input byte-for-byte.
        assert!(!SearchCompressorConfig::default().group_by_file);
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            enable_ccr: false,
            ..Default::default()
        });
        let content = "src/a.py:1:alpha\nsrc/a.py:2:beta\nsrc/b.py:7:gamma";
        let (result, _) = compressor.compress(content, "", 1.0);
        assert_eq!(result.compressed, content);
    }

    #[test]
    fn group_by_file_renders_one_header_per_file_with_nested_matches() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            group_by_file: true,
            enable_ccr: false,
            ..Default::default()
        });
        let content = "src/a.py:1:alpha\nsrc/a.py:2:beta\nsrc/b.py:7:gamma";
        let (result, _) = compressor.compress(content, "", 1.0);
        assert_eq!(
            result.compressed,
            "src/a.py:\n  1:alpha\n  2:beta\nsrc/b.py:\n  7:gamma"
        );
    }

    #[test]
    fn group_by_file_honors_caps_and_emits_nested_summaries() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            group_by_file: true,
            max_matches_per_file: 2,
            max_total_matches: 6,
            max_files: 2,
            enable_ccr: false,
            ..Default::default()
        });
        let mut content = String::new();
        for f in ["a.py", "b.py", "c.py"] {
            for i in 1..=5 {
                content.push_str(&format!("{}:{}:line {}\n", f, i, i));
            }
        }
        let (result, stats) = compressor.compress(&content, "", 1.0);

        // max_files=2 → exactly two file headers (unindented, `path:`).
        let headers: Vec<&str> = result
            .compressed
            .lines()
            .filter(|l| !l.starts_with(' ') && l.ends_with(':'))
            .collect();
        assert_eq!(headers.len(), 2, "output:\n{}", result.compressed);
        assert!(stats.files_dropped >= 1);

        // Per-file cap: at most 2 nested match lines per file.
        let mut per_file: usize = 0;
        for line in result.compressed.lines() {
            if !line.starts_with(' ') {
                per_file = 0;
            } else if !line.trim_start().starts_with('[') {
                per_file += 1;
                assert!(
                    per_file <= 2,
                    "per-file cap violated:\n{}",
                    result.compressed
                );
            }
        }

        // Summaries: 3 of 5 unique matches omitted per surviving file,
        // rendered nested under the file header and recorded in the map.
        assert!(result
            .compressed
            .contains("  [... and 3 more matches in a.py]"));
        assert_eq!(result.summaries.len(), 2);
        for (file, summary) in &result.summaries {
            assert_eq!(summary, &format!("  [... and 3 more matches in {}]", file));
        }

        // COR-41 3-way attribution unchanged by the rendering mode: the
        // 3 omitted per surviving file are per-file-cap drops.
        assert_eq!(stats.matches_dropped_by_dedup, 0);
        assert_eq!(stats.matches_dropped_by_per_file_cap, 6);
        assert_eq!(stats.matches_dropped_by_global_cap, 0);
    }

    #[test]
    fn group_by_file_ccr_roundtrip_via_store() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            group_by_file: true,
            max_matches_per_file: 2,
            max_total_matches: 4,
            min_matches_for_ccr: 5,
            min_compression_ratio_for_ccr: 0.95,
            ..Default::default()
        });
        let mut content = String::new();
        for i in 1..=12 {
            content.push_str(&format!("src/main.py:{}:line content {}\n", i, i));
        }
        let store = InMemoryCcrStore::new();
        let (result, stats) = compressor.compress_with_store(&content, "", 1.0, Some(&store));
        assert!(stats.ccr_emitted);
        assert!(result.compressed.starts_with("src/main.py:\n"));
        // CCR marker grammar unchanged in grouped mode.
        assert!(result.compressed.contains("[12 matches compressed to"));
        // Round-trip: store holds the byte-exact ORIGINAL (flat grep
        // output), not the grouped rendering.
        let key = result.cache_key.as_ref().unwrap();
        assert_eq!(store.get(key).unwrap(), content);
    }

    #[test]
    fn ccr_skipped_when_disabled() {
        let compressor = SearchCompressor::new(SearchCompressorConfig {
            enable_ccr: false,
            ..Default::default()
        });
        let mut content = String::new();
        for i in 1..=20 {
            content.push_str(&format!("src/main.py:{}:line\n", i));
        }
        let store = InMemoryCcrStore::new();
        let (_, stats) = compressor.compress_with_store(&content, "", 1.0, Some(&store));
        assert!(!stats.ccr_emitted);
        assert_eq!(stats.ccr_skip_reason, Some("ccr disabled in config"));
    }
}
