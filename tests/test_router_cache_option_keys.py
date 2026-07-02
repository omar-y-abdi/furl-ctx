"""Option-aware, collision-guarded result-cache keys (COR-18).

The two-tier result cache used to key on ``hash(content)`` alone, with two
consequences:

(a) per-request options were DEFEATED on hits — a Tier-2 hit served a result
    computed under a different bias, silently ignoring the per-call options
    whenever the same bytes recurred within the TTL;
(b) the bare 64-bit SipHash key was served with no content-equality
    verification, so a hash collision substituted another message's
    compressed bytes (CrossMessageDeduper verifies ``first.content ==
    content``; the router cache did not).

``_result_cache_key(content, bias)`` fixes both: the key is
``(hash(content), len(content), round(bias, 3))``, so dict key EQUALITY —
not just the 64-bit hash — verifies content length and the exact per-request
bias before any hit is served. ``context`` is deliberately NOT in the key
(it changes every turn in agent traffic; ``min_ratio`` and CCR backing are
re-checked per hit) — see the helper's docstring.
"""

from __future__ import annotations

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    _result_cache_key,
)


def _make_tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _routable_tool_content() -> str:
    """Deterministic tool output that clears every pre-cache protection gate
    (same fixture family as test_content_router_cache_lookup_paths)."""
    return " ".join(
        f"Line {i}: the nightly report recorded steady throughput and "
        f"nominal latency across shard {i % 5} with no anomalies noted."
        for i in range(12)
    )


def _tool_message(content: str) -> dict:
    return {"role": "tool", "content": content, "tool_call_id": "call_opts"}


def _default_key(content: str):
    return _result_cache_key(content, 1.0)


class TestKeyStructure:
    def test_key_carries_length_guard_and_bias(self):
        """Characterization pin on the key tuple: content hash + LENGTH GUARD
        + rounded bias. The length in the key is the collision guard — dict
        key equality rejects a same-hash/different-length collision as a
        plain miss instead of serving foreign bytes."""
        content = "some tool output payload"
        assert _result_cache_key(content, 1.25) == (
            hash(content),
            len(content),
            1.25,
        )

    def test_near_equal_bias_rounds_to_same_key(self):
        """Bias rides rounded to 3 decimals so float jitter from multiplicative
        hook biases doesn't fragment the cache."""
        content = _routable_tool_content()
        assert _result_cache_key(content, 1.0001) == _result_cache_key(content, 1.0004)
        assert _result_cache_key(content, 1.0) != _result_cache_key(content, 1.5)


class TestOptionAwareServing:
    """End-to-end: entries created under one bias are never served under
    another."""

    def _router(self) -> ContentRouter:
        return ContentRouter(ContentRouterConfig())

    def test_tier2_hit_not_served_under_different_hook_bias(self):
        content = _routable_tool_content()
        router = self._router()
        router._cache.put(_default_key(content), "BIAS-1.0-PAYLOAD", 0.3, "log")

        result = router.apply([_tool_message(content)], _make_tokenizer(), biases={0: 2.0})

        assert result.messages[0]["content"] == content
        assert "BIAS-1.0-PAYLOAD" not in result.messages[0]["content"]

    def test_same_options_still_hit(self):
        """Regression guard: the richer key must not break the normal same-
        options hit path the cache exists for."""
        content = _routable_tool_content()
        router = self._router()
        router._cache.put(_default_key(content), "SAME-OPTIONS-PAYLOAD", 0.3, "log")

        result = router.apply([_tool_message(content)], _make_tokenizer())

        assert result.messages[0]["content"] == "SAME-OPTIONS-PAYLOAD"
        assert "router:log:0.30" in result.transforms_applied
