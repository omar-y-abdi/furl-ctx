"""Pinning tests for the hook audit fixes (workstream A).

One pin per finding, all exercised as real subprocesses (the repo's hook-testing
style), so the shell wrappers are proven, not just inspected:

* F-A1 (CRITICAL): the two functional hooks run via ``sh -c`` (no login profile
  sourced), resolve ``uv`` by appending common install dirs to PATH, and strip
  any non-JSON prefix from the captured stdout. A profile that prints noise to
  stdout no longer corrupts the JSON envelope.
* F-A2: a piped command KILLED mid-run still delivers its partial stdout (the
  buffered capture is flushed by an INT/TERM trap).
* F-A3: the rewrite is mktemp-only, so no predictable-name tempfile (symlink
  race) is ever created; a missing mktemp fails open to the unwrapped command.
* F-A4: the first-run note and the statusline systemMessage are accurate and
  carry no em-dashes, en-dashes, or round brackets.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HOOKS = _ROOT / "plugins" / "furl" / "hooks"
_HOOKS_JSON = _HOOKS / "hooks.json"
_PRETOOL = _HOOKS / "pretool_pipe.py"
_MCP_JSON = _ROOT / "plugins" / "furl" / ".mcp.json"

# Hermetic scope for every hook subprocess: the pretool deny/ask guard reads
# permission rules from the payload cwd and HOME, and these pins target the
# zero-rules path, so both must be rule-free fresh dirs.
_EMPTY_HOME = tempfile.mkdtemp(prefix="furl-audit-home-")
_NO_RULES_CWD = tempfile.mkdtemp(prefix="furl-audit-cwd-")

# The PostToolUse uv body, swapped for a stub so the wrapper (shell) is exercised
# without a real uv resolve.
_POST_UV_BODY_RE = re.compile(
    r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" '
    r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/compress_tool_output\.py"'
)

_ENVELOPE = '{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"C"}}'


def _hook_command(event: str) -> str:
    hooks = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    return str(hooks[event][0]["hooks"][0]["command"])


def _inner_script(command: str) -> str:
    """The body inside ``sh -c '...'`` (the shipped commands quote their body in
    single quotes and use only double quotes inside). Running the body directly
    lets a stub that itself uses single quotes stand in for the uv invocation
    without colliding with the wrapper's quoting; the ``sh -c`` vs ``sh -lc``
    choice is then made by how the test invokes it."""
    assert command.startswith("sh -c '") and command.endswith("'"), command[:12]
    return command[len("sh -c '") : -1]


def _load_counters_module() -> object:
    """Load _furl_ccr_counters.py from its file path without polluting sys.path."""
    spec = importlib.util.spec_from_file_location(
        "_furl_ccr_counters_audit", _HOOKS / "_furl_ccr_counters.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rewrite(command: str) -> str:
    """The rewritten Bash command pretool_pipe.py emits for *command* (pipe on)."""
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": _NO_RULES_CWD}
    )
    env = {**os.environ, "FURL_PRETOOL_PIPE": "1", "HOME": _EMPTY_HOME}
    for _v in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_MANAGED_SETTINGS_PATH"):
        env.pop(_v, None)
    proc = subprocess.run(
        [sys.executable, str(_PRETOOL)], input=payload, capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"]


def _no_ai_tells(text: str) -> bool:
    """No em-dash, en-dash, or round brackets (the AI-tell-free prose rule)."""
    return not any(ch in text for ch in ("—", "–", "(", ")"))


# --- F-A1: profile-safe invocation ------------------------------------------------


def test_faA1_functional_hooks_use_non_login_shell() -> None:
    """Root-cause fix: neither functional hook uses ``sh -lc``. A login shell
    sources profiles that may print to stdout and corrupt the JSON envelope."""
    for event in ("PostToolUse", "PreToolUse"):
        command = _hook_command(event)
        assert command.startswith("sh -c "), f"{event} is not sh -c: {command[:12]!r}"
        assert "sh -lc" not in command, f"{event} still uses a login shell"


def test_faA1_functional_hooks_resolve_uv_via_path_append() -> None:
    """uv is resolved by APPENDING common install dirs to PATH (never a login
    shell, never an override of the user's PATH), so it is found under sh -c."""
    for event in ("PostToolUse", "PreToolUse"):
        command = _hook_command(event)
        assert 'PATH="$PATH:' in command, f"{event} must append, not replace, PATH"
        for uv_dir in ("$HOME/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"):
            assert uv_dir in command, f"{event} missing uv dir {uv_dir}"


def test_faA1_profile_noise_does_not_corrupt_envelope() -> None:
    """PIN: a ~/.profile that prints to stdout must NOT corrupt the emitted
    envelope. sh -c (shipped) yields clean parseable JSON; the old sh -lc would
    source the profile and prepend noise (fail-before, guarded for shells that do
    not source ~/.profile for -lc)."""
    home = Path(tempfile.mkdtemp(prefix="furl-audit-noisyhome-"))
    (home / ".profile").write_text("echo NVM_PROFILE_NOISE\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(home)}
    stub = "printf %s " + shlex.quote(_ENVELOPE)
    inner = _POST_UV_BODY_RE.sub(lambda _m: stub, _inner_script(_hook_command("PostToolUse")))

    # pass-after: sh -c does not source the profile -> clean parseable JSON.
    out = subprocess.run(["/bin/sh", "-c", inner], capture_output=True, text=True, env=env).stdout
    assert json.loads(out) == json.loads(_ENVELOPE), f"sh -c envelope corrupted: {out!r}"

    # fail-before: sh -lc sources the profile and prepends noise to stdout.
    login_out = subprocess.run(
        ["/bin/sh", "-lc", inner], capture_output=True, text=True, env=env
    ).stdout
    if "NVM_PROFILE_NOISE" in login_out:
        # The login shell sourced the profile here; prove the envelope IS corrupted.
        try:
            json.loads(login_out)
            corrupted = False
        except ValueError:
            corrupted = True
        assert corrupted, f"expected sh -lc to corrupt the envelope, got {login_out!r}"


def test_faA1_strip_rescues_nonjson_prefix_within_capture() -> None:
    """Defense in depth: even a stray line emitted by the uv body itself (not the
    profile) is stripped, so the JSON envelope still parses."""
    noisy = "printf %s " + shlex.quote("stray-warning\n" + _ENVELOPE)
    inner = _POST_UV_BODY_RE.sub(lambda _m: noisy, _inner_script(_hook_command("PostToolUse")))
    out = subprocess.run(["/bin/sh", "-c", inner], capture_output=True, text=True).stdout
    assert json.loads(out) == json.loads(_ENVELOPE), f"non-JSON prefix not stripped: {out!r}"


# --- F-A2: partial output survives a kill -----------------------------------------


def _wait_for_descendant_sleep(pgid: int, argv_tail: str = "30", timeout: float = 5.0) -> None:
    """Block until a ``sleep <argv_tail>`` process is running in *pgid*.

    The rewritten script executes ``trap ...; trap ...; ( printf ...; sleep 30 )``
    strictly in that order, so once ``sleep`` is observably running, both traps
    are already installed. Polling for this beats a fixed pre-kill sleep: a
    hardcoded delay has to outguess process fork/exec latency, and under heavy
    parallel load (many concurrent pytest/cargo workers) that latency can eat
    the whole margin, delivering the signal before the trap exists and losing
    the very output the test is pinning (flaky, load-dependent failures with no
    change in the code under test). Polling instead observes the actual
    precondition, so the kill fires at the same logical point every time.

    Portable across Linux and macOS via ``pgrep -g <pgid> -f <pattern>`` rather
    than a /proc scan (macOS has no /proc). The pattern is ANCHORED
    (``^sleep 30$``), not a bare substring: pgrep ``-f`` matches the full
    argument list of every process in the group, and that includes the wrapper
    ``/bin/sh -c '<script>'`` process itself, whose one argv element IS the
    script source, which contains the literal substring "sleep 30" as TEXT
    before the real ``sleep`` binary has ever been exec'd. An unanchored
    pattern therefore matches that wrapper on the very first poll and returns
    immediately, before the trap is actually installed, silently defeating the
    whole readiness check. Confirmed on macOS with a throwaway process: an
    unanchored ``pgrep -g <pgid> -f "sleep 30"`` matched both the wrapper shell
    and the real sleep process; the anchored pattern matched only the real one.
    """
    deadline = time.monotonic() + timeout
    pattern = f"^sleep {re.escape(argv_tail)}$"
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["pgrep", "-g", str(pgid), "-f", pattern], capture_output=True, text=True
        )
        if proc.returncode == 0:
            return
        time.sleep(0.005)
    raise TimeoutError(f"no 'sleep {argv_tail}' descendant appeared in pgid {pgid}")


def _kill_midrun_stdout(script: str, sig: int = signal.SIGTERM) -> str:
    """Run *script*, wait until it has actually entered its sleep (so any trap
    the script installs beforehand is guaranteed active), signal the whole
    group, and return whatever stdout was delivered."""
    proc = subprocess.Popen(
        ["/bin/sh", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    _wait_for_descendant_sleep(pgid)
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    out, _ = proc.communicate(timeout=20)
    return out


def test_faA2_killed_pipe_delivers_partial_stdout() -> None:
    """PIN: a piped command killed mid-run still delivers its partial stdout. The
    fail-before is the SAME rewrite with the trap lines removed (pre-fix shape),
    which loses the buffered output entirely."""
    rewritten = _rewrite("printf 'PARTIAL_SURVIVES\\n'; sleep 30")
    assert "trap " in rewritten, "the F-A2 signal trap must be present"

    # pass-after: the trap flushes the captured partial stdout.
    assert "PARTIAL_SURVIVES" in _kill_midrun_stdout(rewritten)

    # fail-before: strip every trap line -> the pre-fix wrapper loses the output.
    pre_fix = "\n".join(
        line for line in rewritten.splitlines() if not line.lstrip().startswith("trap")
    )
    assert "PARTIAL_SURVIVES" not in _kill_midrun_stdout(pre_fix), (
        "pre-fix wrapper should have lost the partial output"
    )


def test_faA2_killed_pipe_delivers_partial_stdout_on_sigint() -> None:
    """The trap covers SIGINT too (interactive Ctrl-C / kill -INT)."""
    rewritten = _rewrite("printf 'INT_PARTIAL\\n'; sleep 30")
    assert "INT_PARTIAL" in _kill_midrun_stdout(rewritten, sig=signal.SIGINT)


# --- F-A3: tempfile is mktemp-only (0600, no predictable name) ---------------------


def test_faA3_rewrite_is_mktemp_only_no_predictable_name() -> None:
    """PIN: the predictable ``/tmp/furl-pipe.$$`` fallback (created under the
    default umask, symlink-race prone) is gone; only mktemp (unique, 0600,
    atomic) creates the capture tempfile."""
    rewritten = _rewrite("echo hi")
    assert "mktemp" in rewritten
    assert "furl-pipe.$$" not in rewritten, "predictable tempfile name must be gone"
    assert "${TMPDIR" not in rewritten, "predictable ${TMPDIR} fallback must be gone"


def test_faA3_missing_mktemp_fails_open_no_predictable_file(tmp_path) -> None:
    """With mktemp unavailable the wrapper creates NO file and runs the command
    UNWRAPPED (fail-open) — the 0600 posture holds because the only file-creating
    path is mktemp itself."""
    rewritten = _rewrite("echo HELLO_UNWRAPPED")
    bash = shutil.which("bash")
    assert bash is not None
    shim = tmp_path / "empty-bin"  # no mktemp on PATH
    shim.mkdir()
    tdir = tmp_path / "tmp"  # writable TMPDIR: the OLD code would drop a file here
    tdir.mkdir()
    proc = subprocess.run(
        [bash, "-c", rewritten],
        capture_output=True,
        text=True,
        env={"PATH": str(shim), "TMPDIR": str(tdir)},
    )
    assert proc.stdout.strip() == "HELLO_UNWRAPPED", "command must still run unwrapped"
    assert list(tdir.iterdir()) == [], "no predictable furl tempfile may be created"


# --- F-A4: note + statusline accuracy and AI-tell-free prose -----------------------


def test_faA4_first_run_note_is_ai_tell_free_and_accurate() -> None:
    """PIN: the first-run note carries no em/en dashes or round brackets, states
    the true behavior (honored for the shapes furl mirrors; the counter counts
    replacements PRODUCED, not proven delivered), and keeps the diagnostic
    substrings the counter tests rely on."""
    note = str(_load_counters_module().FIRST_RUN_NOTE)  # type: ignore[attr-defined]
    assert _no_ai_tells(note), f"note has an em/en dash or round bracket: {note!r}"
    assert "shapes furl mirrors" in note, "note must scope honoring to mirrored shapes"
    assert "replacements produced" in note and "not" in note
    assert "proven delivered" in note, "note must clarify produced != delivered"
    assert "anthropics/claude-code#68951" in note
    assert "FURL_PRETOOL_PIPE=0" in note


def test_faA4_statusline_systemmessage_is_ai_tell_free() -> None:
    """PIN: the SessionStart systemMessage carries no em/en dashes or round
    brackets, keeps the version string the pin regex needs, and stays accurate."""
    command = _hook_command("SessionStart")
    env = {k: v for k, v in os.environ.items() if k != "FURL_STATUS_LINE"}
    out = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=env).stdout
    message = json.loads(out)["systemMessage"]
    assert _no_ai_tells(message), f"statusline has an AI tell: {message!r}"
    assert message.startswith("furl 1.3.2 · engine furl-ctx 1.3.0"), "version string must survive"
    assert "FURL_PRETOOL_PIPE=0" in message and "furl_stats" in message


# --- MCP server launch: the F-A1 profile-safe fix, applied to .mcp.json -----------


def _mcp_launch_args() -> list[str]:
    return list(json.loads(_MCP_JSON.read_text(encoding="utf-8"))["mcpServers"]["furl"]["args"])


def test_mcp_launch_uses_non_login_shell_and_path_append() -> None:
    """PIN: the stdio MCP server launches under ``sh -c`` (never ``sh -lc``) and
    resolves ``uv`` by APPENDING the same install dirs the hooks use to PATH. The
    server speaks JSON-RPC on stdout, so a login shell that sourced a profile
    printing to stdout would corrupt the stream before the initialize handshake:
    the SAME class of bug the functional hooks fixed. This fails against the
    pre-fix ``.mcp.json`` shape, which is ``-lc`` with no PATH append."""
    args = _mcp_launch_args()
    assert "-lc" not in args, f"MCP launch still uses a login shell: {args!r}"
    assert "-c" in args, f"MCP launch must use sh -c: {args!r}"
    command = " ".join(args)
    assert 'PATH="$PATH:' in command, "MCP launch must append, not replace, PATH"
    for uv_dir in ("$HOME/.local/bin", "$HOME/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin"):
        assert uv_dir in command, f"MCP launch missing uv dir {uv_dir}"
    assert "exec " in command, "MCP launch must exec so the server is the foreground stdio process"
