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

Round-6 finding B adds two more:

3. TTL parity with the plugin: the CLI defaults ``FURL_CCR_TTL_SECONDS`` to
   86400 (24 h, the hook's own setdefault) instead of inheriting the library's
   1800 s — CLI-stored hashes no longer die 30 minutes into a session. An
   explicit user TTL env still wins.

4. ``FURL_CCR_PROJECT_DIR=<project root>`` routes the CLI at that project's
   isolated namespace store (the store the Claude Code plugin writes) — the
   documented bridge between the CLI's global store and the plugin's
   per-project ones.
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
# global sqlite singleton — or change its retention — cleared so the subprocess
# exercises the pure default.
_CCR_ENV_KEYS = (
    "FURL_CCR_BACKEND",
    "FURL_CCR_PROJECT_DIR",
    "FURL_CCR_NAMESPACE",
    "FURL_CCR_SQLITE_PATH",
    "FURL_CCR_TTL_SECONDS",
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
    store singleton, and let monkeypatch snapshot FURL_CCR_BACKEND /
    FURL_CCR_TTL_SECONDS so the CLI's ``os.environ.setdefault`` cannot leak
    past the test."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_TTL_SECONDS", raising=False)
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


def test_retrieve_miss_names_the_namespace_store(_hermetic_cli_env, tmp_path) -> None:
    """F2: with FURL_CCR_PROJECT_DIR active, a miss names the per-namespace sqlite
    store that was actually searched (ccr-ns-<hash>.sqlite3), NOT the global
    ccr.sqlite3 — the miss message must describe the same store the namespace-aware
    retrieve consulted."""
    _hermetic_cli_env.setenv("FURL_CCR_BACKEND", "sqlite")
    _hermetic_cli_env.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))
    rc, stderr = _retrieve_miss_stderr(["retrieve", "0" * 24])
    assert rc == 1
    assert "not found" in stderr
    assert "backend=sqlite" in stderr
    assert "ccr-ns-" in stderr, "miss must name the per-namespace store file"
    # The global file is exactly 'ccr.sqlite3'; the ns file ('ccr-ns-<hash>.sqlite3')
    # does not contain that substring, so its absence proves the global store is
    # NOT the one being named.
    assert "ccr.sqlite3" not in stderr


# ── round-6 finding B: TTL parity + explicit project-store routing ────────────


def _run_cli_compress_json(payload: str) -> dict:
    """Run ``main(["compress", "--json"])`` in-process; return the parsed JSON."""
    from furl_ctx.cli import main

    old_in, old_out = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(payload)
        sys.stdout = io.StringIO()
        rc = main(["compress", "--json"])
        raw = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    assert rc == 0
    return json.loads(raw)


def test_cli_compress_defaults_to_24h_ttl(_hermetic_cli_env) -> None:
    """Bare-shell CLI (no FURL_CCR_TTL_SECONDS): stored entries carry the 24 h
    default (86400 s), matching the plugin hook's own setdefault — not the
    library's 1800 s that made CLI hashes die 30 minutes into a session.

    RED on pre-fix code: the entry TTL was 1800.
    """
    out = _run_cli_compress_json(_PAYLOAD)
    assert os.environ.get("FURL_CCR_TTL_SECONDS") == "86400", (
        "cli main() must setdefault the hook's 24 h TTL"
    )
    hashes = out["ccr_hashes"]
    assert hashes, "payload must offload at least one retrievable CCR hash"

    from furl_ctx.cache.compression_store import get_compression_store

    entry = get_compression_store()._backend.get(hashes[0])
    assert entry is not None
    assert entry.ttl == 86400


def test_cli_explicit_env_ttl_still_wins(_hermetic_cli_env) -> None:
    """setdefault semantics: a user-set FURL_CCR_TTL_SECONDS beats the CLI's
    24 h default (identical to how the hook treats its own setdefault)."""
    _hermetic_cli_env.setenv("FURL_CCR_TTL_SECONDS", "123")
    out = _run_cli_compress_json(_PAYLOAD)
    assert os.environ.get("FURL_CCR_TTL_SECONDS") == "123"

    from furl_ctx.cache.compression_store import get_compression_store

    entry = get_compression_store()._backend.get(out["ccr_hashes"][0])
    assert entry is not None
    assert entry.ttl == 123


def test_project_dir_env_targets_the_namespace_store(tmp_path) -> None:
    """FURL_CCR_PROJECT_DIR=<root> routes the CLI at that project's isolated
    store — the same store the Claude Code plugin's hook/MCP write — while a
    plain invocation keeps the global store and honestly misses there.

    This pins the documented bridge (cli epilogs / README) between the CLI's
    default global store and the plugin's per-project stores; the CLI itself
    never auto-detects a project (cwd guessing would silo data under whatever
    subdirectory the user ran from).
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    env_ns = _clean_ccr_env(tmp_path, FURL_CCR_PROJECT_DIR=str(proj))

    compressed = _cli(["compress", "--json"], env=env_ns, stdin=_PAYLOAD)
    assert compressed.returncode == 0, compressed.stderr
    hash_key = json.loads(compressed.stdout)["ccr_hashes"][0]

    # The write landed in a per-namespace file, not the global ccr.sqlite3.
    assert list((tmp_path / "ws").glob("ccr-ns-*.sqlite3")), (
        "compress under FURL_CCR_PROJECT_DIR must create the namespace store file"
    )

    # Same env → same namespace store → cross-process hit.
    hit = _cli(["retrieve", hash_key], env=env_ns)
    assert hit.returncode == 0, hit.stderr
    assert hit.stdout == _PAYLOAD

    # No env → global store → honest miss naming the global file it searched.
    miss = _cli(["retrieve", hash_key], env=_clean_ccr_env(tmp_path))
    assert miss.returncode == 1
    assert "ccr.sqlite3" in miss.stderr
