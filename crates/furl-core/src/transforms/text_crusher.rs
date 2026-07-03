//! TextCrusher — deterministic, ML-free extractive prose compressor
//! for `PLAIN_TEXT` (Engine P2-11).
//!
//! Fills the largest capability gap left by the ML-compressor excision:
//! `PLAIN_TEXT` routed to a passthrough since Chunk 1. Upstream replaced
//! its ML compressor with a ~360-line deterministic extractive selector;
//! this module implements that design under THIS fork's stricter
//! reversibility invariants.
//!
//! # Pipeline
//!
//! 1. **Protect** — [`super::tag_protector::protect_tags`] swaps custom
//!    workflow tags (`<system-reminder>`, `<tool_call>`, …) for opaque
//!    placeholders. Segments containing a placeholder are mandatory
//!    keeps, so a protected span can never be dropped or split.
//! 2. **Segment** — markdown-structure-aware splitting: code fences are
//!    atomic blocks (never split), headers and list-item lines are their
//!    own segments, paragraphs split into sentence-ish segments on
//!    `.`/`!`/`?` + whitespace + non-lowercase lookahead.
//! 3. **Score** — BM25 against the optional `query` (reuses
//!    [`crate::relevance::BM25Scorer`]) + a U-shaped serial-position
//!    prior (openings state the topic, endings state conclusions) +
//!    salience: error/warn keywords ([`crate::signals::KeywordDetector`]
//!    with [`ImportanceContext::Text`]), numeric tokens, capitalized
//!    entities, and structure kind.
//! 4. **Dedup** — word-shingle Jaccard: a segment ≥ `dedup_threshold`
//!    similar to an earlier unique segment collapses (first occurrence
//!    survives). Above `max_pairwise_dedup_segments` only exact
//!    normalized-hash dedup runs (keeps worst-case cost linear-ish).
//! 5. **Select** — mandatory keeps (headers, placeholder segments, the
//!    first/last `always_keep_*`) + highest-scoring segments under a
//!    char budget of `len × target_ratio × bias`, floored at
//!    `min_kept_segments`. Output preserves original order; original
//!    inter-segment whitespace is kept between adjacent survivors and
//!    dropped runs are marked with a `[...]` elision line.
//! 6. **Restore** — placeholders are spliced back
//!    ([`super::tag_protector::restore_tags`]); since placeholder
//!    segments are mandatory keeps, restoration never discards a wrap.
//!
//! # Reversibility (STRICTER than the log/search siblings)
//!
//! The log/search compressors can ship dropped lines without a marker
//! when below their CCR thresholds. TextCrusher never does: **a crush
//! that drops segments ships if and only if the full original is stored
//! and the `[N segments compressed to M. Retrieve more: hash=…]` marker
//! is appended.** No store, `enable_ccr = false`, or savings below the
//! shippable threshold → byte-exact passthrough. Prose has no
//! line-number structure a reader could use to notice elisions, so an
//! unmarked drop would be silent loss — the invariant CCR-RETENTION.md
//! forbids. (The Python wrapper extends the same discipline across the
//! FFI: a production store-write failure vetoes the compression.)
//!
//! # Size floors (and why 600 / 15)
//!
//! `min_chars = 600` (~150 tokens): the marker line alone costs ~20-30
//! tokens, so on smaller inputs the ceiling on net savings (~85 tokens
//! at the default ratio) is marginal against the routing/CCR overhead.
//! `min_segments = 15`: mandatory keeps (first 2 + last 2) plus the
//! `min_kept_segments = 5` floor already retain ≥ 5 segments, i.e. a
//! third of a 15-segment document — the default `target_ratio = 0.35`.
//! Below 15 segments there is nothing meaningful left to drop.
//!
//! # Determinism
//!
//! No RNG, no clocks, no ML. Ordering is pinned everywhere: stable
//! sorts with explicit `(score desc, index asc)` tie-breaks,
//! `f32::total_cmp`, `BTreeSet` shingle signatures, and the
//! fixed-key `DefaultHasher`. Same input + config + query + bias →
//! byte-identical output (pinned by `determinism_byte_identical`).

use std::collections::BTreeSet;
use std::collections::HashSet;

use crate::ccr::persist::{md5_hex_24, retrieve_more_marker_line};
use crate::ccr::CcrStore;
use crate::ccr::RetrieveUnit;
use crate::relevance::{BM25Scorer, RelevanceScorer};
use crate::signals::{ImportanceContext, KeywordDetector, LineImportanceDetector};
use crate::transforms::tag_protector::{protect_tags, restore_tags};

// ─── Scoring weights (documented in the module docs) ────────────────────

/// Weight of the BM25-vs-query component (only when a query is given).
const W_QUERY: f32 = 0.40;
/// Weight of the U-shaped serial-position prior.
const W_POSITION: f32 = 0.20;
/// Weight of the keyword-detector priority (errors > warnings > notes).
const W_KEYWORD: f32 = 0.35;
/// Bonus for segments carrying numeric tokens (measurements, counts,
/// versions — high-information prose).
const BONUS_NUMERIC: f32 = 0.10;
/// Bonus for segments naming ≥ 2 capitalized entities mid-sentence.
const BONUS_ENTITY: f32 = 0.10;
/// Bonus for fenced code blocks (examples/commands embedded in prose).
const BONUS_CODE_FENCE: f32 = 0.15;

/// The elision line inserted where a run of segments was dropped. A
/// display cue only — recovery rides on the CCR marker, never on this.
const ELISION: &str = "[...]";

// ─── Config ─────────────────────────────────────────────────────────────

/// TextCrusher configuration. Mirrored 1:1 by the Python
/// `TextCrusherConfig` dataclass and the PyO3 kwargs constructor.
#[derive(Debug, Clone)]
pub struct TextCrusherConfig {
    /// Target compressed/original char ratio the selector aims for.
    pub target_ratio: f64,
    /// Inputs shorter than this many bytes pass through untouched.
    pub min_chars: usize,
    /// Inputs with fewer segments than this pass through untouched.
    pub min_segments: usize,
    /// Never keep fewer than this many segments (score-ordered top-up
    /// even when the char budget is exhausted).
    pub min_kept_segments: usize,
    /// The first N segments are mandatory keeps (topic statement).
    pub always_keep_first: usize,
    /// The last N segments are mandatory keeps (conclusion/recency).
    pub always_keep_last: usize,
    /// Word-shingle size for the near-duplicate collapse.
    pub shingle_size: usize,
    /// Jaccard similarity at or above which a segment is a duplicate.
    pub dedup_threshold: f64,
    /// Above this many segments, pairwise Jaccard is skipped and only
    /// exact normalized-hash dedup runs (bounds worst-case cost).
    pub max_pairwise_dedup_segments: usize,
    /// CCR backing. `false` disables the compressor outright (drops
    /// without recovery are forbidden — see module docs), it does NOT
    /// enable unmarked drops.
    pub enable_ccr: bool,
    /// Final ratio (marker included) at or above which the crush is not
    /// worth shipping: passthrough instead. Keeps "compressed output ⟺
    /// marker present ⟺ store backed" while refusing marginal crushes.
    pub max_shippable_ratio: f64,
}

impl Default for TextCrusherConfig {
    fn default() -> Self {
        Self {
            target_ratio: 0.35,
            min_chars: 600,
            min_segments: 15,
            min_kept_segments: 5,
            always_keep_first: 2,
            always_keep_last: 2,
            shingle_size: 4,
            dedup_threshold: 0.9,
            max_pairwise_dedup_segments: 2000,
            enable_ccr: true,
            max_shippable_ratio: 0.9,
        }
    }
}

// ─── Result + stats ─────────────────────────────────────────────────────

/// Crush result. `compressed == original` (with `cache_key == None`)
/// means passthrough — the input shipped byte-exact.
#[derive(Debug, Clone)]
pub struct TextCrushResult {
    pub compressed: String,
    pub original: String,
    pub original_segment_count: usize,
    pub compressed_segment_count: usize,
    /// Char-level ratio (compressed/original), parity with siblings.
    /// The router recomputes token-level ratios via its own counter
    /// (COR-17); this field is diagnostic.
    pub compression_ratio: f64,
    pub cache_key: Option<String>,
}

/// Sidecar diagnostics — same shape every Rust transform uses.
#[derive(Debug, Clone, Default)]
pub struct TextCrusherStats {
    pub segments_total: usize,
    pub segments_kept: usize,
    pub segments_dropped_by_dedup: usize,
    pub segments_dropped_by_budget: usize,
    /// Custom-tag blocks protected by the tag_protector rail.
    pub protected_tag_blocks: usize,
    /// Segments kept unconditionally (structure/placeholder/ends).
    pub mandatory_keeps: usize,
    pub ccr_emitted: bool,
    pub ccr_skip_reason: Option<&'static str>,
    /// Why the input passed through, when it did.
    pub passthrough_reason: Option<&'static str>,
}

// ─── Segmentation ───────────────────────────────────────────────────────

/// Structural kind of a segment. Drives mandatory keeps + salience.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SegmentKind {
    /// Sentence-ish prose run.
    Prose,
    /// Markdown ATX header line (`# …` through `###### …`).
    Header,
    /// Markdown list-item line (`- …`, `* …`, `+ …`, `1. …`, `1) …`).
    ListItem,
    /// Whole fenced code block (``` / ~~~), atomic — never split.
    CodeFence,
}

/// One segment of the cleaned (tag-protected) text. Spans are byte
/// offsets into the cleaned text; the bytes between one segment's `end`
/// and the next segment's `start` are whitespace by construction.
#[derive(Debug, Clone)]
pub struct Segment {
    pub idx: usize,
    pub start: usize,
    pub end: usize,
    pub kind: SegmentKind,
    pub has_placeholder: bool,
}

/// Line-first markdown-aware segmentation. Public for tests + the
/// segment-count floor probe.
pub fn segment_text(text: &str) -> Vec<Segment> {
    let bytes = text.as_bytes();
    let mut segments: Vec<Segment> = Vec::new();

    // Collect line spans [start, end) excluding the newline byte.
    let mut lines: Vec<(usize, usize)> = Vec::new();
    let mut ls = 0usize;
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'\n' {
            lines.push((ls, i));
            ls = i + 1;
        }
    }
    if ls <= bytes.len() {
        lines.push((ls, bytes.len()));
    }

    let push = |start: usize, end: usize, kind: SegmentKind, segments: &mut Vec<Segment>| {
        // Trim the span to content (no leading/trailing whitespace) so
        // inter-segment bytes are pure whitespace by construction.
        let slice = &text[start..end];
        let trimmed_start = start + (slice.len() - slice.trim_start().len());
        let trimmed_end = end - (slice.len() - slice.trim_end().len());
        if trimmed_start < trimmed_end {
            segments.push(Segment {
                idx: segments.len(),
                start: trimmed_start,
                end: trimmed_end,
                kind,
                has_placeholder: false,
            });
        }
    };

    let mut i = 0usize;
    // Paragraph accumulator: [pstart, pend) over raw text.
    let mut para: Option<(usize, usize)> = None;

    macro_rules! flush_para {
        () => {
            if let Some((ps, pe)) = para.take() {
                split_sentences(text, ps, pe, &mut segments);
            }
        };
    }

    while i < lines.len() {
        let (ls, le) = lines[i];
        let line = &text[ls..le];
        let trimmed = line.trim_start();

        if trimmed.is_empty() {
            flush_para!();
            i += 1;
            continue;
        }

        if let Some(fence) = fence_open(trimmed) {
            flush_para!();
            // Scan to the matching close (or EOF). The whole block —
            // opening fence, body, closing fence — is ONE atomic segment.
            let mut j = i + 1;
            let mut block_end = le;
            while j < lines.len() {
                let (js, je) = lines[j];
                if fence_close(text[js..je].trim_start(), fence) {
                    block_end = je;
                    j += 1;
                    break;
                }
                block_end = je;
                j += 1;
            }
            push(ls, block_end, SegmentKind::CodeFence, &mut segments);
            i = j;
            continue;
        }

        if is_header_line(trimmed) {
            flush_para!();
            push(ls, le, SegmentKind::Header, &mut segments);
            i += 1;
            continue;
        }

        if is_list_item_line(trimmed) {
            flush_para!();
            push(ls, le, SegmentKind::ListItem, &mut segments);
            i += 1;
            continue;
        }

        // Plain line: extend the current paragraph.
        para = match para {
            Some((ps, _)) => Some((ps, le)),
            None => Some((ls, le)),
        };
        i += 1;
    }
    flush_para!();

    segments
}

/// Fence opener: 3+ backticks or tildes (CommonMark-lite). Returns the
/// fence character + run length for close matching.
fn fence_open(trimmed: &str) -> Option<(u8, usize)> {
    let bytes = trimmed.as_bytes();
    let ch = *bytes.first()?;
    if ch != b'`' && ch != b'~' {
        return None;
    }
    let run = bytes.iter().take_while(|&&b| b == ch).count();
    if run >= 3 {
        Some((ch, run))
    } else {
        None
    }
}

/// Fence closer: same char, run length ≥ the opener, nothing else but
/// whitespace after the run.
fn fence_close(trimmed: &str, (ch, open_run): (u8, usize)) -> bool {
    let bytes = trimmed.as_bytes();
    let run = bytes.iter().take_while(|&&b| b == ch).count();
    run >= open_run && trimmed[run..].trim().is_empty()
}

/// ATX header: 1-6 `#` then space or end-of-line.
fn is_header_line(trimmed: &str) -> bool {
    let hashes = trimmed.bytes().take_while(|&b| b == b'#').count();
    if !(1..=6).contains(&hashes) {
        return false;
    }
    // MSRV 1.80: `Option::is_none_or` lands in 1.82, so match instead.
    match trimmed.as_bytes().get(hashes) {
        None => true,
        Some(&b) => b == b' ' || b == b'\t',
    }
}

/// List item: `- `, `* `, `+ `, or `1.`/`1)` (≤3 digits) + space.
fn is_list_item_line(trimmed: &str) -> bool {
    let bytes = trimmed.as_bytes();
    match bytes.first() {
        Some(b'-') | Some(b'*') | Some(b'+') => {
            matches!(bytes.get(1), Some(b' ') | Some(b'\t'))
        }
        Some(b) if b.is_ascii_digit() => {
            let digits = bytes.iter().take_while(|&&b| b.is_ascii_digit()).count();
            digits <= 3
                && matches!(bytes.get(digits), Some(b'.') | Some(b')'))
                && matches!(bytes.get(digits + 1), Some(b' ') | Some(b'\t'))
        }
        _ => false,
    }
}

/// Split a paragraph span into sentence-ish segments.
///
/// Boundary rule: a run of `.`/`!`/`?` (plus trailing ASCII closers
/// `)"'\]`), followed by whitespace whose next non-whitespace byte is
/// NOT ascii-lowercase, ends a sentence. Requiring a non-lowercase
/// continuation suppresses abbreviation splits (`e.g. foo`); numbers
/// (`3.14`) never split because the terminator must be followed by
/// whitespace. End-of-paragraph always terminates. All split points sit
/// at ASCII bytes, so byte slicing stays on char boundaries.
fn split_sentences(text: &str, pstart: usize, pend: usize, segments: &mut Vec<Segment>) {
    let bytes = text.as_bytes();
    let mut sent_start = pstart;
    // Skip leading whitespace.
    while sent_start < pend && bytes[sent_start].is_ascii_whitespace() {
        sent_start += 1;
    }

    let mut i = sent_start;
    while i < pend {
        let b = bytes[i];
        if b == b'.' || b == b'!' || b == b'?' {
            // Consume the full terminator run + ASCII closers.
            let mut j = i + 1;
            while j < pend && matches!(bytes[j], b'.' | b'!' | b'?') {
                j += 1;
            }
            while j < pend && matches!(bytes[j], b')' | b'"' | b'\'' | b']') {
                j += 1;
            }
            // Must be followed by whitespace (or paragraph end).
            if j >= pend || bytes[j].is_ascii_whitespace() {
                // Look ahead to the next non-whitespace byte.
                let mut k = j;
                while k < pend && bytes[k].is_ascii_whitespace() {
                    k += 1;
                }
                let continues_lowercase = k < pend && bytes[k].is_ascii_lowercase();
                if !continues_lowercase {
                    if sent_start < j {
                        segments.push(Segment {
                            idx: segments.len(),
                            start: sent_start,
                            end: j,
                            kind: SegmentKind::Prose,
                            has_placeholder: false,
                        });
                    }
                    sent_start = k;
                    i = k;
                    continue;
                }
            }
            i = j;
            continue;
        }
        i += 1;
    }

    // Paragraph tail.
    let tail = text[sent_start..pend].trim_end();
    if !tail.is_empty() {
        segments.push(Segment {
            idx: segments.len(),
            start: sent_start,
            end: sent_start + tail.len(),
            kind: SegmentKind::Prose,
            has_placeholder: false,
        });
    }
}

// ─── Compressor ─────────────────────────────────────────────────────────

/// Top-level deterministic prose compressor.
pub struct TextCrusher {
    config: TextCrusherConfig,
    keywords: KeywordDetector,
    bm25: BM25Scorer,
}

impl TextCrusher {
    pub fn new(config: TextCrusherConfig) -> Self {
        Self {
            config,
            keywords: KeywordDetector::new(),
            bm25: BM25Scorer::default(),
        }
    }

    pub fn config(&self) -> &TextCrusherConfig {
        &self.config
    }

    /// Compress without CCR persistence — always a passthrough for any
    /// input the selector would want to drop from (see module docs).
    /// Exists for parity with siblings; production callers use
    /// [`Self::compress_with_store`].
    pub fn compress(
        &self,
        content: &str,
        query: &str,
        bias: f64,
    ) -> (TextCrushResult, TextCrusherStats) {
        self.compress_with_store(content, query, bias, None)
    }

    /// Compress `content`, persisting the FULL ORIGINAL to `store` and
    /// appending the retrieval marker when segments are dropped. Any
    /// missing precondition for recovery (no store, CCR disabled,
    /// marginal savings) → byte-exact passthrough, never unmarked drops.
    pub fn compress_with_store(
        &self,
        content: &str,
        query: &str,
        bias: f64,
        store: Option<&dyn CcrStore>,
    ) -> (TextCrushResult, TextCrusherStats) {
        let mut stats = TextCrusherStats::default();

        if content.len() < self.config.min_chars {
            stats.passthrough_reason = Some("below min_chars");
            return (passthrough(content, 0), stats);
        }

        // Protection rail: swap custom workflow tags for placeholders
        // BEFORE segmentation so a tag block is opaque (and atomic) to
        // the splitter.
        let (cleaned, blocks, _protect_stats) = protect_tags(content, false);
        stats.protected_tag_blocks = blocks.len();

        let mut segments = segment_text(&cleaned);
        stats.segments_total = segments.len();
        if segments.len() < self.config.min_segments {
            stats.passthrough_reason = Some("below min_segments");
            return (passthrough(content, segments.len()), stats);
        }

        mark_placeholder_segments(&cleaned, &blocks, &mut segments);

        let scores = self.score_segments(&cleaned, &segments, query);
        let mandatory = self.mandatory_flags(&segments);
        stats.mandatory_keeps = mandatory.iter().filter(|&&m| m).count();

        let duplicate = self.duplicate_flags(&cleaned, &segments, &mandatory, &mut stats);

        let kept = self.select(&cleaned, &segments, &scores, &mandatory, &duplicate, bias);
        stats.segments_kept = kept.len();
        stats.segments_dropped_by_budget = segments
            .len()
            .saturating_sub(kept.len())
            .saturating_sub(stats.segments_dropped_by_dedup);

        if kept.len() == segments.len() {
            stats.passthrough_reason = Some("nothing dropped");
            return (passthrough(content, segments.len()), stats);
        }

        // Reversibility gates — all BEFORE the store write, so a
        // passthrough can never leave an orphan store entry.
        if !self.config.enable_ccr {
            stats.ccr_skip_reason = Some("ccr disabled — lossy prose compression requires backing");
            stats.passthrough_reason = Some("ccr disabled");
            return (passthrough(content, segments.len()), stats);
        }
        let Some(store) = store else {
            stats.ccr_skip_reason = Some("no store provided — refusing unmarked drops");
            stats.passthrough_reason = Some("no store");
            return (passthrough(content, segments.len()), stats);
        };

        let rendered = render(&cleaned, &segments, &kept);
        let restored = restore_tags(&rendered, &blocks);

        // Key + marker via the shared `ccr::persist` helpers (ARCH-5) —
        // but NOT `persist_and_mark`: the ratio veto below is computed
        // over the FINAL output (body + marker), so the store write must
        // wait until after the gate or a passthrough would leave an
        // orphan store entry.
        let key = md5_hex_24(content);
        let marker =
            retrieve_more_marker_line(segments.len(), kept.len(), &key, RetrieveUnit::Segments);
        let compressed = format!("{restored}{marker}");
        let ratio = compressed.len() as f64 / content.len().max(1) as f64;
        if ratio >= self.config.max_shippable_ratio {
            stats.ccr_skip_reason = Some("insufficient savings");
            stats.passthrough_reason = Some("insufficient savings");
            return (passthrough(content, segments.len()), stats);
        }

        store.put(&key, content);
        stats.ccr_emitted = true;

        (
            TextCrushResult {
                compressed,
                original: content.to_string(),
                original_segment_count: segments.len(),
                compressed_segment_count: kept.len(),
                compression_ratio: ratio,
                cache_key: Some(key),
            },
            stats,
        )
    }

    // ─── Scoring ────────────────────────────────────────────────────────

    fn score_segments(&self, cleaned: &str, segments: &[Segment], query: &str) -> Vec<f32> {
        let n = segments.len();
        let texts: Vec<&str> = segments.iter().map(|s| &cleaned[s.start..s.end]).collect();

        // BM25 against the query — batch call amortizes tokenization.
        let bm25: Vec<f32> = if query.trim().is_empty() {
            vec![0.0; n]
        } else {
            self.bm25
                .score_batch(&texts, query)
                .into_iter()
                .map(|s| s.score as f32)
                .collect()
        };

        segments
            .iter()
            .zip(texts.iter())
            .zip(bm25.iter())
            .map(|((seg, text), &q)| {
                // U-shaped serial-position prior: 1.0 at both ends,
                // 0.0 mid-document.
                let t = if n > 1 {
                    seg.idx as f32 / (n - 1) as f32
                } else {
                    0.0
                };
                let position = (2.0 * t - 1.0).abs();

                let signal = self.keywords.score(text, ImportanceContext::Text);
                let keyword = if signal.is_match() {
                    signal.priority
                } else {
                    0.0
                };

                let numeric = if text.bytes().any(|b| b.is_ascii_digit()) {
                    BONUS_NUMERIC
                } else {
                    0.0
                };
                let entity = if capitalized_entity_count(text) >= 2 {
                    BONUS_ENTITY
                } else {
                    0.0
                };
                let structure = if seg.kind == SegmentKind::CodeFence {
                    BONUS_CODE_FENCE
                } else {
                    0.0
                };

                (W_QUERY * q
                    + W_POSITION * position
                    + W_KEYWORD * keyword
                    + numeric
                    + entity
                    + structure)
                    .min(1.0)
            })
            .collect()
    }

    // ─── Mandatory keeps ────────────────────────────────────────────────

    fn mandatory_flags(&self, segments: &[Segment]) -> Vec<bool> {
        let n = segments.len();
        segments
            .iter()
            .map(|s| {
                s.has_placeholder
                    || s.kind == SegmentKind::Header
                    || s.idx < self.config.always_keep_first
                    || s.idx >= n.saturating_sub(self.config.always_keep_last)
            })
            .collect()
    }

    // ─── Shingle dedup ──────────────────────────────────────────────────

    /// Mark near-duplicate segments (first occurrence survives).
    /// Mandatory segments are never marked duplicates but DO register as
    /// dedup references, so later copies of a header/end segment still
    /// collapse against it.
    ///
    /// Three deterministic tiers:
    /// 1. exact normalized-word hash — verbatim repeats;
    /// 2. digit-masked hash — repeats that differ only in numerals
    ///    (progress counters, sequence numbers: the dominant prose
    ///    redundancy; same normalization idea as the log compressor's
    ///    conservative warning dedupe). The first occurrence keeps its
    ///    exact numbers; the varying copies are CCR-recoverable;
    /// 3. word-shingle Jaccard ≥ `dedup_threshold` on the masked words —
    ///    paraphrase-level overlap on longer segments. Skipped above
    ///    `max_pairwise_dedup_segments` (tiers 1-2 remain).
    fn duplicate_flags(
        &self,
        cleaned: &str,
        segments: &[Segment],
        mandatory: &[bool],
        stats: &mut TextCrusherStats,
    ) -> Vec<bool> {
        let n = segments.len();
        let pairwise = n <= self.config.max_pairwise_dedup_segments;
        let mut flags = vec![false; n];
        let mut exact_seen: HashSet<u64> = HashSet::new();
        let mut masked_seen: HashSet<u64> = HashSet::new();
        // Shingle signatures of prior unique segments.
        let mut signatures: Vec<BTreeSet<u64>> = Vec::new();

        for (i, seg) in segments.iter().enumerate() {
            let text = &cleaned[seg.start..seg.end];
            let words = normalized_words(text);
            if words.is_empty() {
                continue;
            }
            let masked: Vec<String> = words.iter().map(|w| mask_digits(w)).collect();

            let exact_new = exact_seen.insert(hash_u64(&words.join(" ")));
            let masked_new = masked_seen.insert(hash_u64(&masked.join(" ")));
            if !exact_new || !masked_new {
                if !mandatory[i] {
                    flags[i] = true;
                    stats.segments_dropped_by_dedup += 1;
                }
                continue;
            }

            if !pairwise || masked.len() < self.config.shingle_size {
                continue;
            }

            let sig: BTreeSet<u64> = masked
                .windows(self.config.shingle_size)
                .map(|w| hash_u64(&w.join(" ")))
                .collect();
            let is_dup = !mandatory[i]
                && signatures.iter().any(|prev| {
                    length_compatible(prev.len(), sig.len(), self.config.dedup_threshold)
                        && jaccard(prev, &sig) >= self.config.dedup_threshold
                });
            if is_dup {
                flags[i] = true;
                stats.segments_dropped_by_dedup += 1;
            } else {
                signatures.push(sig);
            }
        }
        flags
    }

    // ─── Selection ──────────────────────────────────────────────────────

    /// Pick survivor indices: mandatory keeps first, then score-ordered
    /// candidates under the char budget, floored at `min_kept_segments`.
    fn select(
        &self,
        cleaned: &str,
        segments: &[Segment],
        scores: &[f32],
        mandatory: &[bool],
        duplicate: &[bool],
        bias: f64,
    ) -> Vec<usize> {
        let budget =
            (cleaned.len() as f64 * self.config.target_ratio * bias.max(0.0)).ceil() as usize;

        let mut kept: Vec<usize> = Vec::new();
        let mut used = 0usize;
        for (i, seg) in segments.iter().enumerate() {
            if mandatory[i] {
                kept.push(i);
                used += seg.end - seg.start;
            }
        }

        let mut candidates: Vec<usize> = (0..segments.len())
            .filter(|&i| !mandatory[i] && !duplicate[i])
            .collect();
        candidates.sort_by(|&a, &b| scores[b].total_cmp(&scores[a]).then_with(|| a.cmp(&b)));

        for &i in &candidates {
            let len = segments[i].end - segments[i].start;
            if used + len <= budget || kept.len() < self.config.min_kept_segments {
                kept.push(i);
                used += len;
            }
        }

        kept.sort_unstable();
        kept
    }
}

// ─── Rendering ──────────────────────────────────────────────────────────

/// Stitch kept segments back together in original order. Adjacent
/// survivors keep their original inter-segment whitespace; a dropped run
/// becomes one `[...]` elision line. Leading/trailing drops get the same
/// elision so the reader can see the document was cut at the edges.
fn render(cleaned: &str, segments: &[Segment], kept: &[usize]) -> String {
    let mut out = String::with_capacity(cleaned.len());
    let mut prev: Option<usize> = None;
    for &i in kept {
        match prev {
            None => {
                if i > 0 {
                    out.push_str(ELISION);
                    out.push('\n');
                }
            }
            Some(p) => {
                if i == p + 1 {
                    // Original whitespace between adjacent survivors.
                    out.push_str(&cleaned[segments[p].end..segments[i].start]);
                } else {
                    if !out.ends_with('\n') {
                        out.push('\n');
                    }
                    out.push_str(ELISION);
                    out.push('\n');
                }
            }
        }
        out.push_str(&cleaned[segments[i].start..segments[i].end]);
        prev = Some(i);
    }
    if let Some(p) = prev {
        if p + 1 < segments.len() {
            if !out.ends_with('\n') {
                out.push('\n');
            }
            out.push_str(ELISION);
        }
    }
    out
}

// ─── Helpers ────────────────────────────────────────────────────────────

fn passthrough(content: &str, segment_count: usize) -> TextCrushResult {
    TextCrushResult {
        compressed: content.to_string(),
        original: content.to_string(),
        original_segment_count: segment_count,
        compressed_segment_count: segment_count,
        compression_ratio: 1.0,
        cache_key: None,
    }
}

/// Mark segments containing a tag-protector placeholder. Placeholders
/// contain no whitespace, so each sits fully inside exactly one segment
/// (segment boundaries only occur at whitespace).
fn mark_placeholder_segments(cleaned: &str, blocks: &[(String, String)], segments: &mut [Segment]) {
    for (placeholder, _) in blocks {
        let mut from = 0usize;
        while let Some(pos) = cleaned[from..].find(placeholder.as_str()) {
            let abs = from + pos;
            if let Some(seg) = segments.iter_mut().find(|s| s.start <= abs && abs < s.end) {
                seg.has_placeholder = true;
            }
            from = abs + placeholder.len();
        }
    }
}

/// Words lowercased and stripped to alphanumerics — the shingle unit.
fn normalized_words(text: &str) -> Vec<String> {
    text.split_whitespace()
        .filter_map(|w| {
            let cleaned: String = w
                .chars()
                .filter(|c| c.is_alphanumeric())
                .flat_map(|c| c.to_lowercase())
                .collect();
            if cleaned.is_empty() {
                None
            } else {
                Some(cleaned)
            }
        })
        .collect()
}

/// Collapse every ASCII digit run in a normalized word to `#` — the
/// unit of the digit-masked dedup tier ("run 17" ≡ "run 18").
fn mask_digits(word: &str) -> String {
    let mut out = String::with_capacity(word.len());
    let mut in_run = false;
    for c in word.chars() {
        if c.is_ascii_digit() {
            if !in_run {
                out.push('#');
                in_run = true;
            }
        } else {
            in_run = false;
            out.push(c);
        }
    }
    out
}

/// Count words with an uppercase initial that are NOT sentence-initial
/// (index > 0) — a cheap named-entity proxy.
fn capitalized_entity_count(text: &str) -> usize {
    text.split_whitespace()
        .skip(1)
        .filter(|w| {
            let mut chars = w.chars();
            matches!(chars.next(), Some(c) if c.is_uppercase())
                && matches!(chars.next(), Some(c) if c.is_alphabetic())
        })
        .count()
}

/// Sizes must be within a factor of each other for Jaccard ≥ t to be
/// possible: |A∩B| ≤ min ≤ max ≤ |A∪B| ⇒ J ≤ min/max.
fn length_compatible(a: usize, b: usize, threshold: f64) -> bool {
    let (min, max) = if a < b { (a, b) } else { (b, a) };
    max > 0 && (min as f64 / max as f64) >= threshold
}

fn jaccard(a: &BTreeSet<u64>, b: &BTreeSet<u64>) -> f64 {
    let inter = a.intersection(b).count();
    let union = a.len() + b.len() - inter;
    if union == 0 {
        return 0.0;
    }
    inter as f64 / union as f64
}

/// Deterministic 64-bit hash (fixed-key SipHash via `DefaultHasher`).
fn hash_u64(s: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    s.hash(&mut h);
    h.finish()
}

// `md5_hex_24` (the CCR cache key, same algorithm as the diff/log/search
// siblings) lives in `crate::ccr::persist` — one shared implementation
// (ARCH-5), imported at the top of this module.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ccr::InMemoryCcrStore;

    const SUBJECTS: [&str; 8] = [
        "The scheduler",
        "Our ingestion service",
        "The billing worker",
        "A background daemon",
        "The metrics exporter",
        "The auth gateway",
        "This migration script",
        "The cache layer",
    ];
    const VERBS: [&str; 8] = [
        "processed",
        "rejected",
        "queued",
        "archived",
        "replicated",
        "validated",
        "throttled",
        "reindexed",
    ];
    const OBJECTS: [&str; 8] = [
        "customer records",
        "audit events",
        "payment batches",
        "session tokens",
        "search documents",
        "webhook deliveries",
        "schema versions",
        "trace spans",
    ];
    const TAILS: [&str; 5] = [
        "before the morning deadline without operator intervention",
        "while the standby region absorbed the overflow traffic",
        "although the retry queue kept growing steadily",
        "and the on-call engineer confirmed the dashboards stayed green",
        "despite intermittent packet loss on the private link",
    ];

    /// Lexically varied filler sentence — combinations stay distinct
    /// under the digit-masked dedup tier (no counter-only variation).
    fn varied_filler(i: usize) -> String {
        format!(
            "{} {} {} {}.",
            SUBJECTS[i % 8],
            VERBS[(i * 3 + 1) % 8],
            OBJECTS[(i * 5 + 2) % 8],
            TAILS[i % 5]
        )
    }

    /// 40 lexically varied sentences across 8 paragraphs — clears both
    /// floors with plenty of droppable material, and no two sentences
    /// collapse under any dedup tier (verified: index arithmetic keeps
    /// all (subject, verb, object, tail) combos distinct).
    fn big_prose() -> String {
        let mut paras: Vec<String> = Vec::new();
        for p in 0..8usize {
            let mut sentences = Vec::new();
            for s in 0..5usize {
                sentences.push(format!(
                    "{} {} {} {}.",
                    SUBJECTS[(p + s) % 8],
                    VERBS[(p * 3 + s) % 8],
                    OBJECTS[(p + 2 * s) % 8],
                    TAILS[(p + s) % 5]
                ));
            }
            paras.push(sentences.join(" "));
        }
        paras.join("\n\n")
    }

    fn crusher() -> TextCrusher {
        TextCrusher::new(TextCrusherConfig::default())
    }

    // ─── Floors ────────────────────────────────────────────────────────

    #[test]
    fn passthrough_below_min_chars() {
        let content = "Tiny prose. Two sentences only.";
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher().compress_with_store(content, "", 1.0, Some(&store));
        assert_eq!(result.compressed, content);
        assert_eq!(result.compression_ratio, 1.0);
        assert!(result.cache_key.is_none());
        assert_eq!(stats.passthrough_reason, Some("below min_chars"));
        assert_eq!(store.len(), 0, "no orphan store writes on passthrough");
    }

    #[test]
    fn passthrough_below_min_segments() {
        // Above min_chars but only a handful of long sentences.
        let content = format!(
            "{} {} {}",
            "A".repeat(250),
            "B".repeat(250),
            "C".repeat(250)
        );
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert_eq!(result.compressed, content);
        assert_eq!(stats.passthrough_reason, Some("below min_segments"));
        assert_eq!(store.len(), 0);
    }

    // ─── Segmentation ──────────────────────────────────────────────────

    #[test]
    fn segmentation_sentences_split_on_terminators() {
        let text = "First sentence here. Second one follows! Third asks a question? Fourth ends.";
        let segs = segment_text(text);
        assert_eq!(segs.len(), 4, "got {:?}", segs);
        assert_eq!(&text[segs[0].start..segs[0].end], "First sentence here.");
        assert_eq!(&text[segs[2].start..segs[2].end], "Third asks a question?");
    }

    #[test]
    fn segmentation_does_not_split_abbreviations_or_decimals() {
        let text = "The value of pi is 3.14 exactly. Use e.g. the standard library.";
        let segs = segment_text(text);
        assert_eq!(segs.len(), 2, "got {:?}", segs);
        assert_eq!(
            &text[segs[0].start..segs[0].end],
            "The value of pi is 3.14 exactly."
        );
    }

    #[test]
    fn segmentation_markdown_structure_boundaries() {
        let text = "# Title\n\nIntro sentence one. Intro sentence two.\n\n\
                    - item alpha\n- item beta\n\n```rust\nfn main() {}\nlet x = 1;\n```\n\nOutro.";
        let segs = segment_text(text);
        let kinds: Vec<SegmentKind> = segs.iter().map(|s| s.kind).collect();
        assert_eq!(
            kinds,
            vec![
                SegmentKind::Header,
                SegmentKind::Prose,
                SegmentKind::Prose,
                SegmentKind::ListItem,
                SegmentKind::ListItem,
                SegmentKind::CodeFence,
                SegmentKind::Prose,
            ],
            "got {:?}",
            segs
        );
    }

    #[test]
    fn segmentation_code_fence_is_atomic() {
        // Sentences inside a fence must NOT split; the whole block is
        // one segment including both fence lines.
        let text =
            "Before text.\n\n```\nFirst line. Second line! Third?\nMore code.\n```\n\nAfter.";
        let segs = segment_text(text);
        let fence: Vec<&Segment> = segs
            .iter()
            .filter(|s| s.kind == SegmentKind::CodeFence)
            .collect();
        assert_eq!(fence.len(), 1);
        let body = &text[fence[0].start..fence[0].end];
        assert!(body.starts_with("```") && body.ends_with("```"), "{body:?}");
        assert!(body.contains("First line. Second line! Third?"));
    }

    #[test]
    fn segmentation_unclosed_fence_extends_to_eof() {
        let text = "Intro.\n\n```python\ncode line one\ncode line two";
        let segs = segment_text(text);
        let last = segs.last().unwrap();
        assert_eq!(last.kind, SegmentKind::CodeFence);
        assert!(&text[last.start..last.end].ends_with("code line two"));
    }

    // ─── Determinism ───────────────────────────────────────────────────

    #[test]
    fn determinism_byte_identical() {
        let content = big_prose();
        let store_a = InMemoryCcrStore::new();
        let store_b = InMemoryCcrStore::new();
        let (a, _) = crusher().compress_with_store(&content, "topic 17", 1.0, Some(&store_a));
        let (b, _) = crusher().compress_with_store(&content, "topic 17", 1.0, Some(&store_b));
        assert_eq!(a.compressed, b.compressed);
        assert_eq!(a.cache_key, b.cache_key);
        // And across construction (no per-instance state).
        let (c, _) = TextCrusher::new(TextCrusherConfig::default()).compress_with_store(
            &content,
            "topic 17",
            1.0,
            Some(&InMemoryCcrStore::new()),
        );
        assert_eq!(a.compressed, c.compressed);
    }

    // ─── Dedup ─────────────────────────────────────────────────────────

    #[test]
    fn shingle_dedup_collapses_near_identical_sentences() {
        // 30 near-copies (only a run counter varies) + 10 lexically
        // distinct sentences. The copies must collapse via the
        // digit-masked tier; the distinct survivors carry the info.
        let mut sentences: Vec<String> = Vec::new();
        for i in 0..30 {
            sentences.push(format!(
                "The deployment pipeline completed successfully with all checks passing in run {i}."
            ));
        }
        for i in 0..10 {
            sentences.push(varied_filler(i));
        }
        let content = sentences.join(" ");
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(stats.segments_dropped_by_dedup >= 25, "stats: {stats:?}");
        assert!(result.compressed.len() < content.len() / 2);
        // First occurrence survives with its exact numerals.
        assert!(result.compressed.contains("in run 0."));
        // The counter-varying copies do not.
        assert!(!result.compressed.contains("in run 7."));
    }

    #[test]
    fn lexically_distinct_sentences_are_not_deduped() {
        let content = big_prose();
        let store = InMemoryCcrStore::new();
        let (_, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert_eq!(
            stats.segments_dropped_by_dedup, 0,
            "varied prose must not collapse: {stats:?}"
        );
    }

    #[test]
    fn exact_duplicate_short_sentences_collapse() {
        let mut sentences = vec!["Retry.".to_string(); 40];
        for i in 0..10 {
            sentences.push(varied_filler(i));
        }
        let content = sentences.join(" ");
        let store = InMemoryCcrStore::new();
        let (_, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(
            stats.segments_dropped_by_dedup >= 35,
            "short exact dupes must collapse: {stats:?}"
        );
    }

    // ─── Selection ─────────────────────────────────────────────────────

    #[test]
    fn selection_respects_target_ratio_with_floor() {
        let content = big_prose();
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(result.cache_key.is_some(), "large prose must crush");
        // Ratio lands under the shippable cap and above nothing-kept.
        assert!(
            result.compression_ratio < 0.9,
            "{}",
            result.compression_ratio
        );
        assert!(stats.segments_kept >= 5);
        // First and last segments (mandatory) survive — derive them from
        // the segmenter itself so the assertion tracks the fixture.
        let segs = segment_text(&content);
        let first = &content[segs[0].start..segs[0].end];
        let last_seg = segs.last().unwrap();
        let last = &content[last_seg.start..last_seg.end];
        assert!(result.compressed.starts_with(first), "first mandatory keep");
        assert!(result.compressed.contains(last), "last mandatory keep");
    }

    #[test]
    fn bias_above_one_keeps_more() {
        let content = big_prose();
        let (lean, _) =
            crusher().compress_with_store(&content, "", 1.0, Some(&InMemoryCcrStore::new()));
        let (fat, _) =
            crusher().compress_with_store(&content, "", 2.0, Some(&InMemoryCcrStore::new()));
        assert!(
            fat.compressed_segment_count >= lean.compressed_segment_count,
            "bias 2.0 must keep at least as many segments"
        );
    }

    #[test]
    fn query_context_biases_selection() {
        let mut sentences: Vec<String> = (0..40).map(varied_filler).collect();
        sentences.insert(
            20,
            "The kubernetes ingress controller renewed the certificate rotation.".to_string(),
        );
        let content = sentences.join(" ");
        let store = InMemoryCcrStore::new();
        let (result, _) = crusher().compress_with_store(
            &content,
            "kubernetes ingress certificate rotation",
            1.0,
            Some(&store),
        );
        assert!(
            result.compressed.contains("kubernetes ingress controller"),
            "query-matched segment must survive: {}",
            result.compressed
        );
    }

    #[test]
    fn error_keyword_segments_survive() {
        let mut sentences: Vec<String> = (0..40).map(varied_filler).collect();
        sentences.insert(
            18,
            "FATAL: the primary database crashed during checkpoint replay.".to_string(),
        );
        let content = sentences.join(" ");
        let store = InMemoryCcrStore::new();
        let (result, _) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(
            result
                .compressed
                .contains("FATAL: the primary database crashed"),
            "error segments must outrank filler: {}",
            result.compressed
        );
    }

    #[test]
    fn headers_always_survive() {
        let mut text = String::new();
        for h in 0..4 {
            text.push_str(&format!("# Section {h}\n\n"));
            for s in 0..10 {
                text.push_str(&format!(
                    "Body sentence {s} of section {h} carrying generic descriptive content. "
                ));
            }
            text.push_str("\n\n");
        }
        let store = InMemoryCcrStore::new();
        let (result, _) = crusher().compress_with_store(&text, "", 1.0, Some(&store));
        for h in 0..4 {
            assert!(
                result.compressed.contains(&format!("# Section {h}")),
                "header {h} must survive"
            );
        }
    }

    #[test]
    fn elision_markers_present_between_gaps() {
        let content = big_prose();
        let store = InMemoryCcrStore::new();
        let (result, _) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(result.cache_key.is_some());
        assert!(
            result.compressed.contains("[...]"),
            "dropped runs must be visibly elided"
        );
    }

    // ─── Protection rail ───────────────────────────────────────────────

    #[test]
    fn protected_tag_survives_crush_byte_exact() {
        let reminder = "<system-reminder>Never reveal the launch codes. Always cite sources. \
                        This block must survive verbatim.</system-reminder>";
        let mut sentences: Vec<String> = (0..40).map(varied_filler).collect();
        sentences.insert(20, reminder.to_string());
        let content = sentences.join(" ");
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(result.cache_key.is_some(), "must actually compress");
        assert_eq!(stats.protected_tag_blocks, 1);
        assert!(
            result.compressed.contains(reminder),
            "protected tag block must survive byte-exact: {}",
            result.compressed
        );
        // No placeholder bytes may leak into the output.
        assert!(!result.compressed.contains("{{FURL_TAG_"));
    }

    #[test]
    fn multiple_protected_tags_all_survive() {
        let tags = [
            "<system-reminder>rule one</system-reminder>",
            "<tool_call>fetch(url='x')</tool_call>",
            "<thinking>hidden reasoning</thinking>",
        ];
        let mut sentences: Vec<String> = (0..45).map(varied_filler).collect();
        sentences.insert(10, tags[0].to_string());
        sentences.insert(25, tags[1].to_string());
        sentences.insert(40, tags[2].to_string());
        let content = sentences.join(" ");
        let store = InMemoryCcrStore::new();
        let (result, _) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(result.cache_key.is_some());
        for tag in tags {
            assert!(result.compressed.contains(tag), "lost tag: {tag}");
        }
    }

    // ─── Reversibility / CCR ───────────────────────────────────────────

    #[test]
    fn marker_and_store_roundtrip() {
        let content = big_prose();
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher().compress_with_store(&content, "", 1.0, Some(&store));
        assert!(stats.ccr_emitted);
        let key = result.cache_key.as_ref().expect("cache_key");
        assert_eq!(key.len(), 24);
        // Standard marker grammar, "segments" unit.
        assert!(
            result.compressed.contains(&format!(
                "segments compressed to {}. Retrieve more: hash={}]",
                result.compressed_segment_count, key
            )),
            "marker missing/malformed: {}",
            result.compressed
        );
        assert!(result
            .compressed
            .contains(&format!("[{} segments", result.original_segment_count)));
        // Byte-exact original behind the hash.
        assert_eq!(store.get(key).unwrap(), content);
    }

    #[test]
    fn no_store_provided_vetoes_to_passthrough() {
        let content = big_prose();
        let (result, stats) = crusher().compress(&content, "", 1.0);
        assert_eq!(result.compressed, content, "unmarked drops are forbidden");
        assert!(result.cache_key.is_none());
        assert_eq!(stats.passthrough_reason, Some("no store"));
        assert_eq!(
            stats.ccr_skip_reason,
            Some("no store provided — refusing unmarked drops")
        );
    }

    #[test]
    fn ccr_disabled_vetoes_to_passthrough() {
        let content = big_prose();
        let crusher = TextCrusher::new(TextCrusherConfig {
            enable_ccr: false,
            ..Default::default()
        });
        let store = InMemoryCcrStore::new();
        let (result, stats) = crusher.compress_with_store(&content, "", 1.0, Some(&store));
        assert_eq!(result.compressed, content);
        assert_eq!(stats.passthrough_reason, Some("ccr disabled"));
        assert_eq!(store.len(), 0, "no store writes when disabled");
    }

    #[test]
    fn insufficient_savings_means_passthrough_and_no_store_write() {
        // Force marginal savings via a target_ratio near 1: everything
        // fits the budget except a couple of segments, so the marker
        // overhead pushes the final ratio past max_shippable_ratio.
        let crusher = TextCrusher::new(TextCrusherConfig {
            target_ratio: 0.99,
            max_shippable_ratio: 0.9,
            ..Default::default()
        });
        let content = big_prose();
        let store = InMemoryCcrStore::new();
        let (result, _stats) = crusher.compress_with_store(&content, "", 1.0, Some(&store));
        assert_eq!(result.compressed, content);
        assert!(result.cache_key.is_none());
        assert_eq!(store.len(), 0, "no orphan store write on passthrough");
    }

    #[test]
    fn compressed_output_always_carries_marker_when_dropping() {
        // The core invariant: output != original ⟹ marker + store entry.
        let content = big_prose();
        let store = InMemoryCcrStore::new();
        let (result, _) = crusher().compress_with_store(&content, "q", 1.0, Some(&store));
        if result.compressed != content {
            assert!(result.compressed.contains("Retrieve more: hash="));
            assert_eq!(store.len(), 1);
            assert_eq!(
                store.get(result.cache_key.as_ref().unwrap()).unwrap(),
                content
            );
        }
    }
}
