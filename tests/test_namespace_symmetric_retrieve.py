"""retrieve/purge/resolve_markers resolve the SAME namespace store compress binds.

Round-5 follow-up F1 (pre-existing since #55). ``compress()`` binds the resolved
namespace store (``FURL_CCR_PROJECT_DIR`` / ``FURL_CCR_NAMESPACE`` / explicit
``session_id``/``agent_id``) for the duration of the call, so offloaded originals
land in the isolated per-namespace store. The library read-side — ``retrieve``,
``purge``, ``resolve_markers`` — called ``get_compression_store()`` with NO
namespace resolution, reading the GLOBAL store instead: with a namespace active,
every retrieve was a guaranteed miss, every purge a no-op, every marker left
unexpanded, under BOTH the sqlite and in-memory backends.

The fix routes all three through ``_active_ccr_store`` — the exact resolution
seam ``compress`` (via ``resolve_ccr_namespace_store``) and ``ccr_export`` /
``ccr_import`` already share: namespace store when one is active, else the
request-scoped/global ``get_compression_store()`` (the no-namespace default path
is byte-identical).
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache.compression_store import reset_compression_store

_MODEL = "claude-sonnet-4-5-20250929"

# Plain text (byte-exact round-trip; JSON gets whitespace-canonicalized): 400
# distinct lines force at least one CCR offload past the token floor.
_PAYLOAD = "\n".join(f"line {i} padding-token-{i} more-filler-{i}" for i in range(1, 401))


@pytest.fixture(autouse=True)
def _clean_namespace_env(tmp_path, monkeypatch):
    """Sandbox the workspace and clear every namespace/backend knob, with a fresh
    store singleton (and namespace registry) around each test."""
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("FURL_CCR_BACKEND", raising=False)
    monkeypatch.delenv("FURL_CCR_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FURL_CCR_NAMESPACE", raising=False)
    reset_compression_store()
    yield
    reset_compression_store()


def _compress_payload() -> tuple[str, list[dict[str, str]]]:
    """Compress the payload in-process; return (ccr_hash, result messages)."""
    from furl_ctx import compress

    result = compress([{"role": "tool", "content": _PAYLOAD}], model=_MODEL)
    assert result.ccr_hashes, "payload must offload at least one retrievable CCR hash"
    return result.ccr_hashes[0], result.messages


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_retrieve_hits_under_project_namespace(backend, tmp_path, monkeypatch) -> None:
    """R0 pinning test: with FURL_CCR_PROJECT_DIR active, compress -> retrieve
    HITS in the same process (pre-fix: guaranteed miss under both backends)."""
    monkeypatch.setenv("FURL_CCR_BACKEND", backend)
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))
    from furl_ctx import retrieve

    hash_key, _ = _compress_payload()

    assert retrieve(hash_key) == _PAYLOAD, (
        "retrieve must resolve the same namespace store compress wrote to"
    )


@pytest.mark.parametrize("backend", ["sqlite", "memory"])
def test_purge_deletes_under_project_namespace(backend, tmp_path, monkeypatch) -> None:
    """purge acts on the namespace store: True on the live entry, then the entry
    is gone, and a second purge is False (pre-fix: always False, entry survived)."""
    monkeypatch.setenv("FURL_CCR_BACKEND", backend)
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))
    from furl_ctx import purge, retrieve

    hash_key, _ = _compress_payload()

    assert purge(hash_key) is True, "purge must find the entry compress just stored"
    assert retrieve(hash_key) is None, "purge must actually remove the entry"
    assert purge(hash_key) is False, "a second purge of the same hash is a miss"


def test_resolve_markers_expands_under_project_namespace(tmp_path, monkeypatch) -> None:
    """resolve_markers expands <<ccr:HASH>> markers stored in the namespace store
    (pre-fix: markers stayed in place — unresolvable window miss)."""
    monkeypatch.setenv("FURL_CCR_BACKEND", "sqlite")
    monkeypatch.setenv("FURL_CCR_PROJECT_DIR", str(tmp_path / "proj"))
    from furl_ctx import resolve_markers

    hash_key, messages = _compress_payload()
    assert f"<<ccr:{hash_key}>>" in json.dumps(messages), "precondition: marker present"

    resolved = resolve_markers(messages)

    assert f"<<ccr:{hash_key}>>" not in json.dumps(resolved), "marker must expand"
    assert any(_PAYLOAD.splitlines()[0] in m.get("content", "") for m in resolved)


def test_session_and_agent_ids_mirror_compress(tmp_path, monkeypatch) -> None:
    """Explicit-tenant symmetry: compress(session_id, agent_id) stores in that
    tenant's store; retrieve/purge with the SAME ids resolve it, while the
    un-namespaced call still misses (tenant isolation preserved by design)."""
    from furl_ctx import compress, purge, retrieve

    result = compress(
        [{"role": "tool", "content": _PAYLOAD}],
        model=_MODEL,
        session_id="tenant-s1",
        agent_id="tenant-a1",
    )
    assert result.ccr_hashes
    hash_key = result.ccr_hashes[0]

    # Wrong tenant (no ids, no env namespace) → global store → loud miss.
    assert retrieve(hash_key) is None
    # Same tenant → hit, byte-exact.
    assert retrieve(hash_key, session_id="tenant-s1", agent_id="tenant-a1") == _PAYLOAD
    # purge honors the same seam.
    assert purge(hash_key) is False
    assert purge(hash_key, session_id="tenant-s1", agent_id="tenant-a1") is True


def test_no_namespace_default_path_unchanged() -> None:
    """Control: with NO namespace active, the trio behaves exactly as today —
    compress -> retrieve hit, marker expansion, purge round-trip on the global
    store. Must pass both pre- and post-fix (pins the byte-identical default)."""
    from furl_ctx import purge, resolve_markers, retrieve
    from furl_ctx.cache.compression_store import get_compression_store

    hash_key, messages = _compress_payload()

    assert retrieve(hash_key) == _PAYLOAD
    resolved = resolve_markers(messages)
    assert f"<<ccr:{hash_key}>>" not in json.dumps(resolved)
    # The entry lives in the plain global store — the exact object
    # get_compression_store() resolves with no namespace env.
    assert get_compression_store().exists(hash_key)
    assert purge(hash_key) is True
