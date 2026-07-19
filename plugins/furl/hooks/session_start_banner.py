#!/usr/bin/env python3
"""Furl SessionStart hook: a one-line, version-aware status banner.

Contract (Claude Code SessionStart hook):
  stdin  : ignored (this banner needs no per-session hook input).
  stdout : ``{"systemMessage": "<one line>"}`` and exit 0 — ``systemMessage`` is
           shown to the user directly, at zero model-context cost (it is NOT
           injected as ``additionalContext``).
  exit 0, always: FAIL-OPEN, never blocks a session start.

Deliberately dependency-free (no ``uv run``, no ``furl_ctx`` import): SessionStart
fires on every session start and must stay cheap — see
tests/test_plugin_hooks_manifest.py::test_session_start_is_cheap_user_visible_and_fail_open.
Invoked via bare ``python3`` (a process spawn comparable in cost to the ``sh -c``
one-liner this replaces), never through ``uv run --with "furl-ctx[...]"``, which
would add a dependency-resolution step on the interactive session-start path.

T7: below Claude Code 2.1.163, ``updatedToolOutput`` is confirmed silently
ignored (anthropics/claude-code#68951 class), so the OLD unconditional
"PostToolUse compression armed" clause overclaimed on old hosts. Because this
script cannot import ``furl_ctx.host_version`` (see above), the SAME cheap,
subprocess-free env-var detection is duplicated here in miniature — no
subprocess fallback, so on non-native installs (where neither env var is set)
this degrades to "unknown", which intentionally preserves the historical,
unconditional wording (cannot prove it is broken, so do not claim it is; see
furl_ctx/host_version.py's module docstring for the full rationale, which this
mirrors). Keep these two implementations in sync by hand if the floor version
or detection heuristic ever changes.
"""

from __future__ import annotations

import json
import os
import re
import sys

_STATUS_LINE_ENV = "FURL_STATUS_LINE"
_MIN_VERSION = (2, 1, 163)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Keep in sync with furl_ctx/host_version.py's release-please-managed pins.
_PLUGIN_VERSION = "1.3.2"
_ENGINE_VERSION = "1.3.0"


def _parse_version(text: str) -> tuple[int, int, int] | None:
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _detect_version() -> tuple[int, int, int] | None:
    """Cheap, subprocess-free detection — see the module docstring for why this
    duplicates (in miniature) furl_ctx.host_version._from_env."""
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH", "").strip()
    if execpath:
        version = _parse_version(os.path.basename(execpath))
        if version is not None:
            return version
    ai_agent = os.environ.get("AI_AGENT", "").strip()
    if ai_agent:
        version = _parse_version(ai_agent.replace("-", "."))
        if version is not None:
            return version
    return None


def _armed_clause(version: tuple[int, int, int] | None) -> str:
    """The PostToolUse compression clause: the historical claim when the floor
    is met OR the version is unknown (cannot prove it is broken), the honest
    degraded line when CONFIRMED below the floor."""
    if version is not None and version < _MIN_VERSION:
        floor = ".".join(str(part) for part in _MIN_VERSION)
        current = ".".join(str(part) for part in version)
        return f"PostToolUse compression requires Claude Code {floor} or newer, current version is {current}"
    return "PostToolUse compression armed"


def main() -> None:
    if os.environ.get(_STATUS_LINE_ENV) == "0":
        return

    message = (
        f"furl {_PLUGIN_VERSION} · engine furl-ctx {_ENGINE_VERSION}; "
        "PreToolUse pipe active, set FURL_PRETOOL_PIPE=0 to disable; "
        f"{_armed_clause(_detect_version())}; "
        "store: per-project under ~/.furl; verify: furl_stats"
    )
    sys.stdout.write(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    # Last-resort guard: no uncaught exception may ever reach the host.
    try:
        main()
    except Exception:
        pass
