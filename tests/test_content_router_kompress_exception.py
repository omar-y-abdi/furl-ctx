"""Regression tests for #4-upstream: real compressor bugs propagate loud at the
strategy-dispatch net in `_apply_strategy_to_content`.

Two-layer fix:
  Layer 1 (committed earlier): `_try_ml_compressor` narrows its catch to
      `_MODEL_UNAVAILABLE_ERRORS` so Kompress bugs propagate out.
  Layer 2 (this fix): `_apply_strategy_to_content` narrows its outer catch-all
      to the same `_MODEL_UNAVAILABLE_ERRORS`, so bugs that escaped Layer 1 are
      NOT re-swallowed by the strategy net.

Together they guarantee: for every strategy, a real compressor bug raises at the
caller boundary; a legitimate model-unavailable error degrades to passthrough.

Mutation-sensitive:
  - Reverting `_try_ml_compressor` to `except Exception` → test_kompress_bug_propagates fails.
  - Reverting `_apply_strategy_to_content` to `except Exception` →
    test_strategy_net_propagates_real_bug / test_all_strategies_propagate_real_bug fail.
  - Changing `_MODEL_UNAVAILABLE_ERRORS` check to unconditional passthrough →
    test_model_unavailable_degrades_to_passthrough still passes but the loud-fail
    tests would now incorrectly pass (wrong behavior), caught by the mutation-revert check.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from headroom.transforms.content_router import ContentRouter, CompressionStrategy
from headroom.transforms.kompress_compressor import KompressModelNotCached

_CONTENT = " ".join(f"word{i:02d}" for i in range(40))


def _router_with_kompress(raising_exc: BaseException | None) -> ContentRouter:
    """Router whose Kompress compressor raises `raising_exc` from compress()."""

    class _StubKompress:
        def compress(self, content, context="", question=None, target_ratio=None):
            if raising_exc is not None:
                raise raising_exc
            return SimpleNamespace(compressed=content, compressed_tokens=len(content.split()))

    router = ContentRouter()
    router._get_kompress = lambda *a, **kw: _StubKompress()  # type: ignore[method-assign]
    return router


# ---------------------------------------------------------------------------
# Layer 1: _try_ml_compressor boundary
# ---------------------------------------------------------------------------


def test_kompress_bug_propagates() -> None:
    """Layer-1: real bug exits _try_ml_compressor loud."""
    router = _router_with_kompress(TypeError("simulated model bug: bad tensor op"))
    with pytest.raises(TypeError, match="simulated model bug"):
        router._try_ml_compressor(_CONTENT, context="")


def test_model_unavailable_degrades_to_passthrough() -> None:
    """Layer-1: model-not-downloaded degrades gracefully in _try_ml_compressor."""
    router = _router_with_kompress(KompressModelNotCached("some/model"))
    compressed, tokens = router._try_ml_compressor(_CONTENT, context="")
    assert compressed == _CONTENT
    assert tokens == len(_CONTENT.split())


# ---------------------------------------------------------------------------
# Layer 2: _apply_strategy_to_content strategy-dispatch net
# ---------------------------------------------------------------------------


def _router_with_failing_strategy(strategy: CompressionStrategy, exc: BaseException) -> ContentRouter:
    """Router where the compressor for `strategy` raises `exc`."""
    router = ContentRouter()

    if strategy in (CompressionStrategy.KOMPRESS, CompressionStrategy.TEXT, CompressionStrategy.CODE_AWARE):
        # Stub _try_ml_compressor — the lowest-level ML path
        def _failing_ml(
            content: str, context: str, question: object = None, **kwargs: object
        ) -> tuple[str, int]:
            raise exc

        router._try_ml_compressor = _failing_ml  # type: ignore[method-assign]
    elif strategy == CompressionStrategy.SMART_CRUSHER:
        class _FailSmartCrusher:
            def crush(self, *a, **kw):
                raise exc

        router._get_smart_crusher = lambda: _FailSmartCrusher()  # type: ignore[method-assign]
    elif strategy == CompressionStrategy.LOG:
        class _FailLogCompressor:
            def compress(self, *a, **kw):
                raise exc

        router._get_log_compressor = lambda: _FailLogCompressor()  # type: ignore[method-assign]
    elif strategy == CompressionStrategy.DIFF:
        class _FailDiffCompressor:
            def compress(self, *a, **kw):
                raise exc

        router._get_diff_compressor = lambda: _FailDiffCompressor()  # type: ignore[method-assign]
    elif strategy == CompressionStrategy.HTML:
        class _FailHtmlExtractor:
            def extract(self, *a, **kw):
                raise exc

        router._get_html_extractor = lambda: _FailHtmlExtractor()  # type: ignore[method-assign]

    return router


def test_strategy_net_propagates_real_bug() -> None:
    """Layer-2: KOMPRESS strategy — real bug propagates out of _apply_strategy_to_content."""
    router = _router_with_failing_strategy(
        CompressionStrategy.KOMPRESS,
        TypeError("kernel bug inside kompress"),
    )
    with pytest.raises(TypeError, match="kernel bug inside kompress"):
        router._apply_strategy_to_content(_CONTENT, CompressionStrategy.KOMPRESS, context="")


def test_strategy_net_model_unavailable_passthrough() -> None:
    """Layer-2: model-unavailable still produces a graceful passthrough at the dispatch net."""
    router = _router_with_failing_strategy(
        CompressionStrategy.KOMPRESS,
        KompressModelNotCached("some/model"),
    )
    compressed, tokens, chain = router._apply_strategy_to_content(
        _CONTENT, CompressionStrategy.KOMPRESS, context=""
    )
    assert compressed == _CONTENT
    assert tokens == len(_CONTENT.split())
    # Chain always starts with the requested strategy
    assert chain[0] == CompressionStrategy.KOMPRESS.value


@pytest.mark.parametrize(
    "strategy",
    [
        CompressionStrategy.KOMPRESS,
        CompressionStrategy.TEXT,
        CompressionStrategy.CODE_AWARE,
        CompressionStrategy.SMART_CRUSHER,
        CompressionStrategy.LOG,
        CompressionStrategy.DIFF,
        CompressionStrategy.HTML,
    ],
)
def test_all_strategies_propagate_real_bug(strategy: CompressionStrategy) -> None:
    """Every strategy propagates a real bug (ValueError) out of the dispatch net.

    Mutation-sensitive: reverting the outer except back to `except Exception`
    makes this test pass (bug is swallowed) for all strategies.
    """
    router = _router_with_failing_strategy(strategy, ValueError(f"real bug in {strategy.value}"))

    # Ensure the compressor is enabled/available for the strategy being tested
    if strategy == CompressionStrategy.LOG:
        router.config.enable_log_compressor = True  # type: ignore[attr-defined]
    elif strategy == CompressionStrategy.HTML:
        router.config.enable_html_extractor = True  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match=f"real bug in {strategy.value}"):
        router._apply_strategy_to_content(_CONTENT, strategy, context="")
