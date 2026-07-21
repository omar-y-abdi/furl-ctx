"""Drift pin for the per-retrieve token overhead constant.

``furl_ctx/compress.py`` hardcodes ``_CCR_RETRIEVE_OVERHEAD_TOKENS`` to price a
CCR round trip when it reports opaque-offload economics. ``verify/measure.py``
independently defines ``RETRIEVE_CALL_OVERHEAD_TOKENS`` for its
effective-savings-under-retrieval model. Both are 12 today and must stay equal
so the library and the bench harness price a round trip the same way.

The library must not import the bench harness to keep them in sync, so the two
constants can silently drift apart. This test is the guard: it does the
cross-import that the library deliberately avoids and asserts the two values
match, and it checks that ``furl_ctx.compress`` still imports no ``verify``
module so the pin, not a library-to-bench dependency, is what holds the line.
"""

from __future__ import annotations

import ast
import inspect

from furl_ctx.compress import _CCR_RETRIEVE_OVERHEAD_TOKENS
from verify.measure import RETRIEVE_CALL_OVERHEAD_TOKENS


def test_library_and_bench_retrieve_overhead_stay_equal() -> None:
    assert _CCR_RETRIEVE_OVERHEAD_TOKENS == RETRIEVE_CALL_OVERHEAD_TOKENS


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
