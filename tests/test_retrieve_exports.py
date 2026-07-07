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
