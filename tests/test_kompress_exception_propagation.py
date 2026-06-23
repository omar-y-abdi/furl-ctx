"""Regression test for #4: blanket except turned any bug into silent passthrough.

compress() / compress_batch() wrapped the whole body in `except Exception`, so a
model or tokenizer BUG (TypeError, AttributeError, ...) was swallowed and
returned as a clean ratio-1.0 passthrough — indistinguishable from an intentional
passthrough. A caller could never tell a real failure from "nothing to compress".

Fix: catch only `_MODEL_UNAVAILABLE_ERRORS` (model/runtime not installed/cached)
as graceful passthrough; let every other exception PROPAGATE so it is loud.

Mutation-sensitive: reverting to `except Exception` makes the bug-type case pass
through silently and `test_bug_type_propagates` fails.
"""
from __future__ import annotations

import pytest

import headroom.transforms.kompress_compressor as kc
from headroom.transforms.kompress_compressor import (
    KompressCompressor,
    KompressConfig,
    KompressModelNotCached,
)


@pytest.fixture(autouse=True)
def _isolate():
    saved = dict(kc._kompress_cache)
    kc._kompress_cache.clear()
    yield
    kc._kompress_cache.clear()
    kc._kompress_cache.update(saved)


class _IdMatrix:
    def __init__(self, batch: int, seq_len: int) -> None:
        self.shape = (batch, seq_len)


class _Encoding:
    def __init__(self, n: int) -> None:
        self._ids = _IdMatrix(1, n)
        self._wids = list(range(n))

    def __getitem__(self, key):
        return self._ids

    def word_ids(self, batch_index: int = 0):
        return self._wids


class _Tokenizer:
    def __call__(self, words, **kwargs):
        return _Encoding(len(words))


class _BuggyModel:
    """Model whose inference method raises a BUG-type exception (TypeError)."""

    def get_keep_mask(self, input_ids, attention_mask, threshold: float = 0.5):
        raise TypeError("simulated model bug: bad tensor op")

    def get_scores(self, input_ids, attention_mask):
        raise TypeError("simulated model bug: bad tensor op")


_CONTENT = " ".join(f"word{i:02d}" for i in range(20))


def _compressor(monkeypatch, loader):
    monkeypatch.setattr(kc, "_load_kompress", loader)
    return KompressCompressor(KompressConfig(chunk_words=350, enable_ccr=False))


def test_bug_type_propagates(monkeypatch) -> None:
    # A model bug (TypeError) must NOT be swallowed into a silent passthrough.
    comp = _compressor(
        monkeypatch, lambda mid, dev: (_BuggyModel(), _Tokenizer(), "onnx")
    )
    with pytest.raises(TypeError):
        comp.compress(_CONTENT)


def test_model_unavailable_passes_through(monkeypatch) -> None:
    # Model/runtime unavailable (KompressModelNotCached) is a LEGITIMATE
    # graceful passthrough — content unchanged, ratio 1.0.
    def _raise_unavailable(mid, dev):
        raise KompressModelNotCached(mid)

    comp = _compressor(monkeypatch, _raise_unavailable)
    result = comp.compress(_CONTENT)
    assert result.compressed == _CONTENT
    assert result.compression_ratio == 1.0


def test_import_error_passes_through(monkeypatch) -> None:
    # ImportError (no [ml] extra) is also graceful passthrough.
    def _raise_import(mid, dev):
        raise ImportError("onnxruntime not installed")

    comp = _compressor(monkeypatch, _raise_import)
    result = comp.compress(_CONTENT)
    assert result.compressed == _CONTENT
    assert result.compression_ratio == 1.0


def test_batch_bug_type_propagates(monkeypatch) -> None:
    # Same contract on the batched path.
    comp = _compressor(
        monkeypatch, lambda mid, dev: (_BuggyModel(), _Tokenizer(), "onnx")
    )
    monkeypatch.setattr(comp, "_should_use_sequential_fallback", lambda: False)
    with pytest.raises(TypeError):
        comp.compress_batch([_CONTENT], target_ratio=[None])
