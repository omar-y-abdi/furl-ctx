"""Regression test for #10: per-request runtime options dropped in worker threads.

apply() sets per-request options (force_kompress, target_ratio, kompress_model,
compression_policy) into self._tls (thread-local) on the MAIN thread. For a
multi-message apply() it compresses messages in a ThreadPoolExecutor; worker
threads have their OWN empty thread-local, so they read the DEFAULTS and silently
dropped the options — e.g. force_kompress=True was ignored for every worker
compression.

Fix: the main thread snapshots the options and passes them into _timed_compress,
which replays them into the worker's thread-local before compressing.

These tests FORCE the parallel worker branch (>=2 cache-miss messages, >=2
workers) and assert the work actually ran off-main-thread, then assert the
option propagated. Compression-neutral for default options (multiturn bench
unchanged).
"""
from __future__ import annotations

import threading

import pytest

from headroom.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    ContentRouterConfig,
)
from headroom.tokenizers import get_tokenizer


@pytest.fixture
def _force_workers(monkeypatch):
    monkeypatch.setenv("HEADROOM_COMPRESS_WORKERS", "4")
    yield


def _two_noncode_messages() -> list[dict]:
    # Two DISTINCT, compressible, non-code messages (so detection would NOT pick
    # KOMPRESS on its own — forcing exposes the dropped option). Long enough to
    # be compression candidates, distinct so neither is a cache hit of the other.
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
    real_compress = ContentRouter.compress

    def spy_compress(self, content, **kwargs):
        # Record, IN THE EXECUTING THREAD, what force_kompress the router sees.
        seen_force.append(bool(getattr(self, "_runtime_force_kompress", False)))
        seen_idents.append(threading.get_ident())
        return real_compress(self, content, **kwargs)

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
    # #10: every worker compression must observe force_kompress=True.
    assert seen_force, "force_kompress was never observed"
    assert all(seen_force), (
        f"force_kompress dropped in a worker thread: observed {seen_force}"
    )


def test_default_options_unchanged_in_workers(monkeypatch, _force_workers) -> None:
    # No options set => workers must observe the defaults (False/None), i.e. the
    # snapshot of defaults is a no-op. Guards the no-degradation path.
    router = ContentRouter(ContentRouterConfig())
    tokenizer = get_tokenizer("gpt-4o")

    seen_force: list[bool] = []
    real_compress = ContentRouter.compress

    def spy_compress(self, content, **kwargs):
        seen_force.append(bool(getattr(self, "_runtime_force_kompress", False)))
        return real_compress(self, content, **kwargs)

    monkeypatch.setattr(ContentRouter, "compress", spy_compress)
    router.apply(_two_noncode_messages(), tokenizer)

    assert seen_force, "compress() was never called"
    assert not any(seen_force), "default force_kompress must remain False in workers"


def test_runtime_options_replay_covers_every_runtime_property() -> None:
    """Structural guard for the main->worker TLS replay (issue #10).

    The two behavioural tests above prove the FOUR options that exist today
    propagate. This one covers the forward-looking trap the router's own
    docstring names: a NEW ``_runtime_*`` property added without wiring it into
    BOTH the main-thread snapshot AND the worker replay makes workers silently
    read its default, and no other test would fail. It parses the live
    ``ContentRouter`` source and asserts the three sets — declared properties,
    snapshot keys, replay keys — are identical, so that drift fails HERE the
    moment it is introduced, not in production.
    """
    import inspect
    import re

    src = inspect.getsource(ContentRouter)
    # Property getters: ``def _runtime_<name>(self) -> ...`` (setters take a
    # second ``value`` arg, so ``(self)`` matches getters only).
    properties = set(re.findall(r"def _runtime_(\w+)\(self\)\s*->", src))
    # Worker replay (``_timed_compress``): ``... = runtime_options["<name>"]``.
    replayed = set(re.findall(r'runtime_options\["(\w+)"\]', src))
    # Main-thread snapshot (parallel path): ``"<name>": self._runtime_<name>``.
    snapshot = set(re.findall(r'"(\w+)":\s*self\._runtime_\w+', src))

    assert properties, "no _runtime_* getters found — the regex drifted from the source"
    assert properties == replayed, (
        f"_runtime_* properties {sorted(properties)} != worker-replay keys "
        f"{sorted(replayed)} in _timed_compress: an option is not replayed into "
        f"worker threads, so workers read its default silently."
    )
    assert properties == snapshot, (
        f"_runtime_* properties {sorted(properties)} != snapshot keys "
        f"{sorted(snapshot)} in the parallel runtime_options dict: an option is "
        f"not snapshotted on the main thread before fan-out."
    )
