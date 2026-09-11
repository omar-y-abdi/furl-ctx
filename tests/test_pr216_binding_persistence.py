"""Restart-level regression coverage for PR #216 explicit-hash safety."""

from __future__ import annotations

from typing import Any

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore

HASH_KEY = "a" * 24


def _open_store(db_path: Any) -> tuple[SqliteBackend, CompressionStore]:
    backend = SqliteBackend(db_path=db_path, max_rows=100)
    return backend, CompressionStore(backend=backend, enable_feedback=False)


def test_explicit_hash_binding_and_poison_state_survive_restart(tmp_path: Any) -> None:
    """A process restart must not erase ownership or a discovered collision."""
    db_path = tmp_path / "binding-persistence.sqlite3"

    first_backend, first = _open_store(db_path)
    first.store("producer-A", "view-A", explicit_hash=HASH_KEY)
    entry = first.retrieve(HASH_KEY)
    assert entry is not None
    assert entry.original_content == "producer-A"
    first_backend.close()

    second_backend, second = _open_store(db_path)
    entry = second.retrieve(HASH_KEY)
    assert entry is not None
    assert entry.original_content == "producer-A"

    # A different producer poisons the explicit key and verified cleanup removes
    # the ambiguous row. The poison record is deliberately not released by
    # collision cleanup: otherwise either side could win after a restart.
    second.store("producer-B", "view-B", explicit_hash=HASH_KEY)
    assert second.retrieve(HASH_KEY) is None
    binding = second_backend.get_binding(HASH_KEY)
    assert binding is not None and binding[1] is True
    second_backend.close()

    third_backend, third = _open_store(db_path)
    try:
        # Neither producer may resurrect a key whose ownership was proven
        # ambiguous in a previous process.
        third.store("producer-A", "view-A", explicit_hash=HASH_KEY)
        third.store("producer-B", "view-B", explicit_hash=HASH_KEY)
        assert third.retrieve(HASH_KEY) is None
        binding = third_backend.get_binding(HASH_KEY)
        assert binding is not None and binding[1] is True
    finally:
        third_backend.close()
