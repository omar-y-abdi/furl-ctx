"""Cross-message dedup: cache-safety contract + CCR recovery invariant.

P0 contract pinned here (prompt-cache ordering):

* message COUNT and ORDER are preserved — compression operates on content
  WITHIN messages, never on the message list;
* the message at index 0 is NEVER modified (not even duplicate blocks
  inside it);
* anything carrying ``cache_control`` passes through byte-faithful;
* the frozen prefix (``frozen_message_count``) is never modified;
* only LATER occurrences are replaced — the first occurrence (the copy a
  provider prefix cache could already hold) stays byte-identical.

Recovery invariant pinned here (same standard as
``tests/test_ccr_recovery_invariant.py``): every elided duplicate carries a
surfaced ``<<ccr:HASH>>`` pointer in the output AND the original is
byte-recoverable from the Python ``compression_store`` under that hash. A
failed store write must veto the replacement (no pointer without payload).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from headroom import compress
from headroom.cache.compression_store import get_compression_store
from headroom.tokenizer import Tokenizer
from headroom.tokenizers import get_tokenizer
from headroom.transforms.cross_message_dedup import (
    MIN_DEDUP_CHARS,
    CrossMessageDeduper,
)

_DUP_RE = re.compile(r"<<ccr:([0-9a-f]{24}) (\d+)_bytes_duplicate>>")

# A realistic repeated tool output: large enough for dedup (>= MIN_DEDUP_CHARS)
# but under the router's min-token gate so the surrounding pipeline leaves it
# alone and the assertions isolate the dedup behaviour.
PAYLOAD = "\n".join(
    f"PASS test_module_{i:02d}::test_case_{i % 7} ({(i * 37) % 900}ms)" for i in range(12)
)
assert len(PAYLOAD) >= MIN_DEDUP_CHARS


def _tokenizer() -> Tokenizer:
    return Tokenizer(get_tokenizer("gpt-4o"), "gpt-4o")


def _apply(messages: list[dict[str, Any]], **kwargs: Any):
    return CrossMessageDeduper().apply(messages, _tokenizer(), **kwargs)


def _msg_bytes(message: dict[str, Any]) -> str:
    return json.dumps(message, sort_keys=True, ensure_ascii=False)


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Run the test suite."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t1"},
        {"role": "user", "content": "Run it again to confirm."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t2"},
    ]


# --------------------------------------------------------------------------- #
# P0: message order / index 0 / cache_control through PUBLIC compress().
# --------------------------------------------------------------------------- #


def test_public_compress_preserves_count_order_and_index0() -> None:
    messages = [
        {"role": "system", "content": "You are a build assistant."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t1"},
        {"role": "user", "content": "Run it again to confirm."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t2"},
        {"role": "user", "content": "Anything new?"},
    ]
    before = [_msg_bytes(m) for m in messages]

    result = compress(messages, model="gpt-4o")
    out = result.messages

    # Count and order: same number of messages, same role sequence, same
    # tool_call_ids in the same positions. The message list is never
    # dropped from, reordered, or appended to.
    assert len(out) == len(messages)
    assert [m.get("role") for m in out] == [m.get("role") for m in messages]
    assert [m.get("tool_call_id") for m in out] == [m.get("tool_call_id") for m in messages]

    # Index 0 is byte-faithful.
    assert _msg_bytes(out[0]) == before[0]

    # The cached prefix direction: everything BEFORE the first rewritten
    # message is byte-identical (dedup only ever rewrites later copies).
    rewritten = [i for i in range(len(out)) if _msg_bytes(out[i]) != before[i]]
    assert rewritten, "the later duplicate should have been rewritten"
    assert min(rewritten) >= 3, "first occurrence and everything before it stay byte-identical"


def test_public_compress_cache_control_blocks_byte_faithful() -> None:
    cached_block = {
        "type": "tool_result",
        "tool_use_id": "tc1",
        "content": PAYLOAD,
        "cache_control": {"type": "ephemeral"},
    }
    later_cached_block = {
        "type": "tool_result",
        "tool_use_id": "tc2",
        "content": PAYLOAD,
        "cache_control": {"type": "ephemeral"},
    }
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "check"}, cached_block]},
        {"role": "user", "content": "again"},
        {"role": "user", "content": [later_cached_block]},
    ]
    before_first = _msg_bytes(messages[0])
    before_later = _msg_bytes(messages[2])

    result = compress(messages, model="gpt-4o")

    # Both cache_control-bearing blocks pass through byte-faithful — even
    # the LATER duplicate, because the client pinned it as a cache
    # breakpoint.
    assert _msg_bytes(result.messages[0]) == before_first
    assert _msg_bytes(result.messages[2]) == before_later


def test_index0_duplicate_blocks_never_rewritten() -> None:
    # Two identical tool_result blocks INSIDE message 0: index-0 is never a
    # replacement target, so both stay verbatim; a copy in a later message
    # IS replaced.
    block = {"type": "tool_result", "tool_use_id": "a", "content": PAYLOAD}
    messages = [
        {"role": "user", "content": [dict(block), dict(block, tool_use_id="b")]},
        {"role": "user", "content": [dict(block, tool_use_id="c")]},
    ]
    before0 = _msg_bytes(messages[0])

    result = _apply(messages)

    assert _msg_bytes(result.messages[0]) == before0
    later = result.messages[1]["content"][0]["content"]
    assert _DUP_RE.search(later), "later duplicate block should carry the pointer"


# --------------------------------------------------------------------------- #
# Recovery invariant: pointer surfaced + original in the store.
# --------------------------------------------------------------------------- #


def test_elided_duplicate_is_recoverable_from_store() -> None:
    result = compress(_conversation(), model="gpt-4o")

    replaced = result.messages[3]["content"]
    sentinel = json.loads(replaced)
    assert set(sentinel) == {"_ccr_dropped"}, "replacement is the drop sentinel object"

    match = _DUP_RE.search(sentinel["_ccr_dropped"])
    assert match, "sentinel must surface the <<ccr:HASH N_bytes_duplicate>> pointer"
    ccr_hash, n_bytes = match.group(1), int(match.group(2))
    assert n_bytes == len(PAYLOAD.encode("utf-8"))

    entry = get_compression_store().retrieve(ccr_hash)
    assert entry is not None, "original must be in the CCR store under the surfaced hash"
    assert entry.original_content == PAYLOAD, "recovery must be byte-exact"

    # The sentinel names the message that still carries the bytes.
    assert "message 1" in sentinel["_ccr_dropped"]
    assert result.messages[1]["content"] == PAYLOAD


def test_store_failure_vetoes_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    # No pointer without payload: if the store write fails, the duplicate
    # bytes stay in place.
    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("store unavailable")

    store = get_compression_store()
    monkeypatch.setattr(store, "store", boom)

    result = _apply(_conversation())

    assert result.messages[3]["content"] == PAYLOAD
    assert result.transforms_applied == []


# --------------------------------------------------------------------------- #
# Frozen prefix / first occurrence / eligibility gates.
# --------------------------------------------------------------------------- #


def test_frozen_prefix_never_modified() -> None:
    messages = [
        {"role": "user", "content": "q"},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t1"},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t2"},
        {"role": "user", "content": "more"},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t3"},
    ]
    before = [_msg_bytes(m) for m in messages]

    result = _apply(messages, frozen_message_count=3)

    # Index 2 is a duplicate but sits inside the frozen prefix — untouched.
    for i in range(3):
        assert _msg_bytes(result.messages[i]) == before[i]
    # Index 4 is outside the prefix — replaced, pointing at message 1.
    sentinel = json.loads(result.messages[4]["content"])
    assert "message 1" in sentinel["_ccr_dropped"]


def test_first_occurrence_never_modified_and_input_not_mutated() -> None:
    messages = _conversation()
    snapshot = [_msg_bytes(m) for m in messages]

    result = _apply(messages)

    # First occurrence byte-identical in the output.
    assert _msg_bytes(result.messages[1]) == snapshot[1]
    # The input list and its messages were not mutated.
    assert [_msg_bytes(m) for m in messages] == snapshot


def test_below_threshold_and_distinct_content_untouched() -> None:
    small = "x" * (MIN_DEDUP_CHARS - 1)
    messages = [
        {"role": "user", "content": "q"},
        {"role": "tool", "content": small, "tool_call_id": "t1"},
        {"role": "tool", "content": small, "tool_call_id": "t2"},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t3"},
        {"role": "tool", "content": PAYLOAD + " trailing-difference", "tool_call_id": "t4"},
    ]
    before = [_msg_bytes(m) for m in messages]

    result = _apply(messages)

    assert [_msg_bytes(m) for m in result.messages] == before
    assert result.transforms_applied == []
    assert result.tokens_after == result.tokens_before


def test_user_assistant_and_error_outputs_untouched() -> None:
    messages = [
        {"role": "user", "content": "q"},
        {"role": "user", "content": PAYLOAD},
        {"role": "user", "content": PAYLOAD},
        {"role": "assistant", "content": PAYLOAD},
        {"role": "assistant", "content": PAYLOAD},
        {"role": "tool", "content": PAYLOAD + " err", "tool_call_id": "e1", "is_error": True},
        {"role": "tool", "content": PAYLOAD + " err", "tool_call_id": "e2", "is_error": True},
    ]
    before = [_msg_bytes(m) for m in messages]

    result = _apply(messages)

    assert [_msg_bytes(m) for m in result.messages] == before


def test_default_pipeline_orders_dedup_before_router() -> None:
    from headroom.transforms import TransformPipeline

    names = [t.name for t in TransformPipeline().transforms]
    assert "cross_message_dedup" in names
    assert names.index("cross_message_dedup") < names.index("content_router")
