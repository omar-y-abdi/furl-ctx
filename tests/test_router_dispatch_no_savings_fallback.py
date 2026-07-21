"""Pin the SMART_CRUSHER no-savings fallback chain's safety invariants.

``StrategyDispatcher.apply`` (``router_dispatch.py``) runs a post-dispatch
fallback whenever SMART_CRUSHER reports no savings: revert an expanded result,
then try the LOG compressor as a last resort and adopt it only if it is
actually smaller. Before this file, none of that safety net had direct
coverage — confirmed by deliberately breaking each branch in turn and
re-running the full suite, which stayed green throughout:

* disabling the "never ship an EXPANDED result" revert (the whole
  ``if compressed_tokens > original_tokens`` arm) — 2597 passed, 0 failed;
* disabling the "adopt the LOG fallback only if smaller" comparison
  (``if log_compressed_tokens < compressed_tokens``) — 2597 passed, 0 failed;
* forcing LOG-fallback adoption unconditionally regardless of size —
  2597 passed, 0 failed;
* removing the ``try/except`` around ``log_compressor.compress`` so a raise
  propagates instead of failing open — 2597 passed, 0 failed.

Each test below reproduces its own exact planted break as a RED proof:
temporarily re-apply the described one-line change to ``router_dispatch.py``,
rerun this file, see the corresponding test (and only that one) fail, then
restore. This includes a boundary case the "not smaller" test alone would
miss: widening the adoption comparison from strict ``<`` to ``<=`` also
stays undetected without a dedicated exactly-equal-size case.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from furl_ctx.transforms.content_router import ContentRouterConfig
from furl_ctx.transforms.router_dispatch import StrategyDispatcher
from furl_ctx.transforms.router_policy import CompressionStrategy

# Plain prose: no commas (won't sniff as CSV) and not JSON-shaped (won't sniff
# as an envelope), so `apply` takes the direct `crusher.crush(...)` path this
# module's own docstring calls out as the ordinary SMART_CRUSHER arm — the
# same content shape `test_router_token_counter_units.py` already relies on
# for that same guarantee.
CONTENT = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 8


def _dispatcher(debug_events: list[dict] | None = None) -> StrategyDispatcher:
    logger = logging.getLogger("test.router_dispatch.no_savings_fallback")
    logger.setLevel(logging.DEBUG)

    def _capture(event: str, **kwargs: object) -> None:
        if debug_events is not None:
            debug_events.append({"event": event, **kwargs})

    return StrategyDispatcher(
        ContentRouterConfig(),
        logger=logger,
        log_router_debug=_capture,
        json_shape=lambda content: {},
    )


def _apply(dispatcher, *, crusher, log_compressor, token_counter=len, debug_events=None):
    return dispatcher.apply(
        CONTENT,
        CompressionStrategy.SMART_CRUSHER,
        "",
        get_smart_crusher=lambda: crusher,
        get_search_compressor=lambda: None,
        get_log_compressor=lambda: log_compressor,
        get_diff_compressor=lambda: None,
        get_text_crusher=lambda: None,
        get_code_aware_compressor=lambda: None,
        token_counter=token_counter,
    )


def _reason(debug_events: list[dict]) -> str:
    (event,) = [e for e in debug_events if e["event"] == "content_router_strategy_result"]
    return event["reason"]


def test_expanded_result_is_reverted_to_the_original() -> None:
    """RED proof: comment out the ``if compressed_tokens > original_tokens``
    arm's body (or replace its condition with ``if False``) in
    ``router_dispatch.py`` and this test fails — an expanded "compressed"
    result ships through unchanged instead of being reverted."""
    debug_events: list[dict] = []
    crusher = SimpleNamespace(
        crush=lambda content, query="", bias=1.0: SimpleNamespace(
            compressed=content + " " * 50 + "PADDING MAKES THIS LONGER THAN THE ORIGINAL"
        )
    )

    compressed, tokens, chain = _apply(
        _dispatcher(debug_events),
        crusher=crusher,
        log_compressor=None,  # the revert must fire without ever consulting the log fallback
        debug_events=debug_events,
    )

    assert compressed == CONTENT
    assert tokens == len(CONTENT)
    assert chain == [CompressionStrategy.SMART_CRUSHER.value, CompressionStrategy.PASSTHROUGH.value]
    assert _reason(debug_events).endswith("_no_savings_passthrough")


def test_log_fallback_is_adopted_when_smaller() -> None:
    """RED proof: replace ``if log_compressed_tokens < compressed_tokens``
    with ``if False`` and this test fails — a genuinely smaller LOG fallback
    is never adopted, so the router silently ships the larger no-savings
    SMART_CRUSHER output instead of the win available to it."""
    debug_events: list[dict] = []
    crusher = SimpleNamespace(
        crush=lambda content, query="", bias=1.0: SimpleNamespace(compressed=content)
    )
    log_compressor = SimpleNamespace(
        compress=lambda content, bias=1.0: SimpleNamespace(compressed=content[:20])
    )

    compressed, tokens, chain = _apply(
        _dispatcher(debug_events),
        crusher=crusher,
        log_compressor=log_compressor,
        debug_events=debug_events,
    )

    assert compressed == CONTENT[:20]
    assert tokens == 20
    assert chain == [CompressionStrategy.SMART_CRUSHER.value, CompressionStrategy.LOG.value]
    assert _reason(debug_events).endswith("_fallback_log_after_no_savings")


def test_log_fallback_is_not_adopted_when_not_smaller() -> None:
    """RED proof: replace the same comparison with ``if True`` and this test
    fails — a LOG fallback that is equal or larger silently overwrites the
    SMART_CRUSHER no-savings output with an inferior (and, for a real
    LogCompressor, lossy) result. The chain still names LOG as attempted
    (module docstring: the chain lists every strategy attempted, not only
    the winner), but the shipped bytes and the decision reason must stay
    SMART_CRUSHER's."""
    debug_events: list[dict] = []
    crusher = SimpleNamespace(
        crush=lambda content, query="", bias=1.0: SimpleNamespace(compressed=content)
    )
    log_compressor = SimpleNamespace(
        compress=lambda content, bias=1.0: SimpleNamespace(compressed=content + "not smaller")
    )

    compressed, tokens, chain = _apply(
        _dispatcher(debug_events),
        crusher=crusher,
        log_compressor=log_compressor,
        debug_events=debug_events,
    )

    assert compressed == CONTENT
    assert tokens == len(CONTENT)
    assert chain == [CompressionStrategy.SMART_CRUSHER.value, CompressionStrategy.LOG.value]
    assert _reason(debug_events) == "smart_crusher"


def test_log_fallback_is_not_adopted_when_exactly_equal() -> None:
    """Boundary case the "not smaller" test above does not reach: a LOG
    fallback of the EXACT same token count as the no-savings SMART_CRUSHER
    output. The comparison is a strict ``<``, so equal must not adopt either.

    RED proof: widen the comparison to ``if log_compressed_tokens <=
    compressed_tokens`` and this test fails — an equal-size LOG result
    silently replaces the SMART_CRUSHER output for no size benefit at all."""
    debug_events: list[dict] = []
    crusher = SimpleNamespace(
        crush=lambda content, query="", bias=1.0: SimpleNamespace(compressed=content)
    )
    log_compressor = SimpleNamespace(
        # Same length as CONTENT, different bytes — proves adoption vs.
        # rejection by content identity, not just by coincidental equality.
        compress=lambda content, bias=1.0: SimpleNamespace(compressed="x" * len(content))
    )

    compressed, tokens, chain = _apply(
        _dispatcher(debug_events),
        crusher=crusher,
        log_compressor=log_compressor,
        debug_events=debug_events,
    )

    assert compressed == CONTENT
    assert tokens == len(CONTENT)
    assert chain == [CompressionStrategy.SMART_CRUSHER.value, CompressionStrategy.LOG.value]
    assert _reason(debug_events) == "smart_crusher"


def test_log_fallback_exception_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    """RED proof: remove the ``try/except Exception`` around
    ``log_compressor.compress(...)`` and this test fails with the planted
    ``RuntimeError`` propagating out of ``apply`` instead of the SMART_CRUSHER
    no-savings output shipping unchanged."""
    debug_events: list[dict] = []
    crusher = SimpleNamespace(
        crush=lambda content, query="", bias=1.0: SimpleNamespace(compressed=content)
    )

    def _raise(content: str, bias: float = 1.0):
        raise RuntimeError("boom")

    log_compressor = SimpleNamespace(compress=_raise)

    with caplog.at_level(logging.DEBUG, logger="test.router_dispatch.no_savings_fallback"):
        compressed, tokens, chain = _apply(
            _dispatcher(debug_events),
            crusher=crusher,
            log_compressor=log_compressor,
            debug_events=debug_events,
        )

    assert compressed == CONTENT
    assert tokens == len(CONTENT)
    assert chain == [CompressionStrategy.SMART_CRUSHER.value, CompressionStrategy.LOG.value]
    assert _reason(debug_events) == "smart_crusher"
    # Pin the stable token and the debug level, not the full sentence: the
    # exception detail interpolated after it is not load-bearing here, and a
    # future rewording of that detail should not break this test.
    matching = [
        r
        for r in caplog.records
        if r.name == "test.router_dispatch.no_savings_fallback"
        and "Log fallback failed" in r.getMessage()
    ]
    assert matching, "expected a log record noting the LOG fallback failed"
    assert matching[0].levelno == logging.DEBUG
