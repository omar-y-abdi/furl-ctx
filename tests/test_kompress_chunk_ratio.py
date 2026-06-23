"""Regression test for #3: per-chunk target_ratio rounding under-keeps words.

When a caller sets an explicit ``target_ratio`` (user-facing API only; the
engine/bench path uses ``target_ratio=None`` and the score threshold), the
kept-word count must match the requested ratio across the WHOLE input.

Bug: the top-k was computed PER CHUNK with ``int(len*ratio)``. For 12 words /
chunk_words=5 / ratio=0.5 the chunks are [5,5,2] -> int(2.5)+int(2.5)+int(1.0)
= 2+2+1 = 5 kept (effective 0.42), not the requested round(12*0.5)=6.

Fix: accumulate per-word scores across all chunks and take ONE global top-k of
``round(n_words * ratio)``. Applies to both the direct path (compress) and the
batched path (compress_batch). The ``target_ratio=None`` threshold branch (the
engine/bench path) is untouched, so the compression floor is unaffected.

Mutation-sensitive: reverting to per-chunk ``int(len*ratio)`` drops the count
back to 5.
"""
from __future__ import annotations

import pytest

import headroom.transforms.kompress_compressor as kc
from headroom.cache.compression_store import reset_compression_store
from headroom.transforms.kompress_compressor import KompressCompressor, KompressConfig


@pytest.fixture(autouse=True)
def _isolate():
    reset_compression_store()
    saved = dict(kc._kompress_cache)
    kc._kompress_cache.clear()
    yield
    kc._kompress_cache.clear()
    kc._kompress_cache.update(saved)
    reset_compression_store()


class _IdMatrix:
    def __init__(self, batch: int, seq_len: int) -> None:
        self.shape = (batch, seq_len)


class _Encoding:
    def __init__(self, word_lists):
        max_len = max(len(w) for w in word_lists)
        self._batch = [
            list(range(len(wl))) + [None] * (max_len - len(wl)) for wl in word_lists
        ]
        self._ids = _IdMatrix(len(word_lists), max_len)

    def __getitem__(self, key):
        return {"input_ids": self._ids, "attention_mask": self._ids}[key]

    def word_ids(self, batch_index: int = 0):
        return self._batch[batch_index]


class _Tokenizer:
    def __call__(self, words, **kwargs):
        word_lists = words if words and isinstance(words[0], list) else [words]
        return _Encoding(word_lists)


class _DescendingScoreModel:
    """get_scores: token i in a chunk gets score (1.0 - i*0.01), strictly
    descending so the global top-k is deterministic and chunk-position-stable."""

    def get_scores(self, input_ids, attention_mask):
        batch, seq_len = input_ids.shape
        return [[1.0 - i * 0.01 for i in range(seq_len)] for _ in range(batch)]


def _comp(monkeypatch, chunk_words: int) -> KompressCompressor:
    monkeypatch.setattr(
        kc, "_load_kompress", lambda mid, dev: (_DescendingScoreModel(), _Tokenizer(), "onnx")
    )
    return KompressCompressor(KompressConfig(chunk_words=chunk_words, enable_ccr=False))


_CONTENT_12 = " ".join(f"w{i:02d}" for i in range(12))


def test_direct_multi_chunk_ratio_matches_global(monkeypatch) -> None:
    # 12 words, chunk 5 => chunks [5,5,2]. ratio 0.5 must keep round(12*0.5)=6,
    # not the per-chunk 2+2+1=5.
    comp = _comp(monkeypatch, chunk_words=5)
    result = comp.compress(_CONTENT_12, target_ratio=0.5)
    assert result.compressed_tokens == 6, f"expected 6 kept, got {result.compressed_tokens}"


def test_batch_multi_chunk_ratio_matches_global(monkeypatch) -> None:
    comp = _comp(monkeypatch, chunk_words=5)
    monkeypatch.setattr(comp, "_should_use_sequential_fallback", lambda: False)
    results = comp.compress_batch([_CONTENT_12], target_ratio=[0.5])
    assert len(results) == 1
    assert results[0].compressed_tokens == 6, f"expected 6 kept, got {results[0].compressed_tokens}"


def test_single_chunk_ratio_unchanged(monkeypatch) -> None:
    # Contrast: when everything fits in ONE chunk, the count was already 6 and
    # must stay 6 — proving the fix is multi-chunk-specific, not a global shift.
    comp = _comp(monkeypatch, chunk_words=350)
    result = comp.compress(_CONTENT_12, target_ratio=0.5)
    assert result.compressed_tokens == 6


# NOTE: inputs < 10 words pass through unchanged (compress() early-returns
# _passthrough), so every case here uses >= 10 words to exercise the ratio path.
@pytest.mark.parametrize(
    "n_words,chunk,ratio,expected",
    [
        (12, 5, 0.5, 6),   # round(6.0)=6   (per-chunk [5,5,2] would give 2+2+1=5)
        (10, 3, 0.5, 5),   # round(5.0)=5   (per-chunk [3,3,3,1] would give 1+1+1+0->min1=4)
        (12, 5, 0.25, 3),  # round(3.0)=3
        (15, 4, 0.5, 8),   # round(7.5)=8   (per-chunk [4,4,4,3] would give 2+2+2+1=7)
    ],
)
def test_kept_count_matches_ratio(monkeypatch, n_words, chunk, ratio, expected) -> None:
    comp = _comp(monkeypatch, chunk_words=chunk)
    content = " ".join(f"w{i:02d}" for i in range(n_words))
    result = comp.compress(content, target_ratio=ratio)
    assert result.compressed_tokens == expected
