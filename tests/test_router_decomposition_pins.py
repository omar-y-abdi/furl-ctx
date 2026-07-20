"""Characterization pins for the §4.1 ContentRouter decomposition (S0).

Pins router behavior that no existing suite locks down, so every subsequent
extraction step (S1-S7) is verified against the CURRENT reality:

* the routing summary INFO line's exact shape (protections-only: full-string
  equality; with compressions: everything exact except the wall-clock
  ``...ns avg`` digits);
* the empty-output guard's routing_log rewrite as seen by the OBSERVER
  stream (the unit-level result pin lives in
  test_content_router_empty_guard_metrics.py);
* the mixed path end-to-end — no monkeypatching, real splitter, real
  compressors, byte-deterministic output;
* a frozen + content-blocks + excluded-tool matrix asserting WHOLE-DICT
  ``route_counts`` equality, including the pre-seeded zero keys and the
  conditionally-seeded keys (``cache_control_protected``, ``cache_miss``)
  exactly as the current router books them.

Gate-chain ORDER is behavior (excluded-tool window before user-skip before
size before error before detection-based protections) — the matrix pins its
observable outcome per lane.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any
from unittest.mock import patch

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers.estimator import EstimatingTokenCounter
from furl_ctx.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    ContentRouterConfig,
    ContentType,
    RouterCompressionResult,
    RoutingDecision,
)

_ROUTER_LOGGER = "furl_ctx.transforms.content_router"


def _rows(seed: int, n: int = 80) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        h = hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()
        out.append(
            {
                "id": h[:24],
                "path": f"src/mod_{i % 7}/file_{h[:6]}.py",
                "status": ["ok", "skip", "ok", "warn"][i % 4],
                "msg": f"unit {h[6:18]} finished in {i % 91}ms",
            }
        )
    return out


_BIG_JSON = json.dumps(_rows(1), ensure_ascii=False)
_BIG_JSON_2 = json.dumps(_rows(2), ensure_ascii=False)


class _SpyObserver:
    def __init__(self) -> None:
        self.compressions: list[tuple[str, int, int]] = []
        self.route_counts: dict[str, int] | None = None

    def record_compression(
        self, *, strategy: str, original_tokens: int, compressed_tokens: int
    ) -> None:
        self.compressions.append((strategy, original_tokens, compressed_tokens))

    def record_router_route_counts(self, route_counts: dict[str, int], /) -> None:
        self.route_counts = dict(route_counts)


def _tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _clone(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(json.dumps(m)) for m in messages]


# ---------------------------------------------------------------------------
# Routing summary log line shape.
# ---------------------------------------------------------------------------


def test_summary_log_line_shape_protections_only(caplog) -> None:
    """No compressions and no cache activity → the summary line is fully
    deterministic; pin it byte-for-byte (part order included)."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c2", "function": {"name": "Read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": _BIG_JSON_2},
        {"role": "user", "content": "please summarize everything above for me now. " * 20},
    ]
    router = ContentRouter(ContentRouterConfig())
    with caplog.at_level(logging.INFO, logger=_ROUTER_LOGGER):
        router.apply(_clone(messages), _tokenizer())

    summaries = [r.getMessage() for r in caplog.records if "msgs — " in r.getMessage()]
    assert summaries == [
        "content_router: 3 msgs — 1 excluded (Read/Glob), 1 skipped (user), 1 skipped (<50 words)"
    ]


def test_summary_log_line_shape_with_compressions(caplog) -> None:
    """With compressions the line gains the compressed-details head and the
    cache-stats tail; everything except the wall-clock ``...ns avg`` digits is
    pinned exactly."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "run_query", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": _BIG_JSON},
    ]
    router = ContentRouter(ContentRouterConfig())
    with caplog.at_level(logging.INFO, logger=_ROUTER_LOGGER):
        router.apply(_clone(messages), _tokenizer())

    summaries = [r.getMessage() for r in caplog.records if "msgs — " in r.getMessage()]
    assert len(summaries) == 1
    assert re.fullmatch(
        r"content_router: 2 msgs — 1 compressed \(smart_crusher:0\.06\), "
        r"1 skipped \(<50 words\), 1 cache misses, "
        r"cache\[1 results, 0 skips, \d+ns avg\]",
        summaries[0],
    ), f"summary line shape changed: {summaries[0]!r}"


# ---------------------------------------------------------------------------
# Empty-output guard: the routing_log rewrite must be what the OBSERVER sees.
# ---------------------------------------------------------------------------


def test_empty_guard_rewrite_reaches_observer_stream(caplog) -> None:
    content = "this is some non-empty content that should compress to something useful"
    n = len(content.split())

    def fake_pure(self, c, strategy, context, question, bias=None, **kwargs):
        return RouterCompressionResult(
            compressed="",
            original=c,
            strategy_used=CompressionStrategy.TEXT,
            routing_log=[RoutingDecision(ContentType.PLAIN_TEXT, CompressionStrategy.TEXT, n, 0)],
        )

    spy = _SpyObserver()
    router = ContentRouter(ContentRouterConfig(), observer=spy)
    with (
        patch.object(ContentRouter, "_compress_pure", fake_pure),
        caplog.at_level(logging.WARNING, logger=_ROUTER_LOGGER),
    ):
        result = router.compress(content)

    assert result.compressed == content
    # The observer receives the REWRITTEN decision (passthrough numbers), not
    # the phantom-savings one the transform produced.
    assert spy.compressions == [("text", n, n)]
    warned = [r.getMessage() for r in caplog.records if "EMPTY output" in r.getMessage()]
    assert len(warned) == 1
    assert warned[0].startswith(
        "content_router: compression produced EMPTY output from non-empty input"
    )
    assert "strategy=text" in warned[0]


# ---------------------------------------------------------------------------
# Mixed path end-to-end (no monkeypatching).
# ---------------------------------------------------------------------------

_MIXED_CONTENT = (
    "Run summary follows with a moderately long prose introduction that "
    "explains what happened during the pipeline execution step by step.\n\n"
    "```python\ndef report(x):\n    return x * 2\n```\n\n"
    + json.dumps(_rows(5, 60), ensure_ascii=False)
    + "\n\nClosing prose paragraph summarizing the results of the run in detail."
)


def test_mixed_path_end_to_end() -> None:
    router = ContentRouter(ContentRouterConfig())
    result = router.compress(_MIXED_CONTENT, context="ctx")

    assert result.strategy_used is CompressionStrategy.MIXED
    assert result.sections_processed == 4
    # The mixed path does not populate the top-level strategy_chain (only the
    # pure path threads it through) — pinned current behavior.
    assert result.strategy_chain == []
    assert [
        (d.content_type.value, d.strategy.value, d.section_index) for d in result.routing_log
    ] == [
        ("text", "text", 0),
        ("source_code", "passthrough", 1),
        ("json_array", "smart_crusher", 2),
        ("text", "text", 3),
    ]
    # The JSON section genuinely compressed; the fenced code section shipped
    # re-fenced and unmangled.
    assert result.compressed != _MIXED_CONTENT
    assert "```python\ndef report(x):\n    return x * 2\n```" in result.compressed
    assert result.compression_ratio < 1.0

    # Byte-determinism across fresh routers (fresh caches, same input).
    router2 = ContentRouter(ContentRouterConfig())
    assert router2.compress(_MIXED_CONTENT, context="ctx").compressed == result.compressed


def test_mixed_path_through_apply_flat_transform_format() -> None:
    """End-to-end apply() over a mixed tool message: the string path books the
    flat ``router:mixed:{ratio}`` transform format."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "run_query", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": _MIXED_CONTENT},
    ]
    router = ContentRouter(ContentRouterConfig())
    result = router.apply(_clone(messages), _tokenizer())

    mixed_transforms = [t for t in result.transforms_applied if t.startswith("router:mixed:")]
    assert len(mixed_transforms) == 1
    assert re.fullmatch(r"router:mixed:0\.\d{2}", mixed_transforms[0])
    assert result.messages[1]["content"] != _MIXED_CONTENT


# ---------------------------------------------------------------------------
# Frozen + content-blocks + excluded-tool matrix: WHOLE-DICT route_counts.
# ---------------------------------------------------------------------------


def test_route_counts_whole_dict_matrix() -> None:
    """One apply() across every routing lane; the observer's route_counts dict
    must match WHOLE-DICT — pre-seeded zeros included. Any change to seeding,
    bump keys, or gate order shows up here."""
    messages = [
        # 0: frozen (books NOTHING — not even a counter).
        {"role": "tool", "tool_call_id": "f1", "content": _BIG_JSON},
        # 1: assistant carrying the tool_calls map; empty content → small.
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "run_query", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "Read", "arguments": "{}"}},
            ],
        },
        # 2: excluded tool (Read) within the protection window.
        {"role": "tool", "tool_call_id": "c2", "content": _BIG_JSON_2},
        # 3: user message (protected).
        {"role": "user", "content": "please summarize everything above for me now. " * 20},
        # 4: small tool output.
        {"role": "tool", "tool_call_id": "c1", "content": "tiny"},
        # 5: non-string content.
        {"role": "assistant", "content": None},
        # 6: content-blocks message — a compressible tool_result, a
        #    cache_control-protected text block, and a small text block.
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": _BIG_JSON},
                {
                    "type": "text",
                    "text": "cached " * 100,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "tiny"},
            ],
        },
        # 7: compressible string-path tool output (cache miss → Pass-2).
        {"role": "tool", "tool_call_id": "c1", "content": _BIG_JSON_2},
    ]

    spy = _SpyObserver()
    router = ContentRouter(ContentRouterConfig(), observer=spy)
    result = router.apply(_clone(messages), _tokenizer(), frozen_message_count=1)

    assert spy.route_counts == {
        "analysis_ctx": 0,
        "cache_control_protected": 1,
        "cache_miss": 2,
        "content_blocks": 1,
        "excluded_tool": 1,
        "non_string": 1,
        "ratio_too_high": 0,
        "recent_code": 0,
        "small": 2,
        "user_msg": 1,
    }
    # The transform strings for the same run (flat string-path format vs
    # label-threaded block-path format) — order is Pass-1 walk order with the
    # Pass-3 merge appended.
    assert result.transforms_applied == [
        "router:excluded:tool",
        "router:protected:user_message",
        "router:tool_result:smart_crusher",
        "router:smart_crusher:0.06",
    ]
    # Frozen message shipped byte-identical.
    assert result.messages[0] == messages[0]


def test_route_counts_matrix_second_apply_serves_reverifiable_sentinels() -> None:
    """Re-applying the SAME conversation on the SAME router resolves both
    compressible lanes through the Tier-2 cache. Each cached output carries a
    ``<<ccr:HASH>>`` row-drop sentinel whose WHOLE-BLOB backing is still live in
    the store, so ``ensure_ccr_backed`` re-verifies it and serves the cached
    output (``cache_hit``), never stale.

    Before MATRIX-01 unified the re-backing gate on the public marker grammar,
    the gate ALSO scanned the internal ``HASH#rows`` granular-index hint — not a
    valid retrievable CCR key (``is_valid_ccr_hash`` rejects it) and never
    resolvable through ``retrieve`` — so a present, recoverable blob spuriously
    recomputed on every hit. The gate now checks exactly the retrievable blob
    hashes the public ``CompressResult.ccr_hashes`` surface advertises, so a live
    blob serves cached. Whole-dict pinned; served bytes byte-identical to the
    first pass (no silent loss — the blob resolves)."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "run_query", "arguments": "{}"}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": _BIG_JSON}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": _BIG_JSON_2},
    ]
    router = ContentRouter(ContentRouterConfig(), observer=_SpyObserver())
    first = router.apply(_clone(messages), _tokenizer())

    spy = _SpyObserver()
    router._observer = spy
    result = router.apply(_clone(messages), _tokenizer())

    assert spy.route_counts == {
        "analysis_ctx": 0,
        "cache_hit": 2,
        "content_blocks": 1,
        "excluded_tool": 0,
        "non_string": 0,
        "ratio_too_high": 0,
        "recent_code": 0,
        "small": 1,
        "user_msg": 0,
    }
    assert result.transforms_applied == [
        "router:tool_result:smart_crusher",
        "router:smart_crusher:0.06",
    ]
    assert result.messages == first.messages
