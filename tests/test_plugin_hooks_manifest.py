"""Regression guard for the Furl plugin hooks manifest (plugins/furl/hooks/hooks.json).

Claude Code's plugin loader validates hooks.json against its hooks schema: event
handlers MUST be wrapped in a top-level ``"hooks"`` record keyed by event name.
Shipping the bare ``{"PostToolUse": [...]}`` shape (no wrapper) made the loader
reject the file with ``hooks: Invalid input: expected record, received undefined``
and silently disabled the hook. These tests pin the valid shape and the exact
runtime behavior (matcher, command, timeout) so it cannot recur.

Furl ships two events: the ``PostToolUse`` compression hook (pinned to a deterministic
library version so ``uv`` cannot serve a stale cached resolution) and a ``SessionStart``
status signal. The status signal is a cheap static line — no ``uv`` resolve — emitted as
a ``systemMessage`` JSON field so it reaches the user's eyes without spending model
context (per the Claude Code hooks docs, ``systemMessage`` is shown to the user and is
not added to Claude's context, unlike raw stdout / ``additionalContext``). It is
fail-open (always exits 0) and honors the ``FURL_STATUS_LINE=0`` opt-out.

Env contract: the manifest sets NO environment variables — neither a per-hook
``env`` object (the loader ignores the field) nor inline ``VAR=value`` assignments
in the command (``VAR=x cmd`` sets the child env unconditionally, which would
override a user's exported values such as ``FURL_CCR_BACKEND=memory``). The CCR
defaults (FURL_CCR_BACKEND=sqlite, FURL_CCR_TTL_SECONDS=86400) are owned by
compress_tool_output.py via ``os.environ.setdefault``, which honors user overrides.

Pure JSON/text checks plus a stdlib subprocess round-trip of the status line — no
furl_ctx import — so the guard runs even without the built extension.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import tomllib

_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_HOOKS_DIR = _ROOT / "plugins" / "furl" / "hooks"
_HOOKS_JSON = _PLUGIN_HOOKS_DIR / "hooks.json"
_HOOK_SCRIPT = _PLUGIN_HOOKS_DIR / "compress_tool_output.py"
_PYPROJECT = _ROOT / "pyproject.toml"

# The library version the PostToolUse `uv run --with` command pins to. Derived from
# pyproject so the expected command below never rots, and so a pin that drifts from the
# shipped library version fails here as well as in test_plugin_version_pins.py.
_LIB_VERSION = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

# The events Furl ships. The loader rejects unknown event keys ("Invalid key in
# record"), so pinning this set also guards against typo'd event names.
_EVENT = "PostToolUse"
_EVENTS = {"PostToolUse", "PreToolUse", "SessionStart"}

# The exact PostToolUse command the plugin ships — byte-identical to the pre-pin
# command except for the deterministic ``==<version>`` pin that stops ``uv`` from
# serving a stale cached resolution. Any other edit here must be deliberate.
_EXPECTED_COMMAND = (
    "sh -lc 'uv run --no-project --with "
    f'"furl-ctx[mcp]=={_LIB_VERSION}" '
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/compress_tool_output.py" || true\''
)

# The PreToolUse pipe hook — ON BY DEFAULT (S1 smart default, user-approved).
# The shell gate is an OPT-OUT: an explicitly falsy FURL_PRETOOL_PIPE
# (0/false/off/no/disabled, normalized via ``tr`` lowercase + strip, review F3)
# skips the body cheaply (no ``uv`` resolve); unset, empty, and ANY other value
# — including unknown junk like "garbage" — launch the rewrite ("on unless
# explicitly disabled"). Bash-only. Any edit here must be deliberate.
_EXPECTED_PRETOOL_COMMAND = (
    'sh -lc \'case "$(printf %s "$FURL_PRETOOL_PIPE" | '
    'tr "[:upper:]" "[:lower:]" | tr -d "[:space:]")" in 0|false|off|no|disabled) ;; *) '
    "uv run --no-project --with "
    f'"furl-ctx[mcp]=={_LIB_VERSION}" '
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/pretool_pipe.py" ;; esac; true\''
)

# Fields the schema does not honor where the old manifest wrongly placed them:
# the host silently ignores ``description``/``id`` at the matcher level and ``env``
# per command hook. They must not reappear.
_FORBIDDEN_MATCHER_KEYS = {"description", "id"}
_FORBIDDEN_HOOK_KEYS = {"env"}


def _load() -> dict[str, Any]:
    return json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))


def _run(command: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # Execute the hook command exactly as Claude Code does — as a shell command line —
    # but via an explicit argv (no shell=True); the command string is the repo's own,
    # not external input.
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env=env,
    )


def test_top_level_is_hooks_record() -> None:
    manifest = _load()
    # The wrapper the loader requires; the bare {"PostToolUse": ...} shape is the bug.
    assert set(manifest.keys()) == {"hooks"}
    assert "PostToolUse" not in manifest
    assert isinstance(manifest["hooks"], dict)


def test_events_are_known_and_arrays() -> None:
    events = _load()["hooks"]
    assert set(events.keys()) == _EVENTS
    for name, groups in events.items():
        assert name in _EVENTS  # only known events are shipped
        assert isinstance(groups, list)
        assert groups


def test_matcher_groups_reject_forbidden_keys() -> None:
    for group in _load()["hooks"][_EVENT]:
        assert set(group.keys()) <= {"matcher", "hooks"}
        assert not (_FORBIDDEN_MATCHER_KEYS & set(group.keys()))
        assert isinstance(group["hooks"], list)
        assert group["hooks"]


def test_command_hooks_are_well_formed_without_env() -> None:
    for group in _load()["hooks"][_EVENT]:
        for hook in group["hooks"]:
            assert hook["type"] == "command"
            assert isinstance(hook["command"], str)
            assert hook["command"]
            assert not (_FORBIDDEN_HOOK_KEYS & set(hook.keys()))


def test_runtime_behavior_preserved() -> None:
    group = _load()["hooks"][_EVENT][0]
    assert group["matcher"] == "Bash|WebFetch|WebSearch|Task"
    hook = group["hooks"][0]
    assert hook["timeout"] == 30
    command = hook["command"]
    assert command == _EXPECTED_COMMAND
    # No inline env pins: `VAR=x cmd` would clobber a user's exported override
    # (e.g. FURL_CCR_BACKEND=memory). Defaults belong to the script's setdefault.
    assert "FURL_CCR" not in command
    # Still invokes the bundled hook script via the plugin-root placeholder.
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/compress_tool_output.py" in command


def test_env_defaults_owned_by_hook_script_setdefault() -> None:
    # The user-overridable defaults must stay in the script; the manifest carries
    # none. Together with test_runtime_behavior_preserved this pins the contract.
    src = _HOOK_SCRIPT.read_text(encoding="utf-8")
    assert 'os.environ.setdefault("FURL_CCR_BACKEND", "sqlite")' in src
    assert 'os.environ.setdefault("FURL_CCR_TTL_SECONDS", "86400")' in src


# --- PreToolUse pipe hook (on by default; FURL_PRETOOL_PIPE=0 disables) -----------


def test_pretool_group_shape() -> None:
    groups = _load()["hooks"]["PreToolUse"]
    assert len(groups) == 1
    group = groups[0]
    assert set(group.keys()) <= {"matcher", "hooks"}
    assert not (_FORBIDDEN_MATCHER_KEYS & set(group.keys()))
    # Bash-only: the pipe rewrites a command's stdout, so only Bash is in scope.
    assert group["matcher"] == "Bash"
    assert isinstance(group["hooks"], list) and group["hooks"]
    for hook in group["hooks"]:
        assert hook["type"] == "command"
        assert isinstance(hook["command"], str) and hook["command"]
        assert not (_FORBIDDEN_HOOK_KEYS & set(hook.keys()))


def test_pretool_command_is_env_gated_and_pinned() -> None:
    hook = _load()["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["timeout"] == 30
    command = hook["command"]
    assert command == _EXPECTED_PRETOOL_COMMAND
    # Opt-OUT shell gate (S1): explicit falsy skips cheaply; the value is
    # normalized (F3) so the gate matches python's _pipe_disabled set exactly.
    assert '"$FURL_PRETOOL_PIPE"' in command
    assert 'tr "[:upper:]" "[:lower:]"' in command
    # Invokes the bundled rewrite script via the plugin-root placeholder.
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/pretool_pipe.py" in command
    # No inline env pins on the hooks.json command itself (the rewrite bakes the
    # CCR env at runtime; the manifest command must not clobber user env).
    assert "FURL_CCR" not in command


def test_pretool_explicit_disable_is_cheap_no_uv_no_output() -> None:
    # With an explicitly falsy flag the shell gate skips the body entirely: no
    # output, exit 0, and crucially NO `uv` process is spawned — disabling the
    # pipe costs nothing. (The DEFAULT path now launches the rewrite hook; that
    # side is exercised via the uv-free gate probe in the parity test below.)
    command = _load()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    for value in ("0", "false", "off", "no", "disabled", "OFF"):
        proc = _run(command, {"FURL_PRETOOL_PIPE": value})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "", f"flag {value!r} must skip the pipe cheaply"


# The uv-launch body inside the shipped PreToolUse command. Swapped for a marker
# echo by the gate-probe tests below, so the GATE itself (the part before the
# body) is exercised from the shipped string without a real `uv` resolve.
_PRETOOL_UV_BODY_RE = re.compile(
    r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" '
    r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/pretool_pipe\.py"'
)


def _pretool_gate_probe() -> str:
    command = _load()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    probe = _PRETOOL_UV_BODY_RE.sub("echo GATE-OPEN", command)
    assert "echo GATE-OPEN" in probe, f"probe substitution failed on: {command!r}"
    return probe


# The gate-parity contract (S1 + review-84 F1): the hooks.json SHELL gate and
# the python ``_pipe_disabled`` gate must agree on EVERY value. ON unless
# explicitly disabled: unset, empty, truthy spellings, and unknown junk all
# leave the pipe ON; only the normalized falsy set turns it off. Both gates
# remove ALL whitespace (the shell's ``tr -d "[:space:]"``, python's ASCII
# whitespace-removal table) before comparing — INTERNAL whitespace included —
# so "semantically identical" holds for every value, not just whitespace-free
# ones.
_GATE_PARITY_CASES: tuple[tuple[str | None, bool], ...] = (
    (None, True),  # unset → ON (the S1 smart default; pre-flip this was OFF)
    ("", True),  # empty → ON
    ("0", False),
    ("false", False),
    ("OFF", False),  # case-insensitive falsy
    (" no ", False),  # whitespace-stripped falsy
    ("disabled", False),  # now an EXPLICIT falsy (pre-flip it was merely unrecognized)
    ("1", True),
    ("TRUE", True),
    ("garbage", True),  # unknown non-falsy → ON ("on unless explicitly disabled")
    ("o f f", False),  # INTERNAL whitespace (F1): both gates remove it → falsy
    ("\tFALSE\n", False),  # mixed whitespace + case
    ("d i s a b l e d", False),
    ("g a r b a g e", True),  # collapsed junk is still junk → ON
)

_PRETOOL_SCRIPT = _PLUGIN_HOOKS_DIR / "pretool_pipe.py"

# Hermetic HOME + cwd for the python-gate subprocess: the deny/ask guard
# (reviewer-84 F3) reads permission rules from the payload cwd and HOME, and
# this parity test targets the FLAG gate only — a developer's real ~/.claude
# deny rules must not turn its rewrites into passthroughs.
_EMPTY_SETTINGS_DIR = tempfile.mkdtemp(prefix="furl-manifest-tests-home-")


def _env_with_flag(value: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = _EMPTY_SETTINGS_DIR
    env.pop("CLAUDE_PROJECT_DIR", None)  # hermetic: no ambient project scope
    env.pop("FURL_PRETOOL_PIPE", None)  # true UNSET for the None case
    if value is not None:
        env["FURL_PRETOOL_PIPE"] = value
    return env


def _shell_gate_enabled(value: str | None) -> bool:
    proc = subprocess.run(
        ["/bin/sh", "-c", _pretool_gate_probe()],
        capture_output=True,
        text=True,
        env=_env_with_flag(value),
    )
    assert proc.returncode == 0, proc.stderr
    return "GATE-OPEN" in proc.stdout


def _python_gate_enabled(value: str | None) -> bool:
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "cwd": _EMPTY_SETTINGS_DIR,
        }
    )
    proc = subprocess.run(
        [sys.executable, str(_PRETOOL_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=_env_with_flag(value),
    )
    assert proc.returncode == 0, proc.stderr
    return "updatedInput" in proc.stdout


def test_pretool_gate_parity_shell_and_python() -> None:
    """S1 pin: BOTH gates implement the same opt-out semantics over the full
    enumeration — unset, empty, falsy spellings (case/whitespace variants),
    truthy spellings, and unknown junk. Pre-flip, unset/empty/'garbage' were OFF
    in both gates and 'disabled' was ON, so this fails on pre-flip code."""
    for value, expected_on in _GATE_PARITY_CASES:
        assert _shell_gate_enabled(value) is expected_on, f"shell gate disagrees on {value!r}"
        assert _python_gate_enabled(value) is expected_on, f"python gate disagrees on {value!r}"


# --- SessionStart status signal ---------------------------------------------------


def test_session_start_group_shape() -> None:
    for group in _load()["hooks"]["SessionStart"]:
        assert set(group.keys()) <= {"matcher", "hooks"}
        assert not (_FORBIDDEN_MATCHER_KEYS & set(group.keys()))
        assert isinstance(group["hooks"], list)
        assert group["hooks"]
        for hook in group["hooks"]:
            assert hook["type"] == "command"
            assert isinstance(hook["command"], str)
            assert hook["command"]
            assert not (_FORBIDDEN_HOOK_KEYS & set(hook.keys()))


def test_session_start_is_cheap_user_visible_and_fail_open() -> None:
    command = _load()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # Cheap: no `uv` resolve on the session-start path (a static printed line).
    assert "uv run" not in command
    # User-visible without model-context cost: a systemMessage JSON field (shown to the
    # user, not injected as context) rather than raw stdout or additionalContext.
    assert "systemMessage" in command
    assert "additionalContext" not in command
    # Honors the documented opt-out.
    assert "FURL_STATUS_LINE" in command
    # Fail-open: the command always ends by exiting 0 so it can never block a session.
    assert command.rstrip().endswith("true'")


def test_session_start_emits_valid_system_message_json() -> None:
    command = _load()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    proc = _run(command)
    assert proc.returncode == 0, proc.stderr
    # Fails loudly if the JSON-in-shell escaping ever regresses.
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == {"systemMessage"}
    message = payload["systemMessage"]
    assert message.startswith("furl ")
    # Names the pinned engine alongside the plugin (see test_plugin_version_pins.py
    # for the full plugin-version / engine-version cross-check); _LIB_VERSION is the
    # same pyproject-derived constant the PostToolUse pin check above uses.
    assert f"engine furl-ctx {_LIB_VERSION}" in message
    assert "furl_stats" in message


def test_session_start_opt_out_suppresses_line() -> None:
    command = _load()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    proc = _run(command, {"FURL_STATUS_LINE": "0"})
    assert proc.returncode == 0
    assert proc.stdout == ""
