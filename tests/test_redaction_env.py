"""``FURL_REDACT_PATTERNS`` — env-expressible pre-storage secret redaction.

Closes the finding-1 gap: the library ``CompressConfig.redactor`` is a Python
callable the env-only Claude Code plugin (hook + MCP server) cannot express, so
the primary distribution channel had NO preventive secret scrubbing. These tests
pin the whole contract:

* the builder parses/validates a pattern list (or file), is resilient to a bad
  regex, and is a true no-op when unset;
* ``compress()`` redacts BEFORE compression AND BEFORE the CCR store write, so
  the stored original — and every ``retrieve()`` of it — holds only redacted
  bytes, and the raw sqlite file on disk never contains the secret;
* the env redactor composes with the library callback (both apply);
* the hook subprocess scrubs its model-visible output for both large
  (compressed) and small (below-threshold) outputs, and the on-disk store;
* the MCP ``furl_compress`` path redacts;
* default OFF is byte-identical (the secret survives — proving nothing fires).

Secret fixtures use a benign, non-real-credential shape so commit scanners do
not flag the test file itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from furl_ctx.redaction import build_env_redactor, compose_redactors, redaction_marker

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / "plugins" / "furl" / "hooks" / "compress_tool_output.py"

# Benign secret shape (NOT a real provider key), so scanners ignore the test.
SECRET = "TOPSECRET-424242"
PATTERN = r"TOPSECRET-[0-9]{6}"


def _big_secret_log() -> str:
    return (
        "\n".join(
            f"line {i} lorem ipsum {SECRET} dolor sit amet consectetur adipiscing"
            for i in range(200)
        )
        + "\n"
    )


# ─── builder unit tests ──────────────────────────────────────────────────────


def test_unset_or_blank_is_none() -> None:
    assert build_env_redactor({}) is None
    assert build_env_redactor({"FURL_REDACT_PATTERNS": ""}) is None
    assert build_env_redactor({"FURL_REDACT_PATTERNS": "   \n  \n"}) is None


def test_single_pattern_and_marker_format() -> None:
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": PATTERN})
    assert redactor is not None
    assert redactor(f"x {SECRET} y") == "x [REDACTED:1] y"
    assert redaction_marker(1) == "[REDACTED:1]"


def test_multi_pattern_distinct_markers_newline_separated() -> None:
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": "AAA[0-9]+\nBBB[0-9]+"})
    assert redactor is not None
    assert redactor("AAA1 BBB2") == "[REDACTED:1] [REDACTED:2]"


def test_comma_inside_pattern_is_not_a_separator() -> None:
    # A regex quantifier {2,4} contains a comma; splitting on commas would
    # corrupt it. Newline separation keeps it intact.
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": r"a{2,4}"})
    assert redactor is not None
    assert redactor("x aaa y") == "x [REDACTED:1] y"


def test_invalid_regex_skipped_with_stable_index(capsys: pytest.CaptureFixture[str]) -> None:
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": "GOOD[0-9]+\n(unbalanced\nTAIL[0-9]+"})
    assert redactor is not None
    # #2 is invalid -> skipped; survivors keep their ORIGINAL 1-based index, so
    # an operator's [REDACTED:3] always means "line 3 of my list".
    assert redactor("GOOD1 TAIL9") == "[REDACTED:1] [REDACTED:3]"
    assert "skipping invalid regex #2" in capsys.readouterr().err


def test_all_patterns_invalid_is_none() -> None:
    assert build_env_redactor({"FURL_REDACT_PATTERNS": "(bad\n[oops"}) is None


def test_comments_and_blank_lines_ignored() -> None:
    redactor = build_env_redactor(
        {"FURL_REDACT_PATTERNS": "# a comment\n\nX[0-9]+\n   # trailing note"}
    )
    assert redactor is not None
    assert redactor("X7") == "[REDACTED:1]"


def test_pattern_file(tmp_path: Path) -> None:
    patterns_file = tmp_path / "patterns.txt"
    patterns_file.write_text("# secrets\nAAA[0-9]+\nBBB[0-9]+\n", encoding="utf-8")
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": f"@{patterns_file}"})
    assert redactor is not None
    assert redactor("AAA1 BBB2") == "[REDACTED:1] [REDACTED:2]"


def test_missing_pattern_file_is_none_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.txt"
    assert build_env_redactor({"FURL_REDACT_PATTERNS": f"@{missing}"}) is None
    assert "cannot read pattern file" in capsys.readouterr().err


def test_compose_redactors_order_and_identity() -> None:
    to_y = lambda s: s.replace("x", "y")  # noqa: E731
    to_z = lambda s: s.replace("y", "z")  # noqa: E731
    assert compose_redactors(None, None) is None
    assert compose_redactors(to_y, None) is to_y
    assert compose_redactors(None, to_z) is to_z
    # first THEN second: x -> y -> z
    composed = compose_redactors(to_y, to_z)
    assert composed is not None
    assert composed("x") == "z"


# ─── compress() integration ──────────────────────────────────────────────────


def _fresh_sqlite_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from furl_ctx.cache.compression_store import reset_compression_store

    db = tmp_path / "ccr.sqlite3"
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(db))
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    reset_compression_store()
    return db


def test_compress_redacts_before_storage_and_retrieval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from furl_ctx import compress, retrieve
    from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store

    _fresh_sqlite_store(monkeypatch, tmp_path)
    monkeypatch.setenv("FURL_REDACT_PATTERNS", PATTERN)

    result = compress([{"role": "tool", "content": _big_secret_log()}])
    assert SECRET not in result.messages[0]["content"]  # model-visible output scrubbed

    store = get_compression_store()
    entries = list(store._backend.items())
    assert entries, "expected at least one CCR entry"
    for hash_key, entry in entries:
        assert SECRET not in entry.original_content  # STORED original scrubbed
        # Byte-exact retrieval returns the REDACTED original (pre-redaction gone).
        assert retrieve(hash_key) == entry.original_content
        assert SECRET not in retrieve(hash_key)
    reset_compression_store()


def test_compress_composes_env_and_config_redactor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from furl_ctx import CompressConfig, compress
    from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store

    _fresh_sqlite_store(monkeypatch, tmp_path)
    monkeypatch.setenv("FURL_REDACT_PATTERNS", PATTERN)  # scrubs SECRET
    cfg = CompressConfig(redactor=lambda s: s.replace("CALLBACKSECRET", "[CB]"))
    text = (
        "\n".join(
            f"line {i} {SECRET} and CALLBACKSECRET padding padding padding" for i in range(200)
        )
        + "\n"
    )
    compress([{"role": "tool", "content": text}], config=cfg)

    store = get_compression_store()
    entries = list(store._backend.items())
    assert entries
    for _hash, entry in entries:
        assert SECRET not in entry.original_content  # env redactor applied
        assert "CALLBACKSECRET" not in entry.original_content  # config redactor applied
    reset_compression_store()


def test_compress_default_off_leaves_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store

    _fresh_sqlite_store(monkeypatch, tmp_path)
    monkeypatch.delenv("FURL_REDACT_PATTERNS", raising=False)  # OFF

    compress([{"role": "tool", "content": _big_secret_log()}])
    # Off => no redaction => the secret is present exactly as before. This is the
    # byte-identical-default invariant's positive control.
    store = get_compression_store()
    assert any(SECRET in entry.original_content for _h, entry in store._backend.items())
    reset_compression_store()


def test_secret_absent_from_sqlite_file_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from furl_ctx import compress
    from furl_ctx.cache.compression_store import reset_compression_store

    _fresh_sqlite_store(monkeypatch, tmp_path)
    monkeypatch.setenv("FURL_REDACT_PATTERNS", PATTERN)
    compress([{"role": "tool", "content": _big_secret_log()}])
    reset_compression_store()  # closes sqlite handles (WAL checkpoint on last close)

    # Read the raw on-disk bytes of EVERY store file (main db + any -wal/-shm)
    # and prove the secret is nowhere on disk.
    for path in tmp_path.iterdir():
        assert SECRET.encode() not in path.read_bytes(), f"secret leaked into {path.name}"


def test_mcp_compress_path_redacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    _fresh_sqlite_store(monkeypatch, tmp_path)
    monkeypatch.setenv("FURL_REDACT_PATTERNS", PATTERN)

    server = FurlMCPServer()
    server._compress_content(_big_secret_log())  # the furl_compress worker

    store = server._get_local_store()
    entries = list(store._backend.items())
    assert entries
    for _hash, entry in entries:
        assert SECRET not in entry.original_content


# ─── hook subprocess path ────────────────────────────────────────────────────


def _run_hook(
    tmp_path: Path, stdout_text: str, patterns: str | None, min_chars: str = "500"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env = dict(os.environ)
    db = tmp_path / "ccr.sqlite3"
    env["FURL_CCR_BACKEND"] = "sqlite"
    env["FURL_CCR_SQLITE_PATH"] = str(db)
    env["FURL_CCR_PROJECT_DIR"] = ""  # disable namespacing -> we control the path
    env.pop("FURL_CCR_NAMESPACE", None)
    env["FURL_HOOK_MIN_CHARS"] = min_chars
    if patterns is None:
        env.pop("FURL_REDACT_PATTERNS", None)  # ensure OFF (no ambient leak)
    else:
        env["FURL_REDACT_PATTERNS"] = patterns
    payload = {
        "tool_name": "Bash",
        "tool_response": {"stdout": stdout_text},
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
    }
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, db


def _store_entries(db: Path) -> list[tuple[str, str | None, str]]:
    from furl_ctx.cache.backends.sqlite import SqliteBackend

    backend = SqliteBackend(db_path=str(db))
    try:
        return [(h, e.tool_name, e.original_content) for h, e in backend.items()]
    finally:
        backend.close()


def test_hook_redacts_large_output_and_on_disk_store(tmp_path: Path) -> None:
    proc, db = _run_hook(tmp_path, _big_secret_log(), patterns=PATTERN)
    assert proc.returncode == 0
    assert SECRET not in proc.stdout  # model-visible replacement is scrubbed
    entries = _store_entries(db)
    assert entries, "hook should have stored a compressed entry"
    for _hash, _tool, original in entries:
        assert SECRET not in original  # on-disk original scrubbed


def test_hook_redacts_small_output_below_threshold(tmp_path: Path) -> None:
    # Too small to compress, but redaction still fires and the scrubbed text is
    # emitted so the secret never reaches the model (matches the library
    # redactor, which redacts regardless of compression).
    small = f"quick result {SECRET} done"
    proc, _db = _run_hook(tmp_path, small, patterns=PATTERN, min_chars="100000")
    assert proc.returncode == 0
    assert proc.stdout.strip(), "expected an emitted (scrubbed) replacement, not passthrough"
    assert SECRET not in proc.stdout
    assert "[REDACTED:1]" in proc.stdout


def test_hook_default_off_is_byte_identical_passthrough(tmp_path: Path) -> None:
    # No patterns + small output -> passthrough: empty stdout, original kept
    # verbatim (byte-identical to shipped behavior).
    small = f"quick result {SECRET} done"
    proc, _db = _run_hook(tmp_path, small, patterns=None, min_chars="100000")
    assert proc.returncode == 0
    assert proc.stdout == ""  # passthrough emits nothing


# ─── MULTILINE anchor semantics (review F4) ──────────────────────────────────


def test_multiline_caret_anchor_matches_mid_string_lines() -> None:
    # Tool output is line-oriented: an operator writing ^ means "line start".
    # Patterns compile with re.MULTILINE, so ^password=... hits a mid-output
    # line — string-anchored-only was the silent-miss surprise (review F4).
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": r"^password=\S+"})
    assert redactor is not None
    out = redactor("header line\npassword=hunter2\ntrailer line")
    assert "hunter2" not in out
    assert out == "header line\n[REDACTED:1]\ntrailer line"


def test_multiline_dollar_anchor_matches_per_line() -> None:
    redactor = build_env_redactor({"FURL_REDACT_PATTERNS": r"token=\S+$"})
    assert redactor is not None
    out = redactor("token=abc123\nnext line stays")
    assert "abc123" not in out
    assert out == "[REDACTED:1]\nnext line stays"


# ─── MCP furl_read path (review F1) ──────────────────────────────────────────


def test_furl_read_redacts_served_output_stored_entry_and_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # furl_read stores RAW file content; with FURL_REDACT_PATTERNS set the
    # secret must be absent from the served (numbered) output, the CCR entry,
    # AND every store-dir file's raw bytes (review F1).
    pytest.importorskip("mcp")
    from furl_ctx.cache.compression_store import reset_compression_store
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret_file = workspace / "notes.txt"
    secret_file.write_text(f"prelude\n{SECRET}\npostlude\n", encoding="utf-8")

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(store_dir / "ccr.sqlite3"))
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    monkeypatch.setenv("FURL_MCP_READ", "1")
    monkeypatch.setenv("FURL_REDACT_PATTERNS", PATTERN)
    reset_compression_store()

    server = FurlMCPServer()
    served = server._read_file_sync(str(secret_file), fresh=True)
    served_text = "".join(block.text for block in served)
    assert SECRET not in served_text, "secret leaked into the served furl_read output"
    assert "[REDACTED:1]" in served_text

    store = server._get_local_store()
    entries = list(store._backend.items())
    assert entries, "furl_read should have stored a CCR entry"
    for _hash, entry in entries:
        assert SECRET not in entry.original_content, "secret persisted in the CCR entry"
        assert "[REDACTED:1]" in entry.original_content

    reset_compression_store()  # close sqlite handles before raw-byte inspection
    for path in store_dir.iterdir():
        assert SECRET.encode() not in path.read_bytes(), f"secret leaked into {path.name}"


# ─── MCP filtered-compress path (review F2 pin) ──────────────────────────────


def test_mcp_compress_filtered_runs_are_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # _compress_filtered receives content AFTER _compress_content's redaction
    # point and re-enters _compress_content per eligible run — so the filtered
    # store path is covered by the same redaction. Pin it (review F2).
    pytest.importorskip("mcp")
    from furl_ctx.ccr.compress_modes import SectionPatterns
    from furl_ctx.ccr.mcp_server import FurlMCPServer

    _fresh_sqlite_store(monkeypatch, tmp_path)
    monkeypatch.setenv("FURL_REDACT_PATTERNS", PATTERN)

    server = FurlMCPServer()
    out = server._compress_content(
        _big_secret_log(),
        patterns=SectionPatterns(include=("line",), exclude=()),
    )
    assert out.get("filtered") is True, f"expected the filtered path, got {out.keys()}"
    assert SECRET not in json.dumps(out)

    store = server._get_local_store()
    entries = list(store._backend.items())
    assert entries, "filtered compress should have stored per-run entries"
    for _hash, entry in entries:
        assert SECRET not in entry.original_content
