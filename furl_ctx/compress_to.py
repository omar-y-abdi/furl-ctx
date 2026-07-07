"""``compress_to`` — compress messages until they fit a token budget.

A thin, bounded, greedy orchestrator over :func:`compress` — no engine changes.
It walks a fixed ladder of increasingly aggressive (existing) ``compress()``
kwargs and stops at the first rung whose result fits ``max_tokens``. If even the
most aggressive rung overshoots, it returns that best (smallest) result with a
warning appended — it never raises and never silently returns over budget.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .compress import CompressConfig, CompressResult, compress
from .tokenizers import get_tokenizer

# Fixed escalation ladder over compress()'s existing kwargs, least → most
# aggressive; each rung layers one more lever onto the previous. We stop at the
# first rung that fits, so ordering is by expected impact. compress_system_messages
# is already True by default, so it is not a lever here.
# lazy: min_tokens_to_compress floors at 50 — messages below that rarely repay the
# compression/marker overhead. Lower it here if a caller needs sub-50-token turns squeezed.
_LADDER: tuple[dict[str, Any], ...] = (
    {},
    {"protect_recent": 0},
    {"protect_recent": 0, "compress_user_messages": True},
    {"protect_recent": 0, "compress_user_messages": True, "min_tokens_to_compress": 50},
    {
        "protect_recent": 0,
        "compress_user_messages": True,
        "min_tokens_to_compress": 50,
        "protect_analysis_context": False,
    },
)


def compress_to(
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    model: str = "claude-sonnet-4-5-20250929",
    model_limit: int = 200000,
    config: CompressConfig | None = None,
) -> CompressResult:
    """Compress *messages* until they fit *max_tokens*, or as close as possible.

    Returns the first laddered result whose token count is ``<= max_tokens``. If
    none fits, returns the smallest result with a ``warnings`` entry noting the
    shortfall — check ``result.tokens_after`` (or count ``result.messages``)
    rather than assuming success.
    """
    counter = get_tokenizer(model)

    # Measure the real output every time — never trust result.tokens_after, which
    # is 0 on the fail-open path (original messages returned with error set) and
    # would falsely read as "fits".
    if counter.count_messages(messages) <= max_tokens:
        return CompressResult(messages=messages)  # already fits — genuine no-op

    best: CompressResult | None = None
    best_tokens = -1
    for overrides in _LADDER:
        result = compress(
            messages, model=model, model_limit=model_limit, config=config, **overrides
        )
        tokens = counter.count_messages(result.messages)
        if tokens <= max_tokens:
            return result
        if best is None or tokens < best_tokens:
            best, best_tokens = result, tokens

    assert best is not None  # the ladder is non-empty, so best is always set
    over = best_tokens - max_tokens
    return replace(
        best,
        warnings=[
            *best.warnings,
            f"compress_to: budget {max_tokens} not met, {over} tokens over after max compression",
        ],
    )
