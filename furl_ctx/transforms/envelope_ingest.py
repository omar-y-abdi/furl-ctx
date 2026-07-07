"""API-envelope ingestion — `{"data": [...records...], "meta": {...}}`.

The dominant REST/GraphQL/vector-DB response shape wraps the records array under
a key inside a JSON object. The base JSON detector only fires on a TOP-LEVEL
array, so these envelopes never reach SmartCrusher (0% saved). This module mirrors
the CSV-ingestion path (``csv_ingest``): one shared sniff predicate feeds both
detection and dispatch, and the compressed inner array ships with the non-array
fields preserved inline plus a marker that recovers the FULL original envelope
byte-exact from the CCR store.

Routes as ``JSON_ARRAY`` (not a new ``ContentType``) for the same reason CSV does:
after unwrapping, the payload IS a records array, and JSON_ARRAY -> SmartCrusher is
exactly where it must land.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._ccr_persist import persist_to_python_ccr
from .csv_ingest import raw_recovery_hash

logger = logging.getLogger(__name__)

# lazy: a fixed heuristic set of common wrapper keys. Envelopes using another key
# (or more than one array-of-dicts key — ambiguous) fall through to raw bytes.
# Widen this list if a real payload uses a wrapper key not covered here.
_COMMON_KEYS: tuple[str, ...] = (
    "data",
    "results",
    "items",
    "hits",
    "records",
    "edges",
    "rows",
    "documents",
)


@dataclass(frozen=True)
class EnvelopeView:
    """A sniffed API envelope: the records array plus the fields around it."""

    key: str
    inner: list[dict[str, Any]]
    other: dict[str, Any]


def sniff_envelope(content: str) -> EnvelopeView | None:
    """Detect a single-array JSON envelope; fail-open (None) on any ambiguity.

    Shared by detection and dispatch so the two can never disagree. Returns None
    unless the content is a JSON object with EXACTLY one common wrapper key whose
    value is a non-empty list of dicts.
    """
    stripped = content.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    matches = [
        k
        for k in _COMMON_KEYS
        if isinstance(obj.get(k), list)
        and obj[k]
        and all(isinstance(item, dict) for item in obj[k])
    ]
    if len(matches) != 1:  # 0 = not an envelope; >1 = ambiguous -> serve raw
        return None

    key = matches[0]
    other = {k: v for k, v in obj.items() if k != key}
    return EnvelopeView(key=key, inner=obj[key], other=other)


def compress_envelope(
    content: str,
    view: EnvelopeView,
    crusher: Any,
    *,
    context: str = "",
    bias: float = 1.0,
    token_counter: Callable[[str], int],
) -> str | None:
    """Compress the envelope's inner array; ship crushed array + meta + marker.

    Returns the shippable compressed text, or ``None`` when the caller must serve
    the RAW envelope unchanged — either the converted render does not beat the raw
    bytes, or the raw-recovery store write failed (veto: the marker never ships
    dangling). The full original envelope is stored under ``key`` so retrieval
    restores it byte-exact; the non-array fields also stay visible inline.
    """
    crushed = crusher.crush(
        json.dumps(view.inner, ensure_ascii=False, separators=(",", ":")),
        query=context,
        bias=bias,
    ).compressed
    key = raw_recovery_hash(content)
    other_json = (
        json.dumps(view.other, ensure_ascii=False, separators=(",", ":"))
        if view.other
        else ""
    )
    marker = f"[{len(view.inner)} items compressed to 0. Retrieve more: hash={key}]"
    candidate = "\n".join(part for part in (crushed, other_json, marker) if part)

    if token_counter(candidate) >= token_counter(content):
        logger.debug(
            "envelope ingest: converted render (%d tokens) does not beat raw "
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
        return None  # store veto — serve the raw envelope, no dangling marker

    logger.info(
        "envelope ingest: '%s' array of %d items compressed; raw recoverable at hash=%s",
        view.key,
        len(view.inner),
        key,
    )
    return candidate
