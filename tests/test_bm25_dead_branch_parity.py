"""Scoring-parity characterization tests for :class:`BM25Scorer`.

These tests exist to guard a dead-branch simplification of
``furl_ctx/relevance/bm25.py`` (the ``avgdl`` ``or 1`` fallback and the two
``math.log(2.0)`` idf fallbacks in ``_bm25_score``). The simplification is
required to be behavior-preserving, so this module pins ``score_batch``'s
observable output over a deliberately varied corpus of 200+ query-document
pairs: UUIDs, long tokens, duplicated terms, CJK, numbers-in-words, pure
numerics including non-ASCII Unicode digits, and empty edges.

The exhaustive float-identical before/after evidence for the deletion lives in
the PR body. This module locks the behavior permanently: a category-covering
sweep plus transparent per-case ``matched_terms`` pins catch shape and range
regressions, and a committed golden snapshot,
``tests/data/bm25_dead_branch_parity_golden.json``, pins the exact ``score``,
``reason``, and ``matched_terms`` for all 468 pairs, so a same-shape
regression invisible to the range check, such as a constant-IDF change,
fails loudly too.

One case is load bearing beyond parity: ``_TOKEN_PATTERN``'s ``\\b\\d{4,}\\b``
numeric-ID branch is NOT redundant with the ``[a-zA-Z0-9_]+`` branch, because
``\\d`` matches Unicode decimal digits that the ASCII class does not. The
Unicode-digit tests below fail if that branch is ever removed.
"""

from __future__ import annotations

import json
from pathlib import Path

from furl_ctx.relevance.bm25 import BM25Scorer

_GOLDEN_PATH = Path(__file__).resolve().parent / "data" / "bm25_dead_branch_parity_golden.json"

_UUID = "550e8400-e29b-41d4-a716-446655440000"
_UUID2 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
_FULLWIDTH_12345 = "１２３４５"  # noqa: RUF001 - fullwidth decimal digits, intentional
_ARABIC_1234 = "١٢٣٤"  # Arabic-Indic decimal digits, intentional
_DEVANAGARI_1234 = "१२३४"  # Devanagari decimal digits, intentional


def build_corpus() -> tuple[list[str], list[str]]:
    """Return a fixed, deterministic (documents, queries) corpus.

    ``len(documents) * len(queries)`` is the pair count and is asserted to be
    at least 200 by the coverage test below.
    """
    documents = [
        # UUIDs
        f'{{"id": "{_UUID}", "name": "Alice"}}',
        f'{{"id": "{_UUID2}", "kind": "namespace"}}',
        f"record {_UUID} updated by alice",
        # long tokens (trigger the >=8 char long-match bonus)
        "longerid other stuff here",
        "shortid other stuff here",
        "supercalifragilistic token here",
        # duplicated terms
        "alpha alpha alpha beta",
        "alpha beta gamma alpha",
        "repeat repeat repeat repeat repeat",
        # CJK ideographs (no ASCII alnum, so they drop out of tokenization)
        "这是一个测试文档 alpha",
        "日本語 のテキスト beta",
        "한국어 문서 gamma 1234",
        # numbers embedded in words
        "abc1234def token",
        "v2 build 2024 release",
        "code99 error42 warn7",
        # pure ASCII numerics
        "order 12345 shipped",
        "9999999999 total",
        "404 500 200 status",
        # pure Unicode-digit numerics (locks the numeric-ID branch)
        f"メッセージ {_FULLWIDTH_12345} 完了",
        f"رقم {_ARABIC_1234} مؤكد",
        f"संख्या {_DEVANAGARI_1234} पूर्ण",
        # JSON-like / mixed
        '{"status": "ok", "count": 42, "id": "abc"}',
        '{"error": "not found", "code": 404}',
        "user alice logged in from 192 168 1 42",
        # empty edges
        "",
        "   ",
        "!!! ??? ...",
        # filler for breadth
        "the quick brown fox jumps",
        "lorem ipsum dolor sit amet",
        "find record by id quickly",
        "alpha gamma",
        "gamma",
        "delta epsilon",
        "error error failed timeout",
        "python java rust golang",
        "hello WORLD Hello world",
        "token_with_underscore value",
        "aaaa bbbb cccc dddd",
        "1234 5678 9012 3456",
    ]
    queries = [
        f"find record {_UUID}",
        "alpha",
        "alpha alpha",
        "longerid shortid",
        "这是一个测试文档",
        "abc1234def",
        "12345",
        _FULLWIDTH_12345,
        _ARABIC_1234,
        "error timeout",
        "gamma delta",
        "",
    ]
    return documents, queries


def test_corpus_is_at_least_200_pairs() -> None:
    documents, queries = build_corpus()
    assert len(documents) * len(queries) >= 200


def test_every_pair_scores_in_unit_range_without_error() -> None:
    documents, queries = build_corpus()
    scorer = BM25Scorer()
    for query in queries:
        results = scorer.score_batch(documents, query)
        assert len(results) == len(documents)
        for result in results:
            assert 0.0 <= result.score <= 1.0
            assert isinstance(result.matched_terms, list)
            assert all(isinstance(term, str) for term in result.matched_terms)
            assert len(result.matched_terms) <= 5


def test_cjk_ideographs_produce_no_tokens() -> None:
    # CJK ideographs are neither ASCII alnum nor decimal digits, so a
    # CJK-only query tokenizes to nothing and every item scores the
    # empty-context path.
    results = BM25Scorer().score_batch(["这是一个测试文档 alpha", "beta"], "这是一个测试文档")
    assert all(result.score == 0.0 for result in results)
    assert all(result.reason == "BM25: empty context" for result in results)
    assert all(result.matched_terms == [] for result in results)


def test_fullwidth_unicode_digit_run_is_one_token_and_matches() -> None:
    # Locks the ``\b\d{4,}\b`` numeric-ID branch: fullwidth decimal digits
    # match ``\d`` but not ``[a-zA-Z0-9_]``. Removing that branch would drop
    # this token and change scoring, so this test guards against it.
    results = BM25Scorer().score_batch(
        [f"メッセージ {_FULLWIDTH_12345} 完了", "no digits here"], _FULLWIDTH_12345
    )
    assert results[0].matched_terms == [_FULLWIDTH_12345]
    assert results[1].matched_terms == []
    assert results[0].score > results[1].score


def test_arabic_indic_unicode_digit_run_is_one_token_and_matches() -> None:
    results = BM25Scorer().score_batch([f"رقم {_ARABIC_1234} مؤكد", "no digits here"], _ARABIC_1234)
    assert results[0].matched_terms == [_ARABIC_1234]
    assert results[1].matched_terms == []


def test_empty_query_scores_every_item_zero() -> None:
    documents, _ = build_corpus()
    results = BM25Scorer().score_batch(documents, "")
    assert all(result.score == 0.0 for result in results)
    assert all(result.reason == "BM25: empty context" for result in results)


def test_score_batch_matches_golden_snapshot_exactly() -> None:
    """Exact-value pin over all 468 pairs: score, reason, and matched_terms
    must match the committed golden snapshot bit for bit.

    ``repr`` of a Python float is the shortest string that round-trips back
    to the identical float, so comparing repr strings is equivalent to
    comparing exact bit patterns without a float-equality footgun. This is
    the one check in this module that a same-shape regression, such as a
    constant-IDF change, cannot pass silently: the range and matched_terms
    shape checks above stay green under that mutation, this does not. See
    ``tests/data/bm25_dead_branch_parity_golden.json``, regenerated from a
    known-good ``BM25Scorer()`` over ``build_corpus()``.
    """
    documents, queries = build_corpus()
    scorer = BM25Scorer()
    rows = []
    for qi, query in enumerate(queries):
        results = scorer.score_batch(documents, query)
        for di, result in enumerate(results):
            rows.append(
                {
                    "qi": qi,
                    "di": di,
                    "score": repr(result.score),
                    "reason": result.reason,
                    "matched_terms": list(result.matched_terms),
                }
            )

    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    rows.sort(key=lambda r: (r["qi"], r["di"]))
    golden.sort(key=lambda r: (r["qi"], r["di"]))
    assert rows == golden
