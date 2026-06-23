"""Regression test for #1: config.score_threshold was dead on the default path.

On the default (target_ratio=None) path, compress() calls
model.get_keep_mask(...). The ONNX wrapper hardcoded ``score > 0.5`` and the
PyTorch model used a fixed argmax gate, so a non-default
``KompressConfig.score_threshold`` never reached the keep decision — even
though the field is documented as the keep cutoff and the batched
target_ratio=None branch DOES honor it (an internal inconsistency).

Fix: thread the threshold to get_keep_mask at the call site
(``self.config.score_threshold``); both wrappers accept it (default 0.5, so
the compression floor is byte-identical at the default value).

Mutation-sensitive: reverting the call site to drop the threshold argument
makes the model receive the 0.5 default instead of the configured 0.7.
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


class _ThresholdCapturingModel:
    """get_keep_mask records the threshold it was handed, then keeps every word
    (mask all-True) so the keep decision itself is irrelevant to the assertion."""

    def __init__(self) -> None:
        self.seen_threshold: float | None = None

    def get_keep_mask(self, input_ids, attention_mask, threshold: float = 0.5):
        self.seen_threshold = threshold
        batch, seq_len = input_ids.shape
        return [[True] * seq_len for _ in range(batch)]

    def get_scores(self, input_ids, attention_mask):  # pragma: no cover - unused here
        batch, seq_len = input_ids.shape
        return [[1.0] * seq_len for _ in range(batch)]


_CONTENT = " ".join(f"w{i:02d}" for i in range(20))


def _comp(monkeypatch, *, score_threshold: float):
    model = _ThresholdCapturingModel()
    monkeypatch.setattr(
        kc, "_load_kompress", lambda mid, dev: (model, _Tokenizer(), "onnx")
    )
    comp = KompressCompressor(
        KompressConfig(score_threshold=score_threshold, enable_ccr=False)
    )
    return comp, model


def test_configured_threshold_reaches_keep_mask(monkeypatch) -> None:
    # Non-default threshold MUST reach the model on the default (target_ratio=None) path.
    comp, model = _comp(monkeypatch, score_threshold=0.7)
    comp.compress(_CONTENT)  # target_ratio defaults to None -> get_keep_mask path
    assert model.seen_threshold == 0.7, (
        f"score_threshold not wired through: model saw {model.seen_threshold}"
    )


def test_default_threshold_is_half(monkeypatch) -> None:
    # At the default config the model still sees 0.5 (the floor-preserving value).
    comp, model = _comp(monkeypatch, score_threshold=0.5)
    comp.compress(_CONTENT)
    assert model.seen_threshold == 0.5
