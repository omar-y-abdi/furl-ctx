"""Effective savings of the README ``code`` fixture AFTER a retrieval round trip.

The front-page ``code`` row reports a raw marker reduction near 99 percent, but
that content is an opaque whole-blob CCR offload: the bytes are in the store, not
gone. To USE the offloaded code an agent must retrieve the whole blob back, so
the context then holds the summary PLUS the full original. This script measures
that round trip on the committed README fixture (``benchmarks/data/code.raw.json``,
the same 7 real repo source files the front-page ``code`` row is built from) and
reports raw reduction against effective savings after retrieval.

Cost model is the verify harness's own ``effective_savings`` with a conservative
12 token per-call retrieval overhead, so the number is comparable to the
effective-savings table in BENCHMARKS.md. Whole-blob offload has no granular row
index, so any retrieval pays the entire payload back.

Run it::

    python -m benchmarks.code_roundtrip
"""

from __future__ import annotations

import json

from benchmarks.datasets import build_code_dataset
from furl_ctx import compress, retrieve
from verify.measure import (
    BENCH_MODEL,
    RETRIEVE_CALL_OVERHEAD_TOKENS,
    _tok,
    effective_savings,
)


def measure_code_roundtrip() -> dict[str, object]:
    """Compress the README code fixture, retrieve the offloaded blob, and
    compute raw vs effective savings. Returns a plain dict for reuse/tests."""
    dataset = build_code_dataset()
    result = compress(dataset.messages, model=BENCH_MODEL)

    tok = _tok()
    recovered: dict[str, str] = {}
    for ccr_hash in result.ccr_hashes:
        original = retrieve(ccr_hash)
        if original is not None:
            recovered[ccr_hash] = (
                original if isinstance(original, str) else json.dumps(original, ensure_ascii=False)
            )
    retrieved_tokens = sum(tok.count_text(blob) for blob in recovered.values())

    # Harness cost model at retrieval fractions {0, 25, 50} percent. Whole-blob
    # offload has no granular row index, so chunk_tokens is None and any nonzero
    # retrieval pays the entire payload back.
    n_offloaded_rows = len(dataset.items)
    eff = effective_savings(
        result.tokens_before,
        result.tokens_after,
        recovered,
        tok,
        n_dropped_rows=n_offloaded_rows,
        chunk_tokens=None,
    )

    raw_reduction = (
        (result.tokens_before - result.tokens_after) / result.tokens_before
        if result.tokens_before
        else 0.0
    )
    # One retrieval reality: to use ANY offloaded code you pull the whole blob once.
    effective_after_one = result.tokens_after + retrieved_tokens + RETRIEVE_CALL_OVERHEAD_TOKENS
    effective_one = (
        (result.tokens_before - effective_after_one) / result.tokens_before
        if result.tokens_before
        else 0.0
    )

    return {
        "model": BENCH_MODEL,
        "tokens_before": result.tokens_before,
        "tokens_after": result.tokens_after,
        "raw_reduction": raw_reduction,
        "transforms_applied": list(result.transforms_applied),
        "opaque_offloads": [
            {
                "hash": o.hash,
                "offloaded_tokens": o.offloaded_tokens,
                "preview_tokens": o.preview_tokens,
                "net_negative_on_retrieval": o.net_negative_on_retrieval,
            }
            for o in result.opaque_offloads
        ],
        "retrieved_tokens": retrieved_tokens,
        "effective_savings": eff,
        "effective_savings_one_retrieval": effective_one,
    }


def main() -> int:
    m = measure_code_roundtrip()
    print("=== README code fixture: opaque-offload round-trip economics ===")
    print(f"model:            {m['model']}")
    print(f"tokens_before:    {m['tokens_before']}")
    print(f"tokens_after:     {m['tokens_after']}")
    print(f"raw reduction:    {m['raw_reduction'] * 100:.1f}%")
    print(f"transforms:       {m['transforms_applied']}")
    print(f"opaque_offloads:  {m['opaque_offloads']}")
    print(f"retrieved tokens: {m['retrieved_tokens']}  (whole offloaded payload)")
    eff = m["effective_savings"]
    print("effective savings after retrieval (harness cost model, 12 tokens/call):")
    for rate in ("0", "25", "50"):
        print(f"  @{rate}% retrieval: {eff[rate] * 100:+.1f}%")
    print(
        f"one retrieval (summary + full original back): {m['effective_savings_one_retrieval'] * 100:+.1f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
