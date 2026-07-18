"""MATRIX · result-cache vs CCR-store divergence on the BRACKET-marker path.

Discovered while building the register. The core promise is "a surfaced
``<<ccr:HASH>>`` / ``[... hash=H]`` pointer ALWAYS resolves byte-exact — no silent
loss." That holds cold, but the process-wide Tier-2 result cache
(``ContentRouter._cache``, 30-min TTL) outlives a CCR store entry (default 1800-s
TTL, plus capacity eviction). On a result-cache HIT after the store entry is gone,
the guard ``CcrMirror.ensure_ccr_backed`` is supposed to re-mirror the entry (or
force a recompute) so the served pointer still resolves — this is pinned for the
DOUBLE-ANGLE ``<<ccr:HASH>>`` sentinel path by ``test_result_cache_ccr_divergence``.

ROOT CAUSE (verified): the guard's gate ``CcrMirror.extract_ccr_hashes`` does NOT
recognize the BRACKET retrieval marker ``[N items compressed to 0. Retrieve more:
hash=H]`` (``router_ccr_mirror.py:100`` → ``extract_ccr_hashes`` returns an empty
set for the bracket form, so ``ensure_ccr_backed`` takes the "no sentinels →
trivially safe to serve" fast-path at ``router_ccr_mirror.py:101-103`` and never
re-backs). ``CompressResult.ccr_hashes`` uses a DIFFERENT scanner
(``hashes_in_text``) that DOES surface the bracket hash — so the public API
advertises a pointer the guard cannot protect. Content that offloads whole via the
envelope/tabular route (e.g. ``yaml_document``, ``go_source``) emits the bracket
form and is therefore vulnerable; content that offloads via the double-angle route
(``xml_document``, ``sql_dump``, logs) is guarded.

Reproduced deterministically: cold compress backs the entry, a store wipe
(simulating eviction — the same simulation ``test_result_cache_ccr_divergence``
uses) leaves the result cache intact, and the next compress serves the cached
bracket pointer whose target is gone → ``retrieve`` returns ``None`` = silent loss.
"""

from __future__ import annotations

from furl_ctx import compress, retrieve
from furl_ctx.cache.compression_store import reset_compression_store
from tests.matrix import _matrix as m


def _cold_then_cache_hit_after_eviction(content: str):
    """Cold compress (backed) → wipe store (evict) → cache-hit compress.

    Returns (bracket_form: bool, recovered_after_hit: str | None).
    """
    r1 = compress([{"role": "tool", "content": content}], model="gpt-4o")
    assert r1.ccr_hashes, "fixture must offload on the cold pass"
    assert retrieve(r1.ccr_hashes[0]) == content, "cold offload must back the entry byte-exact"

    out1 = r1.messages[0]["content"]
    bracket_form = ("hash=" in out1) and ("<<ccr:" not in out1)

    # Simulate CCR store eviction (capacity/TTL) while the process result cache
    # persists — identical to test_result_cache_ccr_divergence's simulation.
    reset_compression_store()
    assert retrieve(r1.ccr_hashes[0]) is None, "store wipe must clear the backing"

    r2 = compress([{"role": "tool", "content": content}], model="gpt-4o")
    assert r2.ccr_hashes, "cache hit must still surface the pointer"
    return bracket_form, retrieve(r2.ccr_hashes[0])


def test_bracket_marker_survives_result_cache_hit_after_eviction() -> None:
    doc = m.yaml_document()  # offloads whole via the envelope/tabular BRACKET path
    bracket_form, recovered = _cold_then_cache_hit_after_eviction(doc)
    assert bracket_form, "precondition: yaml_document must emit the bracket marker form"
    # THE PROMISE: a surfaced retrieval pointer must resolve. Currently None.
    assert recovered == doc, (
        "result-cache hit served a bracket <<ccr>> pointer whose store entry was "
        "evicted and NOT re-backed — the marker is signalled but unrecoverable "
        "(silent data loss)"
    )


def test_double_angle_marker_is_rebacked_on_cache_hit_after_eviction() -> None:
    """Control (PASSES): the double-angle path IS guarded on the public compress()
    apply route — proving the reproduction harness is sound and the gap above is
    specific to the bracket marker form, not to result-cache hits in general.
    """
    doc = m.xml_document()  # offloads whole via the double-angle <<ccr:HASH>> path
    bracket_form, recovered = _cold_then_cache_hit_after_eviction(doc)
    assert not bracket_form, "precondition: xml_document must emit the double-angle form"
    assert recovered == doc, "double-angle pointer must be re-backed on the cache hit"
