"""Shared CCR observability counters for Furl's hooks.

Both the PostToolUse compression hook and the on-by-default PreToolUse pipe compressor
tally their activity into the SAME durable per-project CCR store the MCP server
reads, so ``furl_stats`` surfaces cross-process hook activity even though each
hook is a short-lived subprocess. Counters are cumulative and monotonic (they
survive entry eviction/expiry), so the key diagnostic stays legible. The
PostToolUse hook now MIRRORS each tool's output shape (the #68951 fix), so
current hosts honor the replacement — these counters exist to catch any FUTURE
regression:

    invocations climbing while compressions stay flat AND your context still
    shows raw tool output
    → a mirrored replacement is being dropped again (an output-shape
      regression of the anthropics/claude-code#68951 class)

Everything here is BEST-EFFORT and FAIL-OPEN: a counter problem (furl_ctx
unavailable, store degraded, sqlite lock lost) is a silent no-op and never
affects the hook's stdout/exit or the tool call. The counter names are read back
by ``furl_ctx.ccr.mcp_server`` for the ``furl_stats`` "store" block.
"""

from __future__ import annotations

import sys
from typing import Any

# PostToolUse compression hook (compress_tool_output.py) — the #68951 diagnostic.
HOOK_INVOCATIONS = "hook_invocations_seen"
HOOK_COMPRESSIONS = "hook_compressions_applied"
HOOK_NOOP_PREFIX = "hook_noop:"

# Opt-in PreToolUse pipe compressor (pipe_compress.py) — its own tally, kept
# separate so it never muddies the PostToolUse #68951 signal above.
PIPE_INVOCATIONS = "pipe_invocations_seen"
PIPE_COMPRESSIONS = "pipe_compressions_applied"
PIPE_NOOP_PREFIX = "pipe_noop:"

# One-line heads-up written to stderr (user-visible; never reaches the model) on
# the FIRST durably-recorded PostToolUse invocation per project store. Gated so
# it fires once, not on every tool call — see ``emit_first_run_note_if_first``.
# Printed when the running Claude Code MEETS the compression floor, or when its
# version could not be determined (unknown is never treated as "assume
# broken" — see furl_ctx.host_version's module docstring).
FIRST_RUN_NOTE = (
    "furl: PostToolUse output compression is active and mirrors each tool's "
    "output shape, so Claude Code 2.1.163 and newer honor the replacement for "
    "the shapes furl mirrors, reflecting the anthropics/claude-code#68951 fix; "
    "the counter hook_compressions_applied counts replacements produced, not "
    "proven delivered, so watch furl_stats for invocations climbing while "
    "compressions stay flat; the PreToolUse pipe also runs by default, set "
    "FURL_PRETOOL_PIPE=0 to disable"
)


def first_run_note_below_version_floor(host_version: tuple[int, int, int] | None) -> str:
    """T7: the below-floor variant of ``FIRST_RUN_NOTE``, printed instead when
    the caller has CONFIRMED the running Claude Code is below
    ``furl_ctx.host_version.MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT``.

    PostToolUse compression cannot reach the model on a host this old no matter
    what furl produces — Claude Code silently keeps the original tool output
    (the anthropics/claude-code#68951 class) — so this also explains why
    ``hook_compressions_applied`` stops incrementing (``compress_tool_output.py``
    buckets the no-op as ``hook_noop:below-version-floor`` instead of counting
    an undeliverable replacement as "applied"). *host_version* renders as
    "unknown" if ``None`` is passed, though callers should not reach this
    function at all for the unknown case — see ``FIRST_RUN_NOTE``'s docstring."""
    from furl_ctx.host_version import MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT, format_version

    floor = format_version(MIN_VERSION_FOR_POST_TOOL_USE_REPLACEMENT)
    current = format_version(host_version) if host_version is not None else "unknown"
    return (
        f"furl: PostToolUse output compression requires Claude Code {floor} or "
        f"newer to reach the model, current host version is {current}, so it "
        "is disabled this session; hook_compressions_applied will not "
        "increment below the floor, see hook_noop:below-version-floor in "
        "furl_stats instead; the PreToolUse pipe is unaffected and still runs "
        "by default, set FURL_PRETOOL_PIPE=0 to disable"
    )


def resolve_store() -> Any | None:
    """Return the per-project CCR store this hook shares with the MCP server.

    Prefers the active namespace store (the plugin exports FURL_CCR_PROJECT_DIR,
    so an un-namespaced call resolves the per-project sqlite file the MCP server
    also reads); falls back to the global singleton. FAIL-OPEN: returns ``None``
    if furl_ctx is not importable (the same degraded env where compression itself
    no-ops), so counters simply do not record.
    """
    try:
        from furl_ctx.cache.compression_store import (
            get_compression_store,
            resolve_ccr_namespace_store,
        )

        return resolve_ccr_namespace_store() or get_compression_store()
    except Exception:
        return None


def bump(store: Any | None, name: str, amount: int = 1) -> int | None:
    """Increment ``name`` on ``store``; return its new durable value or ``None``.

    ``None`` covers every non-count outcome: no store, unsupported backend, a
    volatile fallback write, or an error. Never raises.
    """
    if store is None or not name:
        return None
    try:
        return store.increment_counter(name, amount)
    except Exception:
        return None


def emit_first_run_note_if_first(
    store: Any | None,
    new_count: int | None,
    *,
    below_version_floor_note: str | None = None,
) -> None:
    """Write the first-run note to stderr exactly once per durable store.

    Fires only when the invocation counter DURABLY became ``1`` — i.e. the store
    persists counters cross-process (``counters_durable``) and this run recorded
    the first invocation. The in-memory backend (library / unit tests) reports
    ``counters_durable`` False, so the note never fires there and the hook stays
    byte-silent on a no-op. Never raises.

    *below_version_floor_note* (T7): when given, printed INSTEAD of
    ``FIRST_RUN_NOTE`` — pass the caller's already-computed
    ``first_run_note_below_version_floor(...)`` result once it has CONFIRMED the
    running host is below the compression floor. ``None`` (the default)
    preserves ``FIRST_RUN_NOTE``, used both when the floor is met AND when the
    version could not be determined (see that constant's docstring).
    """
    try:
        if new_count == 1 and store is not None and getattr(store, "counters_durable", False):
            sys.stderr.write((below_version_floor_note or FIRST_RUN_NOTE) + "\n")
    except Exception:
        pass
