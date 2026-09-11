from __future__ import annotations

import re
from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text()


def write(path: str, text: str) -> None:
    Path(path).write_text(text)


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one occurrence, found {count}: {old[:80]!r}")
    write(path, text.replace(old, new, 1))


def replace_method(path: str, name: str, new_source: str) -> None:
    lines = read(path).splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith(f"    def {name}(") or line.startswith(f"    async def {name}(")), None)
    if start is None:
        raise RuntimeError(f"{path}: method {name} not found")
    # Keep decorators belonging to the next method; class-level methods/decorators
    # are indented exactly four spaces. Nested functions are eight or more.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("    def ") or line.startswith("    async def ") or line.startswith("    @"):
            end = i
            break
        if line and not line.startswith((" ", "\t", "\n", "\r")):
            end = i
            break
    replacement = new_source.rstrip() + "\n\n"
    write(path, "".join(lines[:start]) + replacement + "".join(lines[end:]))


def add_decorator(path: str, name: str, decorator: str) -> None:
    old = f"    def {name}("
    new = f"    {decorator}\n    def {name}("
    replace_once(path, old, new)


def insert_before_method(path: str, name: str, source: str) -> None:
    marker = f"    def {name}("
    text = read(path)
    pos = text.find(marker)
    if pos < 0:
        raise RuntimeError(f"{path}: insertion method {name} not found")
    write(path, text[:pos] + source.rstrip() + "\n\n" + text[pos:])


# ---------------------------------------------------------------------------
# In-memory backend: authoritative checked operations + binding provenance.
# ---------------------------------------------------------------------------
MEM = "furl_ctx/cache/backends/memory.py"
replace_once(
    MEM,
    "        self._counters: Counter[str] = Counter()\n",
    "        self._counters: Counter[str] = Counter()\n"
    "        # Explicit-hash provenance. A poisoned key stays unsafe until a verified\n"
    "        # purge/clear releases it; this mirrors the durable SQLite authority.\n"
    "        self._bindings: dict[str, tuple[str, bool]] = {}\n",
)
insert_before_method(
    MEM,
    "set",
    '''    def checked_get_all(self, hash_key: str) -> list[CompressionEntry]:
        """Authoritative representations for proof-requiring store operations."""
        entry = self._store.get(hash_key)
        return [] if entry is None else [entry]

    def checked_items(self) -> list[tuple[str, CompressionEntry]]:
        return list(self._store.items())

    def checked_created_at_index(self) -> list[tuple[float, str]]:
        return [(entry.created_at, key) for key, entry in self._store.items()]

    def checked_delete(self, hash_key: str) -> bool:
        return self.delete(hash_key)

    def checked_clear(self) -> None:
        self.clear()

    def claim_binding(self, hash_key: str, fingerprint: str) -> str:
        """Atomically claim an explicit key inside this process.

        Returns ``claimed`` for a new key, ``same`` for the same content, and
        ``conflict`` for a different claimant. A conflict poisons the key so a
        later same-fingerprint write cannot silently resurrect one side.
        """
        current = self._bindings.get(hash_key)
        if current is None:
            self._bindings[hash_key] = (fingerprint, False)
            return "claimed"
        existing, conflicted = current
        if conflicted or existing != fingerprint:
            self._bindings[hash_key] = (existing, True)
            return "conflict"
        return "same"

    def get_binding(self, hash_key: str) -> tuple[str, bool] | None:
        return self._bindings.get(hash_key)

    def release_binding(self, hash_key: str) -> None:
        self._bindings.pop(hash_key, None)
''',
)
replace_once(
    MEM,
    "        self._counters.clear()\n",
    "        self._counters.clear()\n        self._bindings.clear()\n",
)


# ---------------------------------------------------------------------------
# SQLite backend: checked channel that never turns durable I/O failure into a
# volatile absence, plus a persistent atomic binding table.
# ---------------------------------------------------------------------------
SQL = "furl_ctx/cache/backends/sqlite.py"
replace_once(
    SQL,
    "from .memory import InMemoryBackend\n",
    "from .memory import InMemoryBackend\nfrom ..storage_safety import StorageUnavailableError\n",
)
replace_once(
    SQL,
    "_CREATE_COUNTERS_TABLE_SQL = (\n    \"CREATE TABLE IF NOT EXISTS ccr_counters \"\n    \"(name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)\"\n)\n",
    "_CREATE_COUNTERS_TABLE_SQL = (\n    \"CREATE TABLE IF NOT EXISTS ccr_counters \"\n    \"(name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)\"\n)\n\n"
    "_CREATE_BINDINGS_TABLE_SQL = (\n"
    "    \"CREATE TABLE IF NOT EXISTS ccr_bindings (\"\n"
    "    \"hash_key BLOB PRIMARY KEY, content_fingerprint TEXT NOT NULL, \"\n"
    "    \"conflicted INTEGER NOT NULL DEFAULT 0)\"\n"
    ")\n",
)
# Ensure new schema is created on old databases too.
replace_once(
    SQL,
    "            conn.execute(_CREATE_COUNTERS_TABLE_SQL)\n",
    "            conn.execute(_CREATE_COUNTERS_TABLE_SQL)\n            conn.execute(_CREATE_BINDINGS_TABLE_SQL)\n",
)
# Full wipe also resets identity claims.
replace_once(
    SQL,
    "            conn.execute(\"DELETE FROM ccr_counters\")\n",
    "            conn.execute(\"DELETE FROM ccr_counters\")\n            conn.execute(\"DELETE FROM ccr_bindings\")\n",
)
# Coordination identity sits before the first public property.
text = read(SQL)
prop_marker = "    @property\n    def max_rows(self) -> int:\n"
if prop_marker not in text:
    raise RuntimeError("sqlite max_rows property marker not found")
text = text.replace(
    prop_marker,
    '''    @property
    def coordination_identity(self) -> str:
        """Cross-process mutation identity for stores sharing this database."""
        return f"file:{self._db_path.resolve()}"

''' + prop_marker,
    1,
)
write(SQL, text)
insert_before_method(
    SQL,
    "close",
    '''    def _checked_run(self, op_name: str, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run a proof-requiring SQLite operation without fail-open fallback."""
        if self._degraded:
            raise StorageUnavailableError(
                f"SQLite storage is degraded; cannot verify {op_name} against {self._db_path}"
            )
        try:
            return self._run(op_name, fn)
        except _SqliteOpFailed as exc:
            raise StorageUnavailableError(
                f"SQLite storage could not verify {op_name} against {self._db_path}"
            ) from exc

    def checked_get_all(self, hash_key: str) -> list[CompressionEntry]:
        row = self._checked_run(
            "checked_get",
            lambda conn: conn.execute(_SELECT_SQL, (_encode_text(hash_key),)).fetchone(),
        )
        result: list[CompressionEntry] = []
        if row is not None:
            result.append(_row_to_entry(row))
        volatile = self._memory.get(hash_key)
        if volatile is not None:
            result.append(volatile)
        return result

    def checked_items(self) -> list[tuple[str, CompressionEntry]]:
        rows = self._checked_run(
            "checked_items", lambda conn: conn.execute(f"SELECT {_COLUMNS} FROM ccr_entries").fetchall()
        )
        durable = [(_decode_text(row[0]), _row_to_entry(row)) for row in rows]
        # Do not deduplicate duplicate-key durable/volatile representations: a
        # divergent overlay is exactly what a safety check needs to see.
        return durable + self._memory.items()

    def checked_created_at_index(self) -> list[tuple[float, str]]:
        rows = self._checked_run(
            "checked_created_at_index",
            lambda conn: conn.execute("SELECT created_at, hash_key FROM ccr_entries").fetchall(),
        )
        return [(float(created_at), _decode_text(hash_key)) for created_at, hash_key in rows] + self._memory.created_at_index()

    def checked_delete(self, hash_key: str) -> bool:
        deleted = bool(self._checked_run("checked_delete", lambda conn: self._sqlite_delete(conn, hash_key)))
        return self._memory.delete(hash_key) or deleted

    def checked_clear(self) -> None:
        self._checked_run("checked_clear", self._sqlite_clear)
        self._memory.clear()

    def claim_binding(self, hash_key: str, fingerprint: str) -> str:
        return self._checked_run(
            "claim_binding",
            lambda conn: self._sqlite_claim_binding(conn, hash_key, fingerprint),
        )

    def get_binding(self, hash_key: str) -> tuple[str, bool] | None:
        return self._checked_run(
            "get_binding",
            lambda conn: self._sqlite_get_binding(conn, hash_key),
        )

    def release_binding(self, hash_key: str) -> None:
        self._checked_run(
            "release_binding",
            lambda conn: self._sqlite_release_binding(conn, hash_key),
        )
''',
)
insert_before_method(
    SQL,
    "_sqlite_increment_counter",
    '''    def _sqlite_claim_binding(
        self, conn: sqlite3.Connection, hash_key: str, fingerprint: str
    ) -> str:
        encoded = _encode_text(hash_key)
        with conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO ccr_bindings "
                "(hash_key, content_fingerprint, conflicted) VALUES (?, ?, 0)",
                (encoded, fingerprint),
            )
            row = conn.execute(
                "SELECT content_fingerprint, conflicted FROM ccr_bindings WHERE hash_key = ?",
                (encoded,),
            ).fetchone()
            assert row is not None
            existing, conflicted = str(row[0]), bool(row[1])
            if conflicted or existing != fingerprint:
                conn.execute(
                    "UPDATE ccr_bindings SET conflicted = 1 WHERE hash_key = ?", (encoded,)
                )
                return "conflict"
            return "claimed" if cursor.rowcount > 0 else "same"

    def _sqlite_get_binding(
        self, conn: sqlite3.Connection, hash_key: str
    ) -> tuple[str, bool] | None:
        row = conn.execute(
            "SELECT content_fingerprint, conflicted FROM ccr_bindings WHERE hash_key = ?",
            (_encode_text(hash_key),),
        ).fetchone()
        if row is None:
            return None
        return (str(row[0]), bool(row[1]))

    def _sqlite_release_binding(self, conn: sqlite3.Connection, hash_key: str) -> None:
        with conn:
            conn.execute(
                "DELETE FROM ccr_bindings WHERE hash_key = ?", (_encode_text(hash_key),)
            )
''',
)


# ---------------------------------------------------------------------------
# CompressionStore: checked proof channel, read-state propagation, persistent
# explicit-hash provenance, and mutation serialization.
# ---------------------------------------------------------------------------
STORE = "furl_ctx/cache/compression_store.py"
replace_once(
    STORE,
    "from typing import TYPE_CHECKING, Any, Final\n",
    "from functools import wraps\nfrom typing import TYPE_CHECKING, Any, Final, TypeVar, cast\n",
)
replace_once(
    STORE,
    "from ..relevance.bm25 import BM25Scorer\n",
    "from ..relevance.bm25 import BM25Scorer\nfrom .storage_safety import MutationGuard, StorageUnavailableError\n",
)
# Miss formatting must distinguish storage uncertainty from true absence.
replace_once(
    STORE,
    "    if status.get(\"status\") == \"available\":\n",
    '''    if status.get("status") == "unavailable":
        return (
            "CCR storage is temporarily unavailable; this read could not prove the hash "
            "absent or retrieve its content. Retry the retrieval rather than recomputing "
            "from a false missing result."
        )

    if status.get("status") == "unsafe":
        return (
            "CCR found data for this hash but could not verify that the hash is bound to "
            "that content. The entry is quarantined rather than serving potentially "
            "foreign bytes; recompute the source content."
        )

    if status.get("status") == "available":
''',
)
# Add detailed-result types + mutation decorator after CrossStoreMatch.
marker = "\n\nclass CompressionStore:\n"
text = read(STORE)
if marker not in text:
    raise RuntimeError("CompressionStore marker not found")
text = text.replace(
    marker,
    '''

@dataclass(frozen=True)
class StoreRead:
    entry: CompressionEntry | None
    status: dict[str, Any]
    source: str | None = None


@dataclass(frozen=True)
class CrossStoreSearchOutcome:
    matches: tuple[CrossStoreMatch, ...]
    complete: bool
    unavailable: tuple[str, ...] = ()


_F = TypeVar("_F", bound=Any)


def _serialized_mutation(method: _F) -> _F:
    """Hold one logical mutation guard across a public store mutation."""

    @wraps(method)
    def wrapped(self: "CompressionStore", *args: Any, **kwargs: Any) -> Any:
        with self._mutation_guard.hold():
            return method(self, *args, **kwargs)

    return cast(_F, wrapped)
''' + marker,
    1,
)
write(STORE, text)
# Construct guard + binding authority after retry settings are initialized.
replace_once(
    STORE,
    "        self._durable_retry_max_backoff_seconds = durable_retry_max_backoff_seconds\n\n        # Local retrieval-event tracking\n",
    '''        self._durable_retry_max_backoff_seconds = durable_retry_max_backoff_seconds

        backends = tuple(b for b in (self._backend, self._spill) if b is not None)
        coordinated = next(
            (b for b in backends if getattr(b, "coordination_identity", None)), None
        )
        identity = (
            str(getattr(coordinated, "coordination_identity"))
            if coordinated is not None
            else f"memory:{id(self)}"
        )
        self._mutation_guard = MutationGuard(identity)
        binding_candidates = [
            b for b in backends if callable(getattr(b, "claim_binding", None))
            and callable(getattr(b, "get_binding", None))
        ]
        self._binding_backend = next(
            (b for b in binding_candidates if bool(getattr(b, "durable", False))),
            binding_candidates[0] if binding_candidates else None,
        )

        # Local retrieval-event tracking
''',
)
# Safety helpers before the default-TTL property.
text = read(STORE)
insert_marker = "    @property\n    def default_ttl_seconds(self) -> int:\n"
if insert_marker not in text:
    raise RuntimeError("default_ttl_seconds marker not found")
helpers = '''    @staticmethod
    def _content_fingerprint(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()

    @staticmethod
    def _is_marker_key(hash_key: str) -> bool:
        return len(hash_key) in {12, 24} and all(c in "0123456789abcdef" for c in hash_key)

    def _checked_get_backend_locked(
        self, backend: Any, hash_key: str, role: str
    ) -> list[CompressionEntry]:
        checked = getattr(backend, "checked_get_all", None)
        try:
            if callable(checked):
                return list(checked(hash_key))
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked read capability"
                )
            entry = backend.get(hash_key)
            return [] if entry is None else [entry]
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} read failed for {hash_key}") from exc

    def _checked_items_backend_locked(self, backend: Any, role: str) -> list[tuple[str, CompressionEntry]]:
        checked = getattr(backend, "checked_items", None)
        try:
            if callable(checked):
                return list(checked())
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked enumeration capability"
                )
            return list(backend.items())
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} enumeration failed") from exc

    def _checked_index_backend_locked(self, backend: Any, role: str) -> list[tuple[float, str]]:
        checked = getattr(backend, "checked_created_at_index", None)
        try:
            if callable(checked):
                return list(checked())
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked index capability"
                )
            index = getattr(backend, "created_at_index", None)
            if callable(index):
                return list(index())
            return [(entry.created_at, key) for key, entry in backend.items()]
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} index read failed") from exc

    def _checked_delete_backend_locked(self, backend: Any, hash_key: str, role: str) -> bool:
        checked = getattr(backend, "checked_delete", None)
        try:
            if callable(checked):
                return bool(checked(hash_key))
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked delete capability"
                )
            return bool(backend.delete(hash_key))
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} delete failed for {hash_key}") from exc

    def _checked_clear_backend_locked(self, backend: Any, role: str) -> None:
        checked = getattr(backend, "checked_clear", None)
        try:
            if callable(checked):
                checked()
                return
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked clear capability"
                )
            backend.clear()
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} clear failed") from exc

    def _representations_locked(self, hash_key: str) -> list[tuple[str, CompressionEntry]]:
        rows = [
            ("primary", entry)
            for entry in self._checked_get_backend_locked(self._backend, hash_key, "primary")
        ]
        if self._spill is not None:
            rows.extend(
                ("spill", entry)
                for entry in self._checked_get_backend_locked(self._spill, hash_key, "spill")
            )
        return rows

    def _binding_record_locked(self, hash_key: str) -> tuple[str, bool] | None:
        backend = self._binding_backend
        if backend is None:
            return None
        getter = getattr(backend, "get_binding", None)
        if not callable(getter):
            return None
        try:
            record = getter(hash_key)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding read failed for {hash_key}") from exc
        return record

    def _binding_safe_locked(self, hash_key: str, entries: list[CompressionEntry]) -> bool:
        if not entries:
            return True
        fingerprints = {self._content_fingerprint(entry.original_content) for entry in entries}
        if len(fingerprints) != 1:
            return False
        fingerprint = next(iter(fingerprints))
        if not self._is_marker_key(hash_key):
            return True
        record = self._binding_record_locked(hash_key)
        if record is not None:
            recorded, conflicted = record
            return not conflicted and recorded == fingerprint
        # Legacy content-addressed rows are self-authenticating and can be
        # migrated safely without provenance. Arbitrary old explicit-key rows
        # cannot: there is no evidence which pre-upgrade producer owns the key.
        return fingerprint.startswith(hash_key)

    def _claim_binding_locked(self, hash_key: str, original: str) -> str:
        if not self._is_marker_key(hash_key):
            return "same"
        fingerprint = self._content_fingerprint(original)
        backend = self._binding_backend
        if backend is None:
            if fingerprint.startswith(hash_key):
                return "same"
            raise StorageUnavailableError(
                f"no authoritative binding store exists for explicit hash {hash_key}"
            )
        claimer = getattr(backend, "claim_binding", None)
        if not callable(claimer):
            raise StorageUnavailableError(
                f"binding backend cannot atomically claim explicit hash {hash_key}"
            )
        try:
            return str(claimer(hash_key, fingerprint))
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding claim failed for {hash_key}") from exc

    def _poison_binding_locked(self, hash_key: str, old_original: str, new_original: str) -> None:
        if not self._is_marker_key(hash_key) or self._binding_backend is None:
            return
        old = self._content_fingerprint(old_original)
        new = self._content_fingerprint(new_original)
        claimer = getattr(self._binding_backend, "claim_binding", None)
        if not callable(claimer):
            raise StorageUnavailableError(f"binding backend cannot poison {hash_key}")
        try:
            claimer(hash_key, old)
            claimer(hash_key, new)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding poison failed for {hash_key}") from exc

    def _release_binding_locked(self, hash_key: str) -> None:
        backend = self._binding_backend
        if backend is None:
            return
        release = getattr(backend, "release_binding", None)
        if not callable(release):
            return
        try:
            release(hash_key)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding release failed for {hash_key}") from exc

    def _read_live_entry_locked(self, hash_key: str) -> StoreRead:
        base: dict[str, Any] = {
            "hash": hash_key,
            "default_ttl_seconds": self._default_ttl,
            "max_entries": self._max_entries,
        }
        try:
            rows = self._representations_locked(hash_key)
        except StorageUnavailableError as exc:
            return StoreRead(None, {**base, "status": "unavailable", "reason": str(exc)})

        now = self._now()
        live = [(role, entry) for role, entry in rows if not entry.is_expired(now)]
        if not live:
            if rows:
                entry = rows[0][1]
                return StoreRead(
                    None,
                    {
                        **base,
                        "status": "expired",
                        "ttl_seconds": entry.ttl,
                        "created_at": entry.created_at,
                        "expires_at": entry.created_at + entry.ttl,
                        "age_seconds": now - entry.created_at,
                    },
                )
            return StoreRead(None, {**base, "status": "missing"})

        try:
            safe = self._binding_safe_locked(hash_key, [entry for _role, entry in live])
        except StorageUnavailableError as exc:
            return StoreRead(None, {**base, "status": "unavailable", "reason": str(exc)})
        if not safe:
            return StoreRead(
                None,
                {
                    **base,
                    "status": "unsafe",
                    "reason": "hash/content binding is ambiguous or unproven",
                },
            )

        source, entry = next((row for row in live if row[0] == "primary"), live[0])
        copied = replace(entry, search_queries=list(entry.search_queries))
        return StoreRead(
            copied,
            {
                **base,
                "status": "available",
                "ttl_seconds": entry.ttl,
                "created_at": entry.created_at,
                "expires_at": entry.created_at + entry.ttl,
                "age_seconds": now - entry.created_at,
                "source": source,
            },
            source,
        )

    def _delete_hash_verified_locked(self, hash_key: str, *, release_binding: bool) -> bool:
        primary_deleted = self._checked_delete_backend_locked(
            self._backend, hash_key, "primary"
        )
        if primary_deleted:
            self._stale_heap_entries += 1
        spill_deleted = False
        if self._spill is not None:
            spill_deleted = self._checked_delete_backend_locked(
                self._spill, hash_key, "spill"
            )
        residual = self._representations_locked(hash_key)
        if residual:
            raise StorageUnavailableError(
                f"hash {hash_key} remained reachable after verified delete"
            )
        if release_binding:
            self._release_binding_locked(hash_key)
        return primary_deleted or spill_deleted

'''
text = text.replace(insert_marker, helpers + insert_marker, 1)
write(STORE, text)
# Serialize all logical mutations. Reentrant MutationGuard handles cascade -> delete.
for method in ("store", "delete", "delete_cascade_detailed", "clear"):
    add_decorator(STORE, method, "@_serialized_mutation")

# Integrate persistent binding claim/poison into the existing collision branch.
text = read(STORE)
pattern = re.compile(
    r"(?P<indent>            )if conflicting is not None:\n.*?\n            elif existing is not None:",
    re.DOTALL,
)
match = pattern.search(text)
if not match:
    raise RuntimeError("collision branch not found")
collision = '''            binding_conflict = False
            if explicit_hash is not None:
                try:
                    if conflicting is not None:
                        self._poison_binding_locked(
                            hash_key, conflicting.original_content, original
                        )
                        binding_conflict = True
                    else:
                        binding_conflict = self._claim_binding_locked(hash_key, original) == "conflict"
                except StorageUnavailableError as exc:
                    raise CollisionSafetyError(
                        f"Cannot safely bind explicit hash {hash_key}: {exc}",
                        hash_key=hash_key,
                    ) from exc

            if conflicting is not None or binding_conflict:
                existing_len = len(conflicting.original_content) if conflicting is not None else -1
                logger.error(
                    "Hash collision detected: hash=%s tool=%s (existing_len=%d, new_len=%d) — "
                    "dropping the ambiguous binding from every tier; NEITHER content is served",
                    hash_key,
                    tool_name,
                    existing_len,
                    len(original),
                )
                try:
                    self._delete_hash_verified_locked(hash_key, release_binding=False)
                except StorageUnavailableError as exc:
                    raise CollisionSafetyError(
                        f"Cannot safely resolve collision for hash {hash_key}: cleanup could not be verified.",
                        hash_key=hash_key,
                    ) from exc
                collision_dropped = True
            elif existing is not None:'''
text = text[: match.start()] + collision + text[match.end() :]
write(STORE, text)

replace_method(
    STORE,
    "retrieve",
    '''    def retrieve(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
    ) -> CompressionEntry | None:
        """Retrieve a verified live entry; uncertainty is never an absence."""
        entry, _status = self.retrieve_with_status(
            hash_key, query, record_feedback_signal=record_feedback_signal
        )
        return entry

    def retrieve_with_status(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
    ) -> tuple[CompressionEntry | None, dict[str, Any]]:
        """Retrieve plus same-attempt availability/binding status."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            if read.entry is None:
                return None, read.status
            entry = read.entry
            if read.source == "spill":
                return entry, read.status

            entry.record_access(query)
            self._backend.set(hash_key, entry)
            if self._enable_feedback:
                self._log_retrieval(
                    hash_key=hash_key,
                    query=query,
                    items_retrieved=entry.original_item_count,
                    total_items=entry.original_item_count,
                    tool_name=entry.tool_name,
                    retrieval_type="full",
                )
            self._log_retrieval_payload(
                hash_key=hash_key,
                query=query,
                retrieval_type="full",
                payload=entry.original_content,
                items_retrieved=entry.original_item_count,
                total_items=entry.original_item_count,
                entry=entry,
            )
            result = replace(entry, search_queries=list(entry.search_queries))

        if record_feedback_signal and self._enable_feedback:
            self._emit_retrieval_signal(result.tool_name, result.compression_strategy)
        return result, read.status
''',
)
replace_method(
    STORE,
    "get_metadata",
    '''    def get_metadata(self, hash_key: str) -> dict[str, Any] | None:
        """Get metadata only for a verified live entry."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            entry = read.entry
            if entry is None:
                return None
            return {
                "hash": entry.hash,
                "tool_name": entry.tool_name,
                "original_item_count": entry.original_item_count,
                "compressed_item_count": entry.compressed_item_count,
                "query_context": entry.query_context,
                "compressed_content": entry.compressed_content,
                "created_at": entry.created_at,
                "ttl": entry.ttl,
                "compression_strategy": entry.compression_strategy,
                "original_tokens": entry.original_tokens,
                "compressed_tokens": entry.compressed_tokens,
            }
''',
)
replace_method(
    STORE,
    "search",
    '''    def search(
        self,
        hash_key: str,
        query: str,
        max_results: int = 20,
        score_threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        results, _status = self.search_with_status(
            hash_key, query, max_results=max_results, score_threshold=score_threshold
        )
        return results

    def search_with_status(
        self,
        hash_key: str,
        query: str,
        max_results: int = 20,
        score_threshold: float = 0.3,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Search one entry while preserving read availability from the same attempt."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
        entry = read.entry
        if entry is None:
            return [], read.status

        items = self._search_items_from_original(entry.original_content)
        if not items:
            return [], read.status
        item_strs = [json.dumps(item, default=str) for item in items]
        scores = self._scorer.score_batch(item_strs, query)
        scored = ((i, s.score) for i, s in zip(items, scores) if s.score >= score_threshold)
        results = [item for item, _ in heapq.nlargest(max_results, scored, key=itemgetter(1))]
        if results:
            self._record_search_access(hash_key, query)
        if self._enable_feedback:
            with self._lock:
                self._log_retrieval(
                    hash_key=hash_key,
                    query=query,
                    items_retrieved=len(results),
                    total_items=len(items),
                    tool_name=entry.tool_name,
                    retrieval_type="search",
                )
        self._log_retrieval_payload(
            hash_key=hash_key,
            query=query,
            retrieval_type="search",
            payload=json.dumps(results, ensure_ascii=False),
            items_retrieved=len(results),
            total_items=len(items),
            entry=entry,
        )
        return results, read.status
''',
)
replace_method(
    STORE,
    "search_all",
    '''    def search_all(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
    ) -> list[CrossStoreMatch]:
        return list(
            self.search_all_detailed(
                query, max_results=max_results, score_threshold=score_threshold
            ).matches
        )

    def search_all_detailed(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
    ) -> CrossStoreSearchOutcome:
        """Search every readable tier and report whether the scan was complete."""
        if not query or not query.strip():
            return CrossStoreSearchOutcome((), True)

        backends: list[tuple[str, Any]] = [("primary", self._backend)]
        if self._spill is not None:
            backends.append(("spill", self._spill))
        reasons: list[str] = []
        keys: dict[str, None] = {}
        readable_roles: set[str] = set()
        with self._lock:
            for role, backend in backends:
                try:
                    index = self._checked_index_backend_locked(backend, role)
                except StorageUnavailableError as exc:
                    reasons.append(str(exc))
                    continue
                readable_roles.add(role)
                for _created_at, hash_key in index:
                    keys.setdefault(hash_key, None)

        now = self._now()
        live_entries: list[tuple[str, CompressionEntry]] = []
        for hash_key in keys:
            rows: list[tuple[str, CompressionEntry]] = []
            for role, backend in backends:
                if role not in readable_roles:
                    continue
                try:
                    with self._lock:
                        entries = self._checked_get_backend_locked(backend, hash_key, role)
                except StorageUnavailableError as exc:
                    reasons.append(str(exc))
                    readable_roles.discard(role)
                    continue
                rows.extend((role, entry) for entry in entries if not entry.is_expired(now))
            if not rows:
                continue
            try:
                with self._lock:
                    safe = self._binding_safe_locked(hash_key, [entry for _role, entry in rows])
            except StorageUnavailableError as exc:
                reasons.append(str(exc))
                continue
            if not safe:
                reasons.append(f"unsafe hash/content binding for {hash_key}")
                continue
            _role, entry = next((row for row in rows if row[0] == "primary"), rows[0])
            live_entries.append((hash_key, entry))

        if not live_entries:
            return CrossStoreSearchOutcome((), not reasons, tuple(dict.fromkeys(reasons)))
        documents = [entry.original_content for _hash, entry in live_entries]
        scores = self._scorer.score_batch(documents, query)
        ranked = heapq.nlargest(
            max_results,
            (
                (hash_key, entry, score.score)
                for (hash_key, entry), score in zip(live_entries, scores)
                if score.score > score_threshold
            ),
            key=itemgetter(2),
        )
        matches = tuple(
            CrossStoreMatch(
                hash=hash_key,
                score=score,
                preview=self._cross_store_preview(entry.original_content),
                tool_name=entry.tool_name,
            )
            for hash_key, entry, score in ranked
        )
        return CrossStoreSearchOutcome(matches, not reasons, tuple(dict.fromkeys(reasons)))
''',
)
replace_method(
    STORE,
    "exists_any_tier",
    '''    def exists_any_tier(self, hash_key: str) -> bool:
        """Fail-closed purge predicate: uncertainty/unsafe data counts as present."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
        return read.status.get("status") in {"available", "unavailable", "unsafe"}
''',
)
replace_method(
    STORE,
    "get_entry_status",
    '''    def get_entry_status(
        self,
        hash_key: str,
        *,
        clean_expired: bool = False,
    ) -> dict[str, Any]:
        """Return a tri-state-plus safety diagnosis from one checked read."""
        with self._mutation_guard.hold():
            with self._lock:
                read = self._read_live_entry_locked(hash_key)
                if clean_expired and read.status.get("status") == "expired":
                    try:
                        self._delete_hash_verified_locked(hash_key, release_binding=True)
                    except StorageUnavailableError as exc:
                        status = dict(read.status)
                        status["cleanup_unavailable"] = str(exc)
                        return status
                return read.status
''',
)
replace_method(
    STORE,
    "delete",
    '''    def delete(self, hash_key: str) -> bool:
        """Verified all-tier delete; uncertainty is a failed deletion, never absence."""
        with self._lock:
            try:
                return self._delete_hash_verified_locked(hash_key, release_binding=True)
            except StorageUnavailableError as exc:
                logger.warning("CCR verified delete failed for %s: %s", hash_key, exc)
                return False
''',
)
replace_method(
    STORE,
    "_is_co_referenced",
    '''    def _is_co_referenced(self, nested_hash: str, *, ignoring: set[str]) -> bool:
        """Whether any readable live representation outside *ignoring* references it."""
        now = self._now()
        with self._lock:
            try:
                items = self._checked_items_backend_locked(self._backend, "primary")
                if self._spill is not None:
                    items.extend(self._checked_items_backend_locked(self._spill, "spill"))
            except StorageUnavailableError as exc:
                logger.warning(
                    "CCR co-reference proof unavailable; preserving nested hash %s: %s",
                    nested_hash,
                    exc,
                )
                return True

        from furl_ctx.ccr.marker_grammar import hashes_in_text

        return any(
            nested_hash in hashes_in_text(text)
            for key, entry in items
            if key != nested_hash and key not in ignoring and not entry.is_expired(now)
            for text in (entry.compressed_content, entry.original_content)
            if isinstance(text, str) and _may_reference_marker(text)
        )
''',
)
replace_method(
    STORE,
    "_preflight_cascade_graph",
    '''    def _preflight_cascade_graph(
        self,
        hash_key: str,
        *,
        already_visited: set[str],
    ) -> dict[str, tuple[str, ...]] | None:
        """Discover the complete checked marker graph before any mutation."""
        graph: dict[str, tuple[str, ...]] = {}
        seen = set(already_visited)
        pending = [hash_key]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            try:
                with self._lock:
                    rows = self._representations_locked(current)
                    live_entries = [
                        entry for _role, entry in rows if not entry.is_expired(self._now())
                    ]
                    if live_entries and not self._binding_safe_locked(current, live_entries):
                        logger.warning(
                            "CCR cascade preflight found unsafe binding for %s; aborting", current
                        )
                        return None
            except StorageUnavailableError as exc:
                logger.warning(
                    "CCR checked read during delete_cascade preflight failed for %s; "
                    "aborting the entire cascade before mutation: %s",
                    current,
                    exc,
                )
                return None
            nested_seen: dict[str, None] = {}
            for entry in live_entries:
                for nested_hash in self._entry_marker_hashes(entry, exclude=current):
                    nested_seen.setdefault(nested_hash, None)
            nested = tuple(nested_seen)
            graph[current] = nested
            pending.extend(
                nested_hash for nested_hash in reversed(nested) if nested_hash not in seen
            )
        return graph
''',
)
replace_method(
    STORE,
    "_delete_cascade_from_graph",
    '''    def _delete_cascade_from_graph(
        self,
        hash_key: str,
        *,
        graph: dict[str, tuple[str, ...]],
        visited: set[str],
    ) -> CascadeOutcome:
        """Apply a preflighted graph using verified all-tier deletions."""
        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)
        try:
            with self._lock:
                top_deleted = self._delete_hash_verified_locked(
                    hash_key, release_binding=True
                )
        except StorageUnavailableError as exc:
            logger.warning("CCR cascade delete could not verify %s: %s", hash_key, exc)
            return CascadeOutcome(top_deleted=False, failed_hashes=(hash_key,))
        visited.add(hash_key)

        deleted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for nested_hash in graph.get(hash_key, ()):
            if nested_hash in visited:
                continue
            if self._is_co_referenced(nested_hash, ignoring=visited):
                skipped.append(nested_hash)
                continue
            child = self._delete_cascade_from_graph(
                nested_hash, graph=graph, visited=visited
            )
            if child.top_deleted:
                deleted.append(nested_hash)
            deleted.extend(child.nested_deleted)
            skipped.extend(child.nested_shared_skipped)
            failed.extend(child.failed_hashes)
        deleted_set = set(deleted)
        return CascadeOutcome(
            top_deleted=top_deleted,
            nested_deleted=tuple(deleted),
            nested_shared_skipped=tuple(h for h in skipped if h not in deleted_set),
            failed_hashes=tuple(dict.fromkeys(failed)),
        )
''',
)
replace_method(
    STORE,
    "delete_cascade_detailed",
    '''    def delete_cascade_detailed(
        self, hash_key: str, *, _visited: set[str] | None = None
    ) -> CascadeOutcome:
        """Delete a marker graph atomically with respect to competing CCR mutations."""
        visited = _visited if _visited is not None else set()
        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)
        graph = self._preflight_cascade_graph(hash_key, already_visited=visited)
        if graph is None:
            return CascadeOutcome(top_deleted=False, failed_hashes=(hash_key,))
        return self._delete_cascade_from_graph(hash_key, graph=graph, visited=visited)
''',
)
replace_method(
    STORE,
    "clear",
    '''    def clear(self) -> int:
        """Verified all-tier wipe; return nonzero whenever emptiness is unproven."""
        uncertain = False
        residual = 0
        with self._lock:
            for role, backend in (("primary", self._backend), ("spill", self._spill)):
                if backend is None:
                    continue
                try:
                    self._checked_clear_backend_locked(backend, role)
                except StorageUnavailableError as exc:
                    logger.warning("CCR verified %s clear failed: %s", role, exc)
                    uncertain = True
            for role, backend in (("primary", self._backend), ("spill", self._spill)):
                if backend is None:
                    continue
                try:
                    residual += len(self._checked_items_backend_locked(backend, role))
                except StorageUnavailableError as exc:
                    logger.warning("CCR %s post-clear verification failed: %s", role, exc)
                    uncertain = True
            self._retrieval_events.clear()
            self._eviction_heap.clear()
            self._stale_heap_entries = 0
        return max(residual, 1 if uncertain else 0)
''',
)


# ---------------------------------------------------------------------------
# MCP: propagate same-attempt unavailable/partial states instead of retrying an
# outage into a false no-match/success.
# ---------------------------------------------------------------------------
MCP = "furl_ctx/ccr/mcp_server.py"
replace_method(
    MCP,
    "_search_all_content_sync",
    '''    def _search_all_content_sync(self, query: str) -> dict[str, Any]:
        """Blocking body of :meth:`_search_all_content` with completeness metadata."""
        store = self._get_local_store()
        detailed = getattr(store, "search_all_detailed", None)
        if callable(detailed):
            outcome = detailed(query)
            matches = list(outcome.matches)
            complete = bool(outcome.complete)
            unavailable = list(outcome.unavailable)
        else:
            matches = store.search_all(query)
            complete = True
            unavailable = []
        result: dict[str, Any] = {
            "source": "cross_store",
            "query": query,
            "count": len(matches),
            "matches": [
                {
                    "hash": match.hash,
                    "score": round(match.score, 4),
                    "preview": match.preview,
                    "tool_name": match.tool_name,
                }
                for match in matches
            ],
        }
        if not complete:
            result["partial"] = True
            result["unavailable"] = unavailable
            result["warning"] = (
                "CCR search was incomplete because at least one storage tier could not be "
                "read authoritatively; returned matches are safe, but absence is not proven."
            )
            if not matches:
                result["error"] = (
                    "CCR search is incomplete/unavailable; zero safe matches is not an "
                    "authoritative no-match result. Retry the search."
                )
        result["note"] = (
            "Ranked matches across all stored entries. Call furl_retrieve with a hash to get "
            "its full original content."
            if matches
            else (
                "No stored entry matched the query."
                if complete
                else "No safe match could be returned from the incomplete storage scan."
            )
        )
        return result
''',
)
replace_method(
    MCP,
    "_retrieve_content_sync",
    '''    def _retrieve_content_sync(
        self,
        hash_key: str,
        query: str | None,
        filters: RetrieveFilters | None = None,
    ) -> dict[str, Any]:
        """Blocking retrieve core with same-attempt availability propagation."""
        store = self._get_local_store()
        miss_status: dict[str, Any] | None = None
        if query:
            detailed_search = getattr(store, "search_with_status", None)
            if callable(detailed_search):
                results, miss_status = detailed_search(hash_key, query)
            else:
                results = store.search(hash_key, query)
            if results:
                self._stats.record_retrieval(hash_key)
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": results,
                    "count": len(results),
                }
            if miss_status is None:
                if store.exists_any_tier(hash_key):
                    miss_status = {"hash": hash_key, "status": "available"}
                else:
                    getter = getattr(store, "get_entry_status", None)
                    miss_status = (
                        getter(hash_key) if callable(getter) else {"hash": hash_key, "status": "missing"}
                    )
            if miss_status.get("status") == "available":
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "note": (
                        "Entry is available but no stored item matched the query. Retry with "
                        "a different query, or omit the query to retrieve the full original content."
                    ),
                }
        else:
            detailed_retrieve = getattr(store, "retrieve_with_status", None)
            if callable(detailed_retrieve):
                entry, miss_status = detailed_retrieve(hash_key)
            else:
                entry = store.retrieve(hash_key)
            if entry:
                self._stats.record_retrieval(hash_key)
                if filters is not None and not filters.is_empty:
                    return self._apply_retrieve_filters(hash_key, entry, filters)
                return {
                    "hash": hash_key,
                    "source": "local",
                    "content_kind": entry.tool_name,
                    "original_content": entry.original_content,
                    "original_item_count": entry.original_item_count,
                    "compressed_item_count": entry.compressed_item_count,
                    "retrieval_count": entry.retrieval_count,
                }

        from furl_ctx.cache.compression_store import format_retrieval_miss_detail

        if miss_status is None:
            get_status = getattr(store, "get_entry_status", None)
            miss_status = (
                get_status(hash_key, clean_expired=True)
                if callable(get_status)
                else {"hash": hash_key, "status": "missing"}
            )
        return {
            "error": format_retrieval_miss_detail(miss_status),
            "hash": hash_key,
            "status": miss_status.get("status", "missing"),
            "hint": "Content compressed via furl_compress is stored for the session using the configured CCR TTL.",
        }
''',
)
# Sticky failed_hashes: a later healthy read must not erase an uncertainty that
# occurred during the destructive operation itself.
text = read(MCP)
pattern = re.compile(
    r"        expected_gone = dict\.fromkeys\(\n            \(hash_key, \*outcome\.deleted_hashes\(hash_key\), \*outcome\.failed_hashes\)\n        \)\n        survivors = tuple\(h for h in expected_gone if store\.exists_any_tier\(h\)\)"
)
if not pattern.search(text):
    raise RuntimeError("_purge_one verification block not found")
text = pattern.sub(
    '''        expected_gone = dict.fromkeys(
            (hash_key, *outcome.deleted_hashes(hash_key), *outcome.failed_hashes)
        )
        readback_survivors = tuple(
            h for h in expected_gone if store.exists_any_tier(h)
        )
        survivors = tuple(
            dict.fromkeys((*outcome.failed_hashes, *readback_survivors))
        )''',
    text,
    count=1,
)
write(MCP, text)

print("PR 216 storage-safety source patch applied")
