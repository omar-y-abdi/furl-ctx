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
        raise RuntimeError(f"{path}: expected one occurrence, found {count}: {old[:100]!r}")
    write(path, text.replace(old, new, 1))


def replace_method(path: str, name: str, source: str) -> None:
    lines = read(path).splitlines(keepends=True)
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith(f"    def {name}(") or line.startswith(f"    async def {name}(")
        ),
        None,
    )
    if start is None:
        raise RuntimeError(f"{path}: method {name} not found")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("    def ") or line.startswith("    async def ") or line.startswith("    @"):
            end = i
            break
        if line and not line.startswith((" ", "\t", "\n", "\r")):
            end = i
            break
    write(path, "".join(lines[:start]) + source.rstrip() + "\n\n" + "".join(lines[end:]))


def insert_before_method(path: str, name: str, source: str) -> None:
    marker = f"    def {name}("
    text = read(path)
    pos = text.find(marker)
    if pos < 0:
        raise RuntimeError(f"{path}: insertion method {name} not found")
    write(path, text[:pos] + source.rstrip() + "\n\n" + text[pos:])


def replace_top_function(path: str, name: str, source: str) -> None:
    lines = read(path).splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith(f"def {name}(")), None)
    if start is None:
        raise RuntimeError(f"{path}: function {name} not found")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("def ") or line.startswith("class ") or line.startswith("@"):
            end = i
            break
    write(path, "".join(lines[:start]) + source.rstrip() + "\n\n" + "".join(lines[end:]))


# ---------------------------------------------------------------------------
# Backends: add a best-effort read channel separate from proof-oriented reads,
# and make expiry release identity claims while explicit deletion does not.
# ---------------------------------------------------------------------------
MEM = "furl_ctx/cache/backends/memory.py"
replace_method(
    MEM,
    "purge_expired",
    '''    def purge_expired(self, now: float) -> int:
        """Delete expired payloads and their identity claims."""
        expired = [key for key, entry in self._store.items() if entry.is_expired(now)]
        for key in expired:
            del self._store[key]
            self._bindings.pop(key, None)
        return len(expired)
''',
)

SQL = "furl_ctx/cache/backends/sqlite.py"
insert_before_method(
    SQL,
    "set",
    '''    def read_candidates(self, hash_key: str) -> tuple[list[CompressionEntry], bool]:
        """Best-effort read plus whether the durable tier was inspected.

        Ordinary retrieval is deliberately availability-first: a healthy
        volatile fallback remains usable when SQLite is locked/degraded. Safety
        callers use ``checked_get_all`` instead. Returning completeness keeps a
        miss from being misreported as authoritative absence when SQLite could
        not be inspected.
        """
        volatile = self._memory.get(hash_key)
        fallback = [] if volatile is None else [volatile]
        if self._degraded:
            return fallback, False
        try:
            row = self._run(
                "read_candidates",
                lambda conn: conn.execute(_SELECT_SQL, (_encode_text(hash_key),)).fetchone(),
            )
        except _SqliteOpFailed:
            return fallback, False
        result: list[CompressionEntry] = []
        if row is not None:
            result.append(_row_to_entry(row))
        if volatile is not None:
            result.append(volatile)
        return result, True

    def set_volatile(self, hash_key: str, entry: CompressionEntry) -> None:
        """Write only the same-process fallback without touching SQLite."""
        self._memory.set(hash_key, entry)

    def delete_volatile(self, hash_key: str) -> bool:
        """Remove only a same-process fallback row."""
        return self._memory.delete(hash_key)
''',
)
replace_method(
    SQL,
    "purge_expired",
    '''    def purge_expired(self, now: float) -> int:
        """Delete expired rows and release their now-dead identity claims."""
        file_purged = 0
        if not self._degraded:
            try:
                file_purged = self._run(
                    "purge_expired", lambda conn: self._sqlite_purge_expired(conn, now)
                )
            except _SqliteOpFailed:
                file_purged = 0

        volatile_expired = [
            key for key, entry in self._memory.items() if entry.is_expired(now)
        ]
        volatile_purged = self._memory.purge_expired(now)
        if volatile_expired and not self._degraded:
            try:
                self._run(
                    "purge_expired_fallback_bindings",
                    lambda conn: self._sqlite_release_orphaned_bindings(
                        conn, volatile_expired
                    ),
                )
            except _SqliteOpFailed:
                # Expiry GC remains fail-open. A stale claim can only veto a
                # later reuse until SQLite becomes writable; it cannot make a
                # foreign binding retrievable.
                pass
        return file_purged + volatile_purged
''',
)
replace_method(
    SQL,
    "_sqlite_purge_expired",
    '''    def _sqlite_purge_expired(self, conn: sqlite3.Connection, now: float) -> int:
        """Delete expired rows and their binding claims in one transaction."""
        with conn:
            expired = conn.execute(
                "SELECT hash_key FROM ccr_entries WHERE created_at + ttl < ?", (now,)
            ).fetchall()
            if expired:
                conn.executemany(
                    "DELETE FROM ccr_bindings WHERE hash_key = ?", expired
                )
            purged = conn.execute(_PURGE_EXPIRED_SQL, (now,)).rowcount
        if purged:
            with self._state_lock:
                self._row_count -= purged
        return int(purged)

    def _sqlite_release_orphaned_bindings(
        self, conn: sqlite3.Connection, hash_keys: list[str]
    ) -> None:
        encoded = [(_encode_text(key),) for key in hash_keys]
        with conn:
            for (hash_key,) in encoded:
                conn.execute(
                    "DELETE FROM ccr_bindings WHERE hash_key = ? "
                    "AND NOT EXISTS (SELECT 1 FROM ccr_entries WHERE hash_key = ?)",
                    (hash_key, hash_key),
                )
''',
)


# ---------------------------------------------------------------------------
# Store: keep proof operations fail-closed while ordinary reads remain
# availability-first; serialize cascade plan/apply through public delete; make
# explicit identity survive deletion but expire with the payload.
# ---------------------------------------------------------------------------
STORE = "furl_ctx/cache/compression_store.py"
replace_once(
    STORE,
    "        self._binding_backend = next(\n"
    "            (b for b in binding_candidates if bool(getattr(b, \"durable\", False))),\n"
    "            binding_candidates[0] if binding_candidates else None,\n"
    "        )\n\n"
    "        # Local retrieval-event tracking\n",
    "        self._binding_backend = next(\n"
    "            (b for b in binding_candidates if bool(getattr(b, \"durable\", False))),\n"
    "            binding_candidates[0] if binding_candidates else None,\n"
    "        )\n"
    "        # Same-process fallback bindings are evidence only for rows this store\n"
    "        # itself wrote while durable identity authority was unavailable.\n"
    "        self._volatile_bindings: dict[str, str] = {}\n\n"
    "        # Local retrieval-event tracking\n",
)
replace_once(
    STORE,
    "\n\n# Default CCR TTL is 30 minutes so markers survive normal agent sessions.",
    '''

@dataclass(frozen=True)
class CascadePlanNode:
    existed: bool
    nested: tuple[str, ...]


# Default CCR TTL is 30 minutes so markers survive normal agent sessions.''',
)
insert_before_method(
    STORE,
    "_representations_locked",
    '''    def _read_backend_candidates_locked(
        self, backend: Any, hash_key: str, role: str
    ) -> tuple[list[CompressionEntry], bool, str | None]:
        """Best-effort read with an explicit completeness bit."""
        reader = getattr(backend, "read_candidates", None)
        try:
            if callable(reader):
                entries, complete = reader(hash_key)
                reason = None if complete else f"{role} storage could not be fully inspected"
                return list(entries), bool(complete), reason
            entry = backend.get(hash_key)
            return ([] if entry is None else [entry]), True, None
        except Exception as exc:
            logger.warning("CCR %s read failed (non-fatal): %s", role, exc)
            return [], False, f"{role} read failed for {hash_key}: {exc}"
''',
)
insert_before_method(
    STORE,
    "_binding_safe_locked",
    '''    def _binding_state_locked(
        self,
        hash_key: str,
        entries: list[CompressionEntry],
        *,
        complete: bool,
    ) -> str:
        """Return ``safe``, ``unsafe`` or ``unavailable`` for one key."""
        if not entries:
            return "safe"
        fingerprints = {
            self._content_fingerprint(entry.original_content) for entry in entries
        }
        if len(fingerprints) != 1:
            return "unsafe"
        fingerprint = next(iter(fingerprints))
        if not self._is_marker_key(hash_key) or fingerprint.startswith(hash_key):
            return "safe"

        try:
            record = self._binding_record_locked(hash_key)
        except StorageUnavailableError:
            return (
                "safe"
                if self._volatile_bindings.get(hash_key) == fingerprint
                else "unavailable"
            )
        if record is not None:
            recorded, conflicted = record
            return "safe" if not conflicted and recorded == fingerprint else "unsafe"
        if self._volatile_bindings.get(hash_key) == fingerprint:
            return "safe"
        # Upgrade compatibility: one fully inspected, internally consistent
        # legacy row is safe to serve. If any tier was unreadable, absence of a
        # divergent pre-upgrade replica is not proven.
        return "safe" if complete else "unavailable"
''',
)
replace_method(
    STORE,
    "_binding_safe_locked",
    '''    def _binding_safe_locked(self, hash_key: str, entries: list[CompressionEntry]) -> bool:
        state = self._binding_state_locked(hash_key, entries, complete=True)
        if state == "unavailable":
            raise StorageUnavailableError(
                f"hash/content binding authority unavailable for {hash_key}"
            )
        return state == "safe"
''',
)
replace_method(
    STORE,
    "_read_live_entry_locked",
    '''    def _read_live_entry_locked(self, hash_key: str) -> StoreRead:
        base: dict[str, Any] = {
            "hash": hash_key,
            "default_ttl_seconds": self._default_ttl,
            "max_entries": self._max_entries,
        }
        rows: list[tuple[str, CompressionEntry]] = []
        complete = True
        reasons: list[str] = []
        for role, backend in (("primary", self._backend), ("spill", self._spill)):
            if backend is None:
                continue
            entries, tier_complete, reason = self._read_backend_candidates_locked(
                backend, hash_key, role
            )
            rows.extend((role, entry) for entry in entries)
            complete = complete and tier_complete
            if reason:
                reasons.append(reason)

        now = self._now()
        live = [(role, entry) for role, entry in rows if not entry.is_expired(now)]
        if not live:
            if not complete:
                return StoreRead(
                    None,
                    {
                        **base,
                        "status": "unavailable",
                        "reason": "; ".join(reasons),
                    },
                )
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

        state = self._binding_state_locked(
            hash_key, [entry for _role, entry in live], complete=complete
        )
        if state != "safe":
            return StoreRead(
                None,
                {
                    **base,
                    "status": state,
                    "reason": (
                        "; ".join(reasons)
                        if state == "unavailable" and reasons
                        else "hash/content binding is ambiguous or unproven"
                    ),
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
                "complete": complete,
            },
            source,
        )
''',
)
replace_method(
    STORE,
    "_retry_durable_persist",
    '''    def _retry_durable_persist(
        self,
        hash_key: str,
        entry: CompressionEntry,
        *,
        ensure_binding: bool = False,
    ) -> bool:
        """Retry durable identity + payload persistence as one logical write."""
        for attempt in range(1, self._durable_retry_attempts + 1):
            backoff = min(
                self._durable_retry_base_backoff_seconds * (2 ** (attempt - 1)),
                self._durable_retry_max_backoff_seconds,
            )
            if backoff > 0:
                time.sleep(backoff)
            with self._lock:
                if ensure_binding:
                    try:
                        binding = self._claim_binding_locked(
                            hash_key, entry.original_content
                        )
                    except StorageUnavailableError:
                        continue
                    if binding == "conflict":
                        delete_volatile = getattr(
                            self._backend, "delete_volatile", None
                        )
                        if callable(delete_volatile):
                            delete_volatile(hash_key)
                        self._volatile_bindings.pop(hash_key, None)
                        raise CollisionSafetyError(
                            f"Explicit hash {hash_key} was claimed by different content "
                            "while this durable write was retrying.",
                            hash_key=hash_key,
                        )
                if self._persist_and_report_durability(hash_key, entry):
                    self._volatile_bindings.pop(hash_key, None)
                    return True
        return False
''',
)
replace_method(
    STORE,
    "retrieve",
    '''    def retrieve(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
        _status_out: dict[str, Any] | None = None,
    ) -> CompressionEntry | None:
        """Retrieve a live entry while preserving ordinary fail-open availability."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            if _status_out is not None:
                _status_out.update(read.status)
            if read.entry is None:
                return None
            entry = read.entry
            if read.source == "spill":
                return entry

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
        return result
''',
)
replace_method(
    STORE,
    "retrieve_with_status",
    '''    def retrieve_with_status(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
    ) -> tuple[CompressionEntry | None, dict[str, Any]]:
        status: dict[str, Any] = {}
        entry = self.retrieve(
            hash_key,
            query,
            record_feedback_signal=record_feedback_signal,
            _status_out=status,
        )
        return entry, status
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
        *,
        _status_out: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search one entry and optionally expose same-attempt read status."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            if _status_out is not None:
                _status_out.update(read.status)
        entry = read.entry
        if entry is None:
            return []

        items = self._search_items_from_original(entry.original_content)
        if not items:
            return []
        item_strs = [json.dumps(item, default=str) for item in items]
        scores = self._scorer.score_batch(item_strs, query)
        scored = (
            (item, score.score)
            for item, score in zip(items, scores)
            if score.score >= score_threshold
        )
        results = [
            item for item, _ in heapq.nlargest(max_results, scored, key=itemgetter(1))
        ]
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
        return results
''',
)
replace_method(
    STORE,
    "search_with_status",
    '''    def search_with_status(
        self,
        hash_key: str,
        query: str,
        max_results: int = 20,
        score_threshold: float = 0.3,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        status: dict[str, Any] = {}
        results = self.search(
            hash_key,
            query,
            max_results=max_results,
            score_threshold=score_threshold,
            _status_out=status,
        )
        return results, status
''',
)
# search_all body is preserved in search_all_detailed; invert the wrapper so MCP
# and test spies continue to observe the public method.
replace_method(
    STORE,
    "search_all",
    '''    def search_all(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
        *,
        _outcome_out: dict[str, Any] | None = None,
    ) -> list[CrossStoreMatch]:
        outcome = self._search_all_impl(
            query, max_results=max_results, score_threshold=score_threshold
        )
        if _outcome_out is not None:
            _outcome_out["complete"] = outcome.complete
            _outcome_out["unavailable"] = outcome.unavailable
        return list(outcome.matches)
''',
)
# Rename the existing detailed implementation to a private core, then make the
# detailed API delegate through public search_all.
text = read(STORE)
needle = "    def search_all_detailed(\n"
if text.count(needle) != 1:
    raise RuntimeError("search_all_detailed target changed")
text = text.replace(needle, "    def _search_all_impl(\n", 1)
write(STORE, text)
insert_before_method(
    STORE,
    "exists",
    '''    def search_all_detailed(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
    ) -> CrossStoreSearchOutcome:
        details: dict[str, Any] = {}
        matches = self.search_all(
            query,
            max_results=max_results,
            score_threshold=score_threshold,
            _outcome_out=details,
        )
        return CrossStoreSearchOutcome(
            tuple(matches),
            bool(details.get("complete", True)),
            tuple(details.get("unavailable", ())),
        )
''',
)
replace_method(
    STORE,
    "delete",
    '''    def delete(self, hash_key: str) -> bool:
        """Verified all-tier delete; retain identity until natural expiry/full clear."""
        with self._lock:
            try:
                return self._delete_hash_verified_locked(
                    hash_key, release_binding=False
                )
            except StorageUnavailableError as exc:
                logger.warning("CCR verified delete failed for %s: %s", hash_key, exc)
                return False
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
    ) -> dict[str, CascadePlanNode] | None:
        """Discover a stable, checked marker graph before any mutation."""
        graph: dict[str, CascadePlanNode] = {}
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
                            "CCR cascade preflight found unsafe binding for %s; aborting",
                            current,
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
            graph[current] = CascadePlanNode(bool(live_entries), nested)
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
        graph: dict[str, CascadePlanNode],
        visited: set[str],
    ) -> CascadeOutcome:
        """Apply one immutable cascade plan through the public delete contract."""
        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)
        node = graph.get(hash_key, CascadePlanNode(False, ()))
        top_deleted = self.delete(hash_key)
        if node.existed and not top_deleted:
            return CascadeOutcome(top_deleted=False, failed_hashes=(hash_key,))
        visited.add(hash_key)

        deleted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for nested_hash in node.nested:
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
            nested_shared_skipped=tuple(
                h for h in skipped if h not in deleted_set
            ),
            failed_hashes=tuple(dict.fromkeys(failed)),
        )
''',
)
replace_method(
    STORE,
    "clear",
    '''    def clear(self) -> int:
        """Verified all-tier wipe; return nonzero whenever emptiness is unproven."""
        residual = 0
        uncertain = False
        with self._lock:
            for role, backend in (("primary", self._backend), ("spill", self._spill)):
                if backend is None:
                    continue
                checked = getattr(backend, "checked_clear", None)
                if callable(checked):
                    try:
                        checked()
                    except Exception as exc:
                        logger.warning("CCR verified %s clear failed: %s", role, exc)
                        uncertain = True
                    continue
                if bool(getattr(backend, "durable", False)):
                    logger.warning(
                        "CCR durable %s backend has no checked clear capability", role
                    )
                    uncertain = True
                    continue
                try:
                    backend.clear()
                except Exception as exc:
                    logger.warning("CCR %s clear failed: %s", role, exc)
                count = getattr(backend, "count", None)
                if not callable(count):
                    uncertain = True
                    continue
                try:
                    residual += max(0, int(count()))
                except Exception as exc:
                    logger.warning("CCR %s post-clear count failed: %s", role, exc)
                    uncertain = True

            self._retrieval_events.clear()
            self._eviction_heap.clear()
            self._stale_heap_entries = 0
            self._volatile_bindings.clear()
        return max(residual, 1 if uncertain else 0)
''',
)

# Collision section: preserve a valid old binding if cleanup fails, allow the
# established same-process fallback when durable identity is temporarily locked,
# and release a claim only when the prior payload actually expired.
text = read(STORE)
start = text.index("            # Hash collision handling.")
end = text.index("\n        # Collision-drop veto", start)
new_block = '''            # Hash collision handling. A caller-supplied key can alias unrelated
            # content, so live representations are inspected before any rebind.
            existing = self._backend.get(hash_key)
            spilled: CompressionEntry | None = None
            expired_spill = False
            if self._spill is not None:
                if explicit_hash is None:
                    spilled = self._recover_from_spill(hash_key)
                else:
                    try:
                        spilled = self._spill.get(hash_key)
                    except Exception as exc:
                        logger.error(
                            "CCR spill collision-domain read failed for explicit hash %s; "
                            "refusing the new binding: %s",
                            hash_key,
                            exc,
                        )
                        raise CollisionSafetyError(
                            f"Cannot safely bind explicit hash {hash_key}: the spill tier "
                            "could not be inspected for an older same-key binding.",
                            hash_key=hash_key,
                        ) from exc
                    if spilled is not None and spilled.is_expired(self._now()):
                        expired_spill = True
                        spilled = None

            if explicit_hash is not None and existing is None and expired_spill:
                try:
                    self._delete_hash_verified_locked(hash_key, release_binding=True)
                except StorageUnavailableError as exc:
                    raise CollisionSafetyError(
                        f"Cannot safely retire expired binding {hash_key}: {exc}",
                        hash_key=hash_key,
                    ) from exc

            conflicting = next(
                (
                    candidate
                    for candidate in (existing, spilled)
                    if candidate is not None and candidate.original_content != original
                ),
                None,
            )
            binding_conflict = False
            volatile_only = False
            if explicit_hash is not None and conflicting is None:
                try:
                    binding_conflict = (
                        self._claim_binding_locked(hash_key, original) == "conflict"
                    )
                except StorageUnavailableError as exc:
                    set_volatile = getattr(self._backend, "set_volatile", None)
                    if require_durable and callable(set_volatile):
                        set_volatile(hash_key, entry)
                        self._volatile_bindings[hash_key] = self._content_fingerprint(
                            original
                        )
                        heapq.heappush(
                            self._eviction_heap, (entry.created_at, hash_key)
                        )
                        volatile_only = True
                    else:
                        raise CollisionSafetyError(
                            f"Cannot safely bind explicit hash {hash_key}: {exc}",
                            hash_key=hash_key,
                        ) from exc

            if conflicting is not None or binding_conflict:
                existing_len = (
                    len(conflicting.original_content) if conflicting is not None else -1
                )
                logger.error(
                    "Hash collision detected: hash=%s tool=%s (existing_len=%d, new_len=%d) — "
                    "dropping the ambiguous binding from every tier; NEITHER content is served",
                    hash_key,
                    tool_name,
                    existing_len,
                    len(original),
                )
                try:
                    self._delete_hash_verified_locked(
                        hash_key, release_binding=False
                    )
                except StorageUnavailableError as exc:
                    raise CollisionSafetyError(
                        f"Cannot safely resolve collision for hash {hash_key}: cleanup could not be verified.",
                        hash_key=hash_key,
                    ) from exc
                # Do not poison a healthy old binding until its conflicting row
                # has actually been removed. Otherwise a failed cleanup makes
                # still-valid old content needlessly unretrievable.
                if conflicting is not None:
                    try:
                        self._poison_binding_locked(
                            hash_key, conflicting.original_content, original
                        )
                    except StorageUnavailableError as exc:
                        raise CollisionSafetyError(
                            f"Collision cleanup succeeded for {hash_key}, but its "
                            f"identity could not be quarantined: {exc}",
                            hash_key=hash_key,
                        ) from exc
                collision_dropped = True
            elif existing is not None:
                logger.debug("Duplicate store for hash=%s, updating entry", hash_key)
                self._stale_heap_entries += 1

            if not collision_dropped and not volatile_only:
                durable = self._persist_and_report_durability(hash_key, entry)
                if explicit_hash is not None:
                    fingerprint = self._content_fingerprint(original)
                    if durable:
                        self._volatile_bindings.pop(hash_key, None)
                    else:
                        self._volatile_bindings[hash_key] = fingerprint
                heapq.heappush(self._eviction_heap, (entry.created_at, hash_key))
'''
text = text[:start] + new_block + text[end:]
text = text.replace(
    "            durable = self._retry_durable_persist(hash_key, entry)\n",
    "            durable = self._retry_durable_persist(\n"
    "                hash_key, entry, ensure_binding=explicit_hash is not None\n"
    "            )\n",
    1,
)
write(STORE, text)


# ---------------------------------------------------------------------------
# MCP: call public store methods so off-loop/public-contract instrumentation is
# preserved, while carrying the same-attempt status in private output dicts.
# ---------------------------------------------------------------------------
MCP = "furl_ctx/ccr/mcp_server.py"
replace_method(
    MCP,
    "_search_all_content_sync",
    '''    def _search_all_content_sync(self, query: str) -> dict[str, Any]:
        """Blocking body of :meth:`_search_all_content` with completeness metadata."""
        store = self._get_local_store()
        details: dict[str, Any] = {}
        matches = store.search_all(query, _outcome_out=details)
        complete = bool(details.get("complete", True))
        unavailable = list(details.get("unavailable", ()))
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
        miss_status: dict[str, Any] = {}
        if query:
            results = store.search(hash_key, query, _status_out=miss_status)
            if results:
                self._stats.record_retrieval(hash_key)
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": results,
                    "count": len(results),
                }
            if not miss_status:
                getter = getattr(store, "get_entry_status", None)
                miss_status = (
                    getter(hash_key)
                    if callable(getter)
                    else {"hash": hash_key, "status": "missing"}
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
            entry = store.retrieve(hash_key, _status_out=miss_status)
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

        if not miss_status:
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


# ---------------------------------------------------------------------------
# Refine the upgrade regression: fully-readable unique legacy rows are allowed;
# the dangerous case is divergent legacy replicas under the same key.
# ---------------------------------------------------------------------------
TEST = "tests/test_compression_store_review_regressions.py"
replace_top_function(
    TEST,
    "test_legacy_unbound_spill_row_is_never_served_as_foreign_content",
    '''def test_divergent_legacy_unbound_rows_are_never_served_as_foreign_content(
    tmp_path: Any,
) -> None:
    """Pre-upgrade same-key replicas with different bytes are quarantined."""
    spill = SqliteBackend(db_path=tmp_path / "legacy-spill.sqlite3", max_rows=100)
    spill.set(OLD_HASH, _entry(OLD_HASH, "legacy-A"))
    primary = InMemoryBackend()
    primary.set(OLD_HASH, _entry(OLD_HASH, "legacy-B"))
    store = CompressionStore(
        backend=primary,
        spill=spill,
        enable_feedback=False,
    )

    assert store.retrieve(OLD_HASH) is None
    status = store.get_entry_status(OLD_HASH)
    assert status["status"] == "unsafe"
''',
)

print("PR 216 compatibility-safe storage correction applied")
