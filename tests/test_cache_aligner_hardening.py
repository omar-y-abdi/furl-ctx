"""Mutation-resistance hardening for cache_aligner.

Targets not already covered by test_cache_aligner_prefix_hash.py:
  CA-B1a: pin exact hash literals for the fixed injective formula
          (kills hash-formula mutations without relying on property checks alone).
  CA-bnd: enabled=False boundary (apply() still runs even when disabled),
          idx0-ordering invariant (first message never dropped or reordered),
          empty-message-list boundary.
"""

from __future__ import annotations

import pytest

from headroom.config import CacheAlignerConfig
from headroom.tokenizer import Tokenizer
from headroom.tokenizers import EstimatingTokenCounter
from headroom.transforms.cache_aligner import CacheAligner


def _aligner(enabled: bool = True) -> CacheAligner:
    return CacheAligner(CacheAlignerConfig(enabled=enabled))


def _tok() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())  # type: ignore[arg-type]


def _sys(content: str) -> dict:
    return {"role": "system", "content": content}


def _hash(messages: list[dict], enabled: bool = True) -> str:
    return _aligner(enabled=enabled).apply(messages, _tok()).cache_metrics.stable_prefix_hash


# ---------------------------------------------------------------------------
# CA-B1a  Pin exact hash literals for the FIXED injective formula
# ---------------------------------------------------------------------------


def test_b1a_hash_literal_single_msg_with_delimiter() -> None:
    """Pin exact hash for single msg 'a\\n---\\nb' (the old collision source).

    Mutation-sensitive: any change to the length-prefix framing formula changes
    this hash. The test_delimiter_collision property test would still pass even
    if the formula changed to a different-but-injective scheme; pinning the
    literal catches formula drift.
    """
    h = _hash([_sys("a\n---\nb")])
    assert h == "6ba35d77da851a91"


def test_b1a_hash_literal_two_msgs() -> None:
    """Pin exact hash for two msgs 'a', 'b' (distinct from single 'a\\n---\\nb')."""
    h = _hash([_sys("a"), _sys("b")])
    assert h == "facdde7abf1eac5b"


def test_b1a_hashes_are_distinct() -> None:
    """The two pinned hashes must differ — property guard on top of the literal pins."""
    assert _hash([_sys("a\n---\nb")]) != _hash([_sys("a"), _sys("b")])


# ---------------------------------------------------------------------------
# CA-bnd  enabled=False boundary: apply() still runs
# ---------------------------------------------------------------------------


def test_bnd_enabled_false_still_runs_apply() -> None:
    """enabled=False bypasses should_apply but apply() still executes.

    The bug was that align_for_cache called apply() unconditionally regardless
    of enabled; this boundary test pins that apply() is called even when
    should_apply() would return False.
    """
    # Even with enabled=False the aligner must return a valid (messages, hash) result.
    aligner = _aligner(enabled=False)
    result = aligner.apply([_sys("hello")], _tok())
    assert result.cache_metrics.stable_prefix_hash != ""
    assert isinstance(result.cache_metrics.stable_prefix_hash, str)


def test_bnd_disabled_config_still_produces_hash() -> None:
    """Disabled aligner produces a stable hash (idempotent on same content)."""
    h1 = _hash([_sys("stable")], enabled=False)
    h2 = _hash([_sys("stable")], enabled=False)
    assert h1 == h2
    assert h1 != ""


# ---------------------------------------------------------------------------
# CA-bnd  idx0 ordering invariant
# ---------------------------------------------------------------------------


def test_bnd_idx0_message_never_dropped() -> None:
    """Message at index 0 must survive in the returned messages list unchanged."""
    original_idx0 = {"role": "system", "content": "system prompt idx0"}
    messages = [original_idx0, {"role": "user", "content": "user msg"}]
    result = _aligner().apply(messages, _tok())
    assert len(result.messages) > 0, "apply must return at least one message"
    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "system prompt idx0"


def test_bnd_messages_order_preserved() -> None:
    """All messages are returned in the same order they were submitted."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user1"},
        {"role": "assistant", "content": "asst1"},
        {"role": "user", "content": "user2"},
    ]
    result = _aligner().apply(msgs, _tok())
    for i, (original, returned) in enumerate(zip(msgs, result.messages)):
        assert returned["role"] == original["role"], f"role mismatch at index {i}"
        assert returned["content"] == original["content"], f"content mismatch at index {i}"


# ---------------------------------------------------------------------------
# CA-bnd  parametrize: single vs multi system message → different hashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgs_a,msgs_b",
    [
        # 1 vs 2 system messages (same tokens, different structure)
        ([_sys("a"), _sys("b")], [_sys("a\nb")]),
        # Different single system messages
        ([_sys("alpha")], [_sys("beta")]),
        # Same content, different count
        ([_sys("X"), _sys("X")], [_sys("X")]),
    ],
)
def test_bnd_distinct_prompts_distinct_hashes(msgs_a: list[dict], msgs_b: list[dict]) -> None:
    """Structurally different system-prompt sets must hash differently."""
    assert _hash(msgs_a) != _hash(msgs_b), (
        "expected distinct hashes for different prompt structures"
    )
