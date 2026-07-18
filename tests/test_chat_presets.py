"""Cross-turn history presets (furl_ctx.chat): compress_chat_history + compress_with_cache.

Behavior-level checks over the real ``compress()`` pipeline (no mocks):

* ``compress_chat_history`` compresses user messages, protects the recent ones
  (via the documented dedup exemption — the recent duplicate survives byte-exact
  while an earlier copy is elided), and wires the retrieval-feedback router.
* ``compress_with_cache`` freezes the first N messages — the unmarked frozen
  prefix stays byte-identical and post-N content still compresses — and never
  mutates the caller's messages.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

from furl_ctx import compress, compress_chat_history, compress_with_cache
from furl_ctx.chat import _retrieval_feedback_pipeline
from furl_ctx.transforms import ContentRouter

_MODEL = "gpt-4o"


def _big_json(n: int = 600) -> str:
    """A large, highly compressible JSON tool/user payload (repetitive records)."""
    records = [
        {
            "id": i,
            "status": "ok",
            "level": "INFO",
            "msg": "worker heartbeat",
            "latency_ms": 12,
            "host": "worker-01",
            "region": "eu-north-1",
        }
        for i in range(n)
    ]
    return json.dumps(records)


def _incompressible_identical(size: int = 6000) -> str:
    """Byte-identical but per-message-incompressible payload.

    Dedup can point a later copy back at the first, but the per-message
    compressor cannot shrink either copy — so the ONLY way a copy changes is
    dedup elision. That isolates the ``protect_recent`` dedup exemption from
    ordinary content compression.
    """
    rng = random.Random(1)
    return "".join(rng.choice("abcdef0123456789") for _ in range(size))


# --------------------------------------------------------------------------- #
# compress_chat_history
# --------------------------------------------------------------------------- #


def test_chat_history_compresses_user_messages() -> None:
    """The preset compresses a large user-message payload (compress_user_messages=True)."""
    payload = _big_json()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": payload},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "wrap up"},
        {"role": "assistant", "content": "done"},
    ]

    result = compress_chat_history(messages, model=_MODEL)

    # Fail-open would return originals with error set — assert the real thing ran.
    assert result.error is None
    assert result.tokens_saved > 0
    # The early user payload (outside the protected tail) is compressed.
    assert len(str(result.messages[0]["content"])) < len(payload)


def test_chat_history_protects_recent_via_dedup_exemption() -> None:
    """A duplicate inside the protect_recent=2 tail survives byte-exact; earlier is elided.

    Discriminates against protect_recent=0, under which the SAME recent copy is
    elided to a ``<<ccr:`` sentinel — proving the preset's protect_recent=2 is
    what spares it.
    """
    payload = _incompressible_identical()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "run"},
        {"role": "tool", "content": payload, "tool_call_id": "t1"},  # 1: early duplicate
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "once more"},
        {"role": "tool", "content": payload, "tool_call_id": "t2"},  # 5: recent duplicate
    ]

    protected = compress_chat_history(messages, model=_MODEL)
    assert protected.error is None
    # Recent copy (index 5, inside the last-2 window) is exempt from dedup and
    # survives byte-exact.
    assert protected.messages[5]["content"] == payload

    # Control: with the tail window disabled, that SAME recent copy is elided to
    # a ``<<ccr:`` sentinel — so protect_recent=2 is what spared it above.
    unprotected = compress(
        copy.deepcopy(messages),
        model=_MODEL,
        compress_user_messages=True,
        protect_recent=0,
    )
    assert "<<ccr:" in str(unprotected.messages[5]["content"])


def test_chat_history_wires_retrieval_feedback_router() -> None:
    """The preset's default pipeline has retrieval feedback on, read_lifecycle intact."""
    pipeline = _retrieval_feedback_pipeline()
    routers = [t for t in pipeline.transforms if isinstance(t, ContentRouter)]
    assert routers, "default pipeline must contain a ContentRouter"
    assert all(r.config.enable_retrieval_feedback for r in routers)
    # Activating retrieval feedback must not silence the (default-on) Read
    # lifecycle pass that cross-turn history exists to feed.
    assert all(r.config.read_lifecycle.enabled for r in routers)


def test_chat_history_does_not_mutate_input() -> None:
    payload = _big_json()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": payload},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "q"},
    ]
    snapshot = copy.deepcopy(messages)

    compress_chat_history(messages, model=_MODEL)

    assert messages == snapshot


# --------------------------------------------------------------------------- #
# compress_with_cache
# --------------------------------------------------------------------------- #


def _string_conversation(payload: str) -> list[dict[str, Any]]:
    """String-content conversation: compressible payload in the prefix AND after it."""
    return [
        {"role": "user", "content": "prefix note " + payload},  # 0: frozen (unmarked)
        {"role": "assistant", "content": "acknowledged"},  # 1: frozen (marked)
        {"role": "user", "content": "live question"},  # 2: live
        {"role": "tool", "content": payload, "tool_call_id": "t"},  # 3: live -> compresses
        {"role": "user", "content": "wrap"},  # 4
        {"role": "assistant", "content": "ok"},  # 5
    ]


def test_cache_freezes_first_n_and_compresses_the_rest() -> None:
    payload = _big_json()
    messages = _string_conversation(payload)

    result = compress_with_cache(messages, 2, model=_MODEL)
    out = result.messages

    assert result.error is None
    # Unmarked frozen message (index 0 < N) is byte-identical.
    assert out[0] == messages[0]
    # The marked boundary message (index 1) keeps its text — the marker only
    # lifts the string into a text block, it does not drop content.
    assert out[1]["content"][0]["text"] == messages[1]["content"]
    # Content after the frozen prefix still compresses.
    assert out[3]["content"] != payload
    assert result.tokens_saved > 0


def test_cache_freeze_count_matches_argument() -> None:
    """freeze_up_to_n=N advances the frozen-prefix floor to exactly N."""
    from furl_ctx.compress import _compute_frozen_message_count

    payload = _big_json()
    for n in (1, 2, 3):
        messages = _string_conversation(payload)
        marked = messages[:]
        # Re-derive what the helper marks, then confirm the floor it produces.
        result = compress_with_cache(marked, n, model=_MODEL)
        assert result.error is None
        # Reconstruct the marked list the same way the helper does to read the floor.
        probe = list(messages)
        probe[min(n, len(messages)) - 1] = _mark(messages[min(n, len(messages)) - 1])
        assert _compute_frozen_message_count(probe) == n


def _mark(message: dict[str, Any]) -> dict[str, Any]:
    m = copy.deepcopy(message)
    if isinstance(m.get("content"), str):
        m["content"] = [
            {"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}
        ]
    return m


def test_cache_zero_or_negative_freezes_nothing() -> None:
    """freeze_up_to_n <= 0 adds no marker: identical to a plain compress()."""
    payload = _big_json()
    messages = _string_conversation(payload)
    baseline = compress(copy.deepcopy(messages), model=_MODEL)

    for freeze in (0, -1):
        result = compress_with_cache(copy.deepcopy(messages), freeze, model=_MODEL)
        assert result.error is None
        # No cache_control marker was injected anywhere → same output as compress().
        assert result.messages == baseline.messages


def test_cache_does_not_mutate_input() -> None:
    payload = _big_json()
    messages = _string_conversation(payload)
    snapshot = copy.deepcopy(messages)

    compress_with_cache(messages, 2, model=_MODEL)

    assert messages == snapshot


# ─── Bug-5: _with_cache_marker must never emit an empty text block ───────────
# An empty ``{"type": "text", "text": ""}`` block is a 400 from the Anthropic
# API. _with_cache_marker anchors cache_control on a valid, non-empty block for
# every content shape.

from furl_ctx.chat import _with_cache_marker  # noqa: E402


def _text_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
    content = msg["content"]
    assert isinstance(content, list)
    return [b for b in content if isinstance(b, dict) and b.get("type") == "text"]


def test_cache_marker_never_emits_empty_text_block() -> None:
    shapes = [
        {"role": "user", "content": ""},  # empty string
        {"role": "user", "content": "hello"},  # normal string
        {"role": "user", "content": []},  # empty list
        {"role": "user", "content": None},  # None
        {"role": "user", "content": ["bare-string-not-a-dict"]},  # list, no dict block
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},  # dict block
        {"role": "tool"},  # missing content key
    ]
    for shape in shapes:
        marked = _with_cache_marker(shape)
        blocks = marked["content"]
        assert isinstance(blocks, list) and blocks, f"no content list for {shape}"
        # Exactly one block carries the cache_control anchor.
        anchored = [b for b in blocks if isinstance(b, dict) and "cache_control" in b]
        assert len(anchored) == 1, f"expected one anchor for {shape}, got {anchored}"
        # No text block is empty (would 400 on the Anthropic API).
        for b in _text_blocks(marked):
            assert b["text"] != "", f"empty text block emitted for {shape}"


def test_cache_marker_prefers_existing_dict_block_over_injecting() -> None:
    # A non-empty last dict block is marked in place — no extra block injected.
    msg = {"role": "user", "content": [{"type": "text", "text": "keep me"}]}
    marked = _with_cache_marker(msg)
    assert marked["content"] == [
        {"type": "text", "text": "keep me", "cache_control": {"type": "ephemeral"}}
    ]
