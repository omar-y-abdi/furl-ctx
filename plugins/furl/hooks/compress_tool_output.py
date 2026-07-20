#!/usr/bin/env python3
"""Furl PostToolUse hook: compress large tool outputs before they enter context.

Contract (Claude Code PostToolUse hook):
  - stdin  : JSON with ``tool_name``, ``tool_input``, ``tool_response``, ``cwd``,
             ``session_id``, ``hook_event_name``.
  - stdout : to REPLACE the output the model sees, emit
             ``{"hookSpecificOutput": {"hookEventName": "PostToolUse",
                "updatedToolOutput": <tool_response mirrored, text field compressed>}}``
             and exit 0. The value MIRRORS the incoming ``tool_response`` shape
             (see ``_reinject``): Claude Code >= 2.1.163 validates it against the
             tool's output schema and silently drops a mismatched value, so a bare
             string never replaced a Bash ``{stdout, ...}`` object
             (anthropics/claude-code#68951).
  - stdout empty + exit 0 : original tool output passes through unchanged.

Design invariant — FAIL OPEN. Any error (bad stdin, missing furl_ctx, compression
failure, unexpected payload shape) results in exit 0 with NO stdout, so the user's
tool call is never broken. The hook only ever *removes* tokens on the happy path;
it never blocks, mutates inputs, or raises to the host.

Retrievability: compression offloads dropped content to the shared CCR store and
leaves ``<<ccr:HASH>>`` markers. For the ``furl`` MCP server's ``furl_retrieve`` to
resolve those markers, this hook and the server must share one durable store —
both pin ``FURL_CCR_BACKEND=sqlite`` (see hooks.json / .mcp.json) and both derive
the same per-project namespace (``FURL_CCR_PROJECT_DIR``, from CLAUDE_PROJECT_DIR /
stdin ``cwd``), which resolves to a per-project ``~/.furl/ccr-ns-<hash>.sqlite3``
in every process of the session. The legacy global ``~/.furl/ccr.sqlite3`` serves
only when namespacing is explicitly disabled (``FURL_CCR_PROJECT_DIR=""``).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NoReturn

# Tune via environment (all optional; defaults are the shipped behavior).
_ENABLED_ENV = "FURL_HOOK_ENABLED"
_MIN_CHARS_ENV = "FURL_HOOK_MIN_CHARS"
_MODEL_ENV = "FURL_HOOK_MODEL"
_EXCLUDE_ENV = "FURL_HOOK_EXCLUDE_TOOLS"
_MODE_ENV = "FURL_HOOK_MODE"
_VERBOSE_ENV = "FURL_HOOK_VERBOSE"

_DEFAULT_MIN_CHARS = 2000
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
_CCR_MARKER = "<<ccr:"

# Pin the durable, cross-process CCR store BEFORE furl_ctx builds it. Without this
# the library default is an in-memory store that dies when this subprocess exits —
# so a ``<<ccr:HASH>>`` marker this hook emits would have no retrievable original and
# the `furl` MCP server's ``furl_retrieve`` would miss. ``setdefault`` keeps any
# user override (e.g. ``FURL_CCR_BACKEND=memory``) intact. This does not depend on
# the ``env`` block in hooks.json being honored by the host.
os.environ.setdefault("FURL_CCR_BACKEND", "sqlite")
# Opt into the per-namespace durable SPILL tier (T6): a capacity-evicted entry is
# demoted to this project's own ``-spill`` sqlite file instead of being dropped at
# the 1000-entry cap, so a ``<<ccr:HASH>>`` marker stays retrievable past eviction.
# ``setdefault`` keeps a user's ``FURL_CCR_SPILL=0`` opt-out intact. Matches the
# same var in .mcp.json so the hook that spills and the server that reads it agree.
os.environ.setdefault("FURL_CCR_SPILL", "1")
os.environ.setdefault("FURL_CCR_TTL_SECONDS", "86400")

# Furl's own tool output must never be recompressed (would double-compress or
# compress content the model just retrieved). Furl's MCP tools are namespaced
# ``mcp__<server>__furl_*`` by the host. This is the built-in loop-guard base
# (as a glob, always excluded); operators add more via FURL_HOOK_EXCLUDE_TOOLS.
_SELF_TOOL_SUBSTR = "furl_"

# --- observability counters (shared with the PreToolUse pipe + furl_stats) ------
# Resolved ONCE per run in main() and stashed here so the terminal _passthrough /
# _emit tally exactly one outcome (invariant: invocations == compressions + noop
# buckets). Imported lazily and FAIL-OPEN — a counter problem never changes the
# hook's stdout/exit or breaks the tool call. Inert until the runtime furl-ctx
# ships the store-level counter API (older pinned engines just no-op here). See
# _furl_ccr_counters and furl_stats' "store" block.
_run_counter_ctx: tuple[Any, Any] | None = None


def _counters_module() -> Any:
    """Lazily import the sibling counter helper. Returns None when unavailable so
    counting degrades to a no-op instead of ever raising into the hook."""
    try:
        import _furl_ccr_counters

        return _furl_ccr_counters
    except Exception:
        return None


def _host_version_module() -> Any:
    """Lazily import furl_ctx.host_version (T7). Returns None when unavailable so
    version detection degrades to "unknown" instead of ever raising into the
    hook — the same fail-open posture as ``_counters_module``."""
    try:
        import furl_ctx.host_version as host_version

        return host_version
    except Exception:
        return None


def _record_noop(reason: str | None) -> None:
    """Tally a no-op outcome bucket for this run (fail-open, no-op if unset)."""
    ctx = _run_counter_ctx
    if ctx is None or not reason:
        return
    cmod, cstore = ctx
    if cmod is not None and cstore is not None:
        cmod.bump(cstore, cmod.HOOK_NOOP_PREFIX + reason)


def _record_compression() -> None:
    """Tally a genuine compression outcome for this run (fail-open)."""
    ctx = _run_counter_ctx
    if ctx is None:
        return
    cmod, cstore = ctx
    if cmod is not None and cstore is not None:
        cmod.bump(cstore, cmod.HOOK_COMPRESSIONS)


def _flag_enabled(raw: str | None) -> bool:
    """Interpret an on/off env flag. Unset or empty -> enabled (default on)."""
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _noop_verbose_enabled() -> bool:
    """Whether to log a one-line reason when the hook no-ops.

    Opt-in (explicit truthy FURL_HOOK_VERBOSE), NOT default-on: a no-op fires on
    nearly every tool call (most outputs are small / non-text), so defaulting this on
    would flood stderr. This is deliberately stricter than the rare success-path
    annotation, which keeps its shipped default-on ``_flag_enabled`` gate."""
    return os.environ.get(_VERBOSE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def _min_chars() -> int:
    """Minimum tool-output length (chars) before compression is attempted."""
    raw = os.environ.get(_MIN_CHARS_ENV, "").strip()
    if not raw:
        return _DEFAULT_MIN_CHARS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MIN_CHARS
    return value if value > 0 else _DEFAULT_MIN_CHARS


def _extract_text(tool_response: Any) -> str | None:
    """Pull compressible plain text out of a ``tool_response``.

    Handles the shapes Claude Code actually emits for the matched tools, verified
    against live PostToolUse payloads (Claude Code 2.1.x):

      * ``str``                        -> plain tool output.
      * ``[{"type":"text","text":.}]`` -> MCP-style content blocks.
      * ``{"content": <str|blocks>}``  -> wrapped result; also the Task/``Agent``
                                          sub-agent answer (content blocks).
      * ``{"stdout","stderr",..}``     -> Bash. Extracts exactly ONE field:
                                          ``stdout`` when non-empty, else a non-empty
                                          ``stderr``. Never a merge — the compressed
                                          text must map back onto a single field of
                                          the mirrored Bash object (see ``_reinject``),
                                          and folding stderr into stdout also destroys
                                          the engine's structured-array detection.
      * ``{"result": <str>, ..}``      -> WebFetch (its answer lives in ``result``).
      * ``{"text": <str>, ..}``        -> generic single-text-field results.

    Returns ``None`` for anything else — images, mixed non-text blocks, empty
    output, and structured payloads with no free-text field (including WebSearch's
    ``{"query","results":[..]}``, whose whole object is the payload and so has no
    single field to mirror the compressed text back onto — see the ``_reinject``
    note). Totality is preserved: unknown shape -> ``None`` -> caller passes the
    original through untouched.
    """
    if isinstance(tool_response, str):
        return tool_response or None

    if isinstance(tool_response, dict):
        # Wrapped result / MCP-style blocks / Task(Agent) sub-agent answer. Kept
        # first so the legacy ``{"content": ...}`` contract is byte-identical; only
        # fall through when ``content`` is absent or carries no text, so a Bash-style
        # sibling key (``stdout``) on the same dict is still reachable.
        content = tool_response.get("content")
        if content is not None:
            text = _extract_text(content)
            if text is not None:
                return text

        # Bash: {"stdout","stderr","interrupted","isImage","noOutputExpected"}.
        # Exactly ONE field is ever extracted (stdout preferred, stderr as the
        # fallback for stderr-only output), so the compressed text has exactly one
        # home in the mirrored object (``_reinject``) and a structured-array stdout
        # keeps its compression ratio — folding a ``[stderr]`` tail onto it dropped
        # a 23,890-char JSON stdout from ~98% to 0%.
        stdout = tool_response.get("stdout")
        if isinstance(stdout, str):
            if stdout:
                return stdout
            stderr = tool_response.get("stderr")
            if isinstance(stderr, str) and stderr.strip():
                return stderr
            return None

        # WebFetch ("result") and other single-text-field results ("text").
        for key in ("result", "text"):
            value = tool_response.get(key)
            if isinstance(value, str):
                return value or None

        # WebSearch: {"query": ..., "results": [{title, url, ...}, ...]}. The whole
        # object is the payload — there is no single free-text field. An earlier
        # revision extracted it AS ``json.dumps(tool_response)`` (its "Bug-14"), but
        # that text could only ever be emitted as a bare string, which Claude Code
        # >= 2.1.163 validates against WebSearch's output schema and DROPS on
        # mismatch (the exact #68951 class this fix removes) — so no model ever saw
        # a compressed WebSearch result on this path. Shape-mirroring (``_reinject``)
        # replaces ONE field of the incoming object; whole-object JSON has no single
        # field to map the compressed text back onto, so this shape now passes
        # through UNMATCHED rather than emit a value the host rejects. A schema-valid
        # WebSearch mirror that compresses each result's fields in place is future
        # work (see the PR's follow-up note), not a shape this hook can invent.
        return None

    if isinstance(tool_response, list):
        parts: list[str] = []
        for block in tool_response:
            if not isinstance(block, dict):
                return None
            if block.get("type", "text") != "text":
                return None
            text = block.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
        joined = "".join(parts)
        return joined or None

    return None


def _reinject(tool_response: Any, compressed: str) -> Any | None:
    """Mirror *tool_response*'s shape with the compressed text swapped in.

    The dual of :func:`_extract_text`: whatever shape the host handed this hook is
    the shape handed back as ``updatedToolOutput``, with ONLY the text field
    ``_extract_text`` read replaced by *compressed*. Claude Code >= 2.1.163
    validates ``updatedToolOutput`` against the originating tool's output schema
    (e.g. Bash requires ``{stdout: str, stderr: str, interrupted: bool, ...}``) and
    silently keeps the ORIGINAL output on any mismatch — a bare string is why hook
    compression never reached the model (anthropics/claude-code#68951). Mirroring
    the incoming shape passes that validation by construction: no shape is ever
    invented, so every field the tool's schema requires is present because it was
    present in the response being replaced.

    Branch order matches :func:`_extract_text` exactly (``content`` before
    ``stdout`` before ``result``/``text``), so the field replaced is precisely the
    one the compressed text came from. Totality via the leading oracle check:
    returns ``None`` exactly when ``_extract_text`` returns ``None`` (WebSearch's
    whole-object shape included — it extracts to ``None``, so it mirrors to
    ``None``), and the caller falls back to passthrough — fail-open, never a
    mismatched emit.

    Bash detail: the compressed text lands in the ONE field ``_extract_text`` read
    — ``stdout`` when non-empty, else ``stderr`` — and the other field rides
    through byte-identical (but the preserved stderr is still redaction-scrubbed,
    since the legacy merge used to run the whole blob through redaction and the
    mirror must not weaken that). No merging, no emptying: placement stays
    faithful, and the byte-exact original of the replaced field stays retrievable
    from the CCR store.
    """
    if _extract_text(tool_response) is None:
        return None

    if isinstance(tool_response, str):
        return compressed

    if isinstance(tool_response, dict):
        content = tool_response.get("content")
        if content is not None and _extract_text(content) is not None:
            replaced = _reinject(content, compressed)
            if replaced is not None:
                return {**tool_response, "content": replaced}

        stdout = tool_response.get("stdout")
        if isinstance(stdout, str):
            if stdout:
                updated: dict[str, Any] = {**tool_response, "stdout": compressed}
                stderr = tool_response.get("stderr")
                if isinstance(stderr, str) and stderr:
                    # The preserved (non-compressed) field must still honor
                    # FURL_REDACT_PATTERNS: the legacy merge ran the whole blob
                    # through redaction, so the mirrored stderr may not weaken that
                    # guarantee. Identity when no patterns are configured.
                    updated["stderr"] = _apply_env_redaction(stderr)
                return updated
            stderr = tool_response.get("stderr")
            if isinstance(stderr, str) and stderr.strip():
                return {**tool_response, "stderr": compressed}
            return None

        for key in ("result", "text"):
            if isinstance(tool_response.get(key), str):
                return {**tool_response, key: compressed}

        return None

    if isinstance(tool_response, list):
        return [{"type": "text", "text": compressed}]

    return None


def _exclude_tools() -> set[str]:
    """Tools to never (re)compress: Furl's own output (loop guard) plus any the
    operator lists in FURL_HOOK_EXCLUDE_TOOLS (comma-separated; exact names or
    fnmatch globs like ``mcp__*``, per furl_ctx.config.is_tool_excluded)."""
    user = os.environ.get(_EXCLUDE_ENV, "")
    return {f"*{_SELF_TOOL_SUBSTR}*", *(t.strip() for t in user.split(",") if t.strip())}


def _excluded(tool_name: str) -> bool:
    """True if *tool_name* is excluded from compression. Uses the engine's
    glob-aware is_tool_excluded, falling back to the built-in loop guard if
    furl_ctx cannot be imported (the same fail-open posture as compression)."""
    if not tool_name:
        return False
    try:
        from furl_ctx.config import is_tool_excluded

        return is_tool_excluded(tool_name, _exclude_tools())
    except Exception:
        return _SELF_TOOL_SUBSTR in tool_name.lower()


def _mode_kwargs() -> dict[str, object]:
    """FURL_HOOK_MODE -> compress() overrides. ``aggressive`` also compresses code
    in the blob and squeezes smaller outputs; ``normal`` (default) keeps the
    shipped behavior. (``lossless_only`` is not yet wired — it needs an engine-side
    pipeline lever.)"""
    if os.environ.get(_MODE_ENV, "").strip().lower() == "aggressive":
        return {"protect_recent": 0, "min_tokens_to_compress": 50}
    return {}


def _compress_text(text: str, tool_name: str | None = None) -> tuple[str | None, str | None]:
    """Compress one tool-output blob via Furl's public pipeline.

    Returns ``(compressed, None)`` only when compression genuinely helped
    (no fail-open error AND the result is shorter). Returns ``(None, reason)``
    otherwise, signalling "leave the original alone" — where ``reason`` is a
    distinct diagnostic label for the FURL_HOOK_VERBOSE no-op line, so an
    import failure is never mislabeled as "no savings":

      * ``import-failed``  -> furl_ctx is not importable in the hook's env.
      * ``compress-error`` -> compress() raised (caught here; fail-open).
      * ``engine-error``   -> compress() reported an internal fail-open error.
      * ``empty-result``   -> engine returned no messages / non-text content.
      * ``no-savings``     -> compression succeeded but did not shrink the text.

    Never raises.
    """
    try:
        from furl_ctx import compress
    except Exception:
        return None, "import-failed"

    model = os.environ.get(_MODEL_ENV, "").strip() or _DEFAULT_MODEL
    try:
        result = compress(
            [{"role": "tool", "content": text}],
            model=model,
            tool_name=tool_name,
            **_mode_kwargs(),
        )
    except Exception:
        return None, "compress-error"

    # compress() is fail-open: on internal failure it returns the ORIGINAL
    # messages with ``error`` set. Treat that as "do nothing".
    if getattr(result, "error", None):
        return None, "engine-error"

    messages = getattr(result, "messages", None)
    if not messages:
        return None, "empty-result"

    compressed = messages[0].get("content")
    if not isinstance(compressed, str):
        return None, "empty-result"

    # Only replace when we actually saved characters.
    if len(compressed) >= len(text):
        return None, "no-savings"
    return compressed, None


def _passthrough(reason: str | None = None) -> NoReturn:
    """Emit nothing and succeed: the original tool output is kept verbatim.

    When FURL_HOOK_VERBOSE is on and *reason* is given, write ONE diagnostic line to
    stderr naming why nothing was compressed (shape-unmatched / below-min-chars /
    excluded-tool / disabled / ...). stderr is surfaced to the user only; it never
    reaches the model and never blocks the tool call — exit stays 0 (fail-open)."""
    if reason and _noop_verbose_enabled():
        sys.stderr.write(f"furl: no-op ({reason})\n")
    _record_noop(reason)
    sys.exit(0)


def _apply_env_redaction(text: str) -> str:
    """Scrub credentials from *text* before any gate.

    Applies the ON-by-default built-in credential patterns AND
    ``FURL_REDACT_PATTERNS`` here (not only inside ``compress()``) so a
    below-threshold output that never reaches the compressor still has secrets
    removed from what the model sees — matching the library redactor, which
    redacts every message regardless of whether anything compresses. The shared
    ``furl_ctx.redaction`` builder governs the hook, the MCP server, and the
    library identically; disable the built-ins with ``FURL_REDACT_BUILTINS=0``.

    Fail-open and total: if ``furl_ctx`` is not importable (the same degraded
    env where compression itself no-ops) or every redactor is off, the text is
    returned unchanged. Never raises."""
    try:
        from furl_ctx.redaction import build_store_redactor
    except Exception:
        return text
    try:
        redactor = build_store_redactor()
        if redactor is None:
            return text
        return redactor(text)
    except Exception:
        # Compiled-pattern re.sub does not raise, but stay fail-open on any
        # surprise: the durable copy is redacted by compress() downstream, and a
        # broken hook must never break the tool call.
        return text


def _redaction_changed_visible_output(tool_response: Any, text: str, redacted: str) -> bool:
    """True iff redaction changed a field the model would see on a NON-compressing
    passthrough (below-min-chars / no-savings), so the gate must emit the scrubbed
    mirror instead of passing the original through.

    ``redacted != text`` covers the EXTRACTED field. The Bash preserved field
    (``stderr``) needs its own check: ``_extract_text`` reads only ``stdout`` when
    ``stdout`` is non-empty, so a clean small ``stdout`` hid a secret in ``stderr``
    that the old extracted-field-only gate passed straight through — the emit path
    scrubs that ``stderr`` (``_reinject``) but the passthrough paths never did
    (review Finding 1).

    Field-aware on purpose: a plain ``_reinject(...) != tool_response`` gate would
    also fire when ``_reinject`` merely CANONICALIZES a multi-block list / content
    wrapper with NO redaction at all, breaking "zero change when nothing was
    redacted". Non-Bash shapes carry no second independent text field, so the
    extracted-field check governs them alone. Total and fail-open:
    ``_apply_env_redaction`` never raises, so neither does this."""
    if redacted != text:
        return True
    if isinstance(tool_response, dict):
        stdout = tool_response.get("stdout")
        stderr = tool_response.get("stderr")
        # Only when stdout is the extracted field (non-empty) is stderr the
        # *preserved* one; an stderr-only output extracts stderr itself and is
        # already covered by ``redacted != text`` above.
        if isinstance(stdout, str) and stdout and isinstance(stderr, str) and stderr:
            return _apply_env_redaction(stderr) != stderr
    return False


def _emit(tool_response: Any, output_text: str, *, compressed: bool) -> NoReturn:
    """Replace the model-visible tool output with *output_text* and exit 0.

    The single writer of the PostToolUse ``updatedToolOutput`` contract, used by
    the compression path (``compressed=True``) AND the redaction-only paths
    (``compressed=False`` — a scrubbed-but-not-shrunk output). Serialization
    failure falls back to passthrough (fail-open). ``compressed`` selects the
    observability outcome bucket so counters stay honest: a redaction-only emit
    is NOT a compression.

    Shape contract (anthropics/claude-code#68951): Claude Code >= 2.1.163
    validates ``updatedToolOutput`` against the originating tool's output schema
    and silently keeps the ORIGINAL output when it does not parse (2.1.212 logs
    ``PostToolUse hook returned updatedToolOutput that does not match <tool>'s
    output shape`` and falls back). A bare string therefore never replaced a Bash
    ``{stdout, stderr, interrupted, ...}`` object — the reason compression did not
    reach the model. ``_reinject`` mirrors the incoming ``tool_response`` shape
    around *output_text* so the emitted value passes that validation by
    construction; if the shape cannot be mirrored the hook passes through rather
    than emit a value the host would reject (fail-open)."""
    updated = _reinject(tool_response, output_text)
    if updated is None:
        _passthrough("reinject-unmatched")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": updated,
        }
    }
    try:
        sys.stdout.write(json.dumps(output))
    except Exception:
        _passthrough("serialize-failed")
    if compressed:
        _record_compression()
    else:
        _record_noop("redaction-only")
    sys.exit(0)


def main() -> None:
    # --- read + parse stdin (fail open on any problem) ---
    try:
        raw = sys.stdin.read()
    except Exception:
        _passthrough("stdin-read-failed")
    if not raw.strip():
        _passthrough("empty-stdin")
    try:
        payload = json.loads(raw)
    except Exception:
        _passthrough("bad-json")
    if not isinstance(payload, dict):
        _passthrough("non-dict-payload")

    # --- per-project CCR isolation (audit #4) ---
    # Scope the durable store to THIS project so the shared ~/.furl DB cannot
    # commingle originals across projects or evict cross-project. Prefer
    # CLAUDE_PROJECT_DIR (Claude Code's project root) so this hook and the
    # long-lived furl MCP server converge on ONE per-project store; the stdin
    # ``cwd`` then os.getcwd() are fallbacks. ``setdefault`` keeps a user's
    # shared-store override (FURL_CCR_NAMESPACE) or legacy-global opt-out
    # (FURL_CCR_PROJECT_DIR="") intact.
    _cwd = payload.get("cwd")
    os.environ.setdefault(
        "FURL_CCR_PROJECT_DIR",
        os.environ.get("CLAUDE_PROJECT_DIR")
        or (_cwd if isinstance(_cwd, str) and _cwd.strip() else "")
        or os.getcwd(),
    )

    # --- T7: cheap (no subprocess) version floor check ---
    # Resolved once, early, and reused below both for the first-run note's
    # wording and for the post-kill-switch short-circuit. ``_floor_met`` is
    # False only when furl_ctx.host_version has POSITIVELY IDENTIFIED a host
    # below MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT (from the native
    # installer's env vars — no subprocess spawn, safe on this per-tool-call
    # path); None means "cannot prove either way" (non-native install, or
    # furl_ctx unavailable) and is intentionally treated as "assume today's
    # behavior", never as "assume broken" — see host_version's module docstring.
    _hv = _host_version_module()
    _host_version = _hv.detect_host_version() if _hv is not None else None
    _floor_met = _hv.meets_compression_floor(_host_version) if _hv is not None else None

    # --- observability: record THIS invocation (every run past payload parse) ---
    # Resolved once here (after the project dir is scoped, so it hits the SAME
    # per-project store the MCP server reads) and stashed for the terminal
    # _passthrough / _emit, which tally the run's single outcome. On the FIRST
    # durably-recorded invocation, a one-line #68951 heads-up is written to stderr
    # (once per store, not per call — the in-memory backend never triggers it, so
    # a no-op stays byte-silent). All fail-open: counting never breaks the hook.
    global _run_counter_ctx
    _cmod = _counters_module()
    _cstore = _cmod.resolve_store() if _cmod is not None else None
    _run_counter_ctx = (_cmod, _cstore)
    if _cmod is not None and _cstore is not None:
        _below_floor_note = (
            _cmod.first_run_note_below_version_floor(_host_version) if _floor_met is False else None
        )
        _cmod.emit_first_run_note_if_first(
            _cstore,
            _cmod.bump(_cstore, _cmod.HOOK_INVOCATIONS),
            below_version_floor_note=_below_floor_note,
        )

    # --- kill switch ---
    if not _flag_enabled(os.environ.get(_ENABLED_ENV)):
        _passthrough("disabled")

    # --- T7: below the compression floor, Claude Code is CONFIRMED to ignore
    # updatedToolOutput (the anthropics/claude-code#68951 class), so whatever
    # this hook produced would never reach the model — including the
    # redaction-only emit further down, which is equally inert on these hosts.
    # Short-circuit before any of that work (compression, redaction) and
    # bucket the no-op distinctly so hook_compressions_applied never counts an
    # undeliverable replacement as "applied". Unknown (_floor_met is None)
    # intentionally falls through unchanged.
    if _floor_met is False:
        _passthrough("below-version-floor")

    # --- loop guard + operator exclusions (FURL_HOOK_EXCLUDE_TOOLS) ---
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and _excluded(tool_name):
        _passthrough("excluded-tool")

    # --- extract text; bail on non-text / empty / unrecognized payloads ---
    tool_response = payload.get("tool_response")
    text = _extract_text(tool_response)
    if text is None:
        _passthrough("shape-unmatched")

    # --- loop guard: already carries CCR markers -> already compressed ---
    if _CCR_MARKER in text:
        _passthrough("already-compressed")

    # --- secret redaction (FURL_REDACT_PATTERNS), BEFORE the size gate ---
    # Scrub configured secrets from what the model sees even when the output is
    # too small to compress. With no patterns set this returns text unchanged,
    # so the hook stays byte-identical when redaction is off.
    redacted = _apply_env_redaction(text)

    # --- size gate ---
    if len(redacted) < _min_chars():
        # Below the compression threshold. If redaction changed ANY model-visible
        # field — the extracted one OR a preserved Bash stderr — emit the fully
        # scrubbed mirror so no secret reaches the model (review Finding 1);
        # otherwise keep a byte-identical passthrough.
        if _redaction_changed_visible_output(tool_response, text, redacted):
            _emit(tool_response, redacted, compressed=False)
        _passthrough("below-min-chars")

    # --- compress (returns None + a distinct reason unless it genuinely helped) ---
    # The originating tool (from the payload, already read above) rides through
    # compress() so the CCR entry it stores records content_kind (audit: hook
    # entries previously all showed content_kind=null).
    tool_label = tool_name if isinstance(tool_name, str) and tool_name else None
    compressed, compress_fail_reason = _compress_text(redacted, tool_label)
    if compressed is None:
        # Compression did not help. Emit the fully scrubbed mirror if redaction
        # changed any model-visible field (extracted OR a preserved Bash stderr —
        # review Finding 1); otherwise leave the original untouched (byte-identical
        # passthrough).
        if _redaction_changed_visible_output(tool_response, text, redacted):
            _emit(tool_response, redacted, compressed=False)
        _passthrough(compress_fail_reason or "no-savings")

    # --- optional one-line stderr annotation (FURL_HOOK_VERBOSE) ---
    if _flag_enabled(os.environ.get(_VERBOSE_ENV)):
        saved_pct = round((1 - len(compressed) / len(redacted)) * 100)
        sys.stderr.write(
            f"furl: {tool_name or '?'} "
            f"{len(redacted) / 1024:.1f} KB -> {len(compressed) / 1024:.1f} KB  -{saved_pct}%\n"
        )

    # --- replace the tool output the model sees ---
    _emit(tool_response, compressed, compressed=True)


if __name__ == "__main__":
    # Absolute last-resort guard: no uncaught exception may ever reach the host.
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
