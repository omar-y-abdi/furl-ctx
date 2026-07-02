"""Per-request runtime-options isolation on a SHARED ContentRouter.

The proxy reuses ONE ``ContentRouter`` / ``SmartCrusher`` (and one module
level ``compress()`` pipeline) across every concurrent request. Per-request
options — ``target_ratio`` / ``force_kompress`` / ``kompress_model`` —
are NOT router state. They are carried as a frozen
``RouterRuntime`` value passed by argument down the call chain
(``compress(..., runtime=...)``), so two concurrent calls hold distinct
instances and neither can observe the other's options.

These tests pin that isolation contract THROUGH THE PUBLIC PATH (no
``_runtime_*`` poke exists any more):

* mechanism level — a SHARED router, N concurrent ``compress()`` calls each
  with a DISTINCT ``RouterRuntime``, spied AT THE CONSUMER read site
  (``_get_kompress`` / the Kompress ``compress(target_ratio=...)`` call). A
  barrier inside the spy parks every thread at the read simultaneously, so a
  reintroduced shared per-request field would deterministically yield
  last-writer-wins and the per-thread assertions would fail.
* end-to-end — concurrent ``compress()`` calls with different runtime options
  on the shared pipeline produce per-config-deterministic results
  (concurrent == serial), never a crash or cross-contaminated output.

BITE NOTE (why the consumer-spy, not output-equality): Kompress ML is not
loadable in CI, so output-equality is vacuous (prose routes to KOMPRESS then
passes through unchanged regardless of contamination). The mechanism bite
therefore lives in the consumer spy, which stubs the ML boundary and observes
the EXACT ``target_ratio`` / ``model_id`` each thread's runtime delivers to the
compressor. Spying the value the consumer USES (not the arg we passed in) is
what makes a reintroduced ``self._runtime_*`` read actually fail here.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from headroom import compress
from headroom.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    RouterRuntime,
)

N_THREADS = 16


def _make_messages(seed: int) -> list[dict[str, str]]:
    """A message large enough to exercise the routing/compression path."""
    body = f"item-{seed} " + ("the quick brown fox jumps over the lazy dog. " * 60)
    return [{"role": "user", "content": body}]


def _compressible_text(seed: int) -> str:
    """Long, non-code prose so KOMPRESS is a live strategy for this content."""
    return f"item-{seed} " + ("the quick brown fox jumps over the lazy dog. " * 40)


class TestContentRouterRuntimeOptionsIsolation:
    """A shared ContentRouter must not leak per-request options across threads.

    Driven through the PUBLIC ``compress(runtime=...)`` path. The spy sits at
    the CONSUMER read site so a shared mutable per-request field (the old TLS
    bug, or any future ``self.``-stashed option) is caught: under the barrier,
    every thread reads at the same instant, so last-writer-wins corrupts every
    thread but the one that wrote last.
    """

    def test_concurrent_runtimes_do_not_cross_contaminate(self) -> None:
        router = ContentRouter(ContentRouterConfig())  # ONE shared instance.
        barrier = threading.Barrier(N_THREADS)
        # Keyed by the content SEED (``item-{i}``), recording the
        # ``(target_ratio, model_id)`` the ML CONSUMER saw for that call.
        # Distinct seeds per thread → dict writes are race-free without a lock.
        observed: dict[int, tuple[float | None, str | None]] = {}

        # Patch the ML boundary ONCE (not per worker — that would be a write
        # race on the shared router). ``model_id`` is THIS call's binding (a
        # call-local in ``_try_kompress``), and ``target_ratio`` arrives at
        # the SAME ``.compress`` call — so the pair is per-call with no shared
        # bridge. The barrier lives in the EARLIEST consumer (``_get_kompress``)
        # so that, under a hypothetical shared-field regression, every thread's
        # write has completed before any thread performs the downstream read —
        # making last-writer-wins deterministic (no flaky GREEN).
        def fake_get_kompress(model_id: str | None = None) -> object:
            def _record(
                text: str,
                context: str = "",
                question: object = None,
                target_ratio: float | None = None,
            ) -> SimpleNamespace:
                seed = int(re.search(r"item-(\d+)", text).group(1))  # type: ignore[union-attr]
                observed[seed] = (target_ratio, model_id)
                return SimpleNamespace(compressed=text, compressed_tokens=len(text.split()))

            barrier.wait(timeout=30)
            return SimpleNamespace(compress=_record)

        router._get_kompress = fake_get_kompress  # type: ignore[method-assign]

        def worker(i: int) -> None:
            router.compress(
                _compressible_text(i),
                runtime=RouterRuntime(
                    force_kompress=True,
                    target_ratio=0.1 * (i + 1),
                    kompress_model=f"model-{i}",
                ),
            )

        with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
            list(pool.map(worker, range(N_THREADS)))

        # Every call must have reached the ML consumer (no silent skip).
        assert len(observed) == N_THREADS, (
            f"only {len(observed)}/{N_THREADS} calls reached the ML consumer"
        )

        # Each thread's ML consumer must have seen exactly its OWN runtime. Under
        # the barrier, a reintroduced shared per-request field (the old TLS bug,
        # or any future ``self.``-stashed option) yields last-writer-wins, so
        # all-but-one thread would observe a foreign value and these would fail.
        for i in range(N_THREADS):
            got_ratio, got_model = observed[i]
            assert got_ratio == 0.1 * (i + 1), (
                f"thread {i} consumed a foreign target_ratio: "
                f"{got_ratio!r} != {0.1 * (i + 1)!r} — per-request options leaked"
            )
            assert got_model == f"model-{i}", (
                f"thread {i} consumed a foreign kompress_model: "
                f"{got_model!r} != 'model-{i}' — per-request options leaked"
            )

    def test_default_runtime_when_omitted(self) -> None:
        """A direct ``compress()`` with no runtime consumes the documented
        defaults (``target_ratio=None`` / ``kompress_model=None``), regardless
        of what any other call passed."""
        router = ContentRouter(ContentRouterConfig())
        seen: dict[str, object] = {}

        def fake_get_kompress(model_id: str | None = None) -> object:
            seen["model_id"] = model_id

            def fake_compress(
                text: str,
                context: str = "",
                question: object = None,
                target_ratio: float | None = None,
            ) -> SimpleNamespace:
                seen["target_ratio"] = target_ratio
                return SimpleNamespace(compressed=text, compressed_tokens=len(text.split()))

            return SimpleNamespace(compress=fake_compress)

        router._get_kompress = fake_get_kompress  # type: ignore[method-assign]
        router.compress(_compressible_text(0), runtime=RouterRuntime(force_kompress=True))

        assert seen["target_ratio"] is None
        assert seen["model_id"] is None


class TestCompressConcurrentDifferentConfigs:
    """End-to-end: concurrent compress() with different configs on the shared pipeline."""

    def test_concurrent_compress_matches_serial(self) -> None:
        # Distinct per-call runtime options. compress() routes these through
        # the SHARED module-level pipeline (one ContentRouter/SmartCrusher),
        # which is exactly where the race lived.
        # force_kompress is a RouterRuntime knob, NOT a public compress() kwarg —
        # public compress() rejects unknown kwargs (compress.py), and through it
        # force_kompress was always a silent no-op. Its concurrent isolation is
        # covered separately above via router.compress(runtime=RouterRuntime(...)).
        # Here the distinct target_ratio per case is what exercises cross-thread
        # option isolation through the shared module-level pipeline.
        cases = [
            {"target_ratio": 0.2},
            {"target_ratio": 0.5},
            {"target_ratio": 0.8},
            {"target_ratio": 0.3},
        ]
        # Repeat each case so threads genuinely overlap on the shared pipeline.
        work = [(i % len(cases), _make_messages(i)) for i in range(N_THREADS)]

        # Serial baseline: each (config, messages) pair run alone.
        serial = {
            idx: compress(msgs, **cases[case_i]).messages for idx, (case_i, msgs) in enumerate(work)
        }

        # Concurrent run on the same shared pipeline.
        def run(item: tuple[int, tuple[int, list[dict[str, str]]]]):
            idx, (case_i, msgs) = item
            return idx, compress(msgs, **cases[case_i]).messages

        with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
            concurrent = dict(pool.map(run, list(enumerate(work))))

        # Each concurrent call must produce exactly what it produces serially.
        # If runtime options bled across threads, a call would be compressed
        # under a foreign target_ratio and diverge.
        for idx in range(len(work)):
            assert concurrent[idx] == serial[idx], (
                f"call {idx} diverged under concurrency — runtime options leaked"
            )
