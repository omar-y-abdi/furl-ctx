"""Library retrieval exports: retrieve(), resolve_markers(), CompressResult.ccr_hashes."""

from __future__ import annotations

import json

from furl_ctx import compress, resolve_markers, retrieve
from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.transforms.csv_ingest import raw_recovery_hash


def _envelope(n: int = 200) -> str:
    return json.dumps({"data": [{"id": i, "value": f"row-{i}"} for i in range(n)], "total": n})


def test_retrieve_unknown_hash_is_none() -> None:
    assert retrieve("0" * 24) is None


def test_ccr_hashes_empty_when_nothing_offloaded() -> None:
    result = compress([{"role": "tool", "content": "tiny output"}], model="gpt-4o")
    assert result.ccr_hashes == []


def test_marker_hash_retrieves_original_byte_exact() -> None:
    reset_compression_store()
    try:
        env = _envelope()
        result = compress([{"role": "tool", "content": env}], model="gpt-4o")
        h = raw_recovery_hash(env)
        assert h in result.ccr_hashes  # the property surfaces the shipped hash
        assert retrieve(h) == env  # byte-exact original via the exported retrieve
    finally:
        reset_compression_store()


def test_resolve_markers_expands_marker_to_original() -> None:
    reset_compression_store()
    try:
        env = _envelope()
        result = compress([{"role": "tool", "content": env}], model="gpt-4o")
        h = raw_recovery_hash(env)
        restored = resolve_markers(result.messages)
        text = "\n".join(m["content"] for m in restored if isinstance(m.get("content"), str))
        assert f"hash={h}" not in text  # marker expanded, not left dangling
        assert env in text  # original envelope content restored inline
        assert h in result.ccr_hashes  # input result unmutated (still scannable)
    finally:
        reset_compression_store()


def test_resolve_markers_provenance_is_namespace_scoped_characterization() -> None:
    """A marker minted under one namespace does NOT resolve under another.

    CHARACTERIZATION TEST, not a pin: this pins PRE-EXISTING namespace scoping
    that this PR did not change. The scoping logic here is identical on
    ``origin/main``; the PR's ``retrieve.py`` diff touches only the purge-cascade
    docstring/wiring and the Bug-12 ``TypeError``. It is kept because the
    behavior is worth locking down, but it must not be reported as evidence that
    a change in this PR works (RG8).

    Provenance is enforced by store membership + per-namespace isolation, not by
    marker format alone: ``resolve_markers`` only expands a ``<<ccr:HASH>>`` whose
    hash lives in the ACTIVE namespace store. A marker minted for tenant A left in
    text seen while tenant B is active stays a dangling marker (a window miss), so
    tenant B can never expand tenant A's original — even though the hash is a
    syntactically valid CCR hash.
    """
    reset_compression_store()
    try:
        env = _envelope()
        result = compress([{"role": "tool", "content": env}], model="gpt-4o", session_id="tenantA")

        # Same namespace → expands (control).
        under_a = resolve_markers([{**m} for m in result.messages], session_id="tenantA")
        text_a = "\n".join(m["content"] for m in under_a if isinstance(m.get("content"), str))
        assert env in text_a, "same-namespace resolution must expand the marker"

        # Different namespace → must NOT expand; the marker stays in place.
        under_b = resolve_markers([{**m} for m in result.messages], session_id="tenantB")
        text_b = "\n".join(m["content"] for m in under_b if isinstance(m.get("content"), str))
        assert env not in text_b, "cross-namespace resolution must NOT expand the marker"
        assert "ccr:" in text_b or "hash=" in text_b, "the unresolved marker must remain in place"
    finally:
        reset_compression_store()
