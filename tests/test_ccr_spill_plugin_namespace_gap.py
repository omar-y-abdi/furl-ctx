"""T6 (retention audit): ``FURL_CCR_SPILL=1`` now protects the store the shipped
plugin actually writes to -- a per-namespace durable spill tier.

History. PR #134 pinned this as a GAP: ``FURL_CCR_SPILL=1`` wired a spill tier
only into ``get_compression_store()``'s GLOBAL singleton, but the furl plugin
never uses that singleton. Every hook (``compress_tool_output.py``,
``pipe_compress.py``) and the MCP server (``furl_ctx/ccr/mcp_server.py``)
unconditionally ``os.environ.setdefault("FURL_CCR_PROJECT_DIR", ...)``, which
routes every compress()/store call through ``resolve_ccr_namespace_store()`` ->
``_build_namespace_store()`` instead. That function deliberately wired NO spill,
so a namespaced tenant dropped entries at the 1000-entry cap even with the flag
on, and flipping ``FURL_CCR_SPILL=1`` in the plugin config was a byte-for-byte
no-op.

Fix (this batch). ``_build_namespace_store`` now wires a PER-NAMESPACE durable
spill (``_build_namespace_spill_backend``): a capacity-evicted entry is demoted
to this namespace's OWN ``ccr-ns-<digest>-spill.sqlite3`` instead of being
dropped, and retrieval falls through primary->spill, so a ``<<ccr:HASH>>`` marker
stays resolvable past the in-memory eviction window. Each namespace spills to its
OWN hashed file, so one tenant can never read another's spilled rows -- the
isolation the global shared-file spill could not offer, and the reason the global
builder withholds a spill when the primary is already sqlite. The plugin config
now sets ``FURL_CCR_SPILL`` so the shipped deployment actually gets this.

So this file is the red-to-green pin: the scenario PR #134 asserted as a loud
miss now asserts a spill HIT. On the pre-fix code (namespace store with no spill)
the HIT assertion below fails -- that is the RED this fix turns GREEN.
"""

from __future__ import annotations

import os
import stat

import pytest

from furl_ctx.cache.compression_store import (
    reset_compression_store,
    resolve_ccr_namespace_store,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Hermetic: never touch the developer's real ~/.furl, and reset both the
    global singleton and the per-namespace store registry before/after so no
    entry, env, or spill file leaks across tests."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("FURL_CCR_SQLITE_PATH", raising=False)
    monkeypatch.delenv("FURL_CCR_SPILL", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


def _fill(store, n: int, *, prefix: str) -> list[str]:
    return [
        store.store(
            original=f'[{{"row": "{prefix}-{i}", "payload": "value-{i}"}}]',
            compressed=f"<<ccr:{prefix}-{i}>>",
        )
        for i in range(n)
    ]


def _overflow_victim(store, *, tag: str = "row") -> str:
    """Fill to the 1000-entry cap, then push past it, and return the hash of the
    oldest entry -- the one capacity eviction demotes to the spill tier.

    ``tag`` distinguishes each namespace's CONTENT: CCR is content-addressed, so
    two namespaces filled with identical content would hash to the SAME key and a
    cross-namespace read test would be vacuous. Distinct tags give distinct
    hashes, so a cross-read miss genuinely proves file-level isolation.
    """
    hashes = _fill(store, 1000, prefix=tag)
    victim = hashes[0]
    assert store.retrieve(victim) is not None, "precondition: live before overflow"
    _fill(store, 5, prefix=f"{tag}-overflow")  # push 5 moderate writes past the cap
    assert store.get_stats()["entry_count"] == 1000, "capacity eviction must have occurred"
    return victim


def test_spill_env_flag_now_protects_the_plugin_namespaced_store(monkeypatch, tmp_path) -> None:
    """RED->GREEN pin. Reproduces the plugin's REAL runtime env exactly (every hook
    and the MCP server set these three) and proves an entry evicted past the
    1000-row cap is now RECOVERED from the per-namespace spill tier with
    ``FURL_CCR_SPILL=1`` set.

    This is the exact scenario PR #134 pinned as a loud miss. On the pre-fix code
    (``_build_namespace_store`` wiring no spill) ``retrieve(victim)`` returns None
    and the ``is not None`` assertion below FAILS -- that failure is the RED this
    fix turns GREEN.
    """
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SPILL", "1")

    store = resolve_ccr_namespace_store()
    assert store is not None, (
        "FURL_CCR_PROJECT_DIR must select the namespaced store, not the global one"
    )

    victim = _overflow_victim(store)

    recovered = store.retrieve(victim)
    assert recovered is not None, (
        "FURL_CCR_SPILL=1 must now rescue an evicted entry from the plugin's real, "
        "per-project-namespaced store -- _build_namespace_store wires a per-namespace "
        "spill tier, so the marker stays resolvable past the 1000-entry cap"
    )
    assert recovered.original_content == '[{"row": "row-0", "payload": "value-0"}]', (
        "a spill hit must be byte-exact -- the same value that was evicted"
    )


def test_spill_default_off_keeps_the_namespaced_store_a_loud_miss(monkeypatch, tmp_path) -> None:
    """Gate proof. With ``FURL_CCR_SPILL`` UNSET (the default) the namespaced store
    stays single-tier and byte-identical to before this fix: a capacity-evicted
    entry is a loud miss. The spill only ever fires when the operator/plugin opts
    in, so default behavior is unchanged."""
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.delenv("FURL_CCR_SPILL", raising=False)

    store = resolve_ccr_namespace_store()
    assert store is not None

    victim = _overflow_victim(store)

    assert store.retrieve(victim) is None, (
        "spill OFF (default): an evicted entry must remain a loud miss -- the "
        "per-namespace spill is opt-in behind FURL_CCR_SPILL, exactly like the global path"
    )


def test_two_namespaces_spill_to_different_files_and_cannot_read_each_other(
    monkeypatch, tmp_path
) -> None:
    """Isolation contrast. Two projects (two namespaces), both with
    ``FURL_CCR_SPILL=1``, each overflow a victim into the spill. Each tenant
    recovers its OWN spilled victim; neither can read the other's. The two spills
    are physically DIFFERENT files, and each is 0600 under a 0700 dir -- so
    retention past the cap never demotes tenants into one shared database."""
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SPILL", "1")

    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "project-a"))
    store_a = resolve_ccr_namespace_store()
    assert store_a is not None
    victim_a = _overflow_victim(store_a, tag="alpha")

    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "project-b"))
    store_b = resolve_ccr_namespace_store()
    assert store_b is not None
    victim_b = _overflow_victim(store_b, tag="beta")

    assert store_a is not store_b, "distinct projects must resolve to distinct stores"
    assert victim_a != victim_b, "distinct content must hash distinctly (content-addressed)"

    # Each tenant recovers its own spilled victim...
    assert store_a.retrieve(victim_a) is not None, "namespace A must recover its OWN spilled entry"
    assert store_b.retrieve(victim_b) is not None, "namespace B must recover its OWN spilled entry"
    # ...and neither can read the other's spilled entry (no shared file).
    assert store_a.retrieve(victim_b) is None, (
        "namespace A must NOT read namespace B's spilled entry"
    )
    assert store_b.retrieve(victim_a) is None, (
        "namespace B must NOT read namespace A's spilled entry"
    )

    # Physically two distinct spill files, each 0600, under a 0700 workspace dir.
    spill_files = sorted(workspace.glob("ccr-ns-*-spill.sqlite3"))
    assert len(spill_files) == 2, (
        f"each namespace must own a distinct spill file, found {[p.name for p in spill_files]}"
    )
    for spill in spill_files:
        assert stat.S_IMODE(spill.stat().st_mode) == 0o600, f"{spill.name} must be 0600"
    assert stat.S_IMODE(os.stat(workspace).st_mode) == 0o700, "the spill dir must be 0700"


def test_spill_env_flag_does_protect_the_bare_global_store_for_contrast(
    monkeypatch, tmp_path
) -> None:
    """Contrast case: the SAME env flag, on the store the plugin does NOT use
    (FURL_CCR_PROJECT_DIR unset -> the bare global singleton), also works --
    confirming the spill mechanism itself is sound (covered in depth by
    ``tests/test_ccr_spill_tier.py``) and that the fix above is specifically a
    per-namespace wiring, not a change to the global spill feature."""
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.setenv("FURL_CCR_SPILL", "1")
    assert resolve_ccr_namespace_store() is None, "no project dir set -> no namespaced store"

    from furl_ctx.cache.compression_store import get_compression_store

    store = get_compression_store(max_entries=3)
    hashes = _fill(store, 3, prefix="row")
    victim = hashes[0]
    _fill(store, 3, prefix="overflow")

    assert store.retrieve(victim) is not None, (
        "the global store DOES honor FURL_CCR_SPILL=1 -- the mechanism itself works, "
        "the plugin just reaches it through the per-namespace path instead"
    )
