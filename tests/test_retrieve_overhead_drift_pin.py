"""Drift pin for the per-retrieve token overhead constant.

``furl_ctx/compress.py`` hardcodes ``_CCR_RETRIEVE_OVERHEAD_TOKENS`` to price a
CCR round trip when it reports opaque-offload economics. ``verify/measure.py``
independently prices the same round trip for its effective-savings model. Both
are 106 today and must stay equal so the library and the bench harness price a
round trip the same way.

WHAT THIS PIN COMPARES, AND WHY IT MOVED. The harness used to hold the whole
per-retrieve overhead in one constant, ``RETRIEVE_CALL_OVERHEAD_TOKENS``, so the
pin compared against that. The round trip is now decomposed into three MEASURED
terms — outgoing call 31, response scaffolding 68, tool-result envelope 7 — and
``RETRIEVE_CALL_OVERHEAD_TOKENS`` is only the FIRST of them. The library's
constant means "everything a round trip costs beyond the payload", so its
counterpart is the sum, ``RETRIEVE_ROUND_TRIP_TOKENS``. Comparing it against one
of three components would be comparing different quantities, and would pass by
coincidence rather than by agreement.

KNOWN REMAINING DIVERGENCE, deliberately not pinned here because it is a library
behaviour question and not a drift: the two agree on the round trip but NOT on
the payload. The harness charges the JSON-escaped payload, which is what a model
receives from ``furl_retrieve``; the library charges ``offloaded_tokens``, the
raw count carried in the offload metadata (measured 1.94%-6.95% low, and 17.6%
low on the most escape-dense blob in this repo). Closing it would move
``net_negative_on_retrieval`` for callers, so it is reported rather than
smuggled in behind a constant change.

The library must not import the bench harness to keep them in sync, so the two
constants can silently drift apart. This test is the guard: it does the
cross-import that the library deliberately avoids and asserts the two values
match, and it AST-scans ``furl_ctx/compress.py``'s own source for a direct,
absolute ``import verify`` or ``from verify import ...`` statement, at module
level or nested inside a function body. It does not follow transitive imports
through other modules ``compress.py`` imports, does not see relative
``from . import verify`` forms, and does not catch a dynamic
``importlib.import_module('verify...')`` call, so the equality pin above, not
this guard alone, is what actually holds the line.
"""

from __future__ import annotations

import ast
import inspect

from furl_ctx.compress import _CCR_RETRIEVE_OVERHEAD_TOKENS
from verify.measure import RETRIEVE_ROUND_TRIP_TOKENS


def test_library_and_bench_retrieve_overhead_stay_equal() -> None:
    assert _CCR_RETRIEVE_OVERHEAD_TOKENS == RETRIEVE_ROUND_TRIP_TOKENS


def test_library_does_not_import_the_bench_harness() -> None:
    # The equality is held by this pin, not by furl_ctx.compress importing the
    # verify bench harness. Parse the library module and assert no import, top
    # level or lazy, pulls in a ``verify`` module.
    import furl_ctx.compress as compress_module

    tree = ast.parse(inspect.getsource(compress_module))
    top_level_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level_modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top_level_modules.add(node.module.split(".")[0])
    assert "verify" not in top_level_modules
