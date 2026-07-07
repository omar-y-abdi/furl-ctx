"""compress_to: fit-to-budget orchestration over compress()."""

import json

from furl_ctx import compress_to
from furl_ctx.tokenizers import get_tokenizer

_MODEL = "claude-sonnet-4-5-20250929"


def _big_tool_msgs(n: int = 600) -> list[dict[str, object]]:
    """A large, highly compressible tool output (repetitive JSON records)."""
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
    return [{"role": "tool", "content": json.dumps(records)}]


def test_already_fits_is_a_noop() -> None:
    msgs = [{"role": "tool", "content": "small output"}]
    result = compress_to(msgs, max_tokens=10_000)
    assert result.messages == msgs  # returned unchanged
    assert result.error is None


def test_over_budget_converges_under_max() -> None:
    msgs = _big_tool_msgs()
    counter = get_tokenizer(_MODEL)
    before = counter.count_messages(msgs)
    budget = before // 2
    result = compress_to(msgs, max_tokens=budget)
    after = counter.count_messages(result.messages)
    assert after <= budget
    assert after < before  # compression actually engaged


def test_impossible_budget_returns_best_effort_with_warning() -> None:
    result = compress_to(_big_tool_msgs(), max_tokens=5)  # unreachable
    assert any("budget 5 not met" in w for w in result.warnings)
    assert result.messages  # best-effort, non-empty; did not raise or loop forever


def test_input_messages_not_mutated() -> None:
    msgs = _big_tool_msgs()
    snapshot = [dict(m) for m in msgs]
    compress_to(msgs, max_tokens=100)
    assert msgs == snapshot
