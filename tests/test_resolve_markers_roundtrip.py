"""resolve_markers() marker-family substitution correctness (T4).

Pre-mortem audit finding T4: the public ``resolve_markers()`` API replaces
each ``marker_patterns()`` match's SPAN (``match.start()``:``match.end()``)
with the retrieved original content. For the bracket family (shapes G/H,
``BRACKET_RETRIEVE_PATTERN`` / ``GENERIC_BRACKET_PATTERN``) that span already
covers the whole marker text (open ``"["`` to close ``"]"``), so substitution
is clean. For the double-angle family (shapes A-F, ``DOUBLE_ANGLE_PATTERN``)
it does NOT: that pattern is built for HASH EXTRACTION, so its trailing
delimiter only consumes ONE boundary byte after the hash. For any shape with
a descriptive tail (A/B/C/E/F) the rest of the marker (e.g.
``"7_rows_offloaded>>"``) is left glued onto whatever replaces the head, and
even the bare shape (D) leaves a dangling ``">"``. ``json.loads`` on the
resolved content then raises ``JSONDecodeError`` (or, worse, silently
reconstructs the wrong value) while ``resolve_markers`` itself reports
success.

Existing coverage never caught this because it only asserted the ORIGINAL
marker substring was gone (e.g. ``test_namespace_symmetric_retrieve.py``'s
``f"<<ccr:{hash_key}>>" not in json.dumps(resolved)``) — true even when a
corrupted tail is glued onto the recovered content, since that exact
substring is indeed gone. Every assertion here is EXACT-EQUALITY against the
real stored original, never a substring check.

Fix: ``resolve_markers`` now iterates ``marker_grammar.substitution_patterns()``
instead of ``marker_patterns()`` — a full-span variant of the double-angle
family (``DOUBLE_ANGLE_FULL_PATTERN``) replaces the extraction-oriented
``DOUBLE_ANGLE_PATTERN`` there; the bracket entries are reused unchanged.

Audit result: all SIX double-angle sub-shapes (A/B/C/D/E/F) share the ONE
bug — pinned below per-shape, seeded with the byte-identical literals
``crates/furl-core/src/ccr/markers.rs``, ``transforms/cross_message_dedup.py``,
and ``transforms/smart_crusher.py`` are pinned/known to emit. The bracket
family (G/H) was already full-span and is pinned unaffected.
"""

from __future__ import annotations

import json
import re

import pytest

from furl_ctx import compress, resolve_markers
from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store

_MODEL = "claude-sonnet-4-5-20250929"


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_compression_store()
    yield
    reset_compression_store()


# --------------------------------------------------------------------------- #
# Primary repro (mandated): crush a real array through SmartCrusher's row-drop
# path and confirm resolve_markers restores the exact original.
# --------------------------------------------------------------------------- #


def _crush_array_to_double_angle_marker() -> tuple[str, list]:
    """Compress a JSON array through the REAL SmartCrusher row-drop path and
    return ``(marker_text, original_items)`` for the shape-A
    (``<<ccr:HASH N_rows_offloaded>>``) marker it emits — a producer-real
    marker, not a hand-built string.

    80 identical low-uniqueness string rows: SmartCrusher's adaptive sampler
    keeps a couple of literal survivors and offloads the rest to a single CCR
    entry, deterministically forcing a row-drop (same hash every run — fixed
    content into a freshly reset store)."""
    items = ["log-line-0-payload" for _ in range(80)]
    content = json.dumps(items, ensure_ascii=False)
    result = compress([{"role": "tool", "content": content}], model=_MODEL)
    assert result.ccr_hashes, "fixture must trigger a row-drop CCR offload"
    ccr_hash = result.ccr_hashes[0]
    marker_match = re.search(
        r"<<ccr:" + re.escape(ccr_hash) + r"[^>]*>>", result.messages[0]["content"]
    )
    assert marker_match is not None, "fixture must emit a <<ccr:...>> marker for its hash"
    marker_text = marker_match.group(0)
    assert "_rows_offloaded>>" in marker_text, f"expected shape A, got {marker_text!r}"
    return marker_text, items


def test_double_angle_marker_resolves_to_exact_original() -> None:
    """RED on unfixed resolve_markers: a leftover descriptive-tail fragment
    (e.g. ``"78_rows_offloaded>>"``) is glued onto the recovered content
    instead of the marker resolving cleanly to the exact stored original."""
    marker_text, items = _crush_array_to_double_angle_marker()
    ccr_hash = marker_text.split("<<ccr:", 1)[1].split(" ", 1)[0]
    expected_original = get_compression_store().retrieve(ccr_hash).original_content
    assert json.loads(expected_original) == items  # sanity: store holds the full array

    resolved = resolve_markers([{"role": "tool", "content": marker_text}])
    resolved_content = resolved[0]["content"]

    assert resolved_content == expected_original, (
        f"resolve_markers must restore the EXACT stored original for "
        f"{marker_text!r}, got {resolved_content!r}"
    )


def test_resolved_output_is_valid_json_equal_to_original() -> None:
    """RED on unfixed resolve_markers: json.loads raises JSONDecodeError on
    the leftover tail instead of reconstructing the exact original array."""
    marker_text, items = _crush_array_to_double_angle_marker()

    resolved = resolve_markers([{"role": "tool", "content": marker_text}])
    resolved_content = resolved[0]["content"]

    parsed = json.loads(resolved_content)  # must not raise JSONDecodeError
    assert parsed == items


# --------------------------------------------------------------------------- #
# Audit: every double-angle sub-shape shares the SAME substitution bug.
#
# Marker text is byte-identical to the real producers:
#   A/B/C -- crates/furl-core/src/ccr/markers.rs (marker_for_rows_offloaded /
#            marker_for_row_index / marker_for_opaque; byte-pinned there by
#            rows_offloaded_is_byte_identical / row_index_is_byte_identical /
#            opaque_is_byte_identical). B/C use a grammar-valid 12-hex hash
#            (markers.rs's own inline unit-test literals use short
#            illustrative strings that are not width-valid).
#   D     -- furl_ctx/transforms/smart_crusher.py:899 (bare CCR helper).
#   E/F   -- furl_ctx/transforms/cross_message_dedup.py (duplicate_sentinel /
#            near-duplicate sentinel).
#
# Each case seeds the store under exactly the hash resolve_markers' own
# scanner extracts from the marker text (the DOUBLE_ANGLE_PATTERN capture
# group), isolating the substitution-SPAN bug under audit from any unrelated
# store-key-composition question (shape B's granular "#rows" index, in
# particular, is a proportional-retrieval concern outside T4's scope).
# --------------------------------------------------------------------------- #

_DOUBLE_ANGLE_AUDIT_CASES = [
    pytest.param("abc123def456", "<<ccr:abc123def456 7_rows_offloaded>>", id="A-rows_offloaded"),
    pytest.param("9f3a2b112233", "<<ccr:9f3a2b112233#rows 50_chunks>>", id="B-row_index"),
    pytest.param("abc123def456", "<<ccr:abc123def456,base64,2.1KB>>", id="C-opaque"),
    pytest.param("0123456789abcdef01234567", "<<ccr:0123456789abcdef01234567>>", id="D-bare"),
    pytest.param(
        "0011223344556677889900aa",
        "<<ccr:0011223344556677889900aa 4096_bytes_duplicate>>",
        id="E-bytes_duplicate",
    ),
    pytest.param(
        "0011223344556677889900aa",
        "<<ccr:0011223344556677889900aa 4096_bytes_near_duplicate>>",
        id="F-bytes_near_duplicate",
    ),
]


@pytest.mark.parametrize("ccr_hash, marker_text", _DOUBLE_ANGLE_AUDIT_CASES)
def test_every_double_angle_shape_resolves_to_exact_original(ccr_hash, marker_text) -> None:
    """RED on unfixed resolve_markers for A/B/C/D/E/F alike (T4 'other
    families' audit): DOUBLE_ANGLE_PATTERN's head-only capture is shared by
    every shape in the ``<<ccr:...>>`` family, not just rows_offloaded."""
    original = f"ORIGINAL-CONTENT-{ccr_hash}"
    get_compression_store().store(original, "compressed-placeholder", explicit_hash=ccr_hash)

    resolved = resolve_markers([{"role": "tool", "content": marker_text}])
    resolved_content = resolved[0]["content"]

    assert resolved_content == original, (
        f"resolve_markers must restore the EXACT original for {marker_text!r}, "
        f"got {resolved_content!r}"
    )


# --------------------------------------------------------------------------- #
# Pin: the bracket family (G/H) was already full-span; the fix must not
# change its behavior. Byte-identical literals from markers.rs's own
# diff_is_byte_identical / retrieve_more_is_byte_identical pins.
# --------------------------------------------------------------------------- #

_BRACKET_PIN_CASES = [
    pytest.param(
        "deadbeefcafedeadbeefcafe",
        "[120 lines compressed to 18. Retrieve full diff: hash=deadbeefcafedeadbeefcafe]",
        id="G-diff",
    ),
    pytest.param(
        "0011223344556677889900aa",
        "[200 lines compressed to 30. Retrieve more: hash=0011223344556677889900aa]",
        id="H-retrieve_more",
    ),
]


@pytest.mark.parametrize("ccr_hash, marker_text", _BRACKET_PIN_CASES)
def test_bracket_family_resolves_to_exact_original_pin(ccr_hash, marker_text) -> None:
    """Pin: the bracket family already spans its whole marker and must keep
    restoring exactly, byte-for-byte, both before and after the T4 fix."""
    original = f"ORIGINAL-CONTENT-{ccr_hash}"
    get_compression_store().store(original, "compressed-placeholder", explicit_hash=ccr_hash)

    resolved = resolve_markers([{"role": "tool", "content": marker_text}])
    resolved_content = resolved[0]["content"]

    assert resolved_content == original, (
        f"resolve_markers must restore the EXACT original for {marker_text!r}, "
        f"got {resolved_content!r}"
    )
