"""Pin for the ``_furl_workspace_dir_sandbox`` autouse fixture (F-E1, audit R2#11).

A plain ``pytest`` run used to be able to durably write hook-counter rows into
the real ``~/.furl`` (see ``tests/conftest.py`` for the root cause). This test
is the regression pin: it asserts that, DURING the suite, the active workspace
resolves under pytest's own temp tree rather than the developer's real home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from furl_ctx import paths


def test_workspace_dir_resolves_under_pytest_temp_not_real_home(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``workspace_dir()`` must resolve under the session sandbox, not ``~/.furl``."""
    real_home_furl = Path.home() / ".furl"
    resolved = paths.workspace_dir()

    assert resolved != real_home_furl, (
        "the suite's active workspace must never be the developer's real "
        f"~/.furl (F-E1); got {resolved}"
    )

    # `tmp_path_factory` is itself session-scoped, so its base temp dir is the
    # same directory the `_furl_workspace_dir_sandbox` fixture derived
    # `FURL_WORKSPACE_DIR` from — asserting containment (rather than equality)
    # proves this is that sandbox and not merely some other unrelated tmp dir.
    base_temp = tmp_path_factory.getbasetemp()
    assert base_temp in resolved.parents, (
        "the active workspace must resolve under pytest's own temp base "
        f"({base_temp}), proving the conftest sandbox fixture is active; got {resolved}"
    )
