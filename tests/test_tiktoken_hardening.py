"""P0-6: tiktoken hardening — bounded encoding load + special-token totality.

Two production failure modes pinned here:

1. **HANG**: the first ``tiktoken.get_encoding(...)`` for an encoding not
   cached on disk downloads the BPE vocab over the network. Nothing on
   that path had a timeout, so a stalled download hung ``compress()``
   indefinitely. The load now runs on a daemon worker thread with
   ``join(timeout)`` (10s default) — thread-based because compress() may
   run off the main thread, where signal-based timeouts cannot fire. The
   load is also EAGER (in ``TiktokenCounter.__init__``) so a
   failure/timeout surfaces at construction, where
   ``get_tokenizer()`` already degrades to the estimation
   fallback WITHOUT caching the failure (COR-40c: the next ``get_tokenizer()``
   retries the real tokenizer — a transient outage must not pin the
   model to chars/4 estimation for the process lifetime).

2. **CRASH**: ``encoding.encode(text)`` raises ValueError when the text
   contains a literal special-token string (``<|endoftext|>`` — common
   in scraped / LLM-adjacent tool output). count/encode now retry with
   ``disallowed_special=()``, tokenizing the literal as ordinary text.

RED evidence (pre-fix, captured 2026-07-03): ``count_text`` on a payload
containing ``<|endoftext|>`` raised ``ValueError: Encountered text
corresponding to disallowed special token '<|endoftext|>'``; the hung
``_get_encoding`` scenario made the FIRST ``count_text`` block without
bound (constructor never loaded, registry never saw the failure).
"""

from __future__ import annotations

import base64
import threading
import time

import pytest

import furl_ctx.tokenizers.tiktoken_counter as tiktoken_counter_module
from furl_ctx.tokenizers import EstimatingTokenCounter, TiktokenCounter, get_tokenizer
from furl_ctx.tokenizers import registry as tokenizer_registry
from furl_ctx.tokenizers.tiktoken_counter import (
    _MAX_SAFE_SAME_CLASS_RUN,
    _has_backtracking_prone_run,
)


class TestSpecialTokenLiterals:
    """`encoding.encode` must be total over arbitrary tool-output text."""

    def test_count_text_with_endoftext_literal_does_not_raise(self) -> None:
        counter = TiktokenCounter("gpt-4o")
        n = counter.count_text("scraped output containing <|endoftext|> literal")
        assert isinstance(n, int)
        assert n > 0

    def test_count_text_with_assorted_special_token_literals(self) -> None:
        counter = TiktokenCounter("gpt-4o")
        for payload in (
            "<|endoftext|>",
            "prefix <|endoftext|> suffix",
            "<|fim_prefix|>code<|fim_middle|>gap<|fim_suffix|>rest",
            "<|im_start|>assistant says hi<|im_end|>",
        ):
            assert counter.count_text(payload) > 0, payload

    def test_special_token_literal_counts_as_ordinary_text(self) -> None:
        """The retry must yield the ordinary-text tokenization — the
        literal is someone else's DATA, not a control token."""
        counter = TiktokenCounter("gpt-4o")
        payload = "x <|endoftext|> y"
        expected = len(counter.encoding.encode(payload, disallowed_special=()))
        assert counter.count_text(payload) == expected

    def test_encode_with_special_token_literal_round_trips(self) -> None:
        counter = TiktokenCounter("gpt-4o")
        payload = "a <|endoftext|> b"
        ids = counter.encode(payload)
        assert ids
        assert counter.decode(ids) == payload

    def test_count_messages_with_special_token_payload(self) -> None:
        """The compress-path entry point (count_messages → count_text)."""
        counter = TiktokenCounter("gpt-4o")
        messages = [
            {"role": "user", "content": "what does <|endoftext|> mean?"},
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": "the docs say <|endoftext|> terminates a document",
            },
        ]
        assert counter.count_messages(messages) > 0


class TestBoundedEncodingLoad:
    """First-time encoding acquisition must have a hard deadline."""

    @pytest.fixture(autouse=True)
    def _clean_registry_cache(self):
        """Isolate registry state: COR-40c semantics are exactly about
        what does / does not get cached across ``get()`` calls."""
        tokenizer_registry._cache.clear()
        yield
        tokenizer_registry._cache.clear()

    @staticmethod
    def _hung_get_encoding(release: threading.Event):
        """A `_get_encoding` stand-in that blocks like a stalled download."""

        def _hung(name: str):
            release.wait(5.0)
            raise RuntimeError("unreachable: test releases only to unblock the worker")

        return _hung

    def test_hung_encoding_load_raises_timeout_quickly(self, monkeypatch) -> None:
        """RED pre-fix: the constructor never touched the network (lazy
        load), so no TimeoutError was raised — and the eventual first
        count_text blocked without bound. GREEN: construction fails fast
        at the deadline."""
        release = threading.Event()
        monkeypatch.setattr(
            tiktoken_counter_module, "_get_encoding", self._hung_get_encoding(release)
        )
        monkeypatch.setattr(tiktoken_counter_module, "ENCODING_LOAD_TIMEOUT_SECONDS", 0.05)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            TiktokenCounter("gpt-4o")
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, f"timeout took {elapsed:.2f}s — deadline not enforced"
        release.set()  # let the daemon worker exit promptly

    def test_hung_load_degrades_to_estimation_and_is_not_pinned(self, monkeypatch) -> None:
        """Timeout → registry serves the estimation fallback for THIS
        call; once the hang clears, the next ``get()`` retries and gets
        the real tokenizer (COR-40c: failures are never cached)."""
        release = threading.Event()
        with monkeypatch.context() as m:
            m.setattr(tiktoken_counter_module, "_get_encoding", self._hung_get_encoding(release))
            m.setattr(tiktoken_counter_module, "ENCODING_LOAD_TIMEOUT_SECONDS", 0.05)

            degraded = get_tokenizer("gpt-4o")
            assert isinstance(degraded, EstimatingTokenCounter)
            assert degraded.count_text("hello world") > 0  # still counts, just estimates
        release.set()

        recovered = get_tokenizer("gpt-4o")  # patch lifted: creation retried
        assert isinstance(recovered, TiktokenCounter), (
            "a transient load timeout pinned gpt-4o to estimation for the "
            "process lifetime — the failure was cached (COR-40c regression)"
        )

    def test_load_error_degrades_to_estimation_and_is_not_pinned(self, monkeypatch) -> None:
        """The TEST-32b shape: a tiktoken build too old for the encoding
        raises ``ValueError: Unknown encoding o200k_base``. Must degrade
        to estimation for the call — and retry on the next get()."""

        def _broken(name: str):
            raise ValueError(f"Unknown encoding {name}")

        with monkeypatch.context() as m:
            m.setattr(tiktoken_counter_module, "_get_encoding", _broken)
            degraded = get_tokenizer("gpt-4o")
            assert isinstance(degraded, EstimatingTokenCounter)

        recovered = get_tokenizer("gpt-4o")
        assert isinstance(recovered, TiktokenCounter)

    def test_successful_construction_counts_normally(self) -> None:
        """The eager bounded load must not change the happy path."""
        counter = TiktokenCounter("gpt-4o")
        assert counter.encoding_name == "o200k_base"
        assert counter.count_text("hello world") > 0


class TestBacktrackingRunGuard:
    """MATRIX-03: an unbroken long same-class run must be REFUSED before the
    encode. tiktoken's split regex backtracks catastrophically on such runs — a
    hard raise on small-stack platforms (macOS) but a silent super-linear
    passthrough on larger-stack ones (CI Linux), where the router handed the 2 MB
    input back with ``error=None``. Rejecting the run here makes the decline
    loud and DETERMINISTIC on every platform (the ValueError rides compress()'s
    fail-open into ``result.error``)."""

    L = _MAX_SAFE_SAME_CLASS_RUN

    def test_predicate_flags_long_letter_run(self) -> None:
        assert _has_backtracking_prone_run("x" * (self.L + 1), self.L)
        # A run of DISTINCT letters is the same \p{L}+ pre-token — also flagged.
        distinct = "".join("abcdefghijklmnop"[i % 16] for i in range(self.L + 1))
        assert _has_backtracking_prone_run(distinct, self.L)

    def test_predicate_flags_long_whitespace_and_punct_runs(self) -> None:
        assert _has_backtracking_prone_run(" " * (self.L + 1), self.L)
        assert _has_backtracking_prone_run("." * (self.L + 1), self.L)

    def test_predicate_exempts_digit_runs(self) -> None:
        # o200k caps digit tokens at \p{N}{1,3}; long digit runs never backtrack.
        assert not _has_backtracking_prone_run("0" * (self.L * 2), self.L)

    def test_predicate_safe_for_mixed_class_and_split_runs(self) -> None:
        # base64 is mixed alnum: frequent class transitions keep every run short.
        b64 = base64.b64encode(bytes((i * 13) % 256 for i in range(self.L))).decode()
        assert len(b64) >= self.L
        assert not _has_backtracking_prone_run(b64, self.L)
        # A single digit between two sub-limit letter runs keeps each under limit.
        split = "a" * (self.L - 1) + "5" + "a" * (self.L - 1)
        assert not _has_backtracking_prone_run(split, self.L)

    def test_predicate_ignores_text_shorter_than_limit(self) -> None:
        assert not _has_backtracking_prone_run("x" * (self.L - 1), self.L)
        assert not _has_backtracking_prone_run("", self.L)

    def test_count_text_refuses_pathological_run_loudly(self) -> None:
        counter = TiktokenCounter("gpt-4o")
        with pytest.raises(ValueError, match="backtracking"):
            counter.count_text("x" * (2 * 1024 * 1024))

    def test_count_messages_surfaces_the_refusal(self) -> None:
        """The compress-path entry point (count_messages → count_text) surfaces
        the refusal, so compress() fail-open can turn it into result.error."""
        counter = TiktokenCounter("gpt-4o")
        messages = [{"role": "tool", "content": "x" * (2 * 1024 * 1024)}]
        with pytest.raises(ValueError, match="backtracking"):
            counter.count_messages(messages)

    def test_count_text_still_counts_large_safe_content(self) -> None:
        """Content LARGER than the run limit but mixed-class (no backtracking
        risk) must still tokenize normally — the guard is surgical."""
        counter = TiktokenCounter("gpt-4o")
        b64 = base64.b64encode(bytes((i * 13) % 256 for i in range(110_000))).decode()
        assert len(b64) > self.L
        assert counter.count_text(b64) > 0
