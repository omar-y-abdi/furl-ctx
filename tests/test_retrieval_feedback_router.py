"""Router pins for the retrieval-feedback loop (Engine P2-13).

``ContentRouterConfig.enable_retrieval_feedback`` is OPT-IN (default False)
like ``enable_code_aware``:

* Flag OFF (default): the aggregator is never consulted — routing is
  byte-identical whether or not retrieval signals exist. Pinned below with a
  poisoned global aggregator.
* Flag ON, no signals: hints are neutral — routing stays byte-identical.
* Flag ON, hint active: the MECHANISM (not exact bytes) is pinned —
  ``skip_compression`` serves the original verbatim and records a
  ``router:feedback:skip`` transform; ``keep_budget_multiplier`` scales the
  bias handed to ``compress()`` (mirroring how ``_get_tool_bias`` feeds the
  same parameter).

The consult happens on all three compression entrances a tool output can
take: the string path, the flat ``tool_result`` block path, and the nested
``tool_result`` parts path (COR-47's dominant Claude-Code shape).
"""

from __future__ import annotations

import copy
import json

import pytest

from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.cache.retrieval_feedback import (
    RetrievalFeedback,
    reset_retrieval_feedback,
    routing_shape_key,
    set_retrieval_feedback,
)
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    RouterCompressionResult,
)
from furl_ctx.transforms.router_policy import CompressionStrategy

TOOL_NAME = "queryapi"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated_feedback_and_store():
    reset_retrieval_feedback()
    reset_compression_store()
    yield
    reset_retrieval_feedback()
    reset_compression_store()


def _seed_signals(count: int, tool: str | None = TOOL_NAME) -> None:
    fb = RetrievalFeedback(clock=FakeClock())
    shape = routing_shape_key(tool, "json_array")
    for _ in range(count):
        fb.record_retrieval(shape)
    set_retrieval_feedback(fb)


def _tokenizer() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _big_json_array() -> str:
    return json.dumps(
        [
            {"id": i, "region": f"region-{i % 7}", "value": f"record {i} alpha beta gamma"}
            for i in range(80)
        ]
    )


def _string_path_messages() -> list[dict]:
    return [
        {"role": "user", "content": "please continue the run"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": TOOL_NAME}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": _big_json_array()},
    ]


def _block_path_messages(nested: bool) -> list[dict]:
    payload: object = _big_json_array()
    if nested:
        payload = [{"type": "text", "text": _big_json_array()}]
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": TOOL_NAME, "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": payload}],
        },
    ]


def _apply(config: ContentRouterConfig, messages: list[dict]):
    router = ContentRouter(config)
    return router.apply(copy.deepcopy(messages), _tokenizer())


def _canon(messages: list[dict]) -> str:
    return json.dumps(messages, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Default OFF + byte-identity
# ---------------------------------------------------------------------------


def test_flag_defaults_to_off() -> None:
    assert ContentRouterConfig().enable_retrieval_feedback is False


def test_flag_off_routing_is_byte_identical_even_with_seeded_signals() -> None:
    messages = _string_path_messages()

    reset_compression_store()
    baseline = _apply(ContentRouterConfig(), messages)

    # Poison the global aggregator well past every threshold; with the flag
    # OFF (default) it must never be consulted.
    _seed_signals(count=20)
    reset_compression_store()
    with_signals = _apply(ContentRouterConfig(), messages)

    assert _canon(with_signals.messages) == _canon(baseline.messages)
    assert with_signals.transforms_applied == baseline.transforms_applied


def test_flag_on_with_no_signals_is_byte_identical_to_flag_off() -> None:
    messages = _string_path_messages()

    reset_compression_store()
    baseline = _apply(ContentRouterConfig(), messages)

    reset_compression_store()
    flag_on = _apply(ContentRouterConfig(enable_retrieval_feedback=True), messages)

    assert _canon(flag_on.messages) == _canon(baseline.messages)
    assert flag_on.transforms_applied == baseline.transforms_applied


# ---------------------------------------------------------------------------
# Skip hint: original served verbatim (string path)
# ---------------------------------------------------------------------------


def test_skip_hint_serves_original_verbatim_on_string_path() -> None:
    messages = _string_path_messages()
    original_content = messages[2]["content"]

    # Control: without the hint this content genuinely compresses.
    reset_compression_store()
    control = _apply(ContentRouterConfig(), messages)
    assert control.messages[2]["content"] != original_content

    _seed_signals(count=6)  # skip threshold
    reset_compression_store()
    result = _apply(ContentRouterConfig(enable_retrieval_feedback=True), messages)

    assert result.messages[2]["content"] == original_content
    assert "router:feedback:skip" in result.transforms_applied


def test_anonymous_signals_protect_named_tool_routing() -> None:
    # Live CCR producers mostly store with tool_name=None; their retrieval signals land in the
    # tool-anonymous bucket and must still protect the named tool emitting the same content shape.
    messages = _string_path_messages()
    original_content = messages[2]["content"]

    _seed_signals(count=6, tool=None)
    reset_compression_store()
    result = _apply(ContentRouterConfig(enable_retrieval_feedback=True), messages)

    assert result.messages[2]["content"] == original_content
    assert "router:feedback:skip" in result.transforms_applied


# ---------------------------------------------------------------------------
# Keep-budget hint: multiplier scales the bias handed to compress()
# ---------------------------------------------------------------------------


def _apply_with_bias_capture(config: ContentRouterConfig, messages: list[dict]) -> list[float]:
    router = ContentRouter(config)
    captured: list[float] = []

    def _fake_compress(
        content, context="", question=None, bias=1.0, *, token_counter=None, **kwargs
    ):
        # ``**kwargs`` absorbs apply()'s ``detection=`` threading (PERF-2c),
        # matching every other compress stub in the suite.
        captured.append(bias)
        return RouterCompressionResult(
            compressed=content,
            original=content,
            strategy_used=CompressionStrategy.PASSTHROUGH,
        )

    router.compress = _fake_compress  # type: ignore[method-assign]
    router.apply(copy.deepcopy(messages), _tokenizer())
    return captured


def test_hint_multiplier_scales_bias_on_string_path() -> None:
    messages = _string_path_messages()

    control = _apply_with_bias_capture(ContentRouterConfig(), messages)
    assert control == [pytest.approx(1.0)]

    _seed_signals(count=3)  # hint threshold, below skip threshold
    hinted = _apply_with_bias_capture(ContentRouterConfig(enable_retrieval_feedback=True), messages)
    assert hinted == [pytest.approx(1.5)]  # tool bias 1.0 x keep-budget 1.5


def test_hint_multiplier_scales_bias_on_block_path() -> None:
    messages = _block_path_messages(nested=False)

    control = _apply_with_bias_capture(ContentRouterConfig(), messages)
    assert control == [pytest.approx(1.0)]

    _seed_signals(count=3)
    hinted = _apply_with_bias_capture(ContentRouterConfig(enable_retrieval_feedback=True), messages)
    assert hinted == [pytest.approx(1.5)]


# ---------------------------------------------------------------------------
# Block paths: skip hint protects flat and nested tool_result payloads
# ---------------------------------------------------------------------------


def test_skip_hint_protects_flat_tool_result_block() -> None:
    messages = _block_path_messages(nested=False)

    _seed_signals(count=6)
    reset_compression_store()
    result = _apply(ContentRouterConfig(enable_retrieval_feedback=True), messages)

    assert _canon(result.messages[1:2]) == _canon(messages[1:2])
    assert "router:feedback:skip" in result.transforms_applied


def test_skip_hint_protects_nested_tool_result_parts() -> None:
    messages = _block_path_messages(nested=True)

    _seed_signals(count=6)
    reset_compression_store()
    result = _apply(ContentRouterConfig(enable_retrieval_feedback=True), messages)

    assert _canon(result.messages[1:2]) == _canon(messages[1:2])
    assert "router:feedback:skip" in result.transforms_applied
