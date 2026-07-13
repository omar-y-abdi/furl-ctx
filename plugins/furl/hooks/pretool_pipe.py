#!/usr/bin/env python3
"""Furl PreToolUse hook (ON BY DEFAULT — disable with FURL_PRETOOL_PIPE=0): the
real-savings compression path that does NOT depend on PostToolUse
``updatedToolOutput`` (silently dropped by Claude Code >=2.1.163 —
anthropics/claude-code#68951).

Unless disabled, this rewrites a ``Bash`` command so its STDOUT is piped through
the Furl compressor (``pipe_compress.py``) BEFORE it becomes the tool result —
so the model-visible output IS the compressed form, with the original stored
under a ``<<ccr:HASH>>`` marker (retrievable via ``furl_retrieve``), exactly
like the PostToolUse path.

SMART DEFAULT (v10, user-approved): the pipe runs UNLESS ``FURL_PRETOOL_PIPE``
is EXPLICITLY falsy — ``0``/``false``/``off``/``no``/``disabled``
(case-insensitive, ALL whitespace removed). Unset, empty, and any other value —
including unknown junk — leave it ON ("on unless explicitly disabled", so a typo
never silently disables savings). Only ``Bash`` is touched.

CORE PROPERTY — PERMISSION-RULE SAFETY (reviewer-84 F3, non-negotiable): this
hook MUST NEVER rewrite a command that Claude Code would subject to a
permissions **deny** or **ask** rule. Claude Code evaluates those rules against
the REWRITTEN command, and the furl-pipe wrapper no longer matches
``Bash(verb:*)`` patterns — so rewriting a denied command would silently
downgrade a hard deny to "ask" (normal mode) or trip the obfuscation classifier
(auto mode). Before rewriting, the hook reads every ``permissions.deny`` /
``permissions.ask`` Bash rule it CAN see — project scope (``.claude/settings.json``
+ ``.claude/settings.local.json`` under BOTH ``$CLAUDE_PROJECT_DIR`` and the
payload cwd) and user scope (``~/.claude/settings.json`` +
``~/.claude/settings.local.json``) — and when in ANY doubt (unreadable or
malformed settings, unparseable command, compound command, glob rules it cannot
interpret, same-verb near-matches) it PASSES THROUGH: no rewrite, the original
runs, the deterministic rule fires. Fail toward no-compression, never toward
masking a permission rule; no-savings is acceptable, defeating a deny is not.
HONEST BLINDNESS: the hook cannot see CLI flags (``--permission-mode``,
``--disallowedTools``), enterprise managed policy, or session-state approvals.
A bare ``Bash`` deny/ask rule bounds that blindness (it passes everything
through); users relying on CLI/policy-level Bash restrictions should set
``FURL_PRETOOL_PIPE=0`` (documented in the plugin README).

Contract (PreToolUse):
  stdin  : JSON {tool_name, tool_input:{command, ...}, cwd, ...}
  stdout : to REWRITE, emit {"hookSpecificOutput": {"hookEventName":
           "PreToolUse", "updatedInput": {...tool_input, "command": <rewritten>}}}
  stdout empty + exit 0 : the original command runs unchanged.

The rewrite preserves the original command's EXIT CODE exactly. STDERR is never
captured and flows live — but because stdout is buffered for compression,
stderr/stdout interleaving is not preserved: in a merged view all stderr appears
before the (possibly compressed) stdout; ``cmd 2>&1`` merges both into the
compressed stream. Small outputs pass through raw (the compressor's own
threshold). FAIL-OPEN at the shell level twice over: a compressor that cannot
even start falls back to ``cat`` of the captured output, and if the stdout
tempfile cannot even be created the original command runs UNWRAPPED
(uncompressed, uncounted) — never a broken command.
FAIL-OPEN here too: any error emits nothing (exit 0) → original command runs.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

# The engine pin MUST match hooks.json's command pins (a test asserts this) so
# the compressor resolves the SAME furl-ctx the rest of the plugin uses.
_FURL_CTX_PIN = "furl-ctx[mcp]==1.2.0"

# Transparency marker: prepended to the rewritten command (visible in the
# transcript). Names the OPT-OUT since the pipe is on by default.
_PIPE_MARKER = "# furl-pipe (FURL_PRETOOL_PIPE=0 to disable)"

# Loop guard: the STABLE PREFIX of every marker version this plugin has ever
# emitted (old opt-in markers said "(FURL_PRETOOL_PIPE=1)"), so a command
# wrapped by ANY plugin version is never double-wrapped after an upgrade.
_PIPE_GUARD = "# furl-pipe"

_ENABLE_ENV = "FURL_PRETOOL_PIPE"

# The explicit opt-out set (S1 smart default). Shared semantics with the
# hooks.json shell gate — a parity test enumerates both.
_DISABLE_VALUES = frozenset({"0", "false", "off", "no", "disabled"})

# ASCII whitespace removal — the exact character class the shell gate's
# ``tr -d "[:space:]"`` deletes (POSIX locale), so both gates normalize
# identically even for values with INTERNAL whitespace (review-84 F1).
_WS_REMOVE = str.maketrans("", "", " \t\n\r\f\v")


def _pipe_disabled(raw: str | None) -> bool:
    """SMART DEFAULT (v10, user-approved): the pipe runs UNLESS explicitly
    disabled. True only for an explicit falsy value — 0/false/off/no/disabled,
    case-insensitive with ALL ASCII whitespace removed (so `` o f f `` is OFF,
    matching the shell gate's ``tr -d "[:space:]"`` exactly). Unset (None),
    empty, and any unrecognized value return False (pipe ON): "on unless
    explicitly disabled", so a typo like ``FURL_PRETOOL_PIPE=fasle`` never
    silently disables savings. SEMANTICALLY IDENTICAL to the hooks.json shell
    gate for every value (test_pretool_gate_parity_shell_and_python enumerates
    both, internal-whitespace cases included)."""
    if raw is None:
        return False
    return raw.translate(_WS_REMOVE).lower() in _DISABLE_VALUES


def _passthrough() -> None:
    """Emit nothing and succeed: the original command runs unchanged."""
    sys.exit(0)


# --- deny/ask-aware guard (reviewer-84 F3) ----------------------------------------
# Shell constructs that can introduce ANOTHER command. If any appears and any
# deny/ask Bash rule exists, the command is never parsed segment-by-segment —
# it passes through wholesale. ``&`` covers ``&&``, ``|`` covers ``||``.
_COMPOUND_MARKERS = ("\n", ";", "&", "|", "`", "$(", "<(", ">(")

# Glob metacharacters we refuse to interpret in a rule verb: a rule we cannot
# reason about is a rule that COULD match → passthrough.
_GLOB_CHARS = ("*", "?", "[")


def _settings_paths(cwd: str) -> tuple[Path, ...]:
    """The permission-rule sources this hook CAN see, in Claude Code order:
    project settings + project-local settings, then user scope. Project scope is
    read from BOTH ``CLAUDE_PROJECT_DIR`` (the session's project root, provided
    to every hook — where Claude Code actually loads project settings from) AND
    the payload cwd — they usually coincide, but when they differ (cwd in a
    subdirectory) the union is the conservative choice: more readable rules can
    only mean more passthrough, never a masked rule. CLI flags, enterprise
    managed policy, and session state remain invisible here — that blindness is
    documented and bounded by the bare-``Bash``-rule passthrough."""
    project_dirs: list[Path] = []
    project_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if project_root:
        project_dirs.append(Path(project_root))
    if Path(cwd) not in project_dirs:
        project_dirs.append(Path(cwd))
    scopes = [base / ".claude" for base in (*project_dirs, Path.home())]
    return tuple(
        scope / name for scope in scopes for name in ("settings.json", "settings.local.json")
    )


def _bash_bodies_from_entries(entries: object) -> tuple[list[str | None], bool]:
    """Collect the Bash-governing rule bodies from one deny/ask array.

    Returns ``(bodies, doubt)``: ``None`` in *bodies* is a BLANKET rule (a bare
    ``Bash`` or a ``Bash(...`` we cannot parse — either could govern anything).
    Rules for other tools (including ``BashOutput``, which merely shares the
    prefix) are irrelevant to a Bash rewrite and are skipped. Any shape we
    cannot read raises *doubt* instead of being guessed at."""
    bodies: list[str | None] = []
    if not isinstance(entries, list):
        return bodies, True
    doubt = False
    for entry in entries:
        if not isinstance(entry, str):
            doubt = True
            continue
        rule = entry.strip()
        if rule == "Bash":
            bodies.append(None)
        elif rule.startswith("Bash("):
            bodies.append(rule[5:-1] if rule.endswith(")") else None)
    return bodies, doubt


def _load_bash_rule_bodies(paths: tuple[Path, ...]) -> tuple[list[str | None], bool]:
    """Union of every deny/ask Bash rule body across *paths*.

    Precedence is irrelevant to a conservative union: a rule in ANY scope could
    gate the command, so all of them count. ``doubt`` is True when a source
    EXISTS but cannot be read or parsed (unreadable file, invalid JSON, wrong
    shapes) — the caller must pass through, because unknowable rules could
    contain a deny. A missing file is not doubt; it simply has no rules."""
    bodies: list[str | None] = []
    doubt = False
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            doubt = True
            continue
        try:
            data = json.loads(text)
        except ValueError:
            doubt = True
            continue
        if not isinstance(data, dict):
            doubt = True
            continue
        permissions = data.get("permissions")
        if permissions is None:
            continue
        if not isinstance(permissions, dict):
            doubt = True
            continue
        for key in ("deny", "ask"):
            if key not in permissions:
                continue
            found, entry_doubt = _bash_bodies_from_entries(permissions[key])
            bodies.extend(found)
            doubt = doubt or entry_doubt
    return bodies, doubt


def _rule_matches_command(command: str, verb: str, body: str | None) -> bool:
    """True whenever the rule COULD govern *command* — deliberately conservative.

    Over-matching only costs compression on one call; under-matching would mask
    a permission rule. Layers, any hit → match:
      * blanket (``None`` body, empty/pure-wildcard prefix) → always;
      * Claude Code's raw prefix semantics: ``Bash(P:*)`` governs commands
        starting with ``P`` (exact-form bodies are the degenerate no-args case);
      * verb backstop: a rule whose first word shares the command's verb (or a
        ``gi*``-style verb-prefix glob of it) could match some spelling of this
        command → same-verb commands are never rewritten;
      * a rule verb containing glob characters we cannot interpret → match."""
    if body is None:
        return True
    prefix = (body[:-2] if body.endswith(":*") else body).strip()
    if not prefix:
        return True
    if command.startswith(prefix):
        return True
    core = prefix.split()[0].rstrip("*")
    if not core or any(ch in core for ch in _GLOB_CHARS):
        return True
    return verb.startswith(core)


def _deny_guard_passthrough(command: str, bodies: list[str | None]) -> bool:
    """CORE PROPERTY decision: True → do NOT rewrite (see module docstring).

    Zero rules (the common fresh-install case) → False: nothing can be masked,
    every command rewrites, zero-config savings preserved. With rules present,
    passthrough on: any compound construct (never parsed segment-by-segment),
    an unparseable command, an env-assignment-obscured verb, or any rule that
    could match. Rewrite only when the command is a SIMPLE command whose verb
    confidently matches no deny/ask rule."""
    if not bodies:
        return False
    stripped = command.strip()
    if any(marker in stripped for marker in _COMPOUND_MARKERS):
        return True
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return True
    if not tokens:
        return True
    verb = tokens[0]
    if not verb or "=" in verb:
        # No discernible verb (e.g. `''`) or an env-assignment prefix hiding it.
        return True
    return any(_rule_matches_command(stripped, verb, body) for body in bodies)


def _rewrite_command(original: str, project_dir: str, compressor: str) -> str:
    """Build the exit-code-preserving, fail-open pipe rewrite of *original*.

    Design:
      * ``if [ -n "$f" ] && : >"$f"`` — review F1 guard: PROBE that the tempfile
        is actually creatable/writable BEFORE any capture redirect touches the
        original. If the probe fails (mktemp unavailable AND the ``${TMPDIR}``
        fallback path unwritable), the ``else`` branch runs the ORIGINAL COMMAND
        with no redirect — inside a SUBSHELL whose ``)`` sits on its own line
        (review R1), exactly like the then branch: a bare interpolation would let
        an original ending in an ODD number of trailing backslashes line-continue
        into ``fi``, making the WHOLE script a parse error (rc 2, command never
        runs in either branch). The subshell is the branch's last statement, so
        stdout and the exit code still flow through exactly (fail-open: no
        compression rather than no command). Pre-F1, the redirect sat unprobed on
        the subshell and a tempfile failure meant the command NEVER RAN.
      * ``( <orig>\\n) >"$f"`` — a SUBSHELL captures only stdout to the tempfile;
        the closing ``)`` on its own line survives an *orig* that ends in a
        comment/``&``/heredoc. STDERR is never redirected — it flows live — but
        since stdout is buffered here and emitted at the end, stderr/stdout
        interleaving is NOT preserved: merged views show all stderr before the
        (possibly compressed) stdout; ``2>&1`` merges into the compressed stream.
      * ``__furl_ec=$?`` right after captures the original's exact exit code
        (the subshell's = its last command's), restored by the final ``exit``.
      * the compressor reads the tempfile; ``|| cat "$f"`` is the shell-level
        fail-open — if the compressor cannot even start (no ``uv``/python), the
        RAW captured output is emitted, never lost.
      * ``FURL_CCR_PROJECT_DIR`` + ``FURL_CCR_BACKEND=sqlite`` are baked so the
        compressor writes the original into the SAME durable per-project store
        the MCP server reads (a memory store would make the marker unretrievable).
    """
    qdir = shlex.quote(project_dir)
    qcomp = shlex.quote(compressor)
    return (
        f"{_PIPE_MARKER}\n"
        "__furl_f=$(mktemp 2>/dev/null || mktemp -t furlpipe 2>/dev/null"
        ' || printf %s "${TMPDIR:-/tmp}/furl-pipe.$$")\n'
        # NOTE: ``2>/dev/null`` BEFORE ``>"$f"`` — redirections process left to
        # right, so suppression must be in place before the probe redirect can
        # fail, or the failure message would leak to the live stderr stream.
        'if [ -n "$__furl_f" ] && : 2>/dev/null >"$__furl_f"; then\n'
        f"( {original}\n"
        ') >"$__furl_f"\n'
        "__furl_ec=$?\n"
        f"FURL_CCR_PROJECT_DIR={qdir} FURL_CCR_BACKEND=sqlite "
        f'uv run --no-project --with "{_FURL_CTX_PIN}" python3 {qcomp} <"$__furl_f"'
        ' || cat "$__furl_f"\n'
        'rm -f "$__furl_f"\n'
        "exit $__furl_ec\n"
        "else\n"
        'rm -f "$__furl_f" 2>/dev/null\n'
        f"( {original}\n"
        ")\n"
        "fi"
    )


def _project_dir(payload: dict) -> str:
    """Resolve the project dir the SAME way the PostToolUse hook and MCP server
    do (CLAUDE_PROJECT_DIR -> payload cwd -> getcwd), so the pipe's CCR writes
    land in the store the MCP server reads."""
    cwd = payload.get("cwd")
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or (cwd if isinstance(cwd, str) and cwd.strip() else "")
        or os.getcwd()
    )


def main() -> None:
    # Opt-OUT gate FIRST (S1 smart default): an explicitly disabled pipe is a
    # byte-identical no-op — we never even parse stdin, zero added latency.
    if _pipe_disabled(os.environ.get(_ENABLE_ENV)):
        _passthrough()

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

    # Bash only (the matcher is Bash; double-check so a mis-scoped registration
    # can never rewrite another tool's input).
    if payload.get("tool_name") != "Bash":
        _passthrough()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _passthrough()
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        _passthrough()

    # Loop guard: never double-wrap a command we (any plugin version) rewrote —
    # matches the stable "# furl-pipe" prefix, not one marker spelling.
    if _PIPE_GUARD in command:
        _passthrough()

    # SECURITY GUARD (reviewer-84 F3): never rewrite a command a deny/ask rule
    # could govern — doubt of ANY kind (unreadable settings included) passes
    # through so the deterministic rule fires on the ORIGINAL command.
    raw_cwd = payload.get("cwd")
    guard_cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd.strip() else os.getcwd()
    bodies, doubt = _load_bash_rule_bodies(_settings_paths(guard_cwd))
    if doubt or _deny_guard_passthrough(command, bodies):
        _passthrough()

    compressor = str(Path(__file__).resolve().parent / "pipe_compress.py")
    rewritten = _rewrite_command(command, _project_dir(payload), compressor)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "command": rewritten},
        }
    }
    try:
        sys.stdout.write(json.dumps(output))
    except Exception:
        _passthrough()
    sys.exit(0)


if __name__ == "__main__":
    # Last-resort guard: no uncaught exception may reach the host — fail open to
    # the original command (emit nothing, exit 0).
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
