"""Lossless Drain-style log-template encoder.

Public surface:
    * :class:`LogTemplateEncoding` — the wire string plus mining statistics.
    * :func:`encode` — mine + serialise; ``None`` when there is no exploitable
      template structure or the encoding would not be smaller than the input.
    * :func:`encode_verified` — :func:`encode` followed by an immediate decode
      and byte-exact comparison; returns ``None`` on any mismatch so an
      unverified (possibly lossy) encoding can NEVER ship.

Losslessness strategy (the three-part spine):

1. **Reversible line split.**  :func:`content_and_terminators` splits the input
   into ``(content, terminator)`` pairs where ``terminator`` is one of
   ``"\\n" | "\\r\\n" | "\\r" | ""`` and
   ``"".join(c + t for c, t in pairs) == text`` byte-exactly.  All of CRLF, CR,
   and trailing-/no-trailing-newline are handled by this ONE function; every
   downstream record simply carries its own terminator.

2. **Wire newlines are distinct from source newlines.**  The wire uses a raw
   ``"\\n"`` as its record separator, but every record is escaped so it holds no
   raw wire-newline.  The decoder splits the wire on ``"\\n"`` first, then
   re-emits each record's ORIGINAL terminator from its escaped field, so the
   wire's structural ``"\\n"`` never reaches the reconstruction.

3. **Concatenation-preserving tokenisation.**  :func:`tokenize` uses
   ``re.findall(r"\\s+|\\S+", content)`` so ``"".join(tokens) == content``.  No
   whitespace normalisation: whitespace runs are ordinary tokens.  Fixed
   positions are byte-identical across a cluster; variable positions carry the
   exact source token as a parameter.

Any line that cannot be represented losslessly as ``(template, params)`` — or
that would collide with the wire grammar in a way templating cannot express — is
shipped VERBATIM.  Correctness therefore never depends on the mining heuristics;
they only influence how much the output shrinks.

Determinism: pure function of the input string.  No randomness, wall-clock, or
I/O.  Template ids are first-appearance order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from furl_ctx.transforms import log_template_format as fmt
from furl_ctx.transforms.log_template_decoder import LogTemplateDecodeError, decode
from furl_ctx.transforms.log_template_miner import (
    MatchedLine,
    Template,
    is_wildcard,
    mine,
)

# --- Constants ---------------------------------------------------------------

# A template must be backed by at least this many source lines for the transform
# to bother emitting the header + records at all.  Below this, the header cost
# outweighs any saving and the input has "no exploitable template structure"
# (spec).  Three is the smallest count where a shared template can plausibly pay
# for itself (one header line amortised over >=3 record lines).
MIN_TEMPLATE_SUPPORT: int = 3

# Tokeniser: alternating whitespace runs and non-whitespace runs.  This is the
# concatenation-preserving split that makes templating lossless — ``"".join`` of
# the result is the exact input.
_TOKEN_RE = re.compile(r"\s+|\S+")


@dataclass(frozen=True)
class LogTemplateEncoding:
    """A successful encoding: the wire string plus descriptive statistics.

    ``wire`` is the complete, self-describing serialisation understood by
    :func:`furl_ctx.transforms.log_template_decoder.decode`.  The stats are
    informational (for the router / diagnostics in wave 3b) and are NOT required
    to decode.
    """

    wire: str
    template_count: int
    templated_lines: int
    verbatim_lines: int


# --- The reversible line splitter (spine part 1) -----------------------------


def content_and_terminators(text: str) -> tuple[tuple[str, str], ...]:
    """Split ``text`` into ``(content, terminator)`` pairs, fully reversible.

    ``terminator`` is one of ``"\\r\\n"``, ``"\\n"``, ``"\\r"``, or ``""``.  The
    final pair has an empty terminator iff ``text`` does not end with a newline.
    Invariant (the whole point):

        ``"".join(c + t for c, t in content_and_terminators(text)) == text``

    An empty input yields a single ``("", "")`` pair, so the empty string
    round-trips as one terminator-less empty line.  A lone ``"\\r"`` not followed
    by ``"\\n"`` is treated as a CR line ending (old-Mac), and ``"\\r\\n"`` as a
    single CRLF ending — never two line breaks.
    """
    pairs: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            pairs.append(("".join(buf), fmt.TERMINATOR_LF))
            buf = []
            i += 1
        elif ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                pairs.append(("".join(buf), fmt.TERMINATOR_CRLF))
                buf = []
                i += 2
            else:
                pairs.append(("".join(buf), fmt.TERMINATOR_CR))
                buf = []
                i += 1
        else:
            buf.append(ch)
            i += 1
    # Trailing content with no terminator (also covers empty input -> [("","")]).
    # Only when there IS trailing content (or nothing at all): text ending in a
    # terminator must NOT grow a phantom empty line — the docstring's "empty
    # terminator iff text does not end with a newline" invariant, and the stats
    # (templated_lines/verbatim_lines) count real lines only.
    if buf or not pairs:
        pairs.append(("".join(buf), fmt.TERMINATOR_NONE))
    return tuple(pairs)


def tokenize(content: str) -> tuple[str, ...]:
    """Concatenation-preserving tokenisation: ``"".join(tokenize(c)) == c``.

    Empty content tokenises to the empty tuple (its ``"".join`` is ``""``).
    """
    return tuple(_TOKEN_RE.findall(content))


# --- Escaping (spine part 2 support) -----------------------------------------


def _escape(raw: str) -> str:
    """Escape a raw string for safe inclusion in a wire field.

    The escape byte is doubled FIRST (see ``ESCAPE_ORDER`` rationale) so later
    substitutions cannot introduce an un-doubled escape byte.  The result
    contains no raw wire-newline, TAB, or param separator, so it is safe between
    any two structural delimiters.
    """
    out = raw
    for target, replacement in fmt.ESCAPE_ORDER:
        out = out.replace(target, replacement)
    return out


def _encode_terminator(terminator: str) -> str:
    """Encode a line terminator into its escaped wire field (may be empty)."""
    return _escape(terminator)


# --- Header rendering (template text with wildcards) -------------------------


def _render_template_text(template: Template) -> str:
    """Render a template's header text: fixed tokens escaped, slots as ``<*>``.

    A literal ``<*>`` occurring in a FIXED token is first replaced by the
    wildcard sentinel so it cannot be mistaken for a real slot on decode; then
    the normal escape pass runs.  Real variable positions emit the literal
    ``WILDCARD`` marker (``<*>``) directly — it is structural, not data, so it is
    NOT escaped.
    """
    parts: list[str] = []
    for tok in template.tokens:
        if is_wildcard(tok):
            parts.append(fmt.WILDCARD)
        else:
            fixed = str(tok)  # miner stores str at fixed positions
            # Escape FIRST, then substitute the sentinel: the sentinel's own
            # backslash must reach the wire raw. The reverse order double-
            # escaped it ("\\w"), which the decoder faithfully unescaped back
            # to a literal "\w" instead of "<*>" — caught by encode_verified.
            # Safe: "<*>" carries no escapable bytes, so it survives _escape
            # intact, and no escape output can fabricate a false "<*>".
            parts.append(_escape(fixed).replace(fmt.WILDCARD, fmt.WILDCARD_SENTINEL))
    return "".join(parts)


def _header_line(template: Template) -> str:
    """One header line: ``<id><FIELD_SEPARATOR><escaped-template-text>``."""
    return f"{template.template_id}{fmt.FIELD_SEPARATOR}{_render_template_text(template)}"


# --- Record rendering --------------------------------------------------------


def _templated_record(match: MatchedLine, terminator: str) -> str:
    """A templated record line: ``T<sep>id<sep>param|param|...<sep>term``."""
    params_blob = fmt.PARAM_SEPARATOR.join(_escape(p) for p in match.params)
    return fmt.FIELD_SEPARATOR.join(
        (
            fmt.RECORD_TEMPLATED,
            str(match.template_id),
            params_blob,
            _encode_terminator(terminator),
        )
    )


def _verbatim_record(content: str, terminator: str) -> str:
    """A verbatim record line: ``V<sep>escaped-content<sep>term``."""
    return fmt.FIELD_SEPARATOR.join(
        (
            fmt.RECORD_VERBATIM,
            _escape(content),
            _encode_terminator(terminator),
        )
    )


# --- Encoding orchestration --------------------------------------------------


def _template_support(matches: tuple[MatchedLine, ...]) -> dict[int, int]:
    """Count how many source lines resolved to each template id (deterministic)."""
    support: dict[int, int] = {}
    for m in matches:
        support[m.template_id] = support.get(m.template_id, 0) + 1
    return support


def encode(text: str) -> LogTemplateEncoding | None:
    """Encode ``text`` losslessly using mined templates, or return ``None``.

    Returns ``None`` when the input has no exploitable template structure — no
    single template is backed by at least ``MIN_TEMPLATE_SUPPORT`` lines — or
    when the resulting wire is not strictly smaller than the input (measured in
    code points).  Otherwise returns a :class:`LogTemplateEncoding` whose
    ``wire`` decodes byte-exactly back to ``text``.

    This function guarantees losslessness structurally (fixed tokens are
    byte-identical across a cluster; variable tokens are stored verbatim; lines
    that cannot be templated ship verbatim), but callers that must be certain the
    wire round-trips should prefer :func:`encode_verified`.
    """
    pairs = content_and_terminators(text)
    tokenised = tuple(tokenize(content) for content, _ in pairs)
    result = mine(tokenised)

    support = _template_support(result.matches)
    # A template is "worth using" only if enough lines back it.
    usable_ids = {tid for tid, count in support.items() if count >= MIN_TEMPLATE_SUPPORT}
    if not usable_ids:
        return None

    templates_by_id = result.template_by_id()

    # Emit header lines only for usable templates, in ascending id order for
    # determinism and reader-friendliness.
    header_lines = [_header_line(templates_by_id[tid]) for tid in sorted(usable_ids)]

    # Emit records in original order.  A line uses its template only when that
    # template is usable AND the template actually reproduces the line (guarded
    # by re-render equality — cheap insurance against any pattern/param drift);
    # otherwise it ships verbatim.
    record_lines: list[str] = []
    templated_count = 0
    verbatim_count = 0
    for (content, terminator), match in zip(pairs, result.matches):
        template = templates_by_id[match.template_id]
        if match.template_id in usable_ids and _renders_exact(template, match, content):
            record_lines.append(_templated_record(match, terminator))
            templated_count += 1
        else:
            record_lines.append(_verbatim_record(content, terminator))
            verbatim_count += 1

    wire = _assemble_wire(header_lines, record_lines)

    # Size gate: only claim a win if the wire is strictly smaller than the input
    # (code points).  Reported unit: len() over the Python str (code points).
    if len(wire) >= len(text):
        return None

    return LogTemplateEncoding(
        wire=wire,
        template_count=len(usable_ids),
        templated_lines=templated_count,
        verbatim_lines=verbatim_count,
    )


def _renders_exact(template: Template, match: MatchedLine, content: str) -> bool:
    """True iff the template + params rebuild ``content`` byte-exactly.

    Defensive equality check: mining guarantees this by construction, but the
    encoder verifies it per line so a templated record is only ever emitted when
    it is provably reversible.  Any mismatch falls back to a verbatim record.
    """
    if match.template_id != template.template_id:
        return False
    try:
        return template.render_with_params(match.params) == content
    except ValueError:
        return False


def _assemble_wire(header_lines: list[str], record_lines: list[str]) -> str:
    """Join version tag, header block, separator, and records into the wire.

    Layout (one wire ``"\\n"`` between lines; none is added inside a line because
    records/headers are escaped)::

        LT1
        <header line>*
        --RECORDS--
        <record line>*
    """
    lines = [fmt.WIRE_VERSION, *header_lines, fmt.SECTION_SEPARATOR, *record_lines]
    return "\n".join(lines)


def encode_verified(text: str) -> LogTemplateEncoding | None:
    """Encode, then decode and byte-compare; ``None`` on any mismatch or error.

    This is the safe entry point: it NEVER returns an encoding that does not
    reconstruct ``text`` exactly.  If :func:`encode` returns ``None`` (no
    exploitable structure / no size win), so does this.  If the produced wire
    fails to decode back to ``text`` for any reason — including a
    :class:`LogTemplateDecodeError` from malformed output, which would indicate
    an encoder bug — this returns ``None`` rather than shipping unverified.
    """
    encoding = encode(text)
    if encoding is None:
        return None
    try:
        restored = decode(encoding.wire)
    except LogTemplateDecodeError:
        return None
    if restored != text:
        return None
    return encoding


__all__ = [
    "LogTemplateEncoding",
    "MIN_TEMPLATE_SUPPORT",
    "content_and_terminators",
    "tokenize",
    "encode",
    "encode_verified",
]
