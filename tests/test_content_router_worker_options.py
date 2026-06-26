"""Per-request RouterRuntime must reach WORKER threads BY VALUE.

apply() builds ONE frozen ``RouterRuntime`` from its kwargs and threads it by
argument into every compress() call — including the ThreadPoolExecutor workers
that compress cache-miss messages. Because the value is immutable and passed by
argument (not stored in a thread-local the workers don't inherit), every worker
compresses under the SAME per-request options the main thread holds. There is
no main->worker replay to keep in sync.

These tests FORCE the parallel worker branch (>=2 cache-miss messages, >=2
workers), assert the work actually ran off-main-thread, then assert the option
the worker's compress() received BY VALUE matches what apply() was given.

Bite: the spy reads ``runtime.force_kompress`` off the ``runtime`` the worker's
compress() was actually called with. If a worker silently fell back to defaults
(e.g. a future regression that dropped the argument and read a default
``RouterRuntime``), ``seen_force`` would be all-False and
``test_force_kompress_reaches_worker_threads`` fails.
"""
from __future__ import annotations

import threading

import pytest

from headroom.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    RouterRuntime,
)
from headroom.tokenizers import get_tokenizer


@pytest.fixture
def _force_workers(monkeypatch):
    monkeypatch.setenv("HEADROOM_COMPRESS_WORKERS", "4")
    yield


def _two_noncode_messages() -> list[dict]:
    # Two DISTINCT, compressible, non-code messages (so detection would NOT pick
    # KOMPRESS on its own — forcing exposes a dropped option). Long enough to be
    # compression candidates, distinct so neither is a cache hit of the other.
    return [
        {"role": "tool", "content": "alpha " + "data point one " * 40},
        {"role": "tool", "content": "beta " + "different payload two " * 40},
    ]


def test_force_kompress_reaches_worker_threads(monkeypatch, _force_workers) -> None:
    router = ContentRouter(ContentRouterConfig())
    tokenizer = get_tokenizer("gpt-4o")
    main_ident = threading.get_ident()

    seen_force: list[bool] = []
    seen_idents: list[int] = []
    seen_runtime_types: list[type] = []
    real_compress = ContentRouter.compress

    def spy_compress(self, content, *, runtime=RouterRuntime(), **kwargs):
        # Record, IN THE EXECUTING THREAD, the force_kompress the worker's
        # compress() received BY VALUE off its ``runtime`` argument.
        seen_force.append(bool(runtime.force_kompress))
        seen_runtime_types.append(type(runtime))
        seen_idents.append(threading.get_ident())
        return real_compress(self, content, runtime=runtime, **kwargs)

    monkeypatch.setattr(ContentRouter, "compress", spy_compress)

    router.apply(
        _two_noncode_messages(),
        tokenizer,
        force_kompress=True,
    )

    # Proof the worker path actually ran (not the inline main-thread branch).
    assert seen_idents, "compress() was never called"
    assert any(i != main_ident for i in seen_idents), (
        "no compression ran off the main thread — the worker branch was not "
        "exercised, so this test would pass even with the bug present"
    )
    # The worker received a real RouterRuntime by value, not some default sentinel.
    assert all(t is RouterRuntime for t in seen_runtime_types), (
        f"worker received a non-RouterRuntime: {seen_runtime_types}"
    )
    # Every worker compression must observe force_kompress=True by value.
    assert seen_force, "force_kompress was never observed"
    assert all(seen_force), (
        f"force_kompress dropped before a worker thread: observed {seen_force}"
    )


def test_default_options_unchanged_in_workers(monkeypatch, _force_workers) -> None:
    # No options set => workers must observe the defaults (False), i.e. apply()
    # built a default RouterRuntime and threaded it unchanged. Guards no-degradation.
    router = ContentRouter(ContentRouterConfig())
    tokenizer = get_tokenizer("gpt-4o")

    seen_force: list[bool] = []
    real_compress = ContentRouter.compress

    def spy_compress(self, content, *, runtime=RouterRuntime(), **kwargs):
        seen_force.append(bool(runtime.force_kompress))
        return real_compress(self, content, runtime=runtime, **kwargs)

    monkeypatch.setattr(ContentRouter, "compress", spy_compress)
    router.apply(_two_noncode_messages(), tokenizer)

    assert seen_force, "compress() was never called"
    assert not any(seen_force), "default force_kompress must remain False in workers"


def test_every_runtime_field_reaches_workers_by_value(monkeypatch, _force_workers) -> None:
    """Replaces the old structural snapshot/replay guard (issue #10).

    The old guard parsed the source to assert the per-field main-thread snapshot
    dict and the per-field worker-replay dict stayed in sync with the declared
    ``_runtime_*`` properties — protecting a forward-looking trap where a NEW
    per-request field was added without wiring it into BOTH. That trap is gone
    BY CONSTRUCTION: there is no per-field snapshot or replay any more. apply()
    threads ONE frozen ``RouterRuntime`` value, and the worker receives that SAME
    instance by value, so a new field is delivered to workers the moment it is
    added to the dataclass — no second wiring site to drift from.

    This test pins that construction directly: every field apply() put into the
    runtime is observed, unchanged, on the runtime the worker's compress() runs.
    """
    router = ContentRouter(ContentRouterConfig())
    tokenizer = get_tokenizer("gpt-4o")
    main_ident = threading.get_ident()
    policy = object()  # sentinel; identity-compared

    seen: list[RouterRuntime] = []
    seen_idents: list[int] = []
    real_compress = ContentRouter.compress

    def spy_compress(self, content, *, runtime=RouterRuntime(), **kwargs):
        seen.append(runtime)
        seen_idents.append(threading.get_ident())
        return real_compress(self, content, runtime=runtime, **kwargs)

    monkeypatch.setattr(ContentRouter, "compress", spy_compress)

    router.apply(
        _two_noncode_messages(),
        tokenizer,
        force_kompress=True,
        target_ratio=0.42,
        kompress_model="some/model",
        compression_policy=policy,
    )

    assert any(i != main_ident for i in seen_idents), (
        "no compression ran off the main thread — worker branch not exercised"
    )
    assert seen, "compress() was never called"
    # Every declared field of the dataclass must arrive at the worker by value.
    # Driven off the live field set, so a NEW field added to RouterRuntime is
    # automatically covered here — drift fails loud the moment it is unthreaded.
    import dataclasses

    expected = {
        "force_kompress": True,
        "target_ratio": 0.42,
        "kompress_model": "some/model",
        "compression_policy": policy,
    }
    declared = {f.name for f in dataclasses.fields(RouterRuntime)}
    assert declared == set(expected), (
        f"RouterRuntime fields {sorted(declared)} changed — update this test's "
        f"expected map so every field is asserted to reach workers by value."
    )
    for rt in seen:
        for field_name, want in expected.items():
            got = getattr(rt, field_name)
            if field_name == "compression_policy":
                assert got is want, f"worker saw a foreign {field_name}: {got!r}"
            else:
                assert got == want, f"worker saw a foreign {field_name}: {got!r} != {want!r}"
