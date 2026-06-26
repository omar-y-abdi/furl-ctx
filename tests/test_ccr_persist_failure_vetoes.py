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

import textwrap
from typing import Any, Callable

import pytest

from headroom.cache.compression_store import (
    CompressionStore,
    clear_request_compression_store,
    set_request_compression_store,
)
from headroom.transforms.diff_compressor import DiffCompressor, DiffCompressorConfig
from headroom.transforms.log_compressor import LogCompressor, LogCompressorConfig
from headroom.transforms.search_compressor import SearchCompressor, SearchCompressorConfig


class _FailingStore:
    """A store whose ``store()`` always raises (simulating a Python
    compression_store write failure during the mirror). Every other attribute
    delegates to a real store so the compressor's other reads still behave.
    ``store_calls`` lets the test assert the CCR path was actually exercised —
    guarding against a vacuous GREEN where no marker was ever produced."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.store_calls = 0

    def store(self, *args: Any, **kwargs: Any) -> str:
        self.store_calls += 1
        raise RuntimeError("INJECTED compression_store write failure")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _make_large_diff(n_files: int = 5, hunks_each: int = 20) -> str:
    """A synthetic git diff well above min_lines_for_ccr — proven to emit a
    CCR marker (reused from test_diff_compressor_sidecar_persist.py)."""
    parts: list[str] = []
    for i in range(n_files):
        parts.append(
            textwrap.dedent(
                f"""\
                diff --git a/src/module_{i}.py b/src/module_{i}.py
                index abc1234..def5678 100644
                --- a/src/module_{i}.py
                +++ b/src/module_{i}.py
                """
            )
        )
        for h in range(hunks_each):
            parts.append(
                textwrap.dedent(
                    f"""\
                    @@ -{h*10+1},{h*10+6} +{h*10+1},{h*10+6} @@
                     context line one for file {i} hunk {h}
                     context line two for file {i} hunk {h}
                    -old code line A in file {i} hunk {h}
                    +new code line A in file {i} hunk {h}
                    -old code line B in file {i} hunk {h}
                    +new code line B in file {i} hunk {h}
                     context line three for file {i} hunk {h}
                    """
                )
            )
    return "".join(parts)


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


_CASES: dict[str, Callable[[], tuple[str, Callable[[], Any]]]] = {
    "diff": _diff_case,
    "log": _log_case,
    "search": _search_case,
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
    that CCR is globally off."""
    original, run = _CASES[producer]()
    result = run()

    if result.cache_key is None:
        pytest.skip(f"{producer}: CCR did not fire on this build; parametrization gap")
    assert result.compressed != original, (
        f"{producer}: cache_key set but content unchanged — CCR marker not actually emitted"
    )
    assert working_store.retrieve(result.cache_key) is not None, (
        f"{producer}: emitted cache_key does not resolve in the store"
    )
