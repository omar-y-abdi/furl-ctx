"""Shape-mirroring contract for the PostToolUse hook (anthropics/claude-code#68951).

Claude Code >= 2.1.163 validates ``hookSpecificOutput.updatedToolOutput`` against
the originating tool's zod output schema (Bash: an OBJECT ``{stdout, stderr,
interrupted, ...}``) and silently keeps the ORIGINAL output on any mismatch. The
hook used to emit the compressed text as a BARE STRING, so compression never
reached the model. The fix is shape MIRRORING: :func:`_reinject` returns the
host's own incoming ``tool_response`` with ONLY the extracted text field replaced
— no shape is ever invented, so the emitted value passes validation by
construction.

Four layers, mirroring the upstream debug suite this was ported from:

  UNIT        — :func:`_extract_text` / :func:`_reinject` as pure functions.
  PROPERTY    — ``extract(x) is None  <=>  reinject(x, s) is None`` over a shape
                corpus; Bash-required zod keys survive mirroring; every mirror is
                JSON-serializable.
  INTEGRATION — the real hook subprocess (this repo's file + the locally-built
                engine, NOT a pinned PyPI release), fed synthetic PostToolUse
                stdin, asserted on the emitted ``updatedToolOutput`` and on CCR
                retrievability.
  REGRESSION  — a bare string FAILS the Bash zod replica (the bug, as a test); the
                mirrored object passes.

WebSearch note (R1): ``{"query","results":[...]}`` has no single free-text field
— its whole object is the payload — so it cannot be mirrored into one field and
passes through UNMATCHED (``extract -> None``). It is pinned below as a both-None
case; a schema-valid per-result WebSearch mirror is future work.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parents[1] / "plugins" / "furl" / "hooks" / "compress_tool_output.py"
)


def _load_hook() -> Any:
    # The hook does module-level os.environ.setdefault(...); save/restore so that
    # importing it here cannot leak FURL_CCR_* into the rest of the suite.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "furl_hook_output_shape_under_test", _HOOK_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


hook = _load_hook()

# One isolated CCR namespace for the whole session, so integration tests never
# touch (or evict from) a real per-project store.
_NAMESPACE = tempfile.mkdtemp(prefix="furl-hookshape-test-")

MARKER_RE = re.compile(r"<<ccr:([0-9a-f]+)")


# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------


def compressible_rows(n: int = 300) -> str:
    """A JSON array of near-uniform objects — furl's best case, deterministic."""
    return json.dumps(
        [
            {"i": i, "name": "RunTask", "cat": "devtools.timeline", "ph": "X", "dur": i % 9}
            for i in range(n)
        ]
    )


def bash_response(stdout: str, stderr: str = "", **extra: Any) -> dict[str, Any]:
    """The exact Bash tool_response shape observed in live 2.1.x payloads."""
    return {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False, **extra}


def payload(tool_name: str, tool_response: Any) -> dict[str, Any]:
    return {
        "session_id": "test-session",
        "cwd": _NAMESPACE,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {},
        "tool_response": tool_response,
    }


# --------------------------------------------------------------------------
# Subprocess runner — this repo's hook file + the locally-built engine
# (sys.executable == the venv python that has furl_ctx installed), NOT `uv run
# --with furl-ctx==<pin>`: the integration tests must exercise the repo's engine.
# --------------------------------------------------------------------------


def run_hook(
    body: dict[str, Any],
    *,
    env_extra: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, subprocess.CompletedProcess[str]]:
    """Run the hook as the host does; return (parsed stdout | None, proc)."""
    env = os.environ.copy()
    env["FURL_CCR_BACKEND"] = "sqlite"
    env["FURL_CCR_TTL_SECONDS"] = "86400"
    env["FURL_CCR_PROJECT_DIR"] = _NAMESPACE
    env.pop("FURL_HOOK_ENABLED", None)
    env.pop("FURL_HOOK_MIN_CHARS", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(_HOOK_PATH)],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"hook must be fail-open (exit 0); got {proc.returncode}: {proc.stderr}"
    )
    if not proc.stdout.strip():
        return None, proc
    return json.loads(proc.stdout), proc


def updated_output(result: dict[str, Any] | None) -> Any:
    assert result is not None, "expected an updatedToolOutput emission, got passthrough"
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    return hso["updatedToolOutput"]


def ccr_retrieve(hash_: str) -> str | None:
    """Full-retrieve *hash_* from the SAME namespaced store the hook wrote to."""
    env = os.environ.copy()
    env["FURL_CCR_BACKEND"] = "sqlite"
    env["FURL_CCR_PROJECT_DIR"] = _NAMESPACE
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, furl_ctx; c = furl_ctx.retrieve(sys.argv[1]); "
            "sys.stdout.write(c if c is not None else '\\x00MISS')",
            hash_,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return None if proc.stdout == "\x00MISS" else proc.stdout


# --------------------------------------------------------------------------
# Zod-schema replica (evidence: 2.1.212 bundle, Bash outputSchema —
# required: stdout string, stderr string, interrupted boolean; the rest optional)
# --------------------------------------------------------------------------


def assert_passes_bash_zod_replica(value: Any) -> None:
    assert isinstance(value, dict), f"Bash updatedToolOutput must be an object, got {type(value)}"
    assert isinstance(value.get("stdout"), str), "required: stdout string"
    assert isinstance(value.get("stderr"), str), "required: stderr string"
    assert isinstance(value.get("interrupted"), bool), "required: interrupted boolean"


# ==========================================================================
# UNIT — pure functions
# ==========================================================================


class TestReinjectUnit:
    def test_bash_object_mirrored(self) -> None:
        resp = bash_response("x" * 50, "", returnCodeInterpretation="ok")
        out = hook._reinject(resp, "COMPRESSED")
        assert out == {
            "stdout": "COMPRESSED",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "returnCodeInterpretation": "ok",
        }

    def test_bash_nonempty_stderr_preserved_verbatim(self) -> None:
        resp = bash_response("out" * 30, "boom")
        out = hook._reinject(resp, "COMPRESSED")
        assert out["stdout"] == "COMPRESSED"
        assert out["stderr"] == "boom"  # never merged, never emptied
        assert out["interrupted"] is False

    def test_bash_stderr_only_lands_in_stderr_field(self) -> None:
        resp = bash_response("", "big stderr traceback")
        out = hook._reinject(resp, "COMPRESSED")
        assert out["stdout"] == ""  # untouched
        assert out["stderr"] == "COMPRESSED"

    def test_bash_empty_stderr_stays_empty(self) -> None:
        out = hook._reinject(bash_response("data"), "C")
        assert out["stderr"] == ""

    def test_plain_string(self) -> None:
        assert hook._reinject("big text", "C") == "C"

    def test_content_str_wrapper(self) -> None:
        assert hook._reinject({"content": "big", "meta": 1}, "C") == {"content": "C", "meta": 1}

    def test_content_blocks_wrapper(self) -> None:
        resp = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        assert hook._reinject(resp, "C") == {"content": [{"type": "text", "text": "C"}]}

    def test_result_field(self) -> None:
        assert hook._reinject({"result": "page", "url": "u"}, "C") == {"result": "C", "url": "u"}

    def test_text_field(self) -> None:
        assert hook._reinject({"text": "t", "k": 2}, "C") == {"text": "C", "k": 2}

    def test_blocks_list(self) -> None:
        resp = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert hook._reinject(resp, "C") == [{"type": "text", "text": "C"}]

    def test_content_takes_precedence_over_stdout(self) -> None:
        resp = {"content": "inner", "stdout": "raw", "stderr": "", "interrupted": False}
        out = hook._reinject(resp, "C")
        assert out["content"] == "C"
        assert out["stdout"] == "raw"  # extraction read `content`; stdout untouched

    def test_empty_content_falls_through_to_stdout(self) -> None:
        resp = {"content": "", "stdout": "raw data", "stderr": "", "interrupted": False}
        out = hook._reinject(resp, "C")
        assert out["stdout"] == "C"
        assert out["content"] == ""  # untouched: extraction fell through it

    def test_input_dict_not_mutated(self) -> None:
        resp = bash_response("abc")
        snapshot = json.dumps(resp, sort_keys=True)
        hook._reinject(resp, "C")
        assert json.dumps(resp, sort_keys=True) == snapshot


# ==========================================================================
# UNIT — Finding 1 gate predicate (_redaction_changed_visible_output)
# The non-compressing paths (below-min-chars / no-savings) must emit a scrubbed
# mirror when redaction changed ANY model-visible field — not only the extracted
# one — or a secret in a preserved Bash stderr leaks through on passthrough.
# ==========================================================================


class TestRedactionChangedVisibleOutput:
    def test_stderr_only_secret_detected_when_stdout_is_clean(self, monkeypatch) -> None:
        """The leak: a clean stdout is the extracted field, so `redacted == text`,
        yet a secret sits in the preserved stderr. The predicate must still report a
        change so the gate emits the scrubbed mirror."""
        monkeypatch.setenv("FURL_REDACT_BUILTINS", "0")
        monkeypatch.setenv("FURL_REDACT_PATTERNS", r"SECRET-\d+")
        resp = bash_response("all fine here", "auth token SECRET-99999 leaked")
        text = hook._extract_text(resp)
        assert text == "all fine here"  # stdout is the extracted field
        redacted = hook._apply_env_redaction(text)
        assert redacted == text  # the extracted field itself is clean
        assert hook._redaction_changed_visible_output(resp, text, redacted) is True

    def test_all_clean_is_no_change_even_with_patterns_set(self, monkeypatch) -> None:
        """Nothing matches -> no change -> caller keeps a byte-identical passthrough."""
        monkeypatch.setenv("FURL_REDACT_BUILTINS", "0")
        monkeypatch.setenv("FURL_REDACT_PATTERNS", r"SECRET-\d+")
        resp = bash_response("all fine here", "nothing to see")
        text = hook._extract_text(resp)
        redacted = hook._apply_env_redaction(text)
        assert hook._redaction_changed_visible_output(resp, text, redacted) is False

    def test_secret_in_extracted_stdout_detected(self, monkeypatch) -> None:
        monkeypatch.setenv("FURL_REDACT_BUILTINS", "0")
        monkeypatch.setenv("FURL_REDACT_PATTERNS", r"SECRET-\d+")
        resp = bash_response("token SECRET-1 here", "clean stderr")
        text = hook._extract_text(resp)
        redacted = hook._apply_env_redaction(text)
        assert redacted != text
        assert hook._redaction_changed_visible_output(resp, text, redacted) is True

    def test_list_canonicalization_is_not_reported_as_a_change(self, monkeypatch) -> None:
        """Why the predicate is field-aware and not a plain
        ``_reinject(...) != tool_response``: with NO redaction configured,
        ``_reinject`` still canonicalizes a multi-block list to a single block, so a
        mirror-compare gate would FALSELY emit and collapse the blocks. The
        field-aware predicate reports "nothing redacted" and the caller keeps a true
        passthrough — the binding 'zero change when nothing was redacted' contract."""
        monkeypatch.setenv("FURL_REDACT_BUILTINS", "0")
        monkeypatch.delenv("FURL_REDACT_PATTERNS", raising=False)
        resp = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        text = hook._extract_text(resp)
        redacted = hook._apply_env_redaction(text)
        assert redacted == text
        assert hook._reinject(resp, redacted) != resp  # mirror DOES differ (canonicalized)
        assert hook._redaction_changed_visible_output(resp, text, redacted) is False


# ==========================================================================
# PROPERTY — extract/reinject duality + schema preservation
# ==========================================================================

SHAPE_CORPUS: list[Any] = [
    # extractable
    "plain string",
    bash_response("stdout text"),
    bash_response("stdout text", "stderr text"),
    bash_response("", "only stderr"),
    {"content": "wrapped"},
    {"content": [{"type": "text", "text": "block"}]},
    {"result": "webfetch answer", "url": "https://x"},
    {"text": "generic"},
    [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
    {"content": "", "stdout": "fallthrough", "stderr": "", "interrupted": False},
    # NOT extractable
    None,
    42,
    3.14,
    True,
    "",
    {},
    {"stdout": 5},
    {"stdout": ""},  # terminal stdout branch, empty
    {"stdout": "", "result": "unreachable"},  # stdout branch is terminal in extractor
    {"query": "q", "results": [{"title": "t"}]},  # WebSearch: whole-object, no single field (R1)
    [],
    [{"type": "image", "data": "…"}],
    [{"type": "text", "text": 7}],
    ["bare string in list"],
    {"content": 99},
    {"content": {"deep": "dict"}},
]


class TestExtractReinjectDuality:
    @pytest.mark.parametrize("shape", SHAPE_CORPUS, ids=lambda s: repr(s)[:60])
    def test_reinject_defined_iff_extract_defined(self, shape: Any) -> None:
        extracted = hook._extract_text(shape)
        reinjected = hook._reinject(shape, "COMPRESSED")
        assert (extracted is None) == (reinjected is None)

    @pytest.mark.parametrize(
        "shape",
        [
            s
            for s in SHAPE_CORPUS
            if isinstance(s, dict)
            and isinstance(s.get("stdout"), str)
            and hook._extract_text(s) is not None
        ],
        ids=lambda s: repr(s)[:60],
    )
    def test_bash_shapes_keep_required_zod_keys(self, shape: Any) -> None:
        out = hook._reinject(shape, "C")
        assert_passes_bash_zod_replica(out)
        # every original key survives; only stdout/stderr may change value
        for key, val in shape.items():
            assert key in out
            if key not in ("stdout", "stderr"):
                assert out[key] == val

    def test_reinjected_output_is_json_serializable(self) -> None:
        for shape in SHAPE_CORPUS:
            out = hook._reinject(shape, "C")
            if out is not None:
                json.dumps(out)

    def test_websearch_whole_object_is_both_none(self) -> None:
        """R1 pin (required): WebSearch's whole-object shape has no single field to
        mirror, so it stays a both-None case — extract None, reinject None — and the
        duality invariant holds. This is the shape the upstream 1.3.0 branch tried
        to extract as ``json.dumps`` (Bug-14); on this path that was only ever
        emitted as a host-dropped bare string, so R1 is a zero-regression
        restoration of the invariant, not a lost capability."""
        websearch = {"query": "claude code hooks", "results": [{"title": "t", "url": "u"}]}
        assert hook._extract_text(websearch) is None
        assert hook._reinject(websearch, "C") is None


# ==========================================================================
# INTEGRATION — real hook subprocess with the locally-built engine
# ==========================================================================


class TestHookProcessIntegration:
    def test_bash_compression_emits_schema_valid_object(self) -> None:
        big = compressible_rows()
        result, _ = run_hook(payload("Bash", bash_response(big)))
        out = updated_output(result)
        assert_passes_bash_zod_replica(out)
        assert len(out["stdout"]) < len(big), "compression must shrink stdout"
        assert out["interrupted"] is False
        assert out["isImage"] is False

    def test_bash_compressed_original_is_ccr_retrievable(self) -> None:
        big = compressible_rows()
        result, _ = run_hook(payload("Bash", bash_response(big)))
        out = updated_output(result)
        markers = MARKER_RE.findall(out["stdout"])
        assert markers, f"expected a <<ccr:…>> marker in compressed stdout: {out['stdout'][:200]}"
        original = ccr_retrieve(markers[0])
        assert original is not None, (
            "hook-stored original must be retrievable from the shared store"
        )
        assert '"name": "RunTask"' in original or '"name":"RunTask"' in original

    def test_bash_stderr_preserved_while_stdout_compresses(self) -> None:
        """Regression for the fold bug: a non-empty stderr must not cost the
        structured stdout its compression, and stderr must reach the model verbatim
        in its own field."""
        big = compressible_rows()
        result, _ = run_hook(payload("Bash", bash_response(big, "ERR-SENTINEL-42\ntraceback line")))
        out = updated_output(result)
        assert_passes_bash_zod_replica(out)
        assert out["stderr"] == "ERR-SENTINEL-42\ntraceback line"
        assert len(out["stdout"]) < len(big), "stderr presence must not kill stdout compression"

    def test_bash_stderr_only_output_compresses_into_stderr_field(self) -> None:
        big = compressible_rows()
        result, _ = run_hook(payload("Bash", bash_response("", big)))
        out = updated_output(result)
        assert_passes_bash_zod_replica(out)
        assert out["stdout"] == ""
        assert len(out["stderr"]) < len(big)

    def test_plain_string_response_stays_string(self) -> None:
        result, _ = run_hook(payload("WebFetch", compressible_rows()))
        out = updated_output(result)
        assert isinstance(out, str)
        assert len(out) < len(compressible_rows())

    def test_webfetch_result_dict_mirrored(self) -> None:
        resp = {"result": compressible_rows(), "url": "https://example.com", "durationMs": 12}
        result, _ = run_hook(payload("WebFetch", resp))
        out = updated_output(result)
        assert isinstance(out, dict)
        assert out["url"] == "https://example.com"
        assert out["durationMs"] == 12
        assert len(out["result"]) < len(resp["result"])

    def test_task_content_blocks_mirrored(self) -> None:
        resp = {"content": [{"type": "text", "text": compressible_rows()}], "totalDurationMs": 5}
        result, _ = run_hook(payload("Task", resp))
        out = updated_output(result)
        assert isinstance(out, dict)
        assert out["totalDurationMs"] == 5
        assert isinstance(out["content"], list)
        assert out["content"][0]["type"] == "text"
        assert len(out["content"][0]["text"]) < len(compressible_rows())

    def test_small_output_passes_through(self) -> None:
        result, _ = run_hook(payload("Bash", bash_response("tiny")))
        assert result is None

    def test_existing_ccr_marker_passes_through(self) -> None:
        text = "already compressed <<ccr:deadbeefdead>> " + "x" * 3000
        result, _ = run_hook(payload("Bash", bash_response(text)))
        assert result is None

    def test_furl_own_tools_excluded(self) -> None:
        result, _ = run_hook(payload("mcp__plugin_furl_furl__furl_compress", compressible_rows()))
        assert result is None

    def test_kill_switch_disables(self) -> None:
        result, _ = run_hook(
            payload("Bash", bash_response(compressible_rows())),
            env_extra={"FURL_HOOK_ENABLED": "0"},
        )
        assert result is None

    def test_websearch_shape_passes_through(self) -> None:
        """R1 at the process level: a real WebSearch payload is matched by the hook
        but has no mirrorable field, so it passes through (no emission) rather than
        emit a host-rejected value."""
        result, _ = run_hook(
            payload("WebSearch", {"query": "q", "results": [{"title": "t", "url": "u"}] * 200})
        )
        assert result is None

    def test_preserved_stderr_still_honors_redaction(self) -> None:
        """The legacy merge redacted the whole blob; the mirrored, non-compressed
        stderr field must not weaken that guarantee."""
        result, _ = run_hook(
            payload("Bash", bash_response(compressible_rows(), "token SECRET-123 leaked")),
            env_extra={"FURL_REDACT_PATTERNS": r"SECRET-\d+"},
        )
        out = updated_output(result)
        assert_passes_bash_zod_replica(out)
        assert "SECRET-123" not in out["stderr"]
        assert "leaked" in out["stderr"], "non-secret stderr text must survive redaction"

    def test_stderr_only_secret_scrubbed_on_below_min_chars_passthrough(self) -> None:
        """Finding 1, side 1 — the leak the emit-path scrub did NOT cover: a clean
        small stdout is below the compression threshold, so the output was passed
        through untouched while a secret sat in the preserved stderr. The gate now
        emits the fully scrubbed mirror instead of passing the raw secret through."""
        result, _ = run_hook(
            payload("Bash", bash_response("all fine here", "auth token SECRET-99999 leaked")),
            env_extra={"FURL_REDACT_BUILTINS": "0", "FURL_REDACT_PATTERNS": r"SECRET-\d+"},
        )
        out = updated_output(result)  # an emission, not a passthrough
        assert_passes_bash_zod_replica(out)
        assert out["stdout"] == "all fine here"  # the clean extracted field rides through
        assert "SECRET-99999" not in json.dumps(out), "no raw secret anywhere in the emission"
        assert "[REDACTED:" in out["stderr"], "the preserved stderr is scrubbed in place"

    def test_clean_small_output_stays_true_passthrough_with_patterns_set(self) -> None:
        """Finding 1, side 2 — zero behavior change in the common clean case: with
        redaction patterns configured but nothing matching, the hook must still emit
        NOTHING (updatedToolOutput absent), a byte-identical passthrough."""
        result, _ = run_hook(
            payload("Bash", bash_response("all fine here", "nothing secret here")),
            env_extra={"FURL_REDACT_BUILTINS": "0", "FURL_REDACT_PATTERNS": r"SECRET-\d+"},
        )
        assert result is None


# ==========================================================================
# REGRESSION — a bare string is exactly the shape the host rejects
# (adapted from the upstream stock/.orig pair: the repo ships no `.orig`, so the
# rejected shape is pinned directly against the zod replica plus a live emit)
# ==========================================================================


class TestBareStringRegression:
    def test_bare_string_fails_bash_zod_replica(self) -> None:
        """The bug, as a test: the stock hook wrote the compressed text as a BARE
        STRING into ``updatedToolOutput`` — exactly the shape Claude Code >= 2.1.163
        rejects for Bash (its schema needs an object), the mismatch 2.1.212 logs and
        falls back on. This documents the rejected shape the fix removes."""
        with pytest.raises(AssertionError):
            assert_passes_bash_zod_replica('"[7]{compressed bash output}"')

    def test_reinject_turns_the_bare_string_into_a_passing_object(self) -> None:
        """The fix: the same compressed text, run through ``_reinject``, becomes the
        mirrored Bash object that passes the exact validation the bare string
        failed — the model-visible replacement is honored, not dropped."""
        mirrored = hook._reinject(bash_response(compressible_rows()), "[7]{compressed bash output}")
        assert_passes_bash_zod_replica(mirrored)

    def test_live_hook_emits_object_not_bare_string_for_bash(self) -> None:
        """End-to-end: the repo hook, run on a Bash payload, emits an OBJECT that
        passes the replica — never a bare string. This is the pin that would fail
        against the pre-fix ``_emit``."""
        result, _ = run_hook(payload("Bash", bash_response(compressible_rows())))
        out = updated_output(result)
        assert not isinstance(out, str), "the fix must never emit a bare string for Bash"
        assert_passes_bash_zod_replica(out)
