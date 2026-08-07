"""Outcome + recovery pins for the removal of the unconsumable ``_ccr_rows``
granular row-index marker.

Background: a lossy row-drop used to surface TWO markers in the model-visible
output — the whole-blob recovery pointer ``_ccr_dropped``
(``<<ccr:HASH N_rows_offloaded>>``, retrievable) AND a granular row-index
pointer ``_ccr_rows`` (``<<ccr:HASH#rows N_chunks>>``). The ``HASH#rows`` key
fails ``is_valid_ccr_hash`` (it is not 12/24 lowercase hex), the model's
retrieve path (Python ``CompressionStore``) never held it, and #168 stopped
mirroring the per-row chunks it indexed. So the marker advertised a retrieval
that always failed — a false advertisement in a tool whose job is to make
output smaller. It is removed; row-level recovery is served from the whole-blob
parent instead (a bare retrieve returns the full array; ``select_field`` narrows
it).

These pins lock that outcome in:

* the emitted output of a row-offloading array carries NO ``_ccr_rows`` key and
  no ``#rows`` marker anywhere; and
* row-level recovery still works from the parent hash — a full retrieve returns
  the complete original array, and a ``select_field`` narrow still narrows.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.ccr.retrieve_filters import FilterError, RetrieveFilters, apply_filters
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from tests._fixtures import hash_from_marker as _hash_from_marker
from tests._fixtures import high_entropy_logs as _high_entropy_logs
from tests._fixtures import sentinel_from_output as _sentinel_from_output

pytest.importorskip("tiktoken")


def _compress(items: list[dict]) -> tuple[str, dict]:
    reset_compression_store()
    router = ContentRouter(ContentRouterConfig())
    result = router.compress(json.dumps(items, ensure_ascii=False))
    sentinel = _sentinel_from_output(result.compressed)
    assert sentinel is not None, "expected a lossy row-drop with a CCR sentinel"
    return result.compressed, sentinel


def test_row_offload_output_has_no_rows_marker() -> None:
    """OUTCOME PIN (red on the pre-removal engine): a row-offloading array must
    emit the whole-blob ``_ccr_dropped`` pointer but NOT the unconsumable
    ``_ccr_rows`` granular index — and no ``#rows`` marker text anywhere in the
    model-visible output."""
    items = _high_entropy_logs(90, seed=2000)
    compressed, sentinel = _compress(items)

    # Guard the pin against going vacuously green: this input MUST actually
    # trigger a row-drop (otherwise "no _ccr_rows" would be trivially true).
    assert "_ccr_dropped" in sentinel, "input must produce a real row-offload"

    assert "_ccr_rows" not in sentinel, (
        f"the unconsumable granular row-index marker must not be emitted; "
        f"sentinel still carries _ccr_rows={sentinel.get('_ccr_rows')!r}"
    )
    # Belt-and-suspenders across every render shape: the literal `#rows` key
    # suffix must not appear anywhere in the emitted bytes.
    assert "#rows" not in compressed, (
        "the `#rows` granular-index marker text must not appear in the output"
    )


def test_row_level_recovery_from_parent_full_and_narrowed() -> None:
    """RECOVERY PIN: with the granular index gone, row-level recovery is served
    from the whole-blob parent — a bare retrieve returns the FULL original
    array, and a ``select_field`` narrow returns a strict, correct subset."""
    items = _high_entropy_logs(90, seed=2000)
    _compressed, sentinel = _compress(items)

    parent = _hash_from_marker(sentinel["_ccr_dropped"])

    # Full recovery through the model's own retrieve surface (the Python store):
    # the whole original array, byte-exact.
    entry = get_compression_store().retrieve(parent)
    assert entry is not None, "whole-blob parent must resolve from the store"
    recovered = json.loads(entry.original_content)
    assert recovered == items, "parent retrieve must return the complete original array"

    # Narrowing still narrows: a row-select on the recovered array yields a
    # strict, non-empty subset — exactly the rows whose field matches.
    filters = RetrieveFilters.parse({"select_field": "service", "select_equals": "api"})
    assert isinstance(filters, RetrieveFilters), filters
    narrowed = apply_filters(entry.original_content, filters)
    assert not isinstance(narrowed, FilterError), narrowed

    expected = [r for r in items if r["service"] == "api"]
    assert 0 < len(expected) < len(items), "fixture must have a proper 'api' subset"
    assert narrowed.matched_count == len(expected), (
        f"select_field narrow must return exactly the {len(expected)} matching rows, "
        f"got {narrowed.matched_count}"
    )
    assert narrowed.matched_count < narrowed.total_count, (
        "a narrow must return fewer rows than the whole"
    )
    for row in json.loads(narrowed.content):
        assert row["service"] == "api", "every narrowed row must match the select"
