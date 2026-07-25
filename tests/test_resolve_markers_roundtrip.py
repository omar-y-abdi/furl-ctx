"""resolve_markers() marker-family substitution correctness (T4).

Pre-mortem audit finding T4: the public ``resolve_markers()`` API replaces
each ``marker_patterns()`` match's SPAN (``match.start()``:``match.end()``)
with the retrieved original content. For ``BRACKET_RETRIEVE_PATTERN``
(shape H) that span covers exactly the whole marker text (open ``"["`` to
close ``"]"``, literal-anchored), so substitution is clean. For the
double-angle family (shapes A-F, ``DOUBLE_ANGLE_PATTERN``) it does NOT: that
pattern is built for HASH EXTRACTION, so its trailing delimiter only
consumes ONE boundary byte after the hash. For any shape with
a descriptive tail (A/B/C/E/F) the rest of the marker (e.g.
``"7_rows_offloaded>>"``) is left glued onto whatever replaces the head, and
even the bare shape (D) leaves a dangling ``">"``. ``json.loads`` on the
resolved content then raises ``JSONDecodeError`` (or, worse, silently
reconstructs the wrong value) while ``resolve_markers`` itself reports
success.

``GENERIC_BRACKET_PATTERN`` (shape G and case-variants) had the INVERSE span
bug, found after T4 shipped: its original lazy-dot interior
(``\\[.*?compressed.*?hash=…``) crossed ``]``/``[`` freely, so with any
earlier ``[`` on the same line the leftmost match STARTED at that innocent
bracket and the substitution DELETED every byte between it and the real
marker (``"See [ticket-42] for context [120 lines compressed to 18.
Retrieve full diff: hash=…]"`` resolved to ``"See <recovered>"``). Fixed by
making the interior wildcards bracket-free (``[^\\[\\]]``) so a match spans
exactly one ``[…]`` run; pinned in the "generic-bracket substitution span"
section below.

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

Follow-up (adversarial review of the first version of this fix): the initial
``DOUBLE_ANGLE_FULL_PATTERN`` used an UNBOUNDED ``[^>]*`` for the tail, which
reopened a ReDoS on ``resolve_markers`` itself — adversarial input with many
``<<ccr:HASH`` starts and no closing ``">>"`` forces an O(remaining-length)
backtrack at every match-start attempt, making the whole scan O(n^2)
(measured: 562.5 KB of ``"<<ccr:aaaaaaaaaaaa" * 32000`` took 19.66s). The
pattern now bounds the tail to ``[^>]{0,64}`` — see the constant's docstring
in ``marker_grammar.py`` for the measured-real-shape-max justification for
64 — which also shrinks the "forged marker" over-match window: a hash-shaped
prefix can no longer swallow arbitrarily much unrelated intervening text on
its way to a distant, unrelated ``">>"``.
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
        r"<<ccr:" + re.escape(ccr_hash) + r"[^>]{0,64}>>", result.messages[0]["content"]
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
    """Pin: on ISOLATED marker text (nothing else on the line) the bracket
    family spans its whole marker and must keep restoring exactly,
    byte-for-byte, both before and after the T4 fix. (With an innocent
    ``[`` earlier on the same line the generic fallback's original span was
    NOT safe — see the "generic-bracket substitution span" section.)"""
    original = f"ORIGINAL-CONTENT-{ccr_hash}"
    get_compression_store().store(original, "compressed-placeholder", explicit_hash=ccr_hash)

    resolved = resolve_markers([{"role": "tool", "content": marker_text}])
    resolved_content = resolved[0]["content"]

    assert resolved_content == original, (
        f"resolve_markers must restore the EXACT original for {marker_text!r}, "
        f"got {resolved_content!r}"
    )


# --------------------------------------------------------------------------- #
# Generic-bracket substitution span (the bracket-family sibling of T4): a
# GENERIC_BRACKET_PATTERN match must span exactly the marker's own "[...]"
# run, never reach back to an earlier innocent "[" on the same line. The
# original lazy-dot interior did exactly that — leftmost-match semantics
# anchored group(0) at the earliest "[" from which "compressed...hash=...]"
# was reachable, and resolve_markers deleted every byte between that bracket
# and the real marker. Shape G ("Retrieve full diff:") is the live producer
# shape only GENERIC matches (BRACKET_RETRIEVE_PATTERN requires "Retrieve
# more:"), so these cases pin G plus the IGNORECASE variant that also falls
# through to GENERIC. Marker literal byte-identical to markers.rs's
# diff_is_byte_identical pin.
# --------------------------------------------------------------------------- #

_G_HASH = "deadbeefcafedeadbeefcafe"
_G_MARKER = f"[120 lines compressed to 18. Retrieve full diff: hash={_G_HASH}]"


def _seed(ccr_hash: str, original: str) -> None:
    get_compression_store().store(original, "compressed-placeholder", explicit_hash=ccr_hash)


def test_generic_bracket_preceding_bracketed_text_is_preserved() -> None:
    """RED on the lazy-dot pattern: the leftmost match starts at
    ``[ticket-42]`` and the substitution deletes ``"[ticket-42] for
    context "`` — silent loss of innocent bytes, not marker text."""
    _seed(_G_HASH, "ORIGINAL-DIFF-BYTES")

    text = f"See [ticket-42] for context {_G_MARKER} end."
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == "See [ticket-42] for context ORIGINAL-DIFF-BYTES end."


def test_generic_bracket_lone_open_bracket_before_marker_is_preserved() -> None:
    """RED on the lazy-dot pattern: even an UNCLOSED ``[`` upstream on the
    line anchors the match early and its trailing bytes are deleted."""
    _seed(_G_HASH, "ORIGINAL-DIFF-BYTES")

    text = f"index a[0 then {_G_MARKER}"
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == "index a[0 then ORIGINAL-DIFF-BYTES"


def test_generic_bracket_json_escaped_single_line_content_survives_byte_exact() -> None:
    """RED on the lazy-dot pattern. In fresh engine output the shape-G marker
    sits on its own line, but re-serialized content (a JSON-encoded tool
    result) collapses to ONE physical line where earlier ``[`` bytes — array
    subscripts here — precede the marker. The lazy-dot span ate
    ``[0]\\n+a[1]\\n`` (real diff bytes); the whole prefix must survive."""
    _seed(_G_HASH, "ORIGINAL-DIFF-BYTES")

    prefix = '{"result": "diff --git a/x b/x\\n@@ -1 +1 @@\\n-a[0]\\n+a[1]\\n'
    text = prefix + _G_MARKER + '"}'
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == prefix + 'ORIGINAL-DIFF-BYTES"}'


def test_generic_bracket_two_markers_on_one_line_preserve_text_between() -> None:
    """RED on the lazy-dot pattern: after the first marker resolves, the scan
    resumes and the SECOND match anchors at ``[note]`` — deleting it. Both
    markers must resolve independently with the bracketed text intact."""
    hash_b = "0123456789abcdef01234567"
    marker_b = f"[40 lines compressed to 6. Retrieve full diff: hash={hash_b}]"
    _seed(_G_HASH, "FIRST-ORIGINAL")
    _seed(hash_b, "SECOND-ORIGINAL")

    text = f"{_G_MARKER} then [note] {marker_b} done"
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == "FIRST-ORIGINAL then [note] SECOND-ORIGINAL done"


def test_generic_bracket_ignorecase_variant_with_preceding_bracket() -> None:
    """RED on the lazy-dot pattern. An uppercase-variant bracket marker is
    NOT matched by the case-sensitive BRACKET_RETRIEVE_PATTERN, so it falls
    through to the IGNORECASE generic fallback — which must keep both the
    flag (still resolves) and the exact span (``[INFO]`` survives)."""
    _seed(_G_HASH, "ORIGINAL-DIFF-BYTES")

    text = f"[INFO] done: [120 Lines COMPRESSED to 18. Retrieve full diff: hash={_G_HASH}]"
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == "[INFO] done: ORIGINAL-DIFF-BYTES"


def test_bracket_retrieve_shape_h_with_preceding_bracket_pin() -> None:
    """Pin (green before and after): shape H is substituted by the
    literal-anchored BRACKET_RETRIEVE_PATTERN before the generic fallback
    ever scans, so a preceding bracket was never at risk on this shape —
    and must stay that way if the pattern order ever changes."""
    ccr_hash = "0011223344556677889900aa"
    _seed(ccr_hash, "ORIGINAL-ROWS")

    text = f"[job-7] output: [200 lines compressed to 30. Retrieve more: hash={ccr_hash}]"
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == "[job-7] output: ORIGINAL-ROWS"


def test_generic_bracket_unresolvable_hash_leaves_text_untouched() -> None:
    """Pin (green before and after): a store MISS substitutes the span with
    itself, so the text must come back byte-identical — the miss path must
    never be the place an over-wide span starts mutating bytes."""
    text = f"See [ticket-42] for context {_G_MARKER} end."
    resolved = resolve_markers([{"role": "tool", "content": text}])

    assert resolved[0]["content"] == text


# --------------------------------------------------------------------------- #
# Forged-marker over-match window (review finding 2) AND the ReDoS regression
# (review finding 1) — ONE two-sided assertion covers both, because both are
# consequences of the same constant: how far past the hash the tail may reach
# for a ">>".
#
# Over-match: bounding the tail bounds how much unrelated intervening text a
# hash-shaped prefix can swallow on its way to a distant, unrelated ">>".
# Below the bound a marker-shaped span still resolves (correctness for any
# real marker, always far under 64 chars of tail); past it the span must not
# match at all, so surrounding text is left untouched rather than silently
# collapsed into one substitution.
#
# ReDoS: the double-angle SUBSTITUTION pattern runs on Python's backtracking
# `re` engine, so an UNBOUNDED tail (`[^>]*`) makes every failed match-start
# scan forward to the next ">>" or end-of-text — O(remaining) backtrack per
# start, i.e. O(n^2) overall on input shaped like many marker starts with no
# closing ">>" (measured: 562.5 KB took 19.66s unbounded vs 0.0095s bounded).
# Bounding the tail to a small constant caps each failed start at O(bound),
# restoring O(n).
#
# The guard below is the bound itself, asserted DETERMINISTICALLY with NO wall
# clock. It was previously ALSO guarded by a separate
# `assert ratio < 3.0` between two sub-millisecond timings, which was deleted:
# a noisy baseline inflates the denominator, so that assertion FALSE-PASSED the
# very O(n^2) regression it named — measured 5 false-greens in 15 runs at load
# 86, and the miss rate RISES with machine load, making it least trustworthy
# exactly on a busy CI runner. The two checks below are a tight two-sided vise
# at 64/65 that catches every widening of the bound (verified by sweep: 20, 32,
# 128, 1024, 4095 and unbounded all trip it), which is strictly more detection
# power than any reach-ceiling or timing proxy could add.
# --------------------------------------------------------------------------- #


def test_forged_marker_cannot_swallow_more_than_bound_chars_of_filler() -> None:
    ccr_hash = "abc123def456"
    original = "SAFE-ORIGINAL"
    get_compression_store().store(original, "compressed-placeholder", explicit_hash=ccr_hash)

    # Exactly at the bound (64 filler chars): still resolves -- the window
    # is inclusive up to the chosen bound, matching every real shape's
    # tail (all far shorter than 64).
    at_bound = f"<<ccr:{ccr_hash}" + ("x" * 64) + ">>"
    resolved = resolve_markers([{"role": "tool", "content": at_bound}])
    assert resolved[0]["content"] == original, (
        "a marker with exactly 64 filler chars before '>>' must still resolve"
    )

    # One char past the bound: the hash-shaped prefix must not match at all,
    # so it must not reach through the oversized filler to a distant later
    # '>>' and swallow everything in between into a single substitution.
    forged = f"<<ccr:{ccr_hash}" + ("x" * 65) + ">> unrelated trailing text >>"
    resolved = resolve_markers([{"role": "tool", "content": forged}])
    assert resolved[0]["content"] == forged, (
        f"a hash-shaped prefix followed by more than 64 filler chars before "
        f"the nearest '>>' must be left completely untouched, not partially "
        f"substituted; got {resolved[0]['content']!r}"
    )


# --------------------------------------------------------------------------- #
# Double-angle marker tail guard, review finding 3, docs/audits/
# IMPROVEMENT-LEDGER.md's "Guard the double-angle marker tail": shape C's
# `kind` field is the one sub-shape whose text is not a producer-fixed
# literal. `crates/furl-core/src/ccr/markers.rs::marker_for_opaque` now
# neutralizes '>' in `kind` at construction, the producer-side half of this
# guard, PR-pinned by `opaque_marker_neutralizes_angle_bracket_in_kind` in
# `markers.rs`, so no real producer can hand this scan a raw '>' inside a
# tail. These two document the CONSUMER-side consequence directly -- via a
# hand-built marker, bypassing the Rust producer entirely -- so the boundary
# behavior a regression on EITHER side of the guard would fall back to is
# pinned here too, not just asserted about in a docstring.
# --------------------------------------------------------------------------- #


def test_tail_with_lone_angle_bracket_is_left_unresolved_not_corrupted() -> None:
    """A single stray '>' inside the tail, what an unguarded kind like
    "weird>thing" would produce, can never pair up with
    DOUBLE_ANGLE_FULL_PATTERN's own '>>' terminator: `[^>]` cannot consume
    the stray '>' itself, and it is not immediately followed by a second
    '>', so the pattern fails to match this marker AT ALL. Fail-closed: the
    raw marker text -- hash still visible -- is left completely untouched
    rather than partially substituted."""
    ccr_hash = "abc123def456"
    get_compression_store().store("SAFE-ORIGINAL", "compressed-placeholder", explicit_hash=ccr_hash)

    marker = f"<<ccr:{ccr_hash},weird>thing,512B>>"
    resolved = resolve_markers([{"role": "tool", "content": marker}])
    assert resolved[0]["content"] == marker, (
        f"a lone '>' inside the tail must leave the marker completely "
        f"unresolved, not partially substituted; got {resolved[0]['content']!r}"
    )


def test_tail_with_doubled_angle_bracket_truncates_the_substitution() -> None:
    """The genuinely dangerous shape, PR #131 review finding 3: TWO
    adjacent '>' inside the tail, what an unguarded kind like "weird>>hack"
    would produce, accidentally satisfy DOUBLE_ANGLE_FULL_PATTERN's own
    '>>' terminator early, so match.group(0) ends right there instead of at
    the marker's real close -- resolve_markers substitutes only that
    truncated span and glues the unconsumed remainder of the original text
    back on raw, byte for byte. This is the failure `marker_for_opaque`'s
    '>' neutralization now makes unreachable for any real producer; this
    test pins the exact corrupted shape so a regression on either side of
    the guard cannot silently start producing valid-looking-but-wrong
    content again."""
    ccr_hash = "abc123def456"
    get_compression_store().store("SAFE-ORIGINAL", "compressed-placeholder", explicit_hash=ccr_hash)

    marker = f"<<ccr:{ccr_hash},weird>>hack,512B>>"
    resolved = resolve_markers([{"role": "tool", "content": marker}])
    resolved_content = resolved[0]["content"]

    assert resolved_content == "SAFE-ORIGINALhack,512B>>", (
        f"a '>>' pair inside the tail must truncate the substitution at "
        f"exactly that point -- the store's original glued directly to the "
        f"unconsumed remainder of the forged marker; got {resolved_content!r}"
    )
