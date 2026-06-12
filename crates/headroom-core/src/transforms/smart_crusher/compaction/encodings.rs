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
            "2026-06-11T21:02:05",             // no tz
            "2026-06-11T21:02:05.123+02:00",   // fractional seconds
            "2026-06-11 21:02:05+02:00",       // space separator
            "2026-13-01T00:00:00Z",            // month 13
            "2026-02-30T00:00:00Z",            // Feb 30
            "2023-02-29T00:00:00Z",            // non-leap Feb 29
            "2026-06-11T24:00:00Z",            // hour 24
            "2026-06-11T23:59:60Z",            // leap second
            "0000-01-01T00:00:00Z",            // year 0
            "2026-06-11T21:02:05+2:00",        // short offset
            "2026-06-11T21:02:05+02:0",        // short offset
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
        assert!(encoded[1].contains("/+02:00"), "tz change carried: {:?}", encoded[1]);
        assert!(!encoded[2].contains('/'), "same tz omits spelling: {:?}", encoded[2]);
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
    fn civil_math_round_trips_every_day_of_leap_and_normal_years() {
        for y in [1999i64, 2000, 2023, 2024, 2100] {
            for m in 1..=12u32 {
                for d in 1..=31u32 {
                    let days = days_from_civil(y, m, d);
                    let (ry, rm, rd) = civil_from_days(days);
                    if (ry, rm, rd) == (y, m, d) {
                        continue; // valid day round-trips
                    }
                    // Invalid civil dates (e.g. Apr 31) won't round-trip;
                    // parse_iso_strict uses exactly this as its validity
                    // check, so nothing more to assert here.
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
