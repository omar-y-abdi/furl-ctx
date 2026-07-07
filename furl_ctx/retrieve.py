"""Library-side CCR retrieval — turn ``<<ccr:HASH>>`` markers back into content.

The MCP server already exposes retrieve/search; these re-export the same
``CompressionStore`` surface for plain ``from furl_ctx import ...`` users, who
otherwise receive compressed messages carrying markers they cannot resolve.
"""

from __future__ import annotations

import re
from typing import Any

from .cache.compression_store import CompressionStore, get_compression_store
from .ccr.marker_grammar import hash_of_match, marker_patterns


def retrieve(hash: str, query: str | None = None) -> str | None:
    """Return the original content stored under *hash*, or ``None``.

    ``None`` means the hash is not in the store's in-memory window (never stored,
    evicted under capacity, or TTL-expired) — a loud, explicit miss, not a silent
    loss. *query* is optional retrieval-event context.
    """
    entry = get_compression_store().retrieve(hash, query=query)
    return entry.original_content if entry is not None else None


def resolve_markers(
    messages: list[dict[str, Any]], *, store: CompressionStore | None = None
) -> list[dict[str, Any]]:
    """Return a copy of *messages* with every resolvable CCR marker expanded to
    its original content. Unresolvable markers (window miss) are left in place;
    non-string message content is passed through untouched.
    """
    active = store or get_compression_store()

    def _expand(text: str) -> str:
        for pattern in marker_patterns():

            def _sub(match: re.Match[str]) -> str:
                # lazy: bulk expansion does NOT feed the retrieval-feedback loop
                # (record_feedback_signal=False) — it mechanically restores every
                # marker, not the model selectively fetching one.
                entry = active.retrieve(hash_of_match(match), record_feedback_signal=False)
                return entry.original_content if entry is not None else match.group(0)

            text = pattern.sub(_sub, text)
        return text

    resolved: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            resolved.append({**message, "content": _expand(content)})
        else:
            resolved.append(message)
    return resolved
