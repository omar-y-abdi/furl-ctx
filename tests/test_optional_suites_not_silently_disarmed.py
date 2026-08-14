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
  Measured on an idle machine at base ``b1eae3ca``, dropping the ``mcp`` SDK
  moves the run from ``2870 passed`` to ``2515 passed, 34 skipped, 1 failed`` —
  **354 tests stop running**, and only 34 of them even surface as skips; the
  other **320 vanish** from the totals entirely (2870 total − 2550 still
  collected = 320; 320 + 34 = 354).

  The absolute pair moves whenever tests are added or removed; the **354/34/320
  split is the invariant** worth trusting. It has now been measured identical
  across two independent base moves in opposite directions — ``721a3826`` ->
  ``fa64bada`` REMOVED 2 tests (pair 2842->2487, then 2840->2485), and
  ``fa64bada`` -> ``b1eae3ca`` ADDED 30 (pair 2840->2485, then 2870->2515).
  The split held at 354/34/320 through both. So if the pair looks stale,
  re-measure before assuming rot: only the split moving means the *shape* of
  the disarmament changed, which would mean a new module became
  ``importorskip``-gated at import scope. The one red is THIS FILE's mcp tripwire
  firing by design — remove it and nothing turns red at all, which is the
  point: ``furl_ctx.ccr.mcp_server``, the whole MCP server contract, would go
  untested behind a green summary.

* The **AST code-aware suite** — ``tests/test_code_aware_compressor.py``, 15
  tests gated on ``@pytest.mark.skipif(not _HAS_TREE_SITTER)`` where
  ``_HAS_TREE_SITTER = importlib.util.find_spec("tree_sitter_language_pack")``.
  Measured on an idle machine at base ``b1eae3ca``: WITH tree-sitter ``2870
  passed, 0 skipped, 0 failed``; WITHOUT it ``2854 passed, 15 skipped, 1
  failed``, that 1 being this file's code tripwire firing by design. Reconciles
  against a constant collection of 2870: 2854 + 15 + 1. The 15 is the invariant
  here, and it too held across both base moves. Unlike the mcp case
  above nothing
  vanishes, because these skips are decorator-level, not collection-level.
  Isolated to the two relevant files with tree-sitter absent: 21 passed, 15
  skipped, 1 failed, that 1 again being the tripwire.
  Before the ``dev``→``code`` fix below, the CI test job installed ``[dev,mcp]``
  (``.github/workflows/ci.yml``) with no ``code`` extra, so those tests were
  skipping in CI — a *live* gap, not a latent one — leaving the opt-in
  compressor's syntax round-trip, CCR round-trip and store-failure veto
  contracts unexercised behind the same green summary.

Both suites are only as strong as an *incidental* install line. The fix has two
halves, mirroring ``tests/test_security_suite_requires_re2.py`` exactly. Half
one: the ``dev`` extra now pulls in ``furl-ctx[mcp]`` and ``furl-ctx[code]`` (see
``pyproject.toml``), so every test install — CI's ``[dev,mcp]`` included, which
carries ``code`` transitively through ``dev`` — carries both by construction.
Half two is THIS FILE: guards that convert "the extra is absent from the test
environment" from a silent skip into a red test that names what was disarmed and
how much coverage it costs.

This file additionally pins two packaging invariants against the same class of
silent coverage loss: that ``furl-ctx[all]`` reaches every feature extra (so
``pip install furl-ctx[all]`` can never omit ``code`` again), and that the CI
test-job install line names ``code`` directly (defence in depth — ``dev`` already
carries it transitively, verified, so the code suite already runs in the gate).

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
  on — so it fails on precisely the condition that skips those 15 tests, using
  ``find_spec`` (no import) so it never executes the grammar pack.

It lives in its own module so no unrelated collection failure can take it down.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

import tomllib

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_CI_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# Extras that `all` need NOT ship — dev tooling and the aggregate itself.
_NON_FEATURE_EXTRAS = frozenset({"dev", "all"})


def _extra_closure(optional_deps: dict[str, list[str]], roots: set[str]) -> set[str]:
    """Extras transitively reachable from *roots* via ``furl-ctx[X]`` self-refs."""
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for dep in optional_deps.get(name, []):
            match = re.fullmatch(r"furl[-_]ctx\[([\w-]+)\]", dep.strip())
            if match:
                stack.append(match.group(1))
    return seen


_MCP_DISARMED = (
    "the `mcp` SDK is NOT importable, so the MCP feature suites are DISARMED: the 30 test "
    "files gated on pytest.importorskip('mcp') — 354 tests (measured 2870->2515 passing), "
    "covering the entire furl_ctx.ccr.mcp_server contract — STOP RUNNING, surfacing as only "
    "34 skips plus 320 tests that vanish from the totals uncounted while the summary stays "
    "green. Install the SDK the suites require: `pip install 'furl-ctx[mcp]'` (or `[dev]`, "
    "which now pulls it in). This test is the tripwire that keeps that removal passing green."
)

_CODE_DISARMED = (
    "tree-sitter-language-pack is NOT importable, so the AST code-aware suite is DISARMED: "
    "the 15 tests in tests/test_code_aware_compressor.py gated on _HAS_TREE_SITTER "
    "(find_spec('tree_sitter_language_pack')) SILENTLY SKIP instead of running (measured: "
    "2870 passing WITH tree-sitter -> 2854 passing / 15 skipped WITHOUT it), leaving the "
    "opt-in compressor's syntax round-trip, CCR "
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
    exactly the condition that turns those 15 tests into silent skips. Uses
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


def test_mcp_extra_stays_on_v1_until_server_is_migrated() -> None:
    """Keep fresh installs on the low-level SDK API the server implements."""
    optional_deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    requirement = next(dep for dep in optional_deps["mcp"] if dep.startswith("mcp"))
    assert "<2" in requirement, (
        "furl_ctx.ccr.mcp_server uses the MCP v1 low-level Server decorators, so its mcp "
        f"requirement must exclude the incompatible v2 SDK; got {requirement!r}"
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


def test_ci_test_job_installs_code_extra_so_the_code_suite_runs_in_the_gate() -> None:
    """The CI test-job wheel install must name the ``code`` extra explicitly.

    ``dev`` already pulls ``furl-ctx[code]`` (pinned above) and CI's ``[dev,mcp]``
    carries tree-sitter transitively — verified: ``pip install "wheel[dev,mcp]"``
    alone resolves tree-sitter-language-pack, so the AST suite already runs in
    the gate — but naming ``code`` on the install line too mirrors how
    ``google-re2`` is listed explicitly despite ``mcp`` depending on
    ``furl-ctx[re2]``: the requirement then reads directly off the CI line and
    survives a change to the ``dev`` extra. A stdlib-only text scan of ci.yml
    (this module must always collect), it fails red the moment a future edit
    strips ``code`` from the wheel install line.
    """
    # Scan actual install COMMANDS, not comments: this step's comment references
    # `[dev,mcp]` as prose, and a pin must judge the real command, not the prose.
    installs: list[str] = []
    for raw in _CI_YML.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        match = re.search(r'pip install "\$\{WHEEL\}\[([^\]]*)\]"', raw)
        if match:
            installs.append(match.group(1))
    assert installs, (
        'no `pip install "${WHEEL}[...]"` command found in .github/workflows/ci.yml — the CI '
        "test-job install step changed shape; re-point this pin at the new install command"
    )
    for extras_str in installs:
        extras = {e.strip() for e in extras_str.split(",")}
        assert "code" in extras, (
            "the CI test-job wheel install must name the `code` extra so the AST code-aware "
            "suite runs in the gate off a requirement that reads directly off the CI line, not "
            f"only transitively through `dev`; got extras {sorted(extras)}"
        )


def test_all_extra_reaches_every_feature_extra() -> None:
    """``furl-ctx[all]`` must mean ALL — reach every runtime feature extra.

    Derives the feature extras from pyproject (every optional-dependency group
    except dev tooling and ``all`` itself) and asserts ``all`` reaches each
    through its ``furl-ctx[X]`` graph, so a new feature extra that isn't wired
    into ``all`` fails red here instead of silently shipping an incomplete
    ``[all]``. Guards the regression where ``all`` listed only ``[mcp]`` and
    omitted ``[code]``, so ``pip install furl-ctx[all]`` did not install the AST
    code-aware compressor. Derived, not hand-enumerated, so it can't rot.
    """
    optional_deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    feature_extras = {name for name in optional_deps if name not in _NON_FEATURE_EXTRAS}
    reachable = _extra_closure(optional_deps, {"all"}) - {"all"}
    missing = feature_extras - reachable
    assert not missing, (
        "`furl-ctx[all]` must reach every feature extra so `[all]` installs the whole feature "
        f"set; it omits {sorted(missing)}. There are three fixes and only one is usually right:\n"
        f"  (a) {sorted(missing)[0]!r} is a RUNTIME feature extra -> add "
        f"furl-ctx[{sorted(missing)[0]}] (or an extra that pulls it) to the `all` group in "
        "pyproject.toml.\n"
        f"  (b) {sorted(missing)[0]!r} is DEV-ONLY tooling `all` should NOT ship -> add it to "
        "_NON_FEATURE_EXTRAS in this file. That is the escape hatch; do NOT widen `all` to "
        "carry dev tooling to end users.\n"
        "  (c) `all` DOES reach it, via a self-reference _extra_closure cannot parse (an "
        "environment marker, or a dotted extra name) -> widen the walk. It fails safe as this "
        "false red rather than a silent pass, which is why the regex is deliberately strict.\n"
        f"Reachable now: {sorted(reachable)}; feature extras: {sorted(feature_extras)}"
    )
