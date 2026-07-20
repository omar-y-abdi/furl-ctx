"""Best-effort Claude Code HOST version detection, for hook-side version gating.

Why this exists (T7 / the anthropics/claude-code#68951 class): Claude Code
started schema-validating a PostToolUse hook's ``updatedToolOutput`` at some
point and silently keeping the ORIGINAL output on any mismatch, rather than
applying a bare-string replacement unconditionally. ``compress_tool_output.py``
mirrors the tool's output shape to satisfy that validation, but that fix only
helps on hosts new enough to DO the validation-and-apply in the first place —
:data:`MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT` is the first release this
project has empirically confirmed applies a schema-matching replacement rather
than silently dropping the hook's output. Below that floor the PostToolUse hook
can produce a compressed replacement but Claude Code never shows it to the
model, so callers that assert "compression is armed/active" without checking
the running version are making a claim they cannot back up.

Investigated for this fix (see the PR description for the full writeup):
Claude Code's hooks documentation does not expose the running version anywhere
in a hook's stdin JSON payload, and there is no documented, version-stable
environment variable carrying it either — ``CLAUDECODE=1`` is a bare presence
flag, not a version. Two UNDOCUMENTED environment variables were observed
empirically to carry it for the native (curl-installed) distribution only:
``CLAUDE_CODE_EXECPATH`` (the running binary's own path, e.g.
``.../versions/2.1.212``) and ``AI_AGENT`` (e.g. ``claude-code_2-1-212_agent``).
Neither is documented, and neither is expected to be set for an npm-global or
Homebrew install — so :func:`detect_host_version` returns ``None`` (genuinely
unknown, never a guess) whenever they are absent. The only OFFICIALLY
documented way to obtain the version is the ``--version`` CLI flag itself; this
module uses it only as an opt-in fallback (``allow_subprocess=True``) because
spawning a process is too costly to do unconditionally on a hot per-tool-call
path — callers on a hot path should leave it disabled and treat ``None`` as
"cannot prove either way", never as "assume broken" or "assume working".
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping

# First release this project has empirically confirmed applies a PostToolUse
# updatedToolOutput replacement that mirrors the tool's output schema, rather
# than silently keeping the original. See the module docstring.
MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT = (2, 1, 163)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Undocumented, native-installer-only env vars — see the module docstring for
# what was and was not verifiable. Tried in this order; either shape can be
# missing or malformed independently, so both are attempted before giving up.
_EXECPATH_ENV = "CLAUDE_CODE_EXECPATH"
_AI_AGENT_ENV = "AI_AGENT"


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Extract the first ``X.Y.Z`` run of digits from *text*, or ``None``.

    Total and pure: never raises, matches anywhere in *text* (so both
    ``"2.1.212"`` and ``"claude-code_2-1-212_agent".replace("-", ".")`` style
    inputs work once dashes are normalized to dots by the caller), and ignores
    any trailing prerelease/build suffix.
    """
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _from_env(env: Mapping[str, str]) -> tuple[int, int, int] | None:
    """Cheap, subprocess-free detection from the two observed native-installer
    env vars. Returns ``None`` when neither is present or parseable — this is
    the expected, non-error outcome for any non-native install."""
    execpath = (env.get(_EXECPATH_ENV) or "").strip()
    if execpath:
        version = parse_version(os.path.basename(execpath))
        if version is not None:
            return version
    ai_agent = (env.get(_AI_AGENT_ENV) or "").strip()
    if ai_agent:
        version = parse_version(ai_agent.replace("-", "."))
        if version is not None:
            return version
    return None


def _from_subprocess(exe: str, *, timeout: float) -> tuple[int, int, int] | None:
    """Run ``exe --version`` and parse its output. Fail-open: any error (not
    found, times out, non-zero exit, unparseable output) returns ``None``."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed args, no shell, caller-provided exe
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return parse_version(result.stdout)


def detect_host_version(
    *,
    allow_subprocess: bool = False,
    env: Mapping[str, str] | None = None,
    subprocess_timeout: float = 3.0,
) -> tuple[int, int, int] | None:
    """Best-effort running Claude Code version, or ``None`` when it cannot be
    determined. Never raises.

    Cheap path (always tried first): the native-installer env vars via
    :func:`_from_env` — no subprocess, safe for a per-tool-call hot path.

    ``allow_subprocess=True`` additionally falls back to invoking
    ``--version``: first on the exact binary named by ``CLAUDE_CODE_EXECPATH``
    when present (the binary that is actually running THIS session — avoids
    the drift risk of a bare ``claude`` PATH lookup resolving to a DIFFERENT,
    possibly auto-updated install), then on bare ``claude`` from PATH as a
    last resort (the only option left for a non-native install). Only use this
    on an infrequent call site (once per session, an on-demand diagnostic
    tool) — each attempt spawns a whole CLI process.
    """
    source = os.environ if env is None else env
    try:
        version = _from_env(source)
        if version is not None or not allow_subprocess:
            return version
        execpath = (source.get(_EXECPATH_ENV) or "").strip()
        if execpath:
            version = _from_subprocess(execpath, timeout=subprocess_timeout)
            if version is not None:
                return version
        return _from_subprocess("claude", timeout=subprocess_timeout)
    except Exception:
        return None


def meets_compression_floor(
    version: tuple[int, int, int] | None,
    floor: tuple[int, int, int] = MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT,
) -> bool | None:
    """Whether *version* meets *floor*, or ``None`` when *version* is ``None``
    (unknown -- cannot prove either way). Callers MUST treat ``None`` as its
    own state, not coerce it to ``True``/``False`` -- see the module docstring
    on why "unknown" is never the same as "assume broken" or "assume working".
    """
    if version is None:
        return None
    return version >= floor


def format_version(version: tuple[int, int, int]) -> str:
    """Render a version tuple as ``"X.Y.Z"``."""
    return ".".join(str(part) for part in version)


__all__ = [
    "MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT",
    "detect_host_version",
    "format_version",
    "meets_compression_floor",
    "parse_version",
]
