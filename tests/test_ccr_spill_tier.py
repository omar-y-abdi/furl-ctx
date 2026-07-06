"""CCR SPILL TIER (Q10 retention): evicted-but-live entries are DEMOTED to a
durable spill backend instead of deleted, so a ``<<ccr:HASH>>`` marker stays
resolvable past the in-memory eviction window.

Contract pinned here:

* Spill ON  — an entry evicted from the primary under capacity pressure is
  still retrievable via ``store.retrieve()`` (byte-exact ``original_content``),
  because it was demoted to the spill.
* Spill OFF — the same overflow is a loud miss (today's single-tier behavior,
  byte-identical). This is the regression guard for the default path.
* Expiry is honored in the spill (an expired spilled row reads as a miss).
* Fail-open — a raising spill backend never breaks the primary retrieval or a
  fresh ``store`` (the store's own try/except owns fail-open, NOT the backend).
* Cross-tier round trip — written → evicted → retrieved is byte-exact.

Tests inject the spill via the ``CompressionStore(spill=...)`` constructor
param (no env mutation, no monkeypatching of module globals) and use the real
public ``store``/``retrieve`` surface — the exact path production resolves
markers through (``store.retrieve(hash).original_content``).
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.backends.memory import InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    CompressionEntry,
    CompressionStore,
    get_compression_store,
    reset_compression_store,
)


@pytest.fixture(autouse=True)
def _isolate_sqlite(monkeypatch, tmp_path):
    """Point the default SqliteBackend path at a per-test workspace so no test
    touches the developer's ~/.furl or a shared ccr.sqlite3, and reset the
    global singleton on BOTH sides so a two-tier global built from
    ``FURL_CCR_SPILL`` never leaks into a later test file that asserts loud-miss
    on the global store (e.g. ``test_ccr_eviction_loud_miss``)."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("FURL_CCR_SQLITE_PATH", raising=False)
    monkeypatch.delenv("FURL_CCR_SPILL", raising=False)
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


def _sqlite_spill(tmp_path) -> SqliteBackend:
    return SqliteBackend(db_path=tmp_path / "spill.sqlite3")


def _fill(store: CompressionStore, n: int, *, prefix: str) -> list[str]:
    """Store ``n`` distinct entries; return their hash keys in insertion order."""
    hashes: list[str] = []
    for i in range(n):
        h = store.store(
            original=f'[{{"row": "{prefix}-{i}", "payload": "value-{i}"}}]',
            compressed=f"<<ccr:{prefix}-{i}>>",
        )
        hashes.append(h)
    return hashes


# --------------------------------------------------------------------------- #
# Spill ON vs OFF: the core demotion behavior
# --------------------------------------------------------------------------- #


def test_spill_on_capacity_evicted_entry_is_still_retrievable(tmp_path) -> None:
    """Overflow the primary; the oldest (evicted) entry is recoverable from the
    spill via the production retrieval call, byte-exact on ``original_content``."""
    store = CompressionStore(
        max_entries=3,
        backend=InMemoryBackend(),
        spill=_sqlite_spill(tmp_path),
        enable_feedback=False,
    )
    hashes = _fill(store, 3, prefix="on")
    victim = hashes[0]
    victim_content = store.retrieve(victim).original_content  # live in primary

    # Overflow the cap so the oldest-created (victim) is evicted from primary.
    _fill(store, 3, prefix="on-overflow")

    recovered = store.retrieve(victim)
    assert recovered is not None, "spill ON: capacity-evicted entry must survive in the spill"
    assert recovered.original_content == victim_content, (
        "spill hit must be byte-exact — the same value that was evicted"
    )


def test_spill_off_capacity_evicted_entry_is_a_loud_miss(tmp_path) -> None:
    """The default (spill=None) path is unchanged: an evicted entry is a miss.

    This is the byte-identical-default regression guard.
    """
    store = CompressionStore(
        max_entries=3,
        backend=InMemoryBackend(),
        spill=None,
        enable_feedback=False,
    )
    hashes = _fill(store, 3, prefix="off")
    victim = hashes[0]
    assert store.retrieve(victim) is not None  # precondition: live before overflow

    _fill(store, 3, prefix="off-overflow")

    assert store.retrieve(victim) is None, (
        "spill OFF: an evicted entry must remain a loud miss (today's behavior)"
    )


# --------------------------------------------------------------------------- #
# Expiry is honored in the spill
# --------------------------------------------------------------------------- #


def test_spill_honors_expiry_expired_row_is_a_miss(tmp_path) -> None:
    """A spilled entry whose TTL has elapsed reads as a miss, exactly like the
    primary. Uses a fake clock so time advances without sleeping."""
    clock = {"now": 1000.0}
    store = CompressionStore(
        max_entries=2,
        backend=InMemoryBackend(),
        spill=_sqlite_spill(tmp_path),
        enable_feedback=False,
        now_fn=lambda: clock["now"],
    )
    victim = store.store(
        original='[{"row": "expiring"}]',
        compressed="<<ccr:expiring>>",
        ttl=100,
    )
    # Fill to force the victim out of the primary and into the spill.
    _fill(store, 2, prefix="exp-overflow")
    assert store.retrieve(victim) is not None, "precondition: recoverable from spill while live"

    # Advance past the victim's TTL — the spilled row is now expired.
    clock["now"] += 101
    assert store.retrieve(victim) is None, "an expired spilled row must be a miss"


# --------------------------------------------------------------------------- #
# Fail-open: a broken spill never breaks the primary or compression
# --------------------------------------------------------------------------- #


class _RaisingSpill:
    """A spill backend whose every Protocol method raises. Exercises the store's
    OWN fail-open guard (SqliteBackend degrades internally and never raises, so
    it cannot exercise the store's try/except)."""

    def get(self, hash_key: str) -> CompressionEntry | None:
        raise RuntimeError("spill get boom")

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        raise RuntimeError("spill set boom")

    def delete(self, hash_key: str) -> bool:
        raise RuntimeError("spill delete boom")

    def clear(self) -> None:
        raise RuntimeError("spill clear boom")

    def count(self) -> int:
        raise RuntimeError("spill count boom")

    def items(self) -> list[tuple[str, CompressionEntry]]:
        raise RuntimeError("spill items boom")

    def get_stats(self) -> dict[str, object]:
        raise RuntimeError("spill stats boom")


def test_spill_write_failure_does_not_break_eviction_or_primary() -> None:
    """A spill.set() that raises on eviction is swallowed: the primary still
    evicts (capacity is freed) and live entries stay retrievable."""
    store = CompressionStore(
        max_entries=3,
        backend=InMemoryBackend(),
        spill=_RaisingSpill(),
        enable_feedback=False,
    )
    _fill(store, 3, prefix="failwrite")
    # Overflow — each eviction calls the raising spill.set(); must not propagate.
    survivors = _fill(store, 3, prefix="failwrite-overflow")

    # Capacity was actually enforced (eviction proceeded despite the spill error).
    assert store.get_stats()["entry_count"] == 3
    # The newest entries are live in the primary — retrieval is unaffected.
    for h in survivors:
        assert store.retrieve(h) is not None


def test_spill_read_failure_is_a_clean_miss() -> None:
    """A spill.get() that raises on a primary miss is swallowed and treated as a
    miss (fail-open) — never an exception out of ``retrieve``."""
    store = CompressionStore(
        max_entries=2,
        backend=InMemoryBackend(),
        spill=_RaisingSpill(),
        enable_feedback=False,
    )
    # Retrieve a key that was never stored → primary miss → spill.get raises.
    assert store.retrieve("deadbeefdeadbeefdeadbeef") is None


# --------------------------------------------------------------------------- #
# Cross-tier round trip
# --------------------------------------------------------------------------- #


def test_cross_tier_written_evicted_retrieved_byte_exact(tmp_path) -> None:
    """Full lifecycle: an entry is written to the primary, evicted to the spill,
    and retrieved byte-exact — the whole point of the tier."""
    payload = '[{"id": 1, "text": "café ☕ — byte-exact unicode ✓", "n": 42}]'
    store = CompressionStore(
        max_entries=2,
        backend=InMemoryBackend(),
        spill=_sqlite_spill(tmp_path),
        enable_feedback=False,
    )
    h = store.store(original=payload, compressed="<<ccr:xtier>>")
    assert store.retrieve(h).original_content == payload  # live in primary

    # Evict it into the spill by overflowing the cap.
    _fill(store, 2, prefix="xtier-overflow")

    recovered = store.retrieve(h)
    assert recovered is not None, "entry must be recoverable from the spill after eviction"
    assert recovered.original_content == payload, "cross-tier recovery must be byte-exact"


def test_spill_hit_does_not_promote_into_primary(tmp_path) -> None:
    """A spill hit is read-only: it is NOT resurrected into the primary (which
    would desync the eviction heap and mutate access bookkeeping). The primary
    count stays put and repeated reads keep returning the byte-exact value."""
    store = CompressionStore(
        max_entries=3,
        backend=InMemoryBackend(),
        spill=_sqlite_spill(tmp_path),
        enable_feedback=False,
    )
    hashes = _fill(store, 3, prefix="nopromote")
    victim = hashes[0]
    _fill(store, 3, prefix="nopromote-overflow")

    count_before = store.get_stats()["entry_count"]
    first = store.retrieve(victim)
    second = store.retrieve(victim)
    assert first is not None and second is not None
    assert first.original_content == second.original_content
    assert store.get_stats()["entry_count"] == count_before, (
        "spill hit must not promote the entry back into primary"
    )


# --------------------------------------------------------------------------- #
# Production activation path: FURL_CCR_SPILL through get_compression_store()
# (markers resolve through the global store, never a hand-built one — so the
# flag→env-factory→global wiring is the path that actually ships).
# --------------------------------------------------------------------------- #


def test_env_flag_on_wires_spill_into_global_store(monkeypatch) -> None:
    """``FURL_CCR_SPILL=1`` builds a two-tier global: an entry evicted from the
    default in-memory primary is recoverable through ``get_compression_store``."""
    monkeypatch.setenv("FURL_CCR_SPILL", "1")
    reset_compression_store()

    store = get_compression_store(max_entries=3)
    hashes = _fill(store, 3, prefix="env-on")
    victim = hashes[0]
    victim_content = store.retrieve(victim).original_content
    _fill(store, 3, prefix="env-on-overflow")

    assert store.retrieve(victim) is not None, (
        "FURL_CCR_SPILL=1: evicted entry must recover through the global store"
    )
    assert store.retrieve(victim).original_content == victim_content


def test_env_flag_unset_keeps_global_store_single_tier(monkeypatch) -> None:
    """Default (flag unset): the global store has no spill, so an evicted entry
    is a loud miss — the byte-identical-default guard for the PRODUCTION path."""
    monkeypatch.delenv("FURL_CCR_SPILL", raising=False)
    reset_compression_store()

    store = get_compression_store(max_entries=3)
    hashes = _fill(store, 3, prefix="env-off")
    victim = hashes[0]
    assert store.retrieve(victim) is not None  # precondition: live before overflow
    _fill(store, 3, prefix="env-off-overflow")

    assert store.retrieve(victim) is None, (
        "flag unset: the global store must stay single-tier (evicted → loud miss)"
    )
