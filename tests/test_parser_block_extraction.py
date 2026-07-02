"""Block-extraction coverage for headroom/parser.py (was 53%).

Drives the PUBLIC ``parse_message_to_blocks`` over the content-block shapes
whose branches were uncovered: Anthropic ``tool_result`` blocks, Strands/Bedrock
``toolResult`` blocks, multi-part content lists (mixed dict + plain-string
parts), and plain-string content. Assertions are on the parsed Block STRUCTURE
(kind / text / flags / source_index), not on echoed inputs.

The None-text coercion sites are already covered by
``tests/test_parser_none_text.py``; this file covers the orthogonal
container-shape branches (parser.py:201-217, :245-268, :199-200, :218-219).
"""

from __future__ import annotations

import pytest

from headroom.parser import find_tool_units, get_message_content_text, parse_message_to_blocks
from headroom.tokenizers import get_tokenizer


@pytest.fixture(scope="module")
def tokenizer():
    return get_tokenizer("gpt-4o")


# ─── tool_result container shapes → dedicated tool_result blocks ────────────


def test_anthropic_tool_result_block_extracted(tokenizer) -> None:
    # parser.py:208-211, :245-268 — Anthropic Messages format nests tool output
    # under content[].type == "tool_result"; it becomes a dedicated block.
    message = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_ABC",
                "content": [{"type": "text", "text": "TOOL_OUTPUT_BODY"}],
            }
        ],
    }
    blocks = parse_message_to_blocks(message, 7, tokenizer)

    assert len(blocks) == 1
    block = blocks[0]
    assert block.kind == "tool_result"
    assert block.text == "TOOL_OUTPUT_BODY"
    assert block.flags["tool_call_id"] == "toolu_ABC"
    assert block.source_index == 7


def test_strands_tool_result_block_extracted(tokenizer) -> None:
    # parser.py:212-214, :246/:253 — Strands/Bedrock converse format wraps the
    # payload one level deeper under a "toolResult" key with "toolUseId".
    message = {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "ts_42",
                    "content": [{"text": "STRANDS_BODY"}],
                }
            }
        ],
    }
    blocks = parse_message_to_blocks(message, 2, tokenizer)

    assert len(blocks) == 1
    block = blocks[0]
    assert block.kind == "tool_result"
    assert block.text == "STRANDS_BODY"
    assert block.flags["tool_call_id"] == "ts_42"


def test_tool_result_only_message_has_no_empty_container_block(tokenizer) -> None:
    # parser.py:272 — a message whose only payload is a tool_result block must
    # NOT also emit an empty container block; exactly the dedicated block.
    message = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": [{"type": "text", "text": "ONLY_BODY"}],
            }
        ],
    }
    blocks = parse_message_to_blocks(message, 0, tokenizer)
    kinds = [b.kind for b in blocks]
    assert kinds == ["tool_result"]
    assert all(b.text for b in blocks)  # no empty-text container leaked


# ─── multi-part content list (dict text parts + plain strings) ──────────────


def test_multi_part_text_list_is_joined(tokenizer) -> None:
    # parser.py:201-217 — a list with dict text parts AND a bare string part:
    # all text parts join with "\n" into a single block.
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
            "third",  # bare-string part (:215-216)
        ],
    }
    blocks = parse_message_to_blocks(message, 0, tokenizer)

    assert len(blocks) == 1
    assert blocks[0].text == "first\nsecond\nthird"
    assert blocks[0].kind == "user"


def test_multi_part_list_ignores_non_text_parts(tokenizer) -> None:
    # Mixed list: an image part (unknown type) is dropped; only text survives.
    message = {
        "role": "user",
        "content": [
            {"type": "image", "source": {"data": "..."}},
            {"type": "text", "text": "caption"},
        ],
    }
    blocks = parse_message_to_blocks(message, 0, tokenizer)
    assert blocks[0].text == "caption"


# ─── plain-string content & non-string/non-list fallback ────────────────────


def test_plain_string_content_single_block(tokenizer) -> None:
    # parser.py:199-200 — string content path; kind derives from the role.
    blocks = parse_message_to_blocks(
        {"role": "assistant", "content": "a plain reply"}, 4, tokenizer
    )
    assert len(blocks) == 1
    assert blocks[0].kind == "assistant"
    assert blocks[0].text == "a plain reply"
    assert blocks[0].source_index == 4


def test_non_string_non_list_content_stringified(tokenizer) -> None:
    # parser.py:218-219 — content that is neither str nor list (e.g. an int)
    # falls through to str(content); the block carries the stringified form.
    blocks = parse_message_to_blocks({"role": "user", "content": 12345}, 0, tokenizer)
    assert len(blocks) == 1
    assert blocks[0].text == "12345"


def test_empty_message_yields_minimal_unknown_block(tokenizer) -> None:
    # parser.py:307-317 — a message with no content and no tool_calls still
    # yields exactly one minimal block of kind "unknown" with empty text.
    blocks = parse_message_to_blocks({"role": "user"}, 9, tokenizer)
    assert len(blocks) == 1
    assert blocks[0].kind == "unknown"
    assert blocks[0].text == ""
    assert blocks[0].source_index == 9


@pytest.mark.parametrize(
    "role,expected_kind",
    [
        ("system", "system"),
        ("assistant", "assistant"),
        ("tool", "tool_result"),
        ("mystery", "unknown"),
    ],
)
def test_role_maps_to_block_kind(tokenizer, role: str, expected_kind: str) -> None:
    # parser.py:222-232 — role → kind mapping over plain-string content
    # (user is excluded here: it forks on RAG detection, tested elsewhere).
    blocks = parse_message_to_blocks({"role": role, "content": "body text"}, 0, tokenizer)
    assert blocks[0].kind == expected_kind


def test_openai_tool_message_records_tool_call_id(tokenizer) -> None:
    # parser.py:229-230, :236-237 — role="tool" with a tool_call_id maps to a
    # tool_result block carrying that id in flags.
    blocks = parse_message_to_blocks(
        {"role": "tool", "content": "fn result", "tool_call_id": "call_77"},
        3,
        tokenizer,
    )
    assert blocks[0].kind == "tool_result"
    assert blocks[0].flags["tool_call_id"] == "call_77"


# ─── get_message_content_text content arms (parser.py:473-489) ──────────────


@pytest.mark.parametrize(
    "content,expected",
    [
        (None, ""),  # :476-477 explicit None
        ("plain", "plain"),  # :478-479 string passthrough
        ([{"type": "text", "text": "a"}, "b"], "a\nb"),  # :480-488 list + bare str
        (12345, "12345"),  # :489 non-str/non-list → str()
    ],
)
def test_get_message_content_text_content_arms(content, expected: str) -> None:
    # get_message_content_text is a distinct extraction entry from
    # parse_message_to_blocks (which handles content inline); cover each arm.
    assert get_message_content_text({"role": "user", "content": content}) == expected


def test_get_message_content_text_absent_key_is_empty() -> None:
    # :475-477 — a message with no "content" key resolves to "" (None default).
    assert get_message_content_text({"role": "user"}) == ""


# ─── find_tool_units assistant↔response pairing (parser.py:390-470) ──────────


@pytest.mark.parametrize(
    "messages",
    [
        # OpenAI: assistant.tool_calls[].id ↔ role="tool".tool_call_id (:413-416, :443-448)
        [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "f"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
        ],
        # Anthropic: assistant tool_use ↔ user tool_result (:425-428, :456-459)
        [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu1"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": []}],
            },
        ],
        # Strands: assistant toolUse ↔ user toolResult (:429-433, :460-464)
        [
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "ts1"}}]},
            {
                "role": "user",
                "content": [{"toolResult": {"toolUseId": "ts1", "content": []}}],
            },
        ],
    ],
)
def test_find_tool_units_pairs_assistant_with_response(messages) -> None:
    # Each format pairs the assistant at index 0 with its tool response at 1.
    assert find_tool_units(messages) == [(0, [1])]


def test_find_tool_units_no_pairing_is_empty() -> None:
    # Boundary: a conversation with no tool calls yields no units (:466-470).
    assert find_tool_units([{"role": "user", "content": "hi"}]) == []


def test_find_tool_units_unmatched_call_id_excluded() -> None:
    # Boundary: an assistant tool_call whose id has no matching tool response
    # produces no unit (response_indices stays empty → not appended).
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "orphan", "function": {"name": "f"}}]},
        {"role": "tool", "tool_call_id": "different", "content": "r"},
    ]
    assert find_tool_units(messages) == []
