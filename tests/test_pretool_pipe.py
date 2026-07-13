"""PreToolUse pipe (on by default): the rewrite hook (pretool_pipe.py) + the
compressor (pipe_compress.py).

This is the real-savings path that does NOT depend on PostToolUse
``updatedToolOutput`` (dropped by Claude Code >=2.1.163, #68951): it rewrites a
Bash command so its stdout is compressed at the SOURCE. The load-bearing
invariants pinned here:

* ON BY DEFAULT (S1) — unset/empty/unknown values run the pipe; only an explicit
  falsy FURL_PRETOOL_PIPE (0/false/off/no/disabled, case-insensitive) disables
  it, and disabled is a byte-identical no-op.
* the rewrite preserves the ORIGINAL command's EXIT CODE exactly (proved with a
  stub compressor so the property is isolated to the shell wrapper); stderr is
  never captured and flows live — but stdout is buffered, so stderr/stdout
  interleaving is NOT preserved (pinned as documented behavior below); small
  output passes through raw; FAIL-OPEN twice over — when the compressor cannot
  start (``|| cat`` of the captured output) AND when the stdout tempfile cannot
  even be created (review F1: the original command must run UNWRAPPED, never be
  fail-closed).
* the compressor shrinks large output, stores the original under a retrievable
  ``<<ccr:HASH>>`` marker in the shared per-project store, and is byte-safe +
  fail-open on binary / undecodable / furl_ctx-missing input.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks"
_PRETOOL = _HOOKS / "pretool_pipe.py"
_COMPRESSOR = _HOOKS / "pipe_compress.py"

# Hermetic settings scope for every hook subprocess: the deny/ask guard
# (reviewer-84 F3, tests/test_pretool_deny_guard.py) reads permission rules from
# the payload cwd and HOME. These tests target the ZERO-RULES path, so both must
# be rule-free fresh dirs regardless of the developer's real ~/.claude.
_EMPTY_HOME = tempfile.mkdtemp(prefix="furl-pipe-tests-home-")
_NO_RULES_CWD = tempfile.mkdtemp(prefix="furl-pipe-tests-cwd-")

_UV_PREFIX_RE = re.compile(r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" python3')
_COMPRESSOR_SEG_RE = re.compile(
    r"FURL_CCR_PROJECT_DIR=\S+ FURL_CCR_BACKEND=sqlite "
    r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" python3 \S+'
)


def _rewrite(command: str, cwd: str) -> str:
    """Return the rewritten command pretool_pipe.py emits for *command* (flag on)."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
    env = {**os.environ, "FURL_PRETOOL_PIPE": "1", "HOME": _EMPTY_HOME}
    env.pop("CLAUDE_PROJECT_DIR", None)  # hermetic: no ambient project scope
    proc = subprocess.run(
        [sys.executable, str(_PRETOOL)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"]


def _run(command: str, env_extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=env)


def _with_stub_compressor(rewritten: str, stub: str) -> str:
    """Replace the whole compressor invocation with *stub* — isolates the shell
    wrapper's exit-code / stderr / fail-open behavior from any real compression."""
    return _COMPRESSOR_SEG_RE.sub(stub, rewritten)


def _with_local_compressor(rewritten: str) -> str:
    """Swap ``uv run --with <pin> python3`` for the test's own interpreter so the
    REAL compressor runs against the built local furl_ctx — no network, no uv."""
    return _UV_PREFIX_RE.sub(shlex.quote(sys.executable), rewritten)


# --- pretool_pipe.py: the gate + rewrite emission --------------------------------


def _pretool(payload: dict, flag: str | None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": _EMPTY_HOME}
    env.pop("FURL_PRETOOL_PIPE", None)
    env.pop("CLAUDE_PROJECT_DIR", None)  # hermetic: no ambient project scope
    if flag is not None:
        env["FURL_PRETOOL_PIPE"] = flag
    return subprocess.run(
        [sys.executable, str(_PRETOOL)],
        input=json.dumps({"cwd": _NO_RULES_CWD, **payload}),
        capture_output=True,
        text=True,
        env=env,
    )


def test_pipe_on_by_default_unset_env_rewrites() -> None:
    """S1 pin (SMART DEFAULT, user-approved): with FURL_PRETOOL_PIPE UNSET the
    pipe must REWRITE — on unless explicitly disabled. Pre-flip, unset meant the
    opt-in OFF default (empty stdout), so this fails on pre-flip code."""
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag=None)
    assert proc.returncode == 0
    assert "updatedInput" in proc.stdout, "unset env must leave the pipe ON"


def test_pipe_on_for_empty_and_unknown_values() -> None:
    """S1 pin: empty and UNKNOWN non-falsy values leave the pipe ON ("on unless
    explicitly disabled") — a typo like 'fasle' must not silently disable
    savings. Pre-flip these were all OFF."""
    for value in ("", "garbage", "fasle", "2"):
        proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag=value)
        assert proc.returncode == 0
        assert "updatedInput" in proc.stdout, f"{value!r} must leave the pipe ON"


def test_pipe_explicit_falsy_disables() -> None:
    """The documented falsy set — 0/false/off/no/disabled, case-insensitive,
    whitespace-stripped — is the ONLY way to turn the pipe off. (A guard, not a
    flip pin: these values were also off pre-flip, then as unrecognized values;
    now they are the explicit opt-out set.)"""
    for value in ("0", "false", "OFF", " no ", "disabled"):
        proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag=value)
        assert proc.returncode == 0 and proc.stdout == "", f"flag {value!r} must disable"


def test_enabled_rewrites_bash_with_transparent_marker() -> None:
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag="1")
    assert proc.returncode == 0
    new_cmd = json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    # Transcript-visible marker names the OPT-OUT (the pipe is on by default).
    assert new_cmd.startswith("# furl-pipe (FURL_PRETOOL_PIPE=0 to disable)")
    assert "pipe_compress.py" in new_cmd
    assert "furl-ctx[mcp]==" in new_cmd  # pinned engine
    assert "exit $__furl_ec" in new_cmd  # exit-code preservation


def test_enabled_but_non_bash_passthrough() -> None:
    proc = _pretool({"tool_name": "WebFetch", "tool_input": {"command": "echo hi"}}, flag="1")
    assert proc.returncode == 0 and proc.stdout == ""


def test_already_wrapped_not_double_wrapped() -> None:
    wrapped = "# furl-pipe (FURL_PRETOOL_PIPE=0 to disable)\necho already"
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": wrapped}}, flag="1")
    assert proc.returncode == 0 and proc.stdout == ""


def test_old_marker_commands_still_loop_guarded() -> None:
    """Compat: the loop guard matches the stable '# furl-pipe' PREFIX, so a
    command wrapped by an OLDER plugin version (marker '(FURL_PRETOOL_PIPE=1)')
    re-run after upgrade is still never double-wrapped."""
    wrapped = "# furl-pipe (FURL_PRETOOL_PIPE=1)\necho already"
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": wrapped}}, flag="1")
    assert proc.returncode == 0 and proc.stdout == ""


def test_empty_command_passthrough() -> None:
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "   "}}, flag="1")
    assert proc.returncode == 0 and proc.stdout == ""


def test_rewrite_is_valid_shell(tmp_path) -> None:
    rewritten = _rewrite("echo hi && ls", str(tmp_path))
    check = subprocess.run(["bash", "-n", "-c", rewritten], capture_output=True, text=True)
    assert check.returncode == 0, f"rewrite is not valid shell: {check.stderr}"


# --- shell wrapper: exit code, stderr, fail-open (stub compressor) ---------------


def test_exit_code_zero_preserved(tmp_path) -> None:
    cmd = _with_stub_compressor(_rewrite("echo out; true", str(tmp_path)), "cat")
    proc = _run(cmd)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "out"


def test_exit_code_nonzero_preserved(tmp_path) -> None:
    cmd = _with_stub_compressor(_rewrite("echo out; exit 42", str(tmp_path)), "cat")
    assert _run(cmd).returncode == 42


def test_failing_command_keeps_its_code(tmp_path) -> None:
    cmd = _with_stub_compressor(_rewrite("echo hi; false", str(tmp_path)), "cat")
    proc = _run(cmd)
    assert proc.returncode == 1
    assert proc.stdout.strip() == "hi"


def test_stderr_flows_live_on_its_own_stream(tmp_path) -> None:
    """stderr is never captured: its CONTENT arrives intact on the stderr stream
    (ordering relative to stdout is a separate, documented property — see
    ``test_stderr_stdout_interleaving_not_preserved`` below)."""
    cmd = _with_stub_compressor(
        _rewrite("echo to-out; echo to-err >&2; exit 3", str(tmp_path)), "cat"
    )
    proc = _run(cmd)
    assert proc.returncode == 3
    assert proc.stdout.strip() == "to-out"
    assert proc.stderr.strip() == "to-err"


def test_stderr_stdout_interleaving_not_preserved(tmp_path) -> None:
    """Review F2 — pinned DOCUMENTED BEHAVIOR, not a bug: stdout is buffered to
    the tempfile and emitted at the end while stderr flows live, so in a MERGED
    view ALL stderr precedes ALL stdout (bare bash would interleave A, MIDERR, B).
    If a future change alters this ordering, it must be deliberate — and the docs
    (README/SKILL/LIBRARY 'interleaving is not preserved') updated with it."""
    cmd = _with_stub_compressor(_rewrite("echo A; echo MIDERR >&2; echo B", str(tmp_path)), "cat")
    proc = subprocess.run(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge, as a terminal view would
        text=True,
        env={**os.environ},
    )
    assert proc.returncode == 0
    assert proc.stdout.split() == ["MIDERR", "A", "B"]  # stderr first, then stdout


def test_fail_open_when_compressor_cannot_start(tmp_path) -> None:
    """A compressor that cannot even run must fall back to the RAW captured
    output (the shell-level ``|| cat``), never a lost/broken command."""
    cmd = _with_stub_compressor(
        _rewrite("echo raw-line; exit 5", str(tmp_path)), "/nonexistent/furl/compressor"
    )
    proc = _run(cmd)
    assert proc.returncode == 5  # original exit code still preserved
    assert proc.stdout.strip() == "raw-line"  # raw output survived


def test_fail_open_when_tempfile_unavailable(tmp_path) -> None:
    """Review F1 pin: if the stdout tempfile cannot be created (mktemp unavailable
    on a stripped PATH AND the ``${TMPDIR}`` fallback path unwritable), the
    ORIGINAL command must still run — unwrapped — with stdout intact and its exit
    code exact. The pre-fix rewrite put the capture redirect on the subshell with
    no probe, so a tempfile failure meant the command NEVER RAN (fail-closed:
    rc=1, empty stdout — reproduced in review)."""
    rewritten = _rewrite("echo HELLO_MUST_RUN; exit 9", str(tmp_path))
    bash = shutil.which("bash")
    assert bash is not None
    shim = tmp_path / "empty-bin"  # no mktemp, no rm, no cat — bash builtins only
    shim.mkdir()
    proc = subprocess.run(
        [bash, "-c", rewritten],
        capture_output=True,
        text=True,
        env={"PATH": str(shim), "TMPDIR": str(tmp_path / "no-such-dir")},
    )
    assert proc.returncode == 9, f"original exit code lost (stderr: {proc.stderr!r})"
    assert proc.stdout.strip() == "HELLO_MUST_RUN", "original command must still run"
    # The fallback must be silent plumbing: every wrapper-internal failure
    # (mktemp lookup, probe redirect, rm) is stderr-suppressed, so the user sees
    # only the original command's own streams.
    assert proc.stderr == "", f"wrapper noise leaked to stderr: {proc.stderr!r}"


def _bare_bash(command: str) -> subprocess.CompletedProcess[str]:
    """Reference behavior: *command* run by bash directly, no wrapper."""
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


def _run_forced_fallback(rewritten: str, tmp_path) -> subprocess.CompletedProcess[str]:
    """Run *rewritten* in the tempfile-unavailable env (mktemp absent on a
    stripped PATH, ``${TMPDIR}`` fallback unwritable) — forces the else branch."""
    bash = shutil.which("bash")
    assert bash is not None
    shim = tmp_path / "fallback-empty-bin"
    shim.mkdir(exist_ok=True)
    return subprocess.run(
        [bash, "-c", rewritten],
        capture_output=True,
        text=True,
        env={"PATH": str(shim), "TMPDIR": str(tmp_path / "no-such-dir")},
    )


def test_odd_trailing_backslashes_survive_both_branches(tmp_path) -> None:
    """Review R1 pin: a command ending in an ODD number of trailing backslashes
    line-continues onto the next wrapper line. Pre-R1, the else branch
    interpolated the ORIGINAL bare, so the continuation glued into ``fi`` →
    unterminated ``if`` → the WHOLE rewritten script was a parse error (rc 2,
    empty output — the command never ran in EITHER branch, both die at parse).
    The else branch wraps the original in a subshell exactly like the then
    branch: the ``)`` on its own line absorbs the continuation, and the subshell
    is the branch's last statement so the exit code still flows exactly.

    PLATFORM NOTE — do NOT "fix" this back to a bare-bash stdout comparison:
    ``bash -c 'echo foo \\'`` (odd trailing backslash at END OF INPUT) is
    bash-version-dependent — GNU bash 5 (Linux/CI) keeps the dangling backslash
    literal (``foo \\``), macOS bash 3.2 drops it (``foo``) — so a bare stdout
    reference fails on one platform or the other. The WRAPPED result is
    deterministic on every version because the wrapper structurally eliminates
    the ambiguous backslash-at-EOF: the interpolated original is always followed
    by a newline and the ``)`` line, making the odd trailing backslash an
    unambiguous POSIX line continuation. n=1 → ``foo\\n``; n=3 → the leading
    backslash PAIR is an ordinary escape (one literal backslash) and only the
    third continues, → ``foo \\\\n``."""
    wrapped_expected = {1: "foo\n", 3: "foo \\\n"}
    for n, expected_stdout in wrapped_expected.items():
        original = "echo foo " + "\\" * n
        rewritten = _rewrite(original, str(tmp_path))
        # Normal branch (tempfile fine; stub compressor = cat → raw passthrough).
        normal = _run(_with_stub_compressor(rewritten, "cat"))
        assert normal.returncode == 0, f"n={n}: normal rc (stderr: {normal.stderr!r})"
        assert normal.stdout == expected_stdout, f"n={n}: normal-branch stdout"
        # Forced-fallback branch (tempfile unavailable → subshell-wrapped original).
        fallback = _run_forced_fallback(rewritten, tmp_path)
        assert fallback.returncode == 0, f"n={n}: fallback rc (stderr: {fallback.stderr!r})"
        assert fallback.stdout == expected_stdout, f"n={n}: fallback stdout"


def test_even_trailing_backslashes_stay_correct(tmp_path) -> None:
    """Even-count guard: two trailing backslashes are an escaped literal
    backslash (no line continuation) — both branches must match bare bash."""
    original = "echo foo " + "\\" * 2
    expected = _bare_bash(original)
    assert expected.returncode == 0  # sanity: bare bash handles it fine
    rewritten = _rewrite(original, str(tmp_path))
    normal = _run(_with_stub_compressor(rewritten, "cat"))
    assert (normal.returncode, normal.stdout) == (expected.returncode, expected.stdout)
    fallback = _run_forced_fallback(rewritten, tmp_path)
    assert (fallback.returncode, fallback.stdout) == (expected.returncode, expected.stdout)


def test_fallback_branch_exit_code_matrix(tmp_path) -> None:
    """Re-assert the exit-code contract THROUGH the else branch after the R1
    subshell change: 0/1/42/127/130 all flow exactly, stdout intact."""
    for code in (0, 1, 42, 127, 130):
        rewritten = _rewrite(f"echo x; exit {code}", str(tmp_path))
        proc = _run_forced_fallback(rewritten, tmp_path)
        assert proc.returncode == code, f"exit {code} lost (stderr: {proc.stderr!r})"
        assert proc.stdout.strip() == "x", f"exit {code}: stdout lost"


def test_heredoc_command_survives_rewrite(tmp_path) -> None:
    """A well-formed heredoc must survive the wrapper structure (guards the
    if/else fallback shape: heredoc bodies parse inside compound commands)."""
    cmd = _with_stub_compressor(
        _rewrite("cat <<EOF\nhello-heredoc\nEOF\nexit 4", str(tmp_path)), "cat"
    )
    proc = _run(cmd)
    assert proc.returncode == 4
    assert proc.stdout.strip() == "hello-heredoc"


def test_trailing_comment_command_survives_rewrite(tmp_path) -> None:
    """A command ending in a comment must not swallow the wrapper's next token."""
    cmd = _with_stub_compressor(_rewrite("echo done # trailing comment", str(tmp_path)), "cat")
    proc = _run(cmd)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "done"


def test_truthy_flag_variants_enable_python_gate() -> None:
    """Review F3 parity (python half): ``_flag_enabled`` accepts case-insensitive,
    whitespace-stripped truthy values. The hooks.json SHELL gate must accept the
    same set — pinned in test_plugin_hooks_manifest.py
    ::test_pretool_gate_accepts_python_equivalent_variants."""
    for value in ("TRUE", "On", " 1", "YES", "Enabled"):
        proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag=value)
        assert proc.returncode == 0
        assert "updatedInput" in proc.stdout, f"python gate must enable for {value!r}"


# --- full pipe: real compressor against local furl_ctx --------------------------


def _sqlite_env(tmp_path) -> dict:
    return {
        "FURL_CCR_BACKEND": "sqlite",
        "FURL_WORKSPACE_DIR": str(tmp_path / "ws"),
    }


def test_small_output_passes_through_raw(tmp_path) -> None:
    cmd = _with_local_compressor(_rewrite("echo small", str(tmp_path / "proj")))
    proc = _run(cmd, _sqlite_env(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "small"  # below threshold → verbatim


def test_large_output_is_compressed_and_retrievable(tmp_path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    # A JSON array reliably offloads to a <<ccr:HASH>> marker.
    payload = json.dumps([{"id": i, "name": f"item{i}", "v": i * 3} for i in range(300)])
    original_cmd = f"python3 -c {shlex.quote(f'print({payload!r})')}"
    cmd = _with_local_compressor(_rewrite(original_cmd, str(proj)))
    proc = _run(cmd, _sqlite_env(tmp_path))
    assert proc.returncode == 0
    assert "<<ccr:" in proc.stdout, "large output must be offloaded under a marker"
    assert len(proc.stdout) < len(payload), "compressed output must be shorter"

    # The original must be retrievable from the SAME per-project store the marker
    # points at (deliverable: same store/TTL/redaction as the PostToolUse path).
    match = re.search(r"<<ccr:([0-9a-fA-F]+)", proc.stdout)
    assert match is not None
    hash_key = match.group(1)
    prev = {k: os.environ.get(k) for k in ("FURL_WORKSPACE_DIR", "FURL_CCR_PROJECT_DIR")}
    os.environ["FURL_WORKSPACE_DIR"] = str(tmp_path / "ws")
    os.environ["FURL_CCR_PROJECT_DIR"] = str(proj)
    try:
        from furl_ctx.cache.backends.sqlite import SqliteBackend
        from furl_ctx.cache.compression_store import (
            CompressionStore,
            _ccr_namespace_db_path,
            _namespace_key,
        )

        key = _namespace_key(None, None)
        assert key is not None
        store = CompressionStore(backend=SqliteBackend(db_path=_ccr_namespace_db_path(key)))
        try:
            entry = store.retrieve(hash_key)
        finally:
            store.close()
        assert entry is not None, "the marker's original must be retrievable"
        # SmartCrusher stores the array rows semantically (compact re-serialization,
        # identical to the PostToolUse path), so compare parsed content, not
        # incidental input whitespace.
        assert json.loads(entry.original_content) == json.loads(payload)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_pipe_compress_binary_passthrough() -> None:
    data = b"\x00\x01\x02\xff\xfe binary \x80payload"
    proc = subprocess.run([sys.executable, str(_COMPRESSOR)], input=data, capture_output=True)
    assert proc.returncode == 0
    assert proc.stdout == data  # byte-exact passthrough of undecodable input


def test_pipe_compress_already_marked_passthrough() -> None:
    text = ("<<ccr:abc123>> already compressed " + "x" * 5000).encode()
    proc = subprocess.run([sys.executable, str(_COMPRESSOR)], input=text, capture_output=True)
    assert proc.returncode == 0
    assert proc.stdout == text  # loop guard: never double-compress


def test_pipe_compress_fail_open_when_furl_ctx_missing(tmp_path) -> None:
    poison = tmp_path / "site"
    (poison / "furl_ctx").mkdir(parents=True)
    (poison / "furl_ctx" / "__init__.py").write_text("raise ImportError('poison')\n")
    data = ("Z" * 5000).encode()
    proc = subprocess.run(
        [sys.executable, str(_COMPRESSOR)],
        input=data,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(poison)},
    )
    assert proc.returncode == 0
    assert proc.stdout == data  # furl_ctx unimportable → raw passthrough
