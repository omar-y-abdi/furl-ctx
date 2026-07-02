"""Effective-savings-UNDER-RETRIEVAL for the granular CCR offload.

Held-out audit finding (``verify/heldout/REPORT.md`` leniency #2): the
engine used to offload ALL dropped rows of an array into ONE CCR blob
under a single ``<<ccr:HASH>>`` pointer. A single retrieve returns the
WHOLE blob, so the moment the model needs even ONE dropped row it pays
for the entire offloaded payload — effective savings can go NEGATIVE
(``logs@90 high``: ``+55.7% @25% retrieval -> -10.3%`` worst case, i.e.
MORE tokens than uncompressed).

The granular model fixes this: every DROPPED row is also stored as its
own individually-addressable chunk (``ccr_get(row_hash)`` returns exactly
``[row]``), addressed through a per-blob row index. Retrieving one needed
row now costs ONE row, not the whole blob — so effective savings stay
PROPORTIONAL to what is actually retrieved.

Two boundaries of the granular contract (COR-4 / COR-20):

* only DROPPED rows are chunked — kept rows are visible in the output and
  are never written to the store, so one document's persists cannot flood
  the bounded store and evict blobs its own markers still reference; and
* when the dropped count exceeds the store's granular chunk budget
  (``capacity / 4``), chunking is skipped entirely (no ``_ccr_rows``
  index) and the whole-blob remains the sole — still byte-exact —
  recovery key, so a huge array can never self-evict its own chunks.

This test reproduces the audit's effective-savings model on a real-shaped,
deterministically-generated high-entropy logs array (no committed fixture,
no synthetic benchmark file) and asserts:

* the OLD whole-blob retrieval model goes NEGATIVE at >=25% retrieval
  (reproducing the audit), AND
* the NEW granular model stays POSITIVE (and well above the whole-blob
  model) at every retrieval fraction in ``{0, 25, 50}%``.

Token counts use the same ``o200k_base`` (gpt-4o) tiktoken encoding the
engine uses. Retrieval cost is measured against the ACTUAL stored
payloads (whole-blob vs per-row chunks) pulled back through the engine's
own ``ccr_get`` surface — not an estimate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

import pytest

from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig

tiktoken = pytest.importorskip("tiktoken")
_ENC = tiktoken.get_encoding("o200k_base")


def _toks(text: str) -> int:
    return len(_ENC.encode(text))


def _high_entropy_logs(n: int, seed: int) -> list[dict]:
    """Deterministic, real-shaped, NEAR-UNIQUE log rows — the exact tier
    where the single-blob model collapsed (fresh uuid-ish id + random
    sha-ish commit + per-row service/level/message). Generated from a
    seeded SHA stream so it is reproducible without a committed fixture.
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
    """Return the ``{"_ccr_dropped": ..., "_ccr_rows": ...}`` sentinel
    object from the parsed output tree, if present."""
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


def _hash_from_marker(marker: str) -> str:
    """Pull the key out of ``<<ccr:KEY <sep>...>>`` (KEY may carry a
    ``#rows`` suffix for the granular index)."""
    start = marker.index("<<ccr:") + len("<<ccr:")
    rest = marker[start:]
    end = rest.index(" ")
    return rest[:end]


def _count_from_marker(marker: str, suffix: str) -> int:
    """Parse ``N`` out of ``<<ccr:KEY N_{suffix}>>`` (e.g. the dropped
    count from ``..._rows_offloaded`` or the chunk count from
    ``..._chunks``)."""
    m = re.search(rf" (\d+)_{re.escape(suffix)}>>", marker)
    assert m is not None, f"marker {marker!r} does not advertise a {suffix} count"
    return int(m.group(1))


def _is_ordered_subsequence(sub: list, full: list) -> bool:
    """True iff ``sub``'s elements appear in ``full`` in the same relative
    order (byte-exact equality, two-pointer walk)."""
    pos = 0
    for row in sub:
        while pos < len(full) and full[pos] != row:
            pos += 1
        if pos == len(full):
            return False
        pos += 1
    return True


def _sentinel_from_output(compressed: str) -> dict | None:
    """Locate the ``{"_ccr_dropped": ...}`` sentinel in a compressed
    output. Two renders are possible:

    * a JSON array/object tree whose last element is the sentinel object
      (the plain lossy row-drop path), or
    * a JSON STRING wrapping a CSV-schema table whose LAST LINE is the
      sentinel object (the survivor-compaction path).
    """
    tree = json.loads(compressed)
    found = _find_sentinel(tree)
    if found is not None:
        return found
    # Survivor-compaction: the payload is a string; the sentinel is its
    # final newline-delimited line.
    if isinstance(tree, str):
        last_line = tree.strip().rsplit("\n", 1)[-1]
        try:
            obj = json.loads(last_line)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(obj, dict) and "_ccr_dropped" in obj:
            return obj
    return None


def _compress(items: list[dict]) -> tuple[str, object, dict]:
    reset_compression_store()
    router = ContentRouter(ContentRouterConfig())
    result = router.compress(json.dumps(items, ensure_ascii=False))
    crusher = router._get_smart_crusher()
    sentinel = _sentinel_from_output(result.compressed)
    assert sentinel is not None, "expected a lossy drop with a CCR sentinel"
    return result.compressed, crusher, sentinel


def _effective_savings(
    *,
    raw_tokens: int,
    compressed_tokens: int,
    retrieved_tokens: int,
) -> float:
    """Effective savings = 1 - (compressed_on_wire + retrieved) / raw.

    Mirrors the auditor's model: the consumer pays for the compressed
    prompt PLUS whatever it pulls back via retrieval. Negative = the
    retrieval made the whole thing cost MORE than the uncompressed
    original.
    """
    if raw_tokens == 0:
        return 0.0
    return 1.0 - (compressed_tokens + retrieved_tokens) / raw_tokens


@pytest.mark.parametrize("retrieval_fraction", [0.0, 0.25, 0.50])
def test_granular_retrieval_stays_positive(retrieval_fraction: float) -> None:
    items = _high_entropy_logs(90, seed=2000)
    raw = json.dumps(items, ensure_ascii=False)
    raw_tokens = _toks(raw)

    compressed, crusher, sentinel = _compress(items)
    compressed_tokens = _toks(compressed)

    # ── Whole-blob retrieval cost (OLD model) ──
    # A single `<<ccr:HASH>>` retrieve returns the WHOLE offloaded blob,
    # so ANY non-zero retrieval pays for the entire payload.
    blob_hash = _hash_from_marker(sentinel["_ccr_dropped"])
    blob_payload = crusher.ccr_get(blob_hash)
    assert blob_payload is not None, "whole-blob must resolve"
    # Byte-exact recovery, not mere presence: the whole blob must round-trip to
    # the ORIGINAL rows. A mutation that corrupts a row but keeps the payload
    # non-None passes `is not None` — it must fail this content equality.
    assert json.loads(blob_payload) == items, "whole-blob must recover the original rows exactly"
    blob_tokens = _toks(blob_payload)

    # ── Granular retrieval cost (NEW model) ──
    # The `_ccr_rows` marker names a per-blob row index → per-row chunks.
    # Retrieving k rows costs only those k rows.
    assert "_ccr_rows" in sentinel, "granular model must surface a row index"
    index_key = _hash_from_marker(sentinel["_ccr_rows"])
    assert index_key.endswith("#rows")
    index_raw = crusher.ccr_get(index_key)
    assert index_raw is not None, "row index must resolve"
    row_hashes = json.loads(index_raw)
    # Dropped-rows-only persist (COR-4): the index holds one chunk per
    # DROPPED row — kept rows stay visible in the output and are never
    # written to the store. Cross-check the count against the INDEPENDENT
    # source: the dropped count the `_ccr_dropped` marker advertises.
    n_dropped = _count_from_marker(sentinel["_ccr_dropped"], "rows_offloaded")
    assert 0 < n_dropped < len(items), "a lossy drop must keep a visible sample"
    assert len(row_hashes) == n_dropped, "one chunk per DROPPED row (kept rows never chunked)"
    # COR-20: the `_ccr_rows` marker advertises EXACTLY the number of
    # chunks the index holds — no model-visible lie.
    n_chunks = _count_from_marker(sentinel["_ccr_rows"], "chunks")
    assert n_chunks == len(row_hashes), (
        f"marker advertises {n_chunks} chunks but the index holds {len(row_hashes)}"
    )
    # The granular contract is not just "a chunk exists per dropped row" —
    # each chunk must resolve to its OWN single original row, byte-exact,
    # preserving the original relative order. This catches a corrupted or
    # mis-indexed chunk that the count check and `is not None` would miss.
    reconstructed = [json.loads(crusher.ccr_get(rh))[0] for rh in row_hashes]
    assert _is_ordered_subsequence(reconstructed, items), (
        "per-row chunks must recover their original rows exactly, in original order"
    )

    # Number of rows the model needs to pull back.
    k = math.ceil(retrieval_fraction * len(items))

    # Whole-blob: any k>0 pays the full blob; k==0 pays nothing.
    whole_blob_retrieved = blob_tokens if k > 0 else 0

    # Granular: pay only for the k retrieved per-row chunks (worst-case
    # the k largest rows). Each chunk is `[row]`; we strip the 2-char
    # array brackets so we are not double-charging the wrapper, matching
    # how the rows would be served back inline.
    chunk_tokens = sorted(
        (_toks(crusher.ccr_get(rh) or "[]") for rh in row_hashes),
        reverse=True,
    )
    granular_retrieved = sum(chunk_tokens[:k])

    eff_whole = _effective_savings(
        raw_tokens=raw_tokens,
        compressed_tokens=compressed_tokens,
        retrieved_tokens=whole_blob_retrieved,
    )
    eff_granular = _effective_savings(
        raw_tokens=raw_tokens,
        compressed_tokens=compressed_tokens,
        retrieved_tokens=granular_retrieved,
    )

    print(
        f"\nretrieval={retrieval_fraction:.0%} k={k}/{len(items)} "
        f"raw={raw_tokens} compressed={compressed_tokens} "
        f"blob={blob_tokens} granular_retrieved={granular_retrieved} "
        f"| eff_whole_blob={eff_whole:+.1%} eff_granular={eff_granular:+.1%}"
    )

    # The granular model never costs MORE than the whole-blob model.
    assert eff_granular >= eff_whole - 1e-9

    # The granular model stays POSITIVE at every retrieval fraction —
    # the audit's negative-savings failure no longer occurs.
    assert eff_granular > 0.0, (
        f"granular effective savings went non-positive ({eff_granular:+.1%}) "
        f"at {retrieval_fraction:.0%} retrieval"
    )


def test_whole_blob_model_reproduces_audit_negative() -> None:
    """Sanity anchor: confirm the OLD whole-blob model DOES collapse on
    this tier (so the granular win above is real, not a tier that never
    had the problem). At >=25% retrieval the whole-blob effective savings
    must be far below the granular savings."""
    items = _high_entropy_logs(90, seed=2000)
    raw_tokens = _toks(json.dumps(items, ensure_ascii=False))
    compressed, crusher, sentinel = _compress(items)
    compressed_tokens = _toks(compressed)

    blob_tokens = _toks(crusher.ccr_get(_hash_from_marker(sentinel["_ccr_dropped"])))
    index_raw = crusher.ccr_get(_hash_from_marker(sentinel["_ccr_rows"]))
    row_hashes = json.loads(index_raw)

    k = math.ceil(0.25 * len(items))
    eff_whole = _effective_savings(
        raw_tokens=raw_tokens,
        compressed_tokens=compressed_tokens,
        retrieved_tokens=blob_tokens,  # any retrieval = full blob
    )
    chunk_tokens = sorted((_toks(crusher.ccr_get(rh) or "[]") for rh in row_hashes), reverse=True)
    eff_granular = _effective_savings(
        raw_tokens=raw_tokens,
        compressed_tokens=compressed_tokens,
        retrieved_tokens=sum(chunk_tokens[:k]),
    )
    # The headline premise of this anchor test (and the audit it reproduces):
    # under whole-blob retrieval, pulling back even 25% of the rows pays for the
    # ENTIRE offloaded blob, so effective savings go NEGATIVE — the compressed
    # prompt plus the retrieved blob costs MORE than the uncompressed original.
    # Pin the SIGN directly; the relative-gap assertion below cannot catch a
    # regression that lifted whole-blob back to positive while still trailing
    # granular.
    assert eff_whole < 0.0, (
        f"whole-blob effective savings must go negative at 25% retrieval "
        f"(audit reproduction); got {eff_whole:+.1%}"
    )
    # Granular must be strictly, materially better than whole-blob here.
    assert eff_granular > eff_whole + 0.10


def test_oversized_array_skips_granular_index_but_recovers_whole_blob() -> None:
    """COR-4 store-flood gate: an array whose DROPPED count exceeds the
    store's granular chunk budget (``capacity / 4`` = 250 on the default
    1000-entry store) skips per-row chunking entirely — no ``_ccr_rows``
    index is advertised. This is what keeps one document's markers
    honest: a huge array's chunk flood used to evict its OWN earliest
    chunks (and a second array's flood evicted the FIRST array's
    whole-blob), leaving surfaced ``<<ccr:HASH>>`` pointers that resolved
    to nothing. Proportional retrieval intentionally degrades to
    whole-blob retrieval here; recovery does not degrade at all."""
    items = _high_entropy_logs(1100, seed=2001)
    _compressed, crusher, sentinel = _compress(items)

    # No granular index for oversized drops — the corrected contract.
    assert "_ccr_rows" not in sentinel, (
        "an oversized drop must not advertise a granular row index; the chunk "
        "flood would evict entries this document's own markers reference"
    )

    # The whole-blob pointer remains and recovers byte-exactly.
    blob_hash = _hash_from_marker(sentinel["_ccr_dropped"])
    blob_payload = crusher.ccr_get(blob_hash)
    assert blob_payload is not None, "whole-blob must resolve for oversized drops"
    assert json.loads(blob_payload) == items, (
        "whole-blob must recover the original rows byte-exactly"
    )
