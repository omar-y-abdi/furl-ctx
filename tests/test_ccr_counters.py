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

import pytest

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
        # Shadow the OPTIONAL counter extras so getattr() finds no callable — a
        # backend that predates the counter API. It still satisfies the REQUIRED
        # durability contract by inheritance, which is the distinction: the
        # counter extras are advisory and fail open, ``durable``/``set_durable``
        # are required and fail loudly. (An earlier comment here claimed
        # "set_durable is already absent" — true when the store inferred
        # durability from that absence, false now that InMemoryBackend declares
        # itself volatile explicitly.)
        increment_counter = None  # type: ignore[assignment]
        get_counters = None  # type: ignore[assignment]

    store = CompressionStore(backend=_CounterlessBackend())
    assert store.increment_counter("hook_invocations_seen") is None
    assert store.get_counters() == {}
    # Volatile by DECLARATION now, not by the absence of a set_durable attribute.
    assert store.counters_durable is False


def test_backend_missing_durability_contract_is_rejected_at_construction() -> None:
    """A duck-typed backend that does not declare the durability contract is
    rejected when the store is BUILT, naming the missing members.

    The extension seam this protects: backends loaded through the
    ``furl_ctx.ccr_backend`` entry point group are duck-typed at runtime and never
    meet the type checker. Before the contract existed, such a backend reached
    ``getattr(backend, "set_durable", None) is None`` and the store answered
    "durability satisfied" — so a ``require_durable=True`` write never vetoed and
    a marker could ship for content whose durability was never checked.

    Failing is the fix. Failing at CONSTRUCTION, naming the members and pointing
    at the Protocol, is what keeps it from surfacing later as a bare
    ``AttributeError`` raised out of a persist for a member the author never knew
    existed.
    """

    class _NoDurabilityContract:
        """Implements the storage methods but declares no durability."""

        def __init__(self) -> None:
            self._inner = InMemoryBackend()

        def __getattr__(self, name: str):
            if name in {"_inner", "durable", "set_durable"}:
                raise AttributeError(name)
            return getattr(self._inner, name)

    with pytest.raises(TypeError, match="durable") as excinfo:
        CompressionStore(backend=_NoDurabilityContract(), enable_feedback=False)

    message = str(excinfo.value)
    assert "set_durable" in message, f"the error must name every missing member: {message}"
    assert "_NoDurabilityContract" in message, f"the error must name the backend: {message}"


def test_declared_volatile_backend_is_accepted_and_not_treated_as_durable() -> None:
    """The counterpart: DECLARING volatility is accepted and honoured.

    Without this, the rejection above could be satisfied by a store that simply
    refuses every non-sqlite backend, which would break the in-memory default.
    The store must accept a backend that says "I am not durable" and then report
    exactly that — the declaration is the answer, not the presence of a method.
    """
    store = CompressionStore(backend=InMemoryBackend(), enable_feedback=False)
    assert store.counters_durable is False
    assert InMemoryBackend().durable is False
    assert InMemoryBackend().set_durable("h", None) is False  # honest, not "satisfied"


# --------------------------------------------------------------------------- #
# Negative-amount guard. These counters are MONOTONIC cumulative tallies, so a
# negative increment has no meaning and silently corrupts the total it lands in.
# The guard must hold at every layer a caller can reach, and each layer is a
# genuinely separate reachability question, not a repetition:
#
#   * the memory backend — the volatile tally;
#   * the SQLITE DURABLE path — a healthy backend NEVER touches ``self._memory``
#     (it only delegates there when degraded or lock-lost), so a check living
#     only in the memory backend would leave the primary durable path unguarded.
#     This is the delegation gap the guard is really about;
#   * the STORE surface — ``CompressionStore.increment_counter`` is documented
#     fail-open and catches ``Exception`` broadly, so a ValueError raised by a
#     backend is swallowed and returned as an indistinguishable ``None``. Without
#     a store-level check the guard would exist in the backends and be invisible
#     through the public API every real caller uses.
# --------------------------------------------------------------------------- #


def test_memory_backend_rejects_negative_counter_amount() -> None:
    backend = InMemoryBackend()
    backend.increment_counter("hook_invocations_seen", 5)

    with pytest.raises(ValueError, match="must be >= 0"):
        backend.increment_counter("hook_invocations_seen", -1)

    # Rejected, not partially applied: the tally is untouched.
    assert backend.get_counters()["hook_invocations_seen"] == 5


def test_sqlite_durable_path_rejects_negative_counter_amount(tmp_path) -> None:
    """The DURABLE path rejects it, not just the volatile fallback it delegates
    to when degraded. A healthy backend never reaches ``self._memory``, so this
    is the assertion that a memory-only guard cannot satisfy."""
    backend = SqliteBackend(db_path=tmp_path / "counters.sqlite3")
    try:
        assert backend._degraded is False, "precondition: backend must be healthy"
        assert backend.increment_counter("hook_invocations_seen", 5) == 5

        with pytest.raises(ValueError, match="must be >= 0"):
            backend.increment_counter("hook_invocations_seen", -1)

        # The durable file is unchanged — nothing was written before the reject.
        assert backend.get_counters()["hook_invocations_seen"] == 5
    finally:
        backend.close()


def test_store_surface_rejects_negative_counter_amount_despite_fail_open() -> None:
    """The rejection must survive the store's fail-open handler. If the check
    lived only in the backend, this ``pytest.raises`` would fail: the broad
    ``except Exception`` would convert the caller's bug into a silent ``None``
    that looks exactly like an unsupported backend."""
    store = CompressionStore(backend=InMemoryBackend(), enable_feedback=False)
    store.increment_counter("hook_invocations_seen", 5)

    with pytest.raises(ValueError, match="must be >= 0"):
        store.increment_counter("hook_invocations_seen", -1)

    assert store.get_counters()["hook_invocations_seen"] == 5


def test_counter_amount_zero_and_positive_still_accepted(tmp_path) -> None:
    """The guard rejects ONLY negatives. Zero stays legal (a computed step of 0
    is a harmless no-op) — pinned so the guard is never tightened into rejecting
    a legitimate caller."""
    memory = InMemoryBackend()
    assert memory.increment_counter("c", 0) == 0
    assert memory.increment_counter("c", 3) == 3

    store = CompressionStore(backend=InMemoryBackend(), enable_feedback=False)
    assert store.increment_counter("c", 0) == 0

    backend = SqliteBackend(db_path=tmp_path / "zero.sqlite3")
    try:
        assert backend.increment_counter("c", 0) == 0
        assert backend.increment_counter("c", 2) == 2
    finally:
        backend.close()
