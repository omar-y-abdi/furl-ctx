"""The three honest metrics, computed from REAL ``compress()`` output.

Reported separately (never blended):

1. ``lossless_reduction`` — token savings on the path that drops nothing.
   Computed with the REAL tokenizer the engine uses (no len()/4 estimates).
   Only meaningful when ``lossy_drop_ratio == 0`` (no items removed); on the
   lossy path the "savings" come partly from deletion, so we report it but
   flag the case.

2. ``lossy_drop_ratio`` — fraction of distinct input items NOT present in the
   visible output (i.e. removed / offloaded).

3. ``information_retention`` — fraction of distinct input items that are
   either present in the visible output OR recoverable from the CCR store
   via the ``<<ccr:HASH>>`` sentinel. A dropped-but-recoverable item counts
   as retained; a dropped-and-unrecoverable item is LOST.

All token counts go through the engine's own tokenizer selection
(``furl_ctx.tokenizers.get_tokenizer`` wrapped in ``furl_ctx.tokenizer.Tokenizer``)
so the numbers match what the engine itself measures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from furl_ctx import compress
from furl_ctx.cache.compression_store import get_compression_store
from furl_ctx.ccr import marker_grammar
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows

# The model used for token counting. gpt-4o resolves to the real tiktoken
# BPE tokenizer in the engine's registry (genuine token counts, not an
# estimate). The compression behaviour under test is model-agnostic for the
# JSON-array crush path; gpt-4o is chosen purely to get exact BPE counts.
BENCH_MODEL = "gpt-4o"

# The marker prefix is owned by ``marker_grammar`` (single source of truth);
# the substring walker below scans for it then a hex run from the same spec's
# alphabet. Behavior (width-free hex run, like the engine's own walker) is
# unchanged — only the literals are sourced from the owner.
CCR_PREFIX = marker_grammar.CCR_PREFIX
CCR_SENTINEL_KEY = "_ccr_dropped"


@dataclass(frozen=True)
class CaseMetrics:
    """Metrics for one compression case (one dataset, one cardinality)."""

    name: str
    n_input_items: int
    tokens_before: int
    tokens_after: int
    lossless_reduction: float  # token savings ratio (0..1)
    n_present: int  # distinct input items visible in the output
    n_dropped: int  # distinct input items removed from the output
    n_recoverable: int  # of the dropped, how many CCR-recoverable
    lossy_drop_ratio: float  # n_dropped / n_input_items
    information_retention: float  # (present + recoverable) / n_input_items
    took_lossy_path: bool  # did the engine actually drop items?
    transforms: tuple[str, ...]


def _make_tokenizer(model: str = BENCH_MODEL) -> Tokenizer:
    """Build the SAME tokenizer the engine uses for ``model``."""
    return Tokenizer(get_tokenizer(model), model)


def _stringify(content: Any) -> str:
    """Render message content to a string for marker/recovery scanning."""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def collect_ccr_hashes(text: str) -> set[str]:
    """Extract every ``<<ccr:HASH...>>`` hex hash from ``text``.

    Mirrors the engine's substring grammar (smart_crusher.py
    ``_collect_ccr_hashes_from_string``): hex run after the prefix,
    terminated by a non-hex delimiter.
    """
    hashes: set[str] = set()
    idx = 0
    n = len(text)
    while True:
        start = text.find(CCR_PREFIX, idx)
        if start == -1:
            return hashes
        cursor = start + len(CCR_PREFIX)
        end = cursor
        while end < n and text[end] in marker_grammar.HEX_ALPHABET:
            end += 1
        if end > cursor:
            hashes.add(text[cursor:end].lower())
        idx = max(end, cursor + 1)


def _emitted_ccr_hashes(output_text: str) -> set[str]:
    """Extract CCR hashes the ENGINE actually emitted as drop sentinels.

    Only ``{"_ccr_dropped": "<<ccr:HASH ...>>"}`` sentinel rows count. This
    excludes ``<<ccr:...>>`` substrings that merely appear inside an input
    row's value — e.g. this repo's own ``smart_crusher.py`` source (a code
    benchmark item) documents the marker grammar, which would otherwise be a
    false positive.
    """
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        # Columnar / text rendering: a real drop sentinel still carries the
        # key. Scan only sentinel-bearing fragments to avoid input markers.
        hashes: set[str] = set()
        if CCR_SENTINEL_KEY in output_text:
            hashes |= collect_ccr_hashes(output_text)
        return hashes
    if isinstance(parsed, str):
        # Lossy-survivor columnar rendering: the engine ships a JSON
        # string (CSV-schema table) whose FINAL LINE is the sentinel
        # object ``{"_ccr_dropped": "<<ccr:HASH ...>>"}``. Collect hashes
        # only from sentinel-bearing lines that parse to a sentinel
        # object — the exact same strictness as the array branch below
        # (input-embedded ``<<ccr:`` strings in row values don't count).
        hashes = set()
        for line in parsed.split("\n"):
            if CCR_SENTINEL_KEY not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and isinstance(obj.get(CCR_SENTINEL_KEY), str):
                hashes |= collect_ccr_hashes(obj[CCR_SENTINEL_KEY])
        return hashes
    rows = parsed if isinstance(parsed, list) else [parsed]
    hashes = set()
    for row in rows:
        if isinstance(row, dict) and CCR_SENTINEL_KEY in row:
            value = row.get(CCR_SENTINEL_KEY)
            if isinstance(value, str):
                hashes |= collect_ccr_hashes(value)
    return hashes


def _recovered_originals(hashes: set[str], query: str | None) -> list[str]:
    """Retrieve the original content for each CCR hash from the store.

    Returns the list of recovered original-content strings (one per hash that
    resolved). The Python ``CompressionStore`` is mirrored by the engine on
    every drop, keyed by the marker hash.
    """
    store = get_compression_store()
    out: list[str] = []
    for h in hashes:
        entry = store.retrieve(h, query=query)
        if entry is not None and entry.original_content:
            out.append(entry.original_content)
    return out


def _item_signature(item: Any) -> str:
    """Stable signature of a distinct item for presence/recovery matching.

    Canonical JSON (sorted keys) so a row matches regardless of key order
    between the input, the visible output, and the recovered original.
    """
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _output_row_signatures(output_text: str) -> set[str] | None:
    """If the output is a JSON ARRAY of rows, return their canonical sigs.

    The passthrough and lossy paths render a real JSON array of row dicts
    (the lossy path appends a ``{"_ccr_dropped": ...}`` sentinel, which we
    exclude). The lossless columnar path renders a JSON *string*
    (``"[90]{schema}\\nrow\\n..."``) — not an array — so this returns
    ``None`` and presence falls back to the scalar-substring test.
    """
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    sigs: set[str] = set()
    for row in parsed:
        if isinstance(row, dict) and CCR_SENTINEL_KEY in row and len(row) == 1:
            continue  # CCR sentinel, not a real row
        sigs.add(_item_signature(row))
    return sigs


def _decoded_row_signatures(output_text: str) -> set[str] | None:
    """Canonical signatures of rows EXACTLY reconstructed from a columnar
    (CSV-schema) rendering via the documented reference decoder.

    The lossless columnar path ships a JSON *string* whose body is the
    ``[N]{schema}\\nrow\\n...`` table (the lossy-survivor variant appends a
    sentinel line, which the decoder skips). Decoding it back to row
    objects and comparing canonical signatures is the reconstruction-aware
    retention test: a row counts as present only when the output alone
    reproduces it EXACTLY — including through reversible column encodings
    whose cells are not verbatim copies of the original values.

    Returns ``None`` when the output is not a CSV-schema rendering.
    """
    text = output_text
    try:
        parsed = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, str):
        text = parsed
    rows = decode_csv_schema_rows(text)
    if rows is None:
        return None
    return {_item_signature(row) for row in rows}


def _item_present(
    item: Any,
    output_text: str,
    row_sigs: set[str] | None,
    decoded_sigs: set[str] | None = None,
) -> bool:
    """Is ``item`` represented in the visible output?

    Three renderings, strictest applicable test first:
    - JSON array of rows -> exact canonical-signature membership (``row_sigs``).
    - CSV-schema rendering -> DECODE-AND-COMPARE: the row must be exactly
      reconstructible from the output alone (``decoded_sigs``). Verbatim
      string presence does NOT count — reversible encodings are judged by
      reconstruction, and an unreconstructible row is NOT present.
    - Any other text rendering -> every scalar field value must appear
      verbatim in the output text (conservative fallback).
    """
    if row_sigs is not None:
        return _item_signature(item) in row_sigs
    if decoded_sigs is not None:
        return _item_signature(item) in decoded_sigs
    values = _scalar_values(item)
    if not values:
        return _item_signature(item) in output_text
    return all(v in output_text for v in values)


def _scalar_values(item: Any) -> list[str]:
    """Collect every scalar field value of ``item`` as strings."""
    out: list[str] = []
    if isinstance(item, dict):
        for v in item.values():
            out.extend(_scalar_values(v))
    elif isinstance(item, list):
        for v in item:
            out.extend(_scalar_values(v))
    elif isinstance(item, bool):
        out.append("true" if item else "false")
    elif item is None:
        pass
    else:
        out.append(str(item))
    return out


def _item_in_recovered(item: Any, recovered_blobs: list[str]) -> bool:
    """Is ``item`` recoverable from any CCR-retrieved original blob?

    Each blob is the original JSON the engine stashed before dropping. We
    parse it and look for an exactly-matching row (canonical-JSON equality)
    — that is the strict "this distinct item is recoverable" test.
    """
    target = _item_signature(item)
    for blob in recovered_blobs:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            # Non-JSON original (e.g. plain text) — substring fallback.
            if all(v in blob for v in _scalar_values(item)):
                return True
            continue
        rows = parsed if isinstance(parsed, list) else [parsed]
        for row in rows:
            if _item_signature(row) == target:
                return True
    return False


def measure_case(
    name: str,
    query: str,
    items: list[Any],
    messages: list[dict[str, Any]],
    *,
    model: str = BENCH_MODEL,
) -> CaseMetrics:
    """Run ``compress()`` on a case and compute the three honest metrics.

    Args:
        name: Case id (e.g. "logs@90").
        query: User query (for CCR retrieval feedback).
        items: The distinct input row objects.
        messages: The payload to compress.
        model: Token-counting model (default gpt-4o -> real tiktoken).
    """
    tok = _make_tokenizer(model)
    result = compress(messages, model=model)

    output_text = _stringify(result.messages[-1].get("content"))

    # Token reduction (real tokenizer).
    tokens_before = result.tokens_before or tok.count_messages(messages)
    tokens_after = result.tokens_after or tok.count_messages(result.messages)
    lossless_reduction = (
        (tokens_before - tokens_after) / tokens_before if tokens_before > 0 else 0.0
    )

    # CCR markers that the ENGINE emitted: only those carried by a
    # ``{"_ccr_dropped": "<<ccr:HASH ...>>"}`` sentinel row in the output.
    # Markers that merely appear inside an input row's value (e.g. this repo's
    # own source code documents the ``<<ccr:HASH>>`` grammar) are NOT drops
    # and must not be counted.
    emitted_hashes = _emitted_ccr_hashes(output_text)
    recovered = _recovered_originals(emitted_hashes, query)

    row_sigs = _output_row_signatures(output_text)
    decoded_sigs = _decoded_row_signatures(output_text)
    n_present = 0
    n_dropped = 0
    n_recoverable = 0
    for item in items:
        if _item_present(item, output_text, row_sigs, decoded_sigs):
            n_present += 1
        else:
            n_dropped += 1
            if _item_in_recovered(item, recovered):
                n_recoverable += 1

    n_input = len(items)
    lossy_drop_ratio = n_dropped / n_input if n_input else 0.0
    information_retention = (n_present + n_recoverable) / n_input if n_input else 1.0
    took_lossy_path = n_dropped > 0

    return CaseMetrics(
        name=name,
        n_input_items=n_input,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        lossless_reduction=lossless_reduction,
        n_present=n_present,
        n_dropped=n_dropped,
        n_recoverable=n_recoverable,
        lossy_drop_ratio=lossy_drop_ratio,
        information_retention=information_retention,
        took_lossy_path=took_lossy_path,
        transforms=tuple(result.transforms_applied),
    )


def measure_conversation_case(
    name: str,
    query: str,
    items: list[Any],
    messages: list[dict[str, Any]],
    *,
    model: str = BENCH_MODEL,
) -> CaseMetrics:
    """Conversation-level variant of :func:`measure_case`.

    For multi-turn payloads the unit of information is the CONVERSATION, not
    one message: a distinct item counts as *present* when it is visible in
    ANY message of the compressed output (exact row-signature membership on
    JSON-array renderings, decode-and-compare on CSV-schema renderings,
    verbatim-scalar fallback otherwise — the same strictness ladder as
    :func:`measure_case`, applied per message). An item not visible anywhere
    counts as *recoverable* only when a ``{"_ccr_dropped": ...}`` sentinel
    surfaced in SOME message carries a ``<<ccr:HASH>>`` pointer that resolves
    in the CCR store to an original containing the item.
    """
    tok = _make_tokenizer(model)
    result = compress(messages, model=model)

    texts = [_stringify(m.get("content")) for m in result.messages]

    tokens_before = result.tokens_before or tok.count_messages(messages)
    tokens_after = result.tokens_after or tok.count_messages(result.messages)
    lossless_reduction = (
        (tokens_before - tokens_after) / tokens_before if tokens_before > 0 else 0.0
    )

    emitted_hashes: set[str] = set()
    for text in texts:
        emitted_hashes |= _emitted_ccr_hashes(text)
    recovered = _recovered_originals(emitted_hashes, query)

    views = [(text, _output_row_signatures(text), _decoded_row_signatures(text)) for text in texts]
    n_present = 0
    n_dropped = 0
    n_recoverable = 0
    for item in items:
        if any(_item_present(item, text, rs, ds) for text, rs, ds in views):
            n_present += 1
        else:
            n_dropped += 1
            if _item_in_recovered(item, recovered):
                n_recoverable += 1

    n_input = len(items)
    lossy_drop_ratio = n_dropped / n_input if n_input else 0.0
    information_retention = (n_present + n_recoverable) / n_input if n_input else 1.0
    took_lossy_path = n_dropped > 0

    return CaseMetrics(
        name=name,
        n_input_items=n_input,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        lossless_reduction=lossless_reduction,
        n_present=n_present,
        n_dropped=n_dropped,
        n_recoverable=n_recoverable,
        lossy_drop_ratio=lossy_drop_ratio,
        information_retention=information_retention,
        took_lossy_path=took_lossy_path,
        transforms=tuple(result.transforms_applied),
    )
