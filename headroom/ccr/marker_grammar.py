"""CCR marker grammar — the single owned definition of the wire format.

This module is the CONSUMER-side counterpart to the Rust producer family
(``crates/headroom-core/src/ccr/markers.rs``, the ``marker_for_*`` functions).
The producer owns marker CONSTRUCTION; this module owns marker RECOGNITION.
Both halves must agree byte-for-byte, and that agreement is pinned by
``tests/test_ccr_marker_grammar_characterization.py`` (producer-driven).

Before this module existed, the grammar was hand-rolled in six places across
three files with three different width contracts. Now the widths, the hex
alphabets, the ``<<ccr:`` prefix, the separator set, and the per-shape regex
fragments live here, and the production consumer + auxiliary scanners reference
them instead of re-hardcoding the contract.

Two DISTINCT hex notions — do not conflate them
===============================================
1. ``HEX_RE`` (``[a-f0-9]``) — lowercase, case-sensitive — used by the
   regex consumer (``tool_injection``). The producers emit lowercase hex, and
   the exact-width + lowercase check is part of the spoofing guard.
2. ``HEX_ALPHABET`` (``0123456789abcdefABCDEF``) — the char set the substring
   walkers (``smart_crusher._collect_ccr_hashes_from_string``,
   ``benchmarks/metrics.collect_ccr_hashes``) scan for, then ``.lower()``.
   The walkers enforce NO width at all (they keep any hex run) — this module
   does NOT impose ``HASH_WIDTHS`` on them, because that would change their
   behavior.

Two DISTINCT width contracts — also kept separate
=================================================
* ``HASH_WIDTHS = {12, 24}`` — the STRICT consumer set. The bracket-form and
  ``<<ccr:`` regexes in ``tool_injection`` accept exactly these widths; any
  other length is rejected as a spoofing guard. This is the set
  ``CCR_HASH_WIDTHS`` re-exports.
* The recovery floor ``{6,}`` in ``tests/test_ccr_recovery_invariant.py`` is a
  deliberately LOOSER lower bound for the recovery-invariant scan and is NOT
  defined here — it is intentionally distinct from the strict consumer set.

Marker shapes A..I and which producer emits each
================================================
  A  ``<<ccr:HASH N_rows_offloaded>>``        12-hex  markers.rs marker_for_rows_offloaded
  B  ``<<ccr:HASH#rows N_chunks>>``           12-hex  markers.rs marker_for_row_index
  C  ``<<ccr:HASH,KIND,SIZE>>``               12-hex  markers.rs marker_for_opaque
  D  ``<<ccr:HASH>>`` bare                    24-hex  smart_crusher.py (bare CCR helper)
  E  ``<<ccr:HASH N_bytes_duplicate>>``       24-hex  transforms/cross_message_dedup.py
  F  ``<<ccr:HASH N_bytes_near_duplicate>>``  24-hex  transforms/cross_message_dedup.py
  G  ``[N lines compressed to M. Retrieve full diff: hash=H]``  24-hex  markers.rs marker_for_diff
  H  ``[N items compressed to M. Retrieve more: hash=H]``       24-hex  markers.rs marker_for_retrieve_more
  I  ``[Read content stale: ... Retrieve original: hash=H]``    24-hex  transforms/read_lifecycle.py

Shapes A-F + D share the ``<<ccr:`` double-angle-bracket family and are matched
by ``DOUBLE_ANGLE_PATTERN``. Shapes G/H are bracket-forms matched by
``BRACKET_RETRIEVE_PATTERN`` (H) and the ``GENERIC_BRACKET_PATTERN`` fallback
(G). Shape I matches NO consumer pattern (it has no ``compressed`` token and no
``<<ccr:``) — it is recovered by DIRECT store lookup, never by the scanner.
That non-match is load-bearing; do not broaden the fallback to cover it.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Widths.
# --------------------------------------------------------------------------- #

# Accepted CCR hash widths (number of hex characters) — the STRICT consumer set:
# - 12: SmartCrusher path — sha256(payload)[:6] → 12 hex chars
#        (crusher.rs `hash_canonical`, byte-pinned by its parity tests)
# - 24: diff/log/search compressors — md5(payload)[:24] (md5_hex_24);
#        cross_message_dedup, read_lifecycle, and the store
#        default key — sha256(payload)[:24]. (No central key helper; each
#        producer owns its algorithm and threads the hash via explicit_hash.)
# Do NOT add arbitrary lengths — the exact-width check is the spoofing guard.
HASH_WIDTHS: frozenset[int] = frozenset({12, 24})

# --------------------------------------------------------------------------- #
# Hex alphabets — two distinct notions (see module docstring).
# --------------------------------------------------------------------------- #

# Regex character class for the bracket/double-angle consumer patterns.
# Lowercase, case-sensitive: the producers emit lowercase hex.
HEX_CLASS: str = "[a-f0-9]"

# Character set the substring walkers scan for (case-insensitive, lowered
# after capture). The walkers enforce no width — they keep any hex run.
HEX_ALPHABET: str = "0123456789abcdefABCDEF"


def is_valid_ccr_hash(value: object) -> bool:
    """True iff ``value`` is a syntactically valid CCR hash key: a ``str`` of
    exactly ``HASH_WIDTHS`` (12 or 24) lowercase-hex characters.

    The single width+charset spoofing guard, shared by BOTH ccr-hash ingress
    points — ``tool_injection.parse_tool_call`` (model-emitted tool calls) and
    the MCP ``headroom_retrieve`` handler — so the two cannot drift. Rejects
    ``None``, non-``str``, wrong width, and any non-hex character.
    """
    return (
        isinstance(value, str)
        and len(value) in HASH_WIDTHS
        and all(c in "0123456789abcdef" for c in value.lower())
    )


# --------------------------------------------------------------------------- #
# Literal grammar pieces.
# --------------------------------------------------------------------------- #

# The double-angle marker prefix shared by shapes A-F + D.
CCR_PREFIX: str = "<<ccr:"

# The trailing delimiter that terminates the hash capture in the double-angle
# family: a single space / comma / hash-sign / single ``>``, OR the ``>>``
# terminator of a bare ``<<ccr:HASH>>``. Non-capturing on purpose — the scan
# path extracts the LAST capture group, which must remain the hash.
DOUBLE_ANGLE_DELIM: str = r"(?:[ ,#>]|>>)"

# The literal width alternation used inside the double-angle pattern. 24 before
# 12 is fine either way (the trailing delimiter guards width), kept as the
# original literal for byte-identity.
_HASH_WIDTH_ALT: str = rf"({HEX_CLASS}{{24}}|{HEX_CLASS}{{12}})"

# --------------------------------------------------------------------------- #
# Compiled consumer patterns — built FROM the named parts above.
#
# These reproduce the original ``tool_injection._marker_patterns`` literals
# byte-for-byte (minus the retired dead pattern). Equivalence is proven in
# tests/test_ccr_marker_grammar_characterization.py against frozen copies of
# the original literals.
# --------------------------------------------------------------------------- #

# Shape H — standard bracket form: [N <type> compressed to M. Retrieve more: hash=xxx]
# Three groups (count, target, hash); the hash is the LAST group (24 hex chars).
BRACKET_RETRIEVE_PATTERN: re.Pattern = re.compile(
    rf"\[(\d+) \w+ compressed to (\d+)\. Retrieve more: hash=({HEX_CLASS}{{24}})\]"
)

# Shape G (and any other bracket marker carrying a 24-hex hash) — generic
# fallback. One group (the hash). IGNORECASE is on THIS pattern only.
GENERIC_BRACKET_PATTERN: re.Pattern = re.compile(
    rf"\[.*?compressed.*?hash=({HEX_CLASS}{{24}})\]", re.IGNORECASE
)

# Shapes A/B/C/D/E/F — the ``<<ccr:HASH<delim>...>>`` double-angle family.
# One capturing group (the hash); the trailing delimiter is non-capturing.
DOUBLE_ANGLE_PATTERN: re.Pattern = re.compile(rf"{CCR_PREFIX}{_HASH_WIDTH_ALT}{DOUBLE_ANGLE_DELIM}")


def marker_patterns() -> list[re.Pattern]:
    """The ordered consumer pattern list for ``scan_for_markers``.

    Order is preserved from the original ``_marker_patterns`` (standard
    bracket form, generic bracket fallback, double-angle family). The scan
    path runs every pattern and unions the extracted hashes, so order does
    not affect the result set — but it is kept stable for clarity and to
    match the original behavior exactly.
    """
    return [
        BRACKET_RETRIEVE_PATTERN,
        GENERIC_BRACKET_PATTERN,
        DOUBLE_ANGLE_PATTERN,
    ]
