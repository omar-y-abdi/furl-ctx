"""Furl PostToolUse hook: FURL_HOOK_EXCLUDE_TOOLS exclusion + FURL_HOOK_MODE."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_HOOK_PATH = (
    Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks" / "compress_tool_output.py"
)


def _load_hook():
    # Restore os.environ afterwards: the hook's module-level FURL_CCR_BACKEND
    # setdefault must not leak into the rest of the suite.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("furl_hook_under_test", _HOOK_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


_hook = _load_hook()


def test_furl_own_tools_always_excluded(monkeypatch) -> None:
    monkeypatch.delenv("FURL_HOOK_EXCLUDE_TOOLS", raising=False)
    assert _hook._excluded("mcp__furl__furl_compress") is True
    assert _hook._excluded("mcp__x__furl_retrieve") is True


def test_ordinary_tools_not_excluded(monkeypatch) -> None:
    monkeypatch.delenv("FURL_HOOK_EXCLUDE_TOOLS", raising=False)
    assert _hook._excluded("Bash") is False
    assert _hook._excluded("WebFetch") is False
    assert _hook._excluded("") is False


def test_operator_exclusions_exact_and_glob(monkeypatch) -> None:
    monkeypatch.setenv("FURL_HOOK_EXCLUDE_TOOLS", "Bash, mcp__db__*")
    assert _hook._excluded("Bash") is True  # exact
    assert _hook._excluded("mcp__db__query") is True  # fnmatch glob
    assert _hook._excluded("WebFetch") is False


def test_mode_kwargs_normal_and_aggressive(monkeypatch) -> None:
    monkeypatch.delenv("FURL_HOOK_MODE", raising=False)
    assert _hook._mode_kwargs() == {}
    monkeypatch.setenv("FURL_HOOK_MODE", "aggressive")
    assert _hook._mode_kwargs() == {"protect_recent": 0, "min_tokens_to_compress": 50}
