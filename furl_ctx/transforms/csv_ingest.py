"""Tabular ingestion: raw CSV tool output → records → SmartCrusher.

Raw CSV historically routed PLAIN_TEXT → TextCrusher — lossy prose
selection, the wrong tool for a table. This module owns the three steps
that change that:

1. **Sniff** (:func:`sniff_csv`) — delimiter-consistency detection over
   comma / tab / semicolon: ≥ 3 lines, ≥ 2 fields, the header's field
   count repeated across ≥ 90 % of naive lines (the 10 % slack absorbs
   quoted delimiters/newlines the naive count cannot see), then a REAL
   ``csv`` stdlib parse that must come back rectangular. Floors: ≥ 5
   data rows and ≥ 200 bytes. Every ambiguity — delimiter tie, ragged
   rows, duplicate/empty/all-numeric header — returns ``None``
   (fail-open: the content routes exactly as before).
2. **Convert** — header row as keys; cell values coerced to int/float
   ONLY when the coerced value renders back to the exact original cell
   bytes (``"007"`` and ``"1.10"`` stay strings), so the record view
   never misrepresents a cell.
3. **Compress** (:func:`compress_tabular_csv`) — the converted records
   ride the existing JSON_ARRAY / SmartCrusher path; the render ships
   with a raw-recovery marker appended.

Recovery invariant (CCR-RETENTION.md): the ORIGINAL RAW CSV BYTES are
what retrieval must recover — the converted records are a view. The raw
text is persisted under the marker's exact hash via the shared
:func:`~._ccr_persist.persist_to_python_ccr` BEFORE the render ships; on
any store failure the compression is VETOED and the raw CSV passes
through unchanged (the marker never ships dangling).

Detection and dispatch share :func:`sniff_csv` as the ONE predicate, so
a detection claim can never reach a conversion that then disagrees.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._ccr_persist import persist_to_python_ccr

logger = logging.getLogger(__name__)

# Candidate delimiters, per the sniff spec: comma, tab, semicolon.
_DELIMITERS: tuple[str, ...] = (",", "\t", ";")

# Fraction of naive lines that must repeat the header's field count.
_CONSISTENCY_FLOOR = 0.90

# Floors (below either → not tabular, fail-open to the old routing).
MIN_DATA_ROWS = 5
MIN_BYTES = 200

# Cap on the naive consistency scan — cost parity with the sibling
# detectors' line windows (search 100 / log 200 lines). The REAL csv
# parse below still processes everything once a candidate passes.
_SNIFF_SCAN_LINES = 200


@dataclass(frozen=True)
class CsvTable:
    """A successfully sniffed + converted CSV: the record view of the
    raw text. ``row_count`` counts DATA rows (header excluded)."""

    records: list[dict[str, Any]]
    delimiter: str
    row_count: int


def sniff_csv(content: str) -> CsvTable | None:
    """Sniff *content* as delimiter-consistent CSV and convert it.

    Returns ``None`` for anything ambiguous — the caller must treat that
    as "not tabular" and keep the previous routing (fail-open). A
    non-``None`` result is a fully validated, rectangular table.
    """
    if len(content) < MIN_BYTES:
        return None
    stripped = content.strip()
    # JSON / markup shapes are never tabular; bail before any line work.
    if not stripped or stripped.startswith(("[", "{", "<")):
        return None
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    delimiter = _pick_delimiter(lines[:_SNIFF_SCAN_LINES])
    if delimiter is None:
        return None

    try:
        rows = [r for r in csv.reader(io.StringIO(stripped), delimiter=delimiter) if r]
    except csv.Error:
        return None  # malformed under the RFC parser — ambiguous
    if len(rows) < 1 + MIN_DATA_ROWS:
        return None

    header, data = rows[0], rows[1:]
    keys = [cell.strip() for cell in header]
    if len(keys) < 2:
        return None
    # Header discipline: keys must be non-empty, unique, and not ALL
    # numeric-looking (an all-numeric first row is a headerless table —
    # refuse to invent keys from data).
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        return None
    if all(_coerce_cell(k) != k for k in keys):
        return None
    # Rectangular under the REAL parser, or the sniff was wrong.
    if any(len(row) != len(keys) for row in data):
        return None

    records = [dict(zip(keys, (_coerce_cell(cell) for cell in row))) for row in data]
    return CsvTable(records=records, delimiter=delimiter, row_count=len(data))


def _pick_delimiter(lines: list[str]) -> str | None:
    """Pick the single delimiter whose field count is header-consistent.

    A candidate is viable when the header yields ≥ 2 fields and ≥ 90 %
    of the scanned lines repeat the header's count (naive ``str.count``
    — quoting is validated later by the real parser). Two candidates
    tying on (consistency, field count) is an ambiguous sniff → ``None``.
    """
    best_score: tuple[float, int] | None = None
    best_delim: str | None = None
    tied = False
    for delim in _DELIMITERS:
        header_fields = lines[0].count(delim) + 1
        if header_fields < 2:
            continue
        matching = sum(1 for ln in lines if ln.count(delim) + 1 == header_fields)
        ratio = matching / len(lines)
        if ratio < _CONSISTENCY_FLOOR:
            continue
        score = (ratio, header_fields)
        if best_score is None or score > best_score:
            best_score, best_delim, tied = score, delim, False
        elif score == best_score:
            tied = True
    return None if tied else best_delim


def _coerce_cell(cell: str) -> Any:
    """Exact-round-trip numeric coercion for one cell.

    Coerce to int/float ONLY when rendering the coerced value back
    (``str(int)`` / ``repr(float)`` — the same spelling ``json.dumps``
    emits) reproduces the original cell bytes. Anything else — leading
    zeros, ``1e3``, ``inf``/``nan``, padded numbers — stays a string, so
    the record view never misrepresents a cell.
    """
    if not cell or cell[0] not in "-0123456789":
        return cell
    try:
        as_int = int(cell)
    except ValueError:
        pass
    else:
        return as_int if str(as_int) == cell else cell
    try:
        as_float = float(cell)
    except ValueError:
        return cell
    if math.isfinite(as_float) and repr(as_float) == cell:
        return as_float
    return cell


def raw_recovery_hash(content: str) -> str:
    """Cache key for the raw-CSV recovery entry: ``md5(content)[:24]``.

    Same construction as the Rust ``ccr::persist::md5_hex_24`` the
    raw-text compressors use — 24 hex chars, the shape-H marker width.
    A content address, not a security boundary.
    """
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:24]  # nosec B324


def compress_tabular_csv(
    content: str,
    table: CsvTable,
    crusher: Any,
    *,
    context: str = "",
    bias: float = 1.0,
    token_counter: Callable[[str], int],
) -> str | None:
    """Compress a sniffed CSV through SmartCrusher; ship render + marker.

    Returns the shippable compressed text, or ``None`` when the caller
    must serve the RAW CSV unchanged instead — either because the
    converted render (marker included) does not beat the raw bytes in
    tokens, or because the raw-recovery store write failed (veto: the
    marker never ships dangling).

    The appended marker is the standard shape-H bracket form
    (``marker_grammar.BRACKET_RETRIEVE_PATTERN``); ``compressed to 0``
    follows the CCR-offload convention — zero raw rows remain verbatim
    (a converted rendition ships), and the hash recovers the original
    bytes exactly.
    """
    json_view = json.dumps(table.records, ensure_ascii=False, separators=(",", ":"))
    crush_result = crusher.crush(json_view, query=context, bias=bias)

    key = raw_recovery_hash(content)
    marker = f"[{table.row_count} rows compressed to 0. Retrieve more: hash={key}]"
    candidate = f"{crush_result.compressed}\n{marker}"

    if token_counter(candidate) >= token_counter(content):
        logger.debug(
            "tabular ingest: converted render (%d tokens) does not beat raw CSV "
            "(%d tokens); serving raw bytes",
            token_counter(candidate),
            token_counter(content),
        )
        return None

    if not persist_to_python_ccr(
        content,
        candidate,
        key,
        compression_strategy="smart_crusher",
        logger=logger,
    ):
        return None  # store veto — serve the raw CSV, no dangling marker

    logger.info(
        "tabular ingest: %d-row CSV (%d chars) converted and compressed; raw "
        "recoverable at hash=%s",
        table.row_count,
        len(content),
        key,
    )
    return candidate
