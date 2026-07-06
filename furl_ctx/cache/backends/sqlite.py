"""Durable SQLite storage backend for CompressionStore (Engine P1-7).

Restores the durability the upstream engine ships by default: with the
in-memory backend, an MCP server restart destroys every retrievable original,
and sub-agent processes (separate MCP server instances) can never resolve a
hash the main-agent process stored. This backend keeps entries in a SQLite
file under the workspace dir so both survive.

Durability / operational properties (matching the upstream store's armor):

* WAL journal mode — cross-process readers never block the single writer.
* Database file created ``0600`` (created parent dirs ``0700``): originals may
  contain secrets, so the file is owner-only from birth. SQLite creates its
  ``-wal``/``-shm`` sidecars with the database file's permissions.
* Startup purge of expired rows, plus an opportunistic purge every N puts.
* A max-rows cap (default 10 000, ``FURL_CCR_SQLITE_MAX_ROWS``) with
  oldest-``created_at``-first eviction — the same generation-FIFO ordering as
  the in-memory plane.
* Corruption is distinguished from lock contention: any ``sqlite3.Error``
  that is NOT lock contention permanently fails open to an in-memory fallback
  with exactly ONE loud ERROR log (the host process never crashes); a
  ``database is locked`` ``OperationalError`` gets a bounded retry and then
  fails open for that operation only (the write is retained in the in-process
  fallback so no marker dangles, and the backend stays on SQLite).

Deliberate divergences from the in-memory reference semantics:

* The row cap is enforced INSIDE the backend after each insert (evicting down
  to the cap), whereas the memory plane's cap lives in ``CompressionStore``
  and evicts before insert. Both are oldest-first; the backend cap is a
  file-level bound shared across processes, and ``CompressionStore`` still
  applies its own ``max_entries`` policy on top.
* A write that fails open under unrelieved lock contention lands in the
  in-process fallback only: same-process retrieval still round-trips, but
  other processes miss that entry loudly (exactly the pre-durability
  behavior for all entries).

Thread-safety: one connection per thread via ``threading.local``. The MCP
server calls the store from ``run_in_executor`` worker threads; per-thread
connections avoid serializing WAL reads behind a shared-connection lock and
sidestep cross-thread connection-sharing rules entirely. (``CompressionStore``
additionally serializes its own calls behind its lock — per-thread connections
keep the backend independently safe for direct use, as the Protocol requires.)

Byte-exactness: content and metadata strings are stored as BLOBs encoded with
UTF-8 ``surrogatepass``, so any Python ``str`` — including non-UTF8-safe JSON
carrying lone surrogates, control chars, and NULs — round-trips identically.
Hash keys are stored the same way and compared as raw bytes: opaque, exact,
no case folding. This module never logs stored content, only hashes/counts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from ... import paths as _paths
from .memory import InMemoryBackend

if TYPE_CHECKING:
    from ..compression_store import CompressionEntry

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

DEFAULT_MAX_ROWS = 10_000
MAX_ROWS_ENV = "FURL_CCR_SQLITE_MAX_ROWS"

_DEFAULT_PURGE_EVERY_N_PUTS = 64
_DEFAULT_BUSY_TIMEOUT_SECONDS = 0.25
_DEFAULT_LOCK_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 0.05

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ccr_entries (
    hash_key BLOB PRIMARY KEY,
    entry_hash BLOB NOT NULL,
    original_content BLOB NOT NULL,
    compressed_content BLOB NOT NULL,
    original_tokens INTEGER NOT NULL,
    compressed_tokens INTEGER NOT NULL,
    original_item_count INTEGER NOT NULL,
    compressed_item_count INTEGER NOT NULL,
    tool_name BLOB,
    tool_call_id BLOB,
    query_context BLOB,
    created_at REAL NOT NULL,
    ttl INTEGER NOT NULL,
    compression_strategy BLOB,
    retrieval_count INTEGER NOT NULL,
    search_queries TEXT NOT NULL,
    last_accessed REAL
)
"""

_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ccr_entries_created_at_idx ON ccr_entries (created_at)"
)

_COLUMNS = (
    "hash_key, entry_hash, original_content, compressed_content, original_tokens, "
    "compressed_tokens, original_item_count, compressed_item_count, tool_name, "
    "tool_call_id, query_context, created_at, ttl, compression_strategy, "
    "retrieval_count, search_queries, last_accessed"
)

_UPSERT_SQL = (
    f"INSERT OR REPLACE INTO ccr_entries ({_COLUMNS}) "
    f"VALUES ({', '.join('?' * len(_COLUMNS.split(', ')))})"
)

_SELECT_SQL = f"SELECT {_COLUMNS} FROM ccr_entries WHERE hash_key = ?"

_PURGE_EXPIRED_SQL = "DELETE FROM ccr_entries WHERE created_at + ttl < ?"

# Subquery form: DELETE ... ORDER BY ... LIMIT needs a non-default SQLite
# compile flag, the IN-subquery works on every build. Tie-broken on hash_key
# for determinism when two rows share a created_at.
_EVICT_OLDEST_SQL = (
    "DELETE FROM ccr_entries WHERE hash_key IN ("
    "SELECT hash_key FROM ccr_entries ORDER BY created_at ASC, hash_key ASC LIMIT ?)"
)


def _encode_text(value: str) -> bytes:
    """Encode any Python str (incl. lone surrogates) to exact-round-trip bytes."""
    return value.encode("utf-8", "surrogatepass")


def _encode_optional_text(value: str | None) -> bytes | None:
    return None if value is None else _encode_text(value)


def _decode_text(value: bytes) -> str:
    return bytes(value).decode("utf-8", "surrogatepass")


def _decode_optional_text(value: bytes | None) -> str | None:
    return None if value is None else _decode_text(value)


def _is_lock_contention(exc: sqlite3.Error) -> bool:
    """True for transient writer contention; everything else in the sqlite
    error family is treated as the corruption/unusable-file class."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _max_rows_from_env() -> int:
    raw_value = os.environ.get(MAX_ROWS_ENV)
    if raw_value is None or not raw_value.strip():
        return DEFAULT_MAX_ROWS
    try:
        max_rows = int(raw_value)
    except ValueError:
        logger.warning(
            "%s must be a positive integer number of rows, got %r; using %s",
            MAX_ROWS_ENV,
            raw_value,
            DEFAULT_MAX_ROWS,
        )
        return DEFAULT_MAX_ROWS
    if max_rows <= 0:
        logger.warning(
            "%s must be greater than 0, got %s; using %s",
            MAX_ROWS_ENV,
            max_rows,
            DEFAULT_MAX_ROWS,
        )
        return DEFAULT_MAX_ROWS
    return max_rows


class _SqliteOpFailed(Exception):
    """Internal control-flow marker: the SQLite side of one operation failed
    open (after retry/degradation handling); the caller takes its fallback."""


class SqliteBackend:
    """Durable, thread-safe SQLite backend for ``CompressionStore``.

    Implements the ``CompressionStoreBackend`` Protocol. Like the in-memory
    reference it is dumb CRUD: TTL checks on read stay in ``CompressionStore``
    (the Protocol contract); the backend's own purge/cap machinery is a
    retention garbage collector for the shared file, not a read gate.

    Never raises out of Protocol methods: SQLite failures degrade to an
    in-process ``InMemoryBackend`` fallback (see module docstring for the
    corruption-vs-lock discrimination).
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        max_rows: int | None = None,
        purge_every_n_puts: int = _DEFAULT_PURGE_EVERY_N_PUTS,
        busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS,
        lock_retries: int = _DEFAULT_LOCK_RETRIES,
    ) -> None:
        """Open (or create) the durable store.

        Args:
            db_path: Database file location. Defaults to
                ``$FURL_CCR_SQLITE_PATH`` or ``<workspace>/ccr.sqlite3``
                (never the cwd — see ``furl_ctx.paths.ccr_sqlite_path``).
            max_rows: File-level row cap (oldest-first eviction). Defaults to
                ``$FURL_CCR_SQLITE_MAX_ROWS`` or 10 000.
            purge_every_n_puts: Opportunistic expired-row purge cadence.
            busy_timeout_seconds: Per-attempt SQLite busy wait.
            lock_retries: Extra attempts after the first on lock contention.
        """
        if max_rows is not None and max_rows <= 0:
            raise ValueError(f"max_rows must be positive, got {max_rows!r}")
        if purge_every_n_puts <= 0:
            raise ValueError(f"purge_every_n_puts must be positive, got {purge_every_n_puts!r}")

        self._db_path = Path(db_path) if db_path is not None else _paths.ccr_sqlite_path()
        self._max_rows = max_rows if max_rows is not None else _max_rows_from_env()
        self._purge_every_n_puts = purge_every_n_puts
        self._busy_timeout_seconds = busy_timeout_seconds
        self._lock_retries = lock_retries

        self._thread_local = threading.local()
        self._state_lock = threading.Lock()
        self._all_connections: list[sqlite3.Connection] = []
        self._put_count = 0
        self._degraded = False
        self._degradation_logged = False
        # Fallback tier: sole storage once degraded; per-op refuge for writes
        # that lose the bounded lock-contention retry.
        self._memory = InMemoryBackend()

        try:
            self._prepare_filesystem()
        except OSError as exc:
            self._degrade(exc)
            return
        try:
            self._run("init", self._initialize)
        except _SqliteOpFailed as exc:
            # Corruption already degraded inside _run; init-time lock
            # exhaustion also degrades — without the schema no later
            # operation can succeed, so limping on SQLite would only turn
            # every op into a fresh failure.
            if not self._degraded:
                self._degrade(exc.__cause__ or exc)

    # ------------------------------------------------------------------ #
    # Protocol methods
    # ------------------------------------------------------------------ #

    def get(self, hash_key: str) -> CompressionEntry | None:
        """Retrieve an entry by hash key (no TTL check — store's job)."""
        if not self._degraded:
            try:
                row = self._run(
                    "get",
                    lambda conn: conn.execute(_SELECT_SQL, (_encode_text(hash_key),)).fetchone(),
                )
            except _SqliteOpFailed:
                pass
            else:
                if row is not None:
                    return _row_to_entry(row)
        return self._memory.get(hash_key)

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        """Store an entry, overwriting any existing entry with the same key."""
        if not self._degraded:
            try:
                self._run("set", lambda conn: self._sqlite_set(conn, hash_key, entry))
            except _SqliteOpFailed:
                # Per-op fail-open: retain the entry in-process so the marker
                # that references it never dangles for THIS process. Other
                # processes miss it loudly — the pre-durability status quo.
                self._memory.set(hash_key, entry)
            else:
                # Drop any stale fallback shadow from an earlier contended
                # write of the same key: SQLite now holds the newest entry.
                self._memory.delete(hash_key)
            return
        self._memory.set(hash_key, entry)

    def delete(self, hash_key: str) -> bool:
        """Delete an entry by hash key from both tiers."""
        sqlite_deleted = False
        if not self._degraded:
            try:
                sqlite_deleted = self._run(
                    "delete",
                    lambda conn: self._sqlite_delete(conn, hash_key),
                )
            except _SqliteOpFailed:
                sqlite_deleted = False
        memory_deleted = self._memory.delete(hash_key)
        return sqlite_deleted or memory_deleted

    def exists(self, hash_key: str) -> bool:
        """Check presence (no TTL check — store's job)."""
        if not self._degraded:
            try:
                row = self._run(
                    "exists",
                    lambda conn: conn.execute(
                        "SELECT 1 FROM ccr_entries WHERE hash_key = ?",
                        (_encode_text(hash_key),),
                    ).fetchone(),
                )
            except _SqliteOpFailed:
                pass
            else:
                if row is not None:
                    return True
        return self._memory.exists(hash_key)

    def clear(self) -> None:
        """Remove all entries from both tiers."""
        if not self._degraded:
            try:
                self._run("clear", self._sqlite_clear)
            except _SqliteOpFailed:
                pass
        self._memory.clear()

    def count(self) -> int:
        """Number of distinct entries across both tiers."""
        if self._degraded:
            return self._memory.count()
        try:
            sqlite_count = self._run(
                "count",
                lambda conn: int(conn.execute("SELECT COUNT(*) FROM ccr_entries").fetchone()[0]),
            )
        except _SqliteOpFailed:
            return self._memory.count()
        overlay_keys = self._memory.keys()
        if not overlay_keys:
            return sqlite_count
        return sqlite_count + sum(1 for key in overlay_keys if not self._sqlite_has(key))

    def keys(self) -> list[str]:
        """All hash keys across both tiers."""
        if self._degraded:
            return self._memory.keys()
        try:
            sqlite_keys = self._run(
                "keys",
                lambda conn: [
                    _decode_text(row[0])
                    for row in conn.execute("SELECT hash_key FROM ccr_entries").fetchall()
                ],
            )
        except _SqliteOpFailed:
            return self._memory.keys()
        seen = set(sqlite_keys)
        return sqlite_keys + [key for key in self._memory.keys() if key not in seen]

    def items(self) -> list[tuple[str, CompressionEntry]]:
        """All (hash_key, entry) pairs across both tiers."""
        if self._degraded:
            return self._memory.items()
        try:
            rows = self._run(
                "items",
                lambda conn: conn.execute(f"SELECT {_COLUMNS} FROM ccr_entries").fetchall(),
            )
        except _SqliteOpFailed:
            return self._memory.items()
        result = [(_decode_text(row[0]), _row_to_entry(row)) for row in rows]
        seen = {key for key, _entry in result}
        result.extend((key, entry) for key, entry in self._memory.items() if key not in seen)
        return result

    def get_stats(self) -> dict[str, Any]:
        """Backend statistics — counts and sizes only, never content."""
        bytes_used = 0
        if not self._degraded:
            try:
                bytes_used = self._run(
                    "stats",
                    lambda conn: int(
                        conn.execute(
                            "SELECT COALESCE(SUM("
                            "LENGTH(original_content) + LENGTH(compressed_content)), 0) "
                            "FROM ccr_entries"
                        ).fetchone()[0]
                    ),
                )
            except _SqliteOpFailed:
                bytes_used = 0
        return {
            "backend_type": "sqlite",
            "entry_count": self.count(),
            "bytes_used": bytes_used,
            "db_path": str(self._db_path),
            "max_rows": self._max_rows,
            "degraded": self._degraded,
            "fallback_entry_count": self._memory.count(),
        }

    def close(self) -> None:
        """Close every connection this backend opened (all threads)."""
        with self._state_lock:
            connections, self._all_connections = self._all_connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - close is best-effort
                pass

    # ------------------------------------------------------------------ #
    # SQLite plumbing
    # ------------------------------------------------------------------ #

    def _run(self, op_name: str, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        """Execute ``fn`` against this thread's connection with the
        corruption-vs-lock discrimination applied.

        Lock contention: bounded retry, then WARNING + ``_SqliteOpFailed``
        (per-op fail-open; the backend stays on SQLite). Any other
        ``sqlite3.Error``: permanent degradation to the in-memory fallback
        with one loud ERROR, then ``_SqliteOpFailed``.
        """
        attempts = self._lock_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return fn(self._connection())
            except sqlite3.Error as exc:
                if not _is_lock_contention(exc):
                    self._degrade(exc)
                    raise _SqliteOpFailed() from exc
                if attempt < attempts:
                    time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                    continue
                logger.warning(
                    "event=ccr_sqlite_lock_fail_open op=%s attempts=%d db_path=%s — "
                    "lock contention persisted; failing open to the in-process "
                    "fallback for this operation only",
                    op_name,
                    attempts,
                    self._db_path,
                )
                raise _SqliteOpFailed() from exc
        raise AssertionError("unreachable: retry loop returns or raises")  # pragma: no cover

    def _connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._thread_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=self._busy_timeout_seconds)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except BaseException:
                conn.close()  # do not leak a half-initialized connection
                raise
            self._thread_local.conn = conn
            with self._state_lock:
                self._all_connections.append(conn)
        return conn

    def _initialize(self, conn: sqlite3.Connection) -> None:
        """Create the schema and purge rows that expired while we were down."""
        with conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            purged = conn.execute(_PURGE_EXPIRED_SQL, (time.time(),)).rowcount
        if purged > 0:
            logger.info("event=ccr_sqlite_startup_purge purged_rows=%d", purged)

    def _sqlite_set(self, conn: sqlite3.Connection, hash_key: str, entry: CompressionEntry) -> None:
        with conn:
            conn.execute(_UPSERT_SQL, _entry_to_row(hash_key, entry))
            # Count the put only once the write is in flight inside the
            # transaction, so lock-contention retries of the same logical put
            # do not inflate the purge cadence.
            with self._state_lock:
                self._put_count += 1
                purge_now = self._put_count % self._purge_every_n_puts == 0
            if purge_now:
                conn.execute(_PURGE_EXPIRED_SQL, (time.time(),))
            row_count = int(conn.execute("SELECT COUNT(*) FROM ccr_entries").fetchone()[0])
            if row_count > self._max_rows:
                conn.execute(_EVICT_OLDEST_SQL, (row_count - self._max_rows,))

    def _sqlite_delete(self, conn: sqlite3.Connection, hash_key: str) -> bool:
        with conn:
            cursor = conn.execute(
                "DELETE FROM ccr_entries WHERE hash_key = ?", (_encode_text(hash_key),)
            )
        return cursor.rowcount > 0

    def _sqlite_clear(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute("DELETE FROM ccr_entries")

    def _sqlite_has(self, hash_key: str) -> bool:
        try:
            row = self._run(
                "exists",
                lambda conn: conn.execute(
                    "SELECT 1 FROM ccr_entries WHERE hash_key = ?",
                    (_encode_text(hash_key),),
                ).fetchone(),
            )
        except _SqliteOpFailed:
            return False
        return row is not None

    # ------------------------------------------------------------------ #
    # Filesystem preparation + degradation
    # ------------------------------------------------------------------ #

    def _prepare_filesystem(self) -> None:
        """Create the parent dir (0700) and the db file (0600) before SQLite
        ever opens it, so the file is never observable with wider perms."""
        self._ensure_dir_0700(self._db_path.parent)
        if self._db_path.exists():
            os.chmod(self._db_path, 0o600)
            return
        # O_NOFOLLOW (SEC-3, defense-in-depth): refuse to create-through a
        # pre-planted symlink at the db path, so a symlink in the workspace can't
        # redirect the open to an attacker-chosen target. getattr keeps this
        # portable to platforms lacking the flag (it degrades to 0 = no-op).
        create_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._db_path, create_flags, 0o600)
        os.close(fd)
        # os.open's mode is umask-masked; normalize to exactly 0600.
        os.chmod(self._db_path, 0o600)

    @staticmethod
    def _ensure_dir_0700(directory: Path) -> None:
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
        for ancestor in reversed(missing):
            try:
                ancestor.mkdir(mode=0o700)
            except FileExistsError:  # pragma: no cover - concurrent creator won
                continue
            os.chmod(ancestor, 0o700)  # mkdir's mode is umask-masked

    def _degrade(self, exc: BaseException) -> None:
        """Permanently fail open to the in-memory fallback — exactly one loud
        ERROR (never an exception to the host)."""
        with self._state_lock:
            self._degraded = True
            if self._degradation_logged:
                return
            self._degradation_logged = True
        logger.error(
            "event=ccr_sqlite_backend_degraded db_path=%s error=%s: %s — failing open "
            "to the in-memory fallback; durable CCR persistence is DISABLED for this "
            "process (entries will not survive restart and other processes cannot "
            "retrieve them)",
            self._db_path,
            type(exc).__name__,
            exc,
        )


# --------------------------------------------------------------------------- #
# Row <-> entry mapping
# --------------------------------------------------------------------------- #


def _entry_to_row(hash_key: str, entry: CompressionEntry) -> tuple[Any, ...]:
    return (
        _encode_text(hash_key),
        _encode_text(entry.hash),
        _encode_text(entry.original_content),
        _encode_text(entry.compressed_content),
        entry.original_tokens,
        entry.compressed_tokens,
        entry.original_item_count,
        entry.compressed_item_count,
        _encode_optional_text(entry.tool_name),
        _encode_optional_text(entry.tool_call_id),
        _encode_optional_text(entry.query_context),
        entry.created_at,
        entry.ttl,
        _encode_optional_text(entry.compression_strategy),
        entry.retrieval_count,
        json.dumps(entry.search_queries),  # ensure_ascii escapes surrogates
        entry.last_accessed,
    )


def _row_to_entry(row: tuple[Any, ...]) -> CompressionEntry:
    # Deferred import: compression_store lazily imports this package, and the
    # backends must stay importable without pulling the store at module load
    # (mirrors the InMemoryBackend TYPE_CHECKING pattern).
    from ..compression_store import CompressionEntry

    return CompressionEntry(
        hash=_decode_text(row[1]),
        original_content=_decode_text(row[2]),
        compressed_content=_decode_text(row[3]),
        original_tokens=int(row[4]),
        compressed_tokens=int(row[5]),
        original_item_count=int(row[6]),
        compressed_item_count=int(row[7]),
        tool_name=_decode_optional_text(row[8]),
        tool_call_id=_decode_optional_text(row[9]),
        query_context=_decode_optional_text(row[10]),
        created_at=float(row[11]),
        ttl=int(row[12]),
        compression_strategy=_decode_optional_text(row[13]),
        retrieval_count=int(row[14]),
        search_queries=list(json.loads(row[15])),
        last_accessed=float(row[16]) if row[16] is not None else None,
    )
