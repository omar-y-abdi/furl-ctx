"""Characterization test: all 9 CCR marker shapes through the PRODUCTION consumer.

WHAT THIS PINS (and why it exists)
==================================
The CCR retrieval contract has two halves that must agree byte-for-byte:

* a PRODUCER emits a marker into a tool result, e.g.
  ``<<ccr:HASH N_rows_offloaded>>`` or ``[N lines compressed to M. ... hash=H]``,
  and stores the original under ``HASH`` in the CCR ``CompressionStore``;
* a CONSUMER (``headroom.ccr.tool_injection.CCRToolInjector.scan_for_markers``)
  scans messages, extracts ``HASH``, and the retrieval path resolves
  ``store.retrieve(HASH).original_content`` back to the byte-exact original.

The existing recovery gate (``test_ccr_recovery_invariant.py``,
``ccr_roundtrip.rs``) scans markers with its OWN regexes, which only cover
shapes A (``..._rows_offloaded``) and C (``,KIND,SIZE``). It is therefore BLIND
to a regression in the *production consumer* grammar for the other shapes
(B/E/F/G/H/I). This test drives every distinct marker shape through the REAL
``scan_for_markers`` extraction and asserts byte-exact recovery, so a future
marker-grammar refactor that silently breaks a consumer pattern fails HERE,
naming the exact shape.

THE 9 SHAPES (verified against source — file:line cited per case)
=================================================================
  A  ``<<ccr:HASH N_rows_offloaded>>``        12-hex  crusher.rs:1239
  B  ``<<ccr:HASH#rows N_chunks>>``           12-hex  crusher.rs:1212 (index_key="{hash}#rows")
  C  ``<<ccr:HASH,KIND,SIZE>>``               12-hex  walker.rs:193 / formatter.rs:568
  D  ``<<ccr:HASH>>`` bare                    24-hex  smart_crusher.py:871 (explicit_hash :875)
  E  ``<<ccr:HASH N_bytes_duplicate>>``       24-hex  cross_message_dedup.py:164
  F  ``<<ccr:HASH N_bytes_near_duplicate>>``  24-hex  cross_message_dedup.py:188
  G  ``[N lines compressed to M. Retrieve full diff: hash=H]``  24-hex MD5  diff_compressor.rs:479
  H  ``[N items compressed to M. Retrieve more: hash=H]``       24-hex  kompress_compressor.py:947
                                                                 (also log/search compressor, Rust)
  I  ``[Read content stale: ... Retrieve original: hash=H]``    24-hex  read_lifecycle.py:491

METHOD (per the recon + advisor guidance)
=========================================
The blind spot being closed is the CONSUMER GRAMMAR, so the honest, reliable
way to pin it is: construct each marker from the producer's EXACT format string
(read from source, cited per case) and insert the original into the SAME
``CompressionStore`` the consumer pairs with, keyed by the matching hash
(``explicit_hash`` for fixed-width Rust hashes; store-default SHA-256[:24] /
MD5[:24] where the producer uses those). Where a producer is a cheap Python
call that lands in the Python store, we ALSO drive the REAL producer:

  * E / F — call the real ``duplicate_sentinel`` / ``near_duplicate_rendering``
            render functions from ``cross_message_dedup`` (the full emitted JSON
            note, not the bare marker) and explicit-store the original;
  * H     — use the real kompress emitter format string + store-default hash;
  * I     — store via the global store (as ``read_lifecycle`` does) and build
            the marker with read_lifecycle's exact stale format string.

Only E and F are genuinely PRODUCER-DRIVEN — they call the real
``cross_message_dedup`` render functions, which build the marker text. A/B/C/D
construct the marker from the Rust producer's exact format string; G/H/I
construct from the Python producer's exact format string and call ``store.store``
the same way the producer does. Per-shape method (producer-driven vs
format-constructed) is recorded in the ``method`` field of each :class:`Case`.

★ Shape I matches NO consumer pattern (no ``compressed`` token, no ``<<ccr:``)
  and is recovered via a DIRECT store lookup, never via ``scan_for_markers``.
  We PIN that today's behavior with a dual assertion: the scanner does NOT
  surface I's hash, AND a direct ``store.retrieve`` returns the original. A
  future "unify everything through one parser" change can't silently alter it
  without failing this case.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

import pytest

from headroom.cache.compression_store import CompressionStore
from headroom.ccr.tool_injection import CCRToolInjector
from headroom.transforms.cross_message_dedup import (
    duplicate_sentinel,
    near_duplicate_rendering,
)
from headroom.transforms.read_lifecycle import ReadState

# --------------------------------------------------------------------------- #
# Hash helpers — mirror the EXACT producer hash functions (verified in source).
# --------------------------------------------------------------------------- #


def _sha256_12(payload: str) -> str:
    """SmartCrusher 12-hex key: sha256(payload)[:12].

    Rust ``hash_canonical`` (crusher.rs:1607) feeds the row-drop (A) and
    row-index (B) markers; the opaque walker/formatter (C) uses the same
    truncated SHA-256. We mirror it so ``explicit_hash`` lands under the key
    the marker carries.
    """
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _sha256_24(payload: str) -> str:
    """Canonical Python store key: sha256(original)[:24].

    Matches ``CompressionStore.store`` default (compression_store.py:372) used
    by the bare-marker (D) and the dedup (E/F) explicit-store path.
    """
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _md5_24(payload: str) -> str:
    """diff/log/search 24-hex key: md5(content)[:24].

    Mirrors Rust ``md5_hex_24`` (diff_compressor.rs:1140 — lowercase MD5,
    ``hex.truncate(24)``). Feeds shape G's "Retrieve full diff:" marker.
    """
    return hashlib.md5(payload.encode()).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# Case model.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """One marker-shape characterization case.

    ``build`` receives a fresh per-case :class:`CompressionStore`, stores the
    original under the hash the marker will carry, and returns
    ``(emitted_text, expected_hash, original)``. ``emitted_text`` is the FULL
    bytes a producer would put in the tool result (the whole JSON note for
    E/F, the whole marker line for G/H) — exactly what ``scan_for_markers``
    sees in production, so a competing pattern firing first would be caught.
    """

    shape_id: str
    description: str
    method: str  # "producer-driven" | "format-constructed"
    expect_extracted: bool  # does scan_for_markers surface the hash?
    expect_hash_len: int  # asserted len of the extracted/expected hash
    build: Callable[[CompressionStore], tuple[str, str, str]]


# --------------------------------------------------------------------------- #
# Shape builders.  Each cites the producer source it reproduces.
# --------------------------------------------------------------------------- #


def _build_A(store: CompressionStore) -> tuple[str, str, str]:
    # crusher.rs:1239 -> format!("<<ccr:{hash} {dropped_count}_rows_offloaded>>")
    # hash = hash_canonical = sha256[:12].  Stored under that 12-hex key.
    original = json.dumps([{"id": i, "v": f"row-{i}"} for i in range(42)])
    h = _sha256_12(original)
    store.store(original=original, compressed=f"<<ccr:{h}>>", explicit_hash=h)
    marker = f"<<ccr:{h} 42_rows_offloaded>>"
    return marker, h, original


def _build_B(store: CompressionStore) -> tuple[str, str, str]:
    # crusher.rs:1212 -> format!("<<ccr:{index_key} {dropped_count}_chunks>>")
    # crusher.rs (just above) -> index_key = format!("{hash}#rows"), hash = sha256[:12].
    # The consumer pattern 4 delimiter class [ ,#>] stops the hash capture at
    # '#', so the EXTRACTED hash is the bare 12-hex blob hash (not "{hash}#rows").
    # That bare 12-hex blob hash is the whole-blob recovery key, so we store the
    # original under it (matching crusher's unconditional whole-blob persist).
    original = json.dumps([{"k": i, "blob": "x" * 20} for i in range(7)])
    h = _sha256_12(original)
    store.store(original=original, compressed=f"<<ccr:{h}>>", explicit_hash=h)
    marker = f"<<ccr:{h}#rows 7_chunks>>"
    return marker, h, original


def _build_C(store: CompressionStore) -> tuple[str, str, str]:
    # walker.rs:193 / formatter.rs:568 -> "<<ccr:{},{},{}>>".format(hash, kind, size)
    # 12-hex opaque-blob sentinel (lossless:table substitution).
    original = "BASE64BLOBPAYLOAD" * 64
    h = _sha256_12(original)
    store.store(original=original, compressed=f"<<ccr:{h}>>", explicit_hash=h)
    marker = f"<<ccr:{h},base64,1.1kB>>"
    return marker, h, original


def _build_D(store: CompressionStore) -> tuple[str, str, str]:
    # smart_crusher.py:871 -> compressed=f"<<ccr:{ccr_hash}>>", explicit_hash at :875.
    # ccr_hash here is the canonical 24-hex key (the bare marker carries the full
    # 24-hex; the Python mirror stores under explicit_hash=ccr_hash).
    original = json.dumps({"detail": "bare-sentinel payload", "n": 99})
    h = _sha256_24(original)
    store.store(original=original, compressed=f"<<ccr:{h}>>", explicit_hash=h)
    marker = f"<<ccr:{h}>>"
    return marker, h, original


def _build_E(store: CompressionStore) -> tuple[str, str, str]:
    # PRODUCER-DRIVEN: cross_message_dedup.duplicate_sentinel (:155) emits the
    # real JSON note containing `<<ccr:{hash} {n_bytes}_bytes_duplicate>>`
    # (:164). 24-hex canonical key.  We feed the WHOLE note to the consumer.
    original = json.dumps([{"row": i, "data": f"dup-{i}"} for i in range(5)])
    h = _sha256_24(original)
    store.store(original=original, compressed="", explicit_hash=h)
    emitted = duplicate_sentinel(ccr_hash=h, n_bytes=len(original), first_message_index=2)
    # Sanity: the real producer embedded our hash in its emitted note.
    assert f"<<ccr:{h} {len(original)}_bytes_duplicate>>" in emitted
    return emitted, h, original


def _build_F(store: CompressionStore) -> tuple[str, str, str]:
    # PRODUCER-DRIVEN: cross_message_dedup.near_duplicate_rendering (:173) emits
    # the real JSON array note containing
    # `<<ccr:{hash} {n_bytes}_bytes_near_duplicate>>` (:188). 24-hex key.
    original = json.dumps([{"row": i, "data": f"near-{i}"} for i in range(8)])
    h = _sha256_24(original)
    store.store(original=original, compressed="", explicit_hash=h)
    changed = [{"row": 0, "data": "near-0-CHANGED"}]
    emitted = near_duplicate_rendering(
        changed,
        ccr_hash=h,
        n_bytes=len(original),
        n_shared=7,
        n_total=8,
        source_message_index=3,
    )
    assert f"<<ccr:{h} {len(original)}_bytes_near_duplicate>>" in emitted
    return emitted, h, original


def _build_G(store: CompressionStore) -> tuple[str, str, str]:
    # diff_compressor.rs:479 ->
    #   "[{} lines compressed to {}. Retrieve full diff: hash={}]"
    # key = md5_hex_24(content) (diff_compressor.rs:1140, lowercase, truncate 24).
    # Consumer pattern 1 needs "Retrieve more:" (no match), so this resolves via
    # the GENERIC fallback pattern 3 (`\[.*?compressed.*?hash=([a-f0-9]{24})\]`,
    # IGNORECASE) — pin that it DOES extract.
    original = "diff --git a/x b/x\n" + "\n".join(f"+line {i}" for i in range(120))
    h = _md5_24(original)
    store.store(original=original, compressed="<compressed diff>", explicit_hash=h)
    marker = f"[120 lines compressed to 12. Retrieve full diff: hash={h}]"
    return marker, h, original


def _build_H(store: CompressionStore) -> tuple[str, str, str]:
    # FORMAT-CONSTRUCTED from kompress_compressor.py:947 ->
    #   f"\n[{n_words} items compressed to {compressed_count}. Retrieve more: hash={cache_key}]"
    # cache_key = store-default 24-hex (CompressionStore.store SHA-256[:24]) — we
    # call store.store() the same way kompress's _store_in_ccr does (:1354), but
    # we build the marker text ourselves rather than running the kompress model.
    # Also the canonical "Retrieve more:" form shared by log/search compressors.
    # Resolves via consumer pattern 1 (standard "Retrieve more:" form).
    original = " ".join(f"word{i}" for i in range(400))
    h = store.store(original=original, compressed="<compressed text>")
    marker = f"\n[400 items compressed to 40. Retrieve more: hash={h}]"
    return marker, h, original


def _build_I(store: CompressionStore) -> tuple[str, str, str]:
    # FORMAT-CONSTRUCTED from read_lifecycle.py:491 (STALE branch) ->
    #   f"[Read content stale: {file_display} was modified after this read. "
    #   f"Retrieve original: hash={ccr_hash}]"
    # The original is stored via self.store.store(...) (read_lifecycle.py:480) under
    # the store-default 24-hex key.  This marker matches NO consumer pattern (no
    # "compressed" token, no "<<ccr:") — recovery is DIRECT store lookup only.
    original = "the quick brown fox\n" * 50
    h = store.store(
        original=original,
        compressed="",
        tool_name="Read",
        compression_strategy=f"read_lifecycle:{ReadState.STALE.value}",
    )
    file_display = "/repo/src/main.py"
    marker = (
        f"[Read content stale: {file_display} was modified after this read. "
        f"Retrieve original: hash={h}]"
    )
    return marker, h, original


CASES: list[Case] = [
    Case("A_rows_offloaded", "12-hex SmartCrusher row-drop sentinel (space)",
         "format-constructed", True, 12, _build_A),
    Case("B_rows_index", "12-hex SmartCrusher row-index sentinel (# delimiter)",
         "format-constructed", True, 12, _build_B),
    Case("C_opaque_blob", "12-hex opaque-blob sentinel (comma, KIND, SIZE)",
         "format-constructed", True, 12, _build_C),
    Case("D_bare", "24-hex bare <<ccr:HASH>> sentinel (>> immediately after hash)",
         "format-constructed", True, 24, _build_D),
    Case("E_bytes_duplicate", "24-hex cross-message exact-duplicate note (space sep, like A)",
         "producer-driven", True, 24, _build_E),
    Case("F_bytes_near_duplicate", "24-hex cross-message near-duplicate note (space sep)",
         "producer-driven", True, 24, _build_F),
    Case("G_diff_retrieve_full", "24-hex MD5 diff marker ('Retrieve full diff:', generic fallback)",
         "format-constructed", True, 24, _build_G),
    Case("H_retrieve_more", "24-hex kompress/log/search marker ('Retrieve more:')",
         "format-constructed", True, 24, _build_H),
    Case("I_read_stale", "24-hex read_lifecycle stale marker — NOT scanned, direct-store recovery",
         "format-constructed", False, 24, _build_I),
]


@pytest.fixture()
def store() -> CompressionStore:
    """A fresh, isolated store per case.

    A dedicated instance (not the global singleton) keeps each case's hashes
    from colliding and makes ``scan_for_markers`` see only the text we feed.
    """
    return CompressionStore(max_entries=100)


@pytest.fixture()
def injector() -> CCRToolInjector:
    """The REAL production consumer.

    ``CCRToolInjector.scan_for_markers`` is the public extract path; its
    ``_marker_patterns`` are the production grammar under test. We never
    reimplement the regex here.
    """
    return CCRToolInjector()


def _scan(injector: CCRToolInjector, text: str) -> list[str]:
    """Run the REAL public consumer extraction over one message's content."""
    return injector.scan_for_markers([{"role": "user", "content": text}])


@pytest.mark.parametrize("case", CASES, ids=[c.shape_id for c in CASES])
def test_marker_shape_round_trips_through_production_consumer(
    case: Case,
    store: CompressionStore,
    injector: CCRToolInjector,
) -> None:
    """Each marker shape: emit -> REAL scan_for_markers -> byte-exact recovery.

    Green by construction: it characterizes the CURRENT consumer + store
    behavior. A future grammar refactor that drops a shape from the consumer,
    truncates a hash, or changes shape I's direct-recovery contract fails the
    matching parametrized case by name.
    """
    emitted, expected_hash, original = case.build(store)

    detected = _scan(injector, emitted)

    if case.expect_extracted:
        # The production consumer must surface exactly this hash...
        assert expected_hash in detected, (
            f"{case.shape_id}: production consumer did NOT extract the hash from "
            f"the emitted marker.\n  emitted={emitted!r}\n  expected_hash={expected_hash!r}"
            f"\n  detected={detected!r}"
        )
        # ...at the producer's true width (a truncation regression names itself).
        assert len(expected_hash) == case.expect_hash_len
        for h in detected:
            assert len(h) == case.expect_hash_len, (
                f"{case.shape_id}: consumer extracted a {len(h)}-char hash "
                f"({h!r}); expected width {case.expect_hash_len}"
            )
        # ...and the store resolves it to the BYTE-EXACT original.
        entry = store.retrieve(expected_hash)
        assert entry is not None, f"{case.shape_id}: store.retrieve returned None"
        assert entry.original_content == original, (
            f"{case.shape_id}: recovered content is not byte-exact with the original"
        )
    else:
        # Shape I: PIN the direct-store-only recovery contract.
        # (a) the consumer does NOT surface I's hash...
        assert expected_hash not in detected, (
            f"{case.shape_id}: expected NO consumer extraction (direct-store-only "
            f"recovery), but scan_for_markers surfaced it: detected={detected!r}"
        )
        # ...and in fact no marker pattern fires for this shape at all.
        assert detected == [], (
            f"{case.shape_id}: expected scan_for_markers to fire on nothing, "
            f"got detected={detected!r}"
        )
        # (b) ...yet a DIRECT store lookup still returns the byte-exact original.
        entry = store.retrieve(expected_hash)
        assert entry is not None, (
            f"{case.shape_id}: direct store.retrieve returned None — the "
            f"read_lifecycle recovery path is broken"
        )
        assert entry.original_content == original, (
            f"{case.shape_id}: direct-store recovered content is not byte-exact"
        )


def test_all_nine_shapes_present() -> None:
    """Guard: the table covers all 9 distinct marker shapes (A..I), no dupes."""
    ids = [c.shape_id[0] for c in CASES]
    assert sorted(ids) == list("ABCDEFGHI")


def test_24hex_space_separator_extracts_full_width() -> None:
    """★ Highest-value pin: a 24-hex hash with shape-A's SPACE separator must
    extract the FULL 24, never a truncated 12 → whole-blob miss.

    Shapes E/F are 24-hex hashes that use the SAME space separator shape A uses
    at 12-hex. The thing that guarantees full-width capture is the REQUIRED
    delimiter in the consumer pattern (tool_injection.py:236):
    ``<<ccr:(24|12)(?:[ ,#>]|>>)``. Because a contiguous hex run is only accepted
    when a delimiter follows, the regex cannot stop at char 12 of a 24-hex run
    (char 12 is itself hex, not a delimiter) — it backtracks to the 24-branch.

    Verified empirically: the ``24|12`` alternation ORDER is therefore NOT
    load-bearing here — a ``12|24`` reorder extracts the same full 24 for every
    separator. The real guard is the delimiter, and this test pins the behavior
    that actually protects E/F: a 24-hex + space marker resolves to the whole
    24-char hash. If a future refactor drops the trailing-delimiter requirement
    (e.g. ``<<ccr:(\\d{12})`` greedy without a delimiter), this fails loudly.
    """
    injector = CCRToolInjector()
    h24 = "abcdef0123456789abcdef01"  # 24 lowercase hex
    assert len(h24) == 24
    # Shape-A-style space separator, but a 24-hex hash (the E/F width case).
    detected = injector.scan_for_markers(
        [{"role": "user", "content": f"<<ccr:{h24} 5_bytes_duplicate>>"}]
    )
    assert detected == [h24], (
        "24-hex + space marker did not extract the whole 24-char hash "
        f"(truncation/whole-blob-miss regression). Got {detected!r}, expected [{h24!r}]."
    )
    assert len(detected[0]) == 24
