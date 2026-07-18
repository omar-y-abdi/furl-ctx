"""Regression: the PostToolUse hook must extract compressible text from the REAL
``tool_response`` shapes Claude Code emits for the matched tools (Bash, WebFetch,
WebSearch, Task/Agent), not only the legacy ``{"content": ...}`` wrapper.

Ground truth: the four fixtures below are byte-for-byte the ``tool_response`` objects
captured from a live Claude Code 2.1.x session (a stdin-dumping PostToolUse hook on
matcher ``Bash|WebFetch|WebSearch|Task``). Before the fix, every Bash/WebFetch call
took the ``.get("content") -> None -> passthrough`` path, so the plugin's headline
feature was a silent no-op for real tool output. These tests fail against the old
extractor (Bash + WebFetch cases) and pass against the new one, while the legacy
shapes keep working (no regression).

Pure stdlib: the hook imports ``furl_ctx`` lazily (fail-open), so ``_extract_text``
and the verbose no-op paths run without the ``[mcp]``/compiled extra installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks" / "compress_tool_output.py"
)


def _load_hook():
    # The hook does module-level os.environ.setdefault(...); save/restore so that
    # importing it here cannot leak FURL_CCR_* into the rest of the suite.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location("furl_hook_shapes_under_test", _HOOK_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


_hook = _load_hook()


# --- REAL captured tool_response fixtures (live Claude Code 2.1.207) -----------

# Bash: {"stdout","stderr","interrupted","isImage","noOutputExpected"}
_REAL_BASH = {
    "stdout": "alpha\nbeta\ngamma",
    "stderr": "",
    "interrupted": False,
    "isImage": False,
    "noOutputExpected": False,
}

# WebFetch: {"bytes","code","codeText","result","durationMs","url"}
_REAL_WEBFETCH = {
    "bytes": 559,
    "code": 200,
    "codeText": "OK",
    "result": 'The page title is "Example Domain".',
    "durationMs": 2434,
    "url": "https://example.com",
}

# Task (payload tool_name is "Agent"): the sub-agent's answer lives in content blocks.
_REAL_AGENT = {
    "status": "completed",
    "prompt": "Reply with exactly the single word: PINEAPPLE",
    "agentId": "a6f5e1350ce41372a",
    "agentType": "general-purpose",
    "content": [{"type": "text", "text": "PINEAPPLE"}],
    "resolvedModel": "claude-opus-4-8",
    "totalTokens": 9679,
}

# WebSearch: {"query","results","durationSeconds","searchCount"} — results is a list
# of link objects, not free-form prose; no text-bearing string field.
_REAL_WEBSEARCH = {
    "query": "claude code hooks reference",
    "results": [
        {
            "tool_use_id": "srvtoolu_01UZhMNDLFu85MzV1vUYjy1b",
            "content": [
                {
                    "title": "Hooks reference - Claude Code Docs",
                    "url": "https://code.claude.com/docs/en/hooks",
                },
                {
                    "title": "claude-code-hooks",
                    "url": "https://github.com/karanb192/claude-code-hooks",
                },
            ],
        }
    ],
    "durationSeconds": 1,
    "searchCount": 1,
}


# --- new real shapes: these are the RED cases against the old extractor --------


def test_real_bash_stdout_is_extracted() -> None:
    """RED on old code (dict without "content" -> None). Bash stdout must compress."""
    assert _hook._extract_text(_REAL_BASH) == "alpha\nbeta\ngamma"


def test_real_webfetch_result_is_extracted() -> None:
    """RED on old code. WebFetch's answer lives under "result"."""
    assert _hook._extract_text(_REAL_WEBFETCH) == 'The page title is "Example Domain".'


def test_bash_prefers_stdout_and_never_merges_stderr() -> None:
    """Fold-fix (shape-fix): exactly ONE field is extracted. With a non-empty
    stdout the compressed text must map back onto ``stdout`` ALONE — folding a
    ``[stderr]`` tail in destroys the engine's structured-array detection (a JSON
    stdout drops from ~98% to 0%) and has no single home in the mirrored object.
    stderr rides through verbatim in its own field via ``_reinject``, never merged
    into stdout."""
    out = _hook._extract_text({"stdout": "ok-line", "stderr": "boom-error", "interrupted": False})
    assert out == "ok-line"
    assert "boom-error" not in out


def test_bash_stderr_only_when_stdout_empty() -> None:
    """RED on old code. A command that only wrote to stderr is still compressible."""
    out = _hook._extract_text({"stdout": "", "stderr": "fatal: nope", "interrupted": False})
    assert out is not None and "fatal: nope" in out


def test_bash_empty_output_is_none() -> None:
    """Empty stdout+stderr -> nothing to compress -> None (passthrough)."""
    assert _hook._extract_text({"stdout": "", "stderr": "", "interrupted": False}) is None


def test_bash_interrupted_partial_stdout_extracted() -> None:
    assert (
        _hook._extract_text({"stdout": "partial", "stderr": "", "interrupted": True}) == "partial"
    )


def test_real_agent_content_blocks_extracted() -> None:
    """Task/Agent already worked via content recursion — assert no regression."""
    assert _hook._extract_text(_REAL_AGENT) == "PINEAPPLE"


def test_real_websearch_passes_through_unmatched() -> None:
    """WebSearch ({"query","results":[...]}) has no single free-text field — the
    whole object is the payload. An earlier revision extracted it as
    ``json.dumps(tool_response)`` (its "Bug-14"), but that text could only be
    emitted as a bare string, which Claude Code >= 2.1.163 validates against
    WebSearch's output schema and DROPS on mismatch (#68951) — so no model ever saw
    a compressed WebSearch result on this path. The shape-fix mirrors ONE field of
    the incoming object (``_reinject``); whole-object JSON has no single field to
    map the compressed text back onto, so this shape now passes through UNMATCHED
    (extract -> None -> passthrough) rather than emit a value the host rejects. A
    schema-valid WebSearch mirror is tracked as future work."""
    assert _hook._extract_text(_REAL_WEBSEARCH) is None


def test_dict_without_results_or_text_is_still_none() -> None:
    # Totality preserved: a dict with no text field AND no results array is None.
    assert _hook._extract_text({"foo": "bar", "count": 3}) is None


# --- legacy shapes: must keep working exactly as before (no regression) --------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("plain string output", "plain string output"),
        ("", None),
        ({"content": "wrapped text"}, "wrapped text"),
        ({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "ab"),
        ([{"type": "text", "text": "x"}], "x"),
        ([{"type": "image", "source": {"data": "..."}}], None),
        ({"content": [{"type": "image", "source": {}}]}, None),
        ({"unknown_key": "value"}, None),
        ({}, None),
        (None, None),
        (123, None),
    ],
)
def test_legacy_and_unknown_shapes_unchanged(payload: object, expected: object) -> None:
    assert _hook._extract_text(payload) == expected


# --- visibility: FURL_HOOK_VERBOSE emits one stderr line naming the no-op reason


def _run_hook(payload: dict, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "FURL_CCR_BACKEND": "memory", **env_extra},
    )


def test_verbose_reports_shape_unmatched() -> None:
    proc = _run_hook(
        {"tool_name": "Bash", "tool_response": {"foo": "bar"}},
        {"FURL_HOOK_VERBOSE": "1"},
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert "no-op (shape-unmatched)" in proc.stderr


def test_verbose_reports_below_min_chars() -> None:
    proc = _run_hook(
        {
            "tool_name": "Bash",
            "tool_response": {"stdout": "tiny", "stderr": "", "interrupted": False},
        },
        {"FURL_HOOK_VERBOSE": "1"},
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert "no-op (below-min-chars)" in proc.stderr


def test_verbose_reports_excluded_tool() -> None:
    proc = _run_hook(
        {"tool_name": "mcp__x__furl_compress", "tool_response": {"content": "x" * 5000}},
        {"FURL_HOOK_VERBOSE": "1"},
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert "no-op (excluded-tool)" in proc.stderr


def test_verbose_reports_disabled() -> None:
    proc = _run_hook(
        {
            "tool_name": "Bash",
            "tool_response": {"stdout": "x" * 5000, "stderr": "", "interrupted": False},
        },
        {"FURL_HOOK_VERBOSE": "1", "FURL_HOOK_ENABLED": "0"},
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert "no-op (disabled)" in proc.stderr


def test_verbose_reports_import_failed_not_no_savings(tmp_path) -> None:
    """Compress-stage failures carry distinct labels: an unimportable furl_ctx must
    surface as ``import-failed``, never the umbrella ``no-savings`` (F1)."""
    poison = tmp_path / "furl_ctx"
    poison.mkdir()
    (poison / "__init__.py").write_text("raise ImportError('poisoned for test')\n")
    proc = _run_hook(
        {
            "tool_name": "Bash",
            "tool_response": {"stdout": "x" * 5000, "stderr": "", "interrupted": False},
        },
        # PYTHONPATH precedes site-packages, so the poisoned package wins the import.
        {"FURL_HOOK_VERBOSE": "1", "PYTHONPATH": str(tmp_path)},
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert "no-op (import-failed)" in proc.stderr
    assert "no-savings" not in proc.stderr


def test_quiet_by_default_no_noop_line() -> None:
    """Without FURL_HOOK_VERBOSE the hook stays silent on a no-op (stderr empty)."""
    proc = _run_hook({"tool_name": "Bash", "tool_response": {"foo": "bar"}}, {})
    assert proc.returncode == 0 and proc.stdout == ""
    assert proc.stderr == ""
