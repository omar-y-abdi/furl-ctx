"""TiktokenCounter huge-content estimate (PERF).

Above ``_ESTIMATE_ABOVE_LEN`` ``count_text`` extrapolates the token count from a
bounded prefix instead of a full ReDoS-guard scan + BPE encode over tens of MB —
that exact count is discarded by the router's wholesale offload, and computing it
exactly was the dominant cost of ingesting a large trace. These pin: (1) below
the threshold the count stays EXACT; (2) above it the estimate is close AND fast;
(3) a degenerate huge same-class run no longer raises (it offloads instead).
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("tiktoken")

from furl_ctx.tokenizers.tiktoken_counter import TiktokenCounter  # noqa: E402


@pytest.fixture
def counter() -> TiktokenCounter:
    return TiktokenCounter("gpt-4o")


def test_below_threshold_is_exact(counter: TiktokenCounter) -> None:
    # A string just under the estimate threshold is counted by real encoding,
    # so it matches the raw tiktoken length exactly.
    text = "the quick brown fox " * 20_000  # ~400 KB, < 8 MiB
    assert len(text) < counter._ESTIMATE_ABOVE_LEN
    exact = len(counter._encode_tolerant(text))
    assert counter.count_text(text) == exact


def test_above_threshold_estimate_is_close_and_fast(counter: TiktokenCounter) -> None:
    # A >= 8 MiB homogeneous JSON payload: the estimate should land within a few
    # percent of the true count (homogeneous → the prefix is representative) and
    # complete far faster than a full encode of the whole body.
    unit = '{"row":123,"ph":"X","name":"RunTask","dur":42} '
    text = unit * ((9 * 1024 * 1024) // len(unit) + 1)
    assert len(text) >= counter._ESTIMATE_ABOVE_LEN

    t0 = time.perf_counter()
    estimated = counter.count_text(text)
    est_elapsed = time.perf_counter() - t0

    exact = len(counter._encode_tolerant(text))
    rel_err = abs(estimated - exact) / exact
    assert rel_err < 0.05, f"estimate {estimated} vs exact {exact} (rel_err {rel_err:.3f})"
    # The estimate encodes only a 1 MiB prefix, so it is a large fraction faster
    # than encoding the whole ~9 MiB body — assert a real, generous speedup.
    t1 = time.perf_counter()
    len(counter._encode_tolerant(text))
    full_elapsed = time.perf_counter() - t1
    assert est_elapsed < full_elapsed, (
        f"estimate {est_elapsed:.3f}s not faster than full encode {full_elapsed:.3f}s"
    )


def test_estimate_is_cached(counter: TiktokenCounter) -> None:
    # The estimate is memoized like an exact count, so a re-count (the router
    # re-reads original_tokens) returns the SAME value without re-sampling.
    text = "x" * (9 * 1024 * 1024)  # degenerate but huge
    first = counter.count_text(text)
    assert text in counter._count_cache
    assert counter.count_text(text) == first


def test_degenerate_huge_run_does_not_raise(counter: TiktokenCounter) -> None:
    # A single-class run far above _MAX_SAFE_SAME_CLASS_RUN would make the EXACT
    # path raise (to dodge catastrophic backtracking). Above the estimate
    # threshold there is no full encode to protect, so it must NOT raise — the
    # content simply offloads. A positive, finite count comes back.
    text = "a" * (9 * 1024 * 1024)
    n = counter.count_text(text)
    assert n > 0


def test_exact_path_still_refuses_pathological_below_threshold(counter: TiktokenCounter) -> None:
    # Regression guard: the ReDoS refusal is intact for content BELOW the
    # estimate threshold (a 200 KB single-class run, over _MAX_SAFE_SAME_CLASS_RUN
    # but under 8 MiB), so the estimate branch did not weaken the exact path.
    text = "a" * 200_000
    assert len(text) < counter._ESTIMATE_ABOVE_LEN
    with pytest.raises(ValueError, match="same-class run"):
        counter.count_text(text)
