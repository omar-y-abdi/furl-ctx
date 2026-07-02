"""Message-shape-aware tokens_before/after accounting (COR-39).

``apply()`` used to compute ``tokens_before/after`` as
``tokenizer.count_text(str(m.get("content", "")))`` — tokenizing the Python
REPR of block-list content. Deltas were roughly meaningful; absolute numbers
(and the derived ``context_pressure``, which drives ``min_ratio``) were
inflated fictions for block-format conversations.

Pinned here: both totals come from ``tokenizer.count_messages`` (the base
tokenizer walks parts — text payloads, image budgets, tool_result payloads —
and never sees a Python repr).
"""

from __future__ import annotations

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig


def _make_tokenizer() -> Tokenizer:
    # Fixed ratio: deterministic char-based counting for exact assertions.
    return Tokenizer(EstimatingTokenCounter(chars_per_token=4.0))


def _block_conversation() -> list[dict]:
    body = " ".join(f"record {i} status nominal throughput steady" for i in range(30))
    return [
        {"role": "user", "content": [{"type": "text", "text": "Summarize the results."}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_acct", "content": body},
                {"type": "text", "text": "end of tool output"},
            ],
        },
        {"role": "assistant", "content": "All shards report nominal status."},
    ]


def test_tokens_before_counts_message_shape_not_repr():
    tokenizer = _make_tokenizer()
    messages = _block_conversation()
    router = ContentRouter(ContentRouterConfig())

    result = router.apply(messages, tokenizer)

    expected = tokenizer.count_messages(messages)
    assert result.tokens_before == expected

    # The old repr-based accounting counted `[{'type': ...}]` punctuation and
    # key names as payload tokens — strictly more than the parts-aware count.
    repr_based = sum(tokenizer.count_text(str(m.get("content", ""))) for m in messages)
    assert repr_based > expected
    assert result.tokens_before != repr_based


def test_tokens_after_counts_transformed_message_shape():
    tokenizer = _make_tokenizer()
    messages = _block_conversation()
    router = ContentRouter(ContentRouterConfig())

    result = router.apply(messages, tokenizer)

    assert result.tokens_after == tokenizer.count_messages(result.messages)
