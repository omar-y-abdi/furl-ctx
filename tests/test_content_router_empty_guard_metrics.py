"""Regression test for #11: empty-output guard reported phantom savings.

When a transform produced empty output from non-empty input, the router
restores the original content (passthrough) to avoid a provider 400. But it
left the routing_log decisions recording compressed_tokens=0 — so the derived
metrics (tokens_saved / compression_ratio / savings_percentage, all summed over
routing_log) reported a full "savings" for content that was NOT shrunk:
tokens_saved=N, ratio=0.0. Observers and debug logs saw phantom savings.

Fix: when the guard restores content, rewrite each routing_log decision to
passthrough (compressed_tokens == original_tokens) so every derived metric
honestly reports saved=0, ratio=1.0.

Compression-neutral (routing_log metrics only; the actual output was already
restored to the original by the pre-existing guard).
"""

from __future__ import annotations

from unittest.mock import patch

from furl_ctx.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    ContentRouterConfig,
    ContentType,
    RouterCompressionResult,
    RoutingDecision,
)

_CONTENT = "this is some non-empty content that should compress to something useful"


def _empty_output_result(content: str) -> RouterCompressionResult:
    # Simulates a transform that blanked out the input but recorded a
    # phantom-savings routing decision (compressed_tokens=0).
    n = len(content.split())
    return RouterCompressionResult(
        compressed="",
        original=content,
        strategy_used=CompressionStrategy.TEXT,
        routing_log=[RoutingDecision(ContentType.PLAIN_TEXT, CompressionStrategy.TEXT, n, 0)],
    )


def test_empty_guard_restores_content_and_reports_passthrough_metrics() -> None:
    router = ContentRouter(ContentRouterConfig())

    def fake_pure(self, c, strategy, context, question, bias=None, **kwargs):
        return _empty_output_result(c)

    with patch.object(ContentRouter, "_compress_pure", fake_pure):
        result = router.compress(_CONTENT)

    # The guard restores the original (passthrough output).
    assert result.compressed == _CONTENT
    # #11: metrics must reflect passthrough, not phantom savings.
    assert result.tokens_saved == 0, f"phantom savings: tokens_saved={result.tokens_saved}"
    assert result.compression_ratio == 1.0, f"phantom ratio: {result.compression_ratio}"
    assert result.savings_percentage == 0.0
    # routing_log decisions corrected to passthrough.
    for d in result.routing_log:
        assert d.compressed_tokens == d.original_tokens


def test_real_compression_metrics_unaffected() -> None:
    # Guard must not touch a genuine non-empty compression's metrics.
    router = ContentRouter(ContentRouterConfig())

    def fake_pure(self, c, strategy, context, question, bias=None, **kwargs):
        n = len(c.split())
        return RouterCompressionResult(
            compressed="shrunk",
            original=c,
            strategy_used=CompressionStrategy.TEXT,
            routing_log=[RoutingDecision(ContentType.PLAIN_TEXT, CompressionStrategy.TEXT, n, 1)],
        )

    with patch.object(ContentRouter, "_compress_pure", fake_pure):
        result = router.compress(_CONTENT)

    assert result.compressed == "shrunk"
    assert result.tokens_saved > 0, "real compression must still report savings"
    assert result.compression_ratio < 1.0
