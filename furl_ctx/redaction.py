"""Env-expressible content redaction for the plugin distribution channel.

The library exposes ``CompressConfig.redactor`` — a Python callable
``raw -> redacted`` applied BEFORE compression and BEFORE the CCR store write,
so a later ``retrieve()`` only ever sees redacted bytes. But the Claude Code
plugin (PostToolUse hook + MCP server) is configured EXCLUSIVELY through
environment variables; a Python callable cannot be expressed as one. That left
the primary distribution channel with no PREVENTIVE secret scrubbing — only
reactive purge/TTL.

``FURL_REDACT_PATTERNS`` closes that gap. It is a list of stdlib-``re`` regexes
(or a path to a file of them); every match is replaced with a
``[REDACTED:<index>]`` marker before the content is compressed or stored, from
BOTH the hook and the MCP server. It composes with ``CompressConfig.redactor``
(both apply — see :func:`furl_ctx.compress.compress`).

Format (``FURL_REDACT_PATTERNS``):
  * Inline: one regex per LINE (NOT comma-separated — regexes routinely contain
    commas in ``{n,m}`` quantifiers and alternations). Blank lines and lines
    beginning with ``#`` are ignored.
  * File: a value of the form ``@/abs/path/to/patterns`` reads the regexes from
    that file (same one-per-line, ``#``-comment rules).
  * Patterns compile with ``re.MULTILINE``: ``^``/``$`` anchor at LINE
    boundaries, not only the ends of the whole blob. Tool output is
    line-oriented, and an operator writing ``^password=`` means "a line
    starting with password=" — string-only anchoring silently missed every
    mid-output line (review F4).
  * Patterns apply in list order, each ``re.sub`` running over the previous
    pattern's output — so with overlapping patterns a later one can match text
    an earlier replacement produced, nesting markers (e.g.
    ``[REDACTED:[REDACTED:2]]``). No secret bytes survive either way, and
    retrieval stays byte-exact against the stored (redacted) original; order
    the list from most- to least-specific to keep markers clean.

Marker: ``[REDACTED:<1-based-index>]`` — obvious, and a FIXED length independent
of the secret, so the redacted span never leaks the secret's length.

Resilience: an invalid regex (or an unreadable pattern file) is WARNED ABOUT
ONCE on stderr and SKIPPED; the remaining valid patterns still apply. Nothing
here ever raises to its caller — the returned redactor, once built, only runs
``re.sub`` over pre-compiled patterns.

Default: ``FURL_REDACT_PATTERNS`` unset/empty (or every pattern invalid) yields
``None`` — a true no-op with zero overhead and byte-identical behavior.

ReDoS note: patterns are the operator's own; stdlib ``re`` has no match timeout,
so a pathological pattern (nested unbounded quantifiers over adversarial input)
can backtrack expensively. Prefer anchored, bounded patterns. Know the failure
mode: in the PostToolUse hook a redactor that hangs past the hook's timeout
(30 s in the shipped hooks.json) gets the hook process KILLED, and the host's
fail-open contract then passes the ORIGINAL, UNREDACTED output through for that
call — a catastrophic pattern degrades redaction to raw passthrough; it never
blocks the tool call.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping

FURL_REDACT_PATTERNS_ENV = "FURL_REDACT_PATTERNS"

# Opt OUT of the built-in credential patterns (audit Crit-4 / B3). They are ON by
# default; only an explicit falsey value turns them off.
FURL_REDACT_BUILTINS_ENV = "FURL_REDACT_BUILTINS"

_MARKER_PREFIX = "[REDACTED:"

# High-precision built-in credential patterns, applied BY DEFAULT before any
# content is compressed or stored (audit Crit-4 / B3). Tool output routinely
# contains secrets and the store keeps originals for the TTL, so the safe default
# is to scrub the unambiguous credential shapes preventively rather than rely on
# the operator to configure ``FURL_REDACT_PATTERNS`` after the fact. Each pattern
# is deliberately SPECIFIC — a fixed vendor prefix plus a length/charset tail — so
# it does not fire on ordinary logs, code, or prose: byte-exactness for
# non-secret content is unchanged, and only a genuine credential is masked. The
# ``(name, regex)`` pairs render as ``[REDACTED:<name>]`` (fixed length per type,
# so the span never leaks the secret's length). Disable with
# ``FURL_REDACT_BUILTINS=0``; add site-specific shapes with
# ``FURL_REDACT_PATTERNS`` (both compose).
_DEFAULT_CREDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    # PEM/OpenSSH private-key block headers (the key material follows the header).
    ("private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    # AWS access-key id (AKIA/ASIA + 16 upper-alnum).
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Google API key (AIza + 35 url-safe chars).
    ("gcp-api-key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # OpenAI-style secret key (sk- + >=20 url-safe chars).
    ("openai-key", r"\bsk-[A-Za-z0-9_\-]{20,}"),
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_ + >=20 alnum).
    ("github-token", r"\bgh[posru]_[A-Za-z0-9]{20,}"),
    # Slack tokens (xoxb-/xoxa-/xoxp-/xoxr-/xoxs- + a long tail).
    ("slack-token", r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),
    # Compact JWT (three base64url segments) — bearer session tokens.
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
)

# Warn-once dedupe (per process): an invalid pattern must not spam stderr on
# every compress() call, but the operator must be told at least once.
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Emit *message* to stderr at most once per process. stderr, not logging:
    it surfaces in BOTH the hook (reaches the user) and the MCP server (reaches
    the server log) without either needing a configured logging handler."""
    if message in _warned:
        return
    _warned.add(message)
    sys.stderr.write(f"furl: {message}\n")


def redaction_marker(index: int) -> str:
    """The replacement marker for the *index*-th (1-based) pattern."""
    return f"{_MARKER_PREFIX}{index}]"


def _read_pattern_file(path: str) -> list[str] | None:
    """Read a ``@file`` of patterns (one per line). Returns ``None`` (with a
    one-time warning) when the file cannot be read — the whole spec was the
    file, so there is nothing left to redact with."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except OSError as exc:
        _warn_once(
            f"{FURL_REDACT_PATTERNS_ENV}: cannot read pattern file {path!r} ({exc}); redaction disabled"
        )
        return None


def _pattern_lines(raw: str) -> list[str]:
    """Split a spec into candidate pattern strings.

    ``@path`` -> the file's lines; otherwise the raw value's lines. Blank lines
    and ``#`` comments are dropped; each remaining line is stripped.
    """
    stripped = raw.strip()
    if stripped.startswith("@"):
        lines = _read_pattern_file(stripped[1:].strip())
        if lines is None:
            return []
    else:
        lines = raw.splitlines()
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        out.append(text)
    return out


def _compile(patterns: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Compile each pattern; skip + warn-once on any that is invalid.

    Compiled with ``re.MULTILINE`` (review F4): tool output is line-oriented,
    so ``^``/``$`` anchor per line — an operator's ``^password=\\S+`` must hit
    a mid-output line, not just the very start of the blob.

    Returns ``(marker, compiled)`` pairs. The marker's index is the pattern's
    1-based position in the ORIGINAL list, so a skipped invalid pattern does not
    renumber the survivors — an operator's ``[REDACTED:3]`` always means "the
    third line of my list", stable across a typo in line 2.
    """
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for i, pattern in enumerate(patterns, start=1):
        try:
            compiled.append((redaction_marker(i), re.compile(pattern, re.MULTILINE)))
        except re.error as exc:
            _warn_once(
                f"{FURL_REDACT_PATTERNS_ENV}: skipping invalid regex #{i} {pattern!r} ({exc})"
            )
    return compiled


def build_env_redactor(
    env: Mapping[str, str] | None = None,
) -> Callable[[str], str] | None:
    """Build a redactor from ``FURL_REDACT_PATTERNS``, or ``None`` when off.

    ``None`` is returned — a genuine no-op — when the variable is unset, empty,
    or every pattern in it is invalid. Otherwise the returned function applies
    each valid pattern's ``re.sub`` in list order, replacing matches with
    ``[REDACTED:<index>]``. The function is pure and total: it never raises.
    """
    source = os.environ if env is None else env
    raw = source.get(FURL_REDACT_PATTERNS_ENV, "")
    if not raw or not raw.strip():
        return None
    compiled = _compile(_pattern_lines(raw))
    if not compiled:
        return None

    def _redact(text: str) -> str:
        for marker, pattern in compiled:
            text = pattern.sub(marker, text)
        return text

    return _redact


def _builtins_enabled(source: Mapping[str, str]) -> bool:
    """Whether the built-in credential patterns are active. Default ON — only an
    explicit falsey ``FURL_REDACT_BUILTINS`` (``0``/``false``/``no``/``off``)
    disables them, so a fresh install redacts credentials without any config."""
    raw = source.get(FURL_REDACT_BUILTINS_ENV, "")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def build_default_redactor(
    env: Mapping[str, str] | None = None,
) -> Callable[[str], str] | None:
    """Build the ON-by-default built-in credential redactor, or ``None`` when
    disabled via ``FURL_REDACT_BUILTINS`` (audit Crit-4 / B3).

    Applies each :data:`_DEFAULT_CREDENTIAL_PATTERNS` regex in order, replacing a
    match with ``[REDACTED:<name>]``. Pure and total: it never raises (the
    patterns are constants compiled here). Returns ``None`` only when opted out,
    so the default install scrubs credentials with zero configuration.
    """
    source = os.environ if env is None else env
    if not _builtins_enabled(source):
        return None
    compiled = [
        (f"{_MARKER_PREFIX}{name}]", re.compile(pattern))
        for name, pattern in _DEFAULT_CREDENTIAL_PATTERNS
    ]

    def _redact(text: str) -> str:
        for marker, pattern in compiled:
            text = pattern.sub(marker, text)
        return text

    return _redact


def build_store_redactor(
    env: Mapping[str, str] | None = None,
) -> Callable[[str], str] | None:
    """The full pre-store redactor: built-in credentials THEN the operator's
    ``FURL_REDACT_PATTERNS`` (both apply, defense in depth), or ``None`` when
    neither is active. This is the single entry point the compress path, the
    PostToolUse hook, and the MCP server share so all three redact identically."""
    return compose_redactors(build_default_redactor(env), build_env_redactor(env))


def compose_redactors(
    first: Callable[[str], str] | None,
    second: Callable[[str], str] | None,
) -> Callable[[str], str] | None:
    """Compose two optional redactors into ``second(first(text))``.

    Either may be ``None`` (returns the other, or ``None`` when both are).
    Used by ``compress()`` to apply the env redactor BEFORE the library
    ``CompressConfig.redactor`` — both run, defense in depth.
    """
    if first is None:
        return second
    if second is None:
        return first

    def _composed(text: str) -> str:
        return second(first(text))

    return _composed


__all__ = [
    "FURL_REDACT_BUILTINS_ENV",
    "FURL_REDACT_PATTERNS_ENV",
    "build_default_redactor",
    "build_env_redactor",
    "build_store_redactor",
    "compose_redactors",
    "redaction_marker",
]
