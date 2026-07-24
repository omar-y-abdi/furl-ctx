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

The PreToolUse pipe clause is NOT static: it reports the pipe's LIVE status by
running the SAME Bash-permission-rule detection the pipe itself gates on, imported
from ``pretool_pipe`` so there is one source of truth and the banner's claim and the
pipe's behavior cannot drift. When any Bash rule exists the pipe is off, and the
banner now says so, naming the scopes and the ``FURL_PIPE_WITH_RULES`` opt-in,
replacing the old unconditional "pipe active" line that lied whenever a rule was
present. That detection is stdlib-only, so the banner stays dependency-free.
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
_ENGINE_VERSION = "1.3.2"


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


def _join_scopes(scopes: tuple[str, ...]) -> str:
    """Human list of scope labels with NO round brackets (the banner systemMessage
    is pinned AI-tell-free): 'a', 'a and b', 'a, b and c'."""
    if not scopes:
        return "settings"
    if len(scopes) == 1:
        return scopes[0]
    return ", ".join(scopes[:-1]) + " and " + scopes[-1]


def _pretool_pipe_clause_inner() -> str:
    """The LIVE PreToolUse-pipe status line, built from the SAME rule detection the
    pipe gates on, imported from ``pretool_pipe`` so banner and pipe cannot drift.
    Never uses round brackets or dashes (the banner is pinned AI-tell-free) and
    always names the ``FURL_PRETOOL_PIPE=0`` disable knob."""
    from pretool_pipe import _pipe_disabled, _pipe_with_rules_override, _scan_bash_rules

    if _pipe_disabled(os.environ.get("FURL_PRETOOL_PIPE")):
        return "PreToolUse pipe off, FURL_PRETOOL_PIPE=0 is set"

    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    scan = _scan_bash_rules(cwd)
    has_rules = scan.rule_count > 0

    if _pipe_with_rules_override(os.environ.get("FURL_PIPE_WITH_RULES")):
        if has_rules or scan.doubt:
            return (
                "PreToolUse pipe active via FURL_PIPE_WITH_RULES despite Bash "
                "permission rules, so rewrites may change how those rules match, "
                "set FURL_PRETOOL_PIPE=0 to disable"
            )
        return "PreToolUse pipe active, set FURL_PRETOOL_PIPE=0 to disable"

    if has_rules:
        word = "rule" if scan.rule_count == 1 else "rules"
        return (
            f"PreToolUse pipe off, {scan.rule_count} Bash permission {word} in "
            f"{_join_scopes(scan.scopes)} could be masked by a rewrite, set "
            "FURL_PIPE_WITH_RULES=1 to compress anyway or FURL_PRETOOL_PIPE=0 to keep it off"
        )
    if scan.doubt:
        return (
            "PreToolUse pipe off, Bash permission settings could not be read so it "
            "stays off for safety, set FURL_PIPE_WITH_RULES=1 to override or "
            "FURL_PRETOOL_PIPE=0 to keep it off"
        )
    return "PreToolUse pipe active, set FURL_PRETOOL_PIPE=0 to disable"


def _pretool_pipe_clause() -> str:
    """Fail-open wrapper: any problem determining the pipe status leaves ONE stderr
    breadcrumb (user-visible, never model-visible) and returns a conservative clause,
    so a detection error can never crash the banner or block a session start."""
    try:
        return _pretool_pipe_clause_inner()
    except Exception as exc:
        try:
            sys.stderr.write(
                f"furl: could not determine PreToolUse pipe status [{exc.__class__.__name__}]\n"
            )
        except Exception:
            pass
        return "PreToolUse pipe status unknown, set FURL_PRETOOL_PIPE=0 to disable"


def main() -> None:
    if os.environ.get(_STATUS_LINE_ENV) == "0":
        return

    message = (
        f"furl {_PLUGIN_VERSION} · engine furl-ctx {_ENGINE_VERSION}; "
        f"{_pretool_pipe_clause()}; "
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
