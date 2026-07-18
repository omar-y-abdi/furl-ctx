#!/usr/bin/env python3
"""Furl pipe compressor — the STDOUT filter the on-by-default FURL_PRETOOL_PIPE
rewrite (disable with FURL_PRETOOL_PIPE=0) pipes a Bash command's output through.

Unlike the PostToolUse hook (whose ``updatedToolOutput`` Claude Code >=2.1.163
silently DROPS — anthropics/claude-code#68951), this rewrites what the model
sees at the SOURCE: it reads the command's stdout on STDIN and writes a
COMPRESSED form to STDOUT, storing the original under a ``<<ccr:HASH>>`` marker
in the SAME durable per-project CCR store the PostToolUse hook and MCP server
use — so ``furl_retrieve`` resolves it. Same size threshold and same store / TTL
semantics as the PostToolUse path; the same env redaction
(``FURL_REDACT_PATTERNS``) applies on the NORMAL path only — the fail-open paths
skip it (review F5): binary/undecodable stdin and the furl_ctx-unavailable
fallback pass through raw and UNREDACTED, and the raw stdout also transits the
rewrite's ``0600`` tempfile for the command's runtime (see the plugin README's
"Known limitations").

Contract:
  stdin  : raw bytes (a command's stdout).
  stdout : the compressed form (marker + summary) when it genuinely shrinks and
           the original was durably stored; otherwise the input verbatim.

Invariants (all load-bearing — the pipe must never break a command):
  * FAIL-OPEN, byte-exact passthrough. ANY problem — undecodable/binary input,
    furl_ctx unavailable, compression error, no savings, below threshold, a lost
    durable store write — writes the INPUT through UNCHANGED and exits 0.
  * WRITE ONCE, at the very end. Nothing reaches stdout until the final decision,
    so a failure before that leaves nothing partial for the shell-level
    ``|| cat`` fallback (in the rewrite) to duplicate.
  * The command's EXIT CODE and STDERR are the shell rewrite's job, not this
    filter's — it only ever transforms stdout.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Pin the durable, cross-process CCR store BEFORE furl_ctx builds it, and match
# the plugin's retention — identical to compress_tool_output.py. ``setdefault``
# keeps the rewrite-baked FURL_CCR_PROJECT_DIR and any user override intact. A
# non-durable (memory) store would make the emitted ``<<ccr:HASH>>`` marker
# unretrievable, so sqlite is required for the marker to be honest.
os.environ.setdefault("FURL_CCR_BACKEND", "sqlite")
os.environ.setdefault("FURL_CCR_TTL_SECONDS", "86400")

_CCR_MARKER = "<<ccr:"
_MIN_CHARS_ENV = "FURL_HOOK_MIN_CHARS"
_MODEL_ENV = "FURL_HOOK_MODEL"
_DEFAULT_MIN_CHARS = 2000
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def _min_chars() -> int:
    """Minimum stdout length (chars) before compression is attempted — the SAME
    threshold and env var (``FURL_HOOK_MIN_CHARS``) as the PostToolUse hook, so
    small outputs pass through raw identically on both paths."""
    raw = os.environ.get(_MIN_CHARS_ENV, "").strip()
    if not raw:
        return _DEFAULT_MIN_CHARS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MIN_CHARS
    return value if value > 0 else _DEFAULT_MIN_CHARS


def _apply_env_redaction(text: str) -> str:
    """Scrub ``FURL_REDACT_PATTERNS`` secrets before any gate — same builder the
    hook, MCP server, and library share, so one env var governs all four paths.
    Fail-open: unimportable furl_ctx or redactor-off returns *text* unchanged."""
    try:
        from furl_ctx.redaction import build_env_redactor
    except Exception:
        return text
    try:
        redactor = build_env_redactor()
        if redactor is None:
            return text
        return redactor(text)
    except Exception:
        return text


def _compress_text(text: str) -> str | None:
    """Compress *text* via Furl's public pipeline as a Bash tool output.

    Returns the compressed string only when compression genuinely helped (no
    fail-open error AND the result is shorter AND the original was durably
    stored — so the emitted ``<<ccr:HASH>>`` marker resolves). Returns ``None``
    ("leave the original alone") otherwise. Never raises.
    """
    try:
        from furl_ctx import compress
    except Exception:
        return None

    model = os.environ.get(_MODEL_ENV, "").strip() or _DEFAULT_MODEL
    try:
        result = compress([{"role": "tool", "content": text}], model=model, tool_name="Bash")
    except Exception:
        return None

    if getattr(result, "error", None):
        return None
    messages = getattr(result, "messages", None)
    if not messages:
        return None
    compressed = messages[0].get("content")
    if not isinstance(compressed, str):
        return None
    if len(compressed) >= len(text):
        return None
    return compressed


def _counters() -> Any:
    try:
        import _furl_ccr_counters

        return _furl_ccr_counters
    except Exception:
        return None


def _bump(cmod: Any, store: Any, name: str) -> None:
    if cmod is not None and store is not None:
        cmod.bump(store, name)


def _decide(text: str, raw: bytes) -> bytes:
    """Pure-ish outcome: the bytes to emit for decoded stdout *text* (raw bytes
    *raw* preserved for byte-exact passthrough). Records pipe counters. Never
    raises — callers still wrap it, but every branch here is fail-open."""
    cmod = _counters()
    store = cmod.resolve_store() if cmod is not None else None
    _bump(cmod, store, cmod.PIPE_INVOCATIONS) if cmod is not None else None

    # Loop guard: already compressed by an upstream Furl path — pass through.
    if _CCR_MARKER in text:
        _bump(cmod, store, cmod.PIPE_NOOP_PREFIX + "already-compressed") if cmod else None
        return raw

    # Scope the store to THIS project (the rewrite bakes FURL_CCR_PROJECT_DIR;
    # setdefault keeps it — this is only the bare-invocation fallback).
    os.environ.setdefault(
        "FURL_CCR_PROJECT_DIR",
        os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
    )

    redacted = _apply_env_redaction(text)

    if len(redacted) < _min_chars():
        _bump(cmod, store, cmod.PIPE_NOOP_PREFIX + "below-min-chars") if cmod else None
        return redacted.encode("utf-8") if redacted != text else raw

    compressed = _compress_text(redacted)
    if compressed is None:
        _bump(cmod, store, cmod.PIPE_NOOP_PREFIX + "no-savings") if cmod else None
        return redacted.encode("utf-8") if redacted != text else raw

    _bump(cmod, store, cmod.PIPE_COMPRESSIONS) if cmod is not None else None
    return compressed.encode("utf-8")


def main() -> int:
    # Read all of stdin as bytes (binary-safe). Decode failure → the input is not
    # text we can compress; pass the exact bytes through.
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return 0  # nothing we can do; the shell fallback will cat the tempfile
    try:
        text = raw.decode("utf-8")
    except Exception:
        _write_once(raw)
        return 0

    try:
        out = _decide(text, raw)
    except Exception:
        out = raw  # absolute fail-open: original bytes, never a broken pipe
    _write_once(out)
    return 0


def _write_once(data: bytes) -> None:
    """Emit *data* to stdout exactly once, at the end (so a pre-decision failure
    left nothing partial for the rewrite's ``|| cat`` fallback to duplicate)."""
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        pass


if __name__ == "__main__":
    # Last-resort guard: no uncaught exception may reach the shell as a nonzero
    # exit that would trigger the fallback ``cat`` AFTER we already wrote output.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
