"""Byte-exact source spans for the rows of an offloaded JSON array (F10 raw mode).

A filtered ``furl_retrieve`` normally *re-serializes* the rows it keeps
(``json.dumps(..., indent=2)``), so the bytes come back semantically complete but
not byte-identical to the source — fine for reading, wrong for hashing, diffing,
or signature checks. ``raw`` mode instead returns each matched row EXACTLY as it
appeared in the stored original. This module is the span scanner behind it: given
a retrieved original that is a top-level JSON array of objects OR a JSON object
with one dominant inner array (the Chrome-trace ``{metadata, traceEvents:[…]}``
shape), it returns each row's precise source substring.

Contract and boundaries:

* PURE and stdlib-only (``json``). It deliberately does NOT import
  ``retrieve_filters`` — validation and the user-facing ``FilterError`` messages
  live there in ``_extract_rows``; this module only computes spans over a shape
  that was ALREADY accepted, and returns ``None`` for anything it does not
  recognize (the caller maps that to its own error). Keeping the dependency
  one-way avoids an import cycle.
* Byte-exact by construction: a span is ``source[start:end]`` where ``start`` is
  the first non-whitespace byte of an element and ``end`` is
  :meth:`json.JSONDecoder.raw_decode`'s end index (immediately after the value,
  before any trailing whitespace). The array's own ``[ , ]`` punctuation and the
  whitespace between elements are therefore EXCLUDED — the caller rejoins rows
  with fresh punctuation, so only each ROW is guaranteed byte-identical, never
  the whole blob (reading (i), the honest contract).
* Total: any malformed byte the pre-validation somehow let through yields
  ``None`` rather than a raised ``JSONDecodeError``, so the pure filter layer
  above stays non-raising.
"""

from __future__ import annotations

import json
from typing import Any

# JSON insignificant whitespace (RFC 8259 §2). ``str.split`` is not used — we
# must track exact indices, not tokens.
_WS = " \t\n\r"


def _skip_ws(source: str, index: int) -> int:
    """The first index at or after ``index`` whose byte is not JSON whitespace."""
    length = len(source)
    while index < length and source[index] in _WS:
        index += 1
    return index


def _array_spans(source: str, start: int, decoder: json.JSONDecoder) -> list[tuple[Any, str]]:
    """``source[start]`` is ``[``. Return ``(value, exact_span)`` per element.

    ``exact_span`` is the element's source substring with the surrounding
    whitespace and the array's own punctuation excluded, so it is byte-identical
    to how that element was written.
    """
    spans: list[tuple[Any, str]] = []
    index = _skip_ws(source, start + 1)
    length = len(source)
    if index < length and source[index] == "]":
        return spans  # empty array — no rows (upstream rejects this shape anyway)
    while True:
        value, end = decoder.raw_decode(source, index)
        spans.append((value, source[index:end]))
        index = _skip_ws(source, end)
        if index >= length or source[index] != ",":
            break
        index = _skip_ws(source, index + 1)
    return spans


def _dominant_array_spans(
    source: str, start: int, decoder: json.JSONDecoder
) -> list[tuple[Any, str]] | None:
    """``source[start]`` is ``{``. Scan the object's top-level members, find the
    single one whose value is a non-empty array of objects (the SAME dominant
    array rule ``retrieve_filters._extract_rows`` enforces), and return that
    array's element spans. ``None`` when zero or several members qualify — the
    ambiguous/absent case the caller already rejected.
    """
    dominant_value_starts: list[int] = []
    index = _skip_ws(source, start + 1)
    length = len(source)
    while index < length and source[index] != "}":
        _key, key_end = decoder.raw_decode(source, index)  # a JSON string key
        index = _skip_ws(source, key_end)  # now at ':'
        index = _skip_ws(source, index + 1)  # skip ':' then whitespace → value
        value_start = index
        value, value_end = decoder.raw_decode(source, index)
        if isinstance(value, list) and value and all(isinstance(e, dict) for e in value):
            dominant_value_starts.append(value_start)
        index = _skip_ws(source, value_end)
        if index >= length or source[index] != ",":
            break
        index = _skip_ws(source, index + 1)
    if len(dominant_value_starts) != 1:
        return None
    return _array_spans(source, dominant_value_starts[0], decoder)


def array_element_spans(source: str) -> list[tuple[Any, str]] | None:
    """Byte-exact ``(value, span)`` for each row of the rows-array in *source*.

    Handles the two shapes a row-select accepts: a top-level JSON array, or a
    JSON object with one dominant inner array. Returns ``None`` for any other
    shape (already excluded upstream, so ``None`` is the defensive branch) and
    for malformed bytes — never raises, keeping the filter layer above total.
    """
    try:
        decoder = json.JSONDecoder()
        index = _skip_ws(source, 0)
        if index >= len(source):
            return None
        if source[index] == "[":
            return _array_spans(source, index, decoder)
        if source[index] == "{":
            return _dominant_array_spans(source, index, decoder)
        return None
    except json.JSONDecodeError:
        return None
