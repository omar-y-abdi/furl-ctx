"""The optional-extra feature suites REQUIRE their extras — absence must be a
HARD FAILURE here, never a silent skip spread across dozens of files.

Why this file exists
--------------------
Two large test suites only EXECUTE when an optional dependency is importable,
and each disarms *silently* when it is not — leaving a green summary over code
that is no longer tested:

* The **MCP feature suites** — 30 files, every one gated on
  ``pytest.importorskip("mcp")`` at module or function scope. Because that guard
  fires during *collection*, the tests inside a gated module are never counted.
  Measured on this tree, dropping the ``mcp`` SDK moves the run from
  ``2836 passed`` to ``2482 passed, 34 skipped`` — **354 tests stop running**,
  and only 34 of them even surface as skips; the other ~320 vanish from the
  totals entirely. Nothing turns red. ``furl_ctx.ccr.mcp_server`` — the whole
  MCP server contract — goes untested behind a green summary.

* The **AST code-aware suite** — ``tests/test_code_aware_compressor.py``, 16
  tests gated on ``@pytest.mark.skipif(not _HAS_TREE_SITTER)`` where
  ``_HAS_TREE_SITTER = importlib.util.find_spec("tree_sitter_language_pack")``.
  Dropping the ``code`` extra moves the run to ``2820 passed, 16 skipped``. The
  CI test job installs ``[dev,mcp]`` (``.github/workflows/ci.yml``) with no
  ``code`` extra, so those 16 were skipping in CI — a *live* gap, not a latent
  one — leaving the opt-in compressor's syntax round-trip, CCR round-trip and
  store-failure veto contracts unexercised behind the same green summary.

Both suites are only as strong as an *incidental* install line. The fix has two
halves, mirroring ``tests/test_security_suite_requires_re2.py`` exactly. Half
one: the ``dev`` extra now pulls in ``furl-ctx[mcp]`` and ``furl-ctx[code]`` (see
``pyproject.toml``), so every test install — CI's ``[dev,mcp]`` included, which
carries ``code`` transitively through ``dev`` — carries both by construction.
Half two is THIS FILE: guards that convert "the extra is absent from the test
environment" from a silent skip into a red test that names what was disarmed and
how much coverage it costs.

Why these guards cannot themselves be skipped
---------------------------------------------
They carry NO ``skipif``, NO ``importorskip`` and NO module-level ``import mcp``
/ ``import tree_sitter_language_pack`` (which would turn absence into a
collection *error* rather than a named assertion). This module imports only the
standard library, so it always collects. Each runtime guard then asserts on the
EXACT predicate its own suite disarms on:

* the MCP guard *imports* ``mcp`` inside the test body and converts the
  ``ImportError`` that ``pytest.importorskip("mcp")`` would swallow into a skip
  into a failure — so even a broken-but-present install (which ``find_spec``
  alone would wave through while the suites still skipped) is caught;
* the code guard asserts ``find_spec("tree_sitter_language_pack") is not None``
  — the literal expression ``test_code_aware_compressor._HAS_TREE_SITTER`` gates
  on — so it fails on precisely the condition that skips those 16 tests, using
  ``find_spec`` (no import) so it never executes the grammar pack.

It lives in its own module so no unrelated collection failure can take it down.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import tomllib

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

_MCP_DISARMED = (
    "the `mcp` SDK is NOT importable, so the MCP feature suites are DISARMED: the 30 test "
    "files gated on pytest.importorskip('mcp') — 354 tests (measured 2836->2482 passing), "
    "covering the entire furl_ctx.ccr.mcp_server contract — STOP RUNNING, surfacing as only "
    "34 skips plus ~320 tests that vanish from the totals uncounted while the summary stays "
    "green. Install the SDK the suites require: `pip install 'furl-ctx[mcp]'` (or `[dev]`, "
    "which now pulls it in). This test is the tripwire that keeps that removal passing green."
)

_CODE_DISARMED = (
    "tree-sitter-language-pack is NOT importable, so the AST code-aware suite is DISARMED: "
    "the 16 tests in tests/test_code_aware_compressor.py gated on _HAS_TREE_SITTER "
    "(find_spec('tree_sitter_language_pack')) SILENTLY SKIP instead of running (measured "
    "2836->2820 passing, 16 skipped), leaving the opt-in compressor's syntax round-trip, CCR "
    "round-trip and store-failure veto contracts unexercised. Install the extra the suite "
    "requires: `pip install 'furl-ctx[code]'` (or `[dev]`, which now pulls it in). This test "
    "is the tripwire that keeps that removal passing green."
)


def test_mcp_is_installed_so_the_mcp_feature_suites_are_not_silently_disarmed() -> None:
    """HARD FAIL (never skip) when the ``mcp`` SDK is absent.

    Mirrors ``pytest.importorskip("mcp")`` — the exact guard the 30 MCP suites
    use — by importing ``mcp`` and converting the ``ImportError`` it would
    swallow into a skip into a named failure instead. The import lives in the
    test body, never at module scope, so absence is a red assertion and not a
    collection error.
    """
    try:
        importlib.import_module("mcp")
    except ImportError as exc:
        raise AssertionError(_MCP_DISARMED) from exc


def test_code_extra_is_installed_so_the_code_aware_suite_is_not_silently_disarmed() -> None:
    """HARD FAIL (never skip) when tree-sitter-language-pack is absent.

    Keys off ``find_spec("tree_sitter_language_pack")`` — the literal predicate
    ``test_code_aware_compressor._HAS_TREE_SITTER`` gates on — so it fails on
    exactly the condition that turns those 16 tests into silent skips. Uses
    ``find_spec`` (no import) so it never executes the grammar pack.
    """
    assert importlib.util.find_spec("tree_sitter_language_pack") is not None, _CODE_DISARMED


def test_dev_extra_declares_mcp_so_the_mcp_suites_are_present_by_construction() -> None:
    """Pin the declaration that makes the mcp runtime guard pass in every test
    install: the ``dev`` extra must pull in ``furl-ctx[mcp]``. A static TOML
    check (it needs no mcp to run), it catches the removal at its source —
    the same shape as the re2 pin in tests/test_security_suite_requires_re2.py.
    """
    optional_deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert "furl-ctx[mcp]" in optional_deps["dev"], (
        "the dev extra must declare furl-ctx[mcp] so every test install carries the mcp SDK "
        f"the feature suites require; got {optional_deps['dev']!r}"
    )


def test_dev_extra_declares_code_so_the_code_aware_suite_is_present_by_construction() -> None:
    """Pin the declaration that makes the code runtime guard pass in every test
    install — including CI's ``[dev,mcp]``, which carries ``code`` only
    transitively through ``dev``: the ``dev`` extra must pull in
    ``furl-ctx[code]``. A static TOML check, it needs no tree-sitter to run.
    """
    optional_deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert "furl-ctx[code]" in optional_deps["dev"], (
        "the dev extra must declare furl-ctx[code] so every test install (and CI's [dev,mcp] "
        f"transitively) carries tree-sitter for the AST suite; got {optional_deps['dev']!r}"
    )
