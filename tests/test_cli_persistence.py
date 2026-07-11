"""furl CLI persistence: durable-by-default store + honest cross-process miss.

Round-5 finding A. Two guarantees pinned here:

1. The ``furl`` CLI's ``compress`` and ``retrieve`` compose ACROSS separate
   processes — the core "nothing is truly lost" promise. The library default is
   an in-memory store that dies with the process; the CLI entrypoint opts into
   the durable sqlite backend (``furl_ctx/cli.py`` ``main`` setdefault) so a hash
   stored by one invocation is retrievable by the next. The round-trip test runs
   ``compress`` and ``retrieve`` as genuinely separate subprocesses.

2. A retrieve MISS is honest about WHERE it looked: it names the backend (and the
   resolved sqlite path), and when the store is the volatile in-memory backend it
   says plainly that in-memory entries do not survive across processes and points
   at ``FURL_CCR_BACKEND=sqlite``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from furl_ctx.cache.compression_store import reset_compression_store

# A payload big enough to force at least one CCR offload (the retrievable hash).
# Plain text (not JSON): 400 distinct lines offload past the token floor AND
# round-trip byte-exact, so the cross-process assertion can be exact bytes — the
# compressor canonicalizes JSON whitespace, but preserves plain text verbatim
# (mirrors tests/test_cli.py's line-range round-trips).
_PAYLOAD = "\n".join(f"line {i} padding-token-{i} more-filler-{i}" for i in range(1, 401))

# Every FURL_CCR_* knob that could redirect the store away from the CLI default
# global sqlite singleton — cleared so the subprocess exercises the pure default.
_CCR_ENV_KEYS = (
    "FURL_CCR_BACKEND",
    "FURL_CCR_PROJECT_DIR",
    "FURL_CCR_NAMESPACE",
    "FURL_CCR_SQLITE_PATH",
)


def _clean_ccr_env(tmp_path, **overrides: str) -> dict[str, str]:
    """A child-process env sandboxed to ``tmp_path`` with all FURL_CCR_* cleared.

    Scratch HOME + FURL_WORKSPACE_DIR keep the real ``~/.furl`` untouched, so the
    round-trip proves the CLI default alone — not any ambient store.
    """
    env = {k: v for k, v in os.environ.items() if k not in _CCR_ENV_KEYS}
    env["HOME"] = str(tmp_path / "home")
    env["FURL_WORKSPACE_DIR"] = str(tmp_path / "ws")
    env.update(overrides)
    os.makedirs(env["HOME"], exist_ok=True)
    return env


def _cli(args: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "furl_ctx.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_compress_then_retrieve_round_trips_across_processes(tmp_path) -> None:
    """``furl compress`` in one process, ``furl retrieve`` in another → success.

    RED on pre-fix code: without the CLI's durable-by-default backend, the second
    process gets a fresh empty in-memory store and the retrieve is a loud miss
    (exit 1) — the exact evaluator failure. GREEN once the CLI defaults to sqlite.
    """
    env = _clean_ccr_env(tmp_path)  # no FURL_CCR_BACKEND → exercise the default

    compressed = _cli(["compress", "--json"], env=env, stdin=_PAYLOAD)
    assert compressed.returncode == 0, compressed.stderr
    hashes = json.loads(compressed.stdout)["ccr_hashes"]
    assert hashes, "payload must offload at least one retrievable CCR hash"
    hash_key = hashes[0]

    # A genuinely separate process — no shared in-memory state.
    retrieved = _cli(["retrieve", hash_key], env=env)
    assert retrieved.returncode == 0, (
        "cross-process retrieve missed under the CLI default backend: " + retrieved.stderr
    )
    assert retrieved.stdout == _PAYLOAD


def test_round_trip_survives_a_fresh_workspace_only_via_the_file(tmp_path) -> None:
    """The durability is the sqlite FILE, not shared process memory: a third
    process retrieving the same hash still succeeds (the file persists)."""
    env = _clean_ccr_env(tmp_path)
    compressed = _cli(["compress", "--json"], env=env, stdin=_PAYLOAD)
    hash_key = json.loads(compressed.stdout)["ccr_hashes"][0]
    # sqlite file exists on disk under the sandboxed workspace.
    assert (tmp_path / "ws" / "ccr.sqlite3").is_file()
    again = _cli(["retrieve", hash_key], env=env)
    assert again.returncode == 0, again.stderr
    assert again.stdout == _PAYLOAD


# ── honest-miss message shape (both backend cases), in-process for coverage ───


@pytest.fixture
def _hermetic_cli_env(tmp_path, monkeypatch):
    """Isolate the in-process ``main()`` calls: sandbox the workspace, reset the
    store singleton, and let monkeypatch snapshot FURL_CCR_BACKEND so the CLI's
    ``os.environ.setdefault`` cannot leak past the test."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    reset_compression_store()
    yield monkeypatch
    reset_compression_store()


def _retrieve_miss_stderr(argv: list[str]) -> tuple[int, str]:
    """Run ``main(argv)`` in-process, returning (returncode, stderr)."""
    from furl_ctx.cli import main

    captured_err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = io.StringIO()
        sys.stderr = captured_err
        rc = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, captured_err.getvalue()


def test_retrieve_miss_names_the_sqlite_store(_hermetic_cli_env, tmp_path) -> None:
    """Under the durable default, a miss names backend=sqlite + the resolved path,
    and does NOT emit the in-memory cross-process warning."""
    _hermetic_cli_env.setenv("FURL_CCR_BACKEND", "sqlite")
    rc, stderr = _retrieve_miss_stderr(["retrieve", "0" * 24])
    assert rc == 1
    assert "not found" in stderr
    assert "backend=sqlite" in stderr
    assert "ccr.sqlite3" in stderr  # the resolved store path is named
    assert "do not survive across processes" not in stderr  # sqlite IS durable


def test_retrieve_miss_in_memory_is_honest_about_volatility(_hermetic_cli_env) -> None:
    """With FURL_CCR_BACKEND=memory, a miss says plainly that in-memory entries do
    not survive across processes and points at the durable opt-out."""
    _hermetic_cli_env.setenv("FURL_CCR_BACKEND", "memory")
    rc, stderr = _retrieve_miss_stderr(["retrieve", "0" * 24])
    assert rc == 1
    assert "not found" in stderr
    assert "in-memory (process-local)" in stderr
    assert "do not survive across processes" in stderr
    assert "FURL_CCR_BACKEND=sqlite" in stderr
