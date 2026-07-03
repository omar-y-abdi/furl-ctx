"""Shared wire-format constants for the lossless log-template transform.

This module is the SINGLE SOURCE OF TRUTH for the byte-level grammar of the
log-template wire string.  It is imported by BOTH the encoder
(:mod:`furl_ctx.transforms.log_template`) and the independent reference decoder
(:mod:`furl_ctx.transforms.log_template_decoder`).  Sharing the constants — and
ONLY the constants — is what lets the two implementations stay byte-compatible
without the decoder depending on any encoder logic (that independence is what
makes the runtime self-check in :func:`log_template.encode_verified` a real
proof of losslessness rather than a tautology).

There is intentionally NO logic here: only literals and their rationale.  The
grammar is line-oriented, using a raw ``"\\n"`` as the record separator on the
wire.  Because a log line can itself contain newlines of any flavour
(``"\\n"``, ``"\\r\\n"``, ``"\\r"``), every wire record is escaped so that it
contains no raw wire-newline; the decoder therefore splits the wire on ``"\\n"``
*first* (always safe) and only then interprets each record.  The original line
terminator travels inside the record as an escaped field, so it is re-emitted
exactly and the wire's own ``"\\n"`` never leaks into the reconstruction.
"""

from __future__ import annotations

# --- Wire section framing ----------------------------------------------------

# The wire begins with a version tag line so wave-3b integration (and any future
# format revision) can hard-fail fast on an unknown grammar rather than silently
# mis-decoding.  Bump on any breaking grammar change.
WIRE_VERSION: str = "LT1"

# Marks the single line separating the template header block from the record
# block.  Chosen to be visually distinct and to never collide with a record
# marker (records start with a single-character marker + TAB; this is a full
# word).  It occupies its own wire line.
SECTION_SEPARATOR: str = "--RECORDS--"

# --- Record markers ----------------------------------------------------------
# Each record line starts with exactly one marker character followed by a TAB.
# Two record kinds:
#   TEMPLATED — a line that matched a mined template; carries the template id
#               and its ordered parameter list.
#   VERBATIM  — a line shipped as-is (no exploitable template, or it could not
#               round-trip through templating); carries the raw content.
# Both additionally carry the original line terminator as a trailing field.
RECORD_TEMPLATED: str = "T"
RECORD_VERBATIM: str = "V"

# Field separator WITHIN a record line (marker / id / params-blob / terminator).
# A raw TAB inside line content is escaped (see ESCAPE_MAP) so this is
# unambiguous as a structural delimiter.
FIELD_SEPARATOR: str = "\t"

# --- Parameter list ----------------------------------------------------------
# Parameters of a templated record are joined by this separator into a single
# blob.  Any literal occurrence of this byte inside a parameter value is escaped
# (see ESCAPE_MAP), so splitting the unescaped blob on it is unambiguous.
PARAM_SEPARATOR: str = "|"

# The wildcard placeholder written into a template's header text at each
# variable position.  A literal occurrence of this exact substring in otherwise
# fixed template text is escaped in the header (WILDCARD_SENTINEL below) so it
# cannot be mistaken for a real wildcard slot on decode.
WILDCARD: str = "<*>"

# --- Escaping ----------------------------------------------------------------
# Single escape byte.  Every structural byte that could otherwise be confused
# with a delimiter is backslash-escaped with a distinct two-character sequence.
# The decoder unescapes with the reverse map applied in a single left-to-right
# scan, so the escape byte itself must be listed FIRST when escaping (encoder)
# and is handled naturally by the scan when unescaping (decoder).
ESCAPE_CHAR: str = "\\"

# Order matters for ENCODING only: the escape char must be doubled before any
# other substitution, otherwise a later substitution would introduce a
# backslash that the escape-char pass would then double.  Decoding is a single
# scan and is order-independent.  These are the escape sequences (input byte ->
# 2-char sequence):
#   "\\"  -> "\\\\"   (escape char itself)
#   "\n"  -> "\\n"    (raw LF: would break wire record split)
#   "\r"  -> "\\r"    (raw CR: carried in content/terminator fields)
#   "\t"  -> "\\t"    (TAB: the record field separator)
#   "|"   -> "\\p"    (param separator)
ESCAPE_ORDER: tuple[tuple[str, str], ...] = (
    (ESCAPE_CHAR, ESCAPE_CHAR + ESCAPE_CHAR),
    ("\n", ESCAPE_CHAR + "n"),
    ("\r", ESCAPE_CHAR + "r"),
    ("\t", ESCAPE_CHAR + "t"),
    (PARAM_SEPARATOR, ESCAPE_CHAR + "p"),
)

# Reverse mapping (escape-sequence second char -> original byte) used by the
# decoder's single-pass unescape.  Derived here as a literal for independence:
# the decoder must not import encoder logic, only this constant.
#   "\\" -> "\\", "n" -> "\n", "r" -> "\r", "t" -> "\t", "p" -> "|"
UNESCAPE_MAP: dict[str, str] = {
    ESCAPE_CHAR: ESCAPE_CHAR,
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "p": PARAM_SEPARATOR,
}

# The literal ``<*>`` substring, when it appears in FIXED template text (not as a
# real variable slot), is stored in the header as this sentinel and restored on
# decode.  It is escaped structurally too (it contains no escape/sep bytes, so
# the substitution below is what protects it).  Chosen to be a byte sequence
# that cannot arise from unescaping any real parameterisation.
WILDCARD_SENTINEL: str = ESCAPE_CHAR + "w"

# --- Header line grammar -----------------------------------------------------
# Each template header line is:  <id><FIELD_SEPARATOR><escaped-template-text>
# where template text has real variable slots rendered as the literal WILDCARD
# substring, and any literal ``<*>`` from the source rendered as WILDCARD_SENTINEL,
# with all other bytes escaped via ESCAPE_ORDER.  Template ids are decimal
# integers assigned in first-appearance order (deterministic).

# Line terminator tokens as they are stored (escaped) in a record's terminator
# field.  Empty string means "no terminator" (final line, no trailing newline).
TERMINATOR_LF: str = "\n"
TERMINATOR_CRLF: str = "\r\n"
TERMINATOR_CR: str = "\r"
TERMINATOR_NONE: str = ""
