"""Regression test for #13: strategy_chain double-appended 'kompress'.

When SmartCrusher returned no savings, two code paths each appended KOMPRESS and
each ran the ML compressor: an inner SMART_CRUSHER-only fallback AND the generic
post-dispatch fallback (which already lists SMART_CRUSHER as fallback-eligible).
Result: `compress('[1,2,3]')` produced strategy_chain
``['smart_crusher','kompress','kompress','log']`` and ran Kompress twice (a
duplicate _try_kompress -> duplicate CCR/TOIN side effects).

Fix: removed the inner duplicate; the generic fallback handles the KOMPRESS
(then LOG) fallback exactly once. Each strategy now appears at most once.

Compression-neutral: the final compressed bytes and strategy_used are unchanged
(asserted below) — only the redundant second run and its chain entry are gone.
"""
from __future__ import annotations

from headroom.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    ContentRouterConfig,
)


def _router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig())


def test_strategy_chain_has_no_duplicate_kompress() -> None:
    res = _router().compress("[1,2,3]")
    chain = res.strategy_chain
    # Each strategy appears at most once (the #13 invariant).
    assert len(chain) == len(set(chain)), f"strategy_chain has duplicates: {chain}"
    assert chain.count("kompress") <= 1, f"'kompress' double-appended: {chain}"
    # Pin the exact fixed chain (smart_crusher tried, then kompress, then log
    # fallback — each once).
    assert chain == ["smart_crusher", "kompress", "log"]


def test_compression_output_unchanged_by_dedup() -> None:
    # Neutrality proof: the dedup must not change WHAT gets produced — same
    # bytes, same winning strategy. (A tiny array like [1,2,3] is below the
    # savings threshold, so it passes through as SMART_CRUSHER.)
    res = _router().compress("[1,2,3]")
    assert res.compressed == "[1,2,3]"
    assert res.strategy_used is CompressionStrategy.SMART_CRUSHER
