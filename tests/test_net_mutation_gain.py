"""net_mutation_gain (NR2-4): cache-economics gate — model + router wiring.

The pure model: compressing message i saves S tokens but re-bills the
``tokens_after`` suffix at full rate instead of the provider's cached rate —
``net = S - tokens_after * (1 - cached_rate)``; the router skips compressions
with net <= 0. Default OFF (`enable_net_mutation_gate`): the committed bench
cannot regress — the flag-off path never even computes the suffix sums,
pinned here against the flag-on outcome flipping.

The gate is POSITION-dependent while the result cache is content-keyed, so
it is enforced at BOTH serve sites (fresh Pass-3 accepts and Tier-2 cache
hits) — the cache-hit case is pinned explicitly.
"""

from __future__ import annotations

import dataclasses

import pytest

from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.net_mutation_gain import (
    CACHED_TOKEN_RATE,
    MutationContext,
    net_mutation_gain,
)

_TOKENIZER = get_tokenizer("claude-sonnet-4-5-20250929")


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FURL_WORKSPACE_DIR", str(tmp_path))
    reset_compression_store()
    yield
    reset_compression_store()


# ─── pure model ──────────────────────────────────────────────────────────────


class TestNetMutationGainModel:
    def test_positive_gain_when_savings_beat_rebilling(self) -> None:
        # saved 100, suffix 50 at rate 0.1 → penalty 45 → net 55.
        assert net_mutation_gain(100, MutationContext(50), 0.1) == pytest.approx(55.0)

    def test_negative_gain_when_suffix_dominates(self) -> None:
        # saved 10, suffix 1000 at rate 0.1 → penalty 900 → net -890.
        assert net_mutation_gain(10, MutationContext(1000), 0.1) == pytest.approx(-890.0)

    def test_zero_suffix_means_gain_equals_savings(self) -> None:
        assert net_mutation_gain(42, MutationContext(0), 0.1) == pytest.approx(42.0)

    @pytest.mark.parametrize(
        ("saved", "expected_gain"),
        [
            # penalty = 100 * (1 - 0.1) = 90, so saved=90 lands EXACTLY on 0.
            (91, 1.0),  # just above the crossing
            (90, 0.0),  # exactly on it
            (89, -1.0),  # just below it
        ],
    )
    def test_gain_value_at_zero_crossing(self, saved: int, expected_gain: float) -> None:
        # Pins the exact gain at and around the net==0 crossing, so an arithmetic
        # mutation (wrong penalty factor / dropped subtraction) is caught. The
        # router treats net <= 0 as skip; the exact `<= 0` vs `< 0` decision at
        # net==0 is a documented residual (see module note) — no realistic
        # real-token router scenario lands exactly on 0 to exercise it.
        assert net_mutation_gain(saved, MutationContext(100), 0.1) == pytest.approx(expected_gain)

    def test_unknowable_context_returns_none(self) -> None:
        assert net_mutation_gain(100, MutationContext(None), 0.1) is None

    @pytest.mark.parametrize(
        ("rate", "expected"),
        [
            (0.0, 100.0 - 200.0),  # nothing was discounted-cached → full re-bill
            (0.1, 100.0 - 180.0),
            (0.5, 100.0 - 100.0),  # half-rate cache → half penalty → break-even
        ],
    )
    def test_cached_rate_sensitivity(self, rate: float, expected: float) -> None:
        assert net_mutation_gain(100, MutationContext(200), rate) == pytest.approx(expected)

    def test_default_rate_is_the_module_constant(self) -> None:
        assert net_mutation_gain(100, MutationContext(200)) == pytest.approx(
            net_mutation_gain(100, MutationContext(200), CACHED_TOKEN_RATE)
        )

    def test_context_is_immutable(self) -> None:
        ctx = MutationContext(10)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.tokens_after = 20  # type: ignore[misc]


# ─── router wiring ───────────────────────────────────────────────────────────


def compressible_log() -> str:
    """Tool output the LOG arm reliably compresses under the real tokenizer
    (same shape as test_log_template_dispatch's templatable fixture).

    200 lines: ratio ≈ 0.896 in o200k_base (< min_ratio 0.90 → accepted).
    40 lines was sufficient with the old 3.5-cpt estimate but the BPE
    template wire overhead requires more repetition to break even (Q1).
    """
    lines = [
        f"INFO [worker-{i % 4}] processed batch id={1000 + i} "
        f"rows={i * 3} status=ok latency={i}.{i}ms"
        for i in range(200)
    ]
    return "\n".join(lines)


def _tool_msg(text: str) -> dict:
    return {"role": "tool", "content": text, "tool_call_id": "tc_gate"}


def _user_filler(seed: int, repeats: int = 400) -> dict:
    # Large UNIQUE user prose: user messages are never compressed (skip_user)
    # but their tokens count toward the suffix a mutation would re-bill.
    words = " ".join(f"filler{seed}word{j}" for j in range(repeats))
    return {"role": "user", "content": words}


def _apply(messages: list[dict], **config_kwargs) -> tuple[list[dict], list[str]]:
    router = ContentRouter(ContentRouterConfig(**config_kwargs))
    result = router.apply(messages, _TOKENIZER)
    return result.messages, result.transforms_applied


def _is_compression_transform(t: str) -> bool:
    # An ACCEPTED compression books "router:{strategy}:{ratio}"; bookkeeping
    # entries ("router:noop:{reason}", "router:protected:user_message", ...) are
    # not compressions.
    return (
        t.startswith("router:")
        and not t.startswith("router:protected:")
        and not t.startswith("router:noop")
    )


class TestRouterGate:
    def test_flag_off_compresses_despite_huge_suffix(self) -> None:
        """Default OFF = today's behavior: the suffix is never priced."""
        content = compressible_log()
        msgs, transforms = _apply(
            [_tool_msg(content), _user_filler(1), _user_filler(2)],
        )
        assert msgs[0]["content"] != content, "baseline: this fixture must compress"
        assert any(_is_compression_transform(t) for t in transforms)

    def test_flag_on_big_suffix_gates_the_compression(self) -> None:
        """Early compressible message + large suffix → net loss → served raw."""
        content = compressible_log()
        msgs, transforms = _apply(
            [_tool_msg(content), _user_filler(1), _user_filler(2)],
            enable_net_mutation_gate=True,
        )
        assert msgs[0]["content"] == content, "gate must reject the compression"
        assert not any(_is_compression_transform(t) for t in transforms)

    def test_flag_on_last_position_compresses(self) -> None:
        """Same bytes LAST (tokens_after=0): gain == savings > 0 → compress."""
        content = compressible_log()
        msgs, _ = _apply(
            [_user_filler(1), _user_filler(2), _tool_msg(content)],
            enable_net_mutation_gate=True,
        )
        assert msgs[2]["content"] != content, "no suffix → nothing to re-bill → compress"

    def test_flag_on_single_message_compresses(self) -> None:
        content = compressible_log()
        msgs, _ = _apply([_tool_msg(content)], enable_net_mutation_gate=True)
        assert msgs[0]["content"] != content

    def test_gate_applies_to_cache_hits_positionally(self) -> None:
        """Content-keyed cache, position-dependent gate: the SAME bytes must
        compress at the end of one conversation and stay raw early in the
        next — the Tier-2 cache-hit serve site re-evaluates the gate."""
        content = compressible_log()
        router = ContentRouter(ContentRouterConfig(enable_net_mutation_gate=True))

        # Call 1: last position → compresses → result lands in the cache.
        first = router.apply([_user_filler(1), _tool_msg(content)], _TOKENIZER)
        assert first.messages[1]["content"] != content

        # Call 2, same router (warm cache): same bytes EARLY + big suffix →
        # the cached win must be rejected at serve time.
        second = router.apply([_tool_msg(content), _user_filler(2), _user_filler(3)], _TOKENIZER)
        assert second.messages[0]["content"] == content

    def test_rate_one_disables_the_penalty(self) -> None:
        """cached_token_rate=1.0 → (1 - rate) = 0 → penalty 0 → gate never
        fires: sensitivity wiring reaches the config field."""
        content = compressible_log()
        msgs, _ = _apply(
            [_tool_msg(content), _user_filler(1), _user_filler(2)],
            enable_net_mutation_gate=True,
            cached_token_rate=1.0,
        )
        assert msgs[0]["content"] != content
