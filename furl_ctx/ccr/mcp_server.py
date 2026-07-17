"""Furl MCP Server — Context engineering toolkit for AI coding tools.

Exposes Furl's compression, retrieval, and observability as MCP tools
that any MCP-compatible host (Claude Code, Cursor, Codex, etc.) can use.

Tools:
    furl_compress   — Compress content on demand
    furl_retrieve   — Retrieve original uncompressed content by hash
    furl_stats      — Session compression statistics
    furl_purge      — Erase stored originals (one hash, or all)
    furl_search     — Find stored originals by content substring
    furl_list       — List stored entries (newest first)

Usage:
    # As a standalone server (stdio transport, spawned by AI coding tools)
    python -m furl_ctx.ccr.mcp_server

    # Register with an MCP host, e.g. Claude Code:
    #   claude mcp add furl -- python -m furl_ctx.ccr.mcp_server
    # (there is no ``furl`` console script — the module IS the entry point)

Compression and retrieval happen locally in this process via the shared
CCR compression store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from furl_ctx import paths as _paths
from furl_ctx._version import get_version
from furl_ctx.ccr.compress_modes import (
    CompressionMode,
    SectionPatterns,
    build_mode_pipeline,
    partition_content,
)
from furl_ctx.ccr.marker_grammar import CCR_TOOL_NAME, is_valid_ccr_hash
from furl_ctx.ccr.retrieve_filters import (
    FilterError,
    RetrieveFilters,
    apply_filters,
)

# fcntl is Unix-only; on Windows we skip file locking (stats are best-effort).
# Keep the module typed as Any so Windows mypy runs don't try to resolve Unix-only attrs.
fcntl: Any = None
try:
    import fcntl as _fcntl

    fcntl = _fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

# Try to import MCP SDK
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    Server = None  # type: ignore[assignment,misc]
    stdio_server = None  # type: ignore[assignment]

COMPRESS_TOOL_NAME = "furl_compress"
STATS_TOOL_NAME = "furl_stats"
READ_TOOL_NAME = "furl_read"
PURGE_TOOL_NAME = "furl_purge"
SEARCH_TOOL_NAME = "furl_search"
LIST_TOOL_NAME = "furl_list"

# Model this server compresses with — and therefore counts tokens with:
# _handle_read's original_tokens must come from the same tokenizer
# compress() uses (COR-36: a whitespace word count fed as a token count
# understates content by roughly the words-per-token factor).
_MCP_TOKEN_MODEL = "claude-sonnet-4-5-20250929"

# content_kind label for entries the MCP furl_compress tool stores, so
# furl_list / furl_retrieve show where an entry came from (distinct from the
# hook's tool names like "Bash" and furl_read's "furl_read").
_MCP_COMPRESS_TOOL_NAME = "mcp:furl_compress"

logger = logging.getLogger("furl_ctx.ccr.mcp")


def _safe_decode_for_logging(raw: bytes) -> str:
    """Decode bytes to a string for tool-output display.

    Uses an incremental UTF-8 decoder with the replacement character (U+FFFD)
    for invalid bytes — acceptable here because this path is for tool output
    display, not the SSE/wire path (lossy decode kwargs are forbidden
    in furl_ctx/ccr/, so this centralizes the single legitimate lossy use).
    """
    import codecs as _codecs

    decoder = _codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(bytes(raw), final=True)


# Maximum bytes/chars a single tool call will read or ingest. Caps the
# furl_read file read and the furl_compress content input so a single
# oversized payload cannot exhaust memory (OOM DoS). 10 MiB is far above any
# realistic source file or tool output while bounding worst-case allocation.
_MAX_READ_BYTES = 10 * 1024 * 1024


def _describe_arguments_for_log(arguments: dict[str, Any]) -> str:
    """Non-sensitive descriptor of a tool-call ``arguments`` dict for logging.

    Truncating a payload still leaks its leading bytes (file contents, queries,
    retrieved originals). Emit only the *shape* — each key with the length of a
    string value (or the type for non-strings) — never the values themselves.
    Used at DEBUG; safe to enable at any level because no payload is included.
    """
    parts: list[str] = []
    for key in sorted(arguments):
        value = arguments[key]
        if isinstance(value, str):
            parts.append(f"{key}:len={len(value)}")
        else:
            parts.append(f"{key}:{type(value).__name__}")
    return "{" + ", ".join(parts) + "}"


def _result_chars_for_log(result: list[Any]) -> int:
    """Total character count of a handler's TextContent result, for logging.

    A byte/char count is operationally useful (outcome size) without exposing
    the payload — which may carry retrieved original content or file bodies.
    """
    return sum(len(getattr(item, "text", str(item))) for item in result)


def _err(message: str) -> list[TextContent]:
    """A model-visible parameter-error envelope (API-15): compact, no indent.

    Parameter mistakes are the model's to fix, so they ship as success-shaped
    JSON — distinct from internal failures, which are re-raised so the SDK
    marks ``isError=True``.
    """
    return [TextContent(type="text", text=json.dumps({"error": message}))]


def _workspace_root() -> Path:
    """Return the resolved root that furl_read file access is confined to.

    Resolution order (mirrors ``furl_ctx.paths._env`` trim semantics):

    1. ``$FURL_WORKSPACE_DIR`` (trimmed, tilde-expanded) when set to a
       non-blank value.
    2. The current working directory otherwise.

    The result is ``resolve()``-d so the jail check compares two canonical
    (symlink-collapsed) paths.

    Floor (defense-in-depth): an env value that is a bare ``~`` (no path after
    the home marker) or that resolves to the filesystem root ``/`` would widen
    the jail to the entire home/disk, defeating the confinement. Such a value is
    rejected and the jail falls back to the current working directory (logged at
    WARNING so the misconfiguration is visible). The empty/blank case already
    falls through to cwd via the ``if env_value`` guard.
    """
    env_value = os.environ.get(_paths.FURL_WORKSPACE_DIR_ENV, "").strip()
    if env_value and env_value != "~":
        candidate = Path(env_value).expanduser().resolve()
        if candidate != Path("/"):
            return candidate
        logger.warning(
            "event=mcp_workspace_root_rejected reason=resolves_to_filesystem_root "
            "falling_back_to=cwd"
        )
    elif env_value == "~":
        logger.warning(
            "event=mcp_workspace_root_rejected reason=bare_home_marker falling_back_to=cwd"
        )
    return Path.cwd().resolve()


# The component-by-component jail walk needs openat()-style dir_fd opens plus
# O_DIRECTORY/O_NOFOLLOW — present on macOS and Linux, absent on Windows.
_DIR_FD_WALK_SUPPORTED = (
    os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
)


def _open_jailed(path: Path, root: Path) -> int:
    """Open ``path`` read-only with every component pinned under ``root``.

    ``path`` must already be confirmed ``is_relative_to(root)`` by the caller
    (both sides ``resolve()``-d). This function closes the SEC-5 residual in
    that check: ``O_NOFOLLOW`` on a single ``os.open(path)`` guards only the
    FINAL component, so a *directory* component swapped for a symlink between
    the caller's ``resolve()`` and the open was silently followed out of the
    jail (TOCTOU).

    POSIX (macOS/Linux): walk from the workspace root one component at a
    time, each step an ``os.open(part, dir_fd=parent_fd)`` with ``O_NOFOLLOW``
    (plus ``O_DIRECTORY`` for intermediates) — a component swapped to a
    symlink fails its own open (ENOTDIR/ELOOP) instead of being followed, and
    each step resolves relative to the already-pinned parent fd, never by
    re-walking the full path. The returned fd is the pinned descriptor the
    caller fstat()s and reads: no second path lookup anywhere.

    Windows (no ``dir_fd`` support): falls back to one direct open of the
    ``resolve()``-d path. Residual threat model, documented and accepted: on
    that platform an intermediate component swapped for a link between
    ``resolve()`` and open is followed; the jail there rests on the
    resolve()+is_relative_to pre-check (and furl_read ships off-by-default).

    Raises ``FileNotFoundError``/``OSError`` exactly like ``os.open``; callers
    map them to the "File not found" / generic "Cannot read file" envelopes.
    """
    if not _DIR_FD_WALK_SUPPORTED:
        return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))

    rel_parts = path.relative_to(root).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel_parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not rel_parts:
            # path == root: hand back the root fd; the caller's S_ISREG gate
            # turns a directory into the "Not a file" envelope.
            return fd
        final_fd = os.open(rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return final_fd


# Feature flag: enable furl_read tool (file read caching via CCR)
# Set FURL_MCP_READ=on to enable
_READ_ENABLED = os.environ.get("FURL_MCP_READ", "off").lower().strip() in (
    "on",
    "true",
    "1",
    "yes",
    "enabled",
)

# ─── Server-level instructions: CSV decode legend (engine P1-10) ───────────
#
# The compacted-table decode legend ships ONCE per conversation via the MCP
# SDK's server-level ``instructions=`` parameter (surfaced to the host in
# the initialize response) instead of per-table in-band bytes: zero
# wire-byte change to any table render — the grammar characterization
# suites stay untouched — at an amortized conversation-scope cost of ~250
# o200k tokens. This is owner-approved alternative (b) of
# QUESTIONS-FOR-USER.md item 15; the original once-per-conversation carrier
# (``CCRToolInjector.inject_system_instructions``) was excised in SIMP-4,
# leaving server-level instructions as the out-of-band channel.
#
# Agent-first ordering (a fresh agent must decode this cold): a 3-part
# plain-English summary (what the table format IS, what a ``<<ccr:HASH>>``
# marker means, the retrieve-before-reasoning rule) → ONE worked micro-example
# (input line → compact row → read-back) → THEN the compact grammar reference.
#
# Every decode claim below is grammar-verified against the reference
# decoder ``furl_ctx/transforms/csv_schema_decoder.py`` (the documented
# consumer contract for the Rust ``formatter.rs`` output); the executable
# pins live in ``tests/test_mcp_server_instructions.py``. The highest
# comprehension risk is ``%k``: a cell ``53`` under ``time_ms:float%3``
# decodes to 0.053, NOT 53 — the worked example leads with exactly that.
CSV_DECODE_LEGEND = (
    # 1) Plain-English summary: which INPUT produces the table, what a marker
    #    means, and the retrieve-before-reasoning rule. The columnar table is
    #    for STRUCTURED JSON ARRAYS; line-oriented text (the common case) ships
    #    head+tail with a marker, NOT tabled — so an agent reasoning from this
    #    grammar never assumes a log was tabled when it was actually offloaded.
    "Furl tables a structured JSON array of objects — read one before you reason. "
    "Header `[N]{col:type,...}` = N rows; later lines give each row's non-constant columns as CSV. "
    "Line-oriented text (logs, traces) is NOT tabled — it ships head+tail + a `<<ccr:HASH>>` marker: "
    "content offloaded, not lost; furl_retrieve it first — never guess dropped data. "
    # 2) One worked micro-example — a JSON-array ROW (not a raw log line):
    #    input cell → decoded row → read it back.
    "Example: array row `[1]{lvl:string=INFO,ms:float%3}` + line `53` = {lvl:INFO, ms:0.053} — "
    "constant col re-attaches, float%3 = ×10^-3 (53 -> 0.053, not 53). "
    # 3) Compact grammar reference (the table form only).
    "GRAMMAR: type=V constant V; int=B+S row i=B+S*i (0-based); float%k cell/10^k; "
    "string~ ISO ts then ±sec[/tz] deltas; string^ +__affix:col=P,S value=P+cell+S; "
    "string@ +__head:col=<d>h0,h1 cell 1<d>tail=h1+tail; __dict:col=v0,v1 cell indexes it; "
    "= repeats cell above; __null__ null, __missing__ absent key, ? nullable."
)


def _legend_enabled() -> bool:
    """``FURL_MCP_LEGEND`` flag — default ON (the owner wants the legend).

    Re-read from the environment per call (the SEC-7 no-import-freeze
    discipline: paths.py's contract is "every call re-reads the
    environment", and the flag is consumed at server CONSTRUCTION time,
    so tests and host re-configuration see the live value). Recognized
    OFF spellings disable; anything else — including the default — keeps
    the legend on.
    """
    return os.environ.get("FURL_MCP_LEGEND", "on").lower().strip() not in (
        "off",
        "false",
        "0",
        "no",
        "disabled",
    )


# Session-scoped TTL fallback: content persists for the session (1 hour),
# outlasting the library's own 30-minute default. The MCP server process
# lives as long as the coding session. Fallback ONLY — an operator-set
# FURL_CCR_TTL_SECONDS overrides it (see _mcp_session_ttl below): the plugin
# ships FURL_CCR_TTL_SECONDS=86400 via .mcp.json, and before the env was
# honored here, MCP-tool-stored entries silently expired at 1 h while
# hook-compressed entries in the very same per-project store lived 24 h.
MCP_SESSION_TTL = 3600


def _mcp_session_ttl() -> int:
    """Effective TTL (seconds) for entries this server stores — env-aware.

    ``FURL_CCR_TTL_SECONDS`` — the same knob the library store and the
    plugin's .mcp.json use — wins when set to a valid positive integer, so
    the plugin's 24 h retention governs MCP-tool writes exactly as it
    governs the hook's. Unset/blank keeps the bare-server default
    ``MCP_SESSION_TTL`` byte-identical to before env support existed.
    Invalid values (non-integer, <= 0) log one WARNING and fall back to
    ``MCP_SESSION_TTL`` — a bad env var must never crash or silently
    reconfigure the server. Re-read from the environment per call (the
    SEC-7 no-import-freeze discipline, same as ``_legend_enabled``).
    """
    raw_value = os.environ.get("FURL_CCR_TTL_SECONDS")
    if raw_value is None or not raw_value.strip():
        return MCP_SESSION_TTL
    try:
        ttl_seconds = int(raw_value)
    except ValueError:
        logger.warning(
            "FURL_CCR_TTL_SECONDS must be a positive integer number of seconds, got %r; "
            "MCP-stored entries fall back to %s s (the library store resolver falls back "
            "to its own 1800 s default separately, so hook/dropped-row entries diverge)",
            raw_value,
            MCP_SESSION_TTL,
        )
        return MCP_SESSION_TTL
    if ttl_seconds <= 0:
        logger.warning(
            "FURL_CCR_TTL_SECONDS must be greater than 0, got %s; MCP-stored entries fall "
            "back to %s s (the library store resolver falls back to its own 1800 s default "
            "separately, so hook/dropped-row entries diverge)",
            ttl_seconds,
            MCP_SESSION_TTL,
        )
        return MCP_SESSION_TTL
    return ttl_seconds


def _default_store_backend_factory() -> Any:
    """Build the durable SQLite CCR backend (separate seam so tests can
    inject a construction failure without touching the sqlite module)."""
    from furl_ctx.cache.backends.sqlite import SqliteBackend

    return SqliteBackend()


def _default_store_backend() -> Any:
    """Resolve the MCP server's default store backend (Engine P1-7).

    The MCP deployment defaults to the durable SQLite backend: the MCP server
    is the process that restarts mid-session (restart used to destroy every
    retrievable original) and that runs one instance per sub-agent (a
    per-process in-memory store meant sub-agents could never resolve
    main-agent hashes). The library default (plain ``compress()``) stays
    in-memory — durability is a deployment property of THIS server.

    Returns ``None`` when ``FURL_CCR_BACKEND`` is set to anything (including
    ``memory``, the documented opt-out, or ``sqlite``/a plugin name), deferring
    to ``get_compression_store``'s env-selected loader. Never raises: a
    backend construction failure logs one ERROR and falls back to the
    in-memory default rather than blocking the host.
    """
    if (os.environ.get("FURL_CCR_BACKEND") or "").strip():
        return None
    try:
        return _default_store_backend_factory()
    except Exception:
        logger.error(
            "event=mcp_ccr_backend_init_failed backend=sqlite falling_back_to=memory",
            exc_info=True,
        )
        return None


# Shared stats file: all MCP instances (main + sub-agents) append here.
# furl_stats aggregates across all instances within the session window.
# Respects FURL_WORKSPACE_DIR.
#
# Functions, not import-frozen constants (SEC-7): paths.py's contract is
# explicit — "No caching. Every call re-reads the environment" — and the
# furl_read jail already re-reads FURL_WORKSPACE_DIR per call. Freezing these
# two at import let the stats file and the jail disagree about the workspace
# when the env changes after import (tests, host re-configuration). Each use
# site calls these; within one operation the value is read once and kept in a
# local so a single append/read never straddles two workspaces.
SESSION_WINDOW_SECONDS = 7200  # 2 hours — events older than this are pruned


def shared_stats_dir() -> Path:
    """Workspace directory holding the shared stats file (re-read per call)."""
    return _paths.workspace_dir()


def shared_stats_file() -> Path:
    """Path of the cross-process session stats JSONL file (re-read per call)."""
    return _paths.session_stats_path()


def _append_shared_event(event: dict[str, Any]) -> None:
    """Append an event to the shared stats file (cross-process, file-locked)."""
    try:
        shared_stats_dir().mkdir(parents=True, exist_ok=True)
        event["pid"] = os.getpid()
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with open(shared_stats_file(), "a") as f:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            # Flush BEFORE releasing the lock: an append still sitting in the
            # userspace buffer when the lock drops can interleave with the
            # prune-rewrite in _read_shared_events (SEC-6 protocol soundness).
            f.flush()
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass  # Never break compression because of stats


def _read_shared_events(window_seconds: int = SESSION_WINDOW_SECONDS) -> list[dict[str, Any]]:
    """Read shared events within the session time window, pruning old entries.

    Read and prune-rewrite happen on ONE ``r+`` handle under ONE exclusive
    lock (SEC-6). The old flow read under ``LOCK_SH``, released, then rewrote
    under a separate ``open(..., "w")`` + ``LOCK_EX`` — an event appended by
    another MCP process between the two locks was silently destroyed by the
    rewrite, and the ``"w"`` open truncated the file BEFORE its lock was even
    acquired, so the rewrite wasn't atomic even against a correctly-locked
    appender.
    """
    stats_file = shared_stats_file()
    if not stats_file.exists():
        return []
    cutoff = time.time() - window_seconds
    events: list[dict[str, Any]] = []
    keep_lines: list[str] = []
    try:
        with open(stats_file, "r+") as f:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("timestamp", 0) >= cutoff:
                        events.append(evt)
                        keep_lines.append(line + "\n")
                # Prune old entries (only if we dropped some) — same handle,
                # same lock, so no appender can slip between read and rewrite.
                if len(keep_lines) < len(lines):
                    try:
                        f.seek(0)
                        f.truncate()
                        f.writelines(keep_lines)
                        # Land the rewrite before the lock releases (an
                        # unflushed rewrite could interleave with the next
                        # locked appender).
                        f.flush()
                    except Exception:
                        logger.debug("Shared-stats prune failed (non-fatal)", exc_info=True)
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        logger.debug("Shared-stats read failed (non-fatal)", exc_info=True)
    return events


_DEFAULT_COST_RATE_USD_PER_MTOK = 3.0


def _cost_rate_per_mtok() -> float:
    """Blended $/1M-token rate for the savings estimate. FURL_COST_RATE_USD_PER_MTOK
    overrides the ~$3 default; an invalid or negative value falls back to it."""
    raw = os.environ.get("FURL_COST_RATE_USD_PER_MTOK", "").strip()
    if not raw:
        return _DEFAULT_COST_RATE_USD_PER_MTOK
    try:
        rate = float(raw)
    except ValueError:
        return _DEFAULT_COST_RATE_USD_PER_MTOK
    return rate if rate >= 0 else _DEFAULT_COST_RATE_USD_PER_MTOK


@dataclass
class SessionStats:
    """Track compression statistics for the current MCP session.

    ``cache_hits``/``cache_tokens_avoided`` are deliberately SEPARATE from the
    compression totals (COR-36): a furl_read cache hit avoids re-emitting a
    file body but compresses nothing, so booking it as a compression inflated
    ``total_input_tokens``/``total_tokens_saved``/``savings_percent`` with
    fictional work.
    """

    compressions: int = 0
    retrievals: int = 0
    cache_hits: int = 0
    cache_tokens_avoided: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens_saved: int = 0
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)

    def _log_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        _append_shared_event(event)
        # Keep last 50 events
        if len(self.events) > 50:
            self.events = self.events[-50:]

    def record_compression(
        self,
        input_tokens: int,
        output_tokens: int,
        strategy: str,
    ) -> None:
        self.compressions += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_tokens_saved += max(0, input_tokens - output_tokens)
        event = {
            "type": "compress",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "savings_percent": round((1 - output_tokens / input_tokens) * 100, 1)
            if input_tokens > 0
            else 0,
            "strategy": strategy,
            "timestamp": time.time(),
        }
        self._log_event(event)

    def record_retrieval(self, hash_key: str) -> None:
        self.retrievals += 1
        event = {
            "type": "retrieve",
            "hash": hash_key[:12],
            "timestamp": time.time(),
        }
        self._log_event(event)

    def record_cache_hit(self, tokens_avoided: int) -> None:
        """Record a furl_read cache hit WITHOUT touching compression totals.

        A hit avoids re-emitting the file body; it does not compress anything.
        The old behavior booked it via ``record_compression`` — inflating the
        compression totals with fictional savings (COR-36).
        """
        self.cache_hits += 1
        avoided = max(0, tokens_avoided)
        self.cache_tokens_avoided += avoided
        event = {
            "type": "read_cache_hit",
            "tokens_avoided": avoided,
            "timestamp": time.time(),
        }
        self._log_event(event)

    def to_dict(self) -> dict[str, Any]:
        savings_pct = (
            round((self.total_tokens_saved / self.total_input_tokens) * 100, 1)
            if self.total_input_tokens > 0
            else 0
        )
        # Rough cost estimate; FURL_COST_RATE_USD_PER_MTOK overrides the ~$3/1M default.
        cost_saved = round(self.total_tokens_saved * _cost_rate_per_mtok() / 1_000_000, 4)

        return {
            "session_duration_seconds": round(time.time() - self.started_at),
            "compressions": self.compressions,
            "retrievals": self.retrievals,
            "cache_hits": self.cache_hits,
            "cache_tokens_avoided": self.cache_tokens_avoided,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens_saved": self.total_tokens_saved,
            # Crit-2: this is VISIBLE-TOKEN reduction (context hidden from the
            # model), which INCLUDES CCR-offloaded content — retrievable by hash,
            # not lossless byte compression. Labeled explicitly so a high number
            # on high-entropy input is not read as lossless shrinkage.
            "visible_token_reduction_percent": savings_pct,
            # Deprecated alias kept for compatibility — same value.
            "savings_percent": savings_pct,
            "reduction_note": (
                "visible_token_reduction_percent is context reduction, meaning tokens "
                "hidden from the model, and it includes content offloaded to the CCR "
                "store and retrievable by hash; it is NOT lossless byte compression. "
                "estimated_cost_saved_usd is derived from it and is likewise a "
                "context-reduction estimate, not a guaranteed billing delta."
            ),
            "estimated_cost_saved_usd": cost_saved,
            "recent_events": self.events[-10:],
        }


# ─── furl_search / furl_list: shared paging + preview helpers ───────────────
#
# Both discovery tools bound their output (invariant D): a positive-int limit
# capped at ``_SEARCH_LIST_LIMIT_CAP``, previews trimmed to a fixed char budget,
# and previews redacted with the SAME rules the cross-store search uses so a
# match sitting next to a credential never surfaces it.
_SEARCH_LIST_LIMIT_DEFAULT = 20
_SEARCH_LIST_LIMIT_CAP = 100
_MATCH_PREVIEW_RADIUS = 60  # chars of context each side of a furl_search match
_MATCH_PREVIEW_MAX = 240  # hard char cap on a single furl_search preview
_LIST_PREVIEW_CHARS = 120  # leading chars of a furl_list entry preview
# ReDoS guard: the credential regexes are O(N^2) on long base64url/hex runs, so
# these previews redact only the kept window PLUS this margin (never the whole
# multi-MB original). The margin lets a secret straddling the preview cap be
# masked whole before truncation — GUARANTEED only for secrets <= this margin;
# a longer one (a ~1700-char PEM private key, a long JWT) whose anchor falls
# outside the widened window can still leak an unanchored tail fragment.
# (Mirrors compression_store's window guard; kept local to preserve
# mcp_server's lazy compression_store import boundary.)
_PREVIEW_REDACT_MARGIN = 256


def _parse_bounded_limit(raw: Any, default: int, cap: int) -> tuple[int | None, str | None]:
    """A search/list ``limit``: a positive int, silently capped at ``cap``.

    Returns ``(value, None)`` or ``(None, error)``. ``bool`` is excluded (True is
    not the int 1 here); a non-positive limit is a caller bug (a zero-row result
    is meaningless), rejected rather than clamped. A value past the cap is clamped
    DOWN to the cap (the documented ceiling) and the effective limit is echoed in
    the response, so the truncation is never silent."""
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, f"limit must be an integer, got {type(raw).__name__}"
    if raw < 1:
        return None, f"limit must be >= 1, got {raw}"
    return min(raw, cap), None


def _parse_offset(raw: Any) -> tuple[int | None, str | None]:
    """A furl_list ``offset``: a non-negative int (``None``→0). ``bool`` excluded."""
    if raw is None:
        return 0, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, f"offset must be an integer, got {type(raw).__name__}"
    if raw < 0:
        return None, f"offset must be >= 0, got {raw}"
    return raw, None


def _redact_preview_text(text: str) -> str:
    """Redact a preview snippet with the store's credential rules.

    Reuses ``_redact_retrieval_log_payload`` — the same primitive the cross-store
    search preview uses — so furl_search / furl_list never surface a secret a
    per-hash retrieval's log path would have masked. Imported lazily to keep the
    compression_store module off the mcp_server import path until first use."""
    from furl_ctx.cache.compression_store import _redact_retrieval_log_payload

    return _redact_retrieval_log_payload(text)


def _bounded(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars, marking truncation with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _humanize_ttl_remaining(seconds: float) -> str:
    """Humanize seconds-until-expiry as a short ``23h`` / ``45m`` / ``30s`` string.

    Rounds to whole seconds first (killing sub-second aging jitter, so a
    freshly stored 24h entry reads ``24h`` and not ``23h``), then floors to the
    largest whole unit — an "at least this long left" read. Non-positive input
    clamps to ``0s``: an entry at or past expiry is dropped from furl_list by
    ``_live_entries`` before this runs, so the clamp is only a defensive floor
    against clock skew."""
    s = max(0, round(seconds))
    if s >= 3600:
        return f"{s // 3600}h"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def _match_preview(original: str, needle_lower: str) -> str:
    """A short REDACTED window of ``original`` around its first case-insensitive
    match of ``needle_lower``.

    Slice-before-redact (ReDoS guard, see ``_PREVIEW_REDACT_MARGIN``): the
    credential regexes are O(N^2) on long base64url/hex runs, so the redactor
    must never see the whole multi-MB original. The match is located in the RAW
    text (a linear ``find``); the redactor then sees a MARGIN-widened window
    around it — the margin means a secret straddling either display edge is
    seen WHOLE and masked before any cut can leave a recognizable fragment,
    for secrets up to ``_PREVIEW_REDACT_MARGIN`` chars; a longer secret (PEM
    private key, long JWT) whose anchor sits outside the widened window can
    still leak an unanchored tail — the accepted, bounded residual
    (review F4: a bare radius window truncated a prefix-anchored key into an
    unmatchable — and therefore leaked — head or un-anchored tail). The needle
    is re-found inside the REDACTED text and the display window is cut around
    that; if the match fell inside a now-masked credential it is absent from
    the redacted text — fall back to a redacted head, so a match inside a
    secret reveals neither its bytes nor its location. Bounded to
    ``_MATCH_PREVIEW_MAX`` chars."""
    raw_idx = original.lower().find(needle_lower)
    if raw_idx < 0:
        # Defensive: callers only pass a needle already found in the original,
        # but stay total — redact a bounded head.
        return _bounded(
            _redact_preview_text(original[: _MATCH_PREVIEW_MAX + _PREVIEW_REDACT_MARGIN]),
            _MATCH_PREVIEW_MAX,
        )
    wstart = max(0, raw_idx - _MATCH_PREVIEW_RADIUS - _PREVIEW_REDACT_MARGIN)
    wend = min(
        len(original),
        raw_idx + len(needle_lower) + _MATCH_PREVIEW_RADIUS + _PREVIEW_REDACT_MARGIN,
    )
    redacted = _redact_preview_text(original[wstart:wend])
    ridx = redacted.lower().find(needle_lower)
    if ridx < 0:
        # The match sat inside a credential now masked to [REDACTED]; windowing
        # around it would reveal its location — fall back to a redacted head.
        return _bounded(
            _redact_preview_text(original[: _MATCH_PREVIEW_MAX + _PREVIEW_REDACT_MARGIN]),
            _MATCH_PREVIEW_MAX,
        )
    start = max(0, ridx - _MATCH_PREVIEW_RADIUS)
    end = min(len(redacted), ridx + len(needle_lower) + _MATCH_PREVIEW_RADIUS)
    prefix = "…" if (wstart > 0 or start > 0) else ""
    suffix = "…" if (wend < len(original) or end < len(redacted)) else ""
    return _bounded(f"{prefix}{redacted[start:end]}{suffix}", _MATCH_PREVIEW_MAX)


def _list_preview(original: str) -> str:
    """A redacted leading-``_LIST_PREVIEW_CHARS`` preview of a stored original.

    Slice-before-redact (ReDoS guard, see ``_PREVIEW_REDACT_MARGIN``): redact
    only the bounded head the preview can show (+ margin so a secret straddling
    the cap is masked whole before truncation), never the full multi-MB
    original — the credential regexes are O(N^2) on long base64url/hex runs."""
    return _bounded(
        _redact_preview_text(original[: _LIST_PREVIEW_CHARS + _PREVIEW_REDACT_MARGIN]),
        _LIST_PREVIEW_CHARS,
    )


class FurlMCPServer:
    """MCP Server exposing Furl's context engineering toolkit.

    Tools:
        furl_compress — Compress content on demand. Stores original for
                           retrieval.
        furl_retrieve — Retrieve original uncompressed content by hash
                           from the local CCR store (with optional query /
                           pattern / line-range / fields / select_* filters).
        furl_stats    — Session statistics: compressions, savings, cost.
        furl_purge    — Erase stored originals: one hash, or all.
        furl_search   — Find stored originals by a content substring.
        furl_list     — List stored entries, newest first.
        furl_read     — File read with session caching (flag-gated:
                           FURL_MCP_READ).

    Compression and retrieval happen locally in-process via the shared CCR
    compression store.
    """

    def __init__(self) -> None:
        self._stats = SessionStats()
        self._local_store: Any = None  # Lazy-initialized CompressionStore
        self._compressor_initialized = False
        # File read cache: path → (content_hash, ccr_hash, line_count, token_count)
        self._file_cache: dict[str, tuple[str, str, int, int]] = {}

        if not MCP_AVAILABLE or Server is None:
            raise ImportError("MCP SDK not installed. Install with: pip install mcp")

        # Server-level instructions carry the CSV decode legend once per
        # conversation (FURL_MCP_LEGEND, default ON). ``None`` when gated
        # off — the SDK then omits the field from the initialize response.
        # ``version=`` is load-bearing: the SDK's create_initialization_options
        # falls back to ``importlib.metadata.version("mcp")`` when the Server has
        # no version, so serverInfo would otherwise advertise the MCP SDK's own
        # version as if it were Furl's. Report the furl-ctx distribution version
        # instead (``get_version`` is total — "unknown" when the package is not
        # installed, never raises).
        self.server: Server = Server(
            "furl",
            version=get_version(),
            instructions=CSV_DECODE_LEGEND if _legend_enabled() else None,
        )
        self._setup_handlers()

    def _get_local_store(self) -> Any:
        """Get the shared compression store singleton (lazy init).

        Returns the same instance the compress path uses so retrieval can
        see content compressed in-process. The singleton's config is fixed on
        FIRST init, so this passes ``default_ttl=_mcp_session_ttl()`` — the
        env-aware session TTL (FURL_CCR_TTL_SECONDS when set and valid, else
        ``MCP_SESSION_TTL``): the pipeline run inside ``_compress_content``
        persists dropped rows under the marker hash embedded in the
        compressed text WITHOUT an explicit ttl, and under the store's stock
        default (1800 s since Engine P0-3; 300 s when this fix landed) those
        rows expired mid-session while the wrapper hash (stored with the
        session TTL) advertised session persistence. Sharing the session TTL
        as the store default keeps both retrieval surfaces alive for the same
        window — and at the 3600 s fallback the MCP store stays at least as
        durable as the library default. The compress path still passes its
        own per-entry ``ttl`` at store time — the same ``_mcp_session_ttl()``
        value, so ON THIS SINGLETON PATH wrapper hash and dropped rows stay
        in lockstep by construction. Under an active NAMESPACE store (the
        early return below) the dropped-row default is instead env-derived
        at store construction (``_build_namespace_store``; library fallback
        1800 s), so there the two surfaces are in lockstep only when
        FURL_CCR_TTL_SECONDS is set AND valid — the plugin's shipped
        configuration. Unset or invalid env leaves wrapper 3600 s vs
        dropped rows 1800 s on the namespace path (pre-existing corner,
        see ``_compress_content``).

        Backend (Engine P1-7): the MCP deployment defaults to the durable
        SQLite backend so originals survive a server restart and sub-agent
        processes can retrieve main-agent hashes through the shared workspace
        database file. ``FURL_CCR_BACKEND=memory`` opts back out (any other
        explicit value defers to the library's env-selected loader).
        """
        from furl_ctx.cache.compression_store import (
            get_compression_store,
            resolve_ccr_namespace_store,
        )

        # Per-project isolation (audit #4): when a namespace is active — the
        # plugin exports FURL_CCR_PROJECT_DIR from the project root, or a user
        # set FURL_CCR_NAMESPACE — search / list / retrieve / evict MUST target
        # the SAME isolated store the compress path writes to. Without this the
        # read tools would serve the process-global singleton while the hook
        # wrote to the per-project store, so every retrieve would loud-miss.
        # resolve_* returns None when no namespace is active, leaving the
        # singleton path (its session TTL + durable backend) exactly as
        # before — so in-process tests that set no namespace are unaffected.
        namespace_store = resolve_ccr_namespace_store()
        if namespace_store is not None:
            return namespace_store

        if self._local_store is None:
            self._local_store = get_compression_store(
                default_ttl=_mcp_session_ttl(),
                backend=_default_store_backend(),
            )
        return self._local_store

    def _compress_content(
        self,
        content: str,
        mode: CompressionMode = CompressionMode.NORMAL,
        patterns: SectionPatterns | None = None,
    ) -> dict[str, Any]:
        """Compress content using Furl's pipeline (NR2-2 feature c aware).

        ``mode`` selects the pipeline (``NORMAL`` uses the process default, so
        a default call is byte-identical to before this feature).
        ``patterns`` (``include_patterns``/``exclude_patterns``), when
        non-empty, partition the content into eligible/protected line runs
        (compressed independently vs. verbatim) — handled by
        ``_compress_filtered``.

        Returns dict with compressed text, token counts, hash, etc.
        """
        from furl_ctx.compress import compress
        from furl_ctx.redaction import build_store_redactor

        # Pre-store redaction: scrub secrets from the content BEFORE it is
        # compressed, previewed, OR stored. compress() redacts its own message
        # copy, but this tool stores ``original=content`` verbatim (and
        # _compress_filtered stores runs of it), so the redaction MUST also happen
        # here or the raw secret would persist in the CCR store — the exact leak
        # the plugin-reachable redaction closes. The ON-by-default built-in
        # credential patterns (audit Crit-4 / B3) plus ``FURL_REDACT_PATTERNS``
        # both apply; shares the builder with the hook and library so one config
        # governs all three. None (built-ins opted out AND no env patterns) =>
        # content unchanged (byte-identical).
        _redactor = build_store_redactor()
        if _redactor is not None:
            content = _redactor(content)

        # Acquire (and thereby configure) the store singleton BEFORE running
        # the pipeline: compress() persists marker-hash dropped rows through
        # its own no-arg get_compression_store() call, and the singleton's
        # default TTL is fixed on first init. Initializing here first
        # guarantees those embedded-marker entries carry the session TTL
        # (env-aware ``_mcp_session_ttl``) rather than the 1800 s pipeline
        # default, which would silently expire granular retrieval 30 minutes
        # into a session-long window. Under an active NAMESPACE store the
        # default is env-derived at construction instead (see
        # compression_store._build_namespace_store) — with
        # FURL_CCR_TTL_SECONDS set AND valid, the plugin's shipped
        # configuration, both surfaces carry the same env TTL there too.
        # Unset or INVALID env splits them on the namespace path (each
        # resolver falls back separately: 3600 s here, 1800 s in the
        # library) — pre-existing corner, see _get_local_store.
        store = self._get_local_store()

        # Section filtering (non-empty patterns) → run-by-run path. Delegated
        # so the common (unfiltered) path below stays the original single-unit
        # body, byte-identical to before this feature when mode is NORMAL.
        if patterns is not None and not patterns.is_empty:
            return self._compress_filtered(content, mode, patterns)

        # NORMAL mode builds no pipeline (uses the process default singleton),
        # keeping a default call byte-identical; other modes select a
        # configured pipeline via existing ContentRouterConfig knobs.
        pipeline = build_mode_pipeline(mode)

        # Wrap content as a tool message (most common compression target)
        messages = [{"role": "tool", "content": content}]

        result = compress(
            messages,
            model=_MCP_TOKEN_MODEL,
            pipeline=pipeline,
            tool_name=_MCP_COMPRESS_TOOL_NAME,
        )

        if result.error:
            # COR-36: compress() failed open — result.messages are the
            # ORIGINAL messages and tokens_after is 0. Storing that would
            # persist the original as "compressed" under a marker, and
            # recording it would book a fictional savings_percent=100 into
            # the session totals. Skip both and return an error-shaped
            # payload so the host sees the failure loudly (compress()
            # already logged the full traceback at ERROR).
            logger.error("event=mcp_compress_failed error=%s", result.error)
            return {
                "error": f"compression failed: {result.error}",
                "original_tokens": result.tokens_before,
            }

        compressed_content = result.messages[0].get("content", content)
        input_tokens = result.tokens_before
        output_tokens = result.tokens_after

        # Store original in local store for later retrieval. require_durable:
        # the MCP server's default backend is the durable sqlite store; a write
        # that fell open to volatile in-process memory (degraded backend, or a
        # lost lock race) must not be advertised as retrievable-later — a
        # sub-agent process or a restart would miss, silently breaking the
        # exact cross-process durability the sqlite backend exists to provide
        # (review F2). Lazy import, matching this module's convention.
        from furl_ctx.cache.compression_store import DurableWriteError

        hash_key: str | None
        volatile_hash: str | None = None
        try:
            hash_key = store.store(
                original=content,
                compressed=compressed_content
                if isinstance(compressed_content, str)
                else json.dumps(compressed_content),
                original_tokens=input_tokens,
                compressed_tokens=output_tokens,
                tool_name=_MCP_COMPRESS_TOOL_NAME,
                compression_strategy="mcp_compress",
                ttl=_mcp_session_ttl(),
                require_durable=True,
            )
        except DurableWriteError as exc:
            # The durable write did not land. A cheap read-back distinguishes the
            # two causes so the response stays honest (Bug-6):
            #   (a) it fell open to THIS process's volatile tier — still
            #       retrievable now under exc.hash_key; carry it forward.
            #   (b) the binding was DROPPED entirely (a true hash collision) —
            #       nothing is retrievable; revert to the original, claim nothing.
            volatile_hash = exc.hash_key if store.exists(exc.hash_key) else None
            logger.warning(
                "event=mcp_compress_durable_veto hash=%s retrievable=%s error=%s",
                exc.hash_key,
                volatile_hash is not None,
                exc,
            )
            hash_key = None

        # Track stats (the compression itself succeeded on both paths)
        strategy = (
            ", ".join(result.transforms_applied) if result.transforms_applied else "passthrough"
        )
        self._stats.record_compression(input_tokens, output_tokens, strategy)

        tokens_saved = max(0, input_tokens - output_tokens)
        savings_pct = round(tokens_saved / input_tokens * 100, 1) if input_tokens > 0 else 0

        if hash_key is None and volatile_hash is None:
            # Collision-drop veto (Bug-6): a true hash collision made the binding
            # ambiguous, so it was dropped to avoid serving foreign content and
            # NOTHING is retrievable. Return the ORIGINAL uncompressed content and
            # say so plainly — no hash, no false "retrievable" claim.
            return {
                "compressed": content,
                "hash": None,
                "durably_stored": False,
                "original_tokens": input_tokens,
                "compressed_tokens": input_tokens,
                "tokens_saved": 0,
                "visible_token_reduction_percent": 0,
                "savings_percent": 0,
                "transforms": [],
                "note": (
                    "Not stored: a hash collision made the binding ambiguous, so it was "
                    "dropped to avoid serving foreign content. The original is returned "
                    "uncompressed and unchanged; retry to store it under a fresh "
                    "compression."
                ),
            }

        if hash_key is None:
            # Durability veto: the write fell open to THIS process's volatile
            # tier after the retry budget. It IS retrievable now (return the hash
            # so the caller can), but not after a restart and not from other
            # processes — say exactly that, and name the likely cause. The caller
            # still holds the original it sent; nothing is lost.
            return {
                "compressed": compressed_content,
                "hash": volatile_hash,
                "original_tokens": input_tokens,
                "compressed_tokens": output_tokens,
                "tokens_saved": tokens_saved,
                "savings_percent": savings_pct,
                "transforms": result.transforms_applied,
                "durably_stored": False,
                "note": (
                    f"Stored in THIS server process only (volatile fallback after "
                    f"SQLite lock contention). Retrievable now via "
                    f"mcp__furl__{CCR_TOOL_NAME} with hash={volatile_hash}, but it "
                    f"will NOT survive a restart of this server and other furl "
                    f"processes cannot see it. Likely another furl MCP server "
                    f"process — a second, live or stale, Claude Code session on "
                    f"this project — holds the store; see LIBRARY.md “Multiple "
                    f"sessions on one project”. Keep your original if you need "
                    f"recovery that survives a restart."
                ),
            }

        note = (
            f"Original stored with hash={hash_key}. "
            f"Use mcp__furl__{CCR_TOOL_NAME} to get full content later."
        )
        if tokens_saved == 0:
            # A bare "0%" reads as a malfunction. Name the engine's own
            # reason(s): the raw transforms_applied strings (e.g. a router
            # noop tag), rendered generically — their taxonomy belongs to
            # the router, not to this handler. Gated on tokens_saved, not
            # the ROUNDED savings_pct: a real sub-0.05% saving displays as
            # 0.0 but "No token savings" would be false for it.
            note += f" No token savings on this content — engine transforms: {strategy}."

        # A structured compression can embed granular ``<<ccr:…>>`` markers in
        # the compressed view (one per offloaded fragment / row-select), each
        # carrying its OWN hash distinct from this whole-content ``hash``.
        # Surfacing two different-looking hashes for one compression with no
        # explanation reads as a bug; name the relationship so the caller knows
        # both resolve and how they differ (review F6).
        from furl_ctx.ccr.marker_grammar import hashes_in_text

        preview_text = (
            compressed_content
            if isinstance(compressed_content, str)
            else json.dumps(compressed_content)
        )
        embedded_hashes = [h for h in hashes_in_text(preview_text) if h != hash_key]
        if embedded_hashes:
            count = len(embedded_hashes)
            shown = ", ".join(embedded_hashes[:3])
            more = "…" if count > 3 else ""
            marker_word = "marker" if count == 1 else "markers"
            hash_word = "hash" if count == 1 else "hashes"
            # Plain user-facing copy (review F6): no em-dashes, en-dashes, or
            # parentheses; and fragment markers stored under their own keys
            # resolve against the same underlying source document, not one shared
            # stored original.
            note += (
                f" The compressed view also embeds {count} granular <<ccr:…>> "
                f"{marker_word}, each with its own {hash_word}: {shown}{more}. Each "
                f"one retrieves just one offloaded fragment or supports a row-select, "
                f"separate from this whole-content hash={hash_key}. All resolve "
                f"against the same underlying source document; retrieve any of them "
                f"the same way."
            )

        return {
            "compressed": compressed_content,
            "hash": hash_key,
            "original_tokens": input_tokens,
            "compressed_tokens": output_tokens,
            "tokens_saved": tokens_saved,
            "savings_percent": savings_pct,
            "transforms": result.transforms_applied,
            "note": note,
        }

    def _compress_filtered(
        self,
        content: str,
        mode: CompressionMode,
        patterns: SectionPatterns,
    ) -> dict[str, Any]:
        """Compress only the pattern-eligible line runs; keep protected verbatim.

        Each eligible run is compressed independently through ``_compress_content``
        (unfiltered, so it takes the single-unit path and gets its own
        retrievable hash); protected runs pass through unchanged. Runs are
        rejoined in original order, so protected bytes and ordering are exact.
        Aggregates per-run token counts into one envelope with the list of
        hashes.
        """
        runs = partition_content(content, patterns)

        rendered_parts: list[str] = []
        hashes: list[str] = []
        volatile_hashes: list[str] = []
        transforms: list[str] = []
        total_input = 0
        total_output = 0

        for run in runs:
            if not run.eligible or not run.text.strip():
                # Protected run, or an eligible-but-blank run (nothing to
                # compress) — ship verbatim. Blank runs count as zero-token
                # passthrough, matching how a whitespace-only compress no-ops.
                rendered_parts.append(run.text)
                continue
            part = self._compress_content(run.text, mode)
            if "error" in part:
                # A run-level fail-open: surface loudly rather than silently
                # shipping a partial mix (the whole call reports the failure).
                return part
            rendered_parts.append(
                part["compressed"]
                if isinstance(part["compressed"], str)
                else json.dumps(part["compressed"])
            )
            # A vetoed run now returns its VOLATILE hash (durably_stored False)
            # rather than omitting "hash" — so this stays crash-free under
            # contention and the aggregate can flag the volatile runs honestly.
            hashes.append(part["hash"])
            if part.get("durably_stored") is False:
                volatile_hashes.append(part["hash"])
            transforms.extend(part.get("transforms", []))
            total_input += part["original_tokens"]
            total_output += part["compressed_tokens"]

        rendered = "\n".join(rendered_parts)
        tokens_saved = max(0, total_input - total_output)
        savings_pct = round(tokens_saved / total_input * 100, 1) if total_input > 0 else 0

        note = (
            f"Pattern-filtered compression: {len(hashes)} eligible run(s) compressed, "
            f"protected lines passed through verbatim. Retrieve any run via "
            f"mcp__furl__{CCR_TOOL_NAME} with its hash."
        )
        if tokens_saved == 0:
            # Same zero-savings honesty as the single-unit path: name the
            # engine's raw per-run transform strings rather than shipping an
            # unexplained 0%. Gated on tokens_saved, not the rounded
            # savings_pct (see the single-unit path).
            strategy = ", ".join(transforms) if transforms else "passthrough"
            note += f" No token savings on this content — engine transforms: {strategy}."

        if volatile_hashes:
            note += (
                f" WARNING: {len(volatile_hashes)} run(s) are stored in THIS "
                f"process only (volatile fallback after SQLite lock contention) — "
                f"retrievable now but not after a server restart, and invisible to "
                f"other furl processes (hashes: {', '.join(volatile_hashes)}). "
                f"Likely another furl MCP server process (a second Claude Code "
                f"session) holds the store; see LIBRARY.md “Multiple sessions on "
                f"one project”."
            )

        result: dict[str, Any] = {
            "compressed": rendered,
            "mode": mode.value,
            "filtered": True,
            "hashes": hashes,
            "compressed_runs": len(hashes),
            "original_tokens": total_input,
            "compressed_tokens": total_output,
            "tokens_saved": tokens_saved,
            "savings_percent": savings_pct,
            "transforms": transforms,
            "note": note,
        }
        if volatile_hashes:
            # Some runs are volatile-only: flag it so the caller does not treat
            # every hash as durably retrievable. Absent when all runs are durable,
            # keeping the healthy-path shape byte-identical (as the single-unit
            # path only adds durably_stored on a veto).
            result["durably_stored"] = False
            result["volatile_hashes"] = volatile_hashes
        return result

    async def _search_all_content(self, query: str) -> dict[str, Any]:
        """Cross-store full-text search (NR2-2 feature a).

        Ranks EVERY live entry against ``query`` via BM25 and returns the top
        matches as ``(hash, score, preview)`` so the caller can follow up with
        a per-hash retrieve. With the durable SQLite backend this spans
        cross-session / cross-process entries. Previews are redacted at the
        store so this surface never leaks a secret a per-hash retrieval would
        have masked. Pure read — no retrieval is booked (the caller retrieves
        by hash next), so no ``record_retrieval`` here.
        """
        # PERF-16: search_all is synchronous BM25 scoring over SQLite-backed
        # rows; run it in a worker thread so the event loop is never blocked
        # (the documented sqlite-backend invariant — backends/sqlite.py).
        return await asyncio.to_thread(self._search_all_content_sync, query)

    def _search_all_content_sync(self, query: str) -> dict[str, Any]:
        """Blocking body of :meth:`_search_all_content` (runs off the loop)."""
        store = self._get_local_store()
        matches = store.search_all(query)
        return {
            "source": "cross_store",
            "query": query,
            "count": len(matches),
            "matches": [
                {
                    "hash": match.hash,
                    "score": round(match.score, 4),
                    "preview": match.preview,
                    "tool_name": match.tool_name,
                }
                for match in matches
            ],
            "note": (
                "Ranked matches across all stored entries. Call furl_retrieve with a "
                "hash to get its full original content."
            )
            if matches
            else (
                "No stored entry matched the query. The store may be empty, or no "
                "entry contains the query terms."
            ),
        }

    async def _retrieve_content(
        self,
        hash_key: str,
        query: str | None,
        filters: RetrieveFilters | None = None,
    ) -> dict[str, Any]:
        """Retrieve content from the local CCR store.

        Retrieval-feedback wiring (Engine P2-13): this handler is where real
        model-driven retrievals land, and the signal they emit rides the
        store's own access bump — ``store.retrieve`` on the full path,
        ``store.search`` → ``_record_search_access`` on the query path (which
        fires only when results shipped, COR-37). No second emission here:
        the store is the single honest choke point, and a handler-level
        emission would double-count every retrieval.

        ``filters`` (NR2-2 feature b) narrow the no-query full retrieve
        (regex/line-range over text, field projection over JSON arrays). They
        are mutually exclusive with ``query`` at the handler boundary, so a
        non-empty ``filters`` is only ever passed on the no-query path.
        """
        # PERF-16: every store op below (search / exists / retrieve /
        # get_entry_status) and the retrieval-stat file append are synchronous
        # SQLite/file I/O; run the whole body in a worker thread so the event
        # loop stays free (the documented sqlite-backend invariant).
        return await asyncio.to_thread(self._retrieve_content_sync, hash_key, query, filters)

    def _retrieve_content_sync(
        self,
        hash_key: str,
        query: str | None,
        filters: RetrieveFilters | None = None,
    ) -> dict[str, Any]:
        """Blocking body of :meth:`_retrieve_content` (runs off the loop)."""
        store = self._get_local_store()
        if query:
            results = store.search(hash_key, query)
            if results:
                self._stats.record_retrieval(hash_key)
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": results,
                    "count": len(results),
                }
            # Search returned nothing. That does NOT mean the entry was
            # evicted — a LIVE entry with no query match must report "no match",
            # not a false "no longer retrievable" eviction error. Only fall
            # through to the cause-honest miss path when the entry is genuinely
            # gone from the store. Use the side-effect-free ``exists`` check
            # (not ``retrieve``, which logs a retrieval event + bumps access
            # stats) so a no-match query does not inflate retrieval metrics —
            # nothing was actually retrieved.
            if store.exists(hash_key):
                return {
                    "hash": hash_key,
                    "source": "local",
                    "query": query,
                    "results": [],
                    "count": 0,
                    "note": (
                        "Entry is available but no stored item matched the query. "
                        "Retry with a different query, or omit the query to retrieve "
                        "the full original content."
                    ),
                }
        else:
            entry = store.retrieve(hash_key)
            if entry:
                self._stats.record_retrieval(hash_key)
                if filters is not None and not filters.is_empty:
                    return self._apply_retrieve_filters(hash_key, entry, filters)
                return {
                    "hash": hash_key,
                    "source": "local",
                    # Originating tool (content_kind) — surfaced here too, not
                    # just in furl_list, so a retrieve caller can see where the
                    # content came from ("Bash", "mcp:furl_compress", ...).
                    "content_kind": entry.tool_name,
                    "original_content": entry.original_content,
                    "original_item_count": entry.original_item_count,
                    "compressed_item_count": entry.compressed_item_count,
                    "retrieval_count": entry.retrieval_count,
                }

        # Loud, cause-honest miss: the local store came up empty.
        # Mirror response_handler so every model-facing retrieve surface reports
        # a miss the same way (explicit error, never a silent empty result) and
        # attributes it to its real cause (eviction/capacity/expiry) rather than
        # vaguely to the TTL.
        from furl_ctx.cache.compression_store import format_retrieval_miss_detail

        get_status = getattr(store, "get_entry_status", None)
        miss_status = (
            get_status(hash_key, clean_expired=True)
            if callable(get_status)
            else {"hash": hash_key, "status": "missing"}
        )
        return {
            "error": format_retrieval_miss_detail(miss_status),
            "hash": hash_key,
            "status": miss_status.get("status", "missing"),
            "hint": "Content compressed via furl_compress is stored for the "
            "session using the configured CCR TTL.",
        }

    def _apply_retrieve_filters(
        self,
        hash_key: str,
        entry: Any,
        filters: RetrieveFilters,
    ) -> dict[str, Any]:
        """Project a retrieved entry through validated filters (NR2-2 b).

        A shape mismatch (``fields`` on a non-array original) comes back from
        ``apply_filters`` as a ``FilterError`` — surfaced as a structured error
        envelope, never a crash or a silently-empty success.
        """
        outcome = apply_filters(entry.original_content, filters)
        if isinstance(outcome, FilterError):
            return {
                "error": outcome.reason,
                "hash": hash_key,
                "source": "local",
            }
        return {
            "hash": hash_key,
            "source": "local",
            "content_kind": entry.tool_name,
            "filtered": True,
            "filter_kind": outcome.kind,
            "filtered_content": outcome.content,
            "matched_count": outcome.matched_count,
            "total_count": outcome.total_count,
            "original_item_count": entry.original_item_count,
            "compressed_item_count": entry.compressed_item_count,
            "retrieval_count": entry.retrieval_count,
        }

    def _setup_handlers(self) -> None:
        """Register all MCP tool handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            tools = [
                Tool(
                    name=COMPRESS_TOOL_NAME,
                    description=(
                        "Compress content to save context window space. "
                        "Use this on large tool outputs, file contents, search results, "
                        "or any content you want to shrink before reasoning over it. "
                        f"The original is stored and can be retrieved later via mcp__furl__{CCR_TOOL_NAME}. "
                        "Returns compressed text + a hash for retrieval. Optional 'mode' "
                        "controls aggressiveness; optional include/exclude patterns limit "
                        "which lines are compressed."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": (
                                    "The content to compress. Can be any text: file contents, "
                                    "JSON, search results, logs, code, etc."
                                ),
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["lossless_only", "normal", "aggressive"],
                                "description": (
                                    "Compression aggressiveness (default 'normal' = current "
                                    "behavior). 'lossless_only': only proven-lossless "
                                    "transforms run — nothing is dropped or substituted, so "
                                    "the output carries no retrieval markers (larger, fully "
                                    "reversible). 'aggressive': keep fewer items per crush and "
                                    "accept marginal compressions the default would reject "
                                    "(smaller output; all drops stay CCR-recoverable)."
                                ),
                            },
                            "include_patterns": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Glob-or-regex patterns (regex tried first, glob "
                                    "fallback). When set, ONLY content lines matching at "
                                    "least one pattern are eligible for compression; all "
                                    "other lines pass through verbatim."
                                ),
                            },
                            "exclude_patterns": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Glob-or-regex patterns. Any content line matching one is "
                                    "PROTECTED — passed through verbatim, never compressed. "
                                    "Applied on top of include_patterns."
                                ),
                            },
                        },
                        "required": ["content"],
                    },
                ),
                Tool(
                    name=CCR_TOOL_NAME,
                    description=(
                        "Retrieve original uncompressed content by hash. "
                        "Use this when you need full details from previously compressed content. "
                        "The hash comes from furl_compress results or from compression "
                        "markers like [N items compressed... hash=abc123].\n"
                        "Two extra modes: (1) OMIT hash and pass query to search across "
                        "ALL stored entries (returns ranked hash/score/preview matches to "
                        "retrieve individually); (2) pass hash with pattern/fields/line_range, "
                        "or a select_field row-filter (keep the rows of a JSON array by exact "
                        "value or numeric range), to project just part of the original. "
                        "Filters cannot be combined with query.\n"
                        "Examples: furl_retrieve(hash) -> the whole original; "
                        'furl_retrieve(hash, pattern="ERROR") -> only the matching lines; '
                        'furl_retrieve(hash, select_field="id", select_equals=42) -> the '
                        "rows where id==42."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hash": {
                                "type": "string",
                                "description": (
                                    "Hash key from compression (e.g., 'abc123' from "
                                    "hash=abc123). Omit to search across all entries via "
                                    "'query'."
                                ),
                            },
                            "query": {
                                "type": "string",
                                "description": (
                                    "Search query. WITH a hash: return only items in that "
                                    "entry matching the query. WITHOUT a hash: full-text "
                                    "search (BM25-ranked) across every stored entry, "
                                    "returning top matches as hash/score/preview. Mutually "
                                    "exclusive with pattern/fields/line_range."
                                ),
                            },
                            "pattern": {
                                "type": "string",
                                "description": (
                                    "Regex applied line-by-line to the full original "
                                    "(requires a hash, no query). Returns matching lines "
                                    "(prefixed with 1-based line numbers) plus "
                                    "'context_lines' lines of surrounding context. Invalid "
                                    "regex returns an error."
                                ),
                            },
                            "context_lines": {
                                "type": "integer",
                                "description": (
                                    "Lines of context to include on each side of a "
                                    "'pattern' match (default 0, max 50)."
                                ),
                            },
                            "line_range": {
                                "type": "array",
                                "items": {"type": ["integer", "null"]},
                                "minItems": 2,
                                "maxItems": 2,
                                "description": (
                                    "[start, end] 1-based inclusive line window over the "
                                    "full original (requires a hash, no query). Either bound "
                                    "may be null for an open end. Composes with 'pattern' "
                                    "(the range is applied first)."
                                ),
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "For a JSON-array original: project only these keys out "
                                    "of each object element (requires a hash, no query). "
                                    "Errors if the original is not a JSON array. Cannot be "
                                    "combined with pattern/line_range; composes with "
                                    "select_field (projects the columns of the kept rows)."
                                ),
                            },
                            "select_field": {
                                "type": "string",
                                "description": (
                                    "Row-select over a JSON array of objects (requires a "
                                    "hash, no query): the field/column name to match on. It "
                                    "anchors the whole select family — select_equals / "
                                    "select_min / select_max / limit are honored ONLY "
                                    "alongside select_field (any of them without it is an "
                                    "error). Reads a top-level JSON array of objects OR a "
                                    "JSON object with exactly one dominant inner array (e.g. "
                                    "a '{metadata, traceEvents:[...]}' trace). Composes with "
                                    "'fields'; cannot be combined with pattern/line_range or "
                                    "query."
                                ),
                            },
                            "select_equals": {
                                "type": ["string", "number", "boolean", "null"],
                                "description": (
                                    "Equality mode: keep rows whose select_field equals this "
                                    "JSON scalar (string/number/boolean/null; a list or "
                                    "object is rejected). Bool-safe — true never matches the "
                                    "number 1. Mutually exclusive with select_min/select_max."
                                ),
                            },
                            "select_min": {
                                "type": "number",
                                "description": (
                                    "Numeric-range mode: keep rows whose select_field is a "
                                    "number >= select_min (inclusive; open lower bound when "
                                    "omitted). A row whose field is missing or non-numeric is "
                                    "skipped, never an error. Mutually exclusive with "
                                    "select_equals."
                                ),
                            },
                            "select_max": {
                                "type": "number",
                                "description": (
                                    "Numeric-range mode: keep rows whose select_field is a "
                                    "number <= select_max (inclusive; open upper bound when "
                                    "omitted). Must be >= select_min. Mutually exclusive with "
                                    "select_equals."
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "Max rows a select_field row-select returns (positive "
                                    "integer; defaults to 1000 when a select is requested "
                                    "without it). When more rows match, only the first "
                                    "'limit' ship plus one explicit truncation-marker row. "
                                    "Applies only to select_field row-selects."
                                ),
                            },
                        },
                    },
                ),
                Tool(
                    name=STATS_TOOL_NAME,
                    description=(
                        "Show compression statistics. Two clearly-labeled scopes: "
                        "(1) this-server-process counters — compressions, tokens saved, "
                        "estimated cost savings, and recent events done by THIS process; "
                        "and (2) a live 'store' section derived from the shared CCR store "
                        "for this namespace — live_entries, original vs compressed bytes and "
                        "tokens, estimated tokens saved, and oldest/newest entry age. The "
                        "store section reflects entries written by ALL processes (including "
                        "the PostToolUse hook and sub-agents), so it stays truthful even when "
                        "this server process compressed nothing."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                Tool(
                    name=PURGE_TOOL_NAME,
                    description=(
                        "Permanently erase stored originals from the CCR store — the "
                        "data-erase escape hatch (offloaded content otherwise persists for "
                        "the session TTL). Pass EXACTLY ONE of: 'hash' (delete one entry by "
                        "its CCR hash) or 'all'=true (wipe every entry). Returns how many "
                        "entries were deleted. A hash that is already absent deletes nothing "
                        "and is not an error. There is no undo."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hash": {
                                "type": "string",
                                "description": (
                                    "CCR hash of the single entry to erase (12 or 24 "
                                    "lowercase-hex chars). Mutually exclusive with 'all'."
                                ),
                            },
                            "all": {
                                "type": "boolean",
                                "description": (
                                    "When true, erase EVERY stored entry. Mutually exclusive "
                                    "with 'hash'."
                                ),
                            },
                        },
                    },
                ),
                Tool(
                    name=SEARCH_TOOL_NAME,
                    description=(
                        "Find stored originals by a case-insensitive SUBSTRING of their "
                        "content — for when you lost a <<ccr:HASH>> marker but remember some "
                        "of the text. Returns per hit: hash, a short preview around the "
                        "match, created-at, and size (characters), newest first. Substring "
                        "only (NOT a regex). Then call furl_retrieve with a returned hash "
                        "for the full original."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "Non-empty substring to look for (case-insensitive). "
                                    "Matched literally — not a regex."
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": _SEARCH_LIST_LIMIT_CAP,
                                "description": (
                                    f"Max hits to return (default {_SEARCH_LIST_LIMIT_DEFAULT}, "
                                    f"capped at {_SEARCH_LIST_LIMIT_CAP})."
                                ),
                            },
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name=LIST_TOOL_NAME,
                    description=(
                        "List stored CCR entries, newest first — a directory of what "
                        "furl_compress / furl_read have stashed this session. Returns per "
                        "entry: hash, created-at, age (humanized time since storage), ttl "
                        "(the entry's retention window), expires_in (humanized time left "
                        'before the TTL evicts it, e.g. "23h"), size (characters), '
                        "content-kind (the originating tool, when known), and a short "
                        "preview. Page with limit/offset. Use furl_retrieve with a hash for "
                        "the full original, or furl_search to find by content."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": _SEARCH_LIST_LIMIT_CAP,
                                "description": (
                                    f"Max entries to return (default {_SEARCH_LIST_LIMIT_DEFAULT}, "
                                    f"capped at {_SEARCH_LIST_LIMIT_CAP})."
                                ),
                            },
                            "offset": {
                                "type": "integer",
                                "minimum": 0,
                                "description": (
                                    "Number of newest entries to skip, for paging (default 0)."
                                ),
                            },
                        },
                    },
                ),
            ]

            # Conditionally add furl_read (behind feature flag)
            if _READ_ENABLED:
                tools.append(
                    Tool(
                        name=READ_TOOL_NAME,
                        description=(
                            "Read a file with smart caching. First read returns full content "
                            "and caches it. Subsequent reads of the same unchanged file return "
                            "a lightweight cache marker (~20 tokens instead of thousands). "
                            f"Use mcp__furl__{CCR_TOOL_NAME} with the hash to get full content if needed. "
                            "Use this INSTEAD of the built-in Read tool for significant token savings."
                        ),
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "file_path": {
                                    "type": "string",
                                    "description": "Absolute path to the file to read.",
                                },
                                "fresh": {
                                    "type": "boolean",
                                    "description": (
                                        "Force a fresh read, bypassing cache. Use after context "
                                        "compaction, in subagents, or when you need guaranteed "
                                        "current content."
                                    ),
                                },
                            },
                            "required": ["file_path"],
                        },
                    )
                )

            return tools

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            started = time.perf_counter()
            # INFO: operationally-useful identity only (which tool was invoked).
            # The arguments carry sensitive payloads (file contents, queries,
            # paths) and must NOT be logged verbatim at any level — even a
            # truncated dump leaks the leading bytes. The per-call DEBUG line
            # below records only the argument SHAPE (keys + value lengths).
            logger.info("event=mcp_tool_call_received tool=%s", name)
            logger.debug(
                "event=mcp_tool_call_received_detail tool=%s arguments_shape=%s",
                name,
                _describe_arguments_for_log(arguments),
            )
            try:
                if name == COMPRESS_TOOL_NAME:
                    result = await self._handle_compress(arguments)
                elif name == CCR_TOOL_NAME:
                    result = await self._handle_retrieve(arguments)
                elif name == STATS_TOOL_NAME:
                    result = await self._handle_stats()
                elif name == PURGE_TOOL_NAME:
                    result = await self._handle_purge(arguments)
                elif name == SEARCH_TOOL_NAME:
                    result = await self._handle_search(arguments)
                elif name == LIST_TOOL_NAME:
                    result = await self._handle_list(arguments)
                elif name == READ_TOOL_NAME and _READ_ENABLED:
                    result = await self._handle_read(arguments)
                else:
                    result = _err(f"Unknown tool: {name}")
                # INFO: outcome envelope — tool, latency, and result SIZE. The
                # result body can carry retrieved original content or whole file
                # bodies, so it is never logged verbatim; a char count conveys
                # the outcome magnitude without the payload.
                logger.info(
                    "event=mcp_tool_call_completed tool=%s duration_ms=%.2f result_chars=%d",
                    name,
                    (time.perf_counter() - started) * 1000.0,
                    _result_chars_for_log(result),
                )
                return result
            except Exception as e:
                # Full exception detail (message + traceback) is logged server-side
                # at ERROR; the model channel gets only a generic message so internal
                # detail (paths, stack frames, dependency internals) never leaks.
                #
                # RE-RAISE (sanitized) instead of returning a success-shaped
                # ``{"error": ...}`` TextContent: the MCP SDK converts a raised
                # exception into a CallToolResult with ``isError=True``, which is
                # the only machine-readable failure signal hosts/retriers/
                # evaluators have (API-15). Parameter mistakes (missing/mistyped
                # arguments, bad hashes, unknown tools) stay model-visible JSON
                # envelopes — those are the model's to fix, not server failures.
                logger.error(f"Tool {name} failed: {e}", exc_info=True)
                raise RuntimeError(f"Internal error handling tool: {name}") from e

        # TEST-22: expose the routing handler as a named attribute so tests
        # (and any embedder) can call the tool-dispatch logic directly. The
        # SDK registration above wraps `call_tool` in its own private
        # `handler` closure under `request_handlers[CallToolRequest]`;
        # without this attribute the only way to reach the routing branches
        # was closure-cell introspection of that wrapper — which breaks on
        # any `mcp` SDK bump.
        self.route_call_tool = call_tool

    async def _handle_compress(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle furl_compress tool call."""
        content = arguments.get("content")
        if not content:
            return _err("content parameter is required")

        # Non-string params take a parameter error, not the generic internal
        # path — mirrors the retrieve handler's hash guard (API-15).
        if not isinstance(content, str):
            return _err(f"content parameter must be a string, got {type(content).__name__}")

        # Reject oversized input before compressing it (OOM DoS guard). The cap
        # is a BYTE ceiling (matching furl_read's byte-measured limit), so measure
        # the encoded byte length, not the character count (Bug-8) — on multibyte
        # content a char count is up to ~4x short and lets an over-ceiling payload
        # through. Cheap bounds avoid encoding the common small case: chars are a
        # lower bound on UTF-8 bytes and 4*chars an upper bound.
        _char_len = len(content)
        _too_large = _char_len > _MAX_READ_BYTES or (
            _char_len * 4 > _MAX_READ_BYTES
            and len(content.encode("utf-8", errors="replace")) > _MAX_READ_BYTES
        )
        if _too_large:
            _byte_len = len(content.encode("utf-8", errors="replace"))
            return _err(
                f"Content too large to compress: {_byte_len} bytes (limit {_MAX_READ_BYTES} bytes)"
            )

        # NR2-2 feature c: aggressiveness/filter mode. Both default to today's
        # behavior (mode=normal, no patterns), so a plain call is byte-identical.
        mode = CompressionMode.parse(arguments.get("mode"))
        if isinstance(mode, str):
            return _err(mode)

        patterns = SectionPatterns.parse(arguments)
        if isinstance(patterns, str):
            return _err(patterns)

        # Run compression in thread pool (it's CPU-bound)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._compress_content, content, mode, patterns)

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    async def _handle_retrieve(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle furl_retrieve tool call."""
        query = arguments.get("query")
        # Parameter-error treatment for a non-string query — the caller's
        # mistake, not an internal failure (API-15). Checked BEFORE the hash so
        # the no-hash cross-store-search route (feature a) sees a valid query.
        if query is not None and not isinstance(query, str):
            return _err(f"query parameter must be a string, got {type(query).__name__}")

        hash_key = arguments.get("hash")
        if not hash_key:
            # Cross-store search (NR2-2 feature a): no hash + a query → rank all
            # stored entries. No hash AND no query stays the original loud
            # parameter error (a bare retrieve needs a target).
            if query:
                # Schema honesty (symmetry with the with-hash path): filters
                # project a SINGLE stored entry, so they require a hash. The
                # with-hash path rejects query+filters loudly; silently ignoring
                # the same keys here let {query, select_field} run a plain
                # cross-store search as if the filter had been applied. Parse
                # with the same smart constructor so a malformed filter gets its
                # own structured error and a valid one gets the missing-hash one.
                filters = RetrieveFilters.parse(arguments)
                if isinstance(filters, FilterError):
                    return _err(filters.reason)
                if not filters.is_empty:
                    return _err(
                        "filters (pattern/fields/line_range/select_*) require a "
                        "hash: they project a single stored entry. Pass the "
                        "entry's hash to filter it, or drop the filters to "
                        "search across all entries."
                    )
                logger.info("event=mcp_search_all_started")
                logger.debug("event=mcp_search_all_started_detail query_len=%d", len(query))
                result = await self._search_all_content(query)
                response_text = json.dumps(result, indent=2, allow_nan=False)
                logger.info(
                    "event=mcp_search_all_completed matches=%d result_chars=%d",
                    result.get("count", 0),
                    len(response_text),
                )
                return [TextContent(type="text", text=response_text)]
            # No hash and no query: unchanged from before this feature — a bare
            # retrieve needs a target. Kept byte-identical (the cross-store
            # search route is discoverable via the tool schema, not this error).
            return _err("hash parameter is required")

        # Same width+charset spoofing guard the tool-call parse path applies
        # (marker_grammar.is_valid_ccr_hash) — keep both ccr-hash ingress points
        # consistent. A malformed key is a loud 400 here, never reaches the store.
        if not is_valid_ccr_hash(hash_key):
            return _err("invalid hash format (expected 12 or 24 lowercase-hex chars)")

        # Store keys are always lowercase (SHA-256 hexdigest output; store()
        # lowercases explicit hashes) while the format guard above is
        # case-insensitive — normalize at ingress so an upper/title-cased echo
        # of a marker hash HITS instead of missing with a confusing
        # "evicted/never stored" error.
        hash_key = hash_key.lower()

        # Parse per-hash filters (NR2-2 feature b). Filters narrow the no-query
        # full retrieve and are mutually exclusive with a query — a query
        # already selects items within the entry, and the two describe
        # incompatible views. Validation lives in the smart constructor, so any
        # bad regex / range / field list is a structured error here, never a
        # crash downstream.
        filters = RetrieveFilters.parse(arguments)
        if isinstance(filters, FilterError):
            return _err(filters.reason)
        if query is not None and not filters.is_empty:
            return _err(
                "filters (pattern/fields/line_range) cannot be combined "
                "with query; use a query to search within the entry, or "
                "filters to project the full original"
            )

        # INFO: the hash is a content-address (validated 12/24-hex above), safe
        # to log; the query is a user-supplied search string and the result can
        # carry the retrieved ORIGINAL content — neither is logged verbatim. The
        # DEBUG line records whether a query was present and its length only.
        has_query = query is not None
        logger.info(
            "event=mcp_retrieve_started hash=%s has_query=%s has_filters=%s",
            hash_key,
            has_query,
            not filters.is_empty,
        )
        logger.debug(
            "event=mcp_retrieve_started_detail hash=%s query_len=%s",
            hash_key,
            len(query) if isinstance(query, str) else 0,
        )
        result = await self._retrieve_content(hash_key, query, filters)
        try:
            response_text = json.dumps(result, indent=2, allow_nan=False)
        except ValueError:
            # The query path re-serializes parsed items, and a stored numeric
            # that materialized as float inf/nan (e.g. 1e400) would be emitted
            # as bare Infinity — RFC-invalid JSON a strict host rejects. The
            # store's numeric-fidelity fallback (text chunks for lossy
            # canonicals) makes this unreachable in normal operation; as a
            # backstop, return the byte-exact no-query response — the original
            # ships verbatim inside a JSON string — instead of corrupt numbers.
            result = await self._retrieve_content(hash_key, None)
            response_text = json.dumps(result, indent=2, allow_nan=False)
        # INFO: outcome — hash, whether the entry resolved, and the result size.
        # ``original_content`` (when present) must never reach the log; report a
        # boolean hit/miss and a char count instead of the payload.
        resolved = "error" not in result
        result_chars = len(json.dumps(result, ensure_ascii=False, default=str))
        logger.info(
            "event=mcp_retrieve_completed hash=%s resolved=%s result_chars=%d",
            hash_key,
            resolved,
            result_chars,
        )

        return [TextContent(type="text", text=response_text)]

    async def _handle_stats(self) -> list[TextContent]:
        """Handle furl_stats tool call."""
        # PERF-16: store.get_stats() and _read_shared_events() (an flock'd file
        # read) are synchronous I/O — build the aggregate off the event loop.
        stats = await asyncio.to_thread(self._compute_stats)
        return [TextContent(type="text", text=json.dumps(stats, indent=2))]

    def _compute_stats(self) -> dict[str, Any]:
        """Blocking stats aggregation (store + shared-file reads), off the loop."""
        stats = self._stats.to_dict()
        # One up-front contrast so the two scopes below are never conflated: the
        # flat top-level counters are THIS process only; the ``store`` block
        # (entries + hook_activity counters) is the shared cross-process picture.
        stats["scopes"] = (
            "top-level counters = THIS server process this session; "
            "'store' = shared across all sessions/processes on this project."
        )
        # Label the flat counters above as THIS-server-process scope so they are
        # never read as a session-wide total (Finding B): they count only what
        # this MCP server process itself compressed/retrieved. The live,
        # cross-process picture is the ``store`` block below.
        stats["process_scope"] = (
            "counters above (compressions, retrievals, total_*_tokens, "
            "savings_percent, estimated_cost_saved_usd) reflect ONLY what THIS "
            "MCP server process did this session — not the PostToolUse hook or "
            "sub-agents. See 'store' for the shared, cross-process picture."
        )

        # Store-derived, cross-process section. Route through _get_local_store()
        # (the accessor every other handler uses) so per-project isolation — the
        # deployed default — reports the ACTIVE namespace store.
        stats["store"] = self._store_derived_stats()

        # Aggregate cross-process stats (main session + sub-agents)
        my_pid = os.getpid()
        shared_events = _read_shared_events()
        other_events = [e for e in shared_events if e.get("pid") != my_pid]
        if other_events:
            other_compressions = [e for e in other_events if e.get("type") == "compress"]
            other_input = sum(e.get("input_tokens", 0) for e in other_compressions)
            other_output = sum(e.get("output_tokens", 0) for e in other_compressions)
            other_saved = max(0, other_input - other_output)
            stats["sub_agents"] = {
                "compressions": len(other_compressions),
                "retrievals": sum(1 for e in other_events if e.get("type") == "retrieve"),
                "tokens_saved": other_saved,
                "total_input_tokens": other_input,
                "total_output_tokens": other_output,
            }
            # Combined totals
            all_input = self._stats.total_input_tokens + other_input
            all_saved = self._stats.total_tokens_saved + other_saved
            stats["combined"] = {
                "total_compressions": self._stats.compressions + len(other_compressions),
                "total_tokens_saved": all_saved,
                "savings_percent": round(all_saved / all_input * 100, 1) if all_input > 0 else 0,
                "estimated_cost_saved_usd": round(all_saved * _cost_rate_per_mtok() / 1_000_000, 4),
            }

        return stats

    def _store_derived_stats(self) -> dict[str, Any]:
        """Live, store-derived stats for the SHARED CCR store (this namespace).

        Finding B: the process counters in ``SessionStats`` are structurally
        blind to the PostToolUse hook (a separate subprocess) and to sub-agents,
        so ``furl_stats`` used to report 0 while the shared sqlite store held real
        entries from the hook or prior turns — false reassurance. This block is
        computed LIVE from the shared store every call, so it reflects those
        entries even when THIS server process compressed nothing.

        Derived from ``_live_entries`` — the same expiry-filtered, lock-guarded
        snapshot ``furl_list`` / ``furl_search`` read — so the numbers agree with
        what those tools would show at this instant. ``max_entries`` comes from
        ``get_stats`` (which also prunes expired rows as a side benefit).
        """
        store = self._get_local_store()
        max_entries = store.get_stats().get("max_entries", 0)
        live = self._live_entries()
        now = time.time()

        total_original_bytes = sum(len(entry.original_content) for _, entry in live)
        total_compressed_bytes = sum(len(entry.compressed_content) for _, entry in live)
        total_original_tokens = sum(entry.original_tokens for _, entry in live)
        total_compressed_tokens = sum(entry.compressed_tokens for _, entry in live)
        tokens_saved = max(0, total_original_tokens - total_compressed_tokens)

        block: dict[str, Any] = {
            "scope": (
                "shared CCR store for this namespace — reflects entries written by "
                "ALL processes (this server, the PostToolUse hook, sub-agents), even "
                "when this server compressed nothing this session"
            ),
            # ``entries`` kept as the live count (byte-consistent with the block's
            # single snapshot); ``live_entries`` is the explicit, self-describing name.
            "entries": len(live),
            "live_entries": len(live),
            "max_entries": max_entries,
            "total_original_bytes": total_original_bytes,
            "total_compressed_bytes": total_compressed_bytes,
            "total_original_tokens": total_original_tokens,
            "total_compressed_tokens": total_compressed_tokens,
            "estimated_tokens_saved": tokens_saved,
            # Cross-process hook/pipe activity counters (persisted in this store).
            "hook_activity": self._hook_activity_block(store),
        }
        if live:
            ages = [now - entry.created_at for _, entry in live]
            block["oldest_entry_age_seconds"] = round(max(ages))
            block["newest_entry_age_seconds"] = round(min(ages))
        return block

    @staticmethod
    def _hook_activity_block(store: Any) -> dict[str, Any]:
        """Cross-process hook/pipe counters from the shared store (observability).

        Cumulative and monotonic — they survive entry eviction/expiry (unlike the
        live-entry stats above), so ``hook_invocations_seen`` rising while your
        context still shows RAW tool output is the durable signal that Claude Code
        is dropping the PostToolUse hook's replacement output
        (anthropics/claude-code#68951). ``hook_compressions_applied`` counts
        replacements Furl produced (and would have delivered if not dropped); a
        gap between the two is bucketed no-op reasons. The opt-in FURL_PRETOOL_PIPE
        path is unaffected by #68951 — its ``pipe_*`` counters appear only once it
        has run. Empty until the runtime furl-ctx ships the store counter API;
        FAIL-OPEN (never raises into furl_stats).
        """
        try:
            counters = store.get_counters()
        except Exception:
            counters = {}
        if not isinstance(counters, dict):
            counters = {}

        def _bucketed(prefix: str) -> dict[str, int]:
            return {
                name[len(prefix) :]: value
                for name, value in counters.items()
                if name.startswith(prefix)
            }

        block: dict[str, Any] = {
            "note": (
                "cumulative across all processes on this project, monotonic. "
                "invocations rising while output still looks raw → the harness "
                "is dropping replacements (anthropics/claude-code#68951)."
            ),
            "hook_invocations_seen": counters.get("hook_invocations_seen", 0),
            "hook_compressions_applied": counters.get("hook_compressions_applied", 0),
            "hook_noop_reasons": _bucketed("hook_noop:"),
        }
        pipe_seen = counters.get("pipe_invocations_seen", 0)
        pipe_applied = counters.get("pipe_compressions_applied", 0)
        pipe_noop = _bucketed("pipe_noop:")
        if pipe_seen or pipe_applied or pipe_noop:
            block["pipe_invocations_seen"] = pipe_seen
            block["pipe_compressions_applied"] = pipe_applied
            block["pipe_noop_reasons"] = pipe_noop
        return block

    async def _handle_purge(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle furl_purge — erase one entry (by hash) or the whole store.

        The data-erase escape hatch: offloaded originals otherwise persist for
        the session TTL. Requires EXACTLY ONE of ``hash`` / ``all`` — both or
        neither is a loud parameter error (API-15), never a silent no-op or an
        accidental wipe.
        """
        hash_arg = arguments.get("hash")
        all_arg = arguments.get("all", False)

        # ``all`` must be a real boolean — a truthy non-bool (e.g. "yes", 1) is a
        # caller bug, not an implicit wipe.
        if not isinstance(all_arg, bool):
            return _err(f"all parameter must be a boolean, got {type(all_arg).__name__}")

        has_hash = hash_arg is not None
        if has_hash and all_arg:
            return _err("provide exactly one of 'hash' or 'all', not both")
        if not has_hash and not all_arg:
            return _err(
                "provide exactly one of 'hash' (erase one entry) or 'all'=true (wipe the store)"
            )

        if all_arg:
            deleted, still_present = await asyncio.to_thread(self._purge_all)
            if still_present:
                return _err(
                    f"purge verification FAILED: {still_present} entr"
                    f"{'y is' if still_present == 1 else 'ies are'} still in the store "
                    "after clear(). No data was confirmed erased; retry or inspect the store."
                )
            plural = "y" if deleted == 1 else "ies"
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "purged": "all",
                            "deleted_count": deleted,
                            "note": f"Erased {deleted} stored entr{plural} from the CCR store.",
                        },
                        indent=2,
                    ),
                )
            ]

        # Single-hash path: same width/charset guard the retrieve ingress applies,
        # so a malformed key is a loud 400 here and never reaches the store.
        if not isinstance(hash_arg, str):
            return _err(f"hash parameter must be a string, got {type(hash_arg).__name__}")
        if not is_valid_ccr_hash(hash_arg):
            return _err("invalid hash format (expected 12 or 24 lowercase-hex chars)")
        hash_key = hash_arg.lower()

        deleted_one, nested_count, survivors, kept_shared = await asyncio.to_thread(
            self._purge_one, hash_key
        )
        deleted_total = (1 if deleted_one else 0) + nested_count
        if survivors:
            # Read-back verification failed (A1/RG6): something the cascade decided
            # to delete is STILL retrievable. Report it loudly, naming every
            # survivor, rather than claim success — a purge that does not purge is
            # the top data-safety bug in the audits, and an incomplete cascade is
            # invisible if only the top hash is re-checked.
            return _err(
                f"purge verification FAILED: {len(survivors)} entr"
                f"{'y is' if len(survivors) == 1 else 'ies are'} still retrievable "
                f"after delete: {', '.join(survivors)}. No data was confirmed erased; "
                "retry or inspect the store."
            )
        if nested_count:
            note = (
                f"Entry {hash_key} and {nested_count} nested offloaded "
                f"blob{'s' if nested_count != 1 else ''} erased from the CCR store "
                "(verified no longer retrievable)."
            )
        elif deleted_one:
            note = f"Entry {hash_key} erased from the CCR store (verified no longer retrievable)."
        else:
            note = (
                f"No entry {hash_key} in the store (already erased, evicted, or "
                "never stored); nothing to delete."
            )
        if kept_shared:
            # B2: a blob deliberately RETAINED because another live entry still
            # references it must be disclosed. Without this the agent reads
            # "verified no longer retrievable" while the content is still there,
            # which is exactly the false-erase claim the read-back exists to
            # prevent. The retention is correct (RG3); hiding it is not.
            note += (
                f" {len(kept_shared)} nested blob{'s' if len(kept_shared) != 1 else ''} "
                f"kept because another live entry still references "
                f"{'them' if len(kept_shared) != 1 else 'it'}: {', '.join(kept_shared)}."
            )
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "purged": "hash",
                        "hash": hash_key,
                        "found": deleted_one,
                        "deleted_count": deleted_total,
                        "nested_deleted": nested_count,
                        "nested_kept_shared": list(kept_shared),
                        "note": note,
                    },
                    indent=2,
                ),
            )
        ]

    def _purge_one(self, hash_key: str) -> tuple[bool, int, tuple[str, ...], tuple[str, ...]]:
        """Cascade-delete one entry via the library purge primitive (off-loop).

        ``CompressionStore.delete_cascade_detailed`` is exactly what
        ``furl_ctx.retrieve.purge`` delegates to; calling it on THIS server's
        store handle — the same one the retrieve path reads — guarantees a purged
        hash (and every nested ``<<ccr:HASH>>`` blob it owned) is no longer
        retrievable. A read-back with
        :meth:`CompressionStore.exists_any_tier` VERIFIES the erase actually took
        (A1: a purge that silently does not purge is the top data-safety bug in
        both audits).

        The read-back covers the FULL set the cascade decided to delete — the top
        hash AND every nested hash it removed (RG6). Verifying only the top hash
        could not detect an incomplete cascade, which is the exact failure the
        cascade exists to prevent. Hashes deliberately SKIPPED as still-shared
        (RG3) are not verified: they are meant to survive.

        It uses ``exists_any_tier``, not ``exists`` (review B3): ``exists`` is
        primary-only while ``retrieve`` falls back to the spill tier, so a
        primary-only read-back could report success on an entry that is still
        retrievable through the spill after a fail-open spill delete.

        Returns ``(top_deleted, nested_deleted_count, survivors, kept_shared)``:
        ``survivors`` names every hash that should be gone but is still
        retrievable (empty on success), and ``kept_shared`` names every nested
        blob deliberately RETAINED because another live entry still references it
        — the agent is told about those rather than left to infer an erase that
        did not happen (review B2).
        """
        store = self._get_local_store()
        outcome = store.delete_cascade_detailed(hash_key)
        # The named hash is always verified (even when it was already absent, so a
        # delete that silently no-ops on a live entry is still caught); the nested
        # hashes verified are the ones the cascade actually removed. dict.fromkeys
        # dedupes while keeping order -- the top hash appears in both sources.
        expected_gone = dict.fromkeys((hash_key, *outcome.deleted_hashes(hash_key)))
        survivors = tuple(h for h in expected_gone if store.exists_any_tier(h))
        return (
            outcome.top_deleted,
            len(outcome.nested_deleted),
            survivors,
            outcome.nested_shared_skipped,
        )

    def _purge_all(self) -> tuple[int, int]:
        """Wipe every entry; return ``(removed, still_present)`` (off-loop).

        Reads the live ``entry_count`` before ``clear()`` so the reported count
        reflects what was actually erasable (get_stats prunes expired rows first),
        then reads it back AFTER (reviewer #13): the single-hash path verifies its
        erase, and claiming "erased N entries" without checking is the same
        unverified claim on a wider blast radius. ``still_present`` is 0 on success.
        """
        store = self._get_local_store()
        count = int(store.get_stats().get("entry_count", 0))
        store.clear()
        return count, int(store.get_stats().get("entry_count", 0))

    async def _handle_search(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle furl_search — case-insensitive SUBSTRING search over originals.

        For when a ``<<ccr:HASH>>`` marker was lost: find stored originals by a
        substring of their content and get the hash back. Substring only (no
        regex — no injection/ReDoS surface). Output is bounded: at most ``limit``
        (<= 100) hits, each with a short redacted preview around the match.
        """
        query = arguments.get("query")
        if query is None:
            return _err("query parameter is required")
        if not isinstance(query, str):
            return _err(f"query parameter must be a string, got {type(query).__name__}")
        if not query.strip():
            return _err("query parameter must be a non-empty string")

        limit, err = _parse_bounded_limit(
            arguments.get("limit"), _SEARCH_LIST_LIMIT_DEFAULT, _SEARCH_LIST_LIMIT_CAP
        )
        if err is not None:
            return _err(err)
        assert limit is not None  # err is None ⇒ limit resolved

        result = await asyncio.to_thread(self._search_substring, query, limit)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    def _search_substring(self, query: str, limit: int) -> dict[str, Any]:
        """Blocking substring scan over live originals (runs off the loop)."""
        needle = query.lower()
        matches: list[dict[str, Any]] = []
        for hash_key, entry in self._live_entries():
            original = entry.original_content
            if needle not in original.lower():
                continue
            matches.append(
                {
                    "hash": hash_key,
                    "preview": _match_preview(original, needle),
                    "created_at": entry.created_at,
                    "size": len(original),
                }
            )
            if len(matches) >= limit:
                break
        return {
            "query": query,
            "count": len(matches),
            "limit": limit,
            "matches": matches,
            "note": (
                "Substring matches over stored originals (newest first). Call "
                "furl_retrieve with a hash for the full original content."
                if matches
                else "No stored original contains that substring (case-insensitive)."
            ),
        }

    async def _handle_list(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle furl_list — newest-first directory of stored CCR entries."""
        limit, err = _parse_bounded_limit(
            arguments.get("limit"), _SEARCH_LIST_LIMIT_DEFAULT, _SEARCH_LIST_LIMIT_CAP
        )
        if err is not None:
            return _err(err)
        offset, off_err = _parse_offset(arguments.get("offset"))
        if off_err is not None:
            return _err(off_err)
        assert limit is not None and offset is not None

        result = await asyncio.to_thread(self._list_entries, limit, offset)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    def _list_entries(self, limit: int, offset: int) -> dict[str, Any]:
        """Blocking newest-first entry listing (runs off the loop)."""
        live = self._live_entries()
        now = time.time()
        total = len(live)
        page = live[offset : offset + limit]
        entries = [
            {
                "hash": hash_key,
                "created_at": entry.created_at,
                # age + ttl alongside expires_in: "58m left" alone is
                # ambiguous — a fresh 1 h entry and a 23 h-old 24 h entry
                # read identically. (_humanize_ttl_remaining is a plain
                # whole-seconds humanizer, reused for all three.)
                "age": _humanize_ttl_remaining(now - entry.created_at),
                "ttl": _humanize_ttl_remaining(entry.ttl),
                "expires_in": _humanize_ttl_remaining(entry.ttl - (now - entry.created_at)),
                "size": len(entry.original_content),
                "content_kind": entry.tool_name,
                "preview": _list_preview(entry.original_content),
            }
            for hash_key, entry in page
        ]
        return {
            "count": len(entries),
            "total": total,
            "offset": offset,
            "limit": limit,
            "entries": entries,
            "note": (
                "Stored CCR entries, newest first. Retrieve one in full with "
                "furl_retrieve, or find by content with furl_search."
                if entries
                else "No stored entries in the current session window."
            ),
        }

    def _live_entries(self) -> list[tuple[str, Any]]:
        """Live ``(hash, entry)`` pairs, newest ``created_at`` first.

        Consumes the backend Protocol's ``items()`` — the same live-entry read
        ``store.search_all`` performs — and drops expired-but-unreaped rows
        against the wall clock. ``keys()`` is deliberately NOT used: it is not
        part of the ``CompressionStoreBackend`` protocol (ARCH-10), whereas
        ``items()`` is, so this stays backend-agnostic.

        The snapshot is taken UNDER the store's own lock, exactly as
        ``search_all`` does: without it, a concurrent ``store()``/``delete()``
        from another worker thread (tool calls now run off the loop) can mutate
        the backend mid-read — for the in-memory backend an unlocked
        ``list(dict.items())`` racing a write raises "dictionary changed size
        during iteration". The expiry filter and sort run on the local snapshot,
        outside the lock, so the lock is held only for the materialization."""
        store = self._get_local_store()
        now = time.time()
        with store._lock:
            snapshot = list(store._backend.items())
        live = [(hash_key, entry) for hash_key, entry in snapshot if not entry.is_expired(now)]
        live.sort(key=lambda pair: (pair[1].created_at, pair[0]), reverse=True)
        return live

    async def _handle_read(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle furl_read tool call — file read with session caching."""
        file_path = arguments.get("file_path", "")
        fresh = arguments.get("fresh", False)

        if not file_path:
            return _err("file_path parameter is required")

        # Parameter error for a non-string path — mirrors the hash guard; the
        # jail below must only ever see real path strings (API-15).
        if not isinstance(file_path, str):
            return _err(f"file_path parameter must be a string, got {type(file_path).__name__}")

        # PERF-16: the jail walk (resolve/openat), fstat, body read, fcntl-locked
        # stats append and store.store are all synchronous file I/O — run the
        # whole read off the event loop (the documented sqlite-backend invariant).
        return await asyncio.to_thread(self._read_file_sync, file_path, fresh)

    def _read_file_sync(self, file_path: str, fresh: bool) -> list[TextContent]:
        """Blocking body of :meth:`_handle_read` (runs off the loop)."""
        import hashlib

        from furl_ctx.redaction import build_store_redactor

        path = Path(file_path).expanduser().resolve()

        # Path jail: confine reads to the workspace root (resolve() above already
        # canonicalized symlinks, so a symlink pointing outside the root resolves
        # outside it and is rejected here). The check runs BEFORE any exists/stat
        # probe so an out-of-jail path cannot be used as a file-existence oracle.
        # Log the attempted path server-side; return a generic message to the
        # model channel (never echo the rejected path back).
        root = _workspace_root()
        if not path.is_relative_to(root):
            logger.warning(
                "event=mcp_read_path_rejected reason=outside_workspace attempted=%s root=%s",
                path,
                root,
            )
            return _err("path outside workspace")

        # Open ONCE and pin the file descriptor, then stat + read from that SAME
        # fd (TOCTOU defense): the old flow re-opened the path by name for the
        # size stat and again for the body read, so a swap between checks could
        # serve a different inode than the one validated. ``_open_jailed`` walks
        # every component from the workspace root with dir_fd + O_NOFOLLOW
        # (SEC-5: a single O_NOFOLLOW open guarded only the final component; a
        # directory component swapped to a symlink after resolve() escaped).
        # ``fstat`` on the fd drives the regular-file, hardlink, and size checks
        # so they describe exactly the inode we will read — no second lookup.
        try:
            fd = _open_jailed(path, root)
        except FileNotFoundError:
            # Missing path (or a final-component symlink removed between resolve
            # and open). Mirror the prior exists()-check message + path echo.
            return _err(f"File not found: {file_path}")
        except OSError as e:
            # Non-missing open failure: O_NOFOLLOW on a symlink raises ELOOP,
            # permission-denied raises EACCES, etc. Reserve "File not found" for a
            # genuine FileNotFoundError above; route everything else to the
            # generic read-error message (never confirm existence, never echo
            # errno detail to the model). Detail is logged server-side only.
            logger.warning(
                "event=mcp_read_open_failed errno=%s root=%s",
                getattr(e, "errno", None),
                root,
            )
            return _err("Cannot read file")

        # fstat the BARE fd first and run the type/link/size gates before any
        # read wrapper: os.fdopen(fd, "rb") raises IsADirectoryError on a
        # directory fd, so the S_ISREG gate has to happen on the raw fstat. The
        # fd is closed on every path — by os.fdopen's context manager once we
        # reach the read, by the explicit os.close otherwise.
        adopted_for_read = False
        try:
            st = os.fstat(fd)

            # os.open succeeds on a directory (S_ISREG is then False); the prior
            # is_file() guard surfaced that as "Not a file".
            if not stat.S_ISREG(st.st_mode):
                return _err(f"Not a file: {file_path}")

            # Reject a multiply-linked inode: an in-jail hardlink can point at an
            # out-of-jail inode and resolve() cannot see through a hardlink
            # (unlike a symlink), so is_relative_to alone would pass it. The
            # error string is honest about WHY (SEC-5): a legitimately
            # hardlinked in-jail file is rejected too, and telling its owner
            # "path outside workspace" was simply false.
            if st.st_nlink > 1:
                logger.warning(
                    "event=mcp_read_rejected reason=multiply_linked_inode nlink=%d root=%s",
                    st.st_nlink,
                    root,
                )
                return _err("hardlinked file rejected")

            # Reject oversized files via the fd's own size (OOM DoS guard) BEFORE
            # reading the body, so a file past the cap is never allocated. Read
            # the module global live so the cap stays patchable in tests.
            if st.st_size > _MAX_READ_BYTES:
                return _err(
                    f"File too large to read: {st.st_size} bytes (limit {_MAX_READ_BYTES} bytes)"
                )

            # Read from the pinned fd, bounded by the cap so an append after fstat
            # cannot blow the budget on the same descriptor. os.fdopen adopts the
            # fd; its `with` block closes it, so skip the finally-close path.
            adopted_for_read = True
            with os.fdopen(fd, "rb") as fh:
                raw = fh.read(_MAX_READ_BYTES + 1)
        except OSError as e:
            logger.warning(
                "event=mcp_read_failed errno=%s root=%s",
                getattr(e, "errno", None),
                root,
            )
            return _err("Cannot read file")
        finally:
            if not adopted_for_read:
                os.close(fd)

        if len(raw) > _MAX_READ_BYTES:
            return _err(
                f"File too large to read: >{_MAX_READ_BYTES} bytes (limit {_MAX_READ_BYTES} bytes)"
            )

        # Decode the bytes read from the pinned fd. Avoid lossy decode kwargs
        # in furl_ctx/ccr/ — use the centralized safe-log decoder (this path
        # is for tool output display, not SSE/wire path, so a replacement
        # char on invalid bytes is acceptable).
        content = _safe_decode_for_logging(raw)

        # Credential redaction (built-in patterns ON by default + FURL_REDACT_PATTERNS),
        # applied AFTER decode and BEFORE the hash / cache / store / served output —
        # furl_read stored and served the raw file verbatim, bypassing the redaction
        # the other store paths got (review F1). Redacting before the content_hash keeps
        # the file cache coherent: same file + same patterns hash identically
        # (cache hit), while a pattern change hashes differently and forces a
        # fresh (re-redacted) read. The served numbered output is scrubbed too,
        # consistent with the hook. Unset patterns => None => byte-identical.
        _read_redactor = build_store_redactor()
        if _read_redactor is not None:
            content = _read_redactor(content)

        content_hash = hashlib.sha256(content.encode()).hexdigest()[:24]
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        str_path = str(path)

        # Check cache (unless fresh=true)
        if not fresh and str_path in self._file_cache:
            cached_hash, ccr_hash, cached_lines, cached_tokens = self._file_cache[str_path]
            if cached_hash == content_hash:
                # File unchanged — but is the CCR entry still alive?
                store = self._get_local_store()
                if store.exists(ccr_hash):
                    # CCR alive — return cache marker. A hit AVOIDS tokens,
                    # it does not compress: record it under the dedicated
                    # cache counters (COR-36), never the compression totals.
                    self._stats.record_cache_hit(cached_tokens - 5)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "cached",
                                    "file": file_path,
                                    "lines": cached_lines,
                                    "unchanged": True,
                                    "hash": ccr_hash,
                                    "note": (
                                        f"File unchanged since first read ({cached_lines} lines, "
                                        f"~{cached_tokens} tokens). Content already in your context "
                                        f"from the first read. Call mcp__furl__{CCR_TOOL_NAME}(hash='{ccr_hash}') "
                                        f"if you need the full content again."
                                    ),
                                },
                                indent=2,
                            ),
                        )
                    ]
                # CCR expired — clear stale cache, fall through to fresh read
                del self._file_cache[str_path]
            # File changed — fall through to fresh read

        # Fresh read: store in CCR and cache the hash. Count tokens with the
        # same tokenizer the compress path uses (COR-36: a whitespace word
        # count is not a token count and understated every furl_read entry).
        from furl_ctx.tokenizers import get_tokenizer

        token_estimate = get_tokenizer(_MCP_TOKEN_MODEL).count_text(content)

        store = self._get_local_store()
        # require_durable (review F3, symmetry with furl_compress): the full
        # content is served below regardless — nothing is lost on a fresh read —
        # but a write that only reached volatile process memory must not seed
        # ``_file_cache``, or a later unchanged-read response would advertise
        # ``furl_retrieve(hash=...)`` backed by nothing durable.
        from furl_ctx.cache.compression_store import DurableWriteError

        try:
            ccr_hash = store.store(
                original=content,
                compressed=f"[File: {path.name}, {line_count} lines]",
                original_tokens=token_estimate,
                compressed_tokens=5,
                tool_name="furl_read",
                ttl=_mcp_session_ttl(),
                require_durable=True,
            )
        except DurableWriteError as exc:
            logger.warning("event=mcp_read_not_durable file=%s error=%s", path.name, exc)
        else:
            self._file_cache[str_path] = (content_hash, ccr_hash, line_count, token_estimate)

        # Return full content with line numbers (like Claude Code's Read tool)
        numbered_lines = []
        for i, line in enumerate(content.split("\n"), 1):
            numbered_lines.append(f"{i:>6}\t{line}")
        numbered_content = "\n".join(numbered_lines)

        return [
            TextContent(
                type="text",
                text=numbered_content,
            )
        ]

    async def run_stdio(self) -> None:
        """Run the server with stdio transport."""
        async with stdio_server() as (read_stream, write_stream):
            logger.info("Furl MCP Server starting (local CCR store)")
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )


async def main(argv: list[str] | None = None) -> None:
    """Run the Furl MCP server.

    ``argv`` defaults to ``sys.argv[1:]`` (the ``python -m furl_ctx.ccr.mcp_server``
    entry point); the ``furl mcp`` CLI launcher passes an explicit list so both
    the module entry point and the console script share one launch path.
    """
    parser = argparse.ArgumentParser(description="Furl MCP Server — Context engineering toolkit")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args(argv)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # Per-project CCR isolation (audit #4): scope this server's durable store to
    # the project it serves, so one machine-global ~/.furl DB cannot surface
    # project A's originals in project B or evict across projects. CLAUDE_PROJECT_DIR
    # (Claude Code's project root — stable across the main agent, its sub-agents,
    # and the compress hook) is preferred; cwd is the fallback. ``setdefault``
    # leaves a user free to force a shared store (FURL_CCR_NAMESPACE) or the
    # legacy global one (FURL_CCR_PROJECT_DIR="") without being overridden here.
    os.environ.setdefault(
        "FURL_CCR_PROJECT_DIR",
        os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
    )

    server = FurlMCPServer()

    await server.run_stdio()


if __name__ == "__main__":
    asyncio.run(main())
