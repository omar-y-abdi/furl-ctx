//! Reversible column-encoding primitives (pure functions).
//!
//! Each encoding here is used twice: the compactor runs the encoder at
//! STAMP TIME to verify an exact round-trip (encode → decode → compare)
//! before marking a column, and the CSV-schema formatter runs the same
//! encoder to render the cells. One implementation, zero drift.
//!
//! # ISO-8601 delta encoding
//!
//! A column whose every value matches the STRICT shape
//! `YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)` (no fractional seconds, years
//! 0001–9999, real calendar dates) encodes as:
//!
//! - row 0: the full ISO string, verbatim;
//! - row i>0: `{±delta_seconds}` vs the previous row, with `/{tz}`
//!   appended ONLY when the timezone spelling changes
//!   (e.g. `+3600`, `-4012/-07:00`).
//!
//! Decoding reconstructs the exact original strings via pure integer
//! civil-calendar math (Howard Hinnant's `days_from_civil` /
//! `civil_from_days`), preserving the timezone spelling (`Z` stays
//! `Z`). Leap seconds (`:60`) and out-of-range fields fail the strict
//! parse, so such columns are never stamped — they stay plain.

/// Strictly parse `YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)`.
///
/// Returns `(epoch_seconds, tz_spelling)` or `None` when the string
/// deviates from the shape in any way (the column then stays plain).
pub fn parse_iso_strict(s: &str) -> Option<(i64, &str)> {
    let b = s.as_bytes();
    if b.len() != 20 && b.len() != 25 {
        return None;
    }
    let digits = |range: std::ops::Range<usize>| -> Option<i64> {
        let mut v: i64 = 0;
        for &c in &b[range] {
            if !c.is_ascii_digit() {
                return None;
            }
            v = v * 10 + (c - b'0') as i64;
        }
        Some(v)
    };
    if b[4] != b'-' || b[7] != b'-' || b[10] != b'T' || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let year = digits(0..4)?;
    let month = digits(5..7)?;
    let day = digits(8..10)?;
    let hour = digits(11..13)?;
    let minute = digits(14..16)?;
    let second = digits(17..19)?;
    if year < 1 || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    // Calendar validity: round-trip through the civil math.
    let days = days_from_civil(year, month as u32, day as u32);
    if civil_from_days(days) != (year, month as u32, day as u32) {
        return None;
    }
    let (tz, offset) = match (b.len(), b[19]) {
        (20, b'Z') => (&s[19..], 0i64),
        (25, b'+') | (25, b'-') => {
            if b[22] != b':' {
                return None;
            }
            let oh = digits(20..22)?;
            let om = digits(23..25)?;
            if oh > 23 || om > 59 {
                return None;
            }
            let sign = if b[19] == b'+' { 1 } else { -1 };
            (&s[19..], sign * (oh * 3600 + om * 60))
        }
        _ => return None,
    };
    let epoch = days * 86400 + hour * 3600 + minute * 60 + second - offset;
    Some((epoch, tz))
}

/// Render `(epoch_seconds, tz_spelling)` back to the exact strict-shape
/// ISO string. `None` when the local civil date falls outside years
/// 0001–9999 (cannot round-trip the 4-digit year) or the tz spelling is
/// not one `parse_iso_strict` produced.
pub fn render_iso(epoch: i64, tz: &str) -> Option<String> {
    let offset = tz_offset_seconds(tz)?;
    let local = epoch.checked_add(offset)?;
    let days = local.div_euclid(86400);
    let sod = local.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    if !(1..=9999).contains(&y) {
        return None;
    }
    Some(format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}{tz}",
        sod / 3600,
        (sod % 3600) / 60,
        sod % 60
    ))
}

fn tz_offset_seconds(tz: &str) -> Option<i64> {
    if tz == "Z" {
        return Some(0);
    }
    let b = tz.as_bytes();
    if b.len() != 6 || (b[0] != b'+' && b[0] != b'-') || b[3] != b':' {
        return None;
    }
    let num = |i: usize| -> Option<i64> {
        let (a, c) = (b[i], b[i + 1]);
        if !a.is_ascii_digit() || !c.is_ascii_digit() {
            return None;
        }
        Some(((a - b'0') * 10 + (c - b'0')) as i64)
    };
    let (oh, om) = (num(1)?, num(4)?);
    if oh > 23 || om > 59 {
        return None;
    }
    let sign = if b[0] == b'+' { 1 } else { -1 };
    Some(sign * (oh * 3600 + om * 60))
}

/// Days from civil date (proleptic Gregorian), Hinnant's algorithm.
/// Valid for years >= 1 (callers enforce), where all divisions operate
/// on non-negative values — identical semantics in Rust and Python.
fn days_from_civil(mut y: i64, m: u32, d: u32) -> i64 {
    if m <= 2 {
        y -= 1;
    }
    let era = y / 400; // y >= 0 for years >= 1
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 } as i64;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719_468
}

/// Civil date from days (inverse of [`days_from_civil`]).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Streaming encoder state for one ISO-delta column. The SAME state
/// drives the stamping simulation and the formatter rendering.
#[derive(Debug, Default)]
pub struct IsoDeltaState {
    prev: Option<(i64, String)>,
}

impl IsoDeltaState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Render the next cell for `value`. First (and any unparseable)
    /// value renders verbatim and (re)seeds the state; subsequent
    /// values render `{±delta}[/tz]`.
    pub fn next_cell(&mut self, value: &str) -> String {
        let parsed = parse_iso_strict(value);
        match (parsed, &self.prev) {
            (Some((epoch, tz)), Some((prev_epoch, prev_tz))) => {
                let delta = epoch - prev_epoch;
                let cell = if tz == prev_tz {
                    format!("{delta:+}")
                } else {
                    format!("{delta:+}/{tz}")
                };
                self.prev = Some((epoch, tz.to_string()));
                cell
            }
            (Some((epoch, tz)), None) => {
                self.prev = Some((epoch, tz.to_string()));
                value.to_string()
            }
            (None, _) => {
                // Not strict-shape (only possible when rendering rows
                // the stamping never saw): emit verbatim, reset state —
                // the decoder resets on any full ISO/non-delta cell.
                self.prev = None;
                value.to_string()
            }
        }
    }
}

/// Encode a whole column. `None` when any value fails the strict parse
/// — the column must stay plain.
pub fn encode_iso_column(values: &[&str]) -> Option<Vec<String>> {
    if values.iter().any(|v| parse_iso_strict(v).is_none()) {
        return None;
    }
    let mut state = IsoDeltaState::new();
    Some(values.iter().map(|v| state.next_cell(v)).collect())
}

/// Decode a whole encoded column back to the original strings. `None`
/// on any cell that cannot be decoded. Used at stamp time to PROVE the
/// round-trip before the encoding ships.
pub fn decode_iso_column(cells: &[String]) -> Option<Vec<String>> {
    let mut out: Vec<String> = Vec::with_capacity(cells.len());
    let mut prev: Option<(i64, String)> = None;
    for cell in cells {
        if let Some((delta, tz_part)) = parse_delta_cell(cell) {
            let (prev_epoch, prev_tz) = prev.as_ref()?;
            let epoch = prev_epoch.checked_add(delta)?;
            let tz = tz_part.unwrap_or(prev_tz).to_string();
            let rendered = render_iso(epoch, &tz)?;
            out.push(rendered);
            prev = Some((epoch, tz));
        } else {
            let (epoch, tz) = parse_iso_strict(cell)?;
            out.push(cell.clone());
            prev = Some((epoch, tz.to_string()));
        }
    }
    Some(out)
}

/// Parse a `{±delta}[/tz]` cell. `None` when the cell is not a delta
/// (e.g. a full ISO string).
fn parse_delta_cell(cell: &str) -> Option<(i64, Option<&str>)> {
    let (num, tz) = match cell.find('/') {
        Some(i) => (&cell[..i], Some(&cell[i + 1..])),
        None => (cell, None),
    };
    let first = *num.as_bytes().first()?;
    if first != b'+' && first != b'-' {
        return None;
    }
    if num.len() < 2 || !num[1..].bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let delta: i64 = num.parse().ok()?;
    if let Some(tz) = tz {
        tz_offset_seconds(tz)?;
    }
    Some((delta, tz))
}

// ─────────────────────── decimal scale-fold ───────────────────────
//
// A float column whose every value renders as a plain decimal
// (`-?\d+\.\d{1,6}`, no exponent) encodes as the integer value × 10^k
// (k = the column's max fractional digits), e.g. `0.053` → `53` at
// k=3. Encoding and decoding are PURE STRING MANIPULATION — no float
// arithmetic anywhere — so exactness is structural: the digits move,
// nothing is computed. The compactor still proves the round-trip at
// stamp time by re-parsing and re-rendering each decoded value.

/// Fractional digit count of a plain-decimal rendering. `None` for
/// anything else (exponent form, integers, NaN spellings, ...).
pub fn decimal_frac_digits(rendered: &str) -> Option<usize> {
    let rest = rendered.strip_prefix('-').unwrap_or(rendered);
    let (int_part, frac_part) = rest.split_once('.')?;
    if int_part.is_empty() || frac_part.is_empty() {
        return None;
    }
    if !int_part.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if !frac_part.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if frac_part.len() > 6 {
        return None; // cap: beyond this the cells barely shrink
    }
    Some(frac_part.len())
}

/// `0.053` at k=3 → `53`; `-0.5` at k=1 → `-5`; `12.5` at k=3 → `12500`.
pub fn encode_decimal_cell(rendered: &str, k: usize) -> Option<String> {
    let frac_len = decimal_frac_digits(rendered)?;
    if frac_len > k {
        return None;
    }
    let (sign, rest) = match rendered.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", rendered),
    };
    let (int_part, frac_part) = rest.split_once('.')?;
    let mut digits = String::with_capacity(int_part.len() + k);
    digits.push_str(int_part);
    digits.push_str(frac_part);
    for _ in frac_len..k {
        digits.push('0');
    }
    let trimmed = digits.trim_start_matches('0');
    let body = if trimmed.is_empty() { "0" } else { trimmed };
    Some(format!("{sign}{body}"))
}

/// Inverse of [`encode_decimal_cell`]: digits back to a decimal string
/// (`90` at k=3 → `0.090` — parses to the same f64 as the original
/// shortest rendering `0.09`).
pub fn decode_decimal_cell(cell: &str, k: usize) -> Option<String> {
    let (sign, digits) = match cell.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", cell),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let padded = if digits.len() <= k {
        format!("{digits:0>width$}", width = k + 1)
    } else {
        digits.to_string()
    };
    let split = padded.len() - k;
    Some(format!("{sign}{}.{}", &padded[..split], &padded[split..]))
}

// ─────────────────────── cross-row affix fold ───────────────────────
//
// A string column whose every cell shares a common BYTE prefix and/or
// suffix (the structure that repeats even when the middle is unique:
// shared path roots like `crates/furl-core/src/`, URL roots like
// `https://api.github.com/repos/owner/proj/`, file extensions `.rs`,
// fixed key/template heads). The affix is declared ONCE and each row
// carries only its unique middle. Reconstruction is pure byte
// concatenation — `prefix + middle + suffix` — so exactness is
// structural: nothing is computed, the bytes are split and rejoined.
//
// The prefix/suffix are computed on UTF-8 BYTE boundaries that are also
// char boundaries (see `common_affix`), so the middle is always valid
// UTF-8 and the column never splits a multibyte codepoint.

/// Longest common prefix of `values`, truncated to a char boundary of
/// the first value. Empty when the values share no leading byte.
fn common_prefix_len(values: &[&str]) -> usize {
    let Some(first) = values.first() else {
        return 0;
    };
    let fb = first.as_bytes();
    let mut n = fb.len();
    for v in &values[1..] {
        let vb = v.as_bytes();
        let mut i = 0;
        while i < n && i < vb.len() && fb[i] == vb[i] {
            i += 1;
        }
        n = i;
        if n == 0 {
            break;
        }
    }
    // Retreat to a char boundary of `first` so the split is valid UTF-8.
    while n > 0 && !first.is_char_boundary(n) {
        n -= 1;
    }
    n
}

/// Longest common suffix LENGTH (in bytes) of `values`, truncated to a
/// char boundary of the first value. Empty when no trailing byte is
/// shared.
fn common_suffix_len(values: &[&str]) -> usize {
    let Some(first) = values.first() else {
        return 0;
    };
    let fb = first.as_bytes();
    let mut n = fb.len();
    for v in &values[1..] {
        let vb = v.as_bytes();
        let mut i = 0;
        while i < n && i < vb.len() && fb[fb.len() - 1 - i] == vb[vb.len() - 1 - i] {
            i += 1;
        }
        n = i;
        if n == 0 {
            break;
        }
    }
    // Retreat so the suffix STARTS on a char boundary of `first`.
    while n > 0 && !first.is_char_boundary(first.len() - n) {
        n -= 1;
    }
    n
}

/// Compute the `(prefix, suffix)` an affix-fold would declare for a
/// column. Returns the two shared byte-strings (either may be empty);
/// the prefix and suffix never overlap (when the whole column is one
/// constant, the prefix takes it and the suffix is empty).
///
/// Both are guaranteed to be valid UTF-8 substrings of every value and
/// to split each value cleanly: `value == prefix + middle + suffix`.
pub fn common_affix<'a>(values: &[&'a str]) -> (&'a str, &'a str) {
    let Some(first) = values.first() else {
        return ("", "");
    };
    let plen = common_prefix_len(values);
    // The suffix must not eat into the prefix: cap the suffix length so
    // prefix and suffix together never exceed the SHORTEST value's len.
    let min_len = values.iter().map(|v| v.len()).min().unwrap_or(0);
    let raw_slen = common_suffix_len(values);
    let mut slen = raw_slen.min(min_len.saturating_sub(plen));
    // Retreat the (possibly shortened) suffix back to a char boundary.
    while slen > 0 && !first.is_char_boundary(first.len() - slen) {
        slen -= 1;
    }
    (&first[..plen], &first[first.len() - slen..])
}

/// Strip a fixed `prefix`/`suffix` from `value`, returning the unique
/// middle. `None` when `value` does not actually carry both affixes
/// (only possible when rendering a row the stamping never saw).
pub fn encode_affix_cell<'a>(value: &'a str, prefix: &str, suffix: &str) -> Option<&'a str> {
    let mid = value.strip_prefix(prefix)?;
    let mid = mid.strip_suffix(suffix)?;
    Some(mid)
}

/// Reassemble `prefix + middle + suffix`. Total by construction.
pub fn decode_affix_cell(middle: &str, prefix: &str, suffix: &str) -> String {
    let mut s = String::with_capacity(prefix.len() + middle.len() + suffix.len());
    s.push_str(prefix);
    s.push_str(middle);
    s.push_str(suffix);
    s
}

// ─────────────────────── head dictionary fold ───────────────────────
//
// A string column whose values split at the LAST occurrence of a
// delimiter (`/`, `:`, or `.`) into a low-cardinality HEAD (everything
// up to and including the delimiter) and a unique TAIL. The distinct
// heads are declared once on a `__head:name=<DELIM><h0>,<h1>,...` line;
// each row cell renders as `<head_index><DELIM><tail>`. Reconstruction:
// `head[index] + tail` (the head already carries the trailing delimiter,
// so the in-cell delimiter after the index is dropped). Exact by
// construction — the head is a verbatim byte string, the tail is verbatim
// bytes, concatenation is lossless.
//
// This catches the structure cross-row affix folding cannot: paths/keys
// that fall into a FEW directory/namespace groups whose per-group prefix
// is NOT shared by the WHOLE column (so the single common affix is short)
// but repeats across many rows.

/// The delimiters head-dict considers, in priority order. A column is
/// split at the LAST occurrence of the FIRST delimiter that yields a
/// low-cardinality, byte-saving head set.
pub const HEAD_DELIMS: [char; 3] = ['/', ':', '.'];

/// Split `value` at the last occurrence of `delim` into
/// `(head_including_delim, tail)`. `None` when `delim` is absent.
pub fn split_head(value: &str, delim: char) -> Option<(&str, &str)> {
    let idx = value.rfind(delim)?;
    let split = idx + delim.len_utf8();
    Some((&value[..split], &value[split..]))
}

/// Render a head-dict cell: `<head_index><delim><tail>`. The leading
/// integer run is the head index; the single `delim` byte after it
/// separates index from tail and is dropped on decode (the head already
/// ends with `delim`).
pub fn encode_head_cell(head_index: usize, delim: char, tail: &str) -> String {
    let mut s = String::with_capacity(8 + tail.len());
    s.push_str(&head_index.to_string());
    s.push(delim);
    s.push_str(tail);
    s
}

/// Decode a head-dict cell back to `(head_index, tail)`. Reads the
/// maximal leading ASCII-digit run as the index, requires the next byte
/// to be exactly `delim`, and takes the rest as the tail. `None` on any
/// deviation (no digits, missing delimiter) — never invents data.
pub fn decode_head_cell(cell: &str, delim: char) -> Option<(usize, &str)> {
    let bytes = cell.as_bytes();
    let mut k = 0;
    while k < bytes.len() && bytes[k].is_ascii_digit() {
        k += 1;
    }
    if k == 0 {
        return None; // no index
    }
    let idx: usize = cell[..k].parse().ok()?;
    let rest = &cell[k..];
    let tail = rest.strip_prefix(delim)?;
    Some((idx, tail))
}

/// Reassemble a head-dict value: `head + tail` (head already carries its
/// trailing delimiter). Total by construction.
pub fn decode_head_value(head: &str, tail: &str) -> String {
    let mut s = String::with_capacity(head.len() + tail.len());
    s.push_str(head);
    s.push_str(tail);
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_and_render_round_trip_offsets_and_z() {
        for s in [
            "2026-06-11T21:02:05-07:00",
            "2026-06-12T15:01:32+02:00",
            "1970-01-01T00:00:00Z",
            "0001-01-01T00:00:00+00:00",
            "9999-12-31T23:59:59-11:30",
            "2024-02-29T12:00:00Z", // real leap day
        ] {
            let (epoch, tz) = parse_iso_strict(s).unwrap_or_else(|| panic!("parse {s}"));
            assert_eq!(render_iso(epoch, tz).as_deref(), Some(s), "render {s}");
        }
    }

    #[test]
    fn strict_parse_rejects_deviations() {
        for s in [
            "2026-06-11T21:02:05",           // no tz
            "2026-06-11T21:02:05.123+02:00", // fractional seconds
            "2026-06-11 21:02:05+02:00",     // space separator
            "2026-13-01T00:00:00Z",          // month 13
            "2026-02-30T00:00:00Z",          // Feb 30
            "2023-02-29T00:00:00Z",          // non-leap Feb 29
            "2026-06-11T24:00:00Z",          // hour 24
            "2026-06-11T23:59:60Z",          // leap second
            "0000-01-01T00:00:00Z",          // year 0
            "2026-06-11T21:02:05+2:00",      // short offset
            "2026-06-11T21:02:05+02:0",      // short offset
        ] {
            assert!(parse_iso_strict(s).is_none(), "must reject {s}");
        }
    }

    #[test]
    fn epoch_matches_known_values() {
        let (epoch, _) = parse_iso_strict("1970-01-01T00:00:00Z").unwrap();
        assert_eq!(epoch, 0);
        let (epoch, _) = parse_iso_strict("1970-01-02T00:00:00Z").unwrap();
        assert_eq!(epoch, 86400);
        // Offset normalizes: 02:00 east of UTC is BEFORE the same wall
        // clock at UTC.
        let (epoch, _) = parse_iso_strict("1970-01-01T02:00:00+02:00").unwrap();
        assert_eq!(epoch, 0);
    }

    #[test]
    fn column_encode_decode_round_trips_with_tz_changes() {
        let values = [
            "2026-06-11T21:02:05-07:00",
            "2026-06-11T19:55:13+02:00",
            "2026-06-11T18:55:19+02:00",
            "2026-06-10T19:11:46-07:00",
            "2026-06-10T19:11:46-07:00", // identical consecutive (delta +0)
            "2026-06-12T00:00:00Z",
        ];
        let encoded = encode_iso_column(&values).expect("encode");
        assert_eq!(encoded[0], values[0], "first cell verbatim");
        assert!(encoded[1].starts_with('+') || encoded[1].starts_with('-'));
        assert!(
            encoded[1].contains("/+02:00"),
            "tz change carried: {:?}",
            encoded[1]
        );
        assert!(
            !encoded[2].contains('/'),
            "same tz omits spelling: {:?}",
            encoded[2]
        );
        assert_eq!(encoded[4], "+0", "identical consecutive is +0");
        let decoded = decode_iso_column(&encoded).expect("decode");
        assert_eq!(decoded, values, "exact reconstruction");
    }

    #[test]
    fn column_with_any_nonconforming_value_refuses() {
        let values = ["2026-06-11T21:02:05Z", "not a date"];
        assert!(encode_iso_column(&values).is_none());
    }

    #[test]
    fn decimal_scale_encode_decode_pure_string_round_trip() {
        for (rendered, k, expect_enc) in [
            ("0.053", 3, "53"),
            ("0.09", 3, "90"),
            ("12.5", 3, "12500"),
            ("-0.5", 1, "-5"),
            ("0.0", 1, "0"),
            ("100.116", 3, "100116"),
        ] {
            let enc = encode_decimal_cell(rendered, k).expect(rendered);
            assert_eq!(enc, expect_enc, "encode {rendered}");
            let dec = decode_decimal_cell(&enc, k).expect(rendered);
            // The decoded decimal string parses to the SAME f64 as the
            // original rendering (the value is what must round-trip).
            let orig: f64 = rendered.parse().unwrap();
            let back: f64 = dec.parse().unwrap();
            assert_eq!(orig, back, "value round-trip {rendered} -> {enc} -> {dec}");
        }
    }

    #[test]
    fn decimal_scale_refuses_non_plain_renderings() {
        for s in ["1e10", "5", "NaN", "0.1234567", "-", ".5", "5."] {
            assert!(decimal_frac_digits(s).is_none(), "must refuse {s}");
        }
        assert!(decode_decimal_cell("12a", 3).is_none());
        assert!(decode_decimal_cell("", 3).is_none());
    }

    #[test]
    fn affix_prefix_and_suffix_shared_across_rows() {
        let values = [
            "crates/core/src/aa.rs",
            "crates/core/src/bb.rs",
            "crates/core/src/cccc.rs",
        ];
        let (p, s) = common_affix(&values);
        assert_eq!(p, "crates/core/src/");
        assert_eq!(s, ".rs");
        for v in &values {
            let mid = encode_affix_cell(v, p, s).expect("middle");
            assert_eq!(decode_affix_cell(mid, p, s), *v, "round-trip {v}");
        }
    }

    #[test]
    fn affix_prefix_only_when_no_shared_suffix() {
        let values = ["https://x/a", "https://x/bcd", "https://x/ef"];
        let (p, s) = common_affix(&values);
        assert_eq!(p, "https://x/");
        assert_eq!(s, "");
    }

    #[test]
    fn affix_empty_when_nothing_shared() {
        let values = ["abc", "xyz", "qrs"];
        let (p, s) = common_affix(&values);
        assert_eq!(p, "");
        assert_eq!(s, "");
    }

    #[test]
    fn affix_does_not_overlap_when_one_value_is_a_prefix_of_others() {
        // "ab" is fully consumed by the prefix; the suffix must not also
        // claim bytes from it (prefix+suffix <= shortest value length).
        let values = ["ab", "abcab", "abxab"];
        let (p, s) = common_affix(&values);
        assert!(p.len() + s.len() <= 2, "no overlap: p={p:?} s={s:?}");
        for v in &values {
            let mid = encode_affix_cell(v, p, s).expect("middle");
            assert_eq!(decode_affix_cell(mid, p, s), *v);
        }
    }

    #[test]
    fn affix_respects_utf8_char_boundaries() {
        // Shared multibyte head: "héllo-" (é is 2 bytes). The prefix must
        // not split the é, and the middle stays valid UTF-8.
        let values = ["héllo-1", "héllo-22", "héllo-333"];
        let (p, s) = common_affix(&values);
        assert!(std::str::from_utf8(p.as_bytes()).is_ok());
        for v in &values {
            let mid = encode_affix_cell(v, p, s).expect("middle");
            assert_eq!(decode_affix_cell(mid, p, s), *v);
        }
    }

    #[test]
    fn affix_identical_values_take_whole_as_prefix() {
        let values = ["same", "same", "same"];
        let (p, s) = common_affix(&values);
        assert_eq!(p, "same");
        assert_eq!(s, "");
        for v in &values {
            assert_eq!(encode_affix_cell(v, p, s), Some(""));
        }
    }

    #[test]
    fn head_split_and_cell_round_trip() {
        let v = "src/cache/store/foo.rs";
        let (head, tail) = split_head(v, '/').expect("split");
        assert_eq!(head, "src/cache/store/");
        assert_eq!(tail, "foo.rs");
        let cell = encode_head_cell(3, '/', tail);
        assert_eq!(cell, "3/foo.rs");
        let (idx, dtail) = decode_head_cell(&cell, '/').expect("decode");
        assert_eq!(idx, 3);
        assert_eq!(dtail, "foo.rs");
        assert_eq!(decode_head_value(head, dtail), v);
    }

    #[test]
    fn head_cell_index_is_maximal_digit_run() {
        // idx 12, tail begins with a digit — the delimiter ends the run.
        let cell = encode_head_cell(12, '/', "3abc");
        assert_eq!(cell, "12/3abc");
        let (idx, tail) = decode_head_cell(&cell, '/').expect("decode");
        assert_eq!((idx, tail), (12, "3abc"));
    }

    #[test]
    fn head_decode_rejects_malformed() {
        assert!(decode_head_cell("abc", '/').is_none()); // no index
        assert!(decode_head_cell("5x", '/').is_none()); // wrong separator
        assert!(decode_head_cell("/foo", '/').is_none()); // no leading digit
    }

    #[test]
    fn head_split_absent_delim_is_none() {
        assert!(split_head("nodelimiter", '/').is_none());
    }

    #[test]
    fn head_split_colon_and_dot_delims() {
        let (h, t) = split_head("app:prod:session:42", ':').expect("colon");
        assert_eq!(h, "app:prod:session:");
        assert_eq!(t, "42");
        let (h, t) = split_head("com.example.module.Name", '.').expect("dot");
        assert_eq!(h, "com.example.module.");
        assert_eq!(t, "Name");
    }

    /// Independent days-in-month table (proleptic Gregorian) — the oracle
    /// the loop below checks the Hinnant round-trip against. Kept separate
    /// from the production code on purpose: sharing its leap logic would
    /// make the test tautological.
    fn days_in_month(y: i64, m: u32) -> u32 {
        let leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
        match m {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 if leap => 29,
            2 => 28,
            _ => unreachable!("month out of range"),
        }
    }

    #[test]
    fn civil_math_round_trips_every_day_of_leap_and_normal_years() {
        // TEST-17: the old loop asserted NOTHING per iteration (valid days
        // `continue`d, invalid days fell through comment-only) — only the 4
        // spot anchors could fail. Now every (y, m, d) is checked against an
        // independent calendar oracle: valid dates MUST round-trip, invalid
        // ones MUST NOT (parse_iso_strict uses exactly that non-round-trip
        // as its validity check, so both directions are load-bearing).
        // Years cover leap (2000, 2024), non-leap (1999, 2023) and the
        // century non-leap 2100.
        for y in [1999i64, 2000, 2023, 2024, 2100] {
            for m in 1..=12u32 {
                for d in 1..=31u32 {
                    let days = days_from_civil(y, m, d);
                    let round_tripped = civil_from_days(days) == (y, m, d);
                    let valid = d <= days_in_month(y, m);
                    assert_eq!(
                        round_tripped, valid,
                        "{y:04}-{m:02}-{d:02}: round_trip={round_tripped} but \
                         calendar-valid={valid}"
                    );
                }
            }
        }
        // Spot-check fixed anchors.
        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(days_from_civil(2000, 3, 1), 11017);
        assert_eq!(civil_from_days(11017), (2000, 3, 1));
    }
}
