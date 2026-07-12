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
import subprocess
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
_EVENTS = {"PostToolUse", "SessionStart"}

# The exact PostToolUse command the plugin ships — byte-identical to the pre-pin
# command except for the deterministic ``==<version>`` pin that stops ``uv`` from
# serving a stale cached resolution. Any other edit here must be deliberate.
_EXPECTED_COMMAND = (
    "sh -lc 'uv run --no-project --with "
    f'"furl-ctx[mcp]=={_LIB_VERSION}" '
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/compress_tool_output.py" || true\''
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
