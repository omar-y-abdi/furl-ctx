"""CodeAwareCompressor (Engine P2-12) — gates, syntax round-trip, CCR backing.

The opt-in AST code compressor restores the archived tree-sitter capability
behind ``ContentRouterConfig.enable_code_aware`` (default OFF). These tests
pin the contract:

1. Default-off pin: SOURCE_CODE keeps routing to PASSTHROUGH — default
   router behavior (and therefore the benchmark) is byte-unchanged.
2. Syntax round-trip: the compressed render still parses (independent
   ``ast.parse`` check for Python; tree-sitter for brace languages).
3. CCR round-trip: the emitted ``Retrieve more: hash=…`` marker resolves in
   the production ``CompressionStore`` to the byte-exact original.
4. Store-failure veto: a failing store write reverts to the original
   (the marker never ships dangling) — text_crusher's fresh pattern.
5. Missing-dep fail-open: without tree-sitter the compressor passes
   through and warns exactly ONCE.
6. Protection precedence: analysis-intent and recent-code protections
   still win over the enabled compressor; ``lossless_only`` gates it off.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import re
from typing import Any

import pytest

from furl_ctx.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.ccr import marker_grammar
from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import get_tokenizer
from furl_ctx.transforms.content_detector import ContentType, DetectionResult
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.router_policy import (
    CompressionStrategy,
    content_type_from_strategy,
    strategy_from_detection,
    strategy_from_detection_type,
)

_HAS_TREE_SITTER = importlib.util.find_spec("tree_sitter_language_pack") is not None

requires_tree_sitter = pytest.mark.skipif(
    not _HAS_TREE_SITTER,
    reason="optional [code] extra (tree-sitter-language-pack) not installed",
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _python_code(n_funcs: int = 8, body_lines: int = 14) -> str:
    """Detects as SOURCE_CODE/python (verified against the Rust detector),
    is large enough to clear every floor, and has long function bodies so
    the compressor genuinely drops lines."""
    parts = ["import os", "import sys", "from typing import Any", ""]
    for i in range(n_funcs):
        parts.append(f"def handler_{i}(payload: dict, retries: int = {i}) -> Any:")
        parts.append(f'    """Process payload variant {i} with bounded retries."""')
        for j in range(body_lines):
            parts.append(f"    value_{j} = payload.get('field_{j}', {j}) + retries * {i + 1}")
        parts.append(f"    return value_{body_lines - 1}")
        parts.append("")
    return "\n".join(parts)


def _javascript_code(n_funcs: int = 8, body_lines: int = 14) -> str:
    parts = ["import fs from 'fs';", "const config = { retries: 3 };", ""]
    for i in range(n_funcs):
        parts.append(f"function transform_{i}(payload, retries) {{")
        for j in range(body_lines):
            parts.append(f"  const value_{j} = payload['field_{j}'] + retries * {i + 1};")
        parts.append(f"  return value_{body_lines - 1};")
        parts.append("}")
        parts.append("")
    return "\n".join(parts)


class _FailingStore:
    """A store whose ``store()`` always raises, simulating a Python
    compression_store write failure. ``store_calls`` guards against a
    vacuous GREEN where the CCR path was never exercised."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.store_calls = 0

    def store(self, *args: Any, **kwargs: Any) -> str:
        self.store_calls += 1
        raise RuntimeError("INJECTED compression_store write failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def working_store() -> Any:
    real = CompressionStore(max_entries=500, enable_feedback=False)
    set_request_compression_store(real)
    yield real
    clear_request_compression_store()


@pytest.fixture
def failing_store() -> Any:
    fs = _FailingStore(CompressionStore(max_entries=500, enable_feedback=False))
    set_request_compression_store(fs)
    yield fs
    clear_request_compression_store()


@pytest.fixture
def tokenizer() -> Tokenizer:
    return Tokenizer(get_tokenizer("gpt-4o"), "gpt-4o")


def _filler_messages(n_turns: int = 6) -> list[dict[str, Any]]:
    """Pad turns so recency-based protections don't cover the message
    under test. No analysis keywords."""
    turns: list[dict[str, Any]] = []
    for i in range(n_turns):
        turns.append({"role": "user", "content": f"question number {i} about the pipeline"})
        turns.append({"role": "assistant", "content": f"answer number {i} with details"})
    return turns


def _extract_marker_hash(compressed: str) -> str:
    match = re.search(r"hash=([0-9a-f]{24})\]", compressed)
    assert match is not None, f"no recovery marker in output tail: {compressed[-200:]!r}"
    return match.group(1)


# ─── 1. Default-off pin (no tree-sitter required) ───────────────────────────


class TestDefaultOffPin:
    def test_config_default_off(self) -> None:
        assert ContentRouterConfig().enable_code_aware is False

    def test_source_code_maps_to_passthrough_by_default(self) -> None:
        """The pre-P2-12 mapping is preserved byte-for-byte under defaults."""
        config = ContentRouterConfig()
        detection = DetectionResult(ContentType.SOURCE_CODE, 1.0, {"language": "python"})
        assert strategy_from_detection(config, detection) is CompressionStrategy.PASSTHROUGH
        assert (
            strategy_from_detection_type(config, ContentType.SOURCE_CODE)
            is CompressionStrategy.PASSTHROUGH
        )

    def test_source_code_maps_to_code_aware_when_enabled(self) -> None:
        config = ContentRouterConfig(enable_code_aware=True)
        detection = DetectionResult(ContentType.SOURCE_CODE, 1.0, {"language": "python"})
        assert strategy_from_detection(config, detection) is CompressionStrategy.CODE_AWARE
        assert (
            strategy_from_detection_type(config, ContentType.SOURCE_CODE)
            is CompressionStrategy.CODE_AWARE
        )

    def test_code_aware_strategy_maps_back_to_source_code(self) -> None:
        assert content_type_from_strategy(CompressionStrategy.CODE_AWARE) is (
            ContentType.SOURCE_CODE
        )

    def test_default_router_passes_small_code_through_byte_exact(self, working_store: Any) -> None:
        """Below the CCR-offload floor the default router must not touch
        code at all — byte-identical to the pre-P2-12 engine."""
        code = _python_code(n_funcs=2, body_lines=5)
        assert len(code) < 4000, "fixture must stay under the offload floor"
        result = ContentRouter().compress(code)
        assert result.compressed == code
        assert result.strategy_used is CompressionStrategy.PASSTHROUGH

    def test_default_router_never_routes_code_aware(self, working_store: Any) -> None:
        """Large code may still take today's reversible CCR offload, but the
        CODE_AWARE strategy must never appear under the default config."""
        code = _python_code()
        result = ContentRouter().compress(code)
        assert CompressionStrategy.CODE_AWARE.value not in result.strategy_chain
        assert result.strategy_used in (
            CompressionStrategy.PASSTHROUGH,
            CompressionStrategy.CCR_OFFLOAD,
        )


# ─── 2. Compression + syntax round-trip ──────────────────────────────────────


@requires_tree_sitter
class TestSyntaxRoundTrip:
    def test_python_output_parses_with_stdlib_ast(self, working_store: Any) -> None:
        """Independent verifier: the shipped render (marker line included)
        must be valid Python per the stdlib parser, not just per the same
        tree-sitter grammar that produced it."""
        from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor

        code = _python_code()
        result = CodeAwareCompressor().compress(code)

        assert result.compressed != code, "large code must actually compress"
        assert len(result.compressed) < len(code)
        assert result.syntax_valid is True
        ast.parse(result.compressed)  # raises SyntaxError on invalid output

    def test_javascript_output_parses(self, working_store: Any) -> None:
        from furl_ctx.transforms.code_aware_compressor import (
            CodeAwareCompressor,
            _get_parser,
            _has_syntax_issues,
        )

        code = _javascript_code()
        result = CodeAwareCompressor().compress(code)

        assert result.compressed != code
        tree = _get_parser("javascript").parse(bytes(result.compressed, "utf-8"))
        assert not _has_syntax_issues(tree.root_node)

    def test_marker_matches_shape_h_grammar(self, working_store: Any) -> None:
        """The emitted marker must parse under the strict consumer grammar
        (Shape H) so the marker_grammar consumer / recompression pinning recognise it."""
        from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor

        result = CodeAwareCompressor().compress(_python_code())
        assert result.cache_key is not None
        match = marker_grammar.BRACKET_RETRIEVE_PATTERN.search(result.compressed)
        assert match is not None, "marker must match BRACKET_RETRIEVE_PATTERN"
        assert match.group(3) == result.cache_key

    def test_small_code_passes_through_byte_exact(self, working_store: Any) -> None:
        from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor

        code = "def tiny() -> int:\n    return 1\n"
        result = CodeAwareCompressor().compress(code)
        assert result.compressed == code
        assert result.cache_key is None
        assert working_store.get_stats()["entry_count"] == 0

    def test_determinism_across_instances(self, working_store: Any) -> None:
        from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor

        code = _python_code()
        a = CodeAwareCompressor().compress(code, context="handler retries")
        b = CodeAwareCompressor().compress(code, context="handler retries")
        assert a.compressed == b.compressed
        assert a.cache_key == b.cache_key


# ─── 3. CCR backing ──────────────────────────────────────────────────────────


@requires_tree_sitter
class TestCcrBacking:
    def test_marker_hash_resolves_to_byte_exact_original(self, working_store: Any) -> None:
        from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor

        code = _python_code()
        result = CodeAwareCompressor().compress(code)

        assert result.cache_key is not None
        entry = working_store.retrieve(result.cache_key)
        assert entry is not None, "marker hash must resolve in the production store"
        assert entry.original_content == code, "recovery must be byte-exact"

    def test_store_failure_vetoes_marker(self, failing_store: Any) -> None:
        """Store write fails → serve the ORIGINAL, no marker, no cache_key
        (mirrors diff/log/search/text producers)."""
        from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor

        code = _python_code()
        result = CodeAwareCompressor().compress(code)

        assert failing_store.store_calls > 0, "CCR path never exercised — vacuous test"
        assert result.cache_key is None
        assert result.compressed == code

    def test_ccr_disabled_passes_through(self, working_store: Any) -> None:
        """A body-dropping render may only ship with a resolvable recovery
        marker; enable_ccr=False therefore means passthrough, not an
        unrecoverable lossy ship."""
        from furl_ctx.transforms.code_aware_compressor import (
            CodeAwareCompressor,
            CodeAwareConfig,
        )

        code = _python_code()
        result = CodeAwareCompressor(CodeAwareConfig(enable_ccr=False)).compress(code)
        assert result.compressed == code
        assert result.cache_key is None
        assert working_store.get_stats()["entry_count"] == 0


# ─── 4. Missing-dep fail-open ────────────────────────────────────────────────


class TestMissingDepFailOpen:
    def test_passthrough_and_single_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        working_store: Any,
    ) -> None:
        import furl_ctx.transforms.code_aware_compressor as mod

        monkeypatch.setattr(mod, "_check_tree_sitter_available", lambda: False)
        monkeypatch.setattr(mod, "_MISSING_DEP_WARNED", False)

        code = _python_code()
        compressor = mod.CodeAwareCompressor()
        with caplog.at_level(logging.WARNING, logger=mod.__name__):
            first = compressor.compress(code)
            second = compressor.compress(code)

        assert first.compressed == code
        assert first.cache_key is None
        assert second.compressed == code
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1, "missing-dep warning must fire exactly once"
        assert "furl-ctx[code]" in warns[0].getMessage()


# ─── 5. Router integration ───────────────────────────────────────────────────


@requires_tree_sitter
class TestRouterIntegration:
    def test_enabled_router_compresses_code(self, working_store: Any) -> None:
        code = _python_code()
        router = ContentRouter(ContentRouterConfig(enable_code_aware=True))
        result = router.compress(code)

        assert result.strategy_used is CompressionStrategy.CODE_AWARE
        assert result.compressed != code
        marker_hash = _extract_marker_hash(result.compressed)
        entry = working_store.retrieve(marker_hash)
        assert entry is not None
        assert entry.original_content == code
        # The compressed render must not additionally take the offload path.
        assert "_ccr_dropped" not in result.compressed

    def test_lossless_only_gates_code_aware_off(self, working_store: Any) -> None:
        """Strict lossless mode forbids the visible (recoverable) reduction;
        it also disables the offload, so output is byte-exact."""
        code = _python_code()
        router = ContentRouter(ContentRouterConfig(enable_code_aware=True, lossless_only=True))
        result = router.compress(code)
        assert result.compressed == code

    def test_missing_dep_router_falls_back_to_passthrough(
        self, monkeypatch: pytest.MonkeyPatch, working_store: Any
    ) -> None:
        """Enabled but dep missing: the arm resolves to a passthrough-shaped
        result (the reversible offload may still wrap it, exactly as it
        does for any uncompressible large content today)."""
        import furl_ctx.transforms.code_aware_compressor as mod

        monkeypatch.setattr(mod, "_check_tree_sitter_available", lambda: False)
        monkeypatch.setattr(mod, "_MISSING_DEP_WARNED", False)

        code = _python_code()
        router = ContentRouter(ContentRouterConfig(enable_code_aware=True))
        result = router.compress(code)
        assert CompressionStrategy.CODE_AWARE.value in result.strategy_chain
        # No AST compression happened: either untouched, or today's offload.
        assert result.strategy_used in (
            CompressionStrategy.CODE_AWARE,
            CompressionStrategy.PASSTHROUGH,
            CompressionStrategy.CCR_OFFLOAD,
        )
        if result.strategy_used is not CompressionStrategy.CCR_OFFLOAD:
            assert result.compressed == code


# ─── 6. Protection precedence ────────────────────────────────────────────────


@requires_tree_sitter
class TestProtectionPrecedence:
    def _apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **config_kwargs: Any,
    ) -> Any:
        router = ContentRouter(ContentRouterConfig(enable_code_aware=True, **config_kwargs))
        return router.apply(messages, tokenizer)

    def test_analysis_intent_still_protects_code(
        self, tokenizer: Tokenizer, working_store: Any
    ) -> None:
        """A genuine analysis request must keep code verbatim even with the
        compressor enabled — protection 3 runs before routing."""
        code = _python_code()
        messages = (
            _filler_messages(2)
            + [{"role": "assistant", "content": code}]
            + _filler_messages(3)
            + [{"role": "user", "content": "Please analyze the handler implementations."}]
        )
        result = self._apply(messages, tokenizer)
        assert "router:protected:analysis_context" in result.transforms_applied
        assert result.messages[4]["content"] == code

    def test_recent_code_still_protected(self, tokenizer: Tokenizer, working_store: Any) -> None:
        """Code within the protect_recent_code window stays verbatim."""
        code = _python_code()
        messages = _filler_messages(3) + [
            {"role": "user", "content": "Continue with the next step of the plan."},
            {"role": "assistant", "content": code},
        ]
        result = self._apply(messages, tokenizer)
        assert "router:protected:recent_code" in result.transforms_applied
        assert result.messages[-1]["content"] == code

    def test_old_code_without_analysis_intent_compresses(
        self, tokenizer: Tokenizer, working_store: Any
    ) -> None:
        """Control: the SAME code outside every protection window does
        compress — proving the two pins above assert protection, not a
        globally-dead arm."""
        code = _python_code()
        messages = (
            _filler_messages(2)
            + [{"role": "assistant", "content": code}]
            + _filler_messages(3)
            + [{"role": "user", "content": "Continue with the next step of the plan."}]
        )
        result = self._apply(messages, tokenizer)
        compressed = result.messages[4]["content"]
        assert compressed != code, "unprotected old code must compress"
        marker_hash = _extract_marker_hash(compressed)
        entry = working_store.retrieve(marker_hash)
        assert entry is not None
        assert entry.original_content == code
