"""Independent reference decoder for the lossless log-template wire format.

This module reconstructs the ORIGINAL text from a wire string produced by
:func:`furl_ctx.transforms.log_template.encode`.  It is deliberately written as
an INDEPENDENT implementation: it imports only the shared format constants
(:mod:`furl_ctx.transforms.log_template_format`) and NOTHING from the encoder.
That independence is what makes the encoder's ``encode_verified`` self-check a
meaningful proof of losslessness — two separately-authored code paths must agree
byte-for-byte.

Totality / errors:
    * :func:`decode` is total over WELL-FORMED wire strings.
    * Any malformed wire raises :class:`LogTemplateDecodeError`, a domain error
      that carries a human-readable reason plus the offending fragment — never a
      wrong-but-silent reconstruction.  The decoder never returns partial or
      guessed output.

Wire grammar (authoritative description lives in ``log_template_format``)::

    LT1                                   version tag (own line)
    <id>\\t<escaped-template-text>         zero or more header lines
    --RECORDS--                           section separator (own line)
    <record>*                             zero or more record lines

    record :=
        T \\t <id> \\t <param|param|...> \\t <escaped-terminator>   (templated)
      | V \\t <escaped-content>          \\t <escaped-terminator>   (verbatim)

The wire is split on raw ``"\\n"`` FIRST; every field is then unescaped, so the
wire's structural newlines never leak into the output and the original line
terminators (carried per record) are re-emitted exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from furl_ctx.transforms import log_template_format as fmt


class LogTemplateDecodeError(Exception):
    """Raised when a wire string does not conform to the log-template grammar.

    Carries the failure ``reason`` and the offending ``fragment`` as data (not
    just a formatted message) so callers can inspect/branch on the cause without
    string-scraping.
    """

    def __init__(self, reason: str, fragment: str = "") -> None:
        self.reason = reason
        self.fragment = fragment
        detail = f"{reason}" if not fragment else f"{reason}: {fragment!r}"
        super().__init__(detail)


@dataclass(frozen=True)
class _ParsedTemplate:
    """A decoded header entry: id plus the template text as a token stream.

    ``segments`` is the template text split into an alternating stream of fixed
    literal chunks and wildcard markers.  ``wildcard_count`` is precomputed so a
    record's parameter count can be validated cheaply.  Reconstruction walks
    ``segments`` and substitutes the next parameter for each wildcard.
    """

    template_id: int
    segments: tuple[tuple[str, str], ...]  # (kind, value); kind in {"lit","wild"}
    wildcard_count: int


def _unescape(field: str) -> str:
    """Reverse the encoder's escaping in a single left-to-right scan.

    Recognises ``\\\\``, ``\\n``, ``\\r``, ``\\t``, ``\\p`` (via
    :data:`fmt.UNESCAPE_MAP`) and ``\\w`` (the literal-``<*>`` sentinel).  A
    backslash followed by any other byte, or a trailing lone backslash, is
    malformed and raises :class:`LogTemplateDecodeError` — the encoder never
    produces such sequences, so their presence means corrupt input.
    """
    out: list[str] = []
    i = 0
    n = len(field)
    esc = fmt.ESCAPE_CHAR
    sentinel_second = fmt.WILDCARD_SENTINEL[1]  # the char after the escape byte
    while i < n:
        ch = field[i]
        if ch != esc:
            out.append(ch)
            i += 1
            continue
        # Escape sequence: need exactly one following byte.
        if i + 1 >= n:
            raise LogTemplateDecodeError("dangling escape at end of field", field)
        nxt = field[i + 1]
        if nxt == sentinel_second:
            # Literal "<*>" that appeared in fixed template/source text.
            out.append(fmt.WILDCARD)
        elif nxt in fmt.UNESCAPE_MAP:
            out.append(fmt.UNESCAPE_MAP[nxt])
        else:
            raise LogTemplateDecodeError("unknown escape sequence", esc + nxt)
        i += 2
    return "".join(out)


def _parse_template_text(text: str) -> tuple[tuple[str, str], ...]:
    """Split escaped header template text into (literal | wildcard) segments.

    Scans for the structural ``WILDCARD`` (``<*>``) marker, which is emitted
    UN-escaped by the encoder for real variable slots.  Everything between
    markers is an escaped literal chunk that is unescaped here (so a literal
    ``<*>`` from the source — stored as the ``\\w`` sentinel — is restored to
    text and never treated as a slot).

    Returns a tuple of ``(kind, value)`` where ``kind`` is ``"lit"`` (value is
    the already-unescaped literal) or ``"wild"`` (value is empty).
    """
    segments: list[tuple[str, str]] = []
    marker = fmt.WILDCARD
    mlen = len(marker)
    i = 0
    n = len(text)
    lit_start = 0
    while i < n:
        if text.startswith(marker, i):
            if i > lit_start:
                segments.append(("lit", _unescape(text[lit_start:i])))
            segments.append(("wild", ""))
            i += mlen
            lit_start = i
        else:
            i += 1
    if lit_start < n:
        segments.append(("lit", _unescape(text[lit_start:])))
    return tuple(segments)


def _parse_header_line(line: str) -> _ParsedTemplate:
    """Parse one ``<id>\\t<template-text>`` header line.

    Raises :class:`LogTemplateDecodeError` on a missing separator or a
    non-integer id.
    """
    sep = fmt.FIELD_SEPARATOR
    idx = line.find(sep)
    if idx < 0:
        raise LogTemplateDecodeError("header line missing field separator", line)
    id_str = line[:idx]
    text = line[idx + len(sep) :]
    try:
        template_id = int(id_str)
    except ValueError as exc:
        raise LogTemplateDecodeError("header line has non-integer id", id_str) from exc
    segments = _parse_template_text(text)
    wildcard_count = sum(1 for kind, _ in segments if kind == "wild")
    return _ParsedTemplate(
        template_id=template_id,
        segments=segments,
        wildcard_count=wildcard_count,
    )


def _split_params(blob: str) -> tuple[str, ...]:
    """Split an escaped params blob on the (unescaped) param separator.

    The blob is split on the RAW separator byte; because every literal
    occurrence of that byte inside a parameter is escaped by the encoder, a raw
    separator here is always structural.  An empty blob yields the empty tuple
    (a templated record for a zero-wildcard template), NOT a single empty
    parameter.  Each field is then unescaped.
    """
    if blob == "":
        return ()
    raw_parts = blob.split(fmt.PARAM_SEPARATOR)
    return tuple(_unescape(part) for part in raw_parts)


def _render_template(parsed: _ParsedTemplate, params: tuple[str, ...]) -> str:
    """Reconstruct a templated line's content from its params.

    Substitutes params into wildcard segments in order.  Raises
    :class:`LogTemplateDecodeError` when the parameter count does not match the
    template's wildcard count — a structural inconsistency in the wire.
    """
    if len(params) != parsed.wildcard_count:
        raise LogTemplateDecodeError(
            "param count does not match template wildcard count",
            f"template {parsed.template_id}: {len(params)} != {parsed.wildcard_count}",
        )
    out: list[str] = []
    next_param = 0
    for kind, value in parsed.segments:
        if kind == "wild":
            out.append(params[next_param])
            next_param += 1
        else:
            out.append(value)
    return "".join(out)


def _decode_record(line: str, templates: dict[int, _ParsedTemplate]) -> tuple[str, str]:
    """Decode one record line into ``(content, terminator)``.

    Dispatches on the leading marker.  Raises :class:`LogTemplateDecodeError` for
    an unknown marker, a wrong field count, an unknown template id, or a bad id.
    """
    sep = fmt.FIELD_SEPARATOR
    if line == "":
        raise LogTemplateDecodeError("empty record line", line)
    marker = line[0]
    if len(line) < 2 or line[1] != sep:
        raise LogTemplateDecodeError("record marker not followed by separator", line)
    body = line[2:]

    if marker == fmt.RECORD_VERBATIM:
        # V <sep> escaped-content <sep> escaped-terminator
        fields = body.split(sep)
        if len(fields) != 2:
            raise LogTemplateDecodeError(
                "verbatim record must have exactly content and terminator fields",
                line,
            )
        content = _unescape(fields[0])
        terminator = _unescape(fields[1])
        return content, terminator

    if marker == fmt.RECORD_TEMPLATED:
        # T <sep> id <sep> params-blob <sep> escaped-terminator
        fields = body.split(sep)
        if len(fields) != 3:
            raise LogTemplateDecodeError(
                "templated record must have id, params, and terminator fields",
                line,
            )
        id_str, params_blob, term_field = fields
        try:
            template_id = int(id_str)
        except ValueError as exc:
            raise LogTemplateDecodeError("templated record has non-integer id", id_str) from exc
        parsed = templates.get(template_id)
        if parsed is None:
            raise LogTemplateDecodeError("templated record references unknown template id", id_str)
        params = _split_params(params_blob)
        content = _render_template(parsed, params)
        terminator = _unescape(term_field)
        return content, terminator

    raise LogTemplateDecodeError("unknown record marker", marker)


def decode(wire: str) -> str:
    """Reconstruct the original text from a log-template ``wire`` string.

    Total over well-formed wire; raises :class:`LogTemplateDecodeError` on any
    malformation (bad version tag, missing section separator, malformed header or
    record, unknown template id, bad escape).  The reconstruction is byte-exact:
    each record contributes ``content + terminator`` and the pieces are
    concatenated in order, so the wire's own structural newlines never appear in
    the result.
    """
    # Split on the wire's structural newline FIRST — always safe because every
    # field is escaped to contain no raw wire-newline.
    lines = wire.split("\n")

    # Version tag.
    if not lines or lines[0] != fmt.WIRE_VERSION:
        got = lines[0] if lines else ""
        raise LogTemplateDecodeError("missing or unknown wire version tag", got)

    # Locate the section separator that divides headers from records.
    try:
        sep_index = lines.index(fmt.SECTION_SEPARATOR, 1)
    except ValueError as exc:
        raise LogTemplateDecodeError(
            "missing record-section separator", fmt.SECTION_SEPARATOR
        ) from exc

    header_lines = lines[1:sep_index]
    record_lines = lines[sep_index + 1 :]

    templates: dict[int, _ParsedTemplate] = {}
    for header in header_lines:
        parsed = _parse_header_line(header)
        if parsed.template_id in templates:
            raise LogTemplateDecodeError("duplicate template id in header", str(parsed.template_id))
        templates[parsed.template_id] = parsed

    out: list[str] = []
    for record in record_lines:
        content, terminator = _decode_record(record, templates)
        out.append(content)
        out.append(terminator)
    return "".join(out)


__all__ = [
    "LogTemplateDecodeError",
    "decode",
]
