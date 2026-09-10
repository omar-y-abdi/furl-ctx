"""net_mutation_gain — cache-economics model for compression decisions.

Compressing a message MUTATES the conversation prefix at that point: every
token AFTER the mutated message loses its provider prefix-cache discount and
is re-billed at the full input rate on the next request. When that re-billing
cost exceeds the tokens the compression saves, compressing is a net LOSS.

Scope (why this is about IMPLICIT caching only): ``compress()`` already
freezes the prefix up to the highest explicit ``cache_control`` marker —
explicitly cached messages are never mutated. This model targets marker-less
deployments where the provider auto-caches prefixes (e.g. OpenAI's automatic
prefix caching for prompts >= 1024 tokens), which Furl cannot see. The gate
is therefore an owner opt-in (`enable_net_mutation_gate`, default off), and
the math is an honest ESTIMATE: it assumes the ENTIRE suffix was cached, so
the computed loss is an upper bound.

Pure module: no I/O, no config reads, no logging — the router owns the
decision wiring; this module owns only the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass

# Provider cache-read pricing relative to the base input rate. Anthropic bills cache reads at 0.1x base; OpenAI at 0.25-0.5x depending on model.
CACHED_TOKEN_RATE: float = 0.1


@dataclass(frozen=True)
class MutationContext:
    """Positional facts about the message a compression would mutate.

    ``tokens_after`` is the token count of everything AFTER the candidate
    message in the conversation — the suffix that loses its cache discount
    if the candidate's bytes change. ``None`` means the caller cannot know
    the suffix size (no message-list context); the gate must then stay out
    of the way entirely (:func:`net_mutation_gain` returns ``None``).
    """

    tokens_after: int | None


def net_mutation_gain(
    saved_tokens: int,
    ctx: MutationContext,
    cached_rate: float = CACHED_TOKEN_RATE,
) -> float | None:
    """Net token benefit of a compression, priced against cache re-billing.

    Model: the compression saves ``saved_tokens`` outright, but mutating the
    message re-bills the ``ctx.tokens_after`` suffix at full rate instead of
    ``cached_rate`` — a penalty of ``tokens_after * (1 - cached_rate)``
    token-equivalents. Positive result: compression pays for itself even if
    the whole suffix was cached. Result <= 0: skip.

    Total function: ``None`` iff the context is unknowable
    (``ctx.tokens_after is None``) — never raises, never guesses.
    """
    if ctx.tokens_after is None:
        return None
    return float(saved_tokens) - float(ctx.tokens_after) * (1.0 - cached_rate)
