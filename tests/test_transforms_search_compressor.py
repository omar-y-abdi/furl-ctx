from __future__ import annotations

from types import SimpleNamespace

import pytest

from furl_ctx.transforms.search_compressor import (
    SearchCompressionResult,
    SearchCompressor,
    SearchCompressorConfig,
)


def test_search_compressor_compress_paths_and_ccr() -> None:
    """Phase 3e.2: `compress()` is now a single Rust call, so this test
    exercises end-to-end behavior instead of monkeypatching internal
    helpers (which the old orchestration relied on). The CCR plumbing
    is verified via `cache_key` presence + the marker string format
    Rust emits."""
    compressor = SearchCompressor(
        SearchCompressorConfig(enable_ccr=True, min_matches_for_ccr=2, context_keywords=["auth"])
    )
    no_match = compressor.compress("plain text only")
    assert no_match.original_match_count == 0
    assert no_match.compressed == "plain text only"

    # Build a large input so the Rust adaptive sizer's min_k=5 floor doesn't absorb everything and
    # compression actually fires (must drop the ratio below `min_compression_ratio_for_ccr=0.8`).
    lines = [f"src/auth.py:{i}:auth event {i}" for i in range(1, 51)]
    lines += [f"src/db.py:{i}:db query {i}" for i in range(1, 31)]
    content = "\n".join(lines)
    result = compressor.compress(content, context="auth", bias=0.5)  # low bias = drop more
    assert result.original_match_count == 80
    assert result.files_affected == 2
    assert result.compressed_match_count < result.original_match_count
    assert result.cache_key is not None
    assert result.compressed.endswith(f". Retrieve more: hash={result.cache_key}]")
    # Summaries appear for any file whose matches were dropped.
    assert isinstance(result.summaries, dict)
    assert len(result.summaries) >= 1


def test_search_compressor_fails_open_when_omissions_lack_ccr() -> None:
    """With CCR enabled, parser or selection omissions need full-original backing."""
    mixed = "src/main.py:12:needle\nBinary file assets/blob.bin matches\n"
    mixed_result = SearchCompressor().compress(mixed)
    assert mixed_result.cache_key is None
    assert mixed_result.compressed == mixed

    # Rust str::trim does not treat U+001C as whitespace; the native lines_unparsed
    # sidecar must therefore catch it even though Python str.strip() would erase it.
    control_separator = "src/a.py:1:hit\n\x1c\n"
    separator_result = SearchCompressor().compress(control_separator)
    assert separator_result.cache_key is None
    assert separator_result.compressed == control_separator

    capped = "\n".join(f"src/main.py:{i}:match {i}" for i in range(1, 7)) + "\n"
    capped_result = SearchCompressor(
        SearchCompressorConfig(max_matches_per_file=1, min_matches_for_ccr=10)
    ).compress(capped)
    assert capped_result.cache_key is None
    assert capped_result.compressed == capped


def test_search_compressor_no_ccr_opt_out_preserves_lossy_caps() -> None:
    """Explicitly disabling CCR keeps the pre-existing configured cap behavior."""
    content = "\n".join(f"src/main.py:{i}:match {i}" for i in range(1, 7)) + "\n"
    result = SearchCompressor(
        SearchCompressorConfig(enable_ccr=False, max_matches_per_file=1)
    ).compress(content)
    assert result.cache_key is None
    assert result.compressed != content
    assert result.compressed_match_count == 1


def test_search_compressor_persist_to_python_ccr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 3e.2: CCR persistence is now in `_persist_to_python_ccr`,
    which delegates to the production `CompressionStore`. Failures are
    logged (not silently swallowed) — this pins both paths."""
    compressor = SearchCompressor()

    seen: dict[str, tuple[str, str, str | None, str | None]] = {}
    monkeypatch.setitem(
        __import__("sys").modules,
        "furl_ctx.cache.compression_store",
        SimpleNamespace(
            get_compression_store=lambda: SimpleNamespace(
                store=lambda original, compressed, original_item_count=0, explicit_hash=None, compression_strategy=None, require_durable=False: (
                    seen.setdefault(
                        "call", (original, compressed, explicit_hash, compression_strategy)
                    )
                    or "stored-key"
                )
            )
        ),
    )
    compressor._persist_to_python_ccr("orig", "comp", "abc123")
    # explicit_hash carries the Rust marker key so retrieval of the marker hash finds the entry ().
    # compression_strategy attributes the entry to its route for the retrieval-feedback loop (Engine P2-13).
    assert seen["call"] == ("orig", "comp", "abc123", "search")

    # Loud failure: the store raises, but persist swallows + logs (no
    # exception propagates to the compress callsite).
    def broken_store() -> SimpleNamespace:
        raise RuntimeError("boom")

    monkeypatch.setitem(
        __import__("sys").modules,
        "furl_ctx.cache.compression_store",
        SimpleNamespace(get_compression_store=broken_store),
    )
    compressor._persist_to_python_ccr("orig", "comp", "abc123")  # must not raise


def test_search_compression_result_properties() -> None:
    """Result-property contract preserved across the port."""
    result = SearchCompressionResult(
        compressed="tiny",
        original="this is a much longer original string",
        original_match_count=10,
        compressed_match_count=4,
        files_affected=2,
        compression_ratio=0.3,
    )
    assert result.tokens_saved_estimate > 0
    assert result.matches_omitted == 6
