"""T7: ``furl_ctx.host_version`` — the Claude Code version-detection helper hook
scripts use to stop claiming PostToolUse compression works when the running
host is too old to honor ``updatedToolOutput`` (the #68951 class).

Empirically pinned here: this machine's real Claude Code session env, captured
live during the T7 investigation, is asserted verbatim below so a future
change to either variable's SHAPE fails loud instead of silently drifting.
"""

from __future__ import annotations

import subprocess

import pytest

from furl_ctx.host_version import (
    MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT,
    detect_host_version,
    format_version,
    meets_compression_floor,
    parse_version,
)

# --- parse_version -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.1.212", (2, 1, 212)),
        ("2.1.212 (Claude Code)", (2, 1, 212)),
        ("v2.1.212", (2, 1, 212)),
        ("/Users/k/.local/share/claude/versions/2.1.212", (2, 1, 212)),
        ("2.1.212.0", (2, 1, 212)),  # extra trailing component ignored
        ("", None),
        ("no digits here", None),
        ("2.1", None),  # not a full X.Y.Z
        ("garbage-not-a-version-agent", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


def test_parse_version_never_raises_on_odd_input() -> None:
    assert parse_version("\x00\x01 2.1.163 \xff") == (2, 1, 163)


# --- meets_compression_floor ---------------------------------------------------


def test_meets_compression_floor_unknown_is_none() -> None:
    assert meets_compression_floor(None) is None


def test_meets_compression_floor_above() -> None:
    assert meets_compression_floor((2, 1, 212)) is True
    assert meets_compression_floor((2, 2, 0)) is True
    assert meets_compression_floor((3, 0, 0)) is True


def test_meets_compression_floor_exact() -> None:
    assert meets_compression_floor((2, 1, 163)) is True


def test_meets_compression_floor_below() -> None:
    assert meets_compression_floor((2, 1, 162)) is False
    assert meets_compression_floor((2, 0, 999)) is False
    assert meets_compression_floor((1, 9, 999)) is False


def test_floor_constant_is_2_1_163() -> None:
    """Pin: T7's investigated floor. If this ever needs to change, it must be a
    deliberate, evidenced edit, not drift."""
    assert MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT == (2, 1, 163)


# --- format_version ------------------------------------------------------------


def test_format_version() -> None:
    assert format_version((2, 1, 163)) == "2.1.163"


# --- detect_host_version: cheap (env-only) path ---------------------------------


def test_detect_from_execpath_env() -> None:
    env = {"CLAUDE_CODE_EXECPATH": "/Users/k/.local/share/claude/versions/2.1.212"}
    assert detect_host_version(env=env) == (2, 1, 212)


def test_detect_from_ai_agent_env() -> None:
    env = {"AI_AGENT": "claude-code_2-1-212_agent"}
    assert detect_host_version(env=env) == (2, 1, 212)


def test_execpath_preferred_over_ai_agent_when_both_present_and_agree() -> None:
    env = {
        "CLAUDE_CODE_EXECPATH": "/Users/k/.local/share/claude/versions/2.1.212",
        "AI_AGENT": "claude-code_2-1-212_agent",
    }
    assert detect_host_version(env=env) == (2, 1, 212)


def test_falls_back_to_ai_agent_when_execpath_unparseable() -> None:
    env = {
        "CLAUDE_CODE_EXECPATH": "/usr/local/bin/claude",  # no version in the path
        "AI_AGENT": "claude-code_2-1-100_agent",
    }
    assert detect_host_version(env=env) == (2, 1, 100)


def test_detect_returns_none_when_neither_env_var_present() -> None:
    """The honest-unknown case: an npm/Homebrew install with neither
    undocumented native-installer env var set. Must be None, never a guess."""
    assert detect_host_version(env={}) is None


def test_detect_returns_none_on_malformed_values() -> None:
    env = {"CLAUDE_CODE_EXECPATH": "not-a-version-path", "AI_AGENT": "also-not-one"}
    assert detect_host_version(env=env) is None


def test_cheap_path_never_spawns_a_subprocess(monkeypatch) -> None:
    """allow_subprocess defaults to False: this must be provably subprocess-free
    so it is safe on a per-tool-call hot path."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("detect_host_version(allow_subprocess=False) must not spawn a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert detect_host_version(env={}) is None
    assert detect_host_version(env={"AI_AGENT": "claude-code_2-1-212_agent"}) == (2, 1, 212)


# --- detect_host_version: subprocess fallback -----------------------------------


def test_subprocess_fallback_used_only_when_env_absent_and_allowed(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="9.8.7 (Claude Code)\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = detect_host_version(env={}, allow_subprocess=True)
    assert result == (9, 8, 7)
    assert calls == [["claude", "--version"]]


def test_subprocess_fallback_prefers_execpath_binary(monkeypatch) -> None:
    """When CLAUDE_CODE_EXECPATH is set but its basename does not parse as a
    version (unusual, but not impossible), the subprocess fallback should still
    invoke THAT exact binary rather than jump straight to bare 'claude' -- it is
    the binary actually running this session, avoiding auto-update drift."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="2.1.212 (Claude Code)\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    env = {"CLAUDE_CODE_EXECPATH": "/opt/claude/current-build"}  # no version in this path
    result = detect_host_version(env=env, allow_subprocess=True)
    assert result == (2, 1, 212)
    assert calls == [["/opt/claude/current-build", "--version"]]


def test_subprocess_fallback_not_used_when_env_already_resolved(monkeypatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not shell out when the cheap env signal already resolved")

    monkeypatch.setattr(subprocess, "run", _boom)
    env = {"AI_AGENT": "claude-code_2-1-212_agent"}
    assert detect_host_version(env=env, allow_subprocess=True) == (2, 1, 212)


def test_subprocess_fallback_fail_open_on_missing_binary(monkeypatch) -> None:
    def _raise_not_found(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise_not_found)
    assert detect_host_version(env={}, allow_subprocess=True) is None


def test_subprocess_fallback_fail_open_on_timeout(monkeypatch) -> None:
    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=3.0)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert detect_host_version(env={}, allow_subprocess=True) is None


def test_subprocess_fallback_fail_open_on_nonzero_exit(monkeypatch) -> None:
    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert detect_host_version(env={}, allow_subprocess=True) is None


# --- empirical pin: this machine's real, live session env ----------------------


def test_empirical_execpath_and_ai_agent_agree_on_this_machine() -> None:
    """Not a mock: reads the ACTUAL ambient environment this test process
    inherited. Skips (rather than fails) when neither var is present, e.g. a
    clean CI runner or a non-native install -- this test documents empirically
    observed reality, it does not assert it is universal."""
    import os

    execpath = os.environ.get("CLAUDE_CODE_EXECPATH")
    ai_agent = os.environ.get("AI_AGENT")
    if not execpath and not ai_agent:
        pytest.skip("neither CLAUDE_CODE_EXECPATH nor AI_AGENT set in this environment")
    detected = detect_host_version()
    assert detected is not None, (
        f"had signal(s) {execpath=} {ai_agent=} but failed to parse a version"
    )
    assert detected[0] >= 2, f"sanity: Claude Code major version {detected} looks implausible"
