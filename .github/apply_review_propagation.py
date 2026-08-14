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
    '''    ``nested_deleted`` is the DISTINCT nested hashes the cascade removed;
    ``nested_shared_skipped`` is those it deliberately left because another live
    entry still references them (RG3). ``deleted_hashes`` is the full set a
    read-back should find gone (RG6) — the top hash only when it truly went.
    """

    top_deleted: bool
    nested_deleted: tuple[str, ...] = ()
    nested_shared_skipped: tuple[str, ...] = ()
''',
    '''    ``nested_deleted`` is the DISTINCT nested hashes the cascade removed;
    ``nested_shared_skipped`` is those it deliberately left because another live
    entry still references them (RG3). ``failed_hashes`` names hashes the cascade
    attempted to remove but proved still reachable from at least one tier. Those
    failures propagate to the purge read-back so partial mutation cannot be
    reported as a clean erase. ``deleted_hashes`` is the full set a read-back
    should find gone (RG6) — the top hash only when it truly went.
    """

    top_deleted: bool
    nested_deleted: tuple[str, ...] = ()
    nested_shared_skipped: tuple[str, ...] = ()
    failed_hashes: tuple[str, ...] = ()
''',
)

replace_once(
    store_path,
    '''        if self.exists_any_tier(hash_key):
            return CascadeOutcome(top_deleted=False)
        visited.add(hash_key)

        deleted: list[str] = []
        skipped: list[str] = []
''',
    '''        if self.exists_any_tier(hash_key):
            return CascadeOutcome(top_deleted=False, failed_hashes=(hash_key,))
        visited.add(hash_key)

        deleted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
''',
)

replace_once(
    store_path,
    '''            if child.top_deleted:
                deleted.append(nested_hash)
            deleted.extend(child.nested_deleted)
            skipped.extend(child.nested_shared_skipped)

        deleted_set = set(deleted)
        return CascadeOutcome(
            top_deleted=top_deleted,
            nested_deleted=tuple(deleted),
            nested_shared_skipped=tuple(h for h in skipped if h not in deleted_set),
        )
''',
    '''            if child.top_deleted:
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

replace_once(
    mcp_path,
    '''        # hashes verified are the ones the cascade actually removed. dict.fromkeys
        # dedupes while keeping order -- the top hash appears in both sources.
        expected_gone = dict.fromkeys((hash_key, *outcome.deleted_hashes(hash_key)))
''',
    '''        # hashes verified are the ones the cascade actually removed PLUS any
        # hash whose mutation-time delete was proven incomplete. The latter must
        # be read back too, otherwise a surviving nested child can disappear from
        # the outcome and a partial purge can be reported as success. dict.fromkeys
        # dedupes while keeping order -- the top hash appears in both sources.
        expected_gone = dict.fromkeys(
            (hash_key, *outcome.deleted_hashes(hash_key), *outcome.failed_hashes)
        )
''',
)

replace_once(
    mcp_path,
    '''            # gone from the store. Use the side-effect-free ``exists`` check
            # (not ``retrieve``, which logs a retrieval event + bumps access
''',
    '''            # gone from the store. Use the side-effect-free ``exists_any_tier``
            # check (not ``retrieve``, which logs a retrieval event + bumps access
''',
)

replace_once(
    tests_path,
    '''    assert outcome.top_deleted is False
    assert store.exists_any_tier(PARENT_HASH) is True
    assert store.exists_any_tier(CHILD_HASH) is True
    assert outcome.nested_deleted == ()
''',
    '''    assert outcome.top_deleted is False
    assert store.exists_any_tier(PARENT_HASH) is True
    assert store.exists_any_tier(CHILD_HASH) is True
    assert outcome.nested_deleted == ()
    assert outcome.failed_hashes == (PARENT_HASH,)
''',
)
