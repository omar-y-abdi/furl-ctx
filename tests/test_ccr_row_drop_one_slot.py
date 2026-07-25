"""Design A pins — a columnar row-drop stores exactly ONE logical entry (the
whole-blob parent), never one-plus-N per-row chunks.

Context
-------
The reported defect (F8): a columnar compression stored 1 whole-blob parent +
N per-row chunks into the shared Python ``compression_store``; every chunk took
a cap slot, so a few large outputs evicted still-live, within-TTL entries.
PR #165 tried to fix this by exempting the chunks from the logical cap with a
parent/child ownership model (``derived_of`` column + ``ccr_derived_links``
edge table). That model is absent on ``main`` — Design A instead stops mirroring
the per-row chunks at all: the whole-blob parent already holds every dropped
row, and row-level retrieval is served from it (``furl_retrieve(hash, query=…)``
/ ``select_field``). One columnar output therefore consumes one cap slot.

Each test names the DIMENSION it covers. The property is now enforced at the
source: a row-drop persists EXACTLY ONE store entry (the whole-blob parent) —
per-row chunks and the ``{hash}#rows`` index are no longer written anywhere
(the granular row-index machinery, and the ``_ccr_rows`` marker that advertised
it, were removed as an unretrievable false advertisement). These pins assert the
one-entry-per-output property directly, so any regression that re-inflates the
per-output entry count (a new mirror site, a nested-crush path, a cap-accounting
slip) turns them RED.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

from furl_ctx.cache import compression_store as cs
from furl_ctx.cache.backends import CompressionStoreBackend, InMemoryBackend
from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import CompressionStore
from furl_ctx.ccr.retrieve_filters import FilterError, RetrieveFilters, apply_filters
from furl_ctx.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig


def _distinct_rows(n: int, seed: int = 7) -> list[dict]:
    """``n`` near-unique dict rows — the tier where a columnar drop chunks each
    dropped row. Fields are short so the crush produces NO opaque offloads, so
    the whole-blob parent is the only non-row entry a drop can add."""
    services = ["api", "worker", "scheduler", "auth", "billing", "ingest"]
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    rows: list[dict] = []
    for i in range(n):
        h = hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
        rows.append(
            {
                "id": h[:32],
                "service": services[int(h[:2], 16) % len(services)],
                "level": levels[int(h[2:4], 16) % len(levels)],
                "latency_ms": int(h[4:8], 16) % 5000,
                "message": f"request {h[8:20]} handled",
            }
        )
    return rows


def _crush_into(
    store: CompressionStore, monkeypatch: pytest.MonkeyPatch, rows: list[dict]
) -> tuple[SmartCrusher, str]:
    """Run a real columnar crush whose mirror writes into ``store``.

    The mirror imports ``get_compression_store`` inside the function, so patching
    the module attribute redirects the whole-blob write into the store under
    test. Returns ``(crusher, parent_hash)``. A row-drop persists exactly the
    whole-blob parent — there are no per-row chunk entries to probe (the granular
    row index was removed).
    """
    monkeypatch.setattr(cs, "get_compression_store", lambda *a, **k: store)
    crusher = SmartCrusher(config=SmartCrusherConfig())
    result = crusher.crush_array_json(json.dumps(rows), query="")
    parent = result.get("ccr_hash")
    assert parent, "fixture did not drop rows (no ccr_hash) — the pin would be vacuous"
    # The fixture must drop MULTIPLE rows or the one-slot pin is weak: the parent
    # holds the full array while the visible output keeps only a small sample.
    kept = json.loads(result["items"])
    assert len(rows) - len(kept) > 1, "fixture dropped <2 rows — the pin would be weak"
    return crusher, parent


def test_columnar_drop_consumes_exactly_one_cap_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """DIMENSION: logical cap accounting.

    One columnar output is exactly ONE store entry (the whole-blob parent), not
    ``1 + N`` (parent + per-row chunks). This is the direct fix for F8: the
    ``while count() >= max_entries`` cap now counts one slot per compression, so
    a columnar-heavy session no longer evicts live within-TTL entries early.

    RED on main: ``entry_count == 1 + N``. GREEN after: ``== 1``.
    """
    store = CompressionStore(backend=InMemoryBackend(), enable_feedback=False)
    _crusher, parent = _crush_into(store, monkeypatch, _distinct_rows(200))

    assert store.get_stats()["entry_count"] == 1, (
        "a columnar drop consumed more than one cap slot — the per-row chunks are "
        "still being mirrored as independent entries"
    )
    assert store.retrieve(parent) is not None, "the one entry is the whole-blob parent"


def test_dropped_rows_recover_through_parent_not_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """DIMENSION: row-level recovery is served by the parent, not per-row entries.

    The COMPLETE, lossless recovery path is the BARE parent retrieve — it returns
    every dropped row verbatim (assertion (a)); this is the path the docs lead
    with. ``search``/``select_field`` are NARROWING conveniences over that same
    parent, not the recovery path: the BM25 ``query`` is capped (max 20 results,
    0.3 score floor, no caller knob) and can miss a present row, so (b) exercises
    it only as the happy-path narrowing it is. And NO per-row chunk hash is an
    independently addressable Python-store entry (c), so chunks never consume a
    cap/file slot.

    RED on main: the per-row chunk hashes resolve (they were mirrored).
    GREEN after: they are absent; recovery goes through the parent.
    """
    store = CompressionStore(backend=InMemoryBackend(), enable_feedback=False)
    rows = _distinct_rows(200)
    _crusher, parent = _crush_into(store, monkeypatch, rows)

    # (a) COMPLETE recovery via the BARE parent retrieve — every original row is
    # present, verbatim. This is the lossless path the docs lead with (no query,
    # no filter, no cap): it returns the full stored array.
    entry = store.retrieve(parent)
    assert entry is not None
    assert json.loads(entry.original_content) == rows, "parent must recover every dropped row"

    # (b) NARROWING conveniences over the same parent — NOT the recovery path.
    # The BM25 query and a select_field filter can each project a specific row;
    # query is capped (max 20 + a 0.3 floor) and can miss a present row, so this
    # exercises only the happy path where the id substring matches.
    target = rows[123]
    hits = store.search(parent, target["id"])
    assert any(isinstance(h, dict) and h.get("id") == target["id"] for h in hits), (
        "the query path did not surface the row from the parent"
    )
    filters = RetrieveFilters.parse({"select_field": "id", "select_equals": target["id"]})
    assert isinstance(filters, RetrieveFilters)
    outcome = apply_filters(entry.original_content, filters)
    assert not isinstance(outcome, FilterError), f"select_field rejected: {outcome}"
    assert outcome.matched_count == 1, "select_field did not project exactly the one row"

    # (c) the parent is the ONLY store entry — no per-row chunk is an
    # independently addressable entry consuming a cap/file slot. Per-row chunks
    # are not written anywhere; row-level recovery is served from the parent.
    assert store.get_stats()["entry_count"] == 1, (
        "the drop added more than the whole-blob parent — a per-row chunk was "
        "mirrored as its own entry, consuming a cap/file slot the parent covers"
    )


def test_sqlite_physical_rows_track_logical_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIMENSION: the file-level cap (regression guard for the kept _EVICT_OLDEST_SQL).

    ``SqliteBackend`` enforces a SECOND cap — ``DEFAULT_MAX_ROWS = 10_000`` via
    ``_EVICT_OLDEST_SQL`` (oldest-first, NO TTL check, no cascade). DECISION:
    keep that eviction as-is. It is a physical retention backstop, deliberately
    distinct from the logical read-gate (backend docstring). Once the per-row
    chunks are no longer mirrored, PHYSICAL sqlite rows track LOGICAL entries
    (one per compression), so the logical cap (1000) binds a full order of
    magnitude before the file cap (10 000) — the file cap is never flooded by a
    columnar drop, and making its eviction TTL-aware would not help the only case
    it can fire (>10 000 rows ALL within TTL). This pin fails if per-row chunks
    ever inflate physical rows again (the M1/A4 recurrence).

    RED on main: ``count() == 1 + N`` physical rows. GREEN after: ``== 1``.
    """
    backend = SqliteBackend(db_path=tmp_path / "ccr.sqlite3")
    try:
        store = CompressionStore(backend=backend, enable_feedback=False)
        _crush_into(store, monkeypatch, _distinct_rows(200))
        assert backend.count() == 1, (
            "a columnar drop added more than one physical sqlite row — per-row "
            "chunks are consuming file-cap rows and can flood the 10k FIFO cap"
        )
    finally:
        backend.close()


_CAP_INVERSION_WARNING = "inverting the documented cap ordering"


@pytest.mark.parametrize("backend_kind", ["inmemory", "sqlite"])
def test_handful_of_columnar_outputs_do_not_evict_within_cap_content(
    backend_kind: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DIMENSION: the user-visible OUTCOME (the reported F8 harm), not the count.

    The pins above assert the MECHANISM (entry_count == 1, physical rows == 1).
    None of them fills the store to its cap or observes an eviction — but the
    defect was never "the count is wrong", it was "a handful of large outputs
    evicted still-retrievable content well inside its TTL". This pins that
    PROMISE directly: it stores a first columnar output (the anchor) into a
    small-cap store, then a handful more, and asserts the anchor is STILL
    retrievable — and still holds its rows — afterward. It runs on both backends
    (F14) so the outcome is covered on the durable store too.

    Under Design A each columnar output is exactly ONE store entry, so the five
    outputs stored here sit under a modest cap and the anchor survives, unexpired.
    Any regression that re-inflates the per-output entry count — a second mirror
    site, a nested-crush path, a re-introduced per-row chunk write, a
    cap-accounting slip — pushes the five outputs past the cap and evicts the
    anchor (oldest), turning this RED. The cap is sized just above the five
    outputs (below twice that), so a >=2x per-output inflation is caught.
    """
    # Neutralize any ambient FURL_CCR_SQLITE_MAX_ROWS (L2): the sqlite variant
    # builds a backend at its DEFAULT cap, and an env value below max_entries would
    # evict content (or warn) spuriously.
    monkeypatch.delenv("FURL_CCR_SQLITE_MAX_ROWS", raising=False)

    # One output = one entry (Design A). This test stores five distinct outputs;
    # size the cap just above five so they all fit, but below twice five so a
    # regression writing >=2 entries per output floods and evicts the anchor.
    n_outputs = 5
    max_entries = n_outputs + 3
    assert n_outputs <= max_entries < 2 * n_outputs

    sqlite_backend = (
        SqliteBackend(db_path=tmp_path / "outcome.sqlite3") if backend_kind == "sqlite" else None
    )
    backend: CompressionStoreBackend = (
        sqlite_backend if sqlite_backend is not None else InMemoryBackend()
    )
    try:
        store = CompressionStore(backend=backend, enable_feedback=False, max_entries=max_entries)
        # Anchor = the first output. With the cap sized above one output's chunks
        # it survives its OWN crush on BOTH main and the branch, so this setup
        # guard passes either way — the RED is forced onto the OUTCOME assertion.
        _c, anchor = _crush_into(store, monkeypatch, _distinct_rows(200, seed=1))
        assert store.retrieve(anchor) is not None, "anchor not stored — fixture broken"

        # A handful more distinct outputs. Each output is one entry, so all five
        # fit under the cap with room to spare; a regression that writes extra
        # entries per output would push past the cap and evict the anchor.
        for seed in range(2, 6):
            _crush_into(store, monkeypatch, _distinct_rows(200, seed=seed))

        # OUTCOME (must fire RED on main with this message): the anchor — a
        # still-within-TTL output, well under the logical cap — is gone.
        entry = store.retrieve(anchor)
        assert entry is not None, (
            "the first columnar output was evicted while only a handful of later "
            "outputs were stored and the logical cap had ample room — per-row "
            "chunk mirroring flooded the cap and evicted still-retrievable "
            "content (the F8 harm)"
        )
        assert len(json.loads(entry.original_content)) == 200, (
            "the surviving anchor lost rows — its whole-blob content is not intact"
        )
        # F15: the five outputs must be five DISTINCT logical entries, else a
        # content collision would dedupe them and the anchor would survive
        # trivially — a vacuous green. (Reached only on the GREEN path; on main
        # the OUTCOME assertion above has already fired.)
        assert store.get_stats()["entry_count"] == 5, (
            "the five seeded outputs collapsed to fewer entries — anchor survival would be vacuous"
        )
    finally:
        if sqlite_backend is not None:
            sqlite_backend.close()


def test_inverted_physical_cap_loses_within_ttl_and_warns(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DIMENSION: the reported F8 harm is reachable TODAY via cap inversion (F12).

    Setting the physical file cap (``FURL_CCR_SQLITE_MAX_ROWS`` / ``max_rows``)
    below the logical ``max_entries`` is accepted for any positive int, and it
    inverts the documented ordering: the file cap evicts oldest-first with NO TTL
    check before the logical cap binds, so long-TTL entries are lost well inside
    their TTL — the reported harm, verbatim. The design KEEPS the physical
    backstop, so it does not PREVENT this; F4 warns loudly instead. This pins
    BOTH halves: the warning fires, AND the config genuinely loses the oldest
    within-TTL entry (so nobody later dismisses the warning as spurious). The
    other four pins never build a store in this configuration — pins 1/2 and the
    outcome pin use ``InMemoryBackend`` (no physical cap) and pin 3 uses the
    default 10 000 it never approaches — so none of them observe this harm.
    """
    max_entries = 10
    max_rows = 3  # physical cap BELOW the logical cap: the inverted, harmful config
    backend = SqliteBackend(db_path=tmp_path / "inverted.sqlite3", max_rows=max_rows)
    try:
        with caplog.at_level(logging.WARNING, logger="furl_ctx.cache.compression_store"):
            store = CompressionStore(
                backend=backend, max_entries=max_entries, enable_feedback=False
            )
        assert any(_CAP_INVERSION_WARNING in r.getMessage() for r in caplog.records), (
            "F4 warning did not fire for the inverted cap config (max_rows < max_entries)"
        )

        # Store more distinct columnar outputs than the physical cap allows. Under
        # Design A each output is ONE physical row; all are within the default TTL
        # and well under the logical cap (10). Oldest = the first output.
        _c, oldest = _crush_into(store, monkeypatch, _distinct_rows(200, seed=1))
        for seed in range(2, max_rows + 3):  # seeds 2..5 -> 5 outputs total > max_rows
            _crush_into(store, monkeypatch, _distinct_rows(200, seed=seed))

        # The physical cap fired (at most max_rows survive)...
        assert backend.count() <= max_rows, "physical file cap not enforced"
        # ...and it took the OLDEST within-TTL output with it, though the logical
        # cap (10) had ample room and nothing expired. That is the F8 harm.
        assert store.retrieve(oldest) is None, (
            "the oldest within-TTL output survived — expected the inverted physical "
            "cap to have evicted it oldest-first (the harm this config reproduces)"
        )
    finally:
        backend.close()


def test_cap_inversion_warning_fires_only_when_max_rows_below_max_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DIMENSION: the F4 cap-inversion guard itself (unpinned until now, F13).

    The guard warns IFF the backend's physical row cap sits below the logical
    ``max_entries`` — the config that inverts the documented ordering. Three
    states: it fires for that inversion, stays silent at the defaults, and stays
    silent for a backend with no physical cap at all (``InMemoryBackend`` — the
    ``getattr(_max_rows) -> None`` branch the guard's comment claims). The third
    also guards the structural fragility of reaching a PRIVATE attribute not in
    the ``CompressionStoreBackend`` Protocol: if ``_max_rows`` is renamed the
    guard silently degrades to a no-op, and assertion (1) turns red.
    """
    logger_name = "furl_ctx.cache.compression_store"
    # Neutralize ambient FURL_CCR_SQLITE_MAX_ROWS (L2): case (2) builds a backend
    # at its DEFAULT cap, and an env value below max_entries would make the guard
    # fire there and this test fail spuriously.
    monkeypatch.delenv("FURL_CCR_SQLITE_MAX_ROWS", raising=False)

    # (1) fires for the inverted config (max_rows < max_entries)
    caplog.clear()
    inverted = SqliteBackend(db_path=tmp_path / "inv.sqlite3", max_rows=3)
    try:
        with caplog.at_level(logging.WARNING, logger=logger_name):
            CompressionStore(backend=inverted, max_entries=10, enable_feedback=False)
    finally:
        inverted.close()
    assert any(_CAP_INVERSION_WARNING in r.getMessage() for r in caplog.records), (
        "guard did not fire for max_rows < max_entries (has _max_rows been renamed?)"
    )

    # (2) silent at the defaults (max_rows 10_000 >= max_entries 1000)
    caplog.clear()
    default = SqliteBackend(db_path=tmp_path / "def.sqlite3")
    try:
        with caplog.at_level(logging.WARNING, logger=logger_name):
            CompressionStore(backend=default, max_entries=1000, enable_feedback=False)
    finally:
        default.close()
    assert not any(_CAP_INVERSION_WARNING in r.getMessage() for r in caplog.records), (
        "guard fired at the default caps, where the ordering is not inverted"
    )

    # (3) silent for a backend with NO physical cap (getattr(_max_rows) -> None)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=logger_name):
        CompressionStore(backend=InMemoryBackend(), max_entries=10, enable_feedback=False)
    assert not any(_CAP_INVERSION_WARNING in r.getMessage() for r in caplog.records), (
        "guard fired for InMemoryBackend, which has no physical cap to invert"
    )
