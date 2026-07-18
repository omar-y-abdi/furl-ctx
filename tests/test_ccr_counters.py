"""Cross-process observability counters on the CCR store.

The PostToolUse hook and the (on-by-default) PreToolUse pipe are short-lived subprocesses;
the ``furl`` MCP server is long-lived. For ``furl_stats`` to surface hook activity
it can't see directly (Finding B / #68951 diagnostic), the counters must persist
in the SAME durable per-project sqlite file both sides open. These tests pin:

* the in-memory backend tallies PROCESS-LOCALLY (``counters_durable`` False), so a
  first-run gate never fires there and library/unit-test runs stay quiet;
* the sqlite backend tallies DURABLY — new-value read-back, survival across a
  reopen, and (the real point) a SEPARATE PROCESS reads the parent's increments;
* ``increment_counter`` is atomic and monotonic; ``clear`` resets counters;
* the store layer is fail-open: an unsupported backend is a silent no-op.
"""

from __future__ import annotations

import base64
import subprocess
import sys
import textwrap

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore


def test_memory_backend_counter_is_process_local() -> None:
    backend = InMemoryBackend()
    assert backend.increment_counter("hook_invocations_seen") == 1
    assert backend.increment_counter("hook_invocations_seen", 2) == 3
    assert backend.get_counters() == {"hook_invocations_seen": 3}
    backend.clear()
    assert backend.get_counters() == {}


def test_store_on_memory_is_not_durable() -> None:
    store = CompressionStore(backend=InMemoryBackend())
    assert store.increment_counter("hook_invocations_seen") == 1
    assert store.increment_counter("hook_invocations_seen") == 2
    assert store.get_counters() == {"hook_invocations_seen": 2}
    # The in-memory tally is per-process, so it must report NON-durable — this is
    # what keeps the hook's once-per-store first-run note from firing in tests.
    assert store.counters_durable is False


def test_store_on_sqlite_is_durable_and_reads_back(tmp_path) -> None:
    store = CompressionStore(backend=SqliteBackend(db_path=tmp_path / "c.sqlite3"))
    assert store.counters_durable is True
    assert store.increment_counter("hook_invocations_seen") == 1
    assert store.increment_counter("hook_compressions_applied") == 1
    assert store.increment_counter("hook_invocations_seen") == 2
    assert store.get_counters() == {
        "hook_invocations_seen": 2,
        "hook_compressions_applied": 1,
    }
    store.close()


def test_sqlite_counters_survive_reopen(tmp_path) -> None:
    """A durable counter outlives the process that wrote it (the whole point:
    the hook subprocess increments, the long-lived server reads later)."""
    db = tmp_path / "shared.sqlite3"
    writer = CompressionStore(backend=SqliteBackend(db_path=db))
    writer.increment_counter("hook_invocations_seen", 5)
    writer.close()

    reader = CompressionStore(backend=SqliteBackend(db_path=db))
    assert reader.get_counters() == {"hook_invocations_seen": 5}
    # And continues from the persisted value, not from zero.
    assert reader.increment_counter("hook_invocations_seen") == 6
    reader.close()


def test_clear_resets_sqlite_counters(tmp_path) -> None:
    store = CompressionStore(backend=SqliteBackend(db_path=tmp_path / "c.sqlite3"))
    store.increment_counter("hook_invocations_seen", 3)
    store.clear()
    assert store.get_counters() == {}
    store.close()


def test_cross_process_counter_read(tmp_path) -> None:
    """The real cross-process case: a SEPARATE PROCESS reads the increments this
    process durably wrote to the shared sqlite file (mirrors the cross-process
    retrieve test)."""
    db = tmp_path / "shared.sqlite3"
    parent = CompressionStore(backend=SqliteBackend(db_path=db))
    parent.increment_counter("hook_invocations_seen", 4)
    parent.increment_counter("hook_noop:below-min-chars", 2)
    parent.close()

    reader = tmp_path / "reader.py"
    reader.write_text(
        textwrap.dedent(
            """\
            import base64, json, sys
            from furl_ctx.cache.backends.sqlite import SqliteBackend
            from furl_ctx.cache.compression_store import CompressionStore

            store = CompressionStore(backend=SqliteBackend(db_path=sys.argv[1]))
            # Increment from the OTHER process too, then report the merged tally.
            store.increment_counter("hook_invocations_seen")
            payload = json.dumps(store.get_counters()).encode()
            sys.stdout.buffer.write(base64.b64encode(payload))
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(reader), str(db)],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr.decode()!r}"
    import json as _json

    counters = _json.loads(base64.b64decode(proc.stdout).decode())
    # 4 from the parent + 1 from the child = 5 invocations, both processes agree.
    assert counters == {"hook_invocations_seen": 5, "hook_noop:below-min-chars": 2}


def test_store_counters_fail_open_on_unsupported_backend() -> None:
    """A backend without counter methods degrades silently — counters are
    advisory and must never break the store (the pinned-older-engine case)."""

    class _CounterlessBackend(InMemoryBackend):
        # Shadow the extras so getattr() finds no callable — a backend that
        # predates the counter API. (set_durable is already absent here.)
        increment_counter = None  # type: ignore[assignment]
        get_counters = None  # type: ignore[assignment]

    store = CompressionStore(backend=_CounterlessBackend())
    assert store.increment_counter("hook_invocations_seen") is None
    assert store.get_counters() == {}
    assert store.counters_durable is False
