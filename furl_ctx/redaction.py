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

ReDoS note. This used to say "patterns are the operator's own", which framed the
failure mode as something an operator opts into. That was FALSE, and it was the
sentence that made the risk look accepted. The built-in ``jwt`` pattern below is
QUADRATIC on adversarial input and it is ON BY DEFAULT, so a stock install with
zero configuration carries it.

The mechanism, measured by operation count rather than wall clock (an exponent of
exactly 2.00 in characters scanned vs input size): ``-`` is simultaneously a
NON-WORD character, so it creates the ``\\b`` anchor, and a member of the value
class ``[A-Za-z0-9_\\-]``, so the greedy run spans straight across it. Input that
is dense in ``-eyJ`` over an unbroken url-safe-base64 run with no ``.`` therefore
gives O(N) match attempts each scanning an O(N) tail. Realistic content (logs,
code, JSON, hex, natural base64) scans ZERO characters through this pattern and
redaction over it is linear — the cost is adversarial-only, not general.

Why that is a SECURITY bug and not a performance one. In the PostToolUse hook a
redactor that hangs past the hook's timeout (30 s in the shipped hooks.json) gets
the hook process KILLED, and the host's fail-open contract then passes the
ORIGINAL, UNREDACTED output through for that call. Tool output is attacker-
reachable (a repository file, a command's stdout, a fetched page), so a wedge is
a secret-DISCLOSURE path, not a slow-response path.

:func:`redact_within_budget` closes that on the hook path by failing CLOSED: the
redaction runs under a SIGALRM budget and, on overrun, the caller receives
``None`` and withholds the output instead of passing the original through. This
mirrors the choice ``mcp_server._refuse_regex_filters`` already made for
agent-supplied filters: when a match cannot be bounded safely, refuse visibly
rather than run it unbounded.

PRECISE STATEMENT OF THAT GUARANTEE, because an earlier revision of this
paragraph overstated it. The claim is NOT "the unredacted text can never escape
this module by any means"; it is narrower and checkable: NO RETURN PATH of
:func:`redact_within_budget` yields the unredacted input, and the function
resolves to exactly two outcomes — the fully-redacted string, or ``None``.
:exc:`RedactionBudgetExceeded` used to be a THIRD outcome: a SIGALRM delivered
after the guarded call returned but before the handler was restored escaped as an
exception, and a caller's generic ``except Exception`` then returned the ORIGINAL
text. That is closed at three layers now, and the third one is what makes the
other two more than an enumeration of bytecodes that happened to be considered:

  1. the handler DISARMS ITSELF before raising (:func:`_make_budget_handler`), so
     no unwinding route can leave it installed, including routes nobody listed;
  2. :func:`_restore_after_budget` tolerates a late alarm AND the ``OSError``
     CPython raises when it discards one, retrying rather than propagating either;
  3. an outermost handler converts any residual escape to the fail-closed ``None``.

Layers 1 and 2 are both there because enumeration was measured insufficient: with
only 2 and 3 in place, an aimed race harness still found the raising handler
installed after 6 of 16000 calls that returned normally, and the ``OSError`` route
bypassed every ``except`` in the module because it shares no base with
:exc:`RedactionBudgetExceeded`. The exception is EXPORTED so callers can catch it
explicitly rather than folding it into a fail-open catch-all. The completed value
is identified by POSITIVE PROOF that the pass finished, never by absence of an
exception — see :func:`redact_within_budget`.

PLATFORM COVERAGE OF THAT FIX — know this before relying on it. PROTECTED: macOS,
Linux, and Windows under WSL. NOT PROTECTED: native Windows, which has no
``SIGALRM`` at all, so the budget never arms, the withhold path is unreachable,
and the fail-open disclosure above persists there exactly as it did before the
budget existed. That is a disclosed, deliberately un-closed gap, not an oversight:
the fallback runs the redactor unbudgeted because failing closed on a platform
that never had a budget would withhold ALL tool output, and a thread-based
watchdog cannot substitute (the GIL makes the timeout unobservable — measured in
``furl_ctx.ccr.regex_budget``). It is not SILENT, though: a one-time stderr
warning names the cause on any host or call where the budget cannot arm.

Still open, tracked separately, deliberately NOT addressed here:

* The MCP server runs the same redactor in a worker thread (``run_in_executor``),
  where a SIGALRM watchdog can never arm — CPython holds the GIL for the whole
  match, so a wedge there starves the entire event loop for every session. That
  needs a different mechanism.
* ``furl_ctx.ccr.regex_budget._search_with_watchdog`` is STRUCTURALLY IDENTICAL to
  the pre-fix version of the restore here and inherits the same late-alarm defect
  verbatim: a SIGALRM delivered between its guarded ``search`` returning and its
  ``finally`` restoring the handler escapes as ``_BudgetExceeded``, past a
  docstring promising "every entry point returns a verdict and NEVER raises to the
  caller", and leaves its handler installed. It is NOT fixed here on purpose —
  different subsystem, different callers, its own tests and its own red-proof —
  and it is filed rather than left silent. The remediation is the one below:
  positive-proof capture plus a restore that tolerates a late alarm.

Operator patterns supplied via ``FURL_REDACT_PATTERNS`` are arbitrary and
unscreened; prefer anchored, bounded patterns, and note that the shape to avoid is
an unbounded greedy class followed by a REQUIRED literal, with a
boundary-triggering character inside the class.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

FURL_REDACT_PATTERNS_ENV = "FURL_REDACT_PATTERNS"

# Wall-clock budget for ONE redaction pass, and the env var that tunes it.
FURL_REDACT_BUDGET_ENV = "FURL_REDACT_BUDGET_SECONDS"

# Derivation (do NOT retune this blind — the numbers below are what it is for).
#
# LEGITIMATE COST. A full built-in redaction pass over REALISTIC content (logs,
# code, JSON, hex, natural base64) is LINEAR, min-of-N over one tight
# back-to-back sweep on a box at load average 17.63 — a CONTENDED box, so the
# rate is conservative:
#
#     8 MiB 0.774 s | 16 MiB 1.621 s | 32 MiB 3.121 s | 64 MiB 6.157 s
#     => ~0.096 s/MiB, exponent 1.00.
#
# That rate is machine- and load-dependent (re-measured at 0.116 s/MiB on the
# same box at load 28, and 0.235 s/MiB at load 36), so the RATIO below, not the
# absolute second count, is what makes this number portable.
#
# ADVERSARIAL COST, corrected. An earlier revision of this comment cited 0.101 s
# at 64 KB and a 5 s crossing at ~0.5 MB. Those figures were measured on a
# SPARSELY seeded corpus (one ``-eyJ`` per ~124 characters) and were about 25x
# too low, which mattered because this comment tells the next person not to
# retune blind and then handed them wrong data. Cost scales with SEEDING DENSITY
# (number of boundary-anchored ``eyJ`` starts) as well as input size. Measured at
# 64 KB, same sweep, against a benign anchor of 0.116 s/MiB taken alongside:
#
#     gap=120  0.100 s (0.9x)   gap=30  0.407 s (3.5x)   gap=4  1.974 s (17.1x)
#     gap=60   0.194 s (1.7x)   gap=12  1.253 s (10.8x)  gap=1  3.188 s (27.6x)
#
# At the worst density the quadratic is unmistakable: 16 KB 0.165 s, 32 KB
# 0.590 s, 64 KB 2.635 s, 128 KB 10.656 s — exponent 2.02, i.e. 4x the cost for
# 2x the data. The 5.0 s budget is therefore crossed at roughly 70-90 KB of
# worst-case input, NOT at 0.5 MB.
#
# WHY 5.0 s IS STILL RIGHT, and it is a stronger argument than before because the
# separation is WIDER, not narrower. Legitimate content: 5.0 s covers ~52 MiB at
# the 0.096 s/MiB rate (~21 MiB at the pessimistic 0.235 s/MiB), and the hook's
# own ``FURL_HOOK_MAX_COMPRESS_BYTES`` reroute threshold defaults to 5 MB, whose
# expected cost is ~0.5-1.2 s. Adversarial content trips at ~70-90 KB. So the
# budget sits between "a 5 MB legitimate blob costs about a fifth of it" and "a
# 90 KB attack exhausts it" — two orders of magnitude of separation, with the
# budget nowhere near either boundary.
#
# UPPER BOUND, and why the ceiling below exists. The shipped hooks.json
# PostToolUse timeout is 30 s. The redactor runs up to THREE times per hook
# invocation, not twice: the extracted field and a Bash-shaped preserved stderr
# (both budgeted here), plus a THIRD pass inside ``compress()`` at
# ``compress.py:613``, which is NOT budgeted. That third pass is bounded in
# practice only because content that would blow up there was already withheld
# here — it re-scans the same bytes, and redaction does not remove the ``-eyJ``
# runs that make them expensive, so it costs about the same as pass one. Worst
# case is therefore ~3x the budget, which is why the budget is CLAMPED below:
# 3 x 9.0 s leaves a little under 30 s, and 3 x 5.0 s leaves >=15 s of headroom
# for compression and the emit.
DEFAULT_REDACT_BUDGET_SECONDS: Final = 5.0

# Hard ceiling on an operator-supplied budget. Without it, the parser accepted
# any large float while the real safe ceiling is ~9 s (see the 3x argument
# above): an operator setting 12 or 15 s would believe they were comfortably
# under the 30 s host timeout and would silently re-open the fail-open this
# module exists to close. That is the same "a typo must not mean no budget"
# class already guarded at zero and negative, left open at the other end.
MAX_REDACT_BUDGET_SECONDS: Final = 9.0

# Smallest interval a restored caller timer may be re-armed with: ``setitimer(0.0)``
# DISARMS, so a caller deadline that elapsed during our pass must be re-armed just
# above zero to fire immediately rather than never. Mirrors
# ``furl_ctx.ccr.regex_budget._MIN_RESTORED_TIMER`` (review B7).
_MIN_RESTORED_TIMER: Final = 1e-6

# Attribute stamped on each per-call SIGALRM handler so "is this one of ours?" stays
# answerable now that the handler is built per call rather than shared at module level.
_WATCHDOG_MARKER: Final = "_furl_redaction_watchdog"

# Bound on restore attempts. TWO is the most the signal semantics can require: the
# itimer is one-shot and every attempt disarms it first, so at most one already-pending
# alarm can interrupt, and it is consumed by doing so. The slack is there so that a
# repeating fault becomes a WARNING rather than a hang -- an unbounded ``while True``
# would spin forever on any error that does not clear, which is a worse failure than
# giving up loudly inside a security control.
_RESTORE_ATTEMPTS: Final = 8

# Opt OUT of the built-in credential patterns (audit Crit-4 / B3). They are ON by
# default; only an explicit falsey value turns them off.
FURL_REDACT_BUILTINS_ENV = "FURL_REDACT_BUILTINS"

_MARKER_PREFIX = "[REDACTED:"

# ── PEM/OpenSSH private-key blocks ──────────────────────────────────────────
#
# The WHOLE block goes — armor AND the base64 key material between it. Matching
# only the ``-----BEGIN … PRIVATE KEY-----`` armor line left every byte of the
# actual secret in the compressed content and in the CCR store while
# ``[REDACTED:private-key]`` asserted the opposite; the armor is the ANCHOR, not
# the credential. This mirrors ``compression_store._PEM_PRIVATE_KEY_RE``, which
# has always taken the whole block on the (narrower) retrieval-log surface.
#
# ``[A-Z0-9 ]*`` before ``PRIVATE KEY`` admits every label in the wild — bare
# PKCS#8, ``RSA``/``EC``/``DSA``/``OPENSSH``, and ``ENCRYPTED`` — and the
# optional `` BLOCK`` suffix admits PGP, whose real armor is ``BEGIN PGP PRIVATE
# KEY BLOCK-----`` (the old alternation listed ``PGP `` but could never match it,
# because it expected ``PGP PRIVATE KEY-----``). Public material (``BEGIN
# CERTIFICATE``, ``BEGIN PUBLIC KEY``) has no ``PRIVATE KEY`` and never matches.
_PEM_ARMOR_BEGIN = r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_ARMOR_END = r"-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"

# Block interior. ``(?:[^-]|-(?!----))`` is "any char that does not start a
# 5-hyphen armor run", which is both CORRECT (a base64 body and the RFC-1421
# ``Proc-Type``/``DEK-Info`` headers of an encrypted traditional PEM contain no
# ``-----``) and the reason this pattern is cheap on adversarial input: a scan
# launched from a BOGUS armor cannot run past the next armor, so total work stays
# linear in the payload instead of (armor count x bound). A plain ``[\s\S]*?``
# interior costs 12 s on 310 KB of packed bare armors; this costs 5 ms. It spans
# real newlines and the two-char ``\n`` escapes of a JSON-embedded key alike. The
# 100k bound is belt-and-braces on top of that guard: ~30x a 4096-bit RSA PEM.
# A block that somehow exceeds it falls to the truncated branch below, which
# still takes the armor and the body's base64 runs — less than the whole block,
# not nothing. Pinned by ``test_block_body_bound_falls_back_to_truncated_branch``.
_PEM_BLOCK_BODY = r"(?:[^-]|-(?!----)){0,100000}"

# Truncated-block fallback. Tool output is routinely cut mid-payload — that is
# why this library exists — and a block whose ``-----END`` never arrived would
# otherwise match NOTHING, losing even the header marker the old pattern gave.
# So: armor plus any run of base64 lines hanging off it.
#
# The FIRST run's separator is optional (review F-1): a body that abuts the armor
# with no separator at all, or with only a space or a tab, is a real shape once
# newlines have been stripped (single-line env-var keys) and it leaked while every
# run required a leading newline. Later runs still require one, so multi-line
# precision is untouched. Separators are real newlines or the two-char ``\n``
# escapes of a JSON-embedded key.
#
# The ``{16,}`` floor keeps it off prose — an English word beside an armor is far
# short of 16 unbroken base64 characters — so the branch redacts key material,
# not the paragraph that happens to follow. The one disclosed cost of the F-1
# widening: prose abutting the armor with NO separator whose first word is 16+
# unbroken alphanumerics is eaten, at most that one run.
#
# Both bounds below are deliberate and pinned. Past 2048 runs the rest of the
# body survives; that is ~128 KB of base64, far beyond any real key.
_PEM_TRUNCATED_BODY = (
    r"(?:[ \t]*[A-Za-z0-9+/=]{16,})?"
    r"(?:(?:\\n|[\r\n])+[ \t]*[A-Za-z0-9+/=]{16,}){0,2048}"
)

_PEM_PRIVATE_KEY_PATTERN = (
    f"{_PEM_ARMOR_BEGIN}{_PEM_BLOCK_BODY}{_PEM_ARMOR_END}|{_PEM_ARMOR_BEGIN}{_PEM_TRUNCATED_BODY}"
)

# High-precision built-in credential patterns, applied BY DEFAULT before any
# content is compressed or stored (audit Crit-4 / B3). Tool output routinely
# contains secrets and the store keeps originals for the TTL, so the safe default
# is to scrub the unambiguous credential shapes preventively rather than rely on
# the operator to configure ``FURL_REDACT_PATTERNS`` after the fact. Each pattern
# is deliberately SPECIFIC — a fixed vendor prefix plus a length/charset tail, or
# (for a private key) a delimited block — so it does not fire on ordinary logs,
# code, or prose: byte-exactness for non-secret content is unchanged, and only a
# genuine credential is masked. Each also spans the WHOLE secret, never just the
# token that identifies it, or the marker would assert a scrub it did not do. The
# ``(name, regex)`` pairs render as ``[REDACTED:<name>]`` (fixed length per type,
# so the span never leaks the secret's length). Disable with
# ``FURL_REDACT_BUILTINS=0``; add site-specific shapes with
# ``FURL_REDACT_PATTERNS`` (both compose).
_DEFAULT_CREDENTIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    # PEM/OpenSSH private-key blocks — armor AND key material (see above).
    ("private-key", _PEM_PRIVATE_KEY_PATTERN),
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
    return [text for line in lines if (text := line.strip()) and not text.startswith("#")]


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


def redact_budget_seconds(env: Mapping[str, str] | None = None) -> float:
    """The per-pass redaction budget in seconds, from ``FURL_REDACT_BUDGET_SECONDS``.

    Unset, blank, non-numeric, or non-positive all resolve to
    :data:`DEFAULT_REDACT_BUDGET_SECONDS` with a one-time warning for the two
    invalid cases — a typo'd budget must not silently become "no budget", which
    is exactly the fail-open state this whole mechanism exists to remove. Total:
    never raises.
    """
    source = os.environ if env is None else env
    raw = source.get(FURL_REDACT_BUDGET_ENV, "")
    if not raw or not raw.strip():
        return DEFAULT_REDACT_BUDGET_SECONDS
    try:
        seconds = float(raw.strip())
    except ValueError:
        _warn_once(
            f"{FURL_REDACT_BUDGET_ENV}: not a number ({raw.strip()!r}); "
            f"using {DEFAULT_REDACT_BUDGET_SECONDS}s"
        )
        return DEFAULT_REDACT_BUDGET_SECONDS
    if seconds <= 0:
        _warn_once(
            f"{FURL_REDACT_BUDGET_ENV}: must be greater than 0, got {seconds}; "
            f"using {DEFAULT_REDACT_BUDGET_SECONDS}s"
        )
        return DEFAULT_REDACT_BUDGET_SECONDS
    if seconds > MAX_REDACT_BUDGET_SECONDS:
        # Clamp, loudly. The redactor runs up to three times per hook invocation
        # (see the derivation above), so a budget above this cannot fit inside the
        # 30 s host timeout and would silently restore the fail-open it exists to
        # prevent. Accepting it quietly would be the same defect as accepting 0.
        _warn_once(
            f"{FURL_REDACT_BUDGET_ENV}: {seconds}s exceeds the {MAX_REDACT_BUDGET_SECONDS}s "
            f"ceiling (the redactor runs up to 3x per hook invocation and the host "
            f"kills the hook at 30s, after which UNREDACTED output is passed through); "
            f"clamping to {MAX_REDACT_BUDGET_SECONDS}s"
        )
        return MAX_REDACT_BUDGET_SECONDS
    return seconds


class RedactionBudgetExceeded(Exception):
    """Raised from the SIGALRM handler to unwind a wedged redaction.

    PUBLIC on purpose, though :func:`redact_within_budget` guarantees it never
    escapes. A caller that folds it into a generic ``except Exception`` and
    returns the original text fails OPEN on the module's own control-flow signal
    — the exact leak this module exists to close. Exporting it lets callers catch
    it explicitly and fail CLOSED, as a second layer behind that guarantee.
    """


def _make_budget_handler(previous_handler: Any) -> Callable[[int, object], None]:
    """Build the SIGALRM handler for ONE budgeted pass. SELF-DISARMING.

    The handler puts ``previous_handler`` back BEFORE it raises, which is what
    makes "our handler is never left installed process-wide" a structural property
    instead of a claim about which restore call happens to run.

    WHY THAT IS NEEDED, and why the restore calls alone were not enough. Installing
    our raiser and taking it down again are two separate bytecodes, and the alarm
    can be delivered between them at several points — notably AT THE CALL to the
    restore, where the exception is raised before the callee's body runs. Every
    such point had to be enumerated and covered individually, and enumeration was
    measurably incomplete: at 16000 aimed trials the raiser was still found
    installed after 6 calls that returned normally, on a tree whose restore already
    carried a retry loop and a backstop for the window it knew about.

    Self-disarming removes the enumeration. There are exactly two cases and both
    are closed by construction: either this handler RAN, in which case it has
    already restored ``previous_handler`` itself, or it never ran, in which case
    nothing was interrupted and the ordinary restore in the ``finally`` reaches its
    body. A path that leaves the raiser installed would have to be one where the
    handler both ran and did not run.

    Restoring inside the handler is safe: Python-level signal handlers are invoked
    by the eval loop at a bytecode boundary, not in async-signal context, so
    ``signal.signal`` is an ordinary call here.

    The restore is guarded because a handler that raised anything OTHER than
    ``RedactionBudgetExceeded`` would defeat the fail-closed contract — the callers
    match on that type, and a stray ``OSError`` would sail past every one of them
    into the hook's generic handler, which returns the ORIGINAL text.

    Of the three, only ``OSError`` is REACHABLE: CPython raises "Signal N ignored
    due to race condition" from ``signal.signal`` when a tripped signal finds a
    non-callable handler, which is ``SIG_DFL`` in the hook. ``TypeError`` and
    ``ValueError`` are UNREACHABLE BY CONSTRUCTION and retained deliberately as
    backstops, not because they were observed:

    * ``TypeError`` needs ``previous_handler`` to be a value ``signal.signal``
      rejects, and the only such value ``getsignal`` can produce is ``None`` —
      which :func:`_can_arm_watchdog` now refuses to arm on, so it cannot reach
      this closure.
    * ``ValueError`` needs a non-main thread or a bad signal number, and arming
      already succeeded from this thread with this number.

    They stay because the failure directions are not symmetric. If the arm-time
    guard ever regresses, swallowing here yields ``RedactionBudgetExceeded`` and a
    fail-CLOSED withhold; NOT swallowing yields a raw ``TypeError`` that walks
    into the hook's ``except Exception`` and returns the ORIGINAL text. A leaked
    handler is recoverable; a disclosed credential is not.
    """

    def _raise_budget_exceeded(signum: int, frame: object) -> None:
        try:
            signal.signal(signal.SIGALRM, previous_handler)
        except (OSError, ValueError, TypeError):
            pass
        raise RedactionBudgetExceeded()

    setattr(_raise_budget_exceeded, _WATCHDOG_MARKER, True)
    return _raise_budget_exceeded


def _is_budget_handler(handler: object) -> bool:
    """Whether *handler* is a watchdog handler this module installed.

    Identity comparison against a module-level function stopped working once the
    handler became per-call; tests and diagnostics ask this instead.
    """
    return bool(getattr(handler, _WATCHDOG_MARKER, False))


def _can_arm_watchdog() -> bool:
    """Whether a SIGALRM watchdog can be armed on this thread.

    Python-level signal handlers only run on the main thread of the main
    interpreter. The PostToolUse hook is a subprocess whose redaction runs on its
    main thread, so the watchdog genuinely arms there; the MCP server's worker
    threads are exactly the case it cannot cover (tracked separately).

    ALSO REFUSES WHEN THE CURRENT HANDLER CANNOT BE PUT BACK. ``getsignal``
    returns ``None`` when SIGALRM's disposition was neither ``SIG_DFL`` nor
    ``SIG_IGN`` at the moment the signal module initialised — CPython records
    "not our business" and keeps no object — and ``signal.signal`` rejects
    ``None`` with ``TypeError``. Arming on top of that value would install a
    handler that CANNOT be uninstalled: there is no installable object to
    restore, so the self-disarm fails, the restore fails, and our raising handler
    becomes a PERMANENT process-wide fixture rather than a 1-in-16000 one. That
    is strictly worse than having no budget, and it would also hijack SIGALRM
    from whatever C code owned it.

    Never arming is therefore the only correct answer, and it makes the whole
    ``TypeError`` family unreachable by construction rather than caught in some
    places and not others. It reuses the existing unbudgeted fallback, so the
    operator gets the same loud warning as on a platform without SIGALRM.
    """
    if not hasattr(signal, "SIGALRM"):
        return False
    if threading.current_thread() is not threading.main_thread():
        return False
    return signal.getsignal(signal.SIGALRM) is not None


def _unbudgeted_warning() -> str:
    """The operator-facing reason the timeout does not apply on this call.

    Two DIFFERENT causes that want two different operator responses, so they get
    two different messages rather than one vague line: a platform without SIGALRM
    cannot be fixed from Python at all, whereas an off-main-thread call is the
    caller's own choice and can be moved.
    """
    if not hasattr(signal, "SIGALRM"):
        return (
            "redaction is running UNBOUNDED on this host: SIGALRM does not exist on "
            "this platform (native Windows), so the redaction timeout does NOT apply. "
            "A pathological input can still wedge the PostToolUse hook past its 30 s "
            "timeout, after which the host passes the ORIGINAL, UNREDACTED tool output "
            "to the model. Running the plugin under WSL, macOS or Linux restores the "
            "bound."
        )
    if threading.current_thread() is not threading.main_thread():
        return (
            "redaction is running UNBOUNDED on this call: it is not on the main thread, "
            "where Python signal handlers never run, so the redaction timeout does NOT "
            "apply. Call redact_within_budget() from the main thread to get the bound."
        )
    return (
        "redaction is running UNBOUNDED on this call: SIGALRM's handler was installed "
        "outside Python before this interpreter started, so signal.getsignal() cannot "
        "hand back an object we could restore, and the redaction timeout does NOT "
        "apply because arming a watchdog we cannot UNINSTALL would hijack SIGALRM "
        "permanently. This needs an embedding host: no process launched normally can "
        "be in this state, because exec resets handlers. Install the SIGALRM handler "
        "from Python, or leave it at its default, to get the bound."
    )


def _restore_gave_up_warning() -> str:
    """Operator-facing notice that signal state could not be handed back cleanly.

    Deliberately NOT silent. Giving up is the right call — see ``_RESTORE_ATTEMPTS``
    — but it leaves the process's SIGALRM state in a shape this module did not
    choose, and the redaction VERDICT is unaffected either way, so an operator who
    later sees odd alarm behaviour needs this line to connect the two.
    """
    return (
        f"redaction could not hand back the process SIGALRM state after "
        f"{_RESTORE_ATTEMPTS} attempts; the redaction result itself is unaffected, but "
        f"another component's ITIMER_REAL deadline may have been disturbed."
    )


def redact_within_budget(
    text: str,
    redactor: Callable[[str], str],
    *,
    budget_seconds: float,
) -> str | None:
    """Run ``redactor(text)`` under a wall-clock budget. FAIL-CLOSED.

    Returns the redacted text, or ``None`` when the budget was exceeded. The
    UNREDACTED input is deliberately NOT returned in the overrun case: a caller
    holding only ``None`` cannot emit the original by mistake, which makes the
    leak this guards against unrepresentable rather than merely discouraged.

    CPython's ``sre`` engine calls ``PyErr_CheckSignals()`` periodically, so an
    itimer CAN interrupt a wedged ``re.sub`` — verified against the quadratic
    ``jwt`` pattern, which unwinds at the budget instead of running to completion.
    Interruption granularity is the engine's check interval, so the observed
    elapsed time is slightly past the budget rather than exactly on it.

    Both the previous handler and the caller's previous ``ITIMER_REAL`` are
    restored unconditionally, with the time THIS pass consumed subtracted from a
    pending caller deadline, so a budgeted pass never leaks timer state and never
    cancels an alarm the caller had already armed (the review-B7 lesson from
    ``furl_ctx.ccr.regex_budget``). A caller alarm that would have fired DURING
    this pass is indistinguishable from our own and surfaces as an overrun — the
    same, deliberate, trade-off that module makes.

    PLATFORM COVERAGE, stated plainly so it does not have to be derived from the
    ``hasattr`` check below. PROTECTED: macOS, Linux, and Windows under WSL — all
    have ``SIGALRM``, so the budget arms and an overrun fails closed. NOT
    PROTECTED: native Windows, where ``SIGALRM`` does not exist; there the
    redactor runs UNBUDGETED, this function can never return ``None``, and the
    pre-budget fail-open disclosure described in the module docstring persists
    unchanged. Closing native Windows needs a subprocess-based bound (a
    thread-based one cannot work — the GIL makes the timeout unobservable, see
    ``furl_ctx.ccr.regex_budget``) and is tracked separately.

    Where the watchdog cannot arm — native Windows, or a call off the main thread
    — the redactor runs UNBUDGETED, its result is returned, and a one-time
    warning naming which of those two causes applies is written to stderr. The
    fallback itself is deliberate: failing closed where no budget ever existed
    would withhold ALL output on that platform, which is catastrophic rather than
    safe. The WARNING is what keeps it from being a silent no-op, matching the
    ``mcp_server._refuse_regex_filters`` principle that an unavailable guarantee
    must be visible rather than assumed.

    Exceptions from ``redactor`` itself are NOT caught here — they propagate, so
    callers keep whatever fail-open posture they already had for a broken
    redactor. Only the timeout path is new.
    """
    if not _can_arm_watchdog():
        _warn_once(_unbudgeted_warning())
        return redactor(text)
    try:
        return _redact_under_watchdog(text, redactor, budget_seconds)
    except RedactionBudgetExceeded:
        # OUTERMOST GUARANTEE. _restore_after_budget already prevents a late alarm
        # from escaping, but "it cannot escape" must hold STRUCTURALLY rather than
        # by inspection of the restore: this converts any residual escape, on any
        # path present or future, into the fail-CLOSED verdict instead of letting
        # it reach a caller whose generic ``except Exception`` would return the
        # ORIGINAL text. Withholding is the correct verdict here because an escape
        # means the pass could not be shown to have completed.
        return None


def _redact_under_watchdog(
    text: str,
    redactor: Callable[[str], str],
    budget_seconds: float,
) -> str | None:
    """Arm the budget, run ``redactor``, restore. See :func:`redact_within_budget`."""
    # Read the handler first, then install: the handler has to CLOSE OVER what it must
    # put back, because it restores it itself before raising. Reading and installing as
    # two calls is not a race — Python-level handlers only run on the main thread, which
    # is the thread executing these two lines.
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _make_budget_handler(previous_handler))
    previous_remaining, previous_interval = signal.setitimer(signal.ITIMER_REAL, budget_seconds)
    started = time.monotonic()
    # POSITIVE PROOF OF COMPLETION, not absence of exception. ``finished`` is a
    # 1-tuple that can only be CONSTRUCTED once ``redactor(text)`` has already
    # returned a value, so ``finished is not None`` is evidence the pass ran to
    # completion rather than an inference from "nothing was raised". A plain
    # ``result: str | None`` shared between the completed and interrupted paths
    # would classify by absence of exception, and any future edit that let an
    # interrupted pass reach the return would emit a PARTIALLY scrubbed blob as
    # though it were fully scrubbed — worse than withholding, and indistinguishable
    # from success at the call site.
    finished: tuple[str] | None = None
    try:
        try:
            finished = (redactor(text),)
        except RedactionBudgetExceeded:
            finished = None
        finally:
            _restore_after_budget(previous_handler, previous_remaining, previous_interval, started)
    except RedactionBudgetExceeded:
        # R1. The CALL to _restore_after_budget above is itself an eval-breaker and
        # it sits outside every inner ``try``: an alarm delivered exactly there —
        # after the redactor returned, at the transition into the ``finally`` —
        # raises AT THE CALL, so the restore's BODY never runs. What survives to
        # here is therefore a pass that COMPLETED but could not be shown to have
        # completed, and this converts it to a withhold.
        #
        # This is no longer what keeps our handler off the process. The handler
        # disarms ITSELF before raising (see _make_budget_handler), so by the time
        # this clause is reached ``previous_handler`` is already reinstalled, on
        # this route and on every other unwinding route including ones not
        # enumerated here. Enumerating them was tried and measured incomplete —
        # 6 leaks in 16000 aimed trials against a tree that had this clause and the
        # restore's retry loop but no self-disarm. The restore below therefore runs
        # for the ITIMER: the caller's deadline still has to be handed back.
        #
        # Restoring here is safe from a second interruption, and that is provable
        # rather than hopeful: our itimer is armed ONE-SHOT (``setitimer`` with a
        # zero interval), so exactly one alarm can be delivered per call and it has
        # already been consumed to get here.
        _restore_after_budget(previous_handler, previous_remaining, previous_interval, started)
        return None
    return None if finished is None else finished[0]


def _restore_after_budget(
    previous_handler: Any,
    previous_remaining: float,
    previous_interval: float,
    started: float,
) -> None:
    """Disarm our budget and give the caller back its handler and its deadline.

    TOLERATES A LATE ALARM, which is the whole reason this is a function rather
    than a plain ``finally`` body. SIGALRM can be delivered after the guarded call
    has already returned normally — the deadline expires in the handful of
    bytecodes between that return and this restore. The ``except`` clause upstream
    only guards the call itself and is no longer eligible by then, so an unguarded
    restore let ``RedactionBudgetExceeded`` escape as a THIRD exit path from a
    function documented to return ``str | None``, AND skipped the handler restore
    below, leaving our handler installed process-wide so a later unrelated alarm
    would raise our internal exception at an arbitrary point. In the hook that
    escape landed in ``_apply_env_redaction``'s ``except Exception``, which
    returns the ORIGINAL text — a fail-open passthrough of content whose redaction
    had actually SUCCEEDED.

    Swallowing the late alarm is the correct semantics, not merely a way to keep
    quiet: the guarded call already finished, so there is nothing left to
    interrupt, and the completed result is the honest answer. Returning ``None``
    there would withhold output that was successfully scrubbed.

    TOLERATES ``OSError`` FOR THE SAME REASON, on a path that is not obvious from
    reading this function. When a signal has been tripped at C level but the
    Python-level handler is no longer a callable by the time the eval loop gets to
    it, CPython reports the discarded signal as ``OSError("Signal 14 ignored due to
    race condition")`` — raised FROM ``signal.signal`` itself, i.e. from the very
    call below that installs ``previous_handler``. In the hook ``previous_handler``
    is ``SIG_DFL``, which is exactly the non-callable case, so this is the ordinary
    outcome of a late alarm here rather than an exotic one. Catching only
    ``RedactionBudgetExceeded`` let it through, and it is not a subclass of
    anything the callers match on: it would traverse ``_redact_under_watchdog``,
    ``redact_within_budget`` and both of their guards untouched and land in
    ``_apply_env_redaction``'s ``except Exception``, which returns the ORIGINAL
    text. Retrying is also the CORRECT response and not merely a way to swallow it:
    the report is ambiguous about whether the swap landed, and both calls below are
    idempotent, so a second pass settles it either way.

    TOLERATES ``TypeError``/``ValueError`` FOR ASYMMETRY, NOT FOR REACHABILITY.
    Both are unreachable once :func:`_can_arm_watchdog` refuses to arm on a
    ``previous_handler`` of ``None`` — see :func:`_make_budget_handler` for the
    full argument, which applies unchanged here. They are caught anyway because
    escaping this function is a FAIL-OPEN outcome (the hook's generic handler
    returns the original text) while swallowing is fail-closed, so the cheap
    insurance is on the correct side. Catching them here also removes an asymmetry
    that was real: the handler guarded this same call against all three while this
    loop guarded it against one, so the same escape was closed in one place and
    open in the other.

    TERMINATION, argued without assuming "at most one late alarm". Every iteration
    DISARMS our itimer as its first statement, so after the first successful
    ``setitimer(0)`` the SOURCE of these alarms is switched off and no new one can
    be generated; only an already-pending signal can fire, and CPython delivers a
    pending signal at the next bytecode boundary, so it cannot be deferred across
    iterations. Each iteration also ends by restoring the caller's handler, after
    which our handler is not installed and this exception cannot be raised at all.
    Progress is therefore monotonic on both counts. The loop is nevertheless BOUNDED
    (see ``_RESTORE_ATTEMPTS``), because that argument covers the faults this
    function knows about and an unbounded loop would convert any it does not into a
    hang inside a security control.

    WHY A RETRY LOOP RATHER THAN ``pthread_sigmask``. Blocking SIGALRM across the
    restore looks stronger and is weaker here, for a reason specific to this
    caller. In the hook, ``previous_handler`` is ``SIG_DFL``, and SIGALRM's default
    disposition TERMINATES the process. A mask defers delivery rather than
    discarding it, so unblocking after the handler is back delivers any pending
    alarm straight into ``SIG_DFL`` and kills the hook — which lands on the host's
    fail-open contract, i.e. the leak, arrived at by a different route. Discarding
    the pending signal instead would need ``sigtimedwait``, which does not exist on
    macOS. The retry loop keeps OUR raising handler installed until it has
    confirmed no alarm is pending, so the dangerous window closes with the signal
    consumed rather than deferred. The caller's own deadline is re-armed only AFTER
    its handler is back, so a re-armed alarm reaches the caller, not this module.
    """
    for _ in range(_RESTORE_ATTEMPTS):
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        except (RedactionBudgetExceeded, OSError, TypeError, ValueError):
            continue
        break
    else:
        _warn_once(_restore_gave_up_warning())
        return
    # A 0.0 remaining means "was not armed at all", and setitimer(0.0) IS the
    # disarm, so that case needs no re-arm and must not get an epsilon.
    if previous_remaining:
        restored = previous_remaining - (time.monotonic() - started)
        try:
            signal.setitimer(
                signal.ITIMER_REAL, max(restored, _MIN_RESTORED_TIMER), previous_interval
            )
        except (RedactionBudgetExceeded, OSError, TypeError, ValueError):
            # THE SAME TUPLE AS THE DISARM LOOP AND THE HANDLER, deliberately, because
            # "this function never raises" has to hold at EVERY call it makes or it does
            # not hold at all. This one runs from a ``finally``, so an escape here would
            # REPLACE the outcome of the guarded call with an exception the callers do
            # not match on — the fail-open landing again, reached from the re-arm
            # instead of the disarm.
            #
            # Reachability differs from the disarm's and is stated rather than implied.
            # ``OSError`` is the live one (the discarded-signal report). ``TypeError``
            # and ``ValueError`` need non-numeric or negative arguments, and both are
            # excluded by the ``max()`` above and by ``previous_interval`` coming from
            # ``setitimer``'s own return. ``RedactionBudgetExceeded`` needs the restored
            # handler to itself be a budget handler, which happens only when a budgeted
            # pass is NESTED inside another; the outer pass then withholds, which is
            # fail-closed and correct. Enumerating them was the mistake once already, so
            # the tuple is uniform and the argument lives in the comment.
            _warn_once(_restore_gave_up_warning())


__all__ = [
    "DEFAULT_REDACT_BUDGET_SECONDS",
    "FURL_REDACT_BUDGET_ENV",
    "FURL_REDACT_BUILTINS_ENV",
    "FURL_REDACT_PATTERNS_ENV",
    "MAX_REDACT_BUDGET_SECONDS",
    "RedactionBudgetExceeded",
    "build_default_redactor",
    "build_env_redactor",
    "build_store_redactor",
    "compose_redactors",
    "redact_budget_seconds",
    "redact_within_budget",
    "redaction_marker",
]
