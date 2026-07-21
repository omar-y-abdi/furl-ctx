"""Direct unit tests for :class:`BM25Scorer` (``furl_ctx.relevance.bm25``).

Before this file, the only test that named ``BM25Scorer``'s behavior directly
was ``tests/test_ccr.py::test_search_with_bm25``, a single loose integration
check (``len(results) >= 1``, "should prioritize Python items" via ordering)
routed through ``CompressionStore.search``. A handful of other files
(``test_compression_store_search_all.py`` and friends) exercise
``CompressionStore.search`` / ``search_all`` and mention BM25 only in prose
describing the ranking mechanism -- none assert anything BM25-specific, and
none import or construct ``BM25Scorer``. Nothing anywhere pinned an exact
score, a tokenization boundary, or the IDF/length-normalization/bonus
arithmetic directly, even though this scorer sits on ``CompressionStore``'s
retrieval hot path (``furl_ctx/cache/compression_store.py``'s ``search`` /
``search_all``): a silent drift here degrades which cached content a query
actually retrieves, with no error and no failing test.

Proof the gap was real (see the PR body for the full transcript): mutating
the long-match bonus threshold in ``furl_ctx/relevance/bm25.py`` (the ``len(t)
>= 8`` check in ``score_batch``) from 8 to 3 characters left the ENTIRE
existing suite green, including ``tests/`` files that reference "bm25" or
"relevance" by name. ``test_long_match_bonus_applies_at_exactly_eight_chars``
below fails on that exact mutation (see the PR body's red-proof transcript).

The score assertions here are pinned to concrete values hand-derived from the
module's own documented formula, see the ``BM25Scorer`` class docstring,
computed independently for a small, fully-worked corpus, rather than merely
"greater than zero". The tokenization-boundary tests instead drive
``score_batch`` and assert exact ``matched_terms``, so the UUID, digit-in-word,
and numeric-ID boundaries stay pinned through the public API even if the
private ``_tokenize`` helper is later renamed or inlined. See
``tests/test_bm25_dead_branch_parity.py`` for the same boundaries exercised
across a 200+ pair corpus including non-ASCII Unicode digits.
"""

from __future__ import annotations

import math

from furl_ctx.relevance.bm25 import BM25Scorer

# --------------------------------------------------------------------------- #
# Tokenization
# --------------------------------------------------------------------------- #


def test_tokenize_empty_string_returns_no_tokens() -> None:
    assert BM25Scorer()._tokenize("") == []


def test_tokenize_splits_on_non_alphanumeric_delimiters() -> None:
    assert BM25Scorer()._tokenize("ID-1234 code99 X") == ["id", "1234", "code99", "x"]


# --------------------------------------------------------------------------- #
# Tokenization boundaries pinned through the PUBLIC API (score_batch +
# matched_terms), so they survive a behavior-preserving rename or inline of the
# private _tokenize helper. The UUID and digit-in-word cases replace the former
# direct _tokenize assertions for exactly those two boundaries.
# --------------------------------------------------------------------------- #


def test_uuid_is_preserved_as_one_token_through_score_batch() -> None:
    # A UUID matches only when the whole 36-char token is present: a document
    # holding the identical UUID matches it, one whose UUID differs by a single
    # hex character does not. That proves the UUID is one token, not split into
    # hex runs that would match piecewise.
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    altered = "550e8400-e29b-41d4-a716-44665544ffff"
    results = BM25Scorer().score_batch(
        [f'{{"id": "{uuid}"}}', f'{{"id": "{altered}"}}'], f"find {uuid}"
    )
    assert results[0].matched_terms == [uuid]
    assert results[1].matched_terms == []
    assert results[0].score > results[1].score


def test_digits_embedded_in_word_stay_one_token_through_score_batch() -> None:
    # "abc1234def" is one token: it matches a document containing it verbatim
    # and not one where the same characters are split by spaces. The 4+ digit
    # numeric-ID boundary is covered across the corpus, including the non-ASCII
    # Unicode-digit case, by tests/test_bm25_dead_branch_parity.py.
    results = BM25Scorer().score_batch(["abc1234def zeta", "abc 1234 def zeta"], "abc1234def")
    assert results[0].matched_terms == ["abc1234def"]
    assert results[1].matched_terms == []


# --------------------------------------------------------------------------- #
# IDF
# --------------------------------------------------------------------------- #


def test_compute_idf_zero_doc_freq_returns_zero_without_dividing() -> None:
    assert BM25Scorer()._compute_idf("x", doc_count=5, doc_freq=0) == 0.0


def test_compute_idf_matches_hand_derived_value_for_term_in_every_doc() -> None:
    # N=1, n=1: log((1 - 1 + 0.5) / (1 + 0.5) + 1) = log(4/3).
    idf = BM25Scorer()._compute_idf("x", doc_count=1, doc_freq=1)
    assert idf == math.log(4.0 / 3.0)


# --------------------------------------------------------------------------- #
# score_batch: degenerate inputs
# --------------------------------------------------------------------------- #


def test_score_batch_empty_items_returns_empty_list() -> None:
    assert BM25Scorer().score_batch([], "hello") == []


def test_score_batch_empty_context_scores_every_item_zero() -> None:
    results = BM25Scorer().score_batch(["a b c", "d e f"], "")
    assert [r.score for r in results] == [0.0, 0.0]
    assert all(r.reason == "BM25: empty context" for r in results)
    assert all(r.matched_terms == [] for r in results)


def test_score_batch_no_matching_terms_scores_zero_with_no_matches_reason() -> None:
    result = BM25Scorer().score_batch(["completely unrelated document"], "zzz")[0]
    assert result.score == 0.0
    assert result.reason == "BM25: no matches"
    assert result.matched_terms == []


# --------------------------------------------------------------------------- #
# score_batch: exact-value pins for the core formula
# --------------------------------------------------------------------------- #
#
# Corpus: items = ["alpha alpha beta", "gamma"], context = "alpha gamma".
# doc_freq_across = {"alpha": 1, "beta": 1, "gamma": 1} (2 docs total), so
# idf("alpha") == idf("gamma") == log(2) -- the difference between the two
# items' scores below comes entirely from the length-normalization term.
#
# Hand-worked with k1=1.5, b=0.75, avgdl=(3+1)/2=2.0:
#   item 0, term "alpha": doc_len=3, f=2
#     num = 2 * 2.5 = 5
#     den = 2 + 1.5 * (0.25 + 0.75 * 3/2) = 2 + 1.5 * 1.375 = 4.0625
#     score = log(2) * 5 / 4.0625
#   item 1, term "gamma": doc_len=1, f=1
#     num = 1 * 2.5 = 2.5
#     den = 1 + 1.5 * (0.25 + 0.75 * 1/2) = 1 + 1.5 * 0.625 = 1.9375
#     score = log(2) * 2.5 / 1.9375


def test_score_batch_matches_hand_derived_values_for_worked_corpus() -> None:
    items = ["alpha alpha beta", "gamma"]
    results = BM25Scorer(normalize_score=False).score_batch(items, "alpha gamma")

    expected_item0 = math.log(2.0) * 5.0 / 4.0625
    expected_item1 = math.log(2.0) * 2.5 / 1.9375

    assert results[0].score == expected_item0
    assert results[0].matched_terms == ["alpha"]
    assert results[1].score == expected_item1
    assert results[1].matched_terms == ["gamma"]


def test_duplicate_query_terms_scale_score_linearly_with_query_frequency() -> None:
    # "alpha" appears in 2 of 3 docs and "beta"/"gamma"/"delta"/"epsilon"
    # never repeat, so a repeated query term is the only source of the
    # doubling: query frequency (qf) is a pure linear multiplier per the
    # module's own formula (`score += term_score * qf`).
    items = ["alpha beta", "alpha gamma", "delta epsilon"]
    scorer = BM25Scorer(normalize_score=False)

    single = scorer.score_batch(items, "alpha")[0].score
    double = scorer.score_batch(items, "alpha alpha")[0].score

    # Derived symbolically like the worked-corpus test above, not pasted
    # machine output. "alpha" is in 2 of 3 docs, so
    #   idf("alpha") = log((3 - 2 + 0.5) / (2 + 0.5) + 1) = log(1.6).
    # For item 0: doc_len=2, avgdl=(2+2+2)/3=2.0, f=1:
    #   num = 1 * (k1 + 1) = 2.5
    #   den = 1 + 1.5 * (0.25 + 0.75 * 2/2) = 2.5
    # so single = log(1.6) * 2.5 / 2.5. The operation order matters: that
    # product-then-quotient is 1 ULP off from log(1.6) itself, which is why
    # pinning the bare log(1.6) would fail.
    expected_single = math.log(1.6) * 2.5 / 2.5
    assert single == expected_single
    assert double == expected_single * 2
    assert double == single * 2


# --------------------------------------------------------------------------- #
# Long-match bonus: the guarded, previously-unpinned behavior (see module
# docstring above for the mutation that survived the full suite without it).
# --------------------------------------------------------------------------- #


def test_long_match_bonus_applies_at_exactly_eight_chars_not_seven() -> None:
    # Two isolated single-term corpora differing only in the matched term's
    # length (7 vs 8 chars): the 8-char term earns the same base BM25 term
    # score PLUS the 0.3 long-match bonus; the 7-char term earns none.
    items = ["shortid other stuff here", "longerid other stuff here"]
    results = BM25Scorer().score_batch(items, "shortid longerid")

    assert results[0].matched_terms == ["shortid"]  # 7 chars
    assert results[0].score == 0.06931471805599453
    assert results[1].matched_terms == ["longerid"]  # 8 chars
    assert results[1].score == 0.36931471805599453
    # The 0.3 bonus is the only difference between the two scores. Use isclose
    # so the subtraction is not a float-equality trap under operand change.
    assert math.isclose(results[1].score - results[0].score, 0.3, abs_tol=1e-12)


def test_normalization_divides_by_max_score_before_the_bonus_is_added() -> None:
    # The bonus is added AFTER raw_score/max_score, not before: with the
    # default max_score=10.0, a term scoring log(4/3) pre-bonus normalizes
    # to log(4/3)/10 and the 0.3 bonus is added on top of that, not on top
    # of the un-normalized raw term score.
    items = ["longerid other stuff here"]  # single-item corpus: avgdl == doc_len
    raw = BM25Scorer(normalize_score=False).score_batch(items, "longerid")[0].score
    normalized = (
        BM25Scorer(normalize_score=True, max_score=10.0).score_batch(items, "longerid")[0].score
    )

    assert raw == 0.5876820724517808
    assert normalized == 0.3287682072451781
    # Order of operations: (raw - bonus) / max_score + bonus. isclose keeps
    # this from being a float-equality trap that holds only for these operands.
    assert math.isclose(normalized, (raw - 0.3) / 10.0 + 0.3, abs_tol=1e-12)


def test_normalized_score_clamps_to_one_when_it_would_exceed_max_score() -> None:
    result = BM25Scorer(normalize_score=True, max_score=0.05).score_batch(
        ["longerid other stuff here"], "longerid"
    )[0]
    assert result.score == 1.0


# --------------------------------------------------------------------------- #
# matched_terms is capped at 5 even when more terms match
# --------------------------------------------------------------------------- #


def test_matched_terms_capped_at_five_even_with_more_matches() -> None:
    context = "one two three four five six seven"
    result = BM25Scorer().score_batch([context], context)[0]
    assert result.matched_terms == ["one", "two", "three", "four", "five"]
    assert len(result.matched_terms) == 5
