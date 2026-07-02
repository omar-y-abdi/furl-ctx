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

from furl_ctx import compress
from furl_ctx.cache.compression_store import get_compression_store
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.cross_message_dedup import (
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
    # The duplicate sits at index 3; the trailing turns keep it OUTSIDE the
    # default ``protect_recent`` window (4), so the public compress() path is
    # allowed to elide it (dedup never replaces inside that window).
    return [
        {"role": "user", "content": "Run the test suite."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t1"},
        {"role": "user", "content": "Run it again to confirm."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t2"},
        {"role": "assistant", "content": "Both runs pass with identical output."},
        {"role": "user", "content": "Any flakes in the second run?"},
        {"role": "assistant", "content": "None — the outputs are byte-identical."},
        {"role": "user", "content": "Good, wrap up."},
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
        {"role": "assistant", "content": "No — the rerun matches the first output."},
        {"role": "user", "content": "Run the linter next."},
        {"role": "assistant", "content": "Linter queued."},
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
    from furl_ctx.transforms import TransformPipeline

    names = [t.name for t in TransformPipeline().transforms]
    assert "cross_message_dedup" in names
    assert names.index("cross_message_dedup") < names.index("content_router")


# --------------------------------------------------------------------------- #
# Near-duplicate tier: shared rows elided, differing rows kept, recoverable.
# --------------------------------------------------------------------------- #

_NEAR_RE = re.compile(r"<<ccr:([0-9a-f]{24}) (\d+)_bytes_near_duplicate>>")


def _status_rows(n: int, *, generation: int, drift_every: int = 10) -> list[dict[str, Any]]:
    # Service-status-style rows: most are stable across polls, a few drift.
    return [
        {
            "service": f"svc-{i:02d}",
            "state": "running",
            "restarts": i % 3,
            "uptime_s": 86_400 + i * 17 + (generation * 1009 if i % drift_every == 0 else 0),
        }
        for i in range(n)
    ]


def test_near_duplicate_array_ships_only_differing_rows() -> None:
    # High overlap (18/20 rows shared): the rewrite beats even the
    # counterfactual per-message lossless rendering, so the tier fires.
    rows_a = _status_rows(20, generation=0)
    rows_b = _status_rows(20, generation=1)  # rows 0,10 drift; 18 identical
    content_a = json.dumps(rows_a, ensure_ascii=False)
    content_b = json.dumps(rows_b, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Check service status."},
        {"role": "tool", "content": content_a, "tool_call_id": "s1"},
        {"role": "user", "content": "Poll it again."},
        {"role": "tool", "content": content_b, "tool_call_id": "s2"},
    ]

    result = _apply(messages)

    assert result.transforms_applied == ["cross_message_dedup:near:1"]
    # First occurrence verbatim.
    assert result.messages[1]["content"] == content_a

    rendered = json.loads(result.messages[3]["content"])
    assert isinstance(rendered, list)
    sentinel = rendered[-1]
    assert set(sentinel) == {"_ccr_dropped"}
    match = _NEAR_RE.search(sentinel["_ccr_dropped"])
    assert match, "near-dup sentinel must surface the pointer"
    assert "message 1" in sentinel["_ccr_dropped"]
    # Pinned against any further compression pass.
    assert "Retrieve original: hash=" in sentinel["_ccr_dropped"]

    # Exactly the differing rows ship, in original order.
    changed = [row for row in rows_b if row not in rows_a]
    assert rendered[:-1] == changed
    assert len(changed) == 2

    # Recovery is byte-exact for the FULL original.
    entry = get_compression_store().retrieve(match.group(1))
    assert entry is not None
    assert entry.original_content == content_b

    # Every shared row stays visible in the untouched first occurrence.
    shared = [row for row in rows_b if row in rows_a]
    for row in shared:
        assert json.dumps(row, ensure_ascii=False) in result.messages[1]["content"]


def test_near_duplicate_low_overlap_untouched() -> None:
    rows_a = _status_rows(9, generation=0)
    # Only one row in common — below NEAR_DUP_MIN_SHARED_ROWS.
    rows_b = [rows_a[0]] + _status_rows(8, generation=7)[1:]
    for row in rows_b[1:]:
        row["service"] = row["service"] + "-alt"
    messages = [
        {"role": "user", "content": "Check service status."},
        {"role": "tool", "content": json.dumps(rows_a), "tool_call_id": "s1"},
        {"role": "tool", "content": json.dumps(rows_b), "tool_call_id": "s2"},
    ]
    before = [_msg_bytes(m) for m in messages]

    result = _apply(messages)

    assert [_msg_bytes(m) for m in result.messages] == before
    assert result.transforms_applied == []


def test_near_duplicate_counterfactual_gate_refuses_moderate_overlap() -> None:
    # 6/9 rows shared passes the overlap gates but the rewrite (3 raw JSON
    # rows + sentinel) would cost MORE than the per-message lossless table
    # of all 9 rows — the counterfactual gate must refuse. This is the
    # measured real case: the drifted `df -k` pair regressed 347 -> ~430
    # tokens before this gate existed.
    rows_a = _status_rows(9, generation=0, drift_every=4)
    rows_b = _status_rows(9, generation=1, drift_every=4)  # rows 0,4,8 drift
    messages = [
        {"role": "user", "content": "Check service status."},
        {"role": "tool", "content": json.dumps(rows_a), "tool_call_id": "s1"},
        {"role": "tool", "content": json.dumps(rows_b), "tool_call_id": "s2"},
    ]
    before = [_msg_bytes(m) for m in messages]

    result = _apply(messages)

    assert [_msg_bytes(m) for m in result.messages] == before
    assert result.transforms_applied == []


def test_near_duplicate_source_must_be_kept_verbatim() -> None:
    # b is an exact duplicate of a (elided); c near-duplicates both. The
    # pointer must name message 1 (kept verbatim), never message 2 (a
    # sentinel after replacement).
    rows_a = _status_rows(20, generation=0)
    rows_c = _status_rows(20, generation=1)
    content_a = json.dumps(rows_a, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Check service status."},
        {"role": "tool", "content": content_a, "tool_call_id": "s1"},
        {"role": "tool", "content": content_a, "tool_call_id": "s2"},
        {"role": "tool", "content": json.dumps(rows_c, ensure_ascii=False), "tool_call_id": "s3"},
    ]

    result = _apply(messages)

    assert sorted(result.transforms_applied) == [
        "cross_message_dedup:exact:1",
        "cross_message_dedup:near:1",
    ]
    near_sentinel = json.loads(result.messages[3]["content"])[-1]
    assert "message 1" in near_sentinel["_ccr_dropped"]


# --------------------------------------------------------------------------- #
# protect_recent window (COR-52).
# --------------------------------------------------------------------------- #


def test_protect_recent_window_never_replaced() -> None:
    # The newest tool output is the costliest place to force a retrieval
    # round-trip: inside the window nothing is replaced, so the pass is a
    # no-op here.
    messages = [
        {"role": "user", "content": "Run the tests."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t1"},
        {"role": "user", "content": "Run them again."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t2"},
    ]

    result = _apply(messages, protect_recent=4)

    assert result.messages[3]["content"] == PAYLOAD
    assert result.transforms_applied == []


def test_duplicate_outside_protect_recent_window_still_replaced() -> None:
    # The window is a suffix (router accounting: len(messages) - index <= N).
    # A later duplicate outside it is elided; the one inside it is kept.
    messages = [
        {"role": "user", "content": "Run the tests."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t1"},
        {"role": "user", "content": "Run them again."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t2"},  # from_end=3
        {"role": "user", "content": "And once more."},
        {"role": "tool", "content": PAYLOAD, "tool_call_id": "t3"},  # from_end=1
    ]

    result = _apply(messages, protect_recent=2)

    assert result.messages[1]["content"] == PAYLOAD, "first occurrence verbatim"
    assert _DUP_RE.search(str(result.messages[3]["content"])), "outside window: elided"
    assert result.messages[5]["content"] == PAYLOAD, "inside window: kept"
    assert result.transforms_applied == ["cross_message_dedup:exact:1"]


# --------------------------------------------------------------------------- #
# Encoding totality: lone surrogates must never raise (COR-42).
# --------------------------------------------------------------------------- #


def test_lone_surrogate_duplicate_never_crashes() -> None:
    # json.loads legally yields lone surrogates from \ud8xx escapes (and
    # surrogateescape decoding yields them from any non-UTF-8 byte). One
    # weird byte may cost one skipped unit — never an exception (which would
    # fail this and EVERY subsequent request while the message stays in
    # history).
    payload = PAYLOAD + "\udcef"
    messages = [
        {"role": "user", "content": "Read the log."},
        {"role": "tool", "content": payload, "tool_call_id": "t1"},
        {"role": "user", "content": "Read it again."},
        {"role": "tool", "content": payload, "tool_call_id": "t2"},
    ]

    result = _apply(messages)  # must not raise

    assert result.messages[1]["content"] == payload, "first occurrence verbatim"
    later = result.messages[3]["content"]
    if later != payload:  # replaced → the pointer must be recoverable
        match = _DUP_RE.search(str(later))
        assert match, "replacement must surface the ccr pointer"
        entry = get_compression_store().retrieve(match.group(1))
        assert entry is not None
        assert entry.original_content == payload


def test_lone_surrogate_near_duplicate_never_crashes() -> None:
    # Same totality contract on the near-dup tier (row signatures, the byte
    # gates and the rendering all re-encode the surrogate).
    rows_a = _status_rows(20, generation=0)
    rows_b = [dict(row) for row in rows_a]
    rows_b[0] = {**rows_b[0], "detail": "drifted \udcef row"}
    content_a = json.dumps(rows_a, ensure_ascii=False)
    content_b = json.dumps(rows_b, ensure_ascii=False)
    messages = [
        {"role": "user", "content": "Check service status."},
        {"role": "tool", "content": content_a, "tool_call_id": "s1"},
        {"role": "user", "content": "Poll it again."},
        {"role": "tool", "content": content_b, "tool_call_id": "s2"},
    ]

    result = _apply(messages)  # must not raise

    assert result.messages[1]["content"] == content_a, "first occurrence verbatim"
