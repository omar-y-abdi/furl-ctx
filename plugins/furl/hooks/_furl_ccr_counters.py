"""Shared CCR observability counters for Furl's hooks.

Both the PostToolUse compression hook and the opt-in PreToolUse pipe compressor
tally their activity into the SAME durable per-project CCR store the MCP server
reads, so ``furl_stats`` surfaces cross-process hook activity even though each
hook is a short-lived subprocess. Counters are cumulative and monotonic (they
survive entry eviction/expiry), so the key diagnostic stays legible:

    invocations rising while your context still shows raw tool output
    → Claude Code is dropping the hook's replacement output
      (anthropics/claude-code#68951)

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
FIRST_RUN_NOTE = (
    "furl: note — Claude Code >=2.1.163 may ignore replacement output "
    "(anthropics/claude-code#68951); savings apply when fixed; see furl_stats counters"
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


def emit_first_run_note_if_first(store: Any | None, new_count: int | None) -> None:
    """Write ``FIRST_RUN_NOTE`` to stderr exactly once per durable store.

    Fires only when the invocation counter DURABLY became ``1`` — i.e. the store
    persists counters cross-process (``counters_durable``) and this run recorded
    the first invocation. The in-memory backend (library / unit tests) reports
    ``counters_durable`` False, so the note never fires there and the hook stays
    byte-silent on a no-op. Never raises.
    """
    try:
        if new_count == 1 and store is not None and getattr(store, "counters_durable", False):
            sys.stderr.write(FIRST_RUN_NOTE + "\n")
    except Exception:
        pass
