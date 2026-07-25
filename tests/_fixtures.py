"""Shared load-bearing test fixtures (TEST-19).

Single canonical home for fixtures/helpers that were previously duplicated
verbatim across test files — where retuning one copy silently rotted its
siblings:

* ``log_shaped_rows`` — the delicately tuned ALWAYS-LOSSY fixture. It was
  copied between ``test_ccr_recovery_invariant.py`` (as ``_log_shaped_rows``)
  and ``test_result_cache_ccr_divergence.py`` (as ``_log_rows``); the
  divergence suite rots to vacuous-green if its copy drifts back onto the
  lossless path. ``assert_fixture_drops()`` is the canary for that tuning.
* ``canonical_repr`` / ``decode_csv_schema_into`` — the recovery-comparison
  helpers ``test_lossless_column_encodings.py`` used to import ACROSS test
  files from ``tests.test_ccr_recovery_invariant``.
* ``FailingStore`` — the raising-store wrapper triplicated across the
  persist-failure/mirror/code-aware suites.
* ``make_large_diff`` — the CCR-triggering synthetic git diff duplicated
  between the diff-compressor suites.
* ``high_entropy_logs`` / ``find_sentinel`` / ``sentinel_from_output`` /
  ``hash_from_marker`` — the row-drop-forcing generator and its sentinel/marker
  readers, duplicated between ``test_ccr_proportional_retrieval.py`` and
  ``test_ccr_rows_marker_removed.py`` (the latter's docstring admitted the copy).
  The test FUNCTIONS in those files stay separate: they pin different invariants.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from typing import Any

from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows

# ── the tuned lossy log fixture ─────────────────────────────────────────────

_SUBJECT_PREFIXES = ["feat", "fix", "docs", "chore", "refactor", "test", "perf", "ci"]
_SUBJECT_AREAS = [
    "crusher",
    "proxy",
    "ccr",
    "router",
    "bench",
    "tokenizer",
    "store",
    "pipeline",
    "compaction",
    "relevance",
]
_SUBJECT_VERBS = [
    "add",
    "remove",
    "rework",
    "guard",
    "pin",
    "extend",
    "isolate",
    "deflake",
    "speed up",
    "harden",
]
_SUBJECT_THINGS = [
    "the lossy budget",
    "novelty fill",
    "sentinel emission",
    "marker parsing",
    "store mirroring",
    "field-role gates",
    "ditto marks",
    "schema folding",
    "query anchors",
    "drop accounting",
    "TTL handling",
    "thread-local state",
    "import guards",
    "error surfaces",
    "byte parity",
]


def log_shaped_rows(n: int = 90) -> list[dict]:
    """High-entropy distinct rows (git-log shaped) that force the LOSSY path.

    Tuning contract (do not "simplify" any of it):

    * hex identity columns + low-cardinality author + genuinely varied
      unique subjects — uniformly templated subjects trip the engine's
      ``skip:unique_entities_no_signal`` crushability gate and never reach
      the lossy path;
    * the dates carry MICROSECOND precision deliberately: strict-shape
      second-precision ISO columns delta-encode losslessly, which pushed an
      earlier fixture over the 0.30 lossless gate and off the lossy path
      the consumer suites exist to pin. Fractional seconds are entirely
      realistic for logs and are (honestly) refused by the strict encoder,
      keeping this fixture lossy.

    ``assert_fixture_drops()`` is the canary that this tuning still holds.
    """
    return [
        {
            "commit": f"{i * 2654435761 + 12345:040x}",
            "author": f"Author {i % 7}",
            "date": (
                f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
                f"T{i % 24:02d}:{(i * 13) % 60:02d}:00.{(i * 104729) % 1000000:06d}+02:00"
            ),
            "subject": (
                f"{_SUBJECT_PREFIXES[i % 8]}({_SUBJECT_AREAS[i % 10]}): "
                f"{_SUBJECT_VERBS[i % 10]} {_SUBJECT_THINGS[i % 15]} #{i + 100}"
            ),
        }
        for i in range(n)
    ]


def assert_fixture_drops() -> None:
    """Self-check: ``log_shaped_rows`` still routes LOSSY and emits a CCR drop.

    Every suite built on this fixture silently rots to vacuous-green if a
    routing/tuning change tips it onto the lossless path (nothing dropped →
    nothing to recover → recovery asserts trivially hold). Call this once
    per consumer suite so the rot is a loud failure naming the cause.
    """
    from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig

    result = ContentRouter(ContentRouterConfig()).compress(
        json.dumps(log_shaped_rows(), ensure_ascii=False)
    )
    assert "<<ccr:" in result.compressed, (
        "TEST-19 fixture canary: log_shaped_rows() no longer produces a CCR "
        "drop sentinel — the fixture drifted onto the lossless path and every "
        "recovery/divergence suite built on it is now vacuous. Re-tune the "
        "fixture (see its docstring) before trusting those suites."
    )


# ── the high-entropy (row-drop-forcing) log fixture + sentinel readers ──────


def high_entropy_logs(n: int, seed: int) -> list[dict]:
    """Deterministic, real-shaped, NEAR-UNIQUE log rows — the exact tier where
    the single-blob model collapsed and where the lossless compactor is defeated
    into a lossy row drop (fresh uuid-ish id + random sha-ish commit + per-row
    service/level/message). Generated from a seeded SHA stream so it is
    reproducible without a committed fixture.

    Previously duplicated as ``_high_entropy_logs`` in
    ``test_ccr_proportional_retrieval.py`` and ``test_ccr_rows_marker_removed.py``.
    """
    rows: list[dict] = []
    services = ["api", "worker", "scheduler", "auth", "billing", "ingest"]
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    for i in range(n):
        h = hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
        rows.append(
            {
                "id": h[:32],
                # Defensive: never slice past the digest even if the hash
                # function's width ever shrinks.
                "commit": h[32:72] if len(h) >= 72 else (h + h)[32:72],
                "service": services[int(h[:2], 16) % len(services)],
                "level": levels[int(h[2:4], 16) % len(levels)],
                "latency_ms": int(h[4:8], 16) % 5000,
                "message": f"request {h[8:20]} handled in span {h[20:28]}",
            }
        )
    return rows


def find_sentinel(node: object) -> dict | None:
    """Return the ``{"_ccr_dropped": ...}`` sentinel object from the parsed
    output tree, if present."""
    if isinstance(node, dict):
        if "_ccr_dropped" in node:
            return node
        for v in node.values():
            found = find_sentinel(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for x in node:
            found = find_sentinel(x)
            if found is not None:
                return found
    return None


def sentinel_from_output(compressed: str) -> dict | None:
    """Locate the ``{"_ccr_dropped": ...}`` sentinel in a compressed output.
    Two renders are possible: a JSON array/object tree whose last element is the
    sentinel (the plain lossy row-drop path), or a JSON STRING wrapping a
    CSV-schema table whose LAST LINE is the sentinel (the survivor-compaction
    path).
    """
    tree = json.loads(compressed)
    found = find_sentinel(tree)
    if found is not None:
        return found
    if isinstance(tree, str):
        last_line = tree.strip().rsplit("\n", 1)[-1]
        try:
            obj = json.loads(last_line)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(obj, dict) and "_ccr_dropped" in obj:
            return obj
    return None


def hash_from_marker(marker: str) -> str:
    """Pull the KEY out of ``<<ccr:KEY <sep>...>>``."""
    start = marker.index("<<ccr:") + len("<<ccr:")
    rest = marker[start:]
    return rest[: rest.index(" ")]


# ── recovery-comparison helpers (previously cross-imported between files) ───


def canonical_repr(x: object) -> str:
    """Order-independent canonical JSON repr used for row-set comparisons."""
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def decode_csv_schema_into(text: str, recovered: set[str]) -> None:
    """Decode a lossless CSV-schema body (``[N]{cols}\\n<rows>``) back to
    JSON objects via the documented reference decoder
    (``furl_ctx.transforms.csv_schema_decoder``). Those rows are exactly
    reconstructible from the output — lossless — so they count as
    recovered-from-output-alone.

    The decoder understands every column encoding the CSV-schema
    formatter emits (constant fold, ditto marks, and the reversible
    column encodings); "recoverable" here means decode-and-compare
    equality, not verbatim string presence.
    """
    rows = decode_csv_schema_rows(text)
    if rows is None:
        return
    for row in rows:
        recovered.add(canonical_repr(row))


# ── failing-store wrapper (previously triplicated) ──────────────────────────


class FailingStore:
    """A store whose ``store()`` always raises (simulating a Python
    compression_store write failure during the mirror). Every other attribute
    delegates to a real store so the compressor's other reads still behave.
    ``store_calls`` lets a test assert the CCR path was actually exercised —
    guarding against a vacuous GREEN where no marker was ever produced."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.store_calls = 0

    def store(self, *args: Any, **kwargs: Any) -> str:
        self.store_calls += 1
        raise RuntimeError("INJECTED compression_store write failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ── fail-open sqlite backend (durable-write-veto suites) ────────────────────


def make_fail_open_sqlite_backend(db_path: Any) -> Any:
    """A real ``SqliteBackend`` whose *set* op always loses the lock race —
    every write fails open to the volatile in-process fallback (audit #3's
    scenario). Reads/counts still hit the file, so same-process retrieval
    still round-trips; only durability is lost. Canonical home (TEST-19) for
    the helper the durable-veto suites (store / read_lifecycle / MCP) share.
    """
    from furl_ctx.cache.backends.sqlite import SqliteBackend, _SqliteOpFailed

    backend = SqliteBackend(db_path=db_path)
    real_run = backend._run

    def failing_run(op_name: str, fn: Any) -> Any:
        if op_name == "set":
            raise _SqliteOpFailed()  # simulate busy_timeout x retries exhausted
        return real_run(op_name, fn)

    backend._run = failing_run  # type: ignore[method-assign]
    return backend


# ── CCR-triggering synthetic git diff (previously duplicated) ───────────────


def make_large_diff(n_files: int = 5, hunks_each: int = 20) -> str:
    """A synthetic git diff well above ``min_lines_for_ccr`` — proven to
    emit a CCR marker. Generates well above the default threshold (50) so
    consumers stay robust to minor threshold tweaks."""
    parts: list[str] = []
    for i in range(n_files):
        parts.append(
            textwrap.dedent(
                f"""\
                diff --git a/src/module_{i}.py b/src/module_{i}.py
                index abc1234..def5678 100644
                --- a/src/module_{i}.py
                +++ b/src/module_{i}.py
                """
            )
        )
        for h in range(hunks_each):
            parts.append(
                textwrap.dedent(
                    f"""\
                    @@ -{h * 10 + 1},{h * 10 + 6} +{h * 10 + 1},{h * 10 + 6} @@
                     context line one for file {i} hunk {h}
                     context line two for file {i} hunk {h}
                    -old code line A in file {i} hunk {h}
                    +new code line A in file {i} hunk {h}
                    -old code line B in file {i} hunk {h}
                    +new code line B in file {i} hunk {h}
                     context line three for file {i} hunk {h}
                    """
                )
            )
    return "".join(parts)
