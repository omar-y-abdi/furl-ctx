"""CCR marker grammar — the single owned definition of the wire format.

This module is the CONSUMER-side counterpart to the Rust producer family
(``crates/furl-core/src/ccr/markers.rs``, the ``marker_for_*`` functions).
The producer owns marker CONSTRUCTION; this module owns marker RECOGNITION.
Both halves must agree byte-for-byte, and that agreement is pinned by
``tests/test_ccr_marker_grammar_characterization.py`` (producer-driven).

The widths, the hex
alphabets, the ``<<ccr:`` prefix, the separator set, and the per-shape regex
fragments live here, and the production consumer + auxiliary scanners reference
them instead of re-hardcoding the contract.

Two DISTINCT hex notions — do not conflate them
===============================================
1. ``HEX_RE`` (``[a-f0-9]``) — lowercase, case-sensitive — used by the
   compiled consumer patterns below. The producers emit lowercase hex, and
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
  ``<<ccr:`` regexes below accept exactly these widths; any other length is
  rejected as a spoofing guard.
* The recovery floor ``{6,}`` in ``tests/test_ccr_recovery_invariant.py`` is a
  deliberately LOOSER lower bound for the recovery-invariant scan and is NOT
  defined here — it is intentionally distinct from the strict consumer set.

Marker shapes A..I and which producer emits each
================================================
  A  ``<<ccr:HASH N_rows_offloaded>>``        24-hex  markers.rs marker_for_rows_offloaded
  B  ``<<ccr:HASH#rows N_chunks>>``           24-hex  RETIRED — no producer since F8/#168 (parse-only, stale content)
  C  ``<<ccr:HASH,KIND,SIZE>>``               24-hex  markers.rs marker_for_opaque
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
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Final

# --------------------------------------------------------------------------- #
# Widths.
# --------------------------------------------------------------------------- #

# Accept only 24-hex current CCR hashes and 12-hex legacy SmartCrusher hashes retained for live
# transcripts. No producer emits 12-hex now; rejecting every other width is part of the spoofing guard.
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

    The single width+charset spoofing guard at the ccr-hash ingress —
    the MCP ``furl_retrieve`` handler. Rejects ``None``, non-``str``, wrong
    width, and any non-hex character.
    """
    return (
        isinstance(value, str)
        and len(value) in HASH_WIDTHS
        and all(c in "0123456789abcdef" for c in value.lower())
    )


# --------------------------------------------------------------------------- #
# Literal grammar pieces.
# --------------------------------------------------------------------------- #

# Name of the CCR retrieval tool — the consumer-side verb of this grammar. The tool NAME is wire contract exactly like the marker shapes.
CCR_TOOL_NAME: str = "furl_retrieve"

# The double-angle marker prefix shared by shapes A-F + D.
CCR_PREFIX: str = "<<ccr:"

# The trailing delimiter that terminates the hash capture in the double-angle family: a single
# space / comma / hash-sign / single ``>``, OR the ``>>`` terminator of a bare ``<<ccr:HASH>>``.
DOUBLE_ANGLE_DELIM: str = r"(?:[ ,#>]|>>)"

# The literal width alternation used inside the double-angle pattern. 24 before 12 is fine either
# way (the trailing delimiter guards width), kept as the original literal for byte-identity.
_HASH_WIDTH_ALT: str = rf"({HEX_CLASS}{{24}}|{HEX_CLASS}{{12}})"

# .

# Shape H — standard bracket form: [N <type> compressed to M. Retrieve more: hash=xxx]
# Three groups (count, target, hash); the hash is the LAST group (24 hex chars).
BRACKET_RETRIEVE_PATTERN: re.Pattern = re.compile(
    rf"\[(\d+) \w+ compressed to (\d+)\. Retrieve more: hash=({HEX_CLASS}{{24}})\]"
)

# Bracket-marker substitution must stay within one `[...]` span. Bracket-free interior classes
# prevent a match from starting at an earlier innocent bracket and deleting intervening text.
GENERIC_BRACKET_PATTERN: re.Pattern = re.compile(
    rf"\[[^\[\]]*?compressed[^\[\]]*?hash=({HEX_CLASS}{{24}})\]", re.IGNORECASE
)

# Shapes A/B/C/D/E/F — the ``<<ccr:HASH<delim>...>>`` double-angle family.
# One capturing group (the hash); the trailing delimiter is non-capturing.
DOUBLE_ANGLE_PATTERN: re.Pattern = re.compile(rf"{CCR_PREFIX}{_HASH_WIDTH_ALT}{DOUBLE_ANGLE_DELIM}")

# Use a separate full-span double-angle substitution pattern: consume through the first `>>` within a 64-character
# tail. The bound preserves 24/12-hex disambiguation and prevents quadratic backtracking on unterminated marker starts.
DOUBLE_ANGLE_FULL_PATTERN: re.Pattern = re.compile(rf"{CCR_PREFIX}{_HASH_WIDTH_ALT}[^>]{{0,64}}>>")


def marker_patterns() -> list[re.Pattern]:
    """The ordered consumer pattern list for marker scanning.

    Order is preserved from the original ``_marker_patterns`` (standard
    bracket form, generic bracket fallback, double-angle family). A scan
    runs every pattern and unions the extracted hashes (last capture group
    per match, deduped first-seen), so order does not affect the result
    set — but it is kept stable for clarity and to match the original
    behavior exactly.

    EXTRACTION only — a match's span is NOT guaranteed to cover a whole
    marker (see :data:`DOUBLE_ANGLE_PATTERN`). A caller that needs to excise
    and replace the complete marker text wants :func:`substitution_patterns`
    instead.
    """
    return [
        BRACKET_RETRIEVE_PATTERN,
        GENERIC_BRACKET_PATTERN,
        DOUBLE_ANGLE_PATTERN,
    ]


def substitution_patterns() -> list[re.Pattern]:
    """The ordered pattern list for marker SUBSTITUTION (``resolve_markers``):
    every entry's ``match.group(0)`` spans the marker's COMPLETE text — and
    nothing more — so splicing the resolved content in for that exact span
    never leaves a fragment of the marker behind (T4) and never deletes
    innocent bytes around it.

    :data:`BRACKET_RETRIEVE_PATTERN` is literal-anchored (``[`` then digits)
    so its span is exact by construction and it is reused as-is.
    :data:`GENERIC_BRACKET_PATTERN` is span-exact because its interior
    wildcards are bracket-free — see the span-safety note on the constant for
    the leftward over-match its original lazy-dot form allowed. The
    double-angle family uses :data:`DOUBLE_ANGLE_FULL_PATTERN` instead of
    :data:`DOUBLE_ANGLE_PATTERN` — see its docstring for why the
    extraction-oriented pattern is unsafe here.
    """
    return [
        BRACKET_RETRIEVE_PATTERN,
        GENERIC_BRACKET_PATTERN,
        DOUBLE_ANGLE_FULL_PATTERN,
    ]


def hash_of_match(match: re.Match[str]) -> str:
    """The hash a marker-pattern match captured — its last, always-present group."""
    idx = match.lastindex
    assert idx is not None, "marker patterns always capture at least one group"
    hash_value = match.group(idx)
    assert hash_value is not None, "the hash capture group is never optional"
    return hash_value


# Run the wildcard bracket-marker scan with RE2 when available to guarantee linear worker-thread behavior. Literal/bounded
# marker patterns remain on Python `re`; base installs fall back to `re` only for the generic bracket pattern.


def _load_re2() -> Any | None:
    """Import ``re2`` once, or ``None`` when the optional extra is absent."""
    try:
        import re2
    except Exception:  # noqa: BLE001 - absent/broken extra is a normal fallback
        return None
    return re2


_RE2: Final = _load_re2()


def _re2_twin(pattern: re.Pattern[str]) -> Any | None:
    """A linear-time RE2 twin of ``pattern``, or ``None`` when RE2 is absent or
    refuses the source. RE2 honors only inline flags, so an ``re.IGNORECASE``
    pattern is compiled from an inline ``(?i)`` form; the marker patterns carry
    no other flag.
    """
    if _RE2 is None:
        return None
    source = pattern.pattern
    if pattern.flags & re.IGNORECASE:
        source = "(?i)" + source
    try:
        return _RE2.compile(source)
    except Exception:  # noqa: BLE001 - an uncompilable twin falls back to re
        return None


# RE2 twin for the one backtracking-prone consumer pattern. The other two are
# literal-anchored and stay on the exact ``re`` engine, so no behavior changes.
_GENERIC_BRACKET_RE2: Final = _re2_twin(GENERIC_BRACKET_PATTERN)
_RE2_TWINS: Final[dict[re.Pattern[str], Any]] = (
    {GENERIC_BRACKET_PATTERN: _GENERIC_BRACKET_RE2} if _GENERIC_BRACKET_RE2 is not None else {}
)


def finditer_within_budget(pattern: re.Pattern[str], text: str) -> list[Any]:
    """Every non-overlapping match of ``pattern`` in ``text``, scanned in linear
    time via the pattern's RE2 twin when one exists, else the residual ``re``
    engine.

    Returns ``re`` or ``re2`` match objects; both expose
    ``group``/``start``/``end``/``lastindex``, which :func:`hash_of_match` and
    :func:`sub_within_budget` rely on. Total: never raises for the caller. RE2
    refuses a few inputs the ``re`` engine accepts, most notably a lone surrogate
    that has no UTF-8 encoding; those fall back to ``re`` so the scan stays total
    and the compressor's fail-open contract holds.
    """
    twin = _RE2_TWINS.get(pattern)
    if twin is not None:
        # RE2 refuses inputs re accepts (a lone surrogate has no UTF-8
        # encoding); fall back to the total re engine.
        with suppress(Exception):
            return list(twin.finditer(text))
    return list(pattern.finditer(text))


def sub_within_budget(pattern: re.Pattern[str], repl: Callable[[Any], str], text: str) -> str:
    """``pattern.sub(repl, text)`` computed over the bounded scan.

    Splices ``repl(match)`` in for each non-overlapping match, left to right,
    identical to :meth:`re.Pattern.sub` for the marker patterns, which never
    match zero width. Routing through :func:`finditer_within_budget` gives the
    substitution the same linear-time bound the extraction scan has.
    """
    parts: list[str] = []
    last = 0
    for match in finditer_within_budget(pattern, text):
        parts.append(text[last : match.start()])
        parts.append(repl(match))
        last = match.end()
    parts.append(text[last:])
    return "".join(parts)


def hashes_in_text(text: str) -> list[str]:
    """Every CCR marker hash in *text*, in first-seen order (deduped).

    Runs each :func:`marker_patterns` pattern and unions the hashes (the last
    capture group of each match), exactly as the scan contract above describes.
    The scan is bounded through :func:`finditer_within_budget`, so the generic
    bracket fallback runs on RE2's linear-time automaton when available; this
    stays total and quick even on adversarial marker-shaped input near the cap.
    """
    seen: dict[str, None] = {}
    for pattern in marker_patterns():
        for match in finditer_within_budget(pattern, text):
            seen.setdefault(hash_of_match(match), None)
    return list(seen)
