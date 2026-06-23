"""Regression test for #4-upstream: the router re-swallowed propagated Kompress bugs.

`KompressCompressor.compress()` was hardened (#4) to propagate real bugs
(TypeError, AttributeError, ...) and only passthrough on
`_MODEL_UNAVAILABLE_ERRORS`. But `ContentRouter._try_ml_compressor` wrapped the
`compress()` call in a blanket `except Exception`, re-swallowing exactly those
propagated bugs into a silent passthrough — making the upstream #4 fix a no-op
on the router path.

Fix: narrow the router's catch to the SAME `_MODEL_UNAVAILABLE_ERRORS` tuple
(single-sourced from kompress_compressor so it cannot drift), so a Kompress bug
propagates out of `_try_ml_compressor` while a genuine model-unavailable error
still degrades to a graceful passthrough.

Scope note: this surfaces the bug at the `_try_ml_compressor` boundary. The
strategy-dispatch net in `_apply_strategy_to_content` (strategy-agnostic) still
re-catches it for the full KOMPRESS/TEXT path; fully propagating to the caller
would require narrowing that net too, which changes error handling for all
strategies — out of scope for this follow-up.

Mutation-sensitive: reverting the router catch to `except Exception` makes the
bug-type case pass through silently and `test_kompress_bug_propagates` fails.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from headroom.transforms.content_router import ContentRouter
from headroom.transforms.kompress_compressor import KompressModelNotCached

_CONTENT = " ".join(f"word{i:02d}" for i in range(40))


def _router_with_kompress(raising_exc: BaseException | None) -> ContentRouter:
    """Router whose Kompress compressor raises `raising_exc` from compress()."""

    class _StubKompress:
        def compress(self, content, context="", question=None, target_ratio=None):
            if raising_exc is not None:
                raise raising_exc
            return SimpleNamespace(compressed=content, compressed_tokens=len(content.split()))

    router = ContentRouter()
    router._get_kompress = lambda: _StubKompress()  # type: ignore[method-assign]
    return router


def test_kompress_bug_propagates() -> None:
    # A real bug (TypeError) must NOT be swallowed into a silent passthrough.
    router = _router_with_kompress(TypeError("simulated model bug: bad tensor op"))
    with pytest.raises(TypeError, match="simulated model bug"):
        router._try_ml_compressor(_CONTENT, context="")


def test_model_unavailable_degrades_to_passthrough() -> None:
    # The common "model not downloaded" case still degrades gracefully:
    # _try_ml_compressor returns the original content unchanged.
    router = _router_with_kompress(KompressModelNotCached("some/model"))
    compressed, tokens = router._try_ml_compressor(_CONTENT, context="")
    assert compressed == _CONTENT
    assert tokens == len(_CONTENT.split())
