"""SmartCrusher aliased-message frozen-boundary regression (COR-55).

`SmartCrusher.apply` honors `frozen_message_count` by index, but wrote
results by IN-PLACE MUTATION of the deep-copied messages — and
`copy.deepcopy` preserves aliasing via its memo. When the caller's list
contains the SAME dict object below and above the frozen boundary
(framework retry / re-append patterns), crushing the non-frozen
occurrence rewrote the frozen one, violating the frozen-prefix contract
(byte-identical prefix → provider prefix-cache hit).

Reproduction from the finding: `[shared, user, shared]` with `frozen=1`
→ output[0] compressed.

Pinned here: every write site uses the `{**msg, "content": ...}`
copy-on-write idiom (the router/dedup idiom — both verified immune), so
the frozen occurrence ships verbatim while the live occurrence still
crushes. All three write sites are covered: the OpenAI `role=tool`
string shape, the Anthropic `tool_result` string-content shape, and the
canonical Anthropic/MCP nested text-parts shape.
"""

from __future__ import annotations

import json

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.smart_crusher import SmartCrusher


def _tok() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())


def _crushable_payload() -> str:
    """A JSON array big enough (over min_tokens_to_crush) to crush."""
    rows = [
        {
            "id": i,
            "path": f"/repo/src/module_{i}/file_{i}.py",
            "status": "ok",
            "duration_ms": (i * 13) % 500,
            "notes": f"scanned during pass {i % 4} with no findings recorded",
        }
        for i in range(80)
    ]
    return json.dumps(rows)


def test_aliased_tool_message_across_frozen_boundary_stays_verbatim() -> None:
    """The finding's reproduction: OpenAI role=tool string content."""
    payload = _crushable_payload()
    shared = {"role": "tool", "tool_call_id": "c1", "content": payload}
    messages = [shared, {"role": "user", "content": "hi"}, shared]

    result = SmartCrusher().apply(messages, _tok(), frozen_message_count=1)

    # Self-validation: the live occurrence (index 2) actually crushed —
    # without this the frozen assertion proves nothing.
    assert result.messages[2]["content"] != payload
    assert result.markers_inserted
    # The regression: the frozen occurrence must ship byte-identical.
    assert result.messages[0]["content"] == payload, (
        "frozen aliased message was rewritten by crushing its live occurrence"
    )
    # The caller's input objects must never be touched either.
    assert messages[0]["content"] == payload
    assert messages[2]["content"] == payload


def test_aliased_block_message_across_frozen_boundary_stays_verbatim() -> None:
    """Anthropic shape: tool_result block with string content."""
    payload = _crushable_payload()
    shared = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": payload}],
    }
    messages = [shared, {"role": "assistant", "content": "ok"}, shared]

    result = SmartCrusher().apply(messages, _tok(), frozen_message_count=1)

    assert result.messages[2]["content"][0]["content"] != payload
    assert result.messages[0]["content"][0]["content"] == payload, (
        "frozen aliased block message was rewritten by crushing its live occurrence"
    )
    assert messages[0]["content"][0]["content"] == payload


def test_aliased_nested_parts_across_frozen_boundary_stays_verbatim() -> None:
    """Canonical Anthropic/MCP shape: tool_result with nested text parts."""
    payload = _crushable_payload()
    shared = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu2",
                "content": [{"type": "text", "text": payload}],
            }
        ],
    }
    messages = [shared, {"role": "assistant", "content": "ok"}, shared]

    result = SmartCrusher().apply(messages, _tok(), frozen_message_count=1)

    assert result.messages[2]["content"][0]["content"][0]["text"] != payload
    assert result.messages[0]["content"][0]["content"][0]["text"] == payload, (
        "frozen aliased nested-parts message was rewritten by crushing its live occurrence"
    )
    assert messages[0]["content"][0]["content"][0]["text"] == payload


def test_non_aliased_input_behavior_unchanged() -> None:
    """No-regression pin: distinct dicts — frozen stays, live crushes."""
    payload = _crushable_payload()
    messages = [
        {"role": "tool", "tool_call_id": "c1", "content": payload},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c2", "content": payload},
    ]

    result = SmartCrusher().apply(messages, _tok(), frozen_message_count=1)

    assert result.messages[0]["content"] == payload
    assert result.messages[2]["content"] != payload
    assert result.tokens_after < result.tokens_before
