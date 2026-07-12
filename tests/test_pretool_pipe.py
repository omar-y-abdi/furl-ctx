"""Opt-in PreToolUse pipe: the rewrite hook (pretool_pipe.py) + the compressor
(pipe_compress.py).

This is the real-savings path that does NOT depend on PostToolUse
``updatedToolOutput`` (dropped by Claude Code >=2.1.163, #68951): it rewrites a
Bash command so its stdout is compressed at the SOURCE. The load-bearing
invariants pinned here:

* DEFAULT OFF is a byte-identical no-op (the flag must be a deliberate opt-in).
* the rewrite preserves the ORIGINAL command's EXIT CODE exactly (proved with a
  stub compressor so the property is isolated to the shell wrapper), passes
  STDERR through untouched, lets small output through raw, and is FAIL-OPEN even
  when the compressor cannot start (``|| cat`` of the captured output).
* the compressor shrinks large output, stores the original under a retrievable
  ``<<ccr:HASH>>`` marker in the shared per-project store, and is byte-safe +
  fail-open on binary / undecodable / furl_ctx-missing input.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks"
_PRETOOL = _HOOKS / "pretool_pipe.py"
_COMPRESSOR = _HOOKS / "pipe_compress.py"

_UV_PREFIX_RE = re.compile(r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" python3')
_COMPRESSOR_SEG_RE = re.compile(
    r"FURL_CCR_PROJECT_DIR=\S+ FURL_CCR_BACKEND=sqlite "
    r'uv run --no-project --with "furl-ctx\[mcp\]==[^"]*" python3 \S+'
)


def _rewrite(command: str, cwd: str) -> str:
    """Return the rewritten command pretool_pipe.py emits for *command* (flag on)."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
    proc = subprocess.run(
        [sys.executable, str(_PRETOOL)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "FURL_PRETOOL_PIPE": "1"},
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
    env = {**os.environ}
    env.pop("FURL_PRETOOL_PIPE", None)
    if flag is not None:
        env["FURL_PRETOOL_PIPE"] = flag
    return subprocess.run(
        [sys.executable, str(_PRETOOL)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_default_off_is_byte_identical_noop() -> None:
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag=None)
    assert proc.returncode == 0
    assert proc.stdout == ""  # nothing emitted → original command runs unchanged


def test_falsey_flags_stay_off() -> None:
    for value in ("0", "false", "off", "no", ""):
        proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag=value)
        assert proc.returncode == 0 and proc.stdout == "", f"flag {value!r} must stay off"


def test_enabled_rewrites_bash_with_transparent_marker() -> None:
    proc = _pretool({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, flag="1")
    assert proc.returncode == 0
    new_cmd = json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    assert new_cmd.startswith("# furl-pipe (FURL_PRETOOL_PIPE=1)")  # transcript-visible
    assert "pipe_compress.py" in new_cmd
    assert "furl-ctx[mcp]==" in new_cmd  # pinned engine
    assert "exit $__furl_ec" in new_cmd  # exit-code preservation


def test_enabled_but_non_bash_passthrough() -> None:
    proc = _pretool({"tool_name": "WebFetch", "tool_input": {"command": "echo hi"}}, flag="1")
    assert proc.returncode == 0 and proc.stdout == ""


def test_already_wrapped_not_double_wrapped() -> None:
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


def test_stderr_passes_through_untouched(tmp_path) -> None:
    cmd = _with_stub_compressor(
        _rewrite("echo to-out; echo to-err >&2; exit 3", str(tmp_path)), "cat"
    )
    proc = _run(cmd)
    assert proc.returncode == 3
    assert proc.stdout.strip() == "to-out"
    assert proc.stderr.strip() == "to-err"


def test_fail_open_when_compressor_cannot_start(tmp_path) -> None:
    """A compressor that cannot even run must fall back to the RAW captured
    output (the shell-level ``|| cat``), never a lost/broken command."""
    cmd = _with_stub_compressor(
        _rewrite("echo raw-line; exit 5", str(tmp_path)), "/nonexistent/furl/compressor"
    )
    proc = _run(cmd)
    assert proc.returncode == 5  # original exit code still preserved
    assert proc.stdout.strip() == "raw-line"  # raw output survived


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
