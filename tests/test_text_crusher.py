"""TextCrusher (Engine P2-11) — routing pins, CCR round-trip, gates.

The deterministic prose compressor replaces the PLAIN_TEXT passthrough.
These tests pin the Python-facing contract:

1. Routing: large varied prose routes to the TEXT strategy and actually
   compresses (marker emitted); small prose passes through byte-exact.
2. CCR round-trip: the marker hash resolves in the production
   ``CompressionStore`` to the byte-exact original.
3. ``lossless_only`` gates the TEXT arm off (prose passthrough).
4. Error-content protection: ``is_error`` tool results stay verbatim
   even when large enough to crush.
5. Protected tags survive the crush through the router path.

The store-failure veto for this producer lives in
``test_ccr_persist_failure_vetoes.py`` (parametrized ``text`` case,
same harness as diff/log/search).
"""

from __future__ import annotations

from typing import Any

import pytest

from furl_ctx.cache.backends.sqlite import SqliteBackend
from furl_ctx.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.router_policy import CompressionStrategy
from furl_ctx.transforms.text_crusher import TextCrusher, TextCrusherConfig

# ─── Fixtures ────────────────────────────────────────────────────────────────

# Narrative vocabulary on purpose: operational words (scheduler, queue, validated, …) tip
# the detector toward BUILD_OUTPUT; these must detect as PLAIN_TEXT to pin the TEXT route.
_SUBJECTS = [
    "The regional museum",
    "Our neighborhood bakery",
    "The village library",
    "A traveling exhibition",
    "The community orchestra",
    "The local newspaper",
    "The summer festival",
    "The riverside market",
]
_VERBS = [
    "welcomed",
    "celebrated",
    "organized",
    "hosted",
    "featured",
    "documented",
    "supported",
    "expanded",
]
_OBJECTS = [
    "hundreds of visitors",
    "a new membership program",
    "several evening workshops",
    "an oral history project",
    "a seasonal concert series",
    "local artist showcases",
    "weekend reading circles",
    "volunteer training sessions",
]
_TAILS = [
    "during the mild autumn weeks",
    "with help from returning volunteers",
    "despite the ongoing renovations",
    "to considerable local acclaim",
    "as attendance climbed steadily",
]


def _varied_prose(n_sentences: int = 40) -> str:
    """Lexically varied prose: clears the 600-char / 15-segment floors,
    detects as PLAIN_TEXT, and no two sentences collapse under any
    dedup tier (combinations stay distinct)."""
    paras: list[str] = []
    sentences: list[str] = []
    for i in range(n_sentences):
        sentences.append(
            f"{_SUBJECTS[i % 8]} {_VERBS[(i * 3 + 1) % 8]} "
            f"{_OBJECTS[(i * 5 + 2) % 8]} {_TAILS[i % 5]}."
        )
        if len(sentences) == 5:
            paras.append(" ".join(sentences))
            sentences = []
    if sentences:
        paras.append(" ".join(sentences))
    return "\n\n".join(paras)


@pytest.fixture
def tokenizer() -> Tokenizer:
    return Tokenizer(get_tokenizer("gpt-4o"), "gpt-4o")


@pytest.fixture
def working_store(tmp_path: Any) -> Any:
    """The DURABLE store production runs — a sqlite-backed ``CompressionStore``.

    The round-trip tests assert recovery "in the production store"; production is
    the ``SqliteBackend`` (the MCP server's default), whose ``surrogatepass``
    BLOB round-trip is a different operation from the in-memory raw-``str``
    identity. Injecting it here makes that claim literally true AND makes the
    producer's ``require_durable=True`` persist (``_ccr_persist``) a real durable
    write instead of a no-op, so a lost/degraded write would veto the marker.
    Its own per-test ``ccr.sqlite3`` under ``tmp_path`` keeps it isolated.
    """
    real = CompressionStore(
        max_entries=500,
        enable_feedback=False,
        backend=SqliteBackend(db_path=tmp_path / "ccr.sqlite3"),
    )
    set_request_compression_store(real)
    yield real
    clear_request_compression_store()
    real.close()  # release the sqlite fds this store opened


def _filler_messages() -> list[dict[str, Any]]:
    """Pad earlier turns so recency-based protections don't cover the
    message under test."""
    turns: list[dict[str, Any]] = []
    for i in range(6):
        turns.append({"role": "user", "content": f"question number {i} about the report"})
        turns.append({"role": "assistant", "content": f"answer number {i} with details"})
    return turns


# ─── Routing pins ────────────────────────────────────────────────────────────


class TestRouting:
    def test_large_prose_routes_to_text_crusher(self, working_store: Any) -> None:
        """PLAIN_TEXT above the floors must route to the TEXT strategy and
        actually compress — the passthrough era is over."""
        content = _varied_prose()
        router = ContentRouter()
        result = router.compress(content)

        assert result.strategy_used is CompressionStrategy.TEXT
        assert result.compressed != content, "TEXT strategy must compress large prose"
        assert len(result.compressed) < len(content)
        assert "Retrieve more: hash=" in result.compressed, "recovery marker must ship"

    def test_small_prose_passes_through_byte_exact(self, working_store: Any) -> None:
        content = "Short prose only. Two sentences here."
        router = ContentRouter()
        result = router.compress(content)
        assert result.compressed == content

    def test_medium_prose_below_segment_floor_passes_through(self, working_store: Any) -> None:
        """Above min_chars but below min_segments → passthrough."""
        content = (
            "This single paragraph rambles on for quite a while about one topic "
            "without ever ending a sentence because the writer keeps chaining "
            "clauses together with conjunctions and commas, adding more and more "
            "detail about the deployment process and the monitoring setup and "
            "the alert routing and the escalation policy and the postmortem "
            "culture and the weekly review meeting and the quarterly planning "
            "session and the yearly budget negotiation that everyone dreads "
            "attending because it takes an entire afternoon"
        )
        assert len(content) >= 500
        router = ContentRouter()
        result = router.compress(content)
        assert result.compressed == content

    def test_lossless_only_gates_text_crusher_off(self, working_store: Any) -> None:
        """`lossless_only=True` must resolve TEXT to passthrough — the
        crusher drops segments, which strict mode forbids."""
        content = _varied_prose()
        router = ContentRouter(config=ContentRouterConfig(lossless_only=True))
        result = router.compress(content)
        assert result.compressed == content
        assert "Retrieve more: hash=" not in result.compressed

    def test_enable_flag_gates_text_crusher_off(self, working_store: Any) -> None:
        content = _varied_prose()
        router = ContentRouter(config=ContentRouterConfig(enable_text_crusher=False))
        result = router.compress(content)
        assert result.compressed == content

    def test_strategy_chain_records_text(self, working_store: Any) -> None:
        content = _varied_prose()
        router = ContentRouter()
        result = router.compress(content)
        assert result.strategy_chain[0] == CompressionStrategy.TEXT.value


# ─── CCR round-trip ─────────────────────────────────────────────────────────


class TestCcrRoundTrip:
    def test_marker_hash_resolves_to_byte_exact_original(self, working_store: Any) -> None:
        content = _varied_prose()
        crusher = TextCrusher()
        result = crusher.compress(content)

        assert result.cache_key is not None, "large prose must crush"
        assert f"hash={result.cache_key}]" in result.compressed

        entry = working_store.retrieve(result.cache_key)
        assert entry is not None, "marker hash must resolve in the production store"
        assert entry.original_content == content, "recovery must be byte-exact"

    def test_router_path_backs_marker_in_python_store(self, working_store: Any) -> None:
        """End-to-end through the router: the marker in the routed output
        must resolve to the byte-exact original."""
        import re

        content = _varied_prose()
        router = ContentRouter()
        result = router.compress(content)

        match = re.search(r"hash=([0-9a-f]{24})\]", result.compressed)
        assert match is not None, f"no marker in: {result.compressed[-200:]}"
        entry = working_store.retrieve(match.group(1))
        assert entry is not None
        assert entry.original_content == content

    def test_passthrough_never_writes_store(self, working_store: Any) -> None:
        content = "Tiny prose. Nothing to store."
        crusher = TextCrusher()
        result = crusher.compress(content)
        assert result.cache_key is None
        assert working_store.get_stats()["entry_count"] == 0


# ─── Safety gates ────────────────────────────────────────────────────────────


class TestSafetyGates:
    def test_is_error_tool_result_not_compressed(
        self, tokenizer: Tokenizer, working_store: Any
    ) -> None:
        """An `is_error` tool_result block stays verbatim even when the
        content is large enough (and prose enough) to crush."""
        content = _varied_prose()
        router = ContentRouter()
        messages = _filler_messages() + [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": True,
                        "content": content,
                    }
                ],
            },
        ]
        result = router.apply(messages, tokenizer)
        assert "router:protected:error_output" in result.transforms_applied
        block = result.messages[-1]["content"][0]
        assert block["content"] == content

    def test_same_content_without_error_flag_does_compress(
        self, tokenizer: Tokenizer, working_store: Any
    ) -> None:
        """Control for the error-protection pin: the identical block WITHOUT
        `is_error` takes the TEXT route and compresses — proving the flag
        (not the content) is what protects."""
        content = _varied_prose()
        router = ContentRouter()
        messages = _filler_messages() + [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": content,
                    }
                ],
            },
        ]
        result = router.apply(messages, tokenizer)
        block = result.messages[-1]["content"][0]
        assert block["content"] != content, "prose tool_result must compress"
        assert "Retrieve more: hash=" in block["content"]

    def test_protected_tag_survives_router_compression(self, working_store: Any) -> None:
        reminder = (
            "<system-reminder>Keep this block intact through compression. "
            "It must appear verbatim in the output text.</system-reminder>"
        )
        paras = _varied_prose().split("\n\n")
        paras.insert(3, reminder)
        content = "\n\n".join(paras)

        router = ContentRouter()
        result = router.compress(content)
        assert result.strategy_used is CompressionStrategy.TEXT
        assert result.compressed != content, "must actually compress"
        assert reminder in result.compressed, "protected tag must survive byte-exact"
        assert "{{FURL_TAG_" not in result.compressed, "no placeholder leakage"

    def test_bias_keeps_more(self, working_store: Any) -> None:
        content = _varied_prose()
        crusher = TextCrusher()
        lean = crusher.compress(content, bias=1.0)
        fat = crusher.compress(content, bias=2.0)
        assert fat.compressed_segment_count >= lean.compressed_segment_count

    def test_determinism_across_instances(self, working_store: Any) -> None:
        content = _varied_prose()
        a = TextCrusher().compress(content, context="audit events")
        b = TextCrusher().compress(content, context="audit events")
        assert a.compressed == b.compressed
        assert a.cache_key == b.cache_key

    def test_config_floors_respected(self, working_store: Any) -> None:
        """A raised min_chars floor turns a crushable input into passthrough."""
        content = _varied_prose()
        crusher = TextCrusher(config=TextCrusherConfig(min_chars=len(content) + 1))
        result = crusher.compress(content)
        assert result.compressed == content
        assert result.cache_key is None
