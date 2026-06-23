"""Regression test for the [0.8, 0.9) unrecoverable-loss band (#2).

Bug: the CCR recovery marker was only written when ``ratio < 0.8``, but
``apply()`` applies the compression (drops words from the output) whenever
``ratio < 0.9``. So a compression landing in [0.8, 0.9) dropped words from
the visible output with NO CCR backing — the dropped words were
unrecoverable (silent loss). Both the direct path (compress, :898) and the
production batched path (compress_batch, :1152) had the same gate.

Fix: align both CCR gates to the apply gate (``ratio < 0.9``) so every
applied compression is CCR-recoverable. These tests prove RECALL == 100%
via a full CCR round-trip: compress -> cache_key -> store.retrieve(cache_key)
returns the ORIGINAL including the dropped words. Savings are unchanged (the
marker is the same ~12-word addition already used below 0.8).

Mutation-sensitive: reverting either gate to ``< 0.8`` drops the cache_key in
the [0.8, 0.9) band and the round-trip assertion fails.
"""
from __future__ import annotations

import pytest

import headroom.transforms.kompress_compressor as kc
from headroom.cache.compression_store import get_compression_store, reset_compression_store
from headroom.transforms.kompress_compressor import KompressCompressor, KompressConfig


class _IdMatrix:
    """Minimal stand-in for a [batch, seq] tensor (numpy not in this venv).

    The compress paths only use ``.shape`` on input_ids; values are unused
    by the stub model, which keys off seq_len alone.
    """

    def __init__(self, batch: int, seq_len: int) -> None:
        self.shape = (batch, seq_len)


@pytest.fixture(autouse=True)
def _isolate_store_and_cache():
    """Fresh CCR store + clean kompress module caches per test."""
    reset_compression_store()
    saved_cache = dict(kc._kompress_cache)
    kc._kompress_cache.clear()
    yield
    kc._kompress_cache.clear()
    kc._kompress_cache.update(saved_cache)
    reset_compression_store()


class _OneTokenPerWordEncoding:
    """Encoding where each input word maps to exactly one token (no truncation)."""

    def __init__(self, word_lists):
        # one token per word, padded to max len with word_id None (= pad).
        max_len = max(len(w) for w in word_lists)
        self._batch = [
            list(range(len(wl))) + [None] * (max_len - len(wl)) for wl in word_lists
        ]
        self._input_ids = _IdMatrix(len(word_lists), max_len)
        self._attention = _IdMatrix(len(word_lists), max_len)

    def __getitem__(self, key: str):
        return {"input_ids": self._input_ids, "attention_mask": self._attention}[key]

    def word_ids(self, batch_index: int = 0):
        return self._batch[batch_index]


class _StubTokenizer:
    def __call__(self, words, **kwargs):
        # words may be a single split-list (direct path) or a list of lists (batch).
        word_lists = words if words and isinstance(words[0], list) else [words]
        return _OneTokenPerWordEncoding(word_lists)


class _KeepFirstKModel:
    """ONNX-style model. keep_mask keeps first k; get_scores gives first k high."""

    def __init__(self, k: int) -> None:
        self._k = k

    def get_keep_mask(self, input_ids, attention_mask):
        seq_len = input_ids.shape[1]
        return [[i < self._k for i in range(seq_len)]]

    def get_scores(self, input_ids, attention_mask):
        # Score 1.0 for the first k tokens (> threshold 0.5), 0.0 otherwise.
        batch, seq_len = input_ids.shape
        return [[1.0 if i < self._k else 0.0 for i in range(seq_len)] for _ in range(batch)]


def _stub(monkeypatch, keep_k: int) -> KompressCompressor:
    model, tok = _KeepFirstKModel(keep_k), _StubTokenizer()
    monkeypatch.setattr(kc, "_load_kompress", lambda model_id, device: (model, tok, "onnx"))
    return KompressCompressor(KompressConfig(chunk_words=350, enable_ccr=True))


def _content(n: int) -> str:
    return " ".join(f"word{i:02d}" for i in range(n))


def _assert_recoverable(result, content: str, dropped: tuple[str, ...]) -> None:
    for d in dropped:
        assert d not in result.compressed, f"{d} should be dropped from output"
    assert result.cache_key is not None, "applied compression must be CCR-backed (#2)"
    entry = get_compression_store().retrieve(result.cache_key)
    assert entry is not None, "CCR entry must be retrievable"
    assert entry.original_content == content, "retrieve must return byte-exact original"
    for d in dropped:
        assert d in entry.original_content, f"{d} must recover via CCR"


def test_direct_path_band_0_85_is_recoverable(monkeypatch) -> None:
    """compress() (:898): 17/20 => ratio 0.85 ∈ [0.8,0.9), must be recoverable."""
    comp = _stub(monkeypatch, keep_k=17)
    content = _content(20)
    result = comp.compress(content)
    assert 0.8 <= result.compression_ratio < 0.9
    _assert_recoverable(result, content, ("word17", "word18", "word19"))


def test_batch_path_band_0_85_is_recoverable(monkeypatch) -> None:
    """compress_batch() (:1152, batched path): same band, must be recoverable.

    Force the batched code (not the ONNX-CPU sequential fallback, which would
    delegate back to compress()/:898) so we exercise the SECOND CCR gate. The
    is_onnx branch keeps it pure-numpy — no torch needed.
    """
    comp = _stub(monkeypatch, keep_k=17)
    monkeypatch.setattr(comp, "_should_use_sequential_fallback", lambda: False)
    content = _content(20)
    results = comp.compress_batch([content], target_ratio=[None])
    assert len(results) == 1
    result = results[0]
    assert 0.8 <= result.compression_ratio < 0.9
    _assert_recoverable(result, content, ("word17", "word18", "word19"))


def test_direct_path_band_0_70_still_recoverable(monkeypatch) -> None:
    """14/20 => ratio 0.70 < 0.8 was already backed; must stay backed (no regression)."""
    comp = _stub(monkeypatch, keep_k=14)
    content = _content(20)
    result = comp.compress(content)
    assert result.compression_ratio < 0.8
    _assert_recoverable(result, content, ("word17", "word18", "word19"))
