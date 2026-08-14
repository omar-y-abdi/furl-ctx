from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


store_path = "furl_ctx/cache/compression_store.py"
mcp_path = "furl_ctx/ccr/mcp_server.py"
tests_path = "tests/test_compression_store_review_regressions.py"

replace_once(
    store_path,
    '    if status.get("status") == "expired":\n',
    '''    if status.get("status") == "available":
        return (
            "Entry is available in the CCR store, but this retrieval attempt returned "
            "no content. Retry the retrieval."
        )

    if status.get("status") == "expired":
''',
)

replace_once(
    store_path,
    '''        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)
        visited.add(hash_key)

        top_deleted = self.delete(hash_key)
        deleted: list[str] = []
''',
    '''        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)

        top_deleted = self.delete(hash_key)
        # ``delete`` intentionally fails open for spill I/O, so its boolean only
        # means that at least one tier removed a copy. A cascade may ignore this
        # parent during child co-reference checks ONLY after the hash is proven
        # unreachable from every tier. ``exists_any_tier`` is fail-closed on an
        # unreadable spill, which turns mutation-time uncertainty into a stopped
        # cascade instead of a dangling parent marker.
        if self.exists_any_tier(hash_key):
            return CascadeOutcome(top_deleted=False)
        visited.add(hash_key)

        deleted: list[str] = []
''',
)

replace_once(
    mcp_path,
    '''            # stats) so a no-match query does not inflate retrieval metrics —
            # nothing was actually retrieved.
            if store.exists(hash_key):
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": [],
''',
    '''            # stats) so a no-match query does not inflate retrieval metrics —
            # nothing was actually retrieved. The check must span BOTH tiers:
            # ``search`` is spill-aware, while ``exists`` is intentionally
            # primary-only, so using ``exists`` here can turn a spill-only
            # no-match into a false missing-entry error.
            if store.exists_any_tier(hash_key):
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": [],
''',
)

replace_once(
    tests_path,
    '''    CompressionEntry,
    CompressionStore,
)
''',
    '''    CompressionEntry,
    CompressionStore,
    format_retrieval_miss_detail,
)
''',
)

replace_once(
    tests_path,
    '''    def delete(self, hash_key: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("spill delete boom")
        return self.data.pop(hash_key, None) is not None
''',
    '''    def delete(self, hash_key: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("spill delete boom")
        return self.data.pop(hash_key, None) is not None

    def items(self) -> list[tuple[str, CompressionEntry]]:
        return list(self.data.items())
''',
)

p = Path(tests_path)
text = p.read_text(encoding="utf-8")
sentinel = "def test_delete_cascade_stops_when_parent_survives_spill_delete_failure"
if sentinel in text:
    raise SystemExit(f"{tests_path}: second-round tests already present")
text += '''


def test_delete_cascade_stops_when_parent_survives_spill_delete_failure() -> None:
    """A surviving spill parent must keep its referenced child reachable."""
    spill = _DictSpill()
    store = CompressionStore(
        backend=InMemoryBackend(),
        spill=spill,  # type: ignore[arg-type]
        enable_feedback=False,
    )
    store.store("child original", "child view", explicit_hash=CHILD_HASH)
    store.store(
        "parent original",
        f"parent view <<ccr:{CHILD_HASH}>>",
        explicit_hash=PARENT_HASH,
    )

    parent = store._backend.get(PARENT_HASH)
    assert parent is not None
    spill.data[PARENT_HASH] = parent
    assert store._backend.delete(PARENT_HASH) is True

    # Preflight can read the parent, but mutation cannot remove its spill copy.
    # The unsafe implementation added PARENT_HASH to ``visited`` first, ignored
    # that still-live parent during co-reference checks, and then deleted CHILD.
    spill.fail_delete = True
    outcome = store.delete_cascade_detailed(PARENT_HASH)

    assert outcome.top_deleted is False
    assert store.exists_any_tier(PARENT_HASH) is True
    assert store.exists_any_tier(CHILD_HASH) is True
    assert outcome.nested_deleted == ()


def test_available_miss_status_never_claims_the_hash_is_missing() -> None:
    """A read discrepancy must not be formatted as an eviction/missing claim."""
    detail = format_retrieval_miss_detail({"status": "available"})

    assert "available" in detail.lower()
    assert "No entry for this hash" not in detail


def test_mcp_query_no_match_on_spill_only_entry_is_empty_success() -> None:
    """A spill-only entry with zero query hits is still an available entry."""
    pytest.importorskip("mcp")
    from furl_ctx.ccr.mcp_server import FurlMCPServer, SessionStats

    spill = InMemoryBackend()
    store = CompressionStore(
        max_entries=1,
        backend=InMemoryBackend(),
        spill=spill,
        enable_feedback=False,
    )
    store.store("alpha beta gamma", "alpha view", explicit_hash=OLD_HASH)
    store.store("filler payload", "filler view", explicit_hash=FILLER_HASH)
    assert store.exists(OLD_HASH) is False
    assert store.exists_any_tier(OLD_HASH) is True

    server = object.__new__(FurlMCPServer)
    server._local_store = store
    server._stats = SessionStats()

    result = server._retrieve_content_sync(OLD_HASH, "absent-term")

    assert "error" not in result
    assert result["hash"] == OLD_HASH
    assert result["results"] == []
    assert result["count"] == 0
    assert "available" in result["note"].lower()
'''
p.write_text(text, encoding="utf-8")
