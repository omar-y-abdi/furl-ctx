#!/usr/bin/env python3
"""Furl PostToolUse hook: compress large tool outputs before they enter context.

Contract (Claude Code PostToolUse hook):
  - stdin  : JSON with ``tool_name``, ``tool_input``, ``tool_response``, ``cwd``,
             ``session_id``, ``hook_event_name``.
  - stdout : to REPLACE the output the model sees, emit
             ``{"hookSpecificOutput": {"hookEventName": "PostToolUse",
                "updatedToolOutput": "<compressed text>"}}`` and exit 0.
  - stdout empty + exit 0 : original tool output passes through unchanged.

Design invariant — FAIL OPEN. Any error (bad stdin, missing furl_ctx, compression
failure, unexpected payload shape) results in exit 0 with NO stdout, so the user's
tool call is never broken. The hook only ever *removes* tokens on the happy path;
it never blocks, mutates inputs, or raises to the host.

Retrievability: compression offloads dropped content to the shared CCR store and
leaves ``<<ccr:HASH>>`` markers. For the ``furl`` MCP server's ``furl_retrieve`` to
resolve those markers, this hook and the server must share one durable store —
both pin ``FURL_CCR_BACKEND=sqlite`` (see hooks.json / .mcp.json), which resolves
to ``~/.furl/ccr.sqlite3`` in every process of the session.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NoReturn

# Tune via environment (all optional; defaults are the shipped behavior).
_ENABLED_ENV = "FURL_HOOK_ENABLED"
_MIN_CHARS_ENV = "FURL_HOOK_MIN_CHARS"
_MODEL_ENV = "FURL_HOOK_MODEL"
_EXCLUDE_ENV = "FURL_HOOK_EXCLUDE_TOOLS"
_MODE_ENV = "FURL_HOOK_MODE"

_DEFAULT_MIN_CHARS = 2000
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
_CCR_MARKER = "<<ccr:"

# Pin the durable, cross-process CCR store BEFORE furl_ctx builds it. Without this
# the library default is an in-memory store that dies when this subprocess exits —
# so a ``<<ccr:HASH>>`` marker this hook emits would have no retrievable original and
# the `furl` MCP server's ``furl_retrieve`` would miss. ``setdefault`` keeps any
# user override (e.g. ``FURL_CCR_BACKEND=memory``) intact. This does not depend on
# the ``env`` block in hooks.json being honored by the host.
os.environ.setdefault("FURL_CCR_BACKEND", "sqlite")
os.environ.setdefault("FURL_CCR_TTL_SECONDS", "86400")

# Furl's own tool output must never be recompressed (would double-compress or
# compress content the model just retrieved). Furl's MCP tools are namespaced
# ``mcp__<server>__furl_*`` by the host. This is the built-in loop-guard base
# (as a glob, always excluded); operators add more via FURL_HOOK_EXCLUDE_TOOLS.
_SELF_TOOL_SUBSTR = "furl_"


def _flag_enabled(raw: str | None) -> bool:
    """Interpret an on/off env flag. Unset or empty -> enabled (default on)."""
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _min_chars() -> int:
    """Minimum tool-output length (chars) before compression is attempted."""
    raw = os.environ.get(_MIN_CHARS_ENV, "").strip()
    if not raw:
        return _DEFAULT_MIN_CHARS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MIN_CHARS
    return value if value > 0 else _DEFAULT_MIN_CHARS


def _extract_text(tool_response: Any) -> str | None:
    """Pull compressible plain text out of a ``tool_response``.

    Handles the three shapes Claude Code emits:
      * ``str``                       -> Bash / plain tool output.
      * ``[{"type":"text","text":.}]``-> MCP-style content blocks.
      * ``{"content": <str|blocks>}`` -> wrapped tool result.

    Returns ``None`` for anything else (images, mixed non-text blocks, empty) so
    the caller passes the original through untouched.
    """
    if isinstance(tool_response, str):
        return tool_response or None

    if isinstance(tool_response, dict):
        return _extract_text(tool_response.get("content"))

    if isinstance(tool_response, list):
        parts: list[str] = []
        for block in tool_response:
            if not isinstance(block, dict):
                return None
            if block.get("type", "text") != "text":
                return None
            text = block.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
        joined = "".join(parts)
        return joined or None

    return None


def _exclude_tools() -> set[str]:
    """Tools to never (re)compress: Furl's own output (loop guard) plus any the
    operator lists in FURL_HOOK_EXCLUDE_TOOLS (comma-separated; exact names or
    fnmatch globs like ``mcp__*``, per furl_ctx.config.is_tool_excluded)."""
    user = os.environ.get(_EXCLUDE_ENV, "")
    return {f"*{_SELF_TOOL_SUBSTR}*", *(t.strip() for t in user.split(",") if t.strip())}


def _excluded(tool_name: str) -> bool:
    """True if *tool_name* is excluded from compression. Uses the engine's
    glob-aware is_tool_excluded, falling back to the built-in loop guard if
    furl_ctx cannot be imported (the same fail-open posture as compression)."""
    if not tool_name:
        return False
    try:
        from furl_ctx.config import is_tool_excluded

        return is_tool_excluded(tool_name, _exclude_tools())
    except Exception:
        return _SELF_TOOL_SUBSTR in tool_name.lower()


def _mode_kwargs() -> dict[str, object]:
    """FURL_HOOK_MODE -> compress() overrides. ``aggressive`` also compresses code
    in the blob and squeezes smaller outputs; ``normal`` (default) keeps the
    shipped behavior. (``lossless_only`` is not yet wired — it needs an engine-side
    pipeline lever; see harness-plan.md.)"""
    if os.environ.get(_MODE_ENV, "").strip().lower() == "aggressive":
        return {"protect_recent": 0, "min_tokens_to_compress": 50}
    return {}


def _compress_text(text: str) -> str | None:
    """Compress one tool-output blob via Furl's public pipeline.

    Returns the compressed text only when compression genuinely helped
    (no fail-open error AND the result is shorter). Returns ``None`` otherwise,
    signalling "leave the original alone". Never raises.
    """
    try:
        from furl_ctx import compress
    except Exception:
        return None

    model = os.environ.get(_MODEL_ENV, "").strip() or _DEFAULT_MODEL
    try:
        result = compress([{"role": "tool", "content": text}], model=model, **_mode_kwargs())
    except Exception:
        return None

    # compress() is fail-open: on internal failure it returns the ORIGINAL
    # messages with ``error`` set. Treat that as "do nothing".
    if getattr(result, "error", None):
        return None

    messages = getattr(result, "messages", None)
    if not messages:
        return None

    compressed = messages[0].get("content")
    if not isinstance(compressed, str):
        return None

    # Only replace when we actually saved characters.
    if len(compressed) >= len(text):
        return None
    return compressed


def _passthrough() -> NoReturn:
    """Emit nothing and succeed: the original tool output is kept verbatim."""
    sys.exit(0)


def main() -> None:
    # --- read + parse stdin (fail open on any problem) ---
    try:
        raw = sys.stdin.read()
    except Exception:
        _passthrough()
    if not raw.strip():
        _passthrough()
    try:
        payload = json.loads(raw)
    except Exception:
        _passthrough()
    if not isinstance(payload, dict):
        _passthrough()

    # --- kill switch ---
    if not _flag_enabled(os.environ.get(_ENABLED_ENV)):
        _passthrough()

    # --- loop guard + operator exclusions (FURL_HOOK_EXCLUDE_TOOLS) ---
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and _excluded(tool_name):
        _passthrough()

    # --- extract text; bail on non-text / empty payloads ---
    text = _extract_text(payload.get("tool_response"))
    if text is None:
        _passthrough()

    # --- loop guard: already carries CCR markers -> already compressed ---
    if _CCR_MARKER in text:
        _passthrough()

    # --- size gate ---
    if len(text) < _min_chars():
        _passthrough()

    # --- compress (returns None unless it genuinely helped) ---
    compressed = _compress_text(text)
    if compressed is None:
        _passthrough()

    # --- replace the tool output the model sees ---
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": compressed,
        }
    }
    try:
        sys.stdout.write(json.dumps(output))
    except Exception:
        # If we somehow cannot serialize, fall back to passthrough.
        _passthrough()
    sys.exit(0)


if __name__ == "__main__":
    # Absolute last-resort guard: no uncaught exception may ever reach the host.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
