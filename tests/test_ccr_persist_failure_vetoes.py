"""Persist-failure veto pin for the diff / log / search CCR mirror (P0 fix).

Companion to ``test_ccr_mirror_no_silent_loss.py`` (which pins the SmartCrusher
producer). The diff / log / search compressors used to LOG-and-continue on a
Python-store write failure, shipping a ``<<ccr:HASH>>`` marker whose original
was never persisted → ``retrieve()`` misses → unrecoverable silent loss on the
model-facing path (the recovery store the LLM reads is the *Python* store).

The fix: ``_persist_to_python_ccr`` returns ``bool``; on failure the compressor
serves the ORIGINAL uncompressed content (no marker), mirroring
``cross_message_dedup``'s veto and the fail-safe SmartCrusher gets by raising.

RED before the fix (marker shipped, cache_key set, content compressed);
GREEN after (cache_key None, no ``<<ccr:`` marker, compressed == original).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from furl_ctx.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from furl_ctx.transforms.diff_compressor import DiffCompressor, DiffCompressorConfig
from furl_ctx.transforms.log_compressor import LogCompressor, LogCompressorConfig
from furl_ctx.transforms.search_compressor import SearchCompressor, SearchCompressorConfig
from furl_ctx.transforms.text_crusher import TextCrusher, TextCrusherConfig

# TEST-19: shared single-copy helpers (were duplicated verbatim here).
from tests._fixtures import FailingStore as _FailingStore
from tests._fixtures import make_large_diff as _make_large_diff


def _diff_case() -> tuple[str, Callable[[], Any]]:
    content = _make_large_diff()
    c = DiffCompressor(DiffCompressorConfig(enable_ccr=True, min_lines_for_ccr=10))
    return content, lambda: c.compress(content)


def _log_case() -> tuple[str, Callable[[], Any]]:
    content = "\n".join(
        [f"INFO: Processing item {i}" for i in range(200)] + ["ERROR: Failed at item 100"]
    )
    c = LogCompressor(config=LogCompressorConfig(min_lines_for_ccr=50, enable_ccr=True))
    return content, lambda: c.compress(content)


def _search_case() -> tuple[str, Callable[[], Any]]:
    content = "\n".join([f"src/file.py:{i}:line {i}" for i in range(1, 201)])
    c = SearchCompressor(
        config=SearchCompressorConfig(
            enable_ccr=True, min_matches_for_ccr=10, max_matches_per_file=5
        )
    )
    return content, lambda: c.compress(content)


def _text_case() -> tuple[str, Callable[[], Any]]:
    # Lexically varied prose (counter-only variation would collapse in
    # the crusher's digit-masked dedup tier and shrink the crush).
    subjects = ["The scheduler", "Our ingester", "The billing worker", "A daemon"]
    verbs = ["processed", "archived", "replicated", "throttled", "validated"]
    objects = ["customer records", "audit events", "payment batches", "trace spans"]
    tails = [
        "before the morning deadline without operator intervention",
        "while the standby region absorbed the overflow traffic",
        "although the retry queue kept growing steadily",
    ]
    sentences = [
        f"{subjects[i % 4]} {verbs[(i * 2 + 1) % 5]} {objects[(i * 3 + 2) % 4]} {tails[i % 3]}."
        for i in range(40)
    ]
    content = " ".join(sentences)
    c = TextCrusher(config=TextCrusherConfig(enable_ccr=True))
    return content, lambda: c.compress(content)


_CASES: dict[str, Callable[[], tuple[str, Callable[[], Any]]]] = {
    "diff": _diff_case,
    "log": _log_case,
    "search": _search_case,
    "text": _text_case,
}


@pytest.fixture
def failing_store() -> Any:
    fs = _FailingStore(CompressionStore(max_entries=500, enable_feedback=False))
    set_request_compression_store(fs)
    yield fs
    clear_request_compression_store()


@pytest.fixture
def working_store() -> Any:
    real = CompressionStore(max_entries=500, enable_feedback=False)
    set_request_compression_store(real)
    yield real
    clear_request_compression_store()


@pytest.mark.parametrize("producer", list(_CASES))
def test_persist_failure_vetoes_marker(producer: str, failing_store: Any) -> None:
    """When the Python store write fails, the compressor must NOT ship a
    dangling marker — it serves the original uncompressed content instead."""
    original, run = _CASES[producer]()
    result = run()

    # The CCR path MUST have been exercised, else the test is vacuous.
    assert failing_store.store_calls > 0, (
        f"{producer}: store.store() never attempted — fixture no longer triggers CCR"
    )
    assert result.cache_key is None, (
        f"{producer}: cache_key shipped despite the recovery write failing (dangling marker)"
    )
    # compressed == original is the definitive invariant: full content served,
    # so no '[Retrieve ... hash=]' marker references an un-persisted original.
    assert result.compressed == original, (
        f"{producer}: served compressed output (with a dangling recovery marker) "
        "instead of reverting to the original"
    )


@pytest.mark.parametrize("producer", list(_CASES))
def test_working_store_still_emits_marker(producer: str, working_store: Any) -> None:
    """Control: with a working store the SAME fixture DOES emit a resolvable CCR
    marker — proving the veto test asserts a behavior change on failure, not
    that CCR is globally off.

    TEST-9 (hardened): the Python shim's re-persist is the marker's ONLY real
    backing — since PERF-8 the Rust bridges compute the ``cache_key`` in
    key-only mode and persist NOTHING themselves. So this control must pin the
    full recovery contract, hard: after ``compress()``, the marker's hash
    resolves in the production store AND the resolved entry carries the FULL
    original content (not merely "some entry exists")."""
    original, run = _CASES[producer]()
    result = run()

    assert result.cache_key is not None, (
        f"{producer}: CCR did not fire on this build — the fixture no longer "
        "triggers the CCR path. Verify the fixture content meets the CCR threshold "
        "for this producer (diff/log/search compressor)."
    )
    assert result.compressed != original, (
        f"{producer}: cache_key set but content unchanged — CCR marker not actually emitted"
    )
    entry = working_store.retrieve(result.cache_key)
    assert entry is not None, (
        f"{producer}: emitted cache_key does not resolve in the store — the "
        "shim's re-persist (the marker's only backing) did not happen"
    )
    assert entry.original_content == original, (
        f"{producer}: cache_key resolves but the stored payload is not the "
        "original — retrieval would serve corrupted recovery data"
    )
