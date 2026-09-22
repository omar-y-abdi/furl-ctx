"""Library-side CCR retrieval — turn ``<<ccr:HASH>>`` markers back into content.

The MCP server already exposes retrieve/search; these re-export the same
``CompressionStore`` surface for plain ``from furl_ctx import ...`` users, who
otherwise receive compressed messages carrying markers they cannot resolve.

``retrieve`` mirrors the MCP ``furl_retrieve`` handler's slice filters so a
library caller can drill into a large offloaded original WITHOUT dumping the
whole thing back: a regex/line window over text, a field projection over a JSON
array, or a ROW-SELECT (by value or numeric range) over a JSON array of objects
— including a JSON object with one dominant inner array (the Chrome-trace
shape). With no filter it is byte-identical to a plain full retrieve.
"""

from __future__ import annotations

from typing import Any

from .cache.compression_store import CompressionStore, _active_ccr_store
from .ccr.marker_grammar import hash_of_match, sub_within_budget, substitution_patterns
from .ccr.retrieve_filters import FilterError, RetrieveFilters, apply_filters

# Distinguishes "select_equals was omitted" from an explicit ``select_equals=None`` (a real "field is null" match): the MCP dict path keys on
# presence, and the Python keyword path needs the same distinction, which a plain ``None`` default cannot express. Never leaks past ``retrieve``.
_UNSET: Any = object()


def retrieve(
    hash: str,
    *,
    query: str | None = None,
    pattern: str | None = None,
    context_lines: int = 0,
    line_range: list[int | None] | None = None,
    fields: list[str] | None = None,
    select_field: str | None = None,
    select_equals: Any = _UNSET,
    select_min: float | None = None,
    select_max: float | None = None,
    limit: int | None = None,
    raw: bool = False,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> str | None:
    """Return the original content stored under *hash*, or ``None`` on a miss.

    With NO filter argument this is a full retrieve — the byte-exact stored
    original (or ``None`` if the hash is not in the store's window: never stored,
    evicted under capacity, or TTL-expired — a loud, explicit miss, not a silent
    loss). *query* is optional retrieval-event context on that path.

    Store resolution is SYMMETRIC with ``compress()``: when a namespace is
    active — ``FURL_CCR_PROJECT_DIR`` / ``FURL_CCR_NAMESPACE``, or the same
    ``session_id``/``agent_id`` that was passed to ``compress()`` — this reads
    the SAME isolated per-namespace store that compress call wrote to. With no
    namespace active the default path is the request-scoped store if
    middleware set one, else the global singleton. Same resolution seam as
    ``ccr_export``/``ccr_import`` (``_active_ccr_store``).

    The filter arguments narrow what comes back, mirroring the MCP
    ``furl_retrieve`` tool and reusing the same validated
    :class:`~furl_ctx.ccr.retrieve_filters.RetrieveFilters` spec:

    * ``pattern`` / ``context_lines`` / ``line_range`` — regex + line window over
      the original as TEXT LINES (matching lines, 1-based numbered).
    * ``fields`` — project named keys out of a JSON ARRAY of objects.
    * ``select_field`` + ``select_equals`` (equality) OR
      ``select_min`` / ``select_max`` (numeric range), with an optional
      ``limit`` — keep the ROWS whose ``select_field`` matches, over a JSON array
      of objects or a JSON object with one dominant inner array. Composes with
      ``fields`` (project columns of the selected rows).
    * ``raw`` — with a ``select_field`` row-select, return each matched row
      byte-identical to its source bytes instead of the re-serialized projection
      (for hashing, diffing, or signature checks). Requires ``select_field`` and
      is incompatible with ``fields``; default ``False`` keeps the re-serialized
      output. The rows are rejoined with fresh array punctuation, so only each
      row is byte-exact, not the whole blob.

    A filter argument (other than ``query``) makes this a slice: it returns the
    projected text, and ``None`` still means a store miss (the hash resolved to
    nothing). A malformed combination — an invalid regex/range/field list, an
    incompatible filter mix, a ``fields``/select on a non-array original, or
    ``query`` together with a filter — raises :class:`ValueError` (a caller bug,
    surfaced loudly, exactly where the MCP handler returns a structured error).
    """
    filters = RetrieveFilters.parse(
        {
            "pattern": pattern,
            "context_lines": context_lines,
            "line_range": line_range,
            "fields": fields,
            "select_field": select_field,
            # Forward ``select_equals`` only when the caller actually passed one: ``parse`` keys on presence (an equals-null request
            # differs from a range request), and the ``_UNSET`` sentinel preserves that distinction across the keyword boundary.
            **({} if select_equals is _UNSET else {"select_equals": select_equals}),
            "select_min": select_min,
            "select_max": select_max,
            "limit": limit,
            "raw": raw,
        }
    )
    if isinstance(filters, FilterError):
        raise ValueError(filters.reason)
    if query is not None and not filters.is_empty:
        raise ValueError(
            "query cannot be combined with a slice filter (pattern/line_range/"
            "fields/select_*): use query to search within the entry, or a filter "
            "to project the full original"
        )

    entry = _active_ccr_store(session_id, agent_id).retrieve(hash, query=query)
    if entry is None:
        return None
    if filters.is_empty:
        return entry.original_content

    outcome = apply_filters(entry.original_content, filters)
    if isinstance(outcome, FilterError):
        raise ValueError(outcome.reason)
    return outcome.content


def purge(hash: str, *, session_id: str | None = None, agent_id: str | None = None) -> bool:
    """Delete the stored entry for *hash* from the active CCR store.

    The purge surface (B3 SECURITY): permanently removes a single offloaded
    original so it can no longer be recovered via :func:`retrieve` — the
    companion to the fail-closed redactor for content that was already stored
    before a redaction policy existed, or that must be erased on request.

    Acts on the SAME store the retrieve path reads — ``_active_ccr_store``, the
    resolution seam ``compress()``/``ccr_export`` share: the isolated namespace
    store when one is active (``FURL_CCR_PROJECT_DIR`` / ``FURL_CCR_NAMESPACE``,
    or the ``session_id``/``agent_id`` passed at ``compress()`` time), else the
    request-scoped/global store. A purge
    only ever touches the caller's own tenant store — an entry another tenant
    stored is neither visible nor deletable here.

    CASCADES to nested ``<<ccr:HASH>>`` blobs (B3 / audit): a compressed view
    offloads dropped rows to their own store entries, so erasing only the named
    hash would leave those originals independently retrievable — a copy of the
    "purged" data surviving under another hash. This follows the markers the
    entry references and erases them too (cycle-safe), so ``retrieve`` misses on
    the whole tree afterward.

    Returns:
        ``True`` if the named entry was removed, ``False`` if the hash was absent
        (never stored, already purged, or already evicted/expired out of the
        store window). Total: never raises for a missing hash.
    """
    top_deleted, _nested = _active_ccr_store(session_id, agent_id).delete_cascade(hash)
    return top_deleted


def resolve_markers(
    messages: list[dict[str, Any]],
    *,
    store: CompressionStore | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return a copy of *messages* with every resolvable CCR marker expanded to
    its original content. Unresolvable markers (window miss) are left in place;
    non-string message content is passed through untouched.

    With no explicit ``store``, resolution is symmetric with ``compress()``:
    the active namespace store (``FURL_CCR_PROJECT_DIR`` /
    ``FURL_CCR_NAMESPACE``, or the same ``session_id``/``agent_id`` passed to
    ``compress()``) when one is active, else the request-scoped/global store —
    so markers a namespaced compress just emitted actually expand instead of
    silently window-missing against the global store.
    """
    # Bug-12: a public API must fail with a typed, actionable error on the wrong shape, not a
    # bare ``AttributeError`` from ``str.get`` when a plain string is iterated char-by-char.
    if not isinstance(messages, list):
        raise TypeError(
            "resolve_markers expects a list of message dicts "
            "(e.g. [{'role': 'tool', 'content': '...'}]), got "
            f"{type(messages).__name__}"
        )

    active = store or _active_ccr_store(session_id, agent_id)

    def _expand(text: str) -> str:
        for pattern in substitution_patterns():

            def _sub(match: Any) -> str:
                # lazy: bulk expansion does NOT feed the retrieval-feedback loop (record_feedback_signal=False)
                # — it mechanically restores every marker, not the model selectively fetching one.
                entry = active.retrieve(hash_of_match(match), record_feedback_signal=False)
                return entry.original_content if entry is not None else match.group(0)

            text = sub_within_budget(pattern, _sub, text)
        return text

    # A non-dict element passes through untouched (total) rather than crashing on ``.get`` — mirrors the "non-string content is
    # passed through" contract one level up; the ``isinstance(message, dict)`` short-circuit is what keeps ``.get`` off non-dicts.
    return [
        {**message, "content": _expand(content)}
        if isinstance(message, dict) and isinstance(content := message.get("content"), str)
        else message
        for message in messages
    ]
