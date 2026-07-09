"""furl CLI: compress (stdin -> stdout), retrieve (miss + slice flags), doctor."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from typing import Any


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
    ratio = float(proc.stdout.split("corpus compression ratio:")[1].split("%")[0])
    assert 0.0 < ratio < 100.0
    # The needle-recall trust gate is 100% on a healthy engine (the naming arm
    # recalls its needle by construction); it drops if compression starts
    # silently losing content.
    recall = float(proc.stdout.split("trust gate):")[1].split("%")[0])
    assert recall == 100.0


def test_eval_requires_recall_flag(tmp_path) -> None:  # type: ignore[no-untyped-def]
    corpus = tmp_path / "rows.json"
    corpus.write_text(_big_array(), encoding="utf-8")
    proc = _run(["eval", str(corpus)])
    assert proc.returncode == 2  # argparse: missing required --recall
    assert "--recall" in proc.stderr


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
