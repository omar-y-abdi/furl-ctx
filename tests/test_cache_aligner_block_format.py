"""COR-53: format-aware CacheAligner + ContentRouter analysis-intent detection.

The aligner was format-blind: block-format system prompts (Anthropic-style
``content=[{"type": "text", "text": ...}, ...]`` lists) were excluded from
``stable_prefix_hash``, so two entirely different block-format prompts hashed
identically and ``prefix_changed`` reported False — confidently wrong
stability data for any dashboard consuming ``cache_metrics``. ``should_apply``,
volatile-content detection, and ``get_alignment_score`` were blind the same
way, and ``ContentRouter._detect_analysis_intent`` never saw analysis keywords
in block-format user messages.

Fix: the shared ``furl_ctx.utils.concat_text_parts`` helper concatenates the
text parts of block-format content. Plain-string content passes through
unchanged, so existing str-format hashes stay byte-identical — pinned by the
CA-B1a hash literals in test_cache_aligner_hardening.py.

Invariant guard: the aligner stays detector-only on the new code path —
block-format messages are returned unchanged, in order (prompt-cache prefix
ordering is a hard invariant).
"""

from __future__ import annotations

import logging
from typing import Any

from furl_ctx.config import CacheAlignerConfig
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.cache_aligner import CacheAligner
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.utils import concat_text_parts


def _aligner(enabled: bool = True) -> CacheAligner:
    return CacheAligner(CacheAlignerConfig(enabled=enabled))


def _tok() -> Tokenizer:
    return Tokenizer(EstimatingTokenCounter())  # type: ignore[arg-type]


def _sys(content: str) -> dict:
    return {"role": "system", "content": content}


def _sys_blocks(*texts: str) -> dict:
    """Block-format system message with one text block per argument."""
    return {
        "role": "system",
        "content": [{"type": "text", "text": t} for t in texts],
    }


def _hash(messages: list[dict]) -> str:
    return _aligner().apply(messages, _tok()).cache_metrics.stable_prefix_hash


# ── concat_text_parts: the shared helper ────────────────────────────────


def test_concat_str_passthrough_is_identity() -> None:
    # str content must round-trip byte-identically — this is what keeps the
    # pinned CA-B1a str-format hash literals valid after the fix.
    assert concat_text_parts("You are helpful.\n---\nBe terse.") == (
        "You are helpful.\n---\nBe terse."
    )
    assert concat_text_parts("") == ""


def test_concat_joins_text_blocks_in_order() -> None:
    content = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    assert concat_text_parts(content) == "first\nsecond"


def test_concat_skips_non_text_and_malformed_blocks() -> None:
    content: list[Any] = [
        {"type": "image", "source": {"data": "AAAA"}},  # non-text block
        {"type": "text", "text": "kept"},
        {"type": "text", "text": 42},  # malformed: text is not a str
        "bare string entry",  # malformed: not a dict
        {"type": "text"},  # malformed: no text field
    ]
    assert concat_text_parts(content) == "kept"


def test_concat_is_total_on_junk_shapes() -> None:
    # Never raises, always returns a str — untrusted input at the boundary.
    for junk in (None, 42, {"type": "text", "text": "x"}, object(), [], [None]):
        assert concat_text_parts(junk) == ""


# ── stable_prefix_hash: block-format false negatives (the core COR-53 bug) ──


def test_different_block_prompts_hash_differently() -> None:
    # Two ENTIRELY different block-format system prompts. Under the str-only
    # bug both were excluded from the framing, hashed identically, and
    # prefix_changed stayed False.
    h_a = _hash([_sys_blocks("You are a pirate.")])
    h_b = _hash([_sys_blocks("You are a tax auditor.")])
    assert h_a != h_b, "block-format prompts are invisible to the prefix hash (COR-53)"


def test_block_prompt_changes_hash_of_mixed_prompt_set() -> None:
    # Precise repro: adding a block-format system message to a plain one must
    # change the hash. Before the fix both sets framed only "plain".
    plain_only = _hash([_sys("plain")])
    with_block = _hash([_sys("plain"), _sys_blocks("blocky")])
    assert plain_only != with_block


def test_same_block_prompt_hashes_deterministically() -> None:
    msgs = [_sys_blocks("stable block prompt")]
    assert _hash(msgs) == _hash(msgs)


def test_block_prompt_hash_matches_str_equivalent() -> None:
    # Concatenation semantics: a block-format prompt hashes as its joined
    # text parts, so a format-only change (str ↔ blocks) does not flip
    # prefix_changed. Multi-part blocks join with a newline.
    assert _hash([_sys_blocks("You are helpful.")]) == _hash([_sys("You are helpful.")])
    assert _hash([_sys_blocks("a", "b")]) == _hash([_sys("a\nb")])


def test_block_format_prefix_changed_flips() -> None:
    aligner = _aligner()
    first = aligner.apply([_sys_blocks("prompt v1")], _tok())
    second = aligner.apply(
        [_sys_blocks("prompt v2")],
        _tok(),
        previous_prefix_hash=first.cache_metrics.stable_prefix_hash,
    )
    assert second.cache_metrics.prefix_changed is True, (
        "a changed block-format prompt must be reported as a prefix change"
    )


def test_block_prefix_metrics_count_block_text() -> None:
    # bytes / token estimates must see block text (they reported 0 before).
    block = _aligner().apply([_sys_blocks("hello world")], _tok()).cache_metrics
    plain = _aligner().apply([_sys("hello world")], _tok()).cache_metrics
    assert block.stable_prefix_bytes == plain.stable_prefix_bytes > 0
    assert block.stable_prefix_tokens_est == plain.stable_prefix_tokens_est > 0


# ── should_apply / detection / score: same blindness, same fix ──────────


def test_should_apply_true_for_block_system_prompt() -> None:
    assert _aligner().should_apply([_sys_blocks("hello")], _tok()) is True


def test_should_apply_false_for_textless_block_system_prompt() -> None:
    image_only = {
        "role": "system",
        "content": [{"type": "image", "source": {"data": "AAAA"}}],
    }
    assert _aligner().should_apply([image_only], _tok()) is False
    assert _aligner().should_apply([{"role": "system", "content": []}], _tok()) is False


def test_block_volatile_content_emits_warning(caplog) -> None:
    msgs = [
        _sys_blocks("Session 3fa85f64-5717-4562-b3fc-2c963f66afa6 started at 2024-01-15T10:30:00Z")
    ]
    with caplog.at_level(logging.WARNING, logger="furl_ctx.transforms.cache_aligner"):
        result = _aligner().apply(msgs, _tok())
    assert result.warnings, "volatile content in block-format prompts must warn"
    assert "uuid" in result.warnings[0]
    assert "iso8601" in result.warnings[0]
    assert any("volatile content" in rec.getMessage() for rec in caplog.records)


def test_alignment_score_penalizes_block_volatile_content() -> None:
    volatile = [_sys_blocks("id 3fa85f64-5717-4562-b3fc-2c963f66afa6")]
    assert _aligner().get_alignment_score(volatile) < 100.0
    assert _aligner().get_alignment_score([_sys_blocks("no dynamic content here")]) == 100.0


# ── detector-only invariant on the new path ─────────────────────────────


def test_block_messages_returned_unchanged_in_order() -> None:
    msgs = [
        _sys_blocks("block sys prompt 3fa85f64-5717-4562-b3fc-2c963f66afa6"),
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": "hello"},
    ]
    result = _aligner().apply(msgs, _tok())
    assert result.messages == msgs, "detector-only: block-format messages must not be rewritten"
    assert result.transforms_applied == []


# ── ContentRouter._detect_analysis_intent: second consumer of the helper ──


def test_analysis_intent_detected_in_block_format_user_message() -> None:
    router = ContentRouter(ContentRouterConfig())
    msgs = [
        _sys("system"),
        {
            "role": "user",
            "content": [{"type": "text", "text": "Please review this code for bugs."}],
        },
    ]
    assert router._detect_analysis_intent(msgs) is True


def test_analysis_intent_absent_in_block_format_without_keywords() -> None:
    router = ContentRouter(ContentRouterConfig())
    msgs = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Write a haiku about spring."}],
        },
    ]
    assert router._detect_analysis_intent(msgs) is False


def test_analysis_intent_still_only_reads_latest_user_message() -> None:
    # Existing semantics preserved: only the most recent user message is
    # consulted; earlier keyword-bearing messages do not trigger.
    router = ContentRouter(ContentRouterConfig())
    msgs = [
        {"role": "user", "content": "please review and audit this"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": [{"type": "text", "text": "now a haiku please"}]},
    ]
    assert router._detect_analysis_intent(msgs) is False
