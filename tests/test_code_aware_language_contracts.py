"""End-to-end contracts for the code-aware compressor's language matrix.

The compressor advertises eight language hints.  These tests deliberately use
the public compressor and production CCR store rather than its AST extraction
helpers: a refactor may change the render, but every shipped reduction must
remain parseable and must recover the exact source bytes.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator

import pytest
from tree_sitter import Parser
from tree_sitter_language_pack import get_language

from furl_ctx.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.ccr.marker_grammar import BRACKET_RETRIEVE_PATTERN
from furl_ctx.transforms.code_aware_compressor import CodeAwareCompressor, CodeAwareConfig


def _python() -> str:
    lines = ["import os", ""]
    for index in range(8):
        lines.append(f"def work_{index}(value: int) -> int:")
        lines.extend(f"    step_{n} = value + {n}" for n in range(15))
        lines.extend(("    return step_14", ""))
    return "\n".join(lines)


def _brace_functions(language: str) -> str:
    headers = {
        "javascript": ("import fs from 'fs';", "function work_{i}(value) {{"),
        "typescript": ("import fs from 'fs';", "function work_{i}(value: number) {{"),
        "rust": ("use std::fmt;", "pub fn work_{i}(value: i32) -> i32 {{"),
        "c": ("#include <stdio.h>", "int work_{i}(int value) {{"),
        "cpp": ("#include <vector>", "int work_{i}(int value) {{"),
    }
    preamble, signature = headers[language]
    lines = [preamble, ""]
    for index in range(8):
        lines.append(signature.format(i=index))
        if language == "rust":
            lines.extend(f"    let step_{n} = value + {n};" for n in range(15))
            lines.append("    step_14")
        else:
            keyword = "const" if language in {"javascript", "typescript"} else "int"
            lines.extend(f"  {keyword} step_{n} = value + {n};" for n in range(15))
            lines.append("  return step_14;")
        lines.extend(("}", ""))
    return "\n".join(lines)


LANGUAGE_SOURCES: tuple[tuple[str, Callable[[], str]], ...] = (
    ("python", _python),
    ("javascript", lambda: _brace_functions("javascript")),
    ("typescript", lambda: _brace_functions("typescript")),
    ("rust", lambda: _brace_functions("rust")),
    ("c", lambda: _brace_functions("c")),
    ("cpp", lambda: _brace_functions("cpp")),
)


@pytest.fixture
def ccr_store() -> Iterator[CompressionStore]:
    store = CompressionStore(max_entries=32, enable_feedback=False)
    set_request_compression_store(store)
    yield store
    clear_request_compression_store()


def _assert_parses(language: str, rendered: str) -> None:
    """Validate the public render with a parser constructed independently."""
    if language == "python":
        ast.parse(rendered)
        return

    parser = Parser()
    parser.language = get_language(language)  # type: ignore[arg-type]
    root = parser.parse(rendered.encode("utf-8")).root_node

    def has_error(node: object) -> bool:
        return bool(node.type == "ERROR" or node.is_missing) or any(  # type: ignore[attr-defined]
            has_error(child)
            for child in node.children  # type: ignore[attr-defined]
        )

    assert not has_error(root)


@pytest.mark.parametrize(("language", "source_factory"), LANGUAGE_SOURCES)
def test_each_shippable_language_is_parseable_and_byte_exact_recoverable(
    language: str,
    source_factory: Callable[[], str],
    ccr_store: CompressionStore,
) -> None:
    """Every advertised reduction ships valid code with a working recovery reference."""
    source = source_factory()
    result = CodeAwareCompressor(
        CodeAwareConfig(language_hint=language, semantic_analysis=False)
    ).compress(source)

    assert result.compressed != source, f"{language} fixture did not exercise reduction"
    assert result.cache_key is not None
    assert result.language.value == language
    _assert_parses(language, result.compressed)

    marker = BRACKET_RETRIEVE_PATTERN.search(result.compressed)
    assert marker is not None
    assert marker.group(3) == result.cache_key
    recovered = ccr_store.retrieve(result.cache_key)
    assert recovered is not None
    assert recovered.original_content == source


@pytest.mark.parametrize(("language", "source_factory"), LANGUAGE_SOURCES)
def test_configured_minimum_is_an_exact_activation_boundary(
    language: str,
    source_factory: Callable[[], str],
    ccr_store: CompressionStore,
) -> None:
    """Configured minimum length is inclusive: one byte below is inactive, exact is active."""
    source = source_factory()
    compressor = CodeAwareCompressor(
        CodeAwareConfig(
            language_hint=language,
            semantic_analysis=False,
            min_chars=len(source),
        )
    )

    below = compressor.compress(source[:-1])
    exact = compressor.compress(source)

    assert below.compressed == source[:-1]
    assert below.cache_key is None
    assert exact.compressed != source
    assert exact.cache_key is not None
    assert ccr_store.retrieve(exact.cache_key) is not None


@pytest.mark.parametrize(
    "language", ["python", "javascript", "typescript", "go", "rust", "java", "c", "cpp"]
)
def test_invalid_source_never_ships_a_dangling_reduction(
    language: str, ccr_store: CompressionStore
) -> None:
    """A language hint cannot override the fail-open syntax safety policy."""
    source = ("function definitely_broken( {\n" * 30) + "unterminated"
    result = CodeAwareCompressor(CodeAwareConfig(language_hint=language)).compress(source)

    assert result.compressed == source
    assert result.cache_key is None
    assert ccr_store.get_stats()["entry_count"] == 0
