"""Unit tests for the furl_purge MCP tool (the data-erase escape hatch).

Drives the real ``_handle_purge`` handler against a real in-process CCR store,
asserting the structured JSON envelope an MCP host receives. Covers the
exactly-one-of(hash, all) validation boundary and invariant C: purge(hash)
removes retrievability of exactly that hash; purge(all) leaves the store empty;
neither runs without explicit args.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

import mcp.types as mt  # noqa: E402

from furl_ctx.cache.compression_store import reset_compression_store  # noqa: E402
from furl_ctx.ccr.mcp_server import FurlMCPServer  # noqa: E402

# Hook-safe split literal (no verbatim secret in source).
_API_KEY = "sk" + "-" + "abcdefghijklmnopqrstuvwx"

_HASH_A = "a" * 24
_HASH_B = "b" * 24
_HASH_ABSENT = "c" * 24


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("FURL_CCR_BACKEND", "memory")
    reset_compression_store()
    yield
    reset_compression_store()


@pytest.fixture
def server() -> FurlMCPServer:
    return FurlMCPServer()


def _envelope(result: list[mt.TextContent]) -> dict:
    assert len(result) == 1, f"expected one TextContent, got {result!r}"
    item = result[0]
    assert isinstance(item, mt.TextContent)
    return json.loads(item.text)


def _seed(server: FurlMCPServer, hash_key: str, content: str) -> None:
    server._get_local_store().store(original=content, compressed="c", explicit_hash=hash_key)


# ─── validation: exactly one of hash / all ─────────────────────────────────


async def test_purge_neither_arg_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({}))
    assert "error" in env
    assert "exactly one" in env["error"]


async def test_purge_both_args_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": _HASH_A, "all": True}))
    assert "error" in env
    assert "not both" in env["error"]


async def test_purge_all_must_be_boolean(server) -> None:
    env = _envelope(await server._handle_purge({"all": "yes"}))
    assert env["error"].startswith("all parameter must be a boolean")


async def test_purge_all_false_alone_is_neither(server) -> None:
    # all=false is not a wipe request and, with no hash, is the neither case.
    env = _envelope(await server._handle_purge({"all": False}))
    assert "error" in env and "exactly one" in env["error"]


async def test_purge_non_string_hash_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": 123}))
    assert env["error"].startswith("hash parameter must be a string")


async def test_purge_malformed_hash_is_an_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": "not-a-hash"}))
    assert "invalid hash format" in env["error"]


# ─── invariant C: purge(hash) removes exactly that hash ────────────────────


async def test_purge_hash_removes_retrievability_of_exactly_that_hash(server) -> None:
    _seed(server, _HASH_A, "erase me")
    _seed(server, _HASH_B, "keep me")

    purged = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert purged["purged"] == "hash"
    assert purged["deleted_count"] == 1
    assert purged["found"] is True

    # The purged hash is no longer retrievable — a clean, cause-honest miss.
    gone = _envelope(await server._handle_retrieve({"hash": _HASH_A}))
    assert "error" in gone
    assert gone["status"] == "missing"

    # The other hash is untouched.
    kept = _envelope(await server._handle_retrieve({"hash": _HASH_B}))
    assert kept["original_content"] == "keep me"


async def test_purge_absent_hash_is_clean_not_error(server) -> None:
    env = _envelope(await server._handle_purge({"hash": _HASH_ABSENT}))
    assert "error" not in env
    assert env["deleted_count"] == 0
    assert env["found"] is False


async def test_purge_hash_cascades_to_nested_offloaded_blobs(server) -> None:
    # An offloaded original (outer) whose compressed view references a nested
    # dropped-rows blob under another hash. Purging the outer must ALSO erase the
    # nested blob — else a "purged" secret survives under the nested hash (B3 /
    # audit non-cascading-purge; A1).
    nested = "d" * 24
    store = server._get_local_store()
    store.store(original=f"the dropped {_API_KEY} rows", compressed="x", explicit_hash=nested)
    store.store(
        original="full original payload",
        compressed=f"summary <<ccr:{nested}>> tail",
        explicit_hash=_HASH_A,
    )

    purged = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert purged["deleted_count"] == 2
    assert purged["nested_deleted"] == 1
    assert "nested" in purged["note"]

    # BOTH the outer and the nested blob are now unretrievable (verified by the
    # handler's read-back too).
    for h in (_HASH_A, nested):
        gone = _envelope(await server._handle_retrieve({"hash": h}))
        assert "error" in gone, f"{h} still retrievable after cascade purge"


async def test_purge_readback_detects_a_surviving_nested_blob(server) -> None:
    """RG6: read-back must verify the FULL set the cascade decided to delete.

    Before RG6 the read-back checked only ``store.exists(top_hash)``, so a nested
    blob that survived an incomplete cascade was invisible and the handler
    reported success. Simulate that by making ``delete`` a no-op for the nested
    hash only: the top goes, the nested does not, and the handler must say so
    loudly and NAME the survivor.
    """
    nested = "d" * 24
    store = server._get_local_store()
    store.store(original=f"the dropped {_API_KEY} rows", compressed="x", explicit_hash=nested)
    store.store(
        original="full original payload",
        compressed=f"summary <<ccr:{nested}>> tail",
        explicit_hash=_HASH_A,
    )

    real_delete = store.delete

    def _delete_except_nested(hash_key: str) -> bool:
        if hash_key == nested:
            return True  # claims success, erases nothing (an incomplete cascade)
        return real_delete(hash_key)

    store.delete = _delete_except_nested  # type: ignore[method-assign]
    envelope = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert "error" in envelope, "an incomplete cascade must not report success"
    assert nested in envelope["error"], "the error must NAME the surviving hash"
    assert "verification FAILED" in envelope["error"]


async def test_purge_readback_passes_on_a_complete_cascade(server) -> None:
    """RG6 control: the widened read-back must not false-positive a clean purge."""
    nested = "d" * 24
    store = server._get_local_store()
    store.store(original="dropped rows", compressed="x", explicit_hash=nested)
    store.store(
        original="payload",
        compressed=f"summary <<ccr:{nested}>> tail",
        explicit_hash=_HASH_A,
    )
    envelope = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert "error" not in envelope
    assert envelope["deleted_count"] == 2
    assert envelope["nested_deleted"] == 1


async def test_purge_keeps_a_nested_blob_another_live_entry_shares(server) -> None:
    """RG3 at the MCP surface: purging A must not break B's retrieval of shared C."""
    nested = "d" * 24
    store = server._get_local_store()
    store.store(original="shared dropped rows", compressed="x", explicit_hash=nested)
    store.store(original="A payload", compressed=f"A <<ccr:{nested}>>", explicit_hash=_HASH_A)
    store.store(original="B payload", compressed=f"B <<ccr:{nested}>>", explicit_hash=_HASH_B)

    envelope = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert "error" not in envelope, "skipping a shared blob is not a verification failure"
    assert envelope["nested_deleted"] == 0
    # B still resolves its marker; the shared blob was never the caller's to purge.
    still = _envelope(await server._handle_retrieve({"hash": nested}))
    assert "error" not in still, "shared nested blob was erased out from under B"


async def test_purge_is_cycle_safe_on_self_referencing_marker(server) -> None:
    # A compressed view whose marker points back at its OWN hash must not recurse
    # forever — the visited set bounds it, and the entry is still erased once.
    store = server._get_local_store()
    store.store(original="payload", compressed=f"self <<ccr:{_HASH_A}>>", explicit_hash=_HASH_A)
    purged = _envelope(await server._handle_purge({"hash": _HASH_A}))
    assert purged["deleted_count"] == 1
    assert purged["nested_deleted"] == 0


async def test_purge_all_empties_the_store(server) -> None:
    _seed(server, _HASH_A, "one")
    _seed(server, _HASH_B, "two")

    env = _envelope(await server._handle_purge({"all": True}))
    assert env["purged"] == "all"
    assert env["deleted_count"] == 2

    assert server._get_local_store().get_stats()["entry_count"] == 0
    # Every previously-stored hash now misses cleanly.
    for h in (_HASH_A, _HASH_B):
        gone = _envelope(await server._handle_retrieve({"hash": h}))
        assert "error" in gone


async def test_purge_hash_uppercase_is_normalized(server) -> None:
    # Store keys are lowercase; an upper/title-cased echo of a marker hash must
    # still purge (matches the retrieve ingress normalization).
    _seed(server, _HASH_A, "erase me")
    env = _envelope(await server._handle_purge({"hash": _HASH_A.upper()}))
    assert env["deleted_count"] == 1
    assert env["hash"] == _HASH_A  # normalized to lowercase
