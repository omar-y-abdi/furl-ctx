"""Audit #3 — a durable write that loses the sqlite lock race must veto the marker.

The per-tool-call hook writes while the long-lived MCP server holds the WAL
writer. After ``busy_timeout`` × retries of sustained contention
``SqliteBackend.set()`` swallowed ``_SqliteOpFailed`` and stashed the entry in
its in-process ``InMemoryBackend`` — but ``store()`` returned the key regardless,
so the hook shipped ``<<ccr:HASH>>`` and exited, DESTROYING the only (in-memory)
copy. The marker is now durable in the transcript; the original never reached
disk → later ``furl_retrieve`` loud-misses, content unrecoverable.

Fix: ``set_durable()`` reports whether the row reached the file; ``store(...,
require_durable=True)`` raises ``DurableWriteError`` on a volatile fall-open; the
marker-decision callers veto to the ORIGINAL uncompressed content. This pins:
inject the lost lock race → compress serves the ORIGINAL (no marker), failure
surfaced; a normal write still compiles a resolvable marker.
"""

from __future__ import annotations

from typing import Any

import pytest

from furl_ctx.cache.backends.sqlite import SqliteBackend, _SqliteOpFailed
from furl_ctx.cache.compression_store import (
    CompressionStore,
    DurableWriteError,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.transforms.diff_compressor import DiffCompressor, DiffCompressorConfig
from furl_ctx.transforms.log_compressor import LogCompressor, LogCompressorConfig
from tests._fixtures import make_large_diff


def _fail_open_sqlite(tmp_path) -> SqliteBackend:
    """A real SqliteBackend whose *set* op always loses the lock race — every
    write fails open to the volatile in-process fallback (audit #3's scenario).
    Reads/counts still hit the file so same-process retrieval still round-trips.
    """
    backend = SqliteBackend(db_path=tmp_path / "ccr.sqlite3")
    real_run = backend._run

    def failing_run(op_name: str, fn):
        if op_name == "set":
            raise _SqliteOpFailed()  # simulate busy_timeout×retries exhausted
        return real_run(op_name, fn)

    backend._run = failing_run  # type: ignore[method-assign]
    return backend


# ── backend / store unit level ──────────────────────────────────────────────


def test_set_durable_reports_false_on_fail_open(tmp_path) -> None:
    backend = _fail_open_sqlite(tmp_path)
    entry_store = CompressionStore(backend=backend)  # just to mint an entry
    # Craft an entry via a real store then read it back to reuse its shape.
    entry_store.store(original="x", compressed="y", explicit_hash="a" * 12)
    got = backend.get("a" * 12)
    assert got is not None  # same-process retrieval still works (volatile tier)
    # The write reported NON-durable.
    assert backend.set_durable("b" * 12, got) is False


def test_set_durable_true_on_normal_write(tmp_path) -> None:
    backend = SqliteBackend(db_path=tmp_path / "ok.sqlite3")
    store = CompressionStore(backend=backend)
    store.store(original="x", compressed="y", explicit_hash="c" * 12)
    got = backend.get("c" * 12)
    assert got is not None
    assert backend.set_durable("d" * 12, got) is True


def test_store_require_durable_raises_on_volatile_fallback(tmp_path) -> None:
    store = CompressionStore(backend=_fail_open_sqlite(tmp_path))
    with pytest.raises(DurableWriteError):
        store.store(original="s", compressed="m", explicit_hash="e" * 12, require_durable=True)
    # Same-process retrieval still works (the entry landed in the volatile tier);
    # it simply must not back a shipped marker — the caller vetoes on the raise.
    entry = store.retrieve("e" * 12)
    assert entry is not None and entry.original_content == "s"


def test_store_require_durable_noop_without_require(tmp_path) -> None:
    # Pre-fix behavior is preserved when require_durable is not requested: the
    # fail-open still returns the key (no raise) — durability veto is opt-in.
    store = CompressionStore(backend=_fail_open_sqlite(tmp_path))
    key = store.store(original="x", compressed="y", explicit_hash="f" * 12)
    assert key == "f" * 12


def test_store_require_durable_ok_on_normal_sqlite(tmp_path) -> None:
    store = CompressionStore(backend=SqliteBackend(db_path=tmp_path / "ok2.sqlite3"))
    key = store.store(original="x", compressed="y", explicit_hash="ab" * 6, require_durable=True)
    assert key == "ab" * 6
    assert store.retrieve(key) is not None


def test_memory_backend_require_durable_is_noop() -> None:
    # The default in-memory backend has no durability to lose → no veto.
    store = CompressionStore()  # InMemoryBackend
    key = store.store(original="x", compressed="y", require_durable=True)
    assert store.retrieve(key) is not None


# ── compressor level: the marker actually vetoes ────────────────────────────


def _diff_case() -> tuple[str, Any]:
    content = make_large_diff()
    c = DiffCompressor(DiffCompressorConfig(enable_ccr=True, min_lines_for_ccr=10))
    return content, c


def _log_case() -> tuple[str, Any]:
    content = "\n".join(
        [f"INFO: Processing item {i}" for i in range(200)] + ["ERROR: Failed at item 100"]
    )
    c = LogCompressor(config=LogCompressorConfig(min_lines_for_ccr=50, enable_ccr=True))
    return content, c


_CASES = {"diff": _diff_case, "log": _log_case}


@pytest.fixture
def fail_open_store(tmp_path) -> Any:
    store = CompressionStore(backend=_fail_open_sqlite(tmp_path), enable_feedback=False)
    set_request_compression_store(store)
    yield store
    clear_request_compression_store()


@pytest.fixture
def durable_store(tmp_path) -> Any:
    store = CompressionStore(
        backend=SqliteBackend(db_path=tmp_path / "d.sqlite3"), enable_feedback=False
    )
    set_request_compression_store(store)
    yield store
    clear_request_compression_store()


@pytest.mark.parametrize("producer", list(_CASES))
def test_compress_vetoes_marker_when_durable_write_fails(
    producer: str, fail_open_store: Any
) -> None:
    original, compressor = _CASES[producer]()
    result = compressor.compress(original)
    assert result.cache_key is None, (
        f"{producer}: a marker shipped despite the durable write failing (audit #3)"
    )
    assert result.compressed == original, (
        f"{producer}: served a marker instead of reverting to the original"
    )


@pytest.mark.parametrize("producer", list(_CASES))
def test_compress_emits_resolvable_marker_on_durable_write(
    producer: str, durable_store: Any
) -> None:
    original, compressor = _CASES[producer]()
    result = compressor.compress(original)
    assert result.cache_key is not None, f"{producer}: CCR did not fire on a healthy store"
    assert result.compressed != original
    entry = durable_store.retrieve(result.cache_key)
    assert entry is not None and entry.original_content == original
