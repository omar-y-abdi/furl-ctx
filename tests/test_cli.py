"""furl CLI: compress (stdin/file), retrieve (miss + slice flags), purge, doctor, mcp."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from typing import Any

import pytest

from furl_ctx.cache.compression_store import reset_compression_store


def _run(args: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "furl_ctx.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "FURL_CCR_BACKEND": "memory"},
    )


def _big_array() -> str:
    return json.dumps([{"id": i, "status": "ok", "host": "w-01"} for i in range(400)])


def test_doctor_reports_ok() -> None:
    proc = _run(["doctor"])
    assert proc.returncode == 0
    assert "[OK] furl_ctx import" in proc.stdout
    assert "[OK] native _core" in proc.stdout


def test_compress_stdin_to_stdout_shrinks() -> None:
    payload = _big_array()
    proc = _run(["compress"], stdin=payload)
    assert proc.returncode == 0
    assert 0 < len(proc.stdout) < len(payload)


def test_compress_json_reports_token_savings() -> None:
    proc = _run(["compress", "--json"], stdin=_big_array())
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["tokens_after"] < out["tokens_before"]
    assert "compressed" in out and out["error"] is None


def test_compress_binary_file_is_graceful_not_a_traceback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Bug-1 / High-7 / A7: a binary FILE arg must not crash with a raw
    # UnicodeDecodeError traceback. It decodes lossily, warns once, and exits 0.
    binfile = tmp_path / "blob.bin"
    binfile.write_bytes(bytes(range(256)) * 8)
    proc = _run(["compress", str(binfile)])
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    assert "not valid UTF-8" in proc.stderr


def test_compress_binary_stdin_matches_file_graceful_behavior() -> None:
    # A7: binary STDIN must be as graceful as the file-arg path (no traceback,
    # same warning). Uses raw bytes stdin, so the CLI reads via sys.stdin.buffer.
    proc = subprocess.run(
        [sys.executable, "-m", "furl_ctx.cli", "compress"],
        input=bytes(range(256)) * 8,
        capture_output=True,
        env={**os.environ, "FURL_CCR_BACKEND": "memory"},
    )
    assert proc.returncode == 0
    assert b"Traceback" not in proc.stderr
    assert b"not valid UTF-8" in proc.stderr


def test_retrieve_unknown_hash_exits_1() -> None:
    proc = _run(["retrieve", "0" * 24])
    assert proc.returncode == 1
    assert "not found" in proc.stderr


def test_eval_recall_over_corpus_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A dir of two files: one compressible array, one plain doc. eval must
    # parse `--recall`, compress the corpus for the ratio, and run the needle-
    # recall trust gate.
    (tmp_path / "rows.json").write_text(_big_array(), encoding="utf-8")
    (tmp_path / "note.txt").write_text("plain prose, nothing to drop\n", encoding="utf-8")

    proc = _run(["eval", str(tmp_path), "--recall"])
    assert proc.returncode == 0, proc.stderr
    assert "files: 2" in proc.stdout
    # The corpus array compresses (ratio strictly between none and all).
    ratio = float(proc.stdout.split("corpus visible-token reduction:")[1].split("%")[0])
    assert 0.0 < ratio < 100.0
    # The needle-recall trust gate is 100% on a healthy engine (the naming arm
    # recalls its needle by construction); it drops if compression starts
    # silently losing content.
    recall = float(proc.stdout.split("trust gate):")[1].split("%")[0])
    assert recall == 100.0


def test_eval_recall_is_optional_not_a_mandatory_flag(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Bug-13: --recall was a REQUIRED store_true flag (a mandatory flag conveying
    # no choice). It is now optional — eval reports the ratio without it, and the
    # (extra-work) needle-recall gate runs only when --recall is passed.
    corpus = tmp_path / "rows.json"
    corpus.write_text(_big_array(), encoding="utf-8")

    without = _run(["eval", str(corpus)])
    assert without.returncode == 0, without.stderr
    assert "visible-token reduction:" in without.stdout
    assert "trust gate" not in without.stdout  # recall gate is opt-in

    with_recall = _run(["eval", str(corpus), "--recall"])
    assert with_recall.returncode == 0, with_recall.stderr
    assert "trust gate" in with_recall.stdout


def test_eval_missing_corpus_exits_1(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # CAVEAT-14: a nonexistent corpus path used to silently "succeed" — the
    # per-file OSError handler skipped it and eval still exited 0 with a
    # bogus 0.0% ratio, as if the corpus were legitimately empty.
    missing = tmp_path / "does-not-exist"
    proc = _run(["eval", str(missing), "--recall"])
    assert proc.returncode == 1
    assert "not found" in proc.stderr
    assert proc.stdout == ""


# ── retrieve slice tests (in-process round-trips) ────────────────────────────
#
# The FURL_CCR_BACKEND=memory store is per-process, so the compress step and the
# retrieve step must share one Python process.  We call the library to compress,
# capture the hash, then call ``main()`` directly (capturing stdout/stderr via
# StringIO) to exercise the CLI without a subprocess boundary.


def _compress_and_get_hash() -> str:
    """Compress a 200-row JSON array and return its CCR hash."""
    from furl_ctx import compress

    payload = json.dumps(
        [{"id": i, "name": f"event_{i}", "status": "ok", "value": float(i)} for i in range(200)]
    )
    result = compress([{"role": "tool", "content": payload}], model="claude-sonnet-4-5-20250929")
    assert result.ccr_hashes, "expected at least one CCR hash from compression"
    return result.ccr_hashes[0]


def _call_main(argv: list[str]) -> tuple[int, str, str]:
    """Call ``main()`` in-process, returning (returncode, stdout, stderr)."""
    from furl_ctx.cli import main

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = captured_out
        sys.stderr = captured_err
        rc = main(argv)
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
    return rc, captured_out.getvalue(), captured_err.getvalue()


def test_retrieve_no_flags_byte_identical_to_library() -> None:
    """``furl retrieve HASH`` with no slice flags is byte-exact vs the library."""
    from furl_ctx import retrieve

    h = _compress_and_get_hash()
    expected: str | None = retrieve(h)
    assert expected is not None, "library retrieve returned None — hash not in store"

    rc, stdout, _stderr = _call_main(["retrieve", h])
    assert rc == 0
    assert stdout == expected, "CLI output differs from library retrieve()"


def test_retrieve_select_equals_string() -> None:
    """``--select-field name --select-equals event_5`` returns only matching rows."""
    h = _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(
        ["retrieve", h, "--select-field", "name", "--select-equals", "event_5"]
    )
    assert rc == 0
    rows: list[Any] = json.loads(stdout)
    assert len(rows) == 1
    assert rows[0]["name"] == "event_5"
    assert rows[0]["id"] == 5


def test_retrieve_select_equals_int_parsing() -> None:
    """``--select-equals 3`` is parsed as int 3, matching ``id`` field."""
    h = _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(
        ["retrieve", h, "--select-field", "id", "--select-equals", "3"]
    )
    assert rc == 0
    rows: list[Any] = json.loads(stdout)
    assert len(rows) == 1
    assert rows[0]["id"] == 3


def test_retrieve_select_min_max_numeric_range() -> None:
    """``--select-min 3.0 --select-max 5.0`` keeps rows where value is in [3, 5]."""
    h = _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(
        ["retrieve", h, "--select-field", "value", "--select-min", "3.0", "--select-max", "5.0"]
    )
    assert rc == 0
    rows: list[Any] = json.loads(stdout)
    assert len(rows) == 3
    values = [r["value"] for r in rows]
    assert values == [3.0, 4.0, 5.0]


def test_retrieve_select_with_limit() -> None:
    """``--select-min 0 --select-max 100 --limit 3`` caps at 3 data rows.

    The library appends a ``_truncated`` sentinel row when the limit is hit, so
    the raw JSON array has len(data) + 1 elements.  We assert that no more than
    3 non-truncated rows are returned.
    """
    h = _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(
        [
            "retrieve",
            h,
            "--select-field",
            "value",
            "--select-min",
            "0",
            "--select-max",
            "100",
            "--limit",
            "3",
        ]
    )
    assert rc == 0
    rows: list[Any] = json.loads(stdout)
    data_rows = [r for r in rows if "_truncated" not in r]
    assert len(data_rows) == 3


def test_retrieve_incompatible_select_field_and_pattern_exits_2() -> None:
    """``--select-field`` + ``--pattern`` is an incompatible combo → exit 2 + stderr message."""
    h = _compress_and_get_hash()
    rc, _stdout, stderr = _call_main(
        ["retrieve", h, "--select-field", "name", "--pattern", "event"]
    )
    assert rc == 2
    assert stderr, "expected a non-empty error message on stderr"


def test_retrieve_select_equals_suppress_does_not_conflict_with_range() -> None:
    """Omitting ``--select-equals`` does NOT inject ``select_equals=None`` into kwargs.

    Regression guard: if ``--select-equals`` defaulted to ``None`` instead of
    ``argparse.SUPPRESS``, the CLI would forward ``select_equals=None`` whenever
    the flag was absent.  Combined with ``--select-min``, that would trigger the
    library's "select_equals and select_min/select_max are mutually exclusive"
    FilterError (exit 2) even though the user only asked for a range query.

    With SUPPRESS, the range-only path must succeed (exit 0).
    """
    h = _compress_and_get_hash()
    # Range only — no --select-equals.  If SUPPRESS is broken this exits 2.
    rc, stdout, _stderr = _call_main(
        ["retrieve", h, "--select-field", "value", "--select-min", "3.0", "--select-max", "5.0"]
    )
    assert rc == 0, "range-only select must succeed when --select-equals is absent"
    rows: list[Any] = json.loads(stdout)
    # Exactly ids 3, 4, 5 should match (values 3.0, 4.0, 5.0).
    data_rows = [r for r in rows if "_truncated" not in r]
    assert len(data_rows) == 3
    assert [r["value"] for r in data_rows] == [3.0, 4.0, 5.0]


# ── in-process purge / doctor / mcp / compress (coverage-visible) ─────────────
#
# The subprocess tests above exercise the CLI end-to-end but run in a child
# process, so they contribute no coverage. These drive ``main()`` in-process
# (via ``_call_main``) against a hermetic in-memory CCR store.


@pytest.fixture
def inprocess_memory_store(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A fresh in-memory CCR store scoped to this in-process test.

    The default backend is already in-memory, but pinning it (and resetting the
    global store around the test) keeps the compress->purge/retrieve round-trip
    hermetic and free of any sqlite connection.
    """
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


def test_purge_removes_a_stored_hash_then_second_purge_misses(
    inprocess_memory_store, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """``furl purge HASH`` deletes a live original (exit 0), then a repeat purge misses (exit 1)."""
    from furl_ctx import retrieve

    # Silence the High-8 profile banner so this asserts the strong invariant:
    # a successful purge itself emits NOTHING on stderr (the banner is unrelated
    # diagnostic output, covered by its own test).
    monkeypatch.setenv("FURL_PROFILE_BANNER", "0")

    h = _compress_and_get_hash()
    assert retrieve(h) is not None, "precondition: the hash must be stored before purge"

    rc, stdout, stderr = _call_main(["purge", h])
    assert rc == 0
    assert stdout == f"furl: purged {h} from the CCR store\n"
    assert stderr == ""
    assert retrieve(h) is None, "purge must actually remove the entry"

    # A second purge of the same hash is now a loud miss, not a silent success.
    rc2, stdout2, stderr2 = _call_main(["purge", h])
    assert rc2 == 1
    assert stdout2 == ""
    assert "not found" in stderr2


def test_purge_unknown_hash_exits_1_with_stderr(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """Purging a never-stored hash is a loud miss (exit 1, message on stderr), not a crash."""
    rc, stdout, stderr = _call_main(["purge", "0" * 24])
    assert rc == 1
    assert stdout == ""
    assert "not found" in stderr


def test_doctor_inprocess_all_checks_ok(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """In-process ``doctor`` reports OK for import, native core, and the CCR store, exit 0."""
    rc, stdout, _stderr = _call_main(["doctor"])
    assert rc == 0
    assert "[OK] furl_ctx import" in stdout
    assert "[OK] native _core" in stdout
    assert "[OK] CCR store" in stdout


def test_doctor_reports_fail_and_exits_1_when_store_unavailable(
    inprocess_memory_store, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """A broken CCR store surfaces as ``[FAIL]`` and flips the exit code to 1."""
    import furl_ctx.cache.compression_store as store_mod

    def _boom(*_args: Any, **_kwargs: Any):
        raise RuntimeError("store offline")

    monkeypatch.setattr(store_mod, "get_compression_store", _boom)
    rc, stdout, _stderr = _call_main(["doctor"])
    assert rc == 1
    assert "[FAIL] CCR store: store offline" in stdout


def test_mcp_help_exits_0_and_describes_the_launcher(capsys: pytest.CaptureFixture[str]) -> None:
    """``furl mcp --help`` prints the launcher's help (with --debug) and exits 0 — argparse wiring."""
    from furl_ctx.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["mcp", "--help"])
    assert exc_info.value.code == 0
    assert "--debug" in capsys.readouterr().out


def test_mcp_subcommand_runs_the_stdio_server_with_empty_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``furl mcp`` dispatches to the async server main with no extra args, exit 0."""
    import furl_ctx.ccr.mcp_server as mcp_mod

    seen: dict[str, list[str]] = {}

    async def _fake_main(argv: list[str]) -> None:
        seen["argv"] = argv

    monkeypatch.setattr(mcp_mod, "main", _fake_main)
    rc, _stdout, _stderr = _call_main(["mcp"])
    assert rc == 0
    assert seen["argv"] == []


def test_mcp_debug_flag_forwards_debug_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``furl mcp --debug`` forwards ``--debug`` through to the server main."""
    import furl_ctx.ccr.mcp_server as mcp_mod

    seen: dict[str, list[str]] = {}

    async def _fake_main(argv: list[str]) -> None:
        seen["argv"] = argv

    monkeypatch.setattr(mcp_mod, "main", _fake_main)
    rc, _stdout, _stderr = _call_main(["mcp", "--debug"])
    assert rc == 0
    assert seen["argv"] == ["--debug"]


def test_mcp_missing_dependency_surfaces_clean_error_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing MCP SDK (ImportError) becomes a clean ``furl:`` stderr message, exit 1 — no traceback."""
    import furl_ctx.ccr.mcp_server as mcp_mod

    async def _boom(_argv: list[str]) -> None:
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(mcp_mod, "main", _boom)
    # Silence the High-8 profile banner so this asserts the clean-error contract
    # on its own (the banner is separately tested); the ImportError must surface
    # as a `furl:`-prefixed stderr line with no traceback.
    monkeypatch.setenv("FURL_PROFILE_BANNER", "0")
    rc, _stdout, stderr = _call_main(["mcp"])
    assert rc == 1
    assert stderr.startswith("furl: ")
    assert "mcp" in stderr


def test_compress_file_argument_shrinks_and_json_reports_savings(
    inprocess_memory_store, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """``furl compress FILE`` reads the file and shrinks it; ``--json`` reports token savings."""
    payload = _big_array()
    source = tmp_path / "rows.json"
    source.write_text(payload, encoding="utf-8")

    rc, stdout, _stderr = _call_main(["compress", str(source)])
    assert rc == 0
    assert 0 < len(stdout) < len(payload)

    rc_json, stdout_json, _err = _call_main(["compress", "--json", str(source)])
    assert rc_json == 0
    stats = json.loads(stdout_json)
    assert stats["tokens_after"] < stats["tokens_before"]
    assert stats["error"] is None


def test_compress_reads_stdin_when_file_omitted(
    inprocess_memory_store, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """With no file argument, ``compress`` reads stdin (the default ``-``)."""
    payload = _big_array()
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc, stdout, _stderr = _call_main(["compress"])
    assert rc == 0
    assert 0 < len(stdout) < len(payload)


def test_retrieve_line_range_projects_a_numbered_window(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``--line-range 2:4`` parses ``START:END`` and projects that 1-based numbered window."""
    from furl_ctx import compress, retrieve

    # 400 distinct lines guarantee an offload (past the token floor) so a stored
    # original exists to slice; each line's content is fully determined by its index.
    text = "\n".join(f"line {i} padding-token-{i} more-filler-{i}" for i in range(1, 401))
    result = compress([{"role": "tool", "content": text}], model="claude-sonnet-4-5-20250929")
    assert result.ccr_hashes, "400-line text is expected to offload"
    h = result.ccr_hashes[0]
    assert retrieve(h) == text, "stored original must be byte-exact before slicing"

    rc, stdout, _stderr = _call_main(["retrieve", h, "--line-range", "2:4"])
    assert rc == 0
    assert stdout == (
        "2:line 2 padding-token-2 more-filler-2\n"
        "3:line 3 padding-token-3 more-filler-3\n"
        "4:line 4 padding-token-4 more-filler-4"
    )


@pytest.mark.parametrize("bad_range", ["nocolon", "a:b", "1:z"])
def test_retrieve_malformed_line_range_exits_2(
    bad_range: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed ``--line-range`` is rejected by argparse (exit 2), with a usage error.

    ``nocolon`` has no ``:`` separator; ``a:b`` / ``1:z`` carry non-integer bounds.
    """
    from furl_ctx.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["retrieve", "0" * 24, "--line-range", bad_range])
    assert exc_info.value.code == 2
    assert "--line-range" in capsys.readouterr().err


def test_retrieve_open_ended_line_range_from_start(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``--line-range :3`` (open start) projects lines 1..3 — the blank-bound branch."""
    from furl_ctx import compress, retrieve

    text = "\n".join(f"line {i} x{i}" for i in range(1, 401))
    h = compress(
        [{"role": "tool", "content": text}], model="claude-sonnet-4-5-20250929"
    ).ccr_hashes[0]
    assert retrieve(h) == text
    rc, stdout, _stderr = _call_main(["retrieve", h, "--line-range", ":3"])
    assert rc == 0
    assert stdout == "1:line 1 x1\n2:line 2 x2\n3:line 3 x3"


def test_retrieve_unknown_hash_inprocess_exits_1(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """An in-process retrieve of an absent hash is a loud miss: exit 1, stderr, no stdout."""
    rc, stdout, stderr = _call_main(["retrieve", "0" * 24])
    assert rc == 1
    assert stdout == ""
    assert "not found" in stderr


def test_retrieve_fields_projects_named_json_keys(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``--fields name`` projects only that key out of each object in the JSON array."""
    h = _compress_and_get_hash()  # 200 objects: id/name/status/value
    rc, stdout, _stderr = _call_main(["retrieve", h, "--fields", "name"])
    assert rc == 0
    rows = json.loads(stdout)
    assert len(rows) == 200
    assert rows[0] == {"name": "event_0"}
    assert rows[5] == {"name": "event_5"}


def test_retrieve_pattern_with_context_lines_returns_a_window(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``--pattern`` + ``--context-lines 1`` returns the match plus one neighbour each side."""
    from furl_ctx import compress, retrieve

    text = "\n".join(f"line {i} x{i}" for i in range(1, 401))
    h = compress(
        [{"role": "tool", "content": text}], model="claude-sonnet-4-5-20250929"
    ).ccr_hashes[0]
    assert retrieve(h) == text
    rc, stdout, _stderr = _call_main(
        ["retrieve", h, "--pattern", "line 5 ", "--context-lines", "1"]
    )
    assert rc == 0
    assert stdout == "4:line 4 x4\n5:line 5 x5\n6:line 6 x6"


def test_eval_inprocess_over_dir_reports_ratio_and_full_recall(
    inprocess_memory_store, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """In-process ``eval DIR --recall`` walks the dir, pools the ratio, and runs the recall gate."""
    (tmp_path / "rows.json").write_text(_big_array(), encoding="utf-8")
    (tmp_path / "note.txt").write_text("plain prose, nothing to drop\n", encoding="utf-8")

    rc, stdout, _stderr = _call_main(["eval", str(tmp_path), "--recall"])
    assert rc == 0
    assert "files: 2" in stdout
    ratio = float(stdout.split("corpus visible-token reduction:")[1].split("%")[0])
    assert 0.0 < ratio < 100.0
    recall = float(stdout.split("trust gate):")[1].split("%")[0])
    assert recall == 100.0


def test_eval_single_file_reports_one_file(inprocess_memory_store, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``eval FILE`` (not a dir) evaluates exactly that one file."""
    corpus = tmp_path / "rows.json"
    corpus.write_text(_big_array(), encoding="utf-8")
    rc, stdout, _stderr = _call_main(["eval", str(corpus), "--recall"])
    assert rc == 0
    assert "files: 1" in stdout


def test_eval_missing_corpus_inprocess_exits_1(inprocess_memory_store, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """In-process ``eval`` on a nonexistent path fails loud (exit 1, stderr), no stdout."""
    rc, stdout, stderr = _call_main(["eval", str(tmp_path / "nope"), "--recall"])
    assert rc == 1
    assert stdout == ""
    assert "corpus not found" in stderr


def test_eval_empty_dir_reports_no_files_exits_1(inprocess_memory_store, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``eval`` on a directory with no files fails loud: 'no files found', exit 1."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, stdout, stderr = _call_main(["eval", str(empty), "--recall"])
    assert rc == 1
    assert stdout == ""
    assert "no files found" in stderr


def test_eval_skips_undecodable_file_but_still_succeeds(inprocess_memory_store, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A non-UTF-8 file in the corpus is skipped (loud on stderr); eval still exits 0."""
    (tmp_path / "good.json").write_text(_big_array(), encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00\x01not utf-8\xff")

    rc, stdout, stderr = _call_main(["eval", str(tmp_path), "--recall"])
    assert rc == 0
    assert "skipping" in stderr
    assert "files: 2" in stdout  # both files are counted; the binary one is skipped on read


def test_doctor_reports_fail_when_tiktoken_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``doctor`` flags a missing tiktoken as [FAIL] (token counts fall back to estimation)."""
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    monkeypatch.setitem(sys.modules, "tiktoken", None)  # force ``import tiktoken`` to fail
    rc, stdout, _stderr = _call_main(["doctor"])
    assert rc == 1
    assert "[FAIL] tiktoken:" in stdout
    assert "token counts fall back to estimation" in stdout


def test_doctor_reports_fail_when_native_core_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``doctor`` flags a missing native _core as [FAIL] (compression fails open to 0%)."""
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    monkeypatch.setitem(sys.modules, "furl_ctx._core", None)  # force the native import to fail
    rc, stdout, _stderr = _call_main(["doctor"])
    assert rc == 1
    assert "[FAIL] native _core:" in stdout
    assert "compression fails open to 0%" in stdout


# ── Med-11: list / search / stats CLI parity ─────────────────────────────────


def test_list_shows_a_stored_entry(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """After a compress offloads to CCR, ``furl list`` shows the hash + preview.

    A 200-row array offloads the whole array PLUS a per-row entry for sliceable
    retrieval, so the array marker is the OLDEST of many; use a high --limit so
    the newest-first page reaches it.
    """
    h = _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(["list", "--limit", "300"])
    assert rc == 0
    assert h in stdout, "the stored hash must appear in the listing"
    assert "kind=" in stdout


def test_list_json_is_machine_readable(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``furl list --json`` emits a JSON array carrying the stored hash."""
    h = _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(["list", "--json", "--limit", "300"])
    assert rc == 0
    payload = json.loads(stdout)
    assert any(entry["hash"] == h for entry in payload)


def test_list_empty_store_is_a_clean_message(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``furl list`` on an empty store is a clean note, exit 0 (not a crash)."""
    rc, stdout, _stderr = _call_main(["list"])
    assert rc == 0
    assert "no live entries" in stdout


def test_search_finds_a_stored_original_by_content(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``furl search`` substring-matches stored originals and returns hits with a preview."""
    _compress_and_get_hash()  # rows carry name="event_<i>"
    # Substring (not token) match: a partial token like "event_" finds "event_199".
    rc, stdout, _stderr = _call_main(["search", "event_"])
    assert rc == 0
    assert "no stored original matches" not in stdout
    assert "event_" in stdout, "the redacted preview should show the matched text"
    assert "kind=" in stdout


def test_search_no_match_is_a_clean_message(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """A query that matches nothing is a clean note, exit 0."""
    _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(["search", "zzz_no_such_token_zzz"])
    assert rc == 0
    assert "no stored original matches" in stdout


def test_stats_reports_entries_and_profile(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``furl stats`` reports the live entry count and the active profile scope."""
    _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(["stats"])
    assert rc == 0
    assert "entries:" in stdout
    assert "default TTL:" in stdout


def test_stats_json_carries_profile_and_store(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``furl stats --json`` carries both the resolved profile and store block."""
    _compress_and_get_hash()
    rc, stdout, _stderr = _call_main(["stats", "--json"])
    assert rc == 0
    payload = json.loads(stdout)
    assert "profile" in payload and "store" in payload
    assert payload["profile"]["backend"] == "memory"


# ── Med-11: hash-format validation parity with the MCP server ────────────────


def test_retrieve_rejects_malformed_hash_with_exit_2(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """A malformed hash is a usage error (exit 2 + clean message), not a silent miss."""
    rc, stdout, stderr = _call_main(["retrieve", "not-a-hash"])
    assert rc == 2
    assert stdout == ""
    assert "invalid hash format" in stderr


def test_purge_rejects_malformed_hash_with_exit_2(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``furl purge`` applies the same up-front hash-format guard as the MCP server."""
    rc, stdout, stderr = _call_main(["purge", "XYZ"])
    assert rc == 2
    assert stdout == ""
    assert "invalid hash format" in stderr


def test_valid_but_unknown_hash_is_a_miss_not_a_format_error(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """A well-formed but unstored hash stays a MISS (exit 1), distinct from a format error."""
    rc, _stdout, stderr = _call_main(["retrieve", "0" * 24])
    assert rc == 1
    assert "not found" in stderr
    assert "invalid hash format" not in stderr


# ── High-8: active-profile banner ────────────────────────────────────────────


def test_profile_banner_on_by_default(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """Every run surfaces the active profile on stderr by default (High-8)."""
    rc, _stdout, stderr = _call_main(["stats"])
    assert rc == 0
    assert "furl profile:" in stderr
    assert "backend=memory" in stderr


def test_profile_banner_suppressible(
    inprocess_memory_store, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """FURL_PROFILE_BANNER=0 silences the banner for clean pipelines."""
    monkeypatch.setenv("FURL_PROFILE_BANNER", "0")
    rc, _stdout, stderr = _call_main(["stats"])
    assert rc == 0
    assert "furl profile:" not in stderr


def test_doctor_reports_active_profile(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """``doctor`` prints the resolved profile line (backend/TTL/scope)."""
    rc, stdout, _stderr = _call_main(["doctor"])
    assert "profile: backend=" in stdout


def test_list_preview_redacts_credentials(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """RG4: a credential never leaks through a ``furl list`` preview either.

    ``_cmd_list`` printed ``entry.original_content[:80]`` RAW while ``_cmd_search``
    routed its preview through ``build_store_redactor``. The store holds whatever
    the agent compressed, so a plain listing was a residual leak (legacy entries,
    ``FURL_REDACT_BUILTINS=0``, or a secret the store-time redactor did not match).
    """
    from furl_ctx.cache.compression_store import get_compression_store

    secret = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"  # built from parts (env secret-guard)
    store = get_compression_store()
    store.store(f"key={secret} and then some trailing log text", "c", tool_name="TestTool")
    rc, stdout, _stderr = _call_main(["list", "--limit", "300"])
    assert rc == 0
    assert secret not in stdout, "the raw credential must never appear in a list preview"
    assert "[REDACTED" in stdout


def test_list_preview_redacts_secret_straddling_the_display_edge(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """RG4: slice-before-redact with a margin, so an edge-straddling secret is masked.

    A naive ``redact(original[:80])`` would hand the redactor a TRUNCATED secret it
    no longer recognizes, leaking the visible prefix. The preview redacts a
    margin-widened window first, then truncates.
    """
    from furl_ctx.cache.compression_store import get_compression_store

    secret = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"
    # Start the secret at char 70 so it straddles the 80-char display edge. The
    # padding must END on a non-word char: the credential patterns are
    # word-bounded, so abutting the secret with `x` would make it a different
    # (non-secret) word and the fixture would prove nothing.
    padding = "x" * 69 + " "
    original = f"{padding}{secret} trailing"
    assert len(padding) == 70 and len(original) > 80, "fixture: secret must cross the edge"
    store = get_compression_store()
    store.store(original, "c", tool_name="TestTool")
    rc, stdout, _stderr = _call_main(["list", "--limit", "300"])
    assert rc == 0
    assert secret[:10] not in stdout, "a truncated secret prefix leaked at the display edge"


def test_search_preview_redacts_credentials(inprocess_memory_store) -> None:  # type: ignore[no-untyped-def]
    """A credential in a stored original never leaks through a ``furl search`` preview."""
    from furl_ctx.cache.compression_store import get_compression_store

    secret = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"  # built from parts (env secret-guard)
    store = get_compression_store()
    store.store(
        f"log before\nkey={secret}\nlog after the credential region here",
        "compressed-view",
        tool_name="TestTool",
    )
    rc, stdout, _stderr = _call_main(["search", "credential"])
    assert rc == 0
    assert secret not in stdout, "the raw credential must never appear in a search preview"
    assert "[REDACTED" in stdout
