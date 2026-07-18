"""Byte-identity regression oracle for the ``ContentRouter.apply()`` extraction.

This is the frozen oracle guarding the maintainability refactor that moves the
Pass-1 classify loop (and the result-assembly tail) out of ``apply()`` and into
``router_engine``. The bar is ABSOLUTE byte-identity: the same corpus, run
through the real router with real compressors, must produce the same result
before and after the extraction.

Design (the four things a byte-identity oracle must get right):

1. **Frozen golden, not tautological.** The expected output lives in a JSON
   fixture on disk (:data:`GOLDEN_PATH`), generated ONCE from the pre-refactor
   code. The test loads that frozen file and asserts equality; it never
   recaptures a baseline in the same run, so it genuinely detects divergence
   introduced by the extraction. Regenerate ONLY by deleting the golden or
   running with ``FURL_REGEN_APPLY_GOLDEN=1`` on known-good code.

2. **``route_counts`` captured via a capturing observer.** ``route_counts`` is
   the primary risk surface of this refactor (it is all counter bookkeeping)
   and it is NOT part of :class:`TransformResult`. It is forwarded once per
   ``apply()`` through ``observer.record_router_route_counts(...)``. The
   :class:`_CapturingObserver` records it so the oracle diffs it.

3. **Timing VALUES excluded.** ``TransformResult.timing`` holds
   ``perf_counter()`` wall-clock millis — non-deterministic. The oracle records
   the sorted timing KEY SET (structural, deterministic) but never the values.

4. **Corpus varies config AND kwargs**, not just message shapes, so every
   ``match`` arm of the Pass-1 loop is driven: Frozen, ProtectedMsg (user /
   system / excluded-tool / recent-code / error-output / already-compressed),
   ContentBlocks, NonString, Small, and Compressible resolving to each of
   ServeOriginal / ServeCached / Recompute, plus the net-mutation gate on both
   the cache-hit and Pass-2/3 serve sites, and a multi-pending case that
   exercises the parallel Pass-2 executor.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from furl_ctx.tokenizer import Tokenizer
from furl_ctx.tokenizers import EstimatingTokenCounter
from furl_ctx.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    _result_cache_key,
)

GOLDEN_PATH = Path(__file__).parent / "data" / "apply_identity_golden.json"


# --------------------------------------------------------------------------- #
# Capturing observer — records route_counts (not in TransformResult).
# --------------------------------------------------------------------------- #
class _CapturingObserver:
    """Records every ``record_router_route_counts`` payload for the oracle.

    ``record_compression`` is a required protocol method but its token numbers
    are already reflected in the compressed message bytes; we keep only the
    per-``apply()`` merged ``route_counts`` (the counter-bookkeeping surface the
    extraction rearranges).
    """

    def __init__(self) -> None:
        self.route_counts: list[dict[str, int]] = []

    def record_compression(
        self, *, strategy: str, original_tokens: int, compressed_tokens: int
    ) -> None:  # pragma: no cover - trivial sink
        pass

    def record_router_route_counts(self, route_counts: dict[str, int], /) -> None:
        # Copy: the router hands over its live Counter; freeze a plain dict.
        self.route_counts.append(dict(route_counts))


def _make_tokenizer() -> Tokenizer:
    """Deterministic, dependency-free tokenizer (the router-test standard)."""
    return Tokenizer(EstimatingTokenCounter())


# --------------------------------------------------------------------------- #
# Deterministic message builders — each clears exactly the gates it needs to.
# --------------------------------------------------------------------------- #
def _compressible_log(n: int = 30) -> str:
    """Plain-prose tool output well over the raw-``apply()`` 50-token floor:
    no source code, no error indicators, no CCR marker → reaches the Tier-1/
    Tier-2 cache lookup and (cache-cold) the Recompute → Pass-2/3 path."""
    return " ".join(
        f"Record {i}: throughput {1000 + i} rps, latency {10 + i % 7} ms, "
        f"region {i % 5}, status nominal, notes none for this interval."
        for i in range(n)
    )


def _compressible_log_b(n: int = 26) -> str:
    """A second, distinct compressible body (different cache key) for the
    multi-pending parallel case."""
    return " ".join(
        f"Entry {i}: queue depth {i % 9}, retries {i % 3}, shard {i % 4}, "
        f"outcome accepted, elapsed {5 + i % 11} ms, comment none recorded."
        for i in range(n)
    )


def _tool_message(content: Any, tool_call_id: str = "call_pin") -> dict[str, Any]:
    return {"role": "tool", "content": content, "tool_call_id": tool_call_id}


def _tool_result_block_message(text: str) -> dict[str, Any]:
    """Canonical Anthropic tool_result block inside a user-role message → the
    ContentBlocks arm."""
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu_pin", "content": text}],
    }


def _routable_block_text(n: int = 20) -> str:
    """Over the 500-char block floor, no error/CCR markers → the block walker
    compresses it."""
    return " ".join(
        f"Record {i}: throughput {1000 + i} rps, latency {10 + i % 7} ms, "
        f"region {i % 5}, status nominal, notes none for this interval."
        for i in range(n)
    )


def _error_output() -> str:
    """A raw traceback with strong error indicators, over the 50-token floor and
    under the 8000-char cap → error-protected (``_is_unstructured_error_output``).
    """
    return (
        "Traceback (most recent call last):\n"
        '  File "/app/handler.py", line 42, in process\n'
        "    result = self.transform(payload)\n"
        '  File "/app/transform.py", line 17, in transform\n'
        '    raise ValueError("invalid payload shape")\n'
        "ValueError: invalid payload shape\n"
        "During handling of the above exception, another exception occurred:\n"
        "RuntimeError: task failed permanently after 3 retries\n"
    )


def _ccr_marker() -> str:
    """A CCR retrieval marker matching the live ``DOUBLE_ANGLE_PATTERN`` grammar
    (``<<ccr:`` + 12 or 24 lowercase-hex + terminator). Verified against
    ``_looks_like_ccr_output`` in the corpus-coverage self-check below."""
    return "<<ccr:0123456789ab>>"


def _big_protected_suffix() -> str:
    """A large user message (skip_user protects it) placed AFTER a compressible
    message so its token count forms the net-mutation-gate suffix. Sized so the
    ``tokens_after * (1 - cached_rate)`` cache re-billing penalty exceeds the
    tokens the compression would save → the gate suppresses compression."""
    return "please continue the analysis of the preceding data set carefully " * 700


# --------------------------------------------------------------------------- #
# Corpus — each case is (config, messages, kwargs). A capturing observer and a
# fresh tokenizer are supplied by the runner. Pre-seed hooks mutate the router's
# cache before apply() to force ServeCached / ServeOriginal.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    name: str
    config: ContentRouterConfig
    messages: list[dict[str, Any]]
    kwargs: dict[str, Any] = field(default_factory=dict)
    # Optional cache pre-seed: called with the constructed router before apply.
    preseed: Any = None


def _preseed_serve_cached(content: str) -> Any:
    def _seed(router: ContentRouter) -> None:
        # A cached compression whose ratio (0.10) clears the default min_ratio
        # → served from cache (ServeCached).
        router._cache.put(
            _result_cache_key(content, 1.0), content[: max(1, len(content) // 4)], 0.10, "log"
        )

    return _seed


def _preseed_serve_original(content: str) -> Any:
    def _seed(router: ContentRouter) -> None:
        # Tier-1 skip hit: known non-compressible → served verbatim (ServeOriginal).
        router._cache.mark_skip(_result_cache_key(content, 1.0))

    return _seed


def _build_corpus() -> list[Case]:
    log = _compressible_log()
    log_b = _compressible_log_b()
    block_text = _routable_block_text()

    cases: list[Case] = [
        # --- Frozen: leading frozen-prefix message, byte-identical, no book. ---
        Case(
            name="frozen_prefix",
            config=ContentRouterConfig(),
            messages=[_tool_message(log), _tool_message(_compressible_log(28), "c2")],
            kwargs={"frozen_message_count": 1},
        ),
        # --- ProtectedMsg: user message (skip_user default True). ---
        Case(
            name="protected_user",
            config=ContentRouterConfig(),
            messages=[{"role": "user", "content": log}],
        ),
        # --- ProtectedMsg: system message (skip_system default True). ---
        Case(
            name="protected_system",
            config=ContentRouterConfig(),
            messages=[{"role": "system", "content": log}],
        ),
        # --- ProtectedMsg: excluded tool (Read) within protection window. ---
        Case(
            name="protected_excluded_tool",
            config=ContentRouterConfig(),
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_read", "function": {"name": "Read"}, "type": "function"}
                    ],
                },
                _tool_message(log, "call_read"),
            ],
        ),
        # --- ProtectedMsg: recent code (protect_recent). ---
        Case(
            name="protected_recent_code",
            config=ContentRouterConfig(),
            messages=[
                _tool_message(
                    "def handler(self, request):\n"
                    "    result = self.process(request)\n"
                    "    for item in result.items:\n"
                    "        item.validate()\n"
                    "    return result\n" * 4
                )
            ],
            kwargs={"protect_recent": 5},
        ),
        # --- ProtectedMsg: error output. ---
        Case(
            name="protected_error_output",
            config=ContentRouterConfig(),
            messages=[_tool_message(_error_output())],
        ),
        # --- AlreadyCompressed: real CCR marker pinning. ---
        Case(
            name="already_compressed",
            config=ContentRouterConfig(),
            messages=[_tool_message(_ccr_marker() + " " + log)],
        ),
        # --- ContentBlocks: Anthropic tool_result block. ---
        Case(
            name="content_blocks_tool_result",
            config=ContentRouterConfig(),
            messages=[_tool_result_block_message(block_text)],
        ),
        # --- NonString: dict content (neither str nor list). ---
        Case(
            name="non_string_content",
            config=ContentRouterConfig(),
            messages=[{"role": "tool", "content": {"weird": "shape"}, "tool_call_id": "c"}],
        ),
        # --- Small: below the token floor. ---
        Case(
            name="small_content",
            config=ContentRouterConfig(),
            messages=[_tool_message("tiny output")],
        ),
        # --- Compressible → Recompute (cold cache, single pending). ---
        Case(
            name="compressible_recompute_single",
            config=ContentRouterConfig(),
            messages=[_tool_message(log)],
        ),
        # --- Compressible → Recompute, multi-pending (parallel Pass-2). ---
        Case(
            name="compressible_recompute_multi",
            config=ContentRouterConfig(),
            messages=[
                _tool_message(log, "c1"),
                _tool_message(log_b, "c2"),
                _tool_message(_compressible_log(24), "c3"),
            ],
        ),
        # --- Compressible → ServeCached (pre-seeded low-ratio entry). ---
        Case(
            name="compressible_serve_cached",
            config=ContentRouterConfig(),
            messages=[_tool_message(log)],
            preseed=_preseed_serve_cached(log),
        ),
        # --- Compressible → ServeOriginal (pre-seeded Tier-1 skip). ---
        Case(
            name="compressible_serve_original",
            config=ContentRouterConfig(),
            messages=[_tool_message(log)],
            preseed=_preseed_serve_original(log),
        ),
        # --- net_mutation_gate on the Recompute serve site (position econ):
        # a large protected suffix makes the cache re-billing penalty exceed the
        # compression savings, so the freshly-compressed result is rejected. ---
        Case(
            name="net_mutation_gate_recompute",
            config=ContentRouterConfig(enable_net_mutation_gate=True, cached_token_rate=0.9),
            messages=[
                _tool_message(log, "c1"),
                {"role": "user", "content": _big_protected_suffix()},
            ],
            kwargs={"model_limit": 1_000_000},
        ),
        # --- net_mutation_gate on the ServeCached hit site: the gate is
        # re-evaluated on cache hits (content-keyed cache, position-dependent
        # gate), so the same large suffix rejects the cached compression too. ---
        Case(
            name="net_mutation_gate_serve_cached",
            config=ContentRouterConfig(enable_net_mutation_gate=True, cached_token_rate=0.9),
            messages=[
                _tool_message(log, "c1"),
                {"role": "user", "content": _big_protected_suffix()},
            ],
            kwargs={"model_limit": 1_000_000},
            preseed=_preseed_serve_cached(log),
        ),
        # --- Adaptive ratio under pressure (model_limit drives context_pressure). ---
        Case(
            name="adaptive_ratio_under_pressure",
            config=ContentRouterConfig(),
            messages=[_tool_message(log)],
            kwargs={"model_limit": 700},
        ),
        # --- Mixed corpus: many arms in one call, order preserved. ---
        Case(
            name="mixed_diverse_corpus",
            config=ContentRouterConfig(),
            messages=[
                {"role": "system", "content": log},
                {"role": "user", "content": log},
                _tool_message(log, "c1"),
                _tool_message("tiny", "c2"),
                _tool_message(_error_output(), "c3"),
                _tool_result_block_message(block_text),
                {"role": "tool", "content": {"weird": 1}, "tool_call_id": "c4"},
                _tool_message(log_b, "c5"),
            ],
        ),
    ]
    return cases


# --------------------------------------------------------------------------- #
# Runner + normalizer.
# --------------------------------------------------------------------------- #
def _run_case(case: Case) -> dict[str, Any]:
    """Execute one case through the real router and normalize to a JSON-able
    dict. Timing VALUES are dropped; the sorted timing key set is kept."""
    observer = _CapturingObserver()
    router = ContentRouter(case.config, observer=observer)
    if case.preseed is not None:
        case.preseed(router)
    # Deep-copy messages so pre-seed/apply never mutates the shared corpus.
    messages = [dict(m) for m in case.messages]
    result = router.apply(messages, _make_tokenizer(), **case.kwargs)
    return {
        "messages": result.messages,
        "transforms_applied": list(result.transforms_applied),
        "tokens_before": result.tokens_before,
        "tokens_after": result.tokens_after,
        "markers_inserted": list(result.markers_inserted),
        "warnings": list(result.warnings),
        "timing_keys": sorted(result.timing.keys()),
        "route_counts": observer.route_counts,
    }


def _capture_all() -> dict[str, Any]:
    return {case.name: _run_case(case) for case in _build_corpus()}


def _canonical(obj: Any) -> str:
    """Stable JSON serialization for equality comparison + on-disk golden."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2)


def _regen_golden() -> None:
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(_canonical(_capture_all()) + "\n", encoding="utf-8")


# Regenerate the golden when explicitly asked (only on known-good code).
if os.environ.get("FURL_REGEN_APPLY_GOLDEN") == "1":  # pragma: no cover - tooling
    _regen_golden()


def _load_golden() -> dict[str, Any]:
    if not GOLDEN_PATH.exists():  # pragma: no cover - first-run bootstrap guard
        pytest.fail(
            f"golden fixture missing at {GOLDEN_PATH}; regenerate on known-good "
            f"code with FURL_REGEN_APPLY_GOLDEN=1"
        )
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_apply_corpus_matches_frozen_golden() -> None:
    """The whole corpus, run through the real router, matches the frozen golden.

    A single whole-corpus diff (rather than per-case) keeps the failure output
    honest: if the extraction changes any byte of any arm, the JSON differs.
    """
    golden = _load_golden()
    live = _capture_all()
    # Compare via canonical JSON so the diff pytest prints is line-oriented and
    # readable, and so key ordering never causes a false mismatch.
    assert _canonical(live) == _canonical(golden)


@pytest.mark.parametrize("case", _build_corpus(), ids=lambda c: c.name)
def test_apply_case_matches_frozen_golden(case: Case) -> None:
    """Per-case identity — pinpoints WHICH arm diverged when one does."""
    golden = _load_golden()
    assert case.name in golden, f"case {case.name!r} missing from golden — regenerate"
    assert _canonical(_run_case(case)) == _canonical(golden[case.name])


def _nonzero_route_counts(result: dict[str, Any]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for payload in result["route_counts"]:
        for key, value in payload.items():
            if value:
                merged[key] = merged.get(key, 0) + value
    return merged


def test_corpus_covers_every_routing_arm() -> None:
    """Guard against silent coverage rot: assert each intended arm actually
    fired. An oracle that stops exercising an arm stops protecting it, so a
    fixture that drifts out of its target branch must fail here rather than
    quietly pass the identity diff on a shrinking corpus.

    Keyed on the observable ``route_counts`` reason each case is built to
    trigger. Arms with no counter (Frozen books nothing) are asserted through
    their transform signature instead.
    """
    live = _capture_all()
    expected_counter: dict[str, str] = {
        "protected_user": "user_msg",
        "protected_system": "system_msg",
        "protected_excluded_tool": "excluded_tool",
        "protected_recent_code": "recent_code",
        "protected_error_output": "error_protected",
        "content_blocks_tool_result": "content_blocks",
        "non_string_content": "non_string",
        "small_content": "small",
        "compressible_recompute_single": "cache_miss",
        "compressible_serve_cached": "cache_hit",
        "compressible_serve_original": "cache_hit",
        "net_mutation_gate_recompute": "net_mutation_gate",
        "net_mutation_gate_serve_cached": "net_mutation_gate",
    }
    for case_name, counter in expected_counter.items():
        merged = _nonzero_route_counts(live[case_name])
        assert merged.get(counter, 0) >= 1, (
            f"case {case_name!r} was built to trigger route_counts[{counter!r}] "
            f"but that arm did not fire (got {merged}); the fixture has drifted "
            f"out of its target branch"
        )

    # already_compressed: pinning books nothing beyond serving the message
    # verbatim; assert the CCR-marked message is returned byte-identical and no
    # compression transform was emitted for it.
    ac = live["already_compressed"]
    assert ac["transforms_applied"] == ["router:noop:already_compressed"], (
        "already_compressed case did not pin — the CCR marker no longer matches "
        "_looks_like_ccr_output; update _ccr_marker()"
    )

    # multi-pending parallel Pass-2: three distinct compressible bodies must all
    # recompute in one call.
    multi = _nonzero_route_counts(live["compressible_recompute_multi"])
    assert multi.get("cache_miss", 0) == 3, (
        f"multi-pending case must drive three parallel recompressions, got {multi}"
    )

    # frozen_prefix: the frozen leading message must be served byte-identical.
    frozen_case = next(c for c in _build_corpus() if c.name == "frozen_prefix")
    assert live["frozen_prefix"]["messages"][0]["content"] == frozen_case.messages[0]["content"]
