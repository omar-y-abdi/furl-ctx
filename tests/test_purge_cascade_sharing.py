"""RG3/RG5/RG6: the cascade purge must erase what the caller owns -- and only that.

The store is content-addressed with dedup, so two compressions that offload
IDENTICAL dropped content share ONE nested entry. Three defects around that:

* RG3 -- the cascade deleted a shared nested blob unconditionally, leaving the
  OTHER live parent's ``<<ccr:HASH>>`` marker dangling for content the user never
  purged.
* RG5 -- the cheap ``ccr:``/``hash=`` pre-check was case-sensitive while the
  marker grammar's ``GENERIC_BRACKET_PATTERN`` is ``re.IGNORECASE``, so an
  uppercase ``HASH=`` marker's blob silently survived the cascade.
* RG6 -- purge read-back verified only the TOP hash, so an incomplete cascade was
  undetectable.
"""

from __future__ import annotations

from furl_ctx.cache.compression_store import CompressionStore, _may_reference_marker
from furl_ctx.ccr.marker_grammar import hashes_in_text

NESTED_HASH = "c" * 24
PARENT_A = "a" * 24
PARENT_B = "b" * 24


def _marker(hash_key: str) -> str:
    return f"<<ccr:{hash_key}>>"


def _uppercase_marker(hash_key: str) -> str:
    """A bracket marker whose ``HASH=`` keyword is uppercase (RG5).

    ``GENERIC_BRACKET_PATTERN`` is IGNORECASE, so this IS a real reference the
    cascade must follow; only the pre-check used to disagree.
    """
    return f"[10 rows COMPRESSED to 2. Retrieve more: HASH={hash_key}]"


def _store_with_shared_nested() -> CompressionStore:
    """Two live parents A and B both referencing one shared nested blob C."""
    store = CompressionStore()
    store.store("nested original rows", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)
    store.store("B original", f"B view {_marker(NESTED_HASH)}", explicit_hash=PARENT_B)
    return store


def test_cascade_skips_nested_blob_a_live_entry_still_shares() -> None:
    """RG3 pin: purge A -> A gone, C survives, B still retrieves; purge B -> C gone."""
    store = _store_with_shared_nested()

    outcome_a = store.delete_cascade_detailed(PARENT_A)
    assert outcome_a.top_deleted is True, "the NAMED top hash always deletes"
    assert not store.exists(PARENT_A)
    # C is still referenced by live entry B, so the cascade must leave it alone.
    assert store.exists(NESTED_HASH), "shared nested blob was deleted out from under B"
    assert NESTED_HASH in outcome_a.nested_shared_skipped
    assert NESTED_HASH not in outcome_a.nested_deleted
    # B is intact AND its marker still resolves.
    assert store.exists(PARENT_B)
    assert store.retrieve(NESTED_HASH) is not None, "B's marker is now a dangling loud-miss"

    # Purging the last referent DOES take C with it.
    outcome_b = store.delete_cascade_detailed(PARENT_B)
    assert outcome_b.top_deleted is True
    assert outcome_b.nested_deleted == (NESTED_HASH,)
    assert not store.exists(NESTED_HASH), "C should go once its last referent is purged"


def test_cascade_still_deletes_unshared_nested_blob() -> None:
    """The RG3 skip must not weaken B3: a blob only ONE parent owns still goes."""
    store = CompressionStore()
    store.store("nested original", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)

    outcome = store.delete_cascade_detailed(PARENT_A)
    assert outcome.top_deleted is True
    assert outcome.nested_deleted == (NESTED_HASH,)
    assert outcome.nested_shared_skipped == ()
    assert not store.exists(NESTED_HASH)


def test_cascade_follows_uppercase_hash_marker() -> None:
    """RG5 pin: an uppercase ``HASH=`` marker's blob must not survive the cascade."""
    store = CompressionStore()
    store.store("nested original", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_uppercase_marker(NESTED_HASH)}", explicit_hash=PARENT_A)

    outcome = store.delete_cascade_detailed(PARENT_A)
    assert outcome.nested_deleted == (NESTED_HASH,), "case-sensitive pre-check skipped the blob"
    assert not store.exists(NESTED_HASH)


def test_cascade_sharing_is_detected_through_an_uppercase_marker() -> None:
    """RG3 + RG5 together: a co-reference written as ``HASH=`` still protects C."""
    store = CompressionStore()
    store.store("nested original", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)
    store.store("B original", f"B view {_uppercase_marker(NESTED_HASH)}", explicit_hash=PARENT_B)

    outcome = store.delete_cascade_detailed(PARENT_A)
    assert NESTED_HASH in outcome.nested_shared_skipped
    assert store.exists(NESTED_HASH), "an uppercase co-reference must still protect the blob"


def test_delete_cascade_tuple_wrapper_is_unchanged() -> None:
    """The back-compat ``(top_deleted, nested_count)`` contract still holds."""
    store = CompressionStore()
    store.store("nested original", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)

    assert store.delete_cascade(PARENT_A) == (True, 1)
    # Idempotent: a second cascade finds nothing.
    assert store.delete_cascade(PARENT_A) == (False, 0)


def test_cascade_is_cycle_safe() -> None:
    """A marker pointing back at an ancestor must not recurse forever."""
    store = CompressionStore()
    store.store("A original", f"A view {_marker(PARENT_B)}", explicit_hash=PARENT_A)
    store.store("B original", f"B view {_marker(PARENT_A)}", explicit_hash=PARENT_B)

    outcome = store.delete_cascade_detailed(PARENT_A)
    assert outcome.top_deleted is True
    assert not store.exists(PARENT_A)
    assert not store.exists(PARENT_B), "the cycle's other half should still be erased"


def test_deleted_hashes_reports_the_full_set_for_readback() -> None:
    """RG6: the read-back set is top + nested actually deleted, skips excluded."""
    store = _store_with_shared_nested()
    outcome = store.delete_cascade_detailed(PARENT_A)
    # C was SKIPPED (shared), so it must NOT be in the set a read-back expects gone.
    assert outcome.deleted_hashes(PARENT_A) == (PARENT_A,)

    store2 = CompressionStore()
    store2.store("nested original", "nested view", explicit_hash=NESTED_HASH)
    store2.store("A original", f"A view {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)
    outcome2 = store2.delete_cascade_detailed(PARENT_A)
    assert set(outcome2.deleted_hashes(PARENT_A)) == {PARENT_A, NESTED_HASH}


# ---------------------------------------------------------------------------
# B6 -- the pre-check must fold at least as widely as the grammar
# ---------------------------------------------------------------------------

# U+017F LATIN SMALL LETTER LONG S. `.lower()` leaves it alone; `.casefold()`
# maps it to "s", and the grammar's re.IGNORECASE matches it against "s".
_LONG_S_MARKER = f"[10 rows compressed to 2. Retrieve more: haſh={NESTED_HASH}]"


def test_casefold_precheck_agrees_with_the_grammar_on_unicode_folding() -> None:
    """B6: `.lower()` is NARROWER than the grammar's IGNORECASE, so it skipped a
    marker the grammar matches -- silently orphaning the nested blob."""
    # The grammar DOES see this as a real reference...
    assert hashes_in_text(_LONG_S_MARKER) == [NESTED_HASH]
    # ...so the cheap pre-check must not veto it. (`.lower()` returned False here.)
    assert _may_reference_marker(_LONG_S_MARKER) is True
    assert "hash=" not in _LONG_S_MARKER.lower()  # the exact old-code bypass
    assert "hash=" in _LONG_S_MARKER.casefold()


def test_cascade_follows_a_unicode_folded_marker() -> None:
    """B6 end-to-end: the blob behind a U+017F marker is cascade-deleted, not left."""
    store = CompressionStore()
    store.store("nested original rows", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_LONG_S_MARKER}", explicit_hash=PARENT_A)
    outcome = store.delete_cascade_detailed(PARENT_A)
    assert outcome.top_deleted is True
    assert NESTED_HASH in outcome.nested_deleted
    assert store.exists(NESTED_HASH) is False


def test_exists_any_tier_honors_ttl_in_the_spill_like_retrieve_does() -> None:
    """B3: an EXPIRED spill row is not retrievable, so it is not a survivor.

    ``_recover_from_spill`` returns None for an expired row, so counting one as a
    survivor would fail a purge that actually succeeded -- a loud false alarm.
    """
    store = CompressionStore(default_ttl=1)
    store.store("payload", "view", explicit_hash=PARENT_A)
    live_copy = store._backend.get(PARENT_A)
    assert live_copy is not None

    class _Spill:
        def get(self, hash_key: str):
            return live_copy if hash_key == PARENT_A else None

        def delete(self, hash_key: str) -> bool:
            return False

    store._spill = _Spill()  # type: ignore[assignment]
    store._backend.delete(PARENT_A)  # primary gone, spill still holds it
    assert store.exists_any_tier(PARENT_A) is True, "a live spill row IS a survivor"

    # Push time past the TTL: retrieve() now misses, so the read-back must agree.
    store._now = lambda: live_copy.created_at + 10_000  # type: ignore[method-assign]
    assert store.retrieve(PARENT_A) is None
    assert store.exists_any_tier(PARENT_A) is False, "an expired spill row is not a survivor"


def test_exists_any_tier_does_not_promote_the_spill_row_into_primary() -> None:
    """B3: a read-back observes; it must never mutate the store it is checking."""
    store = CompressionStore()
    store.store("payload", "view", explicit_hash=PARENT_A)
    copy = store._backend.get(PARENT_A)

    class _Spill:
        def get(self, hash_key: str):
            return copy if hash_key == PARENT_A else None

        def delete(self, hash_key: str) -> bool:
            return False

    store._spill = _Spill()  # type: ignore[assignment]
    store._backend.delete(PARENT_A)
    assert store.exists_any_tier(PARENT_A) is True
    assert store._backend.get(PARENT_A) is None, "read-back promoted the row back into primary"


def test_diamond_never_reports_a_deleted_hash_as_kept_shared() -> None:
    """#9: the two outcome lists must be DISJOINT.

    Diamond TOP->[A,B], A->[C], B->[C]: C is skipped under A (B still referenced
    it), then legitimately deleted under B once A was gone -- so C landed in BOTH
    ``nested_deleted`` and ``nested_shared_skipped``. Harmless while nothing read
    the skip list; the moment the purge tool surfaces it (B2), the agent is told
    "kept because another entry references it" about a hash that IS erased. A
    false claim about live data is exactly what the disclosure exists to prevent.
    """
    top = "d" * 24
    store = CompressionStore()
    store.store("C original", "C view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)
    store.store("B original", f"B {_marker(NESTED_HASH)}", explicit_hash=PARENT_B)
    store.store("TOP original", f"TOP {_marker(PARENT_A)} {_marker(PARENT_B)}", explicit_hash=top)

    outcome = store.delete_cascade_detailed(top)
    assert NESTED_HASH in outcome.nested_deleted, "C is genuinely deleted by this cascade"
    assert NESTED_HASH not in outcome.nested_shared_skipped, "and must not ALSO be reported kept"
    assert not set(outcome.nested_deleted) & set(outcome.nested_shared_skipped)
    # The report matches reality: C really is gone.
    assert store.exists(NESTED_HASH) is False


def test_unicode_folded_marker_also_counts_as_sharing() -> None:
    """B6: a U+017F marker is a real reference, so it must PROTECT a shared blob
    too -- under-detection is a dangling marker, over-deletion is data loss."""
    store = CompressionStore()
    store.store("nested original rows", "nested view", explicit_hash=NESTED_HASH)
    store.store("A original", f"A view {_marker(NESTED_HASH)}", explicit_hash=PARENT_A)
    store.store("B original", f"B view {_LONG_S_MARKER}", explicit_hash=PARENT_B)
    outcome = store.delete_cascade_detailed(PARENT_A)
    assert outcome.nested_shared_skipped == (NESTED_HASH,)
    assert store.exists(NESTED_HASH) is True


def test_clear_reports_zero_residual_on_a_clean_wipe() -> None:
    """F1: ``clear`` returns the count still reachable after the wipe. A store with no
    spill (or a cleanly-cleared one) empties completely, so the residual is 0 and the
    purge path can honestly report success."""
    store = CompressionStore()
    store.store("A original", "A view", explicit_hash=PARENT_A)
    store.store("B original", "B view", explicit_hash=PARENT_B)
    assert store.clear() == 0
    assert store.exists_any_tier(PARENT_A) is False


def test_clear_surfaces_a_spill_that_would_not_clear() -> None:
    """F1: a spill whose ``clear`` raises still holds its rows (retrievable through the
    spill tier). ``clear`` must COUNT them as residual instead of swallowing the error
    — that swallow was the ``furl_purge all=true`` false-erase bug (``get_stats`` is
    primary-only, so a spill survivor was invisible)."""
    store = CompressionStore()
    store.store("payload", "view", explicit_hash=PARENT_A)
    live_copy = store._backend.get(PARENT_A)
    assert live_copy is not None

    class _RaisingClearSpill:
        def get(self, hash_key: str):
            return live_copy if hash_key == PARENT_A else None

        def clear(self) -> None:
            raise OSError("spill clear failed")

        def count(self) -> int:
            return 1

    store._spill = _RaisingClearSpill()  # type: ignore[assignment]
    assert store.clear() == 1, "a spill that would not clear is a survivor, not an all-clear"
    # Truthful: the row really is still reachable via the spill tier.
    assert store.retrieve(PARENT_A) is not None


def test_clear_fails_closed_when_the_spill_cannot_even_be_counted() -> None:
    """F1: if a broken spill raises from BOTH ``clear`` and ``count``, ``clear`` cannot
    prove the spill empty, so it reports >=1 residual -- a loud retryable purge error,
    never a false all-clear (mirrors ``exists_any_tier``'s unreadable-spill stance)."""
    store = CompressionStore()
    store.store("payload", "view", explicit_hash=PARENT_A)

    class _BrokenSpill:
        def get(self, hash_key: str):
            return None

        def clear(self) -> None:
            raise OSError("spill clear failed")

        def count(self) -> int:
            raise OSError("spill count failed")

    store._spill = _BrokenSpill()  # type: ignore[assignment]
    assert store.clear() == 1, "an un-countable spill must fail closed"
