"""Durable SQLite CCR backend (Engine P1-7) — backend-level contract tests.

The backend restores the durability upstream ships by default: an MCP server
restart must not destroy retrievable originals, and a sub-agent process must be
able to retrieve a hash the main-agent process wrote. These tests pin:

* ``CompressionStoreBackend`` Protocol conformance, run against BOTH the
  in-memory reference backend and the SQLite backend (same expectations).
* Byte-exact round-trips for hostile payloads (lone surrogates, control chars,
  NULs, 10MiB-scale bodies) — originals must come back identical.
* Retention: startup purge of expired rows, opportunistic purge every N puts,
  and a max-rows cap with oldest-first (by ``created_at``) eviction.
* Security: 0600 database file, 0700 created parent directory.
* Failure containment: a corrupt database file fails open to the in-memory
  fallback with exactly ONE loud ERROR log (never an exception to the host);
  lock contention gets a bounded retry then fails open for that operation only.
* Cross-process retrieval through a real subprocess (the sub-agent case).
* The env-selected backend loader resolves ``FURL_CCR_BACKEND=sqlite`` while
  the library default stays in-memory.
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from furl_ctx import paths
from furl_ctx.cache.backends import CompressionStoreBackend, InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    CompressionEntry,
    CompressionStore,
    get_compression_store,
    reset_compression_store,
)

_BACKEND_LOGGER = "furl_ctx.cache.backends.sqlite"


def _entry(
    hash_key: str,
    original: str,
    *,
    compressed: str | None = None,
    created_at: float | None = None,
    ttl: int = 3600,
    retrieval_count: int = 0,
    search_queries: list[str] | None = None,
    last_accessed: float | None = None,
    query_context: str | None = "why was this compressed",
) -> CompressionEntry:
    return CompressionEntry(
        hash=hash_key,
        original_content=original,
        compressed_content=compressed if compressed is not None else f"<<ccr:{hash_key}>>",
        original_tokens=10,
        compressed_tokens=2,
        original_item_count=3,
        compressed_item_count=1,
        tool_name="test_tool",
        tool_call_id="call-1",
        query_context=query_context,
        created_at=created_at if created_at is not None else time.time(),
        ttl=ttl,
        compression_strategy="test_strategy",
        retrieval_count=retrieval_count,
        search_queries=search_queries if search_queries is not None else [],
        last_accessed=last_accessed,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Isolate every test from the developer environment and ~/.furl.

    The default database path honors the workspace-dir contract; point the
    workspace at the test sandbox so no test can touch the real ~/.furl, and
    clear every backend-selection env var.
    """
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    monkeypatch.delenv("FURL_CCR_SQLITE_PATH", raising=False)
    monkeypatch.delenv("FURL_CCR_SQLITE_MAX_ROWS", raising=False)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    """Both concrete backends, driven through the identical Protocol suite."""
    if request.param == "memory":
        return InMemoryBackend()
    return SqliteBackend(db_path=tmp_path / "conformance.sqlite3")


# --------------------------------------------------------------------------- #
# Protocol conformance — memory and sqlite must behave identically.
# --------------------------------------------------------------------------- #


def test_backend_satisfies_protocol(backend) -> None:
    assert isinstance(backend, CompressionStoreBackend)


def test_set_get_round_trips_full_entry_equality(backend) -> None:
    entry = _entry(
        "abcdef123456",
        '[{"id": 0, "name": "alice"}]',
        retrieval_count=2,
        search_queries=["alice", "bob"],
        last_accessed=1234567890.5,
    )
    backend.set("abcdef123456", entry)
    got = backend.get("abcdef123456")
    assert got == entry  # dataclass equality: every field byte-/value-exact


def test_get_unknown_key_returns_none(backend) -> None:
    assert backend.get("feedfacefeed") is None


def test_exists_true_for_live_false_for_unknown(backend) -> None:
    backend.set("abcdef123456", _entry("abcdef123456", "[1]"))
    assert backend.exists("abcdef123456") is True
    assert backend.exists("feedfacefeed") is False


def test_delete_returns_true_then_false(backend) -> None:
    backend.set("abcdef123456", _entry("abcdef123456", "[1]"))
    assert backend.delete("abcdef123456") is True
    assert backend.delete("abcdef123456") is False
    assert backend.get("abcdef123456") is None


def test_overwrite_same_key_replaces_entry(backend) -> None:
    backend.set("abcdef123456", _entry("abcdef123456", "[1]"))
    replacement = _entry("abcdef123456", "[1, 2, 3]", retrieval_count=9)
    backend.set("abcdef123456", replacement)
    assert backend.count() == 1
    assert backend.get("abcdef123456") == replacement


def test_clear_empties_storage(backend) -> None:
    backend.set("abcdef123456", _entry("abcdef123456", "[1]"))
    backend.set("123456abcdef", _entry("123456abcdef", "[2]"))
    backend.clear()
    assert backend.count() == 0
    assert backend.keys() == []
    assert backend.items() == []


def test_count_and_keys_track_contents(backend) -> None:
    assert backend.count() == 0
    backend.set("abcdef123456", _entry("abcdef123456", "[1]"))
    backend.set("123456abcdef", _entry("123456abcdef", "[2]"))
    assert backend.count() == 2
    assert sorted(backend.keys()) == ["123456abcdef", "abcdef123456"]


def test_items_returns_all_pairs(backend) -> None:
    e1 = _entry("abcdef123456", "[1]")
    e2 = _entry("123456abcdef", "[2]")
    backend.set("abcdef123456", e1)
    backend.set("123456abcdef", e2)
    assert sorted(backend.items(), key=lambda kv: kv[0]) == [
        ("123456abcdef", e2),
        ("abcdef123456", e1),
    ]


def test_get_stats_reports_entry_count_and_backend_type(backend) -> None:
    backend.set("abcdef123456", _entry("abcdef123456", "[1]"))
    stats = backend.get_stats()
    assert stats["entry_count"] == 1
    assert isinstance(stats["backend_type"], str) and stats["backend_type"]


# --------------------------------------------------------------------------- #
# Byte-exact round-trips — hostile payloads (sqlite-specific pins).
# --------------------------------------------------------------------------- #

_WEIRD_PAYLOADS = {
    "lone_surrogate_json": '{"bad": "\ud800", "n": 1}',
    "paired_lone_surrogates": "prefix 😀 suffix",
    "control_chars": "\x00\x01\x02 \x1b[31mred\x1b[0m \r\n\ttail",
    "nul_heavy": "a\x00b\x00c\x00",
    "escaped_surrogate_literal": '{"k": "\\ud800 stays escaped"}',
    "unicode_mix": "héllo — ünïcode 😀 ✓ 中文  line-sep",
}


@pytest.mark.parametrize("payload", sorted(_WEIRD_PAYLOADS))
def test_round_trip_weird_payloads_byte_exact(tmp_path, payload: str) -> None:
    """Originals may be non-UTF8-safe JSON (lone surrogates), carry control
    chars, or embed NULs — retrieval must return the IDENTICAL string."""
    original = _WEIRD_PAYLOADS[payload]
    backend = SqliteBackend(db_path=tmp_path / "weird.sqlite3")
    backend.set("abcdef123456", _entry("abcdef123456", original, compressed=original[::-1]))
    got = backend.get("abcdef123456")
    assert got is not None
    assert got.original_content == original
    assert got.compressed_content == original[::-1]


def test_round_trip_10mib_payload_byte_exact(tmp_path) -> None:
    line = "x" * 1023 + "\n"
    original = line * (10 * 1024) + "\ud800\x00 tail"  # 10 MiB + hostile tail
    assert len(original) > 10 * 1024 * 1024
    backend = SqliteBackend(db_path=tmp_path / "big.sqlite3")
    backend.set("abcdef123456", _entry("abcdef123456", original))
    got = backend.get("abcdef123456")
    assert got is not None
    assert got.original_content == original


def test_metadata_fields_with_weird_chars_round_trip(tmp_path) -> None:
    backend = SqliteBackend(db_path=tmp_path / "meta.sqlite3")
    entry = _entry(
        "abcdef123456",
        "[1]",
        query_context="query with lone surrogate \udfff and NUL \x00",
        search_queries=["find \ud800", "tab\there"],
    )
    backend.set("abcdef123456", entry)
    got = backend.get("abcdef123456")
    assert got == entry


def test_hash_keys_are_case_sensitive_opaque(tmp_path) -> None:
    """Keys are opaque strings: no case folding, no normalization."""
    backend = SqliteBackend(db_path=tmp_path / "keys.sqlite3")
    lower = _entry("abcdef123456", "[1]")
    mixed = _entry("AbCdEf123456", "[2]")
    backend.set("abcdef123456", lower)
    backend.set("AbCdEf123456", mixed)
    assert backend.count() == 2
    assert backend.get("abcdef123456") == lower
    assert backend.get("AbCdEf123456") == mixed
    assert backend.get("ABCDEF123456") is None


# --------------------------------------------------------------------------- #
# Durability + retention.
# --------------------------------------------------------------------------- #


def test_entries_survive_backend_restart(tmp_path) -> None:
    """The P1-7 headline: a process restart must not destroy originals."""
    db = tmp_path / "restart.sqlite3"
    original = '{"secret-shaped": "payload", "weird": "\ud800\x00"}'
    first = SqliteBackend(db_path=db)
    first.set("abcdef123456", _entry("abcdef123456", original))
    del first

    reborn = SqliteBackend(db_path=db)
    got = reborn.get("abcdef123456")
    assert got is not None
    assert got.original_content == original


def test_startup_purges_expired_rows_only(tmp_path) -> None:
    db = tmp_path / "purge.sqlite3"
    writer = SqliteBackend(db_path=db)
    now = time.time()
    writer.set("expiredexpired", _entry("expiredexpired", "[1]", created_at=now - 100, ttl=10))
    writer.set("livelivelive", _entry("livelivelive", "[2]", created_at=now, ttl=3600))
    assert writer.count() == 2

    fresh = SqliteBackend(db_path=db)  # startup purge runs here
    assert fresh.get("expiredexpired") is None
    live = fresh.get("livelivelive")
    assert live is not None and live.original_content == "[2]"
    assert fresh.keys() == ["livelivelive"]


def test_opportunistic_purge_fires_every_n_puts(tmp_path) -> None:
    backend = SqliteBackend(db_path=tmp_path / "opp.sqlite3", purge_every_n_puts=2)
    now = time.time()
    backend.set("expiredexpired", _entry("expiredexpired", "[1]", created_at=now - 100, ttl=10))
    assert backend.exists("expiredexpired")  # put #1: no purge yet
    backend.set("livelivelive", _entry("livelivelive", "[2]", created_at=now, ttl=3600))
    assert backend.keys() == ["livelivelive"]  # put #2 purged the expired row


def test_max_rows_cap_evicts_oldest_first(tmp_path) -> None:
    """Cap eviction is oldest-``created_at``-first — the same ordering as the
    in-memory plane's generation-FIFO — regardless of insertion order."""
    backend = SqliteBackend(db_path=tmp_path / "cap.sqlite3", max_rows=3)
    base = time.time()
    for key, offset in [
        ("off20a1b2c3d4", -20.0),
        ("off40a1b2c3d4", -40.0),
        ("off00a1b2c3d4", 0.0),
        ("off30a1b2c3d4", -30.0),
        ("off10a1b2c3d4", -10.0),
    ]:
        backend.set(key, _entry(key, f"[{offset}]", created_at=base + offset, ttl=3600))
    assert backend.count() == 3
    assert sorted(backend.keys()) == sorted(["off00a1b2c3d4", "off10a1b2c3d4", "off20a1b2c3d4"])


def test_max_rows_env_override_applies(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FURL_CCR_SQLITE_MAX_ROWS", "2")
    backend = SqliteBackend(db_path=tmp_path / "env-cap.sqlite3")
    base = time.time()
    for i, key in enumerate(["aaaaaa111111", "bbbbbb222222", "cccccc333333"]):
        backend.set(key, _entry(key, f"[{i}]", created_at=base + i, ttl=3600))
    assert backend.count() == 2
    assert sorted(backend.keys()) == ["bbbbbb222222", "cccccc333333"]


@pytest.mark.parametrize("garbage", ["banana", "0", "-3"])
def test_max_rows_env_garbage_falls_back_to_default(tmp_path, monkeypatch, garbage, caplog) -> None:
    monkeypatch.setenv("FURL_CCR_SQLITE_MAX_ROWS", garbage)
    with caplog.at_level(logging.WARNING, logger=_BACKEND_LOGGER):
        backend = SqliteBackend(db_path=tmp_path / "bad-env.sqlite3")
    assert backend._max_rows == 10_000
    assert any(
        r.levelno == logging.WARNING and "FURL_CCR_SQLITE_MAX_ROWS" in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------------------------------- #
# Security — file permissions.
# --------------------------------------------------------------------------- #


def test_db_file_created_0600(tmp_path) -> None:
    db = tmp_path / "perm.sqlite3"
    SqliteBackend(db_path=db)
    assert db.is_file()
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_db_create_uses_o_nofollow(tmp_path, monkeypatch) -> None:
    # SEC-3 (defense-in-depth): the create-path open must carry O_NOFOLLOW so a
    # pre-planted symlink at the db path cannot redirect the open. Capture the
    # flags os.open is called with on the create branch.
    captured: dict[str, int] = {}
    real_open = os.open

    def _spy_open(path, flags, *args, **kwargs):
        captured["flags"] = flags
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _spy_open)
    SqliteBackend(db_path=tmp_path / "nofollow.sqlite3")
    assert "flags" in captured, "create path did not call os.open"
    assert captured["flags"] & os.O_NOFOLLOW, "O_NOFOLLOW not set on db create"


@pytest.mark.skipif(getattr(os, "O_NOFOLLOW", 0) == 0, reason="platform lacks O_NOFOLLOW")
def test_db_create_refuses_to_follow_planted_symlink(tmp_path) -> None:
    # A dangling symlink at the db path (so the create branch runs) must NOT be
    # followed: O_NOFOLLOW makes os.open raise, the backend fails open to the
    # in-memory fallback, and the symlink's target is never created.
    target = tmp_path / "attacker_target.sqlite3"  # dangling — does not exist yet
    db_link = tmp_path / "db.sqlite3"
    os.symlink(target, db_link)

    backend = SqliteBackend(db_path=db_link)

    assert backend._degraded, "backend should degrade rather than follow the symlink"
    assert not target.exists(), "O_NOFOLLOW must prevent creating the symlink target"


def test_existing_db_file_tightened_to_0600(tmp_path) -> None:
    db = tmp_path / "loose.sqlite3"
    db.touch(mode=0o644)
    os.chmod(db, 0o644)  # touch() is umask-masked; force the loose mode
    SqliteBackend(db_path=db)
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_created_parent_dir_is_0700(tmp_path) -> None:
    db = tmp_path / "deep" / "nested" / "perm.sqlite3"
    SqliteBackend(db_path=db)
    assert stat.S_IMODE(os.stat(db.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(db.parent.parent).st_mode) == 0o700


# --------------------------------------------------------------------------- #
# Failure containment — corruption vs lock contention.
# --------------------------------------------------------------------------- #


def test_corrupt_db_fails_open_with_one_loud_error(tmp_path, caplog) -> None:
    """A corrupt database file must NEVER crash the host: the backend degrades
    to the in-memory fallback, logs exactly ONE ERROR, and keeps serving."""
    db = tmp_path / "corrupt.sqlite3"
    db.write_bytes(b"this is definitely not a sqlite database " * 64)

    with caplog.at_level(logging.DEBUG, logger=_BACKEND_LOGGER):
        backend = SqliteBackend(db_path=db)
        # Ops keep working through the fallback — and never raise.
        entry = _entry("abcdef123456", '{"post-corruption": true}')
        backend.set("abcdef123456", entry)
        assert backend.get("abcdef123456") == entry
        assert backend.exists("abcdef123456") is True
        assert backend.count() == 1
        assert backend.keys() == ["abcdef123456"]
        assert backend.delete("abcdef123456") is True
        backend.clear()
        stats = backend.get_stats()

    assert stats["degraded"] is True
    errors = [r for r in caplog.records if r.name == _BACKEND_LOGGER and r.levelno == logging.ERROR]
    assert len(errors) == 1, f"expected exactly ONE loud ERROR, got {len(errors)}"


def test_locked_db_bounded_retry_then_fails_open_for_that_op(tmp_path, caplog) -> None:
    """Lock contention is NOT corruption: a locked write retries (bounded),
    then fails open for that operation only — the entry stays retrievable
    in-process, the backend does NOT degrade, and later writes land in SQLite."""
    db = tmp_path / "locked.sqlite3"
    backend = SqliteBackend(db_path=db, busy_timeout_seconds=0.05, lock_retries=2)

    blocker = sqlite3.connect(db)
    try:
        blocker.execute("PRAGMA busy_timeout=0")
        blocker.execute("BEGIN EXCLUSIVE")
        held_entry = _entry("abcdef123456", '{"written": "under contention"}')
        with caplog.at_level(logging.DEBUG, logger=_BACKEND_LOGGER):
            backend.set("abcdef123456", held_entry)  # must neither raise nor hang
    finally:
        blocker.rollback()
        blocker.close()

    # Fail-open for THAT op: served from the in-process fallback, no degradation.
    assert backend.get("abcdef123456") == held_entry
    assert backend.get_stats()["degraded"] is False
    backend_records = [r for r in caplog.records if r.name == _BACKEND_LOGGER]
    assert any(r.levelno == logging.WARNING for r in backend_records), "lock fail-open is loud"
    assert not any(r.levelno == logging.ERROR for r in backend_records), "contention != corruption"

    # After the lock clears, the SAME backend writes durably again.
    late_entry = _entry("123456abcdef", '{"written": "after release"}')
    backend.set("123456abcdef", late_entry)
    fresh = SqliteBackend(db_path=db)
    assert fresh.get("123456abcdef") == late_entry
    # The contended write never reached the file (documented divergence: it
    # was retained for this process only).
    assert fresh.get("abcdef123456") is None


# --------------------------------------------------------------------------- #
# Path contract — workspace dir, env override, no cwd fallback.
# --------------------------------------------------------------------------- #


def test_default_path_follows_workspace_dir_contract(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(workspace))
    assert paths.ccr_sqlite_path() == workspace / "ccr.sqlite3"
    SqliteBackend()
    assert (workspace / "ccr.sqlite3").is_file()


def test_env_path_override_wins(tmp_path, monkeypatch) -> None:
    override = tmp_path / "elsewhere" / "custom.sqlite3"
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(override))
    assert paths.ccr_sqlite_path() == override
    SqliteBackend()
    assert override.is_file()


def test_no_cwd_fallback(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    elsewhere = tmp_path / "some-cwd"
    elsewhere.mkdir()
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(workspace))
    monkeypatch.chdir(elsewhere)
    SqliteBackend()
    assert (workspace / "ccr.sqlite3").is_file()
    assert list(elsewhere.iterdir()) == [], "the backend must never write to the cwd"


# --------------------------------------------------------------------------- #
# CompressionStore integration — TTL/eviction semantics ride on the backend.
# --------------------------------------------------------------------------- #


def test_compression_store_round_trip_via_sqlite_backend(tmp_path) -> None:
    store = CompressionStore(max_entries=10, backend=SqliteBackend(db_path=tmp_path / "s.sqlite3"))
    original = '[{"id": 0, "v": "needle", "weird": "\\ud800"}]'
    key = store.store(original, "<<ccr:abcdef123456>>", explicit_hash="abcdef123456")
    entry = store.retrieve(key)
    assert entry is not None
    assert entry.original_content == original


def test_compression_store_eviction_works_on_sqlite_backend(tmp_path) -> None:
    """The store's oldest-first eviction loop (heap + progress rebuild) must
    hold ``count <= max_entries`` on the SQLite backend exactly as it does on
    memory — including ``created_at`` float equality across the REAL column."""
    store = CompressionStore(max_entries=3, backend=SqliteBackend(db_path=tmp_path / "e.sqlite3"))
    keys = [
        store.store(f'[{{"id": {i}}}]', f"<<ccr:{i:012x}>>", explicit_hash=f"{i:012x}")
        for i in range(6)
    ]
    assert store._backend.count() <= 3
    assert store.retrieve(keys[-1]) is not None
    assert store.retrieve(keys[0]) is None


def test_compression_store_ttl_expiry_on_sqlite_backend(tmp_path) -> None:
    store = CompressionStore(max_entries=10, backend=SqliteBackend(db_path=tmp_path / "t.sqlite3"))
    key = store.store("[1]", "<<ccr:abcdef123456>>", explicit_hash="abcdef123456", ttl=1)
    assert store.retrieve(key) is not None
    time.sleep(1.1)
    assert store.retrieve(key) is None, "expired entries must miss through the store"


def test_cross_process_retrieve_reads_hash_the_parent_wrote(tmp_path) -> None:
    """The sub-agent case: a SEPARATE PROCESS resolves a hash this process
    stored, byte-exact — through the production ``CompressionStore.retrieve``
    call, not a raw file peek."""
    db = tmp_path / "shared.sqlite3"
    original = '{"rows": [1, 2], "weird": "\ud800\x00\ttab", "text": "héllo 😀"}'
    parent_store = CompressionStore(max_entries=10, backend=SqliteBackend(db_path=db))
    # explicit_hash mirrors production's weird-content shape: the store's
    # DEFAULT hash path (sha256 over original.encode()) predates this backend
    # and rejects lone surrogates — producers of non-UTF8-safe content always
    # supply the marker hash themselves.
    hash_key = parent_store.store(original, "<<ccr:abcdef123456>>", explicit_hash="abcdef123456")

    reader = tmp_path / "reader.py"
    reader.write_text(
        textwrap.dedent(
            """\
            import base64
            import sys

            from furl_ctx.cache.backends.sqlite import SqliteBackend
            from furl_ctx.cache.compression_store import CompressionStore

            db_path, hash_key = sys.argv[1], sys.argv[2]
            store = CompressionStore(max_entries=10, backend=SqliteBackend(db_path=db_path))
            entry = store.retrieve(hash_key)
            if entry is None:
                sys.exit(3)
            payload = entry.original_content.encode("utf-8", "surrogatepass")
            sys.stdout.buffer.write(base64.b64encode(payload))
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(reader), str(db), hash_key],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"subprocess retrieve failed: {proc.stderr.decode()!r}"
    recovered = base64.b64decode(proc.stdout).decode("utf-8", "surrogatepass")
    assert recovered == original


# --------------------------------------------------------------------------- #
# Env-selected backend loader — 'sqlite' registered; library default unchanged.
# --------------------------------------------------------------------------- #


@pytest.fixture
def _reset_singleton():
    reset_compression_store()
    yield
    reset_compression_store()


def test_env_backend_sqlite_selects_sqlite_for_global_store(
    tmp_path, monkeypatch, _reset_singleton
) -> None:
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SQLITE_PATH", str(tmp_path / "global.sqlite3"))
    store = get_compression_store()
    assert isinstance(store._backend, SqliteBackend)


def test_library_default_backend_stays_memory(_reset_singleton) -> None:
    """Durability is an MCP-deployment property: plain library compress()
    keeps the in-memory backend unless FURL_CCR_BACKEND opts in."""
    store = get_compression_store()
    assert isinstance(store._backend, InMemoryBackend)


def test_ccr_sqlite_path_helper_never_uses_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FURL_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_SQLITE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = paths.ccr_sqlite_path()
    assert resolved == Path.home() / ".furl" / "ccr.sqlite3"
    assert not str(resolved).startswith(str(tmp_path))
