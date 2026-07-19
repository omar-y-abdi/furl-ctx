"""CCR marker grammar — the single owned definition of the wire format.

This module is the CONSUMER-side counterpart to the Rust producer family
(``crates/furl-core/src/ccr/markers.rs``, the ``marker_for_*`` functions).
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
from collections.abc import Callable
from typing import Any, Final

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

    The single width+charset spoofing guard at the ccr-hash ingress —
    the MCP ``furl_retrieve`` handler (the proxy-side
    ``tool_injection.parse_tool_call`` twin was excised with its module,
    SIMP-4). Rejects ``None``, non-``str``, wrong width, and any non-hex
    character.
    """
    return (
        isinstance(value, str)
        and len(value) in HASH_WIDTHS
        and all(c in "0123456789abcdef" for c in value.lower())
    )


# --------------------------------------------------------------------------- #
# Literal grammar pieces.
# --------------------------------------------------------------------------- #

# Name of the CCR retrieval tool — the consumer-side verb of this grammar.
# The MCP server registers it (hosts alias it as
# ``mcp__<server>__furl_retrieve``), and the router's retrieval-loop guard
# (router_message_policy.ALWAYS_EXCLUDE_TOOLS) excludes its outputs from
# re-compression. Re-homed here from the excised ``tool_injection`` module
# (SIMP-4): the tool NAME is wire contract exactly like the marker shapes.
CCR_TOOL_NAME: str = "furl_retrieve"

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
# byte-for-byte (minus the retired dead pattern; the injector module itself
# was excised — SIMP-4 — so this module is now the sole owner of the
# consumer patterns). Equivalence is proven in
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

# T4 fix: a SEPARATE full-span variant of the double-angle family, for callers
# that excise-and-replace the WHOLE marker (resolve_markers's substitution)
# rather than merely extract the hash. DOUBLE_ANGLE_PATTERN above is an
# EXTRACTION pattern: its trailing delimiter only needs to consume ONE
# boundary byte to confirm the hash capture is complete, so match.group(0)
# stops right there — correct for hashes_in_text (which only reads the
# capture group), WRONG as a substitution span for any shape with a
# descriptive tail (A/B/C/E/F: match.group(0) ends mid-marker, e.g.
# ``"<<ccr:HASH "`` for shape A, leaving ``"N_rows_offloaded>>"`` glued onto
# whatever replaces it) and even for the bare shape (D: DOUBLE_ANGLE_DELIM's
# character class matches a single ``">"`` before its own ``">>"``
# alternative ever gets a chance to fire — alternation takes the first
# successful branch — so match.group(0) stops one byte short of the real
# close and a lone ``">"`` is left dangling).
#
# ``[^>]{0,64}`` up to a literal ``">>"`` instead: through every byte the
# double-angle grammar never emits inside a marker body, so it halts at the
# FIRST ``">>"`` within that window — the marker's real close for every real
# shape (verified against A-F; none of their tails contain ``">"``).
# Zero-width for the bare shape (D), where the delimiter IS the terminator.
# Hash-width disambiguation (24 tried before 12) still holds: the byte
# immediately after a real hash is always non-hex (space / comma /
# hash-sign / ``">"``), so the 24-hex branch still fails to find 24
# consecutive hex characters for a true 12-hex hash and correctly backs
# off, exactly as it does for DOUBLE_ANGLE_PATTERN.
#
# The ``{0,64}`` bound (not an unbounded ``*``) closes a ReDoS the
# unbounded form reopened: on adversarial input with many ``<<ccr:HASH``
# starts and no closing ``">>"`` anywhere (e.g. ``"<<ccr:aaaaaaaaaaaa" *
# 32000``, 562.5 KB), an unbounded ``[^>]*`` must scan to the end of the
# text, fail to find ``">>"``, then backtrack one byte at a time all the
# way back — an O(remaining-length) cost repeated at every one of the many
# match-start attempts, so the whole scan is O(text_length^2). Measured:
# 562.5 KB took 19.66s unbounded versus 0.0095s bounded here, and bounded
# scales linearly (roughly 2x time per 2x input) where unbounded scaled
# roughly 4x. ``finditer_within_budget`` gives this pattern no RE2 twin
# (only ``GENERIC_BRACKET_PATTERN`` has one, see below), so it stays on
# Python's backtracking ``re`` engine — the bound is the only thing
# keeping the worst-case backtrack cost at O(64) per position instead of
# O(text_length) per position.
#
# 64 is chosen with wide headroom over the measured maximum real tail
# across every shape (arithmetic, `` `` = one literal space):
#   A ``<<ccr:HASH {n}_rows_offloaded>>``       16 literal chars + digits
#   B ``<<ccr:HASH#rows {n}_chunks>>``          13 literal chars + digits,
#     n hard-capped at store.capacity()/4 = 250 (3 digits) —
#     crates/furl-core/src/ccr/mod.rs DEFAULT_CAPACITY=1000,
#     .../smart_crusher/persist.rs GRANULAR_CHUNK_CAPACITY_DIVISOR=4
#   C ``<<ccr:HASH,{kind},{size}>>``            2 literal commas + kind
#     [4-6 chars, kind is one of "base64"/"string"/"html" in every
#     production call site — OpaqueKind::Other's only construction site
#     in the whole crate is a #[cfg(test)] fixture in compaction/ir.rs] +
#     humanize_bytes() output [a handful of chars at any realistic size]
#   D ``<<ccr:HASH>>`` bare                     0 chars, delimiter IS the close
#   E ``<<ccr:HASH {n}_bytes_duplicate>>``      17 literal chars + digits
#   F ``<<ccr:HASH {n}_bytes_near_duplicate>>`` 22 literal chars + digits
# A/E/F have no in-repo hard cap on the digit run, but digit count grows
# only as log10(n): even a wildly pessimistic 20-digit byte/row count (the
# ~64-bit address-space ceiling, far past any real message size) keeps
# every shape's total under 42 chars, comfortably inside the 64-char
# window with room to spare.
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
    every entry's ``match.group(0)`` spans the marker's COMPLETE text, so
    splicing the resolved content in for that exact span never leaves a
    fragment of the marker behind (T4).

    :data:`BRACKET_RETRIEVE_PATTERN` and :data:`GENERIC_BRACKET_PATTERN`
    already span their whole marker (opening ``"["`` to closing ``"]"``) and
    are reused as-is. The double-angle family uses
    :data:`DOUBLE_ANGLE_FULL_PATTERN` instead of :data:`DOUBLE_ANGLE_PATTERN`
    — see its docstring for why the extraction-oriented pattern is unsafe
    here.
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


# --------------------------------------------------------------------------- #
# Bounded marker scanning (marker-scan DoS).
#
# ``GENERIC_BRACKET_PATTERN`` carries two lazy ``.*?`` wildcards and runs over
# agent/tool-produced text up to the MCP server's 10 MiB read cap. Under
# CPython's backtracking ``re`` engine that scan is quadratic-or-worse on
# adversarial input: many ``[`` starts, each forcing a long forward scan for
# ``compressed`` then ``hash=``. On the MCP worker thread no SIGALRM watchdog
# can fire, so a wedged scan is a process-wide freeze, the same DoS class
# ``regex_budget`` closed for agent-supplied filters.
#
# The bound is RE2 (``google-re2``, shipped by the ``mcp`` extra): it matches
# with an automaton in LINEAR time, so the pathological class does not exist for
# it, and it is the one engine that holds on a worker thread. RE2 has no flags
# argument, so an ``re.IGNORECASE`` pattern is compiled from an inline ``(?i)``
# form. Extraction parity with the ``re`` engine, the same hashes in the same
# order, is pinned by ``tests/test_marker_scan_budget.py`` over the
# characterization corpus.
#
# Only ``GENERIC_BRACKET_PATTERN`` needs the automaton. ``BRACKET_RETRIEVE_PATTERN``
# and ``DOUBLE_ANGLE_PATTERN`` are literal-anchored and linear under ``re``, so
# they keep the exact ``re`` engine and are intentionally NOT twinned. When RE2
# is absent, a base install without the ``re2``/``mcp`` extra, the scan falls back
# to the residual ``re`` engine: Ctrl-C-interruptible on the main thread, the same
# residual tier ``regex_budget`` documents. A supported MCP deployment always
# ships RE2, closing the worker-thread freeze in production.
# --------------------------------------------------------------------------- #


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
        try:
            return list(twin.finditer(text))
        except Exception:  # noqa: BLE001 - RE2 refuses inputs re accepts (a lone
            # surrogate has no UTF-8 encoding); fall back to the total re engine.
            pass
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
