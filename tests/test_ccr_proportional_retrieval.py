"""Whole-blob recovery + its honest effective-savings cost for a CCR row-drop.

Held-out audit finding (``verify/heldout/REPORT.md`` leniency #2): the engine
offloads ALL dropped rows of an array into ONE CCR blob under a single
``<<ccr:HASH>>`` pointer. A single retrieve returns the WHOLE blob, so the
moment the model needs even ONE dropped row it pays for the entire offloaded
payload — effective savings can go NEGATIVE (``logs@90 high``: ``+55.7% @25%
retrieval -> -10.3%`` worst case, i.e. MORE tokens than uncompressed).

A granular per-row offload — a ``_ccr_rows`` index of individually-addressable
per-row chunks — was once layered on top to make retrieval proportional. It
never worked on the path the MODEL reads: the ``HASH#rows`` index key fails
``is_valid_ccr_hash`` at the sole entry to ``furl_retrieve``, and the Python
store never mirrored the chunks, so a per-row chunk was never resolvable
through the production surface. It was removed (Design A, F8/#168): no
``_ccr_rows`` index is emitted and no per-row chunk is stored anywhere.

What remains — and what this test pins against the PRODUCTION store the model
actually reads (``get_compression_store().retrieve``, the store MCP
``furl_retrieve`` serves), NOT the crusher's Rust-side ``ccr_get`` handle the
model never queries — is the whole-blob contract:

* a row-drop's whole-blob parent recovers every dropped row BYTE-EXACT, and
* under whole-blob retrieval, pulling back even 25% of the rows pays for the
  ENTIRE blob, so effective savings go NEGATIVE. That cost is the honest one;
  the retired granular offload pretended to avoid it, and ``verify/measure.py``
  now charges it directly at eff@25 / eff@50.

Token counts use the same ``o200k_base`` (gpt-4o) tiktoken encoding the engine
uses. Retrieval cost is measured against the ACTUAL stored whole-blob payload
the production store serves — not an estimate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

import pytest

from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig

tiktoken = pytest.importorskip("tiktoken")
_ENC = tiktoken.get_encoding("o200k_base")


def _toks(text: str) -> int:
    return len(_ENC.encode(text))


def _high_entropy_logs(n: int, seed: int) -> list[dict]:
    """Deterministic, real-shaped, NEAR-UNIQUE log rows — the exact tier where
    the single-blob model collapsed (fresh uuid-ish id + random sha-ish commit +
    per-row service/level/message). Generated from a seeded SHA stream so it is
    reproducible without a committed fixture.
    """
    rows: list[dict] = []
    services = ["api", "worker", "scheduler", "auth", "billing", "ingest"]
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    for i in range(n):
        h = hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
        rows.append(
            {
                "id": h[:32],
                "commit": h[32:72] if len(h) >= 72 else (h + h)[32:72],
                "service": services[int(h[:2], 16) % len(services)],
                "level": levels[int(h[2:4], 16) % len(levels)],
                "latency_ms": int(h[4:8], 16) % 5000,
                "message": f"request {h[8:20]} handled in span {h[20:28]}",
            }
        )
    return rows


def _find_sentinel(node: object) -> dict | None:
    """Return the ``{"_ccr_dropped": ...}`` sentinel object from the parsed
    output tree, if present."""
    if isinstance(node, dict):
        if "_ccr_dropped" in node:
            return node
        for v in node.values():
            found = _find_sentinel(v)
            if found is not None:
                return found
    elif isinstance(node, list):
        for x in node:
            found = _find_sentinel(x)
            if found is not None:
                return found
    return None


def _sentinel_from_output(compressed: str) -> dict | None:
    """Locate the ``{"_ccr_dropped": ...}`` sentinel in a compressed output.
    Two renders are possible: a JSON array/object tree whose last element is the
    sentinel (the plain lossy row-drop path), or a JSON STRING wrapping a
    CSV-schema table whose LAST LINE is the sentinel (the survivor-compaction
    path).
    """
    tree = json.loads(compressed)
    found = _find_sentinel(tree)
    if found is not None:
        return found
    if isinstance(tree, str):
        last_line = tree.strip().rsplit("\n", 1)[-1]
        try:
            obj = json.loads(last_line)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(obj, dict) and "_ccr_dropped" in obj:
            return obj
    return None


def _hash_from_marker(marker: str) -> str:
    """Pull the KEY out of ``<<ccr:KEY <sep>...>>``."""
    start = marker.index("<<ccr:") + len("<<ccr:")
    rest = marker[start:]
    end = rest.index(" ")
    return rest[:end]


def _count_from_marker(marker: str, suffix: str) -> int:
    """Parse ``N`` out of ``<<ccr:KEY N_{suffix}>>`` (e.g. the dropped count
    from ``..._rows_offloaded``)."""
    m = re.search(rf" (\d+)_{re.escape(suffix)}>>", marker)
    assert m is not None, f"marker {marker!r} does not advertise a {suffix} count"
    return int(m.group(1))


def _effective_savings(*, raw_tokens: int, compressed_tokens: int, retrieved_tokens: int) -> float:
    """Effective savings = 1 - (compressed_on_wire + retrieved) / raw.

    Mirrors the auditor's model: the consumer pays for the compressed prompt
    PLUS whatever it pulls back via retrieval. Negative = the retrieval made the
    whole thing cost MORE than the uncompressed original.
    """
    if raw_tokens == 0:
        return 0.0
    return 1.0 - (compressed_tokens + retrieved_tokens) / raw_tokens


def _compress(items: list[dict]) -> dict:
    """Compress ``items`` and return ``{"compressed", "sentinel"}``. Resets the
    production compression store first so the whole-blob mirror lands in a
    known-empty store (the same store ``furl_retrieve`` reads)."""
    reset_compression_store()
    router = ContentRouter(ContentRouterConfig())
    result = router.compress(json.dumps(items, ensure_ascii=False))
    sentinel = _sentinel_from_output(result.compressed)
    assert sentinel is not None, "expected a lossy drop with a CCR sentinel"
    return {"compressed": result.compressed, "sentinel": sentinel}


def _recover_whole_blob(sentinel: dict) -> str:
    """Recover the whole-blob parent through the PRODUCTION store the model
    reads (``get_compression_store``) — the store MCP ``furl_retrieve`` serves,
    NOT the crusher's process-local Rust ``ccr_get`` handle the model never
    queries. Returns the stored canonical-JSON payload."""
    blob_hash = _hash_from_marker(sentinel["_ccr_dropped"])
    entry = get_compression_store().retrieve(blob_hash)
    assert entry is not None, "whole-blob must resolve from the PRODUCTION store"
    return entry.original_content


def test_row_drop_whole_blob_recovers_byte_exact() -> None:
    """A high-entropy row-drop's whole-blob parent recovers every dropped row
    BYTE-EXACT through the production store, and the output advertises NO
    granular ``_ccr_rows`` index (the retired per-row offload)."""
    items = _high_entropy_logs(90, seed=2000)
    out = _compress(items)
    sentinel = out["sentinel"]

    assert "_ccr_rows" not in sentinel, "the retired granular row index must not be advertised"

    # Byte-exact recovery, not mere presence: a mutation that corrupts a row but
    # keeps the payload non-None must fail this content equality.
    assert json.loads(_recover_whole_blob(sentinel)) == items, (
        "whole-blob must recover the original rows exactly"
    )

    n_dropped = _count_from_marker(sentinel["_ccr_dropped"], "rows_offloaded")
    assert 0 < n_dropped < len(items), "a lossy drop must keep a visible sample"


@pytest.mark.parametrize("retrieval_fraction", [0.25, 0.50])
def test_whole_blob_retrieval_cost_goes_negative(retrieval_fraction: float) -> None:
    """Honest cost model (audit reproduction): under whole-blob retrieval,
    pulling back even 25% of the dropped rows pays for the ENTIRE offloaded
    blob, so effective savings go NEGATIVE — the compressed prompt plus the
    retrieved blob costs MORE than the uncompressed original.

    The retired granular offload pretended to avoid this; with it gone the
    number is reported, not hidden. ``verify/measure.py`` charges this same
    whole-blob cost at eff@25 / eff@50.
    """
    items = _high_entropy_logs(90, seed=2000)
    raw_tokens = _toks(json.dumps(items, ensure_ascii=False))
    out = _compress(items)
    compressed_tokens = _toks(out["compressed"])

    blob_tokens = _toks(_recover_whole_blob(out["sentinel"]))
    k = math.ceil(retrieval_fraction * len(items))
    eff_whole = _effective_savings(
        raw_tokens=raw_tokens,
        compressed_tokens=compressed_tokens,
        retrieved_tokens=blob_tokens if k > 0 else 0,  # any retrieval = the full blob
    )
    assert eff_whole < 0.0, (
        f"whole-blob effective savings must go negative at {retrieval_fraction:.0%} "
        f"retrieval (audit reproduction); got {eff_whole:+.1%}"
    )


def test_oversized_drop_recovers_whole_blob() -> None:
    """A large array's row-drop recovers byte-exact from the whole-blob parent
    through the production store and advertises no granular index. (Pre-Design-A
    a drop this size skipped granular chunking to avoid a store flood; now no
    size ever chunks.)"""
    items = _high_entropy_logs(1100, seed=2001)
    out = _compress(items)
    assert "_ccr_rows" not in out["sentinel"], "no granular row index is ever advertised"
    assert json.loads(_recover_whole_blob(out["sentinel"])) == items, (
        "whole-blob must recover the original rows byte-exactly"
    )
