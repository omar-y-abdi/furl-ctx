"""MATRIX-01 unit pin: the mirror's re-backing gate must recognise EVERY marker
shape the public ``CompressResult.ccr_hashes`` surface advertises.

``CcrMirror.extract_ccr_hashes`` is the gate ``ensure_ccr_backed`` uses to decide
whether a cached result-cache output still needs re-backing. Before the fix it
short-circuited on ``"<<ccr:" not in text`` and only walked the double-angle
family, so a surfaced BRACKET pointer
(``[N ... compressed to M. Retrieve more: hash=H]``) yielded an EMPTY set — the
guard then served that pointer unverified and it dangled after store eviction
(silent data loss). The public scanner ``hashes_in_text`` always surfaced that
same hash to callers, so the guard protected a strict subset of the API surface.

The fix delegates ``extract_ccr_hashes`` to the SAME owned grammar
(``marker_grammar.hashes_in_text``). These tests pin that the two scanners now
agree byte-for-byte across every marker shape — the bracket regression in
particular — so they can never drift back apart.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.ccr.marker_grammar import hashes_in_text
from furl_ctx.transforms.router_ccr_mirror import CcrMirror

# Concrete valid hashes: 24-hex (bracket / bare double-angle) and 12-hex
# (row-drop / opaque double-angle), lowercase — the strict consumer widths.
_H24 = "a1b2c3d4e5f6a1b2c3d4e5f6"
_H12 = "0123456789ab"

# One representative of every consumer-visible marker shape (grammar shapes
# A/C/D double-angle + G/H bracket).
_BRACKET_RETRIEVE = f"[120 items compressed to 0. Retrieve more: hash={_H24}]"
_BRACKET_DIFF = f"[10 lines compressed to 3. Retrieve full diff: hash={_H24}]"
_DOUBLE_ANGLE_ROWDROP = f"<<ccr:{_H12} 393_rows_offloaded>>"
_DOUBLE_ANGLE_BARE = f"<<ccr:{_H24}>>"
_DOUBLE_ANGLE_OPAQUE = f"<<ccr:{_H12},base64,4.5KB>>"


def test_extract_detects_bracket_retrieve_marker() -> None:
    """The exact MATRIX-01 regression: a bracket retrieval pointer (no
    ``<<ccr:``) must be surfaced, not silently dropped by a ``"<<ccr:" not in
    text`` short-circuit."""
    assert "<<ccr:" not in _BRACKET_RETRIEVE  # precondition: pure bracket form
    assert CcrMirror.extract_ccr_hashes(_BRACKET_RETRIEVE) == {_H24}


@pytest.mark.parametrize(
    "text, expected",
    [
        (_BRACKET_RETRIEVE, {_H24}),
        (_BRACKET_DIFF, {_H24}),
        (_DOUBLE_ANGLE_ROWDROP, {_H12}),
        (_DOUBLE_ANGLE_BARE, {_H24}),
        (_DOUBLE_ANGLE_OPAQUE, {_H12}),
        ("no markers here at all", set()),
        ("", set()),
    ],
)
def test_extract_matches_public_scanner(text: str, expected: set[str]) -> None:
    """The mirror gate agrees with the public ``hashes_in_text`` surface for
    every shape — the invariant the fix establishes."""
    got = CcrMirror.extract_ccr_hashes(text)
    assert got == expected
    assert got == set(hashes_in_text(text)), "mirror gate diverged from public surface"


def test_extract_detects_bracket_embedded_in_json() -> None:
    """Result-cache outputs are typically JSON; a bracket pointer inside a JSON
    string value must still be surfaced (a cached-output shape)."""
    cached = json.dumps({"_ccr_dropped": _BRACKET_RETRIEVE, "kept": ["a", "b"]})
    assert CcrMirror.extract_ccr_hashes(cached) == {_H24}


def test_extract_surfaces_all_hashes_in_mixed_text() -> None:
    """A mixed output carrying several distinct markers surfaces every hash —
    bracket AND double-angle together, deduped, matching the public surface."""
    mixed = f"prefix {_DOUBLE_ANGLE_ROWDROP} middle {_BRACKET_RETRIEVE} tail {_DOUBLE_ANGLE_BARE}"
    got = CcrMirror.extract_ccr_hashes(mixed)
    assert got == {_H12, _H24}
    assert got == set(hashes_in_text(mixed))
