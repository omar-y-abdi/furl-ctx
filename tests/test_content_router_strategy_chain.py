"""Regression test for #13: strategy_chain double-appended a fallback entry.

When SmartCrusher returned no savings, two code paths each appended the same
fallback strategy and ran it twice (duplicate CCR side effects). The fix
made the generic post-dispatch fallback the single owner: each strategy now
appears at most once in the chain.

Post-excision (ML text compressor removed) the no-savings fallback for
SMART_CRUSHER goes straight to LOG, so the pinned chain is
``['smart_crusher', 'log']``.

Compression-neutral: the final compressed bytes and strategy_used are unchanged
(asserted below).
"""

from __future__ import annotations

from furl_ctx.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    ContentRouterConfig,
)


def _router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig())


def test_strategy_chain_has_no_duplicates() -> None:
    res = _router().compress("[1,2,3]")
    chain = res.strategy_chain
    # Each strategy appears at most once (the #13 invariant).
    assert len(chain) == len(set(chain)), f"strategy_chain has duplicates: {chain}"
    # Pin the exact chain (smart_crusher tried, then the log fallback — once).
    assert chain == ["smart_crusher", "log"]


def test_compression_output_unchanged_by_dedup() -> None:
    # Neutrality proof: the dedup must not change WHAT gets produced — same
    # bytes, same winning strategy. (A tiny array like [1,2,3] is below the
    # savings threshold, so it passes through as SMART_CRUSHER.)
    res = _router().compress("[1,2,3]")
    assert res.compressed == "[1,2,3]"
    assert res.strategy_used is CompressionStrategy.SMART_CRUSHER
