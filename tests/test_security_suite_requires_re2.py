"""The ReDoS security suite REQUIRES google-re2 — its absence must be a HARD
FAILURE here, never a silent skip elsewhere.

Why this file exists
--------------------
The catastrophic-backtracking bound in ``tests/test_regex_budget.py`` and the
10 MiB marker-scan bound in ``tests/test_marker_scan_budget.py`` only actually
EXECUTE when RE2 (the ``google-re2`` package, the linear-time engine) is
importable. Every one of those assertions is individually gated:

* ``test_regex_budget.py`` — ``@pytest.mark.skipif(not re2_available())`` plus one
  inline ``pytest.skip``: 25 test instances.
* ``test_marker_scan_budget.py`` — ``if mg._RE2 is None: pytest.skip(...)``: 3 tests.

Measured on this tree: with ``google-re2`` installed those two files run
67 passed / 0 skipped; with it uninstalled they run 39 passed / **28 skipped**.
The 28 that vanish are the security assertions — the ones proving an
agent-supplied regex or a marker-shaped ~10 MiB blob cannot wedge the MCP worker
thread. They do NOT fail when RE2 is gone. They stop existing, and the suite
still reports green.

That makes the coverage only as strong as an *incidental* install line. RE2 is
declared solely in the optional ``re2`` extra (pulled in by ``mcp``), so a test
environment reduced to ``pip install furl-ctx[dev]`` — a plausible "dependency
cleanup", and a documented prior state of the CI test job (see the ``[dev]``-only
note in ``.github/workflows/ci.yml``) — carries no RE2, skips all 28, and stays
green. In that same environment the PRODUCTION engines degrade to the UNBUDGETED
residual ``re`` path (``regex_budget.search_within_budget`` off the main thread,
and ``marker_grammar.finditer_within_budget`` with no RE2 twin), which is the
exact hang these tests exist to prevent — so losing the coverage is precisely
when it matters most.

The fix has two halves. Half one: the ``dev`` extra now pulls in
``furl-ctx[re2]``, so every test install carries the engine by construction
rather than by remembering to also install ``[mcp]``. Half two is THIS FILE: a
guard that converts "RE2 absent from the test environment" from a silent skip
into a red test that names what was disarmed.

Why the guard cannot itself be skipped
--------------------------------------
It carries NO ``skipif``, NO ``importorskip``, and NO module-level ``import re2``
(which would turn absence into a collection *error* rather than a named
assertion). It imports only ``regex_budget`` and ``marker_grammar``, both of
which import cleanly with or without RE2 (their ``_load_re2`` swallows the absent
import). It then asserts on the EXACT predicates the two suites skip on —
``regex_budget.re2_available()`` and ``marker_grammar._RE2 is not None`` — so it
fails on precisely the condition that would disarm them, and it lives in its own
module so no unrelated collection failure can take it down with it.
"""

from __future__ import annotations

from pathlib import Path

import tomllib

from furl_ctx.ccr import marker_grammar, regex_budget

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

_DISARMED = (
    "google-re2 is NOT importable, so the ReDoS security suite is DISARMED: the 28 "
    "catastrophic-backtracking / 10 MiB-scan assertions in tests/test_regex_budget.py (25) "
    "and tests/test_marker_scan_budget.py (3) SILENTLY SKIP instead of running, while "
    "production's regex_budget and marker_grammar fall back to the UNBUDGETED residual `re` "
    "engine. Install the engine the suite requires: `pip install 'furl-ctx[re2]'` (or "
    "`[dev]`, which now pulls it in). This test is the tripwire that keeps that removal from "
    "passing as green."
)


def test_re2_is_installed_so_the_redos_security_suite_is_not_silently_disarmed() -> None:
    """HARD FAIL (never skip) when RE2 is absent from the test environment.

    Keys off the same two predicates the security suites gate on, so it fails on
    exactly the condition that would turn their assertions into silent skips.
    """
    assert regex_budget.re2_available(), _DISARMED
    assert marker_grammar._RE2 is not None, _DISARMED


def test_dev_extra_declares_re2_so_the_engine_is_present_by_construction() -> None:
    """Pin the declaration that makes the runtime guard above pass in every test
    install: the ``dev`` extra must pull in ``furl-ctx[re2]``. Without it a plain
    ``pip install furl-ctx[dev]`` carries no RE2 and the guard would fail on a
    legitimate test setup. A static TOML check (it needs no RE2 to run), it
    catches the removal at its source — mirroring the T11 pin on the ``mcp``
    extra in ``tests/test_regex_budget.py``.
    """
    optional_deps = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert "furl-ctx[re2]" in optional_deps["dev"], (
        "the dev extra must declare furl-ctx[re2] so every test install carries the "
        f"linear-time engine the security suite requires; got {optional_deps['dev']!r}"
    )
