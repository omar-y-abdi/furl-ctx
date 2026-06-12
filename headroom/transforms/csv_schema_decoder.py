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
_CCR_SENTINEL_KEY = "_ccr_dropped"


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
    lines = text.split("\n")
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
    var_cols = [s for s in specs if not s.has_const]

    if not var_cols and const_cols:
        # Degenerate fully-constant table: every row is identical and
        # carried entirely by the declaration; [N] gives the count.
        row = {s.name: s.const_value for s in const_cols}
        return [dict(row) for _ in range(declared_count)]

    rows: list[dict[str, Any]] = []
    carry_raw: list[str | None] = [None] * len(var_cols)
    for line in lines[1:]:
        if not line or _is_sentinel_line(line):
            continue
        parts = split_unquoted(line)
        if len(parts) != len(var_cols):
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
            row[spec.name] = _decode_cell(resolved, spec.type_tag)
        if not ok:
            continue
        for spec in const_cols:
            row[spec.name] = spec.const_value
        rows.append(row)
    return rows
