"""T6 (retention audit): ``FURL_CCR_SPILL=1`` has NO effect on the store the
shipped plugin actually writes to.

Premise checked here, empirically, before touching any plugin config:
``tests/test_ccr_spill_tier.py``'s "production activation path" tests
(``test_env_flag_on_wires_spill_into_global_store`` and its sibling) prove spill
wires into ``get_compression_store()``'s GLOBAL singleton. But the furl plugin
never uses that singleton: every hook (``compress_tool_output.py``,
``pipe_compress.py``) and the MCP server (``furl_ctx/ccr/mcp_server.py``)
unconditionally ``os.environ.setdefault("FURL_CCR_PROJECT_DIR", ...)`` (falling
back through ``CLAUDE_PROJECT_DIR`` to ``cwd`` to ``os.getcwd()``, so it is NEVER
actually empty), which routes every compress()/store call through
``resolve_ccr_namespace_store()`` -> ``_build_namespace_store()`` instead
(``furl_ctx/cache/compression_store.py``). That function deliberately never wires
a spill backend regardless of ``FURL_CCR_SPILL`` -- see its own comment: "the env
spill (FURL_CCR_SPILL) targets the shared global ccr.sqlite3, so wiring one would
demote every tenant into a single shared file -- the exact cross-namespace leak
this isolation forbids."

So flipping ``FURL_CCR_SPILL=1`` in the plugin's ``hooks.json`` / ``.mcp.json``
env would be a byte-for-byte no-op for the deployment that ships: it changes
nothing about what a real Claude Code session experiences. This is pinned as a
PASSING test (today's true, current behavior) rather than shipped as a "fix" that
does not fix anything -- see ``docs/audits/IMPROVEMENT-LEDGER.md`` and the PR
description for the full writeup and what a real fix would require (wiring a
per-namespace spill backend into ``_build_namespace_store``, out of scope for
this batch: that module is not in this batch's touched-file allowlist, and it
would need its own design pass, not a one-line config flip, to avoid the exact
cross-namespace file-sharing regression that function's docstring already warns
against).
"""

from __future__ import annotations

import pytest

from furl_ctx.cache.compression_store import (
    reset_compression_store,
    resolve_ccr_namespace_store,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Hermetic: never touch the developer's real ~/.furl, and reset both the
    global singleton and the per-namespace store registry before/after so no
    entry or env leaks across tests."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("FURL_CCR_SQLITE_PATH", raising=False)
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


def test_spill_env_flag_does_not_protect_the_plugin_namespaced_store(monkeypatch, tmp_path) -> None:
    """Reproduces the plugin's REAL runtime env exactly (every hook and the MCP
    server set these three) and proves an entry evicted past the 1000-row cap
    stays a loud miss even with FURL_CCR_SPILL=1 set -- the namespaced store never
    wires a spill tier, so the env flag this batch was asked to flip has no
    observable effect on the shipped plugin."""
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_SPILL", "1")

    store = resolve_ccr_namespace_store()
    assert store is not None, (
        "FURL_CCR_PROJECT_DIR must select the namespaced store, not the global one"
    )

    hashes = _fill(store, 1000, prefix="row")
    victim = hashes[0]
    assert store.retrieve(victim) is not None, "precondition: live before overflow"

    _fill(store, 5, prefix="overflow")  # push 5 more moderate writes past the cap

    assert store.get_stats()["entry_count"] == 1000, "capacity eviction must have actually occurred"
    assert store.retrieve(victim) is None, (
        "FURL_CCR_SPILL=1 must NOT rescue an evicted entry in the plugin's real, "
        "per-project-namespaced store -- if this starts passing (victim IS "
        "retrievable), _build_namespace_store started wiring a spill tier and "
        "the plugin config change this batch deliberately withheld can now ship"
    )


def test_spill_env_flag_does_protect_the_bare_global_store_for_contrast(
    monkeypatch, tmp_path
) -> None:
    """Contrast case: the SAME env flag, on the store the plugin does NOT use
    (FURL_CCR_PROJECT_DIR unset -> the bare global singleton), genuinely works --
    confirming the spill mechanism itself is sound (also covered in depth by
    ``tests/test_ccr_spill_tier.py``) and that the namespaced-store gap above is
    specifically a namespacing wiring gap, not a broken spill feature."""
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
        "the plugin just never reaches this code path (see the test above)"
    )
