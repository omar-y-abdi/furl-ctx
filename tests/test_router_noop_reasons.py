"""Every router no-op must explain itself (``router:noop:{reason}``).

Regression guard for the evaluator report where ``furl_compress`` returned 0%
savings with a bare, unexplained ``router:noop`` and the product read as broken.
Two properties are pinned:

1. **No silent no-op.** Whenever the router ships everything through untouched,
   ``transforms_applied`` carries a machine-readable reason suffix (never the
   bare ``router:noop``), drawn from a fixed vocabulary derived from the
   route-count lanes that fired (the transform-less shape/pinning lanes fold
   into the umbrella ``no_eligible_content``).
2. **Structured content that CAN compress still does.** A realistic KEY=value
   env dump and a 60x-repeated line are reduced (via TEXT / SmartCrusher), so
   the reason work did not silence real compression. Only genuinely
   incompressible content (unique keys AND unique values, prose the extractor
   can't shrink) no-ops — and then it says ``no_savings``, honestly.

Payload shapes reconstruct the three the evaluator ran (a repeated lorem line,
the head of the repo's own CHANGELOG.md, a ~5 KB KEY=value dump) plus the
synthetic lane triggers. No real filesystem paths or secret-shaped strings are
embedded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from furl_ctx.compress import compress
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.router_policy import noop_transform

_MODEL = "claude-sonnet-4-5-20250929"

# The full machine-readable vocabulary a router no-op may carry.
_KNOWN_REASONS = frozenset(
    {
        "no_savings",
        "below_min_tokens",
        "net_mutation_gate",
        "non_string",
        "already_compressed",
        "no_eligible_content",
    }
)


# --------------------------------------------------------------------------- #
# Payload builders — deterministic, self-contained, no real paths/secrets.
# --------------------------------------------------------------------------- #
def _repeated_line(n: int = 60) -> str:
    return "\n".join(["Lorem ipsum dolor sit amet, consectetur adipiscing."] * n)


def _changelog_head(n_chars: int = 2179) -> str:
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    return changelog.read_text(encoding="utf-8")[:n_chars]


def _env_dump_realistic() -> str:
    """~5 KB KEY=value dump with the redundancy a real ``printenv`` carries:
    repeated key prefixes, recurring values, section comments, blank lines."""
    tmpl = [
        ("URL", "postgres://svc:pw@host-{i}.internal:5432/db{i}"),
        ("POOL_SIZE", "16"),
        ("TIMEOUT_SECONDS", "30"),
        ("SSL_MODE", "require"),
        ("MAX_RETRIES", "3"),
        ("REGION", "us-east-1"),
    ]
    prefixes = [
        "DATABASE",
        "REDIS",
        "HTTP",
        "CACHE",
        "QUEUE",
        "AUTH",
        "BILLING",
        "SEARCH",
        "STORAGE",
        "EMAIL",
        "SMS",
        "ANALYTICS",
        "CDN",
        "PROXY",
        "WORKER",
        "SCHEDULER",
        "METRICS",
        "TRACING",
        "FLAGS",
        "S3",
        "SQS",
        "KMS",
    ]
    lines: list[str] = []
    for i, pfx in enumerate(prefixes):
        lines.append(f"# {pfx} configuration")
        for key, val in tmpl:
            lines.append(f"{pfx}_{key}={val.format(i=i)}")
        lines.append("")
    return "\n".join(lines)


def _env_dump_incompressible(n: int = 100) -> str:
    """Pathological KEY=value dump: unique key AND high-entropy unique value on
    every line, sized (~3 KB, ~1.6 K tokens) below the 4 KB reversible-offload
    floor. No faithful (lossless or reversible-preview) reduction exists — the
    evaluator's hard case. The router's honest outcome is a no-op, and it must
    SAY so, not fall silent."""
    return "\n".join(
        f"APP_PARAM_{i}={hashlib.sha256(str(i).encode()).hexdigest()[:20]}" for i in range(n)
    )


def _compress_tool(content: str):
    """The faithful MCP ``furl_compress`` shape: one tool message, real
    tokenizer, process-default pipeline."""
    return compress([{"role": "tool", "content": content}], model=_MODEL)


def _noop_reasons(transforms: list[str]) -> list[str]:
    return [t for t in transforms if t.startswith("router:noop")]


# --------------------------------------------------------------------------- #
# 1. The pure reason-mapping (no mocks — a total function of route_counts).
# --------------------------------------------------------------------------- #
class TestNoopTransformPure:
    @pytest.mark.parametrize(
        ("route_counts", "expected"),
        [
            ({"ratio_too_high": 1}, "router:noop:no_savings"),
            ({"small": 1}, "router:noop:below_min_tokens"),
            ({"net_mutation_gate": 1}, "router:noop:net_mutation_gate"),
            ({"non_string": 1}, "router:noop:non_string"),
            ({"already_compressed": 1}, "router:noop:already_compressed"),
            # shape/pinning lanes booked without a transform → umbrella reason
            ({"content_blocks": 1}, "router:noop:no_eligible_content"),
            ({"nested_blocks": 1}, "router:noop:no_eligible_content"),
            ({"cache_control_protected": 1}, "router:noop:no_eligible_content"),
            ({}, "router:noop:no_eligible_content"),
            # all-zero pre-seeded lanes (a fully-frozen prefix) → catch-all
            ({"small": 0, "ratio_too_high": 0}, "router:noop:no_eligible_content"),
            # cache bookkeeping alone never explains a no-op
            ({"cache_hit": 3}, "router:noop:no_eligible_content"),
        ],
    )
    def test_reason_mapping(self, route_counts: dict[str, int], expected: str) -> None:
        assert noop_transform(route_counts) == expected

    def test_prefix_preserved_for_every_output(self) -> None:
        # summarize_routing_markers() and the MCP display match on this prefix.
        for rc in ({"ratio_too_high": 1}, {"small": 1}, {}, {"non_string": 1}):
            assert noop_transform(rc).startswith("router:noop")

    def test_dominant_lane_wins_across_a_batch(self) -> None:
        # 3 small vs 1 ratio_too_high → the small lane dominates.
        assert noop_transform({"small": 3, "ratio_too_high": 1}) == "router:noop:below_min_tokens"
        assert noop_transform({"small": 1, "ratio_too_high": 3}) == "router:noop:no_savings"

    def test_shape_lane_dominance_is_over_all_noop_lanes(self) -> None:
        # Review F1's failing scenario: 10 content-block messages shipped
        # verbatim vs 1 small string message. The shape lane dominates, so the
        # umbrella reason wins — NOT below_min_tokens, which described only
        # 1 of 11 messages.
        assert (
            noop_transform({"content_blocks": 10, "small": 1}) == "router:noop:no_eligible_content"
        )

    def test_granular_lane_beats_shape_lane_on_ties(self) -> None:
        # The umbrella is vacuously true of every no-op; on equal counts the
        # specific gate that stopped content is the better explanation.
        assert noop_transform({"content_blocks": 3, "small": 3}) == "router:noop:below_min_tokens"

    def test_suffix_is_always_known_vocabulary(self) -> None:
        for rc in (
            {"ratio_too_high": 2},
            {"small": 1},
            {"net_mutation_gate": 1},
            {"non_string": 1},
            {"already_compressed": 1},
            {},
        ):
            reason = noop_transform(rc).split("router:noop:", 1)[1]
            assert reason in _KNOWN_REASONS


# --------------------------------------------------------------------------- #
# 2. End-to-end via the faithful furl_compress path: no bare no-op survives.
# --------------------------------------------------------------------------- #
class TestNoBareNoopEndToEnd:
    def test_small_content_reasoned_below_min_tokens(self) -> None:
        res = _compress_tool("a short tool result well under the floor")
        assert res.transforms_applied == ["router:noop:below_min_tokens"]

    def test_incompressible_env_dump_reasoned_no_savings(self) -> None:
        content = _env_dump_incompressible()
        res = _compress_tool(content)
        assert res.transforms_applied == ["router:noop:no_savings"]
        # Honest 0% — and no lossy deletion snuck in (bytes preserved).
        assert res.tokens_after == res.tokens_before
        assert res.messages[0]["content"] == content

    def test_block_message_batch_reasoned_no_eligible_content(self) -> None:
        # Review F1 end-to-end: assistant text-block messages are protected by
        # default in the block walk, so each books ONLY the content_blocks
        # shape lane; one small tool string books small=1. The umbrella reason
        # must win over below_min_tokens — and the no-op stays byte-neutral.
        block_messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Design note {i}: this module owns its own retry "
                            "budget and reports upstream saturation through the "
                            "shared telemetry channel, so the scheduler can "
                            "rebalance work before queue depth breaches the "
                            "alerting threshold operations monitors."
                        ),
                    }
                ],
            }
            for i in range(10)
        ]
        messages = block_messages + [{"role": "tool", "content": "All checks passed."}]
        res = compress(messages, model=_MODEL)
        assert not res.error
        assert res.transforms_applied == ["router:noop:no_eligible_content"]
        assert res.messages == messages
        assert res.tokens_after == res.tokens_before

    def test_changelog_head_never_bare_noop(self) -> None:
        # The repo CHANGELOG head is prose+headers the extractor can't shrink;
        # it must no-op WITH a reason. (If it ever becomes compressible the
        # assertion still holds: then there is no router:noop entry at all.)
        res = _compress_tool(_changelog_head())
        for entry in _noop_reasons(res.transforms_applied):
            assert entry != "router:noop"
            assert entry.split("router:noop:", 1)[1] in _KNOWN_REASONS

    @pytest.mark.parametrize(
        "content",
        [
            "tiny",
            "a short tool result well under the floor",
            _env_dump_incompressible(),
            _changelog_head(),
            _repeated_line(60),
            _env_dump_realistic(),
        ],
    )
    def test_no_transform_is_ever_the_bare_noop(self, content: str) -> None:
        res = _compress_tool(content)
        assert "router:noop" not in res.transforms_applied
        for entry in _noop_reasons(res.transforms_applied):
            assert entry.split("router:noop:", 1)[1] in _KNOWN_REASONS


# --------------------------------------------------------------------------- #
# 3. Structured content that CAN compress still compresses (fidelity guard).
# --------------------------------------------------------------------------- #
class TestStructuredContentStillCompresses:
    def test_realistic_env_dump_compresses(self) -> None:
        content = _env_dump_realistic()
        assert len(content) > 3000  # ~5 KB, the evaluator's scale
        res = _compress_tool(content)
        # A redundant env dump reduces (TEXT extraction) — never a silent no-op.
        assert res.tokens_after < res.tokens_before
        assert not any(t.startswith("router:noop") for t in res.transforms_applied)

    def test_repeated_line_compresses(self) -> None:
        res = _compress_tool(_repeated_line(60))
        assert res.tokens_after < res.tokens_before
        assert _noop_reasons(res.transforms_applied) == []


# --------------------------------------------------------------------------- #
# 4. Deterministic lane triggers via the raw router (control min_tokens etc.).
# --------------------------------------------------------------------------- #
class TestRouterLaneReasons:
    def _apply(self, messages: list[dict], **apply_kwargs):
        router = ContentRouter(ContentRouterConfig())
        return router.apply(messages, Tokenizer(EstimatingTokenCounter()), **apply_kwargs)

    def test_non_string_content_reasoned(self) -> None:
        # A tool message whose content is neither str nor list → non_string lane.
        res = self._apply([{"role": "tool", "content": 12345}])
        assert res.transforms_applied == ["router:noop:non_string"]

    def test_below_min_tokens_reasoned(self) -> None:
        res = self._apply([{"role": "tool", "content": "short"}], min_tokens_to_compress=50)
        assert res.transforms_applied == ["router:noop:below_min_tokens"]
