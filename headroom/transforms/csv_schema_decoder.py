"""Reference decoder for the CSV-schema lossless rendering.

This is the documented CONSUMER CONTRACT for the ``CsvSchemaFormatter``
output (``crates/headroom-core/src/transforms/smart_crusher/compaction/
formatter.rs``): a consumer holding ONLY the rendered text reconstructs
every original row exactly. "Lossless" in this engine means *exact
reconstruction through this decoder*, not verbatim string presence.

Grammar decoded here (one table)::

    [N]{col:type[?][=CONST],...}     declaration line
    <row lines>                       one CSV-escaped line per row

Encodings understood:

* **Constant-column fold** — ``name:type=value`` declares the constant
  once; the column is omitted from rows and re-attached on decode. A
  ``string``-tagged constant is never type-coerced.
* **Ditto marks** — a bare ``=`` cell carries forward the SAME column's
  previous *rendered* cell (a literal ``=`` data cell is CSV-quoted by
  the formatter, so the bare marker is unambiguous).
* **Arithmetic fold** — ``name:int=BASE+STEP`` declares an exact
  integer progression; the column is omitted from rows and row ``i``
  decodes to ``BASE + STEP*i``. Unambiguous against a constant: an int
  constant renders as a bare integer, never two integers joined by
  ``+``.
* **ISO-delta** — ``name:string~`` marks a column of strict-shape
  ISO-8601 timestamps (``YYYY-MM-DDTHH:MM:SS(Z|±HH:MM)``). The first
  materialized cell is the full timestamp verbatim; each later cell is
  ``{±delta_seconds}[/tz]`` against the previous row (timezone spelling
  only when it changes). Reconstruction uses pure integer civil-calendar
  math and preserves the exact original spelling (``Z`` stays ``Z``).
* **Dictionary columns** — a ``__dict:name=v0,v1,...`` line directly
  after the declaration lists a low-cardinality string column's distinct
  values (verbatim, CSV-escaped, first-appearance order); row cells in
  that column are dictionary indexes. A plain data cell starting with
  ``__dict:`` is CSV-quoted by the formatter, so the preamble lines are
  unambiguous.
* **Decimal scale-fold** — ``name:float%k`` marks a float column whose
  cells are the integer value × 10^k (``53`` at k=3 decodes to
  ``0.053``). Decoding is pure string manipulation followed by a float
  parse — no float arithmetic — so the reconstructed value is exact.
* **Affix fold** — ``name:string^`` marks a string column whose values
  share a common byte prefix and/or suffix. A ``__affix:name=PREFIX,SUFFIX``
  preamble line (both CSV-escaped) declares the shared affix once; each
  row cell carries only its unique middle, reconstructed as
  ``prefix + middle + suffix`` (pure byte concatenation — exact). A plain
  data cell starting with ``__affix:`` is CSV-quoted by the formatter, so
  the preamble lines are unambiguous.
* **Head-dict fold** — ``name:string@`` marks a string column whose
  values split at the last delimiter (``/`` ``:`` ``.``) into a
  low-cardinality HEAD and a unique TAIL. A ``__head:name=<DELIM><h0>,...``
  preamble line declares the delimiter (first char) and the distinct heads
  (CSV-escaped, first-appearance order, each carrying its trailing
  delimiter); each row cell is ``<head_index><delim><tail>``, reconstructed
  as ``head[index] + tail``. A plain data cell starting with ``__head:`` is
  CSV-quoted by the formatter, so the preamble lines are unambiguous.

Lines that do not parse as rows (e.g. the lossy-survivor
``{"_ccr_dropped": ...}`` sentinel line) are skipped; callers treat
rows the decoder cannot reconstruct as NOT recovered — the decoder
never invents data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_HEADER_RE = re.compile(r"^\[(\d+)\]\{(.+)\}$")
_ARITH_RE = re.compile(r"^(-?\d+)\+(-?\d+)$")
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})$"
)
_DELTA_RE = re.compile(r"^([+-]\d+)(?:/(Z|[+-]\d{2}:\d{2}))?$")
_CCR_SENTINEL_KEY = "_ccr_dropped"


def _days_from_civil(y: int, m: int, d: int) -> int:
    """Days since 1970-01-01 (proleptic Gregorian, Hinnant's algorithm).

    Valid for years >= 1, where every division operates on non-negative
    values — identical semantics to the Rust encoder.
    """
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z: int) -> tuple[int, int, int]:
    """Inverse of :func:`_days_from_civil`."""
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y, m, d)


def _tz_offset_seconds(tz: str) -> int:
    return 0 if tz == "Z" else (1 if tz[0] == "+" else -1) * (
        int(tz[1:3]) * 3600 + int(tz[4:6]) * 60
    )


def _parse_iso(s: str) -> tuple[int, str] | None:
    """Strict ISO-8601 parse -> ``(epoch_seconds, tz_spelling)``."""
    m = _ISO_RE.match(s)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(m.group(i)) for i in range(1, 7))
    tz = m.group(7)
    if y < 1 or not 1 <= mo <= 12 or not 1 <= d <= 31:
        return None
    if h > 23 or mi > 59 or sec > 59:
        return None
    days = _days_from_civil(y, mo, d)
    if _civil_from_days(days) != (y, mo, d):
        return None  # invalid calendar date (e.g. Feb 30)
    epoch = days * 86400 + h * 3600 + mi * 60 + sec - _tz_offset_seconds(tz)
    return epoch, tz


def _render_iso(epoch: int, tz: str) -> str | None:
    """Render ``(epoch, tz_spelling)`` back to the exact ISO string."""
    local = epoch + _tz_offset_seconds(tz)
    days, sod = divmod(local, 86400)
    y, m, d = _civil_from_days(days)
    if not 1 <= y <= 9999:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}T{sod // 3600:02d}:{(sod % 3600) // 60:02d}:{sod % 60:02d}{tz}"


@dataclass(frozen=True)
class ColumnSpec:
    """One decoded column declaration from the ``[N]{...}`` header."""

    name: str
    type_tag: str  # "int" / "float" / "string" / "bool" / "json" / ...
    nullable: bool
    # (has_const, const_value) — a plain Optional can't represent a
    # legitimate `None` (JSON null) constant, so totality needs the flag.
    has_const: bool
    const_value: Any
    # (base, step) of an arithmetic fold; None when not arith-encoded.
    arith: tuple[int, int] | None = None
    # True when the column is ISO-delta encoded (``name:string~``).
    iso_delta: bool = False
    # Fractional-digit count of a decimal scale-fold (``name:float%k``);
    # None when not scale-encoded.
    dec_scale: int | None = None
    # ``(prefix, suffix)`` of a cross-row affix fold (``name:string^``);
    # None when not affix-encoded. The shared affix lives on the
    # ``__affix:name=PREFIX,SUFFIX`` preamble line; row cells carry only
    # the unique middle, reconstructed as ``prefix + middle + suffix``.
    affix: tuple[str, str] | None = None
    # True when the column is head-dict encoded (``name:string@``). The
    # delimiter + distinct heads live on the ``__head:name=...`` preamble
    # line; row cells are ``<head_index><delim><tail>``, reconstructed as
    # ``head[index] + tail``.
    head_dict: bool = False


def _split_logical_lines(text: str) -> list[str]:
    """Split *text* into logical CSV lines respecting RFC-4180 quoting.

    A ``'\\n'`` character is treated as a line break ONLY when not inside a
    double-quoted field.  Inside a quoted field (between an opening ``"``
    and its closing ``"``) newlines are part of the current logical line.

    RFC-4180 doubled-quote escaping (``""`` inside a quoted field) is two
    quote-character toggles in sequence — the first closes the current
    in-quotes state and the second reopens it, producing a net no-op for
    the ``in_quotes`` flag.  This is the correct behaviour: ``""`` does NOT
    represent a literal ``"`` at the split level (that is ``_unquote_csv``'s
    concern), and the flag ends up in the same state it started.

    The result is byte-identical to ``text.split('\\n')`` for any input that
    contains no double-quote characters, so existing caller behaviour is
    fully preserved.

    Examples::

        >>> _split_logical_lines('a\\nb\\n')
        ['a', 'b', '']
        >>> _split_logical_lines('"a\\nb",c')
        ['"a\\nb",c']
        >>> _split_logical_lines('')
        ['']
    """
    lines: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "\n" and not in_quotes:
            lines.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    lines.append("".join(buf))
    return lines


def split_unquoted(s: str) -> list[str]:
    """Split on commas OUTSIDE CSV double-quoted segments."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in s:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _unquote_csv(raw: str) -> str:
    """Strip CSV quotes and unescape doubled quotes."""
    return raw[1:-1].replace('""', '"')


def _decode_cell(raw: str, type_tag: str) -> Any:
    """One rendered cell back to a JSON value.

    CSV-quoted cells are ALWAYS strings (the formatter only quotes
    string renderings). For unquoted cells the declared type tag
    disambiguates: a ``string`` column's cell stays a string even when
    it happens to look numeric; other tags go through ``json.loads``
    with a raw-string fallback.
    """
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return _unquote_csv(raw)
    base_tag = type_tag.rstrip("?")
    if base_tag == "string" and raw != "":
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _parse_header_segment(seg: str) -> ColumnSpec | None:
    """Parse one ``name:type[?][=CONST]`` declaration segment."""
    if ":" not in seg:
        return None
    name, decl = seg.split(":", 1)
    if "=" in decl:
        type_tag, raw = decl.split("=", 1)
        if type_tag.rstrip("?") == "int":
            arith = _ARITH_RE.match(raw)
            if arith:
                return ColumnSpec(
                    name=name,
                    type_tag="int",
                    nullable=False,
                    has_const=False,
                    const_value=None,
                    arith=(int(arith.group(1)), int(arith.group(2))),
                )
        if type_tag.rstrip("?") == "string":
            # String-tagged constant: never coerce (a numeric-looking
            # constant like "123" stays a string).
            value: Any = (
                _unquote_csv(raw)
                if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2
                else raw
            )
        else:
            value = _decode_cell(raw, type_tag)
        return ColumnSpec(
            name=name,
            type_tag=type_tag.rstrip("?"),
            nullable=type_tag.endswith("?"),
            has_const=True,
            const_value=value,
        )
    if decl.endswith("~"):
        bare = decl[:-1]
        return ColumnSpec(
            name=name,
            type_tag=bare.rstrip("?"),
            nullable=bare.endswith("?"),
            has_const=False,
            const_value=None,
            iso_delta=True,
        )
    if decl.endswith("^"):
        bare = decl[:-1]
        # Affix prefix/suffix are filled in from the ``__affix:`` preamble
        # line; a placeholder here keeps the column flagged as affix-folded.
        return ColumnSpec(
            name=name,
            type_tag=bare.rstrip("?"),
            nullable=bare.endswith("?"),
            has_const=False,
            const_value=None,
            affix=("", ""),
        )
    if decl.endswith("@"):
        bare = decl[:-1]
        # Head dictionary + delimiter come from the ``__head:`` preamble.
        return ColumnSpec(
            name=name,
            type_tag=bare.rstrip("?"),
            nullable=bare.endswith("?"),
            has_const=False,
            const_value=None,
            head_dict=True,
        )
    scale_m = re.match(r"^(.+)%(\d+)$", decl)
    if scale_m:
        bare = scale_m.group(1)
        return ColumnSpec(
            name=name,
            type_tag=bare.rstrip("?"),
            nullable=bare.endswith("?"),
            has_const=False,
            const_value=None,
            dec_scale=int(scale_m.group(2)),
        )
    return ColumnSpec(
        name=name,
        type_tag=decl.rstrip("?"),
        nullable=decl.endswith("?"),
        has_const=False,
        const_value=None,
    )


def _is_sentinel_line(line: str) -> bool:
    """True for the lossy-survivor ``{"_ccr_dropped": ...}`` final line."""
    if not line.startswith("{"):
        return False
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and _CCR_SENTINEL_KEY in obj


def decode_csv_schema_rows(text: str) -> list[dict[str, Any]] | None:
    """Decode a CSV-schema rendering back to its original row objects.

    Returns ``None`` when ``text`` is not a CSV-schema table (no
    ``[N]{...}`` declaration). Rows that cannot be reconstructed are
    skipped — the result is exactly the set of rows the output alone
    proves.
    """
    if not text.startswith("["):
        return None
    lines = _split_logical_lines(text)
    header = _HEADER_RE.match(lines[0])
    if not header:
        return None
    declared_count = int(header.group(1))
    specs: list[ColumnSpec] = []
    for seg in split_unquoted(header.group(2)):
        spec = _parse_header_segment(seg)
        if spec is None:
            return None
        specs.append(spec)

    const_cols = [s for s in specs if s.has_const]
    arith_cols = [s for s in specs if s.arith is not None]
    var_cols = [s for s in specs if not s.has_const and s.arith is None]

    if not var_cols and const_cols and not arith_cols:
        # Degenerate fully-constant table: every row is identical and
        # carried entirely by the declaration; [N] gives the count.
        row = {s.name: s.const_value for s in const_cols}
        return [dict(row) for _ in range(declared_count)]

    # Dictionary preamble: `__dict:name=v0,v1,...` lines directly after
    # the declaration. Only declared column names are accepted — any
    # other line ends the preamble and is processed as a row.
    var_names = {s.name for s in var_cols}
    affix_names = {s.name for s in var_cols if s.affix is not None}
    head_names = {s.name for s in var_cols if s.head_dict}
    dict_values: dict[str, list[str]] = {}
    affixes: dict[str, tuple[str, str]] = {}
    head_dicts: dict[str, tuple[str, list[str]]] = {}
    body_start = 1

    def _unq(seg: str) -> str:
        return (
            _unquote_csv(seg)
            if seg.startswith('"') and seg.endswith('"') and len(seg) >= 2
            else seg
        )

    for line in lines[1:]:
        if line.startswith("__dict:") and "=" in line:
            name, payload = line[len("__dict:") :].split("=", 1)
            if name not in var_names:
                break
            dict_values[name] = [_unq(seg) for seg in split_unquoted(payload)]
            body_start += 1
            continue
        if line.startswith("__affix:") and "=" in line:
            name, payload = line[len("__affix:") :].split("=", 1)
            if name not in affix_names:
                break
            segs = split_unquoted(payload)
            if len(segs) != 2:
                break  # malformed affix line — never invent data
            affixes[name] = (_unq(segs[0]), _unq(segs[1]))
            body_start += 1
            continue
        if line.startswith("__head:") and "=" in line:
            name, payload = line[len("__head:") :].split("=", 1)
            if name not in head_names or not payload:
                break
            delim = payload[0]
            heads = [_unq(seg) for seg in split_unquoted(payload[1:])]
            head_dicts[name] = (delim, heads)
            body_start += 1
            continue
        break

    rows: list[dict[str, Any]] = []
    carry_raw: list[str | None] = [None] * len(var_cols)
    # Per-column (epoch, tz) state for ISO-delta columns.
    iso_state: list[tuple[int, str] | None] = [None] * len(var_cols)
    ordinal = 0  # row index for arithmetic folds — counts every row line
    for line in lines[body_start:]:
        if _is_sentinel_line(line):
            continue
        if not line:
            # An empty physical line is a REAL empty-string value ONLY when
            # there is exactly one variable column (multi-col empty rows still
            # carry their `,` separators, so they are never blank). Bug #24:
            # the old `if not line: continue` dropped that row AND failed to
            # advance `ordinal`, so every later arith-fold value was shifted.
            # Bound emission by the declared row count so the trailing newline
            # artifact (an extra `""` beyond row N) is not turned into a
            # phantom row.
            if len(var_cols) == 1 and len(rows) < declared_count:
                # Fall through to the normal parse path: split_unquoted("")
                # yields [""], which parses as the empty-string cell.
                pass
            else:
                continue
        parts = split_unquoted(line)
        if len(parts) != len(var_cols):
            ordinal += 1  # malformed row still occupies its index
            continue
        row = {}
        ok = True
        for j, (spec, raw) in enumerate(zip(var_cols, parts)):
            if raw == "=":
                resolved = carry_raw[j]
                if resolved is None:
                    ok = False  # ditto before any value: not a data row
                    break
            else:
                resolved = raw
                carry_raw[j] = raw
            if spec.head_dict:
                hd = head_dicts.get(spec.name)
                value = _decode_head_cell(resolved, hd)
                if value is None:
                    ok = False  # never invent data on a bad head cell
                    break
                row[spec.name] = value
            elif spec.affix is not None:
                pre, suf = affixes.get(spec.name, ("", ""))
                # ``resolved`` is the raw CSV cell — still wrapped in quotes if
                # the affix middle contained a newline/comma/quote (e.g. a
                # multi-line log message under a shared prefix/suffix). Unquote
                # it BEFORE re-applying the affix, exactly like every other
                # branch does, otherwise the quote characters leak into the
                # reconstructed value (silent corruption on the lossless path).
                row[spec.name] = pre + _unq(resolved) + suf
            elif spec.iso_delta:
                value = _decode_iso_delta_cell(resolved, iso_state, j)
                if value is None:
                    ok = False
                    break
                row[spec.name] = value
            elif spec.dec_scale is not None:
                value = _decode_decimal_scaled_cell(resolved, spec.dec_scale)
                if value is None:
                    ok = False  # never invent data on a bad cell
                    break
                row[spec.name] = value
            elif spec.name in dict_values:
                values = dict_values[spec.name]
                if not resolved.isdigit() or int(resolved) >= len(values):
                    ok = False  # never invent data on a bad index
                    break
                row[spec.name] = values[int(resolved)]
            else:
                row[spec.name] = _decode_cell(resolved, spec.type_tag)
        if not ok:
            ordinal += 1
            continue
        for spec in const_cols:
            row[spec.name] = spec.const_value
        for spec in arith_cols:
            base, step = spec.arith  # type: ignore[misc]
            row[spec.name] = base + step * ordinal
        rows.append(row)
        ordinal += 1
    return rows


def _decode_decimal_scaled_cell(resolved: str, scale: int) -> float | None:
    """Decode one cell of a decimal scale-fold column (``name:float%k``).

    Pure string manipulation: pad the integer digits to at least
    ``scale+1`` places and re-insert the decimal point, then parse — no
    float arithmetic, so the value is exactly the one the formatter
    encoded. Returns ``None`` for malformed cells (never invent data).
    """
    sign = ""
    digits = resolved
    if digits.startswith("-"):
        sign, digits = "-", digits[1:]
    if not digits.isdigit():
        return None
    padded = digits.zfill(scale + 1)
    split = len(padded) - scale
    try:
        return float(f"{sign}{padded[:split]}.{padded[split:]}")
    except ValueError:  # pragma: no cover — digits guarantee parse
        return None


def _decode_head_cell(
    resolved: str, hd: tuple[str, list[str]] | None
) -> str | None:
    """Decode one head-dict cell ``<idx><delim><tail>`` -> ``head[idx] + tail``.

    Reads the maximal leading digit run as the head index, requires the
    next char to be exactly the column's delimiter, and takes the rest as
    the tail. Returns ``None`` on any deviation (no preamble, bad index,
    missing delimiter) — the caller skips the row rather than inventing
    data.
    """
    if hd is None:
        return None
    delim, heads = hd
    k = 0
    while k < len(resolved) and resolved[k].isdigit():
        k += 1
    if k == 0:
        return None
    idx = int(resolved[:k])
    rest = resolved[k:]
    if not rest.startswith(delim):
        return None
    tail = rest[len(delim) :]
    if idx >= len(heads):
        return None
    return heads[idx] + tail


def _decode_iso_delta_cell(
    resolved: str, iso_state: list[tuple[int, str] | None], j: int
) -> str | None:
    """Decode one (ditto-resolved) cell of an ISO-delta column.

    A ``{±delta}[/tz]`` cell advances the column's carried epoch (the
    timezone spelling carries forward unless restated); a full
    strict-shape ISO cell (re)seeds the state verbatim. Returns ``None``
    when the cell cannot be decoded (e.g. a delta with no seed) — the
    caller skips the row rather than inventing data.
    """
    delta_m = _DELTA_RE.match(resolved)
    if delta_m:
        state = iso_state[j]
        if state is None:
            return None  # delta before any seed: undecodable
        epoch, tz = state
        epoch += int(delta_m.group(1))
        if delta_m.group(2):
            tz = delta_m.group(2)
        rendered = _render_iso(epoch, tz)
        if rendered is None:
            return None
        iso_state[j] = (epoch, tz)
        return rendered
    parsed = _parse_iso(resolved)
    if parsed is not None:
        iso_state[j] = parsed
        return resolved
    # Not delta, not ISO — verbatim string; state resets.
    iso_state[j] = None
    return resolved
