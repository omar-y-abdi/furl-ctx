"""Mutation-resistance hardening for kompress_compressor.

Plan items:
  K-B1a: _bucket_count parametrized literal pins.
  K-B1b: _kompress_content_signature.structure_hash literal pins.
  K-9:   multi-subword max-score semantics (word0 uses max of its tokens).
  K-10:  multi-subword OR keep semantics (either token true → word kept).
  K-chunk: chunk-offset exact output (kills wid+chunk_start mutations).
  K-bnd:  n_words < 10 short-input boundary (compress passthrough at 9 vs 10).
"""
from __future__ import annotations

import pytest

import headroom.transforms.kompress_compressor as kc
from headroom.cache.compression_store import reset_compression_store
from headroom.transforms.kompress_compressor import (
    KompressCompressor,
    KompressConfig,
    _bucket_count,
    _kompress_content_signature,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_compression_store()
    saved = dict(kc._kompress_cache)
    kc._kompress_cache.clear()
    yield
    kc._kompress_cache.clear()
    kc._kompress_cache.update(saved)
    reset_compression_store()


# ---------------------------------------------------------------------------
# K-B1a  _bucket_count — pinned literal boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (1, "1-2"),
        (2, "2-4"),
        (3, "2-4"),
        (4, "4-8"),
        (7, "4-8"),
        (8, "8-16"),
        (15, "8-16"),
        (16, "16-32"),
        (31, "16-32"),
        (32, "32-64"),
        (127, "64-128"),
        (128, "128-256"),
        (255, "128-256"),
        (256, "256-512"),
    ],
)
def test_b1a_bucket_count_pinned_literals(value: int, expected: str) -> None:
    """Pin bucket_count formula for all boundary transitions.

    Mutation-sensitive: removing the (value <= 0) guard, or changing bit_length
    arithmetic, changes the output for the 0 or power-of-2 cases.
    """
    assert _bucket_count(value) == expected


# ---------------------------------------------------------------------------
# K-B1b  _kompress_content_signature — structure_hash pinned literals
# ---------------------------------------------------------------------------


def test_b1b_signature_hash_plain_content() -> None:
    """Pin structure_hash for a plain sentence (no errors, no paths).

    Mutation-sensitive: changing any shape component changes the sha256 prefix.
    """
    sig = _kompress_content_signature("hello world this is a test string for kompress")
    assert sig.structure_hash == "8666dafa67919f1fd5313c7f"
    assert sig.has_error_like_field is False


def test_b1b_signature_hash_error_content() -> None:
    """Pin structure_hash for error-like content + assert has_error_like_field."""
    sig = _kompress_content_signature("ERROR: fatal exception occurred traceback")
    assert sig.structure_hash == "aad3962be514bf419b181f27"
    assert sig.has_error_like_field is True


def test_b1b_signature_hash_empty_content() -> None:
    """Pin structure_hash for empty string."""
    sig = _kompress_content_signature("")
    assert sig.structure_hash == "c82c95f4649398549cb57165"


def test_b1b_signature_different_shapes_different_hashes() -> None:
    """Two structurally distinct inputs must produce different hashes."""
    s1 = _kompress_content_signature("hello world this is a test string for kompress")
    s2 = _kompress_content_signature("ERROR: fatal exception occurred traceback")
    assert s1.structure_hash != s2.structure_hash


# ---------------------------------------------------------------------------
# Stub infrastructure for behavioral tests
# ---------------------------------------------------------------------------


class _IdMatrix:
    def __init__(self, batch: int, seq_len: int) -> None:
        self.shape = (batch, seq_len)


class _Encoding:
    def __init__(self, word_ids_list: list[int | None]) -> None:
        self._wids = word_ids_list
        self._ids = _IdMatrix(1, len(word_ids_list))

    def __getitem__(self, key: str) -> _IdMatrix:
        return self._ids

    def word_ids(self, batch_index: int = 0) -> list[int | None]:
        return self._wids


class _SimpleTokenizer:
    """Returns a pre-wired encoding (not word-splitting)."""

    def __init__(self, word_ids: list[int | None], scores: list[float] | None = None) -> None:
        self._word_ids = word_ids
        self._scores = scores or [0.0] * len(word_ids)

    def __call__(self, words, **kwargs) -> _Encoding:
        return _Encoding(self._word_ids)


class _ScoreModel:
    """ONNX-style model returning pre-wired per-token scores."""

    def __init__(self, scores: list[float], masks: list[bool]) -> None:
        self._scores = scores
        self._masks = masks

    def get_keep_mask(self, input_ids, attention_mask, threshold: float = 0.5) -> list[list[bool]]:
        return [self._masks]

    def get_scores(self, input_ids, attention_mask) -> list[list[float]]:
        return [self._scores]


def _make_compressor(monkeypatch, model, tokenizer) -> KompressCompressor:
    monkeypatch.setattr(kc, "_load_kompress", lambda mid, dev: (model, tokenizer, "onnx"))
    return KompressCompressor(KompressConfig(chunk_words=350, enable_ccr=False))


# ---------------------------------------------------------------------------
# K-9  max-score semantics: a word with 2 tokens uses the MAX token score
# ---------------------------------------------------------------------------


def test_k9_multi_subword_max_score_semantics(monkeypatch) -> None:
    """word0 has two sub-tokens (wid=0 for both); word0 score = max(0.9, 0.1) = 0.9.
    word1 has two sub-tokens (wid=1 for both); word1 score = max(0.3, 0.4) = 0.4.

    With 12 words, target_ratio=1/12 → keep 1: word0 (max 0.9) wins over word1 (0.4).

    Mutation-sensitive: `s > → s <` (min instead of max) → word0 score = 0.1 →
    word1 (max 0.4) would be kept instead.

    We use 12 words to exceed the n_words < 10 passthrough guard.
    The first two words (w0, w1) are the tested ones; the tokenizer maps:
      token 0 → word 0 (w0, two tokens)
      token 1 → word 0 (w0, second token)
      token 2 → word 1 (w1, two tokens)
      token 3 → word 1 (w1, second token)
      tokens 4-11 → words 2-9 (one token each, low scores)
    """
    # Build a tokenizer that returns pre-wired word_ids for a 12-word chunk.
    # word_ids: [0,0,1,1,2,3,4,5,6,7,8,9] (first two words get 2 tokens each)
    # scores:   [0.9, 0.1, 0.3, 0.4, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
    word_ids_12 = [0, 0, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    scores_12 = [0.9, 0.1, 0.3, 0.4, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
    masks_12 = [False] * 12

    class _WiredTokenizer:
        def __call__(self, words, **kwargs) -> _Encoding:
            return _Encoding(word_ids_12)

    comp = _make_compressor(monkeypatch, _ScoreModel(scores_12, masks_12), _WiredTokenizer())

    # 12 words; keep round(12 * 1/12) = 1: word0 (max score 0.9) must win
    content = " ".join(f"w{i}" for i in range(12))
    result = comp.compress(content, target_ratio=1 / 12)
    assert result.compressed.split()[0] == "w0", (
        f"max semantics: word0 (max score 0.9) must win; got {result.compressed!r}"
    )
    assert len(result.compressed.split()) == 1, (
        f"keep=1 expected, got {len(result.compressed.split())}"
    )


# ---------------------------------------------------------------------------
# K-10  OR semantics: either token True → word kept
# ---------------------------------------------------------------------------


def test_k10_multi_subword_or_mask_semantics(monkeypatch) -> None:
    """word0's tokens: [True, False] → word0 KEPT (OR semantics).
       word1's tokens: [False, True] → word1 KEPT (OR semantics).

    Both words kept, even though each has one False token.
    Mutation-sensitive: AND semantics → neither word kept.

    Uses 12 words to exceed the n_words < 10 passthrough guard.
    word0 gets tokens [True, False], word1 gets [False, True],
    words 2-9 get [False] each → only w0 and w1 survive via OR.
    """
    # word_ids: [0, 0, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    # masks:    [T, F, F, T, F, F, F, F, F, F, F, F]
    word_ids_12 = [0, 0, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    masks_12 = [True, False, False, True, False, False, False, False, False, False, False, False]
    scores_12 = [0.0] * 12

    class _WiredTokenizer12:
        def __call__(self, words, **kwargs) -> _Encoding:
            return _Encoding(word_ids_12)

    comp = _make_compressor(monkeypatch, _ScoreModel(scores_12, masks_12), _WiredTokenizer12())

    content = " ".join(f"w{i}" for i in range(12))
    result = comp.compress(content)  # target_ratio=None → mask path
    compressed_words = result.compressed.split()
    assert "w0" in compressed_words, (
        f"OR semantics: w0 (True,False) must be kept; got {result.compressed!r}"
    )
    assert "w1" in compressed_words, (
        f"OR semantics: w1 (False,True) must be kept; got {result.compressed!r}"
    )


# ---------------------------------------------------------------------------
# K-chunk  chunk-offset: 3-chunk selective-keep → exact output
# ---------------------------------------------------------------------------


class _SelectiveKeepModel:
    """Keeps words at specified global indices. Chunk-aware via word_ids."""

    def __init__(self, keep_global: frozenset[int]) -> None:
        self._keep = keep_global

    def get_keep_mask(self, input_ids, attention_mask, threshold: float = 0.5) -> list[list[bool]]:
        seq_len = input_ids.shape[1]
        return [[False] * seq_len]

    def get_scores(self, input_ids, attention_mask) -> list[list[float]]:
        seq_len = input_ids.shape[1]
        return [[0.0] * seq_len]


class _ChunkAwareTokenizer:
    """One token per word. chunk_words slicing gives offset-aware word_ids [0..N-1]."""

    def __call__(self, words, **kwargs) -> "_ChunkEncoding":
        n = len(words)
        return _ChunkEncoding(n)


class _ChunkEncoding:
    def __init__(self, n: int) -> None:
        self._n = n
        self._ids = _IdMatrix(1, n)

    def __getitem__(self, key: str) -> _IdMatrix:
        return self._ids

    def word_ids(self, batch_index: int = 0) -> list[int | None]:
        return list(range(self._n))


class _KeepEvenModel:
    """Keeps even-indexed words in the current chunk (wid is chunk-local 0..N-1).

    The compressor adds chunk_start, so global positions are correct only if
    wid + chunk_start math is right.
    """

    def get_keep_mask(self, input_ids, attention_mask, threshold: float = 0.5) -> list[list[bool]]:
        n = input_ids.shape[1]
        return [[i % 2 == 0 for i in range(n)]]

    def get_scores(self, input_ids, attention_mask) -> list[list[float]]:
        n = input_ids.shape[1]
        return [[1.0 if i % 2 == 0 else 0.0 for i in range(n)]]


def test_k_chunk_offset_exact_output(monkeypatch) -> None:
    """3-chunk selective-keep (even words in each chunk) → exact output string.

    Content: 'zero one two three four five six seven eight nine ten' (11 words).
    chunk_words=4 → chunks [0..3], [4..7], [8..10].
    Even chunk-local positions: 0,2 | 0,2 | 0,2 → global: 0,2,4,6,8,10.
    Words: zero(0), two(2), four(4), six(6), eight(8), ten(10).

    Mutation-sensitive: removing chunk_start offset → word ids repeat per chunk →
    output would be 'zero two zero two zero two' (wrong globals).
    """
    words = "zero one two three four five six seven eight nine ten"
    monkeypatch.setattr(kc, "_load_kompress", lambda mid, dev: (_KeepEvenModel(), _ChunkAwareTokenizer(), "onnx"))
    comp = KompressCompressor(KompressConfig(chunk_words=4, enable_ccr=False))
    result = comp.compress(words)
    assert result.compressed == "zero two four six eight ten", (
        f"chunk-offset must be applied; got {result.compressed!r}"
    )


# ---------------------------------------------------------------------------
# K-bnd  n_words < 10 boundary: 9 words → passthrough, 10 words → compress
# ---------------------------------------------------------------------------


def test_k_bnd_nine_words_passthrough(monkeypatch) -> None:
    """9 words: below the n_words < 10 threshold → passthrough (not attempted)."""
    content = " ".join(f"w{i}" for i in range(9))
    monkeypatch.setattr(kc, "_load_kompress", lambda mid, dev: (_KeepEvenModel(), _ChunkAwareTokenizer(), "onnx"))
    comp = KompressCompressor(KompressConfig(chunk_words=350, enable_ccr=False))
    result = comp.compress(content)
    # Short inputs pass through unchanged
    assert result.compressed == content, (
        f"9 words must passthrough; got {result.compressed!r}"
    )
    assert result.compression_ratio == 1.0


def test_k_bnd_ten_words_compressed(monkeypatch) -> None:
    """10 words: AT the threshold → compression is attempted."""
    content = " ".join(f"w{i}" for i in range(10))
    monkeypatch.setattr(kc, "_load_kompress", lambda mid, dev: (_KeepEvenModel(), _ChunkAwareTokenizer(), "onnx"))
    comp = KompressCompressor(KompressConfig(chunk_words=350, enable_ccr=False))
    result = comp.compress(content)
    # KeepEvenModel keeps even words (0,2,4,6,8) → 5 words out of 10
    assert result.compressed != content, "10 words must be attempted for compression"
    assert result.compression_ratio < 1.0
