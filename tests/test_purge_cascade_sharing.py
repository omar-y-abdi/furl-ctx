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

from furl_ctx.cache.compression_store import CompressionStore

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
