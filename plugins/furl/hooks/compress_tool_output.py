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
both pin ``FURL_CCR_BACKEND=sqlite`` (see hooks.json / .mcp.json) and both derive
the same per-project namespace (``FURL_CCR_PROJECT_DIR``, from CLAUDE_PROJECT_DIR /
stdin ``cwd``), which resolves to a per-project ``~/.furl/ccr-ns-<hash>.sqlite3``
in every process of the session. The legacy global ``~/.furl/ccr.sqlite3`` serves
only when namespacing is explicitly disabled (``FURL_CCR_PROJECT_DIR=""``).
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
_VERBOSE_ENV = "FURL_HOOK_VERBOSE"

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


def _noop_verbose_enabled() -> bool:
    """Whether to log a one-line reason when the hook no-ops.

    Opt-in (explicit truthy FURL_HOOK_VERBOSE), NOT default-on: a no-op fires on
    nearly every tool call (most outputs are small / non-text), so defaulting this on
    would flood stderr. This is deliberately stricter than the rare success-path
    annotation, which keeps its shipped default-on ``_flag_enabled`` gate."""
    return os.environ.get(_VERBOSE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


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

    Handles the shapes Claude Code actually emits for the matched tools, verified
    against live PostToolUse payloads (Claude Code 2.1.x):

      * ``str``                        -> plain tool output.
      * ``[{"type":"text","text":.}]`` -> MCP-style content blocks.
      * ``{"content": <str|blocks>}``  -> wrapped result; also the Task/``Agent``
                                          sub-agent answer (content blocks).
      * ``{"stdout","stderr",..}``     -> Bash. Uses ``stdout``; appends ``stderr``
                                          only when non-empty, under a clear
                                          ``[stderr]`` separator, so error text stays
                                          compressible AND retrievable.
      * ``{"result": <str>, ..}``      -> WebFetch (its answer lives in ``result``).
      * ``{"text": <str>, ..}``        -> generic single-text-field results.

    Returns ``None`` for anything else — images, mixed non-text blocks, empty
    output, and structured payloads with no free-text field (e.g. WebSearch's
    ``{"query","results":[{title,url}..]}`` link list, deliberately left to pass
    through rather than forced through a prose compressor). Totality is preserved:
    unknown shape -> ``None`` -> caller passes the original through untouched.
    """
    if isinstance(tool_response, str):
        return tool_response or None

    if isinstance(tool_response, dict):
        # Wrapped result / MCP-style blocks / Task(Agent) sub-agent answer. Kept
        # first so the legacy ``{"content": ...}`` contract is byte-identical; only
        # fall through when ``content`` is absent or carries no text, so a Bash-style
        # sibling key (``stdout``) on the same dict is still reachable.
        content = tool_response.get("content")
        if content is not None:
            text = _extract_text(content)
            if text is not None:
                return text

        # Bash: {"stdout","stderr","interrupted","isImage","noOutputExpected"}.
        stdout = tool_response.get("stdout")
        if isinstance(stdout, str):
            stderr = tool_response.get("stderr")
            if isinstance(stderr, str) and stderr.strip():
                combined = f"{stdout}\n\n[stderr]\n{stderr}" if stdout else stderr
                return combined or None
            return stdout or None

        # WebFetch ("result") and other single-text-field results ("text").
        for key in ("result", "text"):
            value = tool_response.get(key)
            if isinstance(value, str):
                return value or None

        return None

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


def _compress_text(text: str) -> tuple[str | None, str | None]:
    """Compress one tool-output blob via Furl's public pipeline.

    Returns ``(compressed, None)`` only when compression genuinely helped
    (no fail-open error AND the result is shorter). Returns ``(None, reason)``
    otherwise, signalling "leave the original alone" — where ``reason`` is a
    distinct diagnostic label for the FURL_HOOK_VERBOSE no-op line, so an
    import failure is never mislabeled as "no savings":

      * ``import-failed``  -> furl_ctx is not importable in the hook's env.
      * ``compress-error`` -> compress() raised (caught here; fail-open).
      * ``engine-error``   -> compress() reported an internal fail-open error.
      * ``empty-result``   -> engine returned no messages / non-text content.
      * ``no-savings``     -> compression succeeded but did not shrink the text.

    Never raises.
    """
    try:
        from furl_ctx import compress
    except Exception:
        return None, "import-failed"

    model = os.environ.get(_MODEL_ENV, "").strip() or _DEFAULT_MODEL
    try:
        result = compress([{"role": "tool", "content": text}], model=model, **_mode_kwargs())
    except Exception:
        return None, "compress-error"

    # compress() is fail-open: on internal failure it returns the ORIGINAL
    # messages with ``error`` set. Treat that as "do nothing".
    if getattr(result, "error", None):
        return None, "engine-error"

    messages = getattr(result, "messages", None)
    if not messages:
        return None, "empty-result"

    compressed = messages[0].get("content")
    if not isinstance(compressed, str):
        return None, "empty-result"

    # Only replace when we actually saved characters.
    if len(compressed) >= len(text):
        return None, "no-savings"
    return compressed, None


def _passthrough(reason: str | None = None) -> NoReturn:
    """Emit nothing and succeed: the original tool output is kept verbatim.

    When FURL_HOOK_VERBOSE is on and *reason* is given, write ONE diagnostic line to
    stderr naming why nothing was compressed (shape-unmatched / below-min-chars /
    excluded-tool / disabled / ...). stderr is surfaced to the user only; it never
    reaches the model and never blocks the tool call — exit stays 0 (fail-open)."""
    if reason and _noop_verbose_enabled():
        sys.stderr.write(f"furl: no-op ({reason})\n")
    sys.exit(0)


def main() -> None:
    # --- read + parse stdin (fail open on any problem) ---
    try:
        raw = sys.stdin.read()
    except Exception:
        _passthrough("stdin-read-failed")
    if not raw.strip():
        _passthrough("empty-stdin")
    try:
        payload = json.loads(raw)
    except Exception:
        _passthrough("bad-json")
    if not isinstance(payload, dict):
        _passthrough("non-dict-payload")

    # --- per-project CCR isolation (audit #4) ---
    # Scope the durable store to THIS project so the shared ~/.furl DB cannot
    # commingle originals across projects or evict cross-project. Prefer
    # CLAUDE_PROJECT_DIR (Claude Code's project root) so this hook and the
    # long-lived furl MCP server converge on ONE per-project store; the stdin
    # ``cwd`` then os.getcwd() are fallbacks. ``setdefault`` keeps a user's
    # shared-store override (FURL_CCR_NAMESPACE) or legacy-global opt-out
    # (FURL_CCR_PROJECT_DIR="") intact.
    _cwd = payload.get("cwd")
    os.environ.setdefault(
        "FURL_CCR_PROJECT_DIR",
        os.environ.get("CLAUDE_PROJECT_DIR")
        or (_cwd if isinstance(_cwd, str) and _cwd.strip() else "")
        or os.getcwd(),
    )

    # --- kill switch ---
    if not _flag_enabled(os.environ.get(_ENABLED_ENV)):
        _passthrough("disabled")

    # --- loop guard + operator exclusions (FURL_HOOK_EXCLUDE_TOOLS) ---
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and _excluded(tool_name):
        _passthrough("excluded-tool")

    # --- extract text; bail on non-text / empty / unrecognized payloads ---
    text = _extract_text(payload.get("tool_response"))
    if text is None:
        _passthrough("shape-unmatched")

    # --- loop guard: already carries CCR markers -> already compressed ---
    if _CCR_MARKER in text:
        _passthrough("already-compressed")

    # --- size gate ---
    if len(text) < _min_chars():
        _passthrough("below-min-chars")

    # --- compress (returns None + a distinct reason unless it genuinely helped) ---
    compressed, compress_fail_reason = _compress_text(text)
    if compressed is None:
        _passthrough(compress_fail_reason or "no-savings")

    # --- optional one-line stderr annotation (FURL_HOOK_VERBOSE) ---
    if _flag_enabled(os.environ.get(_VERBOSE_ENV)):
        saved_pct = round((1 - len(compressed) / len(text)) * 100)
        sys.stderr.write(
            f"furl: {tool_name or '?'} "
            f"{len(text) / 1024:.1f} KB -> {len(compressed) / 1024:.1f} KB  -{saved_pct}%\n"
        )

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
        _passthrough("serialize-failed")
    sys.exit(0)


if __name__ == "__main__":
    # Absolute last-resort guard: no uncaught exception may ever reach the host.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
