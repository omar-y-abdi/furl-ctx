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

Env contract: the manifest sets no FURL_* environment variables, neither a
per-hook ``env`` object (the loader ignores the field) nor an inline
``FURL_...=value`` assignment that would clobber a user's exported override such
as ``FURL_CCR_BACKEND=memory``. The two functional hooks APPEND common uv install
dirs to ``PATH`` (``PATH="$PATH:..."``) so ``uv`` resolves under ``sh -c`` without
a login shell; appending never overrides an existing entry. The CCR defaults
(FURL_CCR_BACKEND=sqlite, FURL_CCR_TTL_SECONDS=86400) are owned by
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
_PLUGIN_DIR = _ROOT / "plugins" / "furl"
_PLUGIN_HOOKS_DIR = _PLUGIN_DIR / "hooks"
_HOOKS_JSON = _PLUGIN_HOOKS_DIR / "hooks.json"
_HOOK_SCRIPT = _PLUGIN_HOOKS_DIR / "compress_tool_output.py"
_SESSION_START_SCRIPT = _PLUGIN_HOOKS_DIR / "session_start_banner.py"
_PYPROJECT = _ROOT / "pyproject.toml"

# T7: the two undocumented, native-installer-only env vars the module and the module both read. Every SessionStart test below scrubs them and
# sets an explicit value instead, so results are deterministic regardless of what Claude Code version, if any, is actually running the suite.
_VERSION_ENV_VARS = ("CLAUDE_CODE_EXECPATH", "AI_AGENT")
_ABOVE_FLOOR_ENV = {"AI_AGENT": "claude-code_2-1-212_agent"}
_BELOW_FLOOR_ENV = {"AI_AGENT": "claude-code_2-1-100_agent"}

# Hermetic scope for the SessionStart banner subprocess. The banner now reports the PreToolUse pipe's LIVE status by reading Bash permission rules from the same
# scopes the pipe gates on (HOME/.claude, CLAUDE_PROJECT_DIR, the cwd, managed settings), so these must be rule-free fresh dirs with the config-path env vars unset.
_BANNER_EMPTY_HOME = tempfile.mkdtemp(prefix="furl-manifest-banner-home-")
_BANNER_EMPTY_CWD = tempfile.mkdtemp(prefix="furl-manifest-banner-cwd-")
_BANNER_CONFIG_ENV_VARS = (
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
)

# The library version the PostToolUse `uv run --with` command pins to. Derived from pyproject so the expected command below
# never rots, and so a pin that drifts from the shipped library version fails here as well as in test_plugin_version_pins.py.
_LIB_VERSION = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

# The events Furl ships. The loader rejects unknown event keys ("Invalid key in
# record"), so pinning this set also guards against typo'd event names.
_EVENT = "PostToolUse"
_EVENTS = {"PostToolUse", "PreToolUse", "SessionStart"}

# uv install dirs appended to PATH (F-A1) so ``uv`` resolves under ``sh -c``
# without a login shell; appending never overrides an existing PATH entry.
_PATH_AUG = 'PATH="$PATH:$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin"'

# The exact PostToolUse command the plugin ships (F-A1). It runs via ``sh -c`` so NO login profile is sourced (a profile that
# prints to stdout would corrupt the JSON envelope). The ``==<version>`` pin stops ``uv`` from serving a stale cached resolution.
_EXPECTED_COMMAND = (
    "sh -c 'o=$(" + _PATH_AUG + " uv run --no-project --with "
    f'"furl-ctx[mcp]=={_LIB_VERSION}" '
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/compress_tool_output.py"); '
    'pre=${o%%"{"*}; case $o in *"{"*) printf %s "${o#"$pre"}" ;; '
    '*) printf %s "$o" ;; esac; true\''
)

# The PreToolUse pipe hook, ON BY DEFAULT (S1 smart default, user-approved). The shell gate is an OPT-OUT: an explicitly falsy
# FURL_PRETOOL_PIPE (0/false/off/no/disabled, normalized via ``tr`` lowercase + strip, review F3) skips the body cheaply (no ``uv`` resolve).
_EXPECTED_PRETOOL_COMMAND = (
    'sh -c \'case "$(printf %s "$FURL_PRETOOL_PIPE" | '
    'tr "[:upper:]" "[:lower:]" | tr -d "[:space:]")" in 0|false|off|no|disabled) ;; '
    "*) o=$(" + _PATH_AUG + " uv run --no-project --with "
    f'"furl-ctx[mcp]=={_LIB_VERSION}" '
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/pretool_pipe.py"); '
    'pre=${o%%"{"*}; case $o in *"{"*) printf %s "${o#"$pre"}" ;; '
    '*) printf %s "$o" ;; esac ;; esac; true\''
)

# Fields the schema does not honor where the old manifest wrongly placed them: the host silently ignores
# ``description``/``id`` at the matcher level and ``env`` per command hook. They must not reappear.
_FORBIDDEN_MATCHER_KEYS = {"description", "id"}
_FORBIDDEN_HOOK_KEYS = {"env"}


def _load() -> dict[str, Any]:
    return json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))


def _run(command: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # Execute the hook command exactly as Claude Code does — as a shell command line — but via an
    # explicit argv (no shell=True); the command string is the repo's own, not external input.
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_session_start(env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the SessionStart command exactly as Claude Code does: as the shell
    command line from hooks.json, with CLAUDE_PLUGIN_ROOT set (Claude Code sets
    it for every hook subprocess) so ``${CLAUDE_PLUGIN_ROOT}/hooks/...`` resolves.
    Scrubs the T7 version-detection env vars first so results never depend on
    whatever Claude Code version, if any, is actually running the test suite.

    Also runs hermetically for the banner's new PreToolUse-pipe status: a rule-free
    HOME and cwd, with the permission-config path vars unset, so the banner resolves
    to its default "pipe active" wording regardless of the developer's real
    ~/.claude Bash rules (the OFF/doubt/override wordings are pinned separately)."""
    command = _load()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    env = {"CLAUDE_PLUGIN_ROOT": str(_PLUGIN_DIR), "HOME": _BANNER_EMPTY_HOME}
    if env_extra:
        env.update(env_extra)
    full_env = dict(os.environ)
    for var in (*_VERSION_ENV_VARS, *_BANNER_CONFIG_ENV_VARS):
        full_env.pop(var, None)
    full_env.update(env)
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=_BANNER_EMPTY_CWD,
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
    # Do not pin CCR environment values in the manifest command; runtime rewriting adds them without clobbering user environment.
    assert "FURL_CCR" not in command


def test_pretool_explicit_disable_is_cheap_no_uv_no_output() -> None:
    # With an explicitly falsy flag the shell gate skips the body entirely: no output,
    # exit 0, and crucially NO `uv` process is spawned — disabling the pipe costs nothing.
    command = _load()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    for value in ("0", "false", "off", "no", "disabled", "OFF"):
        proc = _run(command, {"FURL_PRETOOL_PIPE": value})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "", f"flag {value!r} must skip the pipe cheaply"


# The uv-launch body inside the shipped PreToolUse command.
_PRETOOL_UV_BODY_RE = re.compile(
    r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" '
    r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/pretool_pipe\.py"'
)


def _pretool_gate_probe() -> str:
    command = _load()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    probe = _PRETOOL_UV_BODY_RE.sub("echo GATE-OPEN", command)
    assert "echo GATE-OPEN" in probe, f"probe substitution failed on: {command!r}"
    return probe


# The shell gate and Python `_pipe_disabled` gate must agree on every value. ON unless explicitly disabled
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
    # A misspelled falsy value must not disable the pipe; only recognized explicit values may opt out.
    ("fasle", True),
    ("2", True),  # non-1 numeric: not in _DISABLE_VALUES → ON
    ("On", True),  # mixed-case truthy
    (" 1", True),  # whitespace-padded truthy
    ("YES", True),
    # NB: "disabled" is falsy but "Enabled" is NOT its inverse — it is merely an
    # unrecognized value, and unrecognized means ON.
    ("Enabled", True),
    ("o f f", False),  # INTERNAL whitespace (F1): both gates remove it → falsy
    ("\tFALSE\n", False),  # mixed whitespace + case
    ("d i s a b l e d", False),
    ("g a r b a g e", True),  # collapsed junk is still junk → ON
)

_PRETOOL_SCRIPT = _PLUGIN_HOOKS_DIR / "pretool_pipe.py"

# Hermetic HOME + cwd for the python-gate subprocess: the deny/ask guard (reviewer-84 F3) reads
# permission rules from the payload cwd and HOME, and this parity test targets the FLAG gate only.
_EMPTY_SETTINGS_DIR = tempfile.mkdtemp(prefix="furl-manifest-tests-home-")


def _env_with_flag(value: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = _EMPTY_SETTINGS_DIR
    for _v in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MANAGED_SETTINGS_PATH"):
        env.pop(_v, None)  # hermetic: no ambient project/user/managed scope
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
    # Cheap: no `uv` resolve on the session-start path — bare python3, no dependency resolution (T7 made the banner
    # version-aware but it stayed dependency-free for exactly this reason; see the module's module docstring).
    assert "uv run" not in command
    assert "python3" in command
    assert "session_start_banner.py" in command
    assert "additionalContext" not in command
    # Honors the documented opt-out, cheaply (before even spawning python3).
    assert "FURL_STATUS_LINE" in command
    # Fail-open: the command always ends by exiting 0 so it can never block a session.
    assert command.rstrip().endswith("true'")
    # The systemMessage-emitting logic now lives in the script this command
    # invokes, not the command string itself.
    script = _SESSION_START_SCRIPT.read_text(encoding="utf-8")
    assert "systemMessage" in script


def test_session_start_emits_valid_system_message_json() -> None:
    proc = _run_session_start(_ABOVE_FLOOR_ENV)
    assert proc.returncode == 0, proc.stderr
    # Fails loudly if the JSON-in-Python escaping ever regresses.
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == {"systemMessage"}
    message = payload["systemMessage"]
    assert message.startswith("furl ")
    # Name the pinned engine alongside the plugin; `_LIB_VERSION` is the same project-version constant used by the hook pin checks.
    assert f"engine furl-ctx {_LIB_VERSION}" in message
    assert "furl_stats" in message
    assert "PostToolUse compression armed" in message


def test_session_start_opt_out_suppresses_line() -> None:
    proc = _run_session_start({"FURL_STATUS_LINE": "0"})
    assert proc.returncode == 0
    assert proc.stdout == ""


# --- T7: version-aware PostToolUse clause ---------------------------------------


def test_session_start_below_floor_replaces_armed_claim() -> None:
    proc = _run_session_start(_BELOW_FLOOR_ENV)
    assert proc.returncode == 0, proc.stderr
    message = json.loads(proc.stdout)["systemMessage"]
    assert "PostToolUse compression armed" not in message
    assert "PostToolUse compression requires Claude Code 2.1.163 or newer" in message
    assert "current version is 2.1.100" in message
    # The pipe is unaffected by this gate and must still be claimed as active.
    assert "PreToolUse pipe active" in message


def test_session_start_unknown_version_preserves_armed_claim() -> None:
    """Neither env var set (e.g. a non-native install): cannot prove the host is
    broken, so this must behave exactly like today (claim armed), never like
    "assume broken" — see furl_ctx/host_version.py's module docstring."""
    proc = _run_session_start(env_extra=None)
    assert proc.returncode == 0, proc.stderr
    message = json.loads(proc.stdout)["systemMessage"]
    assert "PostToolUse compression armed" in message


# F3: the banner reports the pipe's LIVE, rule-aware status -------------------- The old banner printed an unconditional
# "PreToolUse pipe active" that LIED whenever a Bash permission rule existed (the pipe gates itself off then).


def _no_ai_tells(text: str) -> bool:
    """No em-dash, en-dash, or round brackets — the banner systemMessage rule."""
    return not any(ch in text for ch in ("—", "–", "(", ")"))


def _banner_message(
    *,
    home_settings: dict[str, Any] | str | None = None,
    cwd_settings: dict[str, Any] | None = None,
    env_extra: dict[str, str] | None = None,
    above_floor: bool = True,
) -> str:
    """Render the SessionStart banner systemMessage hermetically, with HOME/.claude
    and the cwd carrying exactly the given Bash-permission settings and every
    config-path env var scrubbed, so the PreToolUse-pipe clause is deterministic."""
    home = Path(tempfile.mkdtemp(prefix="furl-banner-home-"))
    work = Path(tempfile.mkdtemp(prefix="furl-banner-cwd-"))
    if home_settings is not None:
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        text = home_settings if isinstance(home_settings, str) else json.dumps(home_settings)
        (home / ".claude" / "settings.json").write_text(text, encoding="utf-8")
    if cwd_settings is not None:
        (work / ".claude").mkdir(parents=True, exist_ok=True)
        (work / ".claude" / "settings.json").write_text(json.dumps(cwd_settings), encoding="utf-8")
    command = _load()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    env = {
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_DIR),
        "HOME": str(home),
        "AI_AGENT": (_ABOVE_FLOOR_ENV if above_floor else _BELOW_FLOOR_ENV)["AI_AGENT"],
    }
    if env_extra:
        env.update(env_extra)
    full_env = dict(os.environ)
    for var in (*_VERSION_ENV_VARS, *_BANNER_CONFIG_ENV_VARS):
        full_env.pop(var, None)
    full_env.update(env)
    proc = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, text=True, env=full_env, cwd=str(work)
    )
    assert proc.returncode == 0, proc.stderr
    message = str(json.loads(proc.stdout)["systemMessage"])
    # Every path stays AI-tell-free and keeps the version prefix + disable knob.
    assert _no_ai_tells(message), f"banner has an AI tell: {message!r}"
    assert message.startswith(f"furl {_LIB_VERSION} · engine furl-ctx {_LIB_VERSION}")
    assert "FURL_PRETOOL_PIPE=0" in message
    assert "furl_stats" in message
    return message


def test_banner_pipe_off_names_rule_count_scope_and_optin() -> None:
    """One user-scope Bash deny rule: pipe OFF, and the banner says so, naming the
    count, the scope, and the FURL_PIPE_WITH_RULES opt-in — never 'pipe active'."""
    msg = _banner_message(home_settings={"permissions": {"deny": ["Bash(rm:*)"]}})
    assert "PreToolUse pipe active" not in msg
    assert "PreToolUse pipe off" in msg
    assert "1 Bash permission rule in user settings" in msg
    assert "FURL_PIPE_WITH_RULES=1" in msg


def test_banner_pipe_off_counts_multiple_rules_and_scopes() -> None:
    """Count is the total across deny/ask/allow; allow counts too (allowlist posture)."""
    msg = _banner_message(
        home_settings={
            "permissions": {"deny": ["Bash(rm:*)", "Bash(curl:*)"], "allow": ["Bash(ls:*)"]}
        }
    )
    assert "3 Bash permission rules in user settings" in msg


def test_banner_pipe_off_on_settings_doubt() -> None:
    """Unreadable settings are doubt: the pipe stays off for safety and the banner
    reports that honestly instead of claiming active."""
    msg = _banner_message(home_settings="{ definitely not json")
    assert "PreToolUse pipe active" not in msg
    assert "could not be read" in msg


def test_banner_override_reported_with_tradeoff() -> None:
    """FURL_PIPE_WITH_RULES: the pipe rewrites despite rules, and the banner reports
    the conscious override plus its trade-off, not a bare 'active'."""
    msg = _banner_message(
        home_settings={"permissions": {"deny": ["Bash(rm:*)"]}},
        env_extra={"FURL_PIPE_WITH_RULES": "1"},
    )
    assert "active via FURL_PIPE_WITH_RULES" in msg
    assert "may change how those rules match" in msg


def test_banner_pipe_off_when_explicitly_disabled() -> None:
    msg = _banner_message(
        home_settings={"permissions": {"deny": ["Bash(rm:*)"]}},
        env_extra={"FURL_PRETOOL_PIPE": "0"},
    )
    assert "PreToolUse pipe off, FURL_PRETOOL_PIPE=0 is set" in msg


def test_banner_pipe_active_when_zero_rules() -> None:
    """The savings case: zero readable Bash rules -> the banner claims the pipe
    active, matching the pipe's own zero-rules rewrite behavior."""
    msg = _banner_message(home_settings={"permissions": {"deny": [], "ask": [], "allow": []}})
    assert "PreToolUse pipe active" in msg
    assert "pipe off" not in msg
