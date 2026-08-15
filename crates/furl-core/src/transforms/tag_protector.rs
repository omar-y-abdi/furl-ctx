//! Protect custom workflow/XML-like tags from prose compression by replacing matched spans with opaque placeholders, forcing placeholder
//! segments to survive selection, then restoring originals. Standard HTML tags are not protected unless configured as custom.

use std::collections::HashSet;
use std::sync::OnceLock;

/// HTML5 living-standard element names — the set of tags this module will NEVER protect
/// (they're handled at a different layer; everything else is treated as custom).
const HTML5_TAGS: &[&str] = &[
    // Main root
    "html",
    // Document metadata
    "base",
    "head",
    "link",
    "meta",
    "style",
    "title",
    // Sectioning root
    "body",
    // Content sectioning
    "address",
    "article",
    "aside",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hgroup",
    "main",
    "nav",
    "section",
    "search",
    // Text content
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "hr",
    "li",
    "menu",
    "ol",
    "p",
    "pre",
    "ul",
    // Inline text semantics
    "a",
    "abbr",
    "b",
    "bdi",
    "bdo",
    "br",
    "cite",
    "code",
    "data",
    "dfn",
    "em",
    "i",
    "kbd",
    "mark",
    "q",
    "rp",
    "rt",
    "ruby",
    "s",
    "samp",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "time",
    "u",
    "var",
    "wbr",
    // Image and multimedia
    "area",
    "audio",
    "img",
    "map",
    "track",
    "video",
    // Embedded content
    "embed",
    "iframe",
    "object",
    "param",
    "picture",
    "portal",
    "source",
    // SVG and MathML
    "svg",
    "math",
    // Scripting
    "canvas",
    "noscript",
    "script",
    // Demarcating edits
    "del",
    "ins",
    // Table content
    "caption",
    "col",
    "colgroup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    // Forms
    "button",
    "datalist",
    "fieldset",
    "form",
    "input",
    "label",
    "legend",
    "meter",
    "optgroup",
    "option",
    "output",
    "progress",
    "select",
    "textarea",
    // Interactive
    "details",
    "dialog",
    "summary",
    // Web Components
    "slot",
    "template",
];

fn known_html_tags() -> &'static HashSet<&'static str> {
    static SET: OnceLock<HashSet<&'static str>> = OnceLock::new();
    SET.get_or_init(|| HTML5_TAGS.iter().copied().collect())
}

/// Default placeholder prefix. Brace-doubled to look unlike anything a real workflow tag would emit. Falls
/// back to a salted variant if the input itself contains the prefix (see [`pick_placeholder_prefix`]).
const DEFAULT_PREFIX: &str = "{{FURL_TAG_";
const PLACEHOLDER_SUFFIX: &str = "}}";

/// Case-insensitive HTML tag check. Lowercases the input lazily so we
/// don't allocate for the common ASCII-lowercase case.
pub fn is_known_html_tag(tag_name: &str) -> bool {
    let set = known_html_tags();
    if set.contains(tag_name) {
        return true;
    }
    if tag_name.bytes().any(|b| b.is_ascii_uppercase()) {
        let lower = tag_name.to_ascii_lowercase();
        return set.contains(lower.as_str());
    }
    false
}

/// Pick a placeholder prefix that doesn't collide with anything in `text`. The salt is bounded; we never need more than one attempt.
fn pick_placeholder_prefix(text: &str) -> (String, bool) {
    if !text.contains(DEFAULT_PREFIX) {
        return (DEFAULT_PREFIX.to_string(), false);
    }
    for salt in 0u32..16 {
        let candidate = format!("{{{{FURL_TAG_{salt}_");
        if !text.contains(&candidate) {
            return (candidate, true);
        }
    }
    // 16 salt attempts collided — fall back to a UUID-shaped marker. The OnceLock cache
    // is so two consecutive calls in the same process don't pay the formatting cost.
    static FALLBACK: OnceLock<String> = OnceLock::new();
    let prefix = FALLBACK
        .get_or_init(|| "{{FURL_TAG_FALLBACK_a4f1c7e2_".to_string())
        .clone();
    (prefix, true)
}

#[derive(Debug)]
struct OpenTag {
    /// Lowercase name for case-insensitive close-matching.
    name_lower: String,
    /// Byte offset of the `<` that opened this tag.
    open_start: usize,
}

/// Outcome of a single `<…>` parse attempt at a given offset.
enum TagParse {
    /// Opening tag (`<name attr=…>`). `name_end` is exclusive.
    Open {
        name_end: usize,
        tag_end: usize,
        is_self_closing: bool,
    },
    /// Closing tag (`</name>`).
    Close { name_end: usize, tag_end: usize },
    /// Not a tag (e.g. `<` followed by whitespace or digit).
    NotTag,
}

/// Parse a `<…>` starting at `start`. Conservatively rejects malformed shapes — we'd rather emit a `<` verbatim than over-protect on bad input.
fn parse_tag_at(bytes: &[u8], start: usize) -> TagParse {
    debug_assert!(bytes[start] == b'<');
    let mut i = start + 1;
    let n = bytes.len();
    if i >= n {
        return TagParse::NotTag;
    }

    let is_close = bytes[i] == b'/';
    if is_close {
        i += 1;
    }
    // After consuming a possible '/' we may be at end-of-input (e.g. literal `</` with nothing after).
    if i >= n {
        return TagParse::NotTag;
    }
    let name_start = i;
    if !is_name_start(bytes[i]) {
        return TagParse::NotTag;
    }
    i += 1;
    while i < n && is_name_cont(bytes[i]) {
        i += 1;
    }
    let name_end = i;
    if name_end == name_start {
        return TagParse::NotTag;
    }

    if is_close {
        // Allow optional whitespace, then `>`.
        while i < n && bytes[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= n || bytes[i] != b'>' {
            return TagParse::NotTag;
        }
        return TagParse::Close {
            name_end,
            tag_end: i + 1,
        };
    }

    // Opening tag: skip attributes until `>` (handle `/>` for self-closing). Quoted attribute
    // values can contain `>`; a single-pass attribute lexer handles the common cases.
    let mut self_closing = false;
    while i < n {
        match bytes[i] {
            b'>' => {
                return TagParse::Open {
                    name_end,
                    tag_end: i + 1,
                    is_self_closing: self_closing,
                };
            }
            b'/' => {
                // Self-closing ONLY when the `/` immediately precedes `>` (`.../>`). A bare `/` elsewhere is ordinary attribute-value text — an unquoted URL like
                // `url=http://x.com` contains slashes that must NOT flip the tag to self-closing (COR-9), or the body gets exposed and the close tag orphaned.
                self_closing = i + 1 < n && bytes[i + 1] == b'>';
                i += 1;
            }
            b'"' | b'\'' => {
                let quote = bytes[i];
                i += 1;
                while i < n && bytes[i] != quote {
                    i += 1;
                }
                if i >= n {
                    return TagParse::NotTag;
                }
                i += 1;
                self_closing = false;
            }
            _ => {
                i += 1;
            }
        }
    }

    TagParse::NotTag
}

#[inline]
fn is_name_start(b: u8) -> bool {
    b.is_ascii_alphabetic() || b == b'_'
}

#[inline]
fn is_name_cont(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':')
}

/// A single span that was identified as worth replacing. In marker-only mode each opening custom tag and
/// each closing custom tag becomes its own Span (the body between them is left visible to the compressor).
#[derive(Debug, Clone, Copy)]
struct Span {
    start: usize,
    end: usize,
}

/// Protect custom workflow tags from text compression. replace each entire `<custom>…</custom>` span (including nested children) with a single placeholder.
/// replace only the tag markers (open and close emitted as separate placeholders) so the compressor can squash content while the tag boundaries survive.
pub fn protect_tags(text: &str, compress_tagged_content: bool) -> (String, Vec<(String, String)>) {
    if text.is_empty() || !text.contains('<') {
        return (text.to_string(), Vec::new());
    }

    let (prefix, _salted) = pick_placeholder_prefix(text);

    //
    let spans = identify_spans(text, compress_tagged_content);

    // Because `spans` is sorted left-to-right and non-overlapping (block mode collapses nested matches into the outermost
    // span; marker mode emits open/close markers that are byte-disjoint by construction) this is a straightforward scan.
    match emit_output(text, &spans, &prefix) {
        Some((cleaned, blocks)) => (cleaned, blocks),
        // Should be unreachable — `identify_spans` returns spans whose bytes are slices of
        // `text`. If we ever fail to splice them back, fall back to emitting the original.
        None => (text.to_string(), Vec::new()),
    }
}

fn identify_spans(text: &str, compress_tagged_content: bool) -> Vec<Span> {
    let bytes = text.as_bytes();
    let n = bytes.len();
    let mut spans: Vec<Span> = Vec::new();
    let mut stack: Vec<OpenTag> = Vec::new();

    let mut i = 0;
    while i < n {
        let b = bytes[i];
        if b != b'<' {
            // Skip ahead to the next `<`. We don't care about non-tag bytes for span identification; they'll be copied verbatim in phase 2.
            i = memchr(b'<', &bytes[i..]).map(|j| i + j).unwrap_or(n);
            continue;
        }

        match parse_tag_at(bytes, i) {
            TagParse::NotTag => {
                i += 1;
            }
            TagParse::Open {
                name_end,
                tag_end,
                is_self_closing,
            } => {
                let name = &text[i + 1..name_end];
                if is_known_html_tag(name) {
                    i = tag_end;
                    continue;
                }
                if is_self_closing {
                    spans.push(Span {
                        start: i,
                        end: tag_end,
                    });
                    i = tag_end;
                    continue;
                }
                if compress_tagged_content {
                    // Marker-only mode: emit the open as its own span *and* push the name on the stack so the close gets matched and emitted as its own span.
                    spans.push(Span {
                        start: i,
                        end: tag_end,
                    });
                }
                // Both modes push to the stack so close-matching works.
                stack.push(OpenTag {
                    name_lower: name.to_ascii_lowercase(),
                    open_start: i,
                });
                i = tag_end;
            }
            TagParse::Close { name_end, tag_end } => {
                let close_name = &text[i + 2..name_end];
                if is_known_html_tag(close_name) {
                    i = tag_end;
                    continue;
                }
                let close_name_lower = close_name.to_ascii_lowercase();
                let matching = stack
                    .iter()
                    .rposition(|open| open.name_lower == close_name_lower);

                match matching {
                    Some(stack_idx) => {
                        if compress_tagged_content {
                            // Remove the matched open tag AND every orphan open nested inside it (their open markers were already recorded as spans
                            // and we keep them). `truncate(stack_idx)` drops the matched tag at `stack_idx` and everything above it in ONE step.
                            stack.truncate(stack_idx);
                            spans.push(Span {
                                start: i,
                                end: tag_end,
                            });
                        } else {
                            // Block mode: collapse [open..close] into a single span.
                            let open_start = stack[stack_idx].open_start;
                            stack.truncate(stack_idx);
                            spans.retain(|s| s.start < open_start);
                            spans.push(Span {
                                start: open_start,
                                end: tag_end,
                            });
                        }
                        i = tag_end;
                    }
                    None => {
                        i = tag_end;
                    }
                }
            }
        }
    }

    // Stack remnants are orphan opens (no matching close ever arrived). In block mode their inner self-closing spans
    // we recorded are still safe to keep: they were below an unmatched outer open, so they were never collapsed.
    spans
}

fn emit_output(
    text: &str,
    spans: &[Span],
    prefix: &str,
) -> Option<(String, Vec<(String, String)>)> {
    let mut out = String::with_capacity(text.len());
    let mut blocks: Vec<(String, String)> = Vec::new();
    let mut cursor: usize = 0;

    for (counter, span) in (0_u64..).zip(spans.iter()) {
        if span.start < cursor {
            // Overlap shouldn't happen given how we collapse nested spans, but bail loudly
            // if it does — silently producing wrong output is worse than failing the test.
            return None;
        }
        out.push_str(&text[cursor..span.start]);
        let placeholder = format!("{prefix}{counter}{PLACEHOLDER_SUFFIX}");
        let original = &text[span.start..span.end];
        blocks.push((placeholder.clone(), original.to_string()));
        out.push_str(&placeholder);
        cursor = span.end;
    }
    out.push_str(&text[cursor..]);
    Some((out, blocks))
}

/// Restore protected blocks with one placeholder regex pass, validating request IDs when present. Unknown or mismatched
/// placeholders stay literal; restored content is not rescanned, preventing recursive substitution and quadratic replacement.
pub fn restore_tags(text: &str, blocks: &[(String, String)]) -> String {
    restore_tags_with_request_id(text, blocks, None)
}

/// Placeholder restoration is single-pass and non-recursive. Build the lookup once, replace recognized tokens
/// directly, and leave unknown tokens unchanged so restored payload text cannot trigger a second substitution.
pub fn restore_tags_with_request_id(
    text: &str,
    blocks: &[(String, String)],
    request_id: Option<&str>,
) -> String {
    if blocks.is_empty() {
        return text.to_string();
    }

    // Defensive filter: an empty placeholder would match at every
    // position (byte-injection everywhere). Skip such blocks loudly.
    let valid: Vec<(usize, &(String, String))> = blocks
        .iter()
        .enumerate()
        .filter(|(_, (placeholder, original))| {
            if placeholder.is_empty() {
                tracing::error!(
                    target: "furl::tag_protector",
                    event = "tag_protector_empty_placeholder",
                    original_preview = %original.chars().take(80).collect::<String>(),
                    "empty placeholder in restore blocks — skipped"
                );
                false
            } else {
                true
            }
        })
        .collect();
    if valid.is_empty() {
        return text.to_string();
    }

    let patterns: Vec<&str> = valid.iter().map(|(_, (p, _))| p.as_str()).collect();
    let Ok(automaton) = aho_corasick::AhoCorasick::builder()
        .match_kind(aho_corasick::MatchKind::LeftmostLongest)
        .build(&patterns)
    else {
        // Automaton construction can only fail on pathological pattern sets (size limits). Treat every block as lost rather than guessing at substitutions.
        for (_, (_, original)) in &valid {
            tag_lost_error(original, text.len(), request_id);
        }
        return text.to_string();
    };

    let mut result = String::with_capacity(text.len());
    let mut used = vec![false; valid.len()];
    let mut cursor = 0usize;
    for m in automaton.find_iter(text) {
        let pattern_idx = m.pattern().as_usize();
        result.push_str(&text[cursor..m.start()]);
        if used[pattern_idx] {
            // Duplicate occurrence of an already-substituted placeholder: compressor-fabricated bytes, kept verbatim (never a second copy of the protected span).
            tracing::warn!(
                target: "furl::tag_protector",
                event = "tag_protector_duplicate_placeholder",
                placeholder = %&text[m.start()..m.end()],
                "duplicate placeholder occurrence left verbatim"
            );
            result.push_str(&text[m.start()..m.end()]);
        } else {
            used[pattern_idx] = true;
            let (_, (_, original)) = valid[pattern_idx];
            result.push_str(original);
        }
        cursor = m.end();
    }
    result.push_str(&text[cursor..]);

    // Lost placeholders (never seen in the compressed text): the wrap is DISCARDED — no orphan-tag
    // append (Hotfix-A9) — with a structured ERROR per block so operators can alert on the corruption.
    let compressed_length = text.len();
    for (i, (_, (_, original))) in valid.iter().enumerate() {
        if !used[i] {
            tag_lost_error(original, compressed_length, request_id);
        }
    }
    result
}

#[inline(never)]
fn tag_lost_error(original: &str, compressed_length: usize, request_id: Option<&str>) {
    let preview: String = original.chars().take(80).collect();
    match request_id {
        Some(rid) => tracing::error!(
            target: "furl::tag_protector",
            event = "tag_protector_placeholder_lost",
            tag_preview = %preview,
            compressed_length = compressed_length,
            request_id = %rid,
            action = "discarded_wrap",
            "tag placeholder lost during compression — wrap discarded"
        ),
        None => tracing::error!(
            target: "furl::tag_protector",
            event = "tag_protector_placeholder_lost",
            tag_preview = %preview,
            compressed_length = compressed_length,
            action = "discarded_wrap",
            "tag placeholder lost during compression — wrap discarded"
        ),
    }
}

// ─── Tiny byte-search helper ──────────────────────────────────────────

#[inline]
fn memchr(needle: u8, haystack: &[u8]) -> Option<usize> {
    haystack.iter().position(|&b| b == needle)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn protect(text: &str) -> (String, Vec<(String, String)>) {
        let (cleaned, blocks) = protect_tags(text, false);
        (cleaned, blocks)
    }

    #[test]
    fn passthrough_when_no_angle_bracket() {
        let (cleaned, blocks) = protect("Just plain text");
        assert_eq!(cleaned, "Just plain text");
        assert!(blocks.is_empty());
    }

    #[test]
    fn html_tags_emitted_verbatim() {
        let text = "<div>Some content</div>";
        let (cleaned, blocks) = protect(text);
        assert_eq!(cleaned, text);
        assert!(blocks.is_empty());
    }

    #[test]
    fn html_tag_check_case_insensitive() {
        assert!(is_known_html_tag("DIV"));
        assert!(is_known_html_tag("Span"));
        assert!(!is_known_html_tag("system-reminder"));
        assert!(!is_known_html_tag("EXTREMELY_IMPORTANT"));
    }

    #[test]
    fn custom_tag_replaced_with_placeholder() {
        let text = "Before <system-reminder>Important</system-reminder> After";
        let (cleaned, blocks) = protect(text);
        assert!(!cleaned.contains("<system-reminder>"));
        assert!(!cleaned.contains("Important"));
        assert!(cleaned.contains("Before"));
        assert!(cleaned.contains("After"));
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].1, "<system-reminder>Important</system-reminder>");
    }

    #[test]
    fn custom_tag_with_attributes() {
        let text = r#"<context key="session" type="persistent">user data</context>"#;
        let (_cleaned, blocks) = protect(text);
        assert_eq!(blocks.len(), 1);
        assert!(blocks[0].1.contains(r#"key="session""#));
    }

    #[test]
    fn self_closing_custom_tag() {
        let text = "Text <marker/> more text";
        let (_cleaned, blocks) = protect(text);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].1, "<marker/>");
    }

    #[test]
    fn self_closing_html_tag_not_protected() {
        let text = "Text <br/> more <hr/> text";
        let (cleaned, blocks) = protect(text);
        assert_eq!(cleaned, text);
        assert!(blocks.is_empty());
    }

    #[test]
    fn unquoted_slash_attribute_is_not_self_closing() {
        // A `/` is only self-closing when immediately followed by `>`.
        let text = "<citation url=http://x.com>body</citation>";
        let (cleaned, blocks) = protect(text);
        assert_eq!(
            blocks.len(),
            1,
            "unquoted-URL element must protect as one span, got {}: {:?}",
            blocks.len(),
            blocks
        );
        assert_eq!(blocks[0].1, text, "the full element must be captured");
        assert!(
            !cleaned.contains("body"),
            "the body must NOT be exposed (tag was mis-parsed self-closing): {cleaned:?}"
        );
        // Round-trips exactly — no orphan close, no asymmetry.
        let restored = restore_tags(&cleaned, &blocks);
        assert_eq!(restored, text);
    }

    #[test]
    fn nested_custom_tags_collapse_to_outer_span() {
        let text = "<outer><inner>deep</inner></outer>";
        let (cleaned, blocks) = protect(text);
        assert!(!cleaned.contains("<outer>"));
        assert!(!cleaned.contains("<inner>"));
        // Outer span captures inner — single placeholder.
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].1, "<outer><inner>deep</inner></outer>");
    }

    #[test]
    fn mixed_html_and_custom() {
        let text = "<div>HTML</div> <system-reminder>Rule</system-reminder> <p>HTML2</p>";
        let (cleaned, blocks) = protect(text);
        assert!(cleaned.contains("<div>"));
        assert!(cleaned.contains("<p>"));
        assert!(!cleaned.contains("<system-reminder>"));
        assert_eq!(blocks.len(), 1);
    }

    #[test]
    fn real_workflow_tags() {
        let cases = [
            "<tool_call>search({query: 'test'})</tool_call>",
            "<thinking>Let me analyze this</thinking>",
            "<EXTREMELY_IMPORTANT>Never skip validation</EXTREMELY_IMPORTANT>",
            "<user-prompt-submit-hook>check perms</user-prompt-submit-hook>",
            "<system-reminder>Rules</system-reminder>",
            "<result>Success: 42 items</result>",
        ];
        for tag in cases {
            let text = format!("Before {tag} After");
            let (_cleaned, blocks) = protect(&text);
            assert_eq!(blocks.len(), 1, "failed to protect: {tag}");
            assert_eq!(blocks[0].1, tag);
        }
    }

    #[test]
    fn empty_input_returns_empty() {
        let (cleaned, blocks) = protect("");
        assert!(cleaned.is_empty());
        assert!(blocks.is_empty());
    }

    #[test]
    fn compress_tagged_content_true_emits_marker_placeholders() {
        let text = "Before <system-reminder>Compressible content</system-reminder> After";
        let (cleaned, blocks) = protect_tags(text, true);
        assert!(!cleaned.contains("<system-reminder>"));
        assert!(!cleaned.contains("</system-reminder>"));
        assert!(cleaned.contains("Compressible content"));
        assert_eq!(blocks.len(), 2);
    }

    #[test]
    fn marker_mode_nested_tags_protect_both_closes() {
        // A compressor stripping that orphan outer close yields asymmetric tags after restore (the
        // exact failure this module prevents). and NO raw tag may survive in the cleaned output.
        let text = "<outer><inner>x</inner>y</outer>";
        let (cleaned, blocks) = protect_tags(text, true);

        assert_eq!(
            blocks.len(),
            4,
            "expected 4 marker placeholders (both opens + both closes), \
             got {}: {:?}",
            blocks.len(),
            blocks
        );
        // Body text stays inline; every tag marker is replaced.
        assert!(cleaned.contains('x') && cleaned.contains('y'));
        for raw in ["<outer>", "<inner>", "</inner>", "</outer>"] {
            assert!(
                !cleaned.contains(raw),
                "raw tag {raw:?} leaked into cleaned marker-mode output: {cleaned:?}"
            );
        }
        // Restore is symmetric: round-trips back to the exact original.
        let restored = restore_tags(&cleaned, &blocks);
        assert_eq!(restored, text, "nested marker-mode restore must round-trip");
    }

    #[test]
    fn restore_basic() {
        let original = "Before <system-reminder>Rule</system-reminder> After";
        let (cleaned, blocks) = protect_tags(original, false);
        let restored = restore_tags(&cleaned, &blocks);
        assert_eq!(restored, original);
    }

    #[test]
    fn restore_empty_blocks_passthrough() {
        assert_eq!(restore_tags("untouched", &[]), "untouched");
    }

    #[test]
    fn restore_lost_placeholder_discards_wrap() {
        // Hotfix-A9: when a placeholder is missing from the compressed text, the wrap is
        // DISCARDED — the compressed text is returned as-is, with no orphan-tag append.
        let blocks = vec![("{{FURL_TAG_0}}".to_string(), "<tag>data</tag>".to_string())];
        let compressed = "text without placeholder";
        let restored = restore_tags(compressed, &blocks);
        // Compressed text returned unchanged; original tag NOT injected.
        assert_eq!(restored, compressed);
        assert!(!restored.contains("<tag>"));
        assert!(!restored.contains("</tag>"));
        assert!(!restored.contains("<tag>data</tag>"));
    }

    // ─── PERF-15 regression (fixed during the P2-11 restoration) ─────

    #[test]
    fn perf15_duplicate_placeholder_substitutes_first_occurrence_only() {
        // A compressor that DUPLICATES a placeholder must not duplicate the protected tag block on restore.
        let blocks = vec![(
            "{{FURL_TAG_0}}".to_string(),
            "<system-reminder>rule</system-reminder>".to_string(),
        )];
        let compressed = "head {{FURL_TAG_0}} mid {{FURL_TAG_0}} tail";
        let restored = restore_tags(compressed, &blocks);
        assert_eq!(
            restored, "head <system-reminder>rule</system-reminder> mid {{FURL_TAG_0}} tail",
            "first occurrence substitutes; the duplicate stays verbatim"
        );
        assert_eq!(
            restored.matches("<system-reminder>").count(),
            1,
            "the protected block must appear exactly once"
        );
    }

    #[test]
    fn perf15_substituted_originals_are_never_rescanned() {
        // Single left-to-right scan over the COMPRESSED text: a restored block whose body happens to contain
        // a LATER placeholder literal must not have that body corrupted by a second substitution pass.
        let blocks = vec![
            (
                "{{FURL_TAG_0}}".to_string(),
                // Protected content legitimately containing the literal
                // text of the NEXT placeholder (user-authored bytes).
                "<doc>literal {{FURL_TAG_1}} inside</doc>".to_string(),
            ),
            ("{{FURL_TAG_1}}".to_string(), "<b>2</b>".to_string()),
        ];
        let compressed = "a {{FURL_TAG_0}} b {{FURL_TAG_1}} c";
        let restored = restore_tags(compressed, &blocks);
        assert_eq!(
            restored, "a <doc>literal {{FURL_TAG_1}} inside</doc> b <b>2</b> c",
            "the restored body's placeholder-shaped bytes stay verbatim"
        );
    }

    #[test]
    fn perf15_out_of_order_placeholders_restore_correctly() {
        // Compressors may reorder segments. The scan substitutes by pattern identity at each match position, so block order and text order don't need to agree.
        let blocks = vec![
            ("{{FURL_TAG_0}}".to_string(), "<a>first</a>".to_string()),
            ("{{FURL_TAG_1}}".to_string(), "<b>second</b>".to_string()),
        ];
        let compressed = "{{FURL_TAG_1}} then {{FURL_TAG_0}}";
        let restored = restore_tags(compressed, &blocks);
        assert_eq!(restored, "<b>second</b> then <a>first</a>");
    }

    #[test]
    fn perf15_empty_placeholder_blocks_are_ignored() {
        // Defensive: an empty placeholder string would match at every position under a substring
        // scan. Such blocks are skipped (logged) rather than allowed to inject bytes everywhere.
        let blocks = vec![
            (String::new(), "<evil>injected</evil>".to_string()),
            ("{{FURL_TAG_0}}".to_string(), "<a>ok</a>".to_string()),
        ];
        let compressed = "x {{FURL_TAG_0}} y";
        let restored = restore_tags(compressed, &blocks);
        assert_eq!(restored, "x <a>ok</a> y");
        assert!(!restored.contains("<evil>"));
    }

    #[test]
    fn restore_lost_placeholder_idempotent_when_all_missing() {
        // Invariant #3: if every placeholder is missing from the compressed text, the function returns the compressed text byte-for-byte unchanged.
        let blocks = vec![
            ("{{FURL_TAG_0}}".to_string(), "<a>1</a>".to_string()),
            ("{{FURL_TAG_1}}".to_string(), "<b>2</b>".to_string()),
            ("{{FURL_TAG_2}}".to_string(), "<c>3</c>".to_string()),
        ];
        let compressed = "compressor stripped every placeholder";
        let restored = restore_tags(compressed, &blocks);
        assert_eq!(restored, compressed);
    }

    #[test]
    fn restore_partial_loss_keeps_present_drops_lost() {
        // Mixed case: some placeholders survive, others are lost. The surviving ones get substituted;
        // the lost ones are discarded. No orphan-tag bytes appear anywhere in the output.
        let blocks = vec![
            ("{{FURL_TAG_0}}".to_string(), "<a>1</a>".to_string()),
            ("{{FURL_TAG_1}}".to_string(), "<lost>x</lost>".to_string()),
        ];
        let compressed = "head {{FURL_TAG_0}} tail";
        let restored = restore_tags(compressed, &blocks);
        assert_eq!(restored, "head <a>1</a> tail");
        assert!(!restored.contains("<lost"));
        assert!(!restored.contains("</lost>"));
    }

    #[test]
    fn restore_roundtrip_preserves_content() {
        let original = "Start <system-reminder>Rule 1: validate</system-reminder> middle \
             <tool_call>search(q='test')</tool_call> end";
        let (cleaned, blocks) = protect_tags(original, false);
        let restored = restore_tags(&cleaned, &blocks);
        assert_eq!(restored, original);
    }

    // ─── Bug-fix tests (fixed_in_3e4) ─────────────────────────────────

    #[test]
    fn fixed_in_3e4_replace_first_does_not_collide_on_duplicate_blocks() {
        //
        let text = "<system-reminder>same</system-reminder> middle \
             <system-reminder>same</system-reminder>";
        let (cleaned, blocks) = protect_tags(text, false);
        // BOTH blocks should be replaced by DIFFERENT placeholders.
        assert_eq!(blocks.len(), 2);
        assert!(!cleaned.contains("<system-reminder>"));
        assert!(!cleaned.contains("</system-reminder>"));
        assert_ne!(blocks[0].0, blocks[1].0);
        // Roundtrip is exact.
        assert_eq!(restore_tags(&cleaned, &blocks), text);
    }

    #[test]
    fn fixed_in_3e4_handles_50_plus_nested_custom_tags() {
        // Bug #3: Python had a hard-coded 50-iteration safety cap that silently truncated tag protection on deeply nested input.
        let depth = 60;
        let mut text = String::new();
        for _ in 0..depth {
            text.push_str("<lvl>");
        }
        text.push_str("core");
        for _ in 0..depth {
            text.push_str("</lvl>");
        }
        let (cleaned, blocks) = protect_tags(&text, false);
        // The outermost span eats everything: one placeholder, no
        // residual `<lvl>` markers in the cleaned text.
        assert!(!cleaned.contains("<lvl>"));
        assert!(!cleaned.contains("</lvl>"));
        assert_eq!(blocks.len(), 1);
        // Roundtrip exact even at depth=60.
        assert_eq!(restore_tags(&cleaned, &blocks), text);
    }

    #[test]
    fn fixed_in_3e4_self_closing_duplicates_get_distinct_placeholders() {
        // Bug #4: same first-occurrence-replace bug for self-closing
        // tags. `<marker/>` appearing twice would collapse.
        let text = "<marker/> middle <marker/>";
        let (cleaned, blocks) = protect_tags(text, false);
        assert_eq!(blocks.len(), 2);
        assert_ne!(blocks[0].0, blocks[1].0);
        assert!(!cleaned.contains("<marker/>"));
        assert_eq!(restore_tags(&cleaned, &blocks), text);
    }

    #[test]
    fn fixed_in_3e4_placeholder_collision_is_avoided() {
        // Bug #5: input contains literal `{{FURL_TAG_…}}`. The
        // walker should pick a salted prefix.
        let text = "User wrote {{FURL_TAG_0}} on purpose. \
             <system-reminder>real one</system-reminder>";
        let (_cleaned, blocks) = protect_tags(text, false);
        assert_eq!(blocks.len(), 1);
        // Placeholder used must NOT collide with the user's literal.
        assert_ne!(blocks[0].0, "{{FURL_TAG_0}}");
    }

    // ─── Edge-case correctness ────────────────────────────────────────

    #[test]
    fn orphan_close_tag_emitted_verbatim() {
        let text = "no opener </ghost> here";
        let (cleaned, blocks) = protect_tags(text, false);
        // Nothing protected; close stays in the cleaned text.
        assert_eq!(blocks.len(), 0);
        assert!(cleaned.contains("</ghost>"));
    }

    #[test]
    fn orphan_open_tag_emitted_verbatim() {
        // An open with no matching close should round-trip exactly —
        // no protection, no data loss.
        let text = "<ghost>dangling content with no close";
        let (cleaned, blocks) = protect_tags(text, false);
        assert!(blocks.is_empty());
        assert_eq!(cleaned, text);
    }

    #[test]
    fn malformed_lone_lt_emitted_verbatim() {
        let text = "if a < b then c";
        let (cleaned, blocks) = protect_tags(text, false);
        assert_eq!(cleaned, text);
        assert!(blocks.is_empty());
    }

    #[test]
    fn truncated_close_marker_does_not_panic() {
        // Hotfix-A9: proptest seed `</` would index past end-of-input in `parse_tag_at`. Pre-fix this panicked with
        // an OOB; the bounds-check now returns NotTag and the function falls through to emitting `</` verbatim.
        for text in ["</", "<", "<a/", "<a", "<a /", "</a"] {
            let (cleaned, blocks) = protect_tags(text, false);
            assert_eq!(cleaned, text);
            assert!(blocks.is_empty());
        }
    }

    #[test]
    fn attribute_with_gt_inside_quotes() {
        let text = r#"<context attr="a > b">payload</context>"#;
        let (cleaned, blocks) = protect_tags(text, false);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].1, text);
        assert!(!cleaned.contains("payload"));
    }

    #[test]
    fn html_close_inside_custom_block_does_not_pop_stack() {
        // An HTML close tag while a custom open is on top should not confuse the stack: the HTML
        // close is emitted verbatim, the custom span still closes when its own close arrives.
        let text = "<custom>x</div> y</custom>";
        let (cleaned, blocks) = protect_tags(text, false);
        // The whole `<custom>...</custom>` span wins, including the
        // verbatim `</div>` inside.
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].1, "<custom>x</div> y</custom>");
        assert!(!cleaned.contains("<custom>"));
    }

    // ─── Hotfix-A9 invariants ────────────────────────────────────────

    /// Count `<custom>` style opening tags (excludes self-closers and excludes the closing-tag `</…>` form).
    /// Only used by the proptest below — keeps the invariant check independent of the parser under test.
    fn count_open_tags(s: &str) -> usize {
        let bytes = s.as_bytes();
        let mut count = 0_usize;
        let mut i = 0_usize;
        while i < bytes.len() {
            if bytes[i] != b'<' {
                i += 1;
                continue;
            }
            // Skip closing tags `</…>`.
            if i + 1 < bytes.len() && bytes[i + 1] == b'/' {
                i += 1;
                continue;
            }
            // Must be followed by a name-start char to count as a tag.
            if i + 1 >= bytes.len() || !is_name_start(bytes[i + 1]) {
                i += 1;
                continue;
            }
            // Walk to the matching `>`. If we hit `/>` first, this is
            // self-closing and doesn't count as an unbalanced opener.
            let mut j = i + 1;
            let mut self_closing = false;
            while j < bytes.len() && bytes[j] != b'>' {
                if bytes[j] == b'/' {
                    self_closing = true;
                }
                j += 1;
            }
            if j >= bytes.len() {
                // No closing `>` — not a tag.
                break;
            }
            if !self_closing {
                count += 1;
            }
            i = j + 1;
        }
        count
    }

    fn count_close_tags(s: &str) -> usize {
        let bytes = s.as_bytes();
        let mut count = 0_usize;
        let mut i = 0_usize;
        while i < bytes.len() {
            if bytes[i] != b'<' {
                i += 1;
                continue;
            }
            if i + 1 >= bytes.len() || bytes[i + 1] != b'/' {
                i += 1;
                continue;
            }
            // `</name>` — confirm name-start and find the closing `>`.
            if i + 2 >= bytes.len() || !is_name_start(bytes[i + 2]) {
                i += 1;
                continue;
            }
            let mut j = i + 2;
            while j < bytes.len() && bytes[j] != b'>' {
                j += 1;
            }
            if j >= bytes.len() {
                break;
            }
            count += 1;
            i = j + 1;
        }
        count
    }

    proptest::proptest! {
        /// Invariant `restore_tags` never INTRODUCES tag-count asymmetry. restoring on a compressed text with any subset of
        /// placeholders missing must produce the same `opens - closes` skew as the cleaned text after stripping the placeholders.
        #[test]
        fn restore_never_introduces_asymmetry(content in "[a-z<>/]{0,200}") {
            let (cleaned, blocks) = protect_tags(&content, false);
            // Baseline: strip every placeholder from `cleaned`. This is the "lost everything" worst case; the discard-wrap path must produce exactly this output.
            let mut stripped = cleaned.clone();
            for (placeholder, _original) in &blocks {
                stripped = stripped.replace(placeholder.as_str(), "");
            }
            let baseline_skew = count_open_tags(&stripped) as i64
                - count_close_tags(&stripped) as i64;

            // With every placeholder lost, restore_tags must return the compressed text with
            // placeholders dropped — which is exactly `stripped`. So asymmetry equals baseline.
            let restored_all_lost = restore_tags(&stripped, &blocks);
            let lost_skew = count_open_tags(&restored_all_lost) as i64
                - count_close_tags(&restored_all_lost) as i64;
            proptest::prop_assert_eq!(
                lost_skew, baseline_skew,
                "discard-wrap path introduced asymmetry: baseline={}, after_restore={}, restored={:?}",
                baseline_skew, lost_skew, restored_all_lost
            );

            // With every placeholder PRESENT, restore_tags must round- trip exactly to the
            // original `content`, which by construction has the same skew as `content` itself.
            let restored_full = restore_tags(&cleaned, &blocks);
            let full_skew = count_open_tags(&restored_full) as i64
                - count_close_tags(&restored_full) as i64;
            let content_skew = count_open_tags(&content) as i64
                - count_close_tags(&content) as i64;
            proptest::prop_assert_eq!(
                full_skew, content_skew,
                "full-restore path drifted from input skew: input={}, restored={}",
                content_skew, full_skew
            );
        }

        /// Invariant: when every placeholder is stripped before restore, the function returns the compressed
        /// text byte-for-byte unchanged (no orphan-tag injection, no whitespace insertion, no prepends/appends).
        #[test]
        fn restore_idempotent_when_all_placeholders_lost(
            content in "[a-z<>/]{0,200}",
            compressed in "[ -~]{0,200}",
        ) {
            let (_cleaned, blocks) = protect_tags(&content, false);
            // Drop all placeholders by feeding `restore_tags` arbitrary text the compressor "produced".
            let any_placeholder_present = blocks
                .iter()
                .any(|(p, _)| compressed.contains(p.as_str()));
            proptest::prop_assume!(!any_placeholder_present);
            let restored = restore_tags(&compressed, &blocks);
            proptest::prop_assert_eq!(restored, compressed);
        }

        /// Invariant: `restore_tags` never adds bytes that weren't already in `compressed` or part of a substituted placeholder original. Concretely: the restored
        /// length is at most `compressed.len()` plus the sum of lengths of originals that actually got substituted; lost-placeholder originals contribute zero bytes.
        #[test]
        fn restore_no_orphan_byte_injection(
            content in "[a-z<>/]{0,200}",
        ) {
            let (cleaned, blocks) = protect_tags(&content, false);
            let restored = restore_tags(&cleaned, &blocks);
            // Sum of the byte-lengths of the originals that were actually substituted (placeholder still present in `cleaned`). Lost placeholders contribute zero.
            let substituted_original_bytes: usize = blocks
                .iter()
                .filter(|(p, _)| cleaned.contains(p.as_str()))
                .map(|(p, original)| original.len().saturating_sub(p.len()))
                .sum();
            // Upper bound: cleaned.len() + delta from substitution.
            let upper_bound = cleaned.len() + substituted_original_bytes;
            proptest::prop_assert!(
                restored.len() <= upper_bound,
                "restored too long: restored.len={} upper_bound={} cleaned.len={}",
                restored.len(), upper_bound, cleaned.len()
            );
        }
    }
}
