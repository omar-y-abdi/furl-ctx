"""Furl MCP Server — Context engineering toolkit for AI coding tools.

Exposes Furl's compression, retrieval, and observability as MCP tools
that any MCP-compatible host (Claude Code, Cursor, Codex, etc.) can use.

Tools:
    furl_compress   — Compress content on demand
    furl_retrieve   — Retrieve original uncompressed content by hash
    furl_stats      — Session compression statistics

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

# Model this server compresses with — and therefore counts tokens with:
# _handle_read's original_tokens must come from the same tokenizer
# compress() uses (COR-36: a whitespace word count fed as a token count
# understates content by roughly the words-per-token factor).
_MCP_TOKEN_MODEL = "claude-sonnet-4-5-20250929"

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
# suites stay untouched — at an amortized conversation-scope cost of ~190
# o200k tokens. This is owner-approved alternative (b) of
# QUESTIONS-FOR-USER.md item 15; the original once-per-conversation carrier
# (``CCRToolInjector.inject_system_instructions``) was excised in SIMP-4,
# leaving server-level instructions as the out-of-band channel.
#
# Every decode claim below is grammar-verified against the reference
# decoder ``furl_ctx/transforms/csv_schema_decoder.py`` (the documented
# consumer contract for the Rust ``formatter.rs`` output); the executable
# pins live in ``tests/test_mcp_server_instructions.py``. The highest
# comprehension risk is ``%k``: a cell ``53`` under ``time_ms:float%3``
# decodes to 0.053, NOT 53.
CSV_DECODE_LEGEND = (
    "Compacted tables: `[N]{col:type,...}` = N rows; body lines = CSV rows "
    "of the remaining columns. "
    "type=V: constant V. "
    "int=B+S: row i = B+S*i (i from 0). "
    "float%k: cell/10^k (53 at %3 = 0.053, not 53). "
    "string~: full ISO timestamp first, then ±seconds[/tz] deltas. "
    "string^ + __affix:col=P,S: value = P+cell+S. "
    "string@ + __head:col=<d>h0,h1: cell 1<d>tail = h1+tail. "
    "__dict:col=v0,v1: cells index the list. "
    "= alone repeats the cell above. "
    "__null__ null, __missing__ absent key, ? nullable. "
    "<<ccr:HASH>> markers: dropped rows; retrieve via furl_retrieve."
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


# Session-scoped TTL: content persists for the session (1 hour), outlasting
# the library's own 30-minute default. The MCP server process lives as long
# as the coding session.
MCP_SESSION_TTL = 3600


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
            "savings_percent": savings_pct,
            "estimated_cost_saved_usd": cost_saved,
            "recent_events": self.events[-10:],
        }


class FurlMCPServer:
    """MCP Server exposing Furl's context engineering toolkit.

    Tools:
        furl_compress — Compress content on demand. Stores original for
                           retrieval.
        furl_retrieve — Retrieve original uncompressed content by hash
                           from the local CCR store.
        furl_stats    — Session statistics: compressions, savings, cost.

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
        # no version, so serverInfo would otherwise advertise the MCP SDK's
        # version (e.g. 1.28.1) as if it were Furl's. Report the furl-ctx
        # distribution version instead (``get_version`` is total — "unknown" when
        # the package is not installed, never raises).
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
        FIRST init, so this passes ``default_ttl=MCP_SESSION_TTL``: the
        pipeline run inside ``_compress_content`` persists dropped rows under
        the marker hash embedded in the compressed text WITHOUT an explicit
        ttl, and under the store's stock default (1800 s since Engine P0-3;
        300 s when this fix landed) those rows expired mid-session while the
        wrapper hash (stored with ``ttl=MCP_SESSION_TTL``) advertised session
        persistence. Sharing the session TTL as the store default keeps both
        retrieval surfaces alive for the same window — and at 3600 s the MCP
        store stays at least as durable as the library default. The compress
        path still passes its own per-entry ``ttl`` at store time.

        Backend (Engine P1-7): the MCP deployment defaults to the durable
        SQLite backend so originals survive a server restart and sub-agent
        processes can retrieve main-agent hashes through the shared workspace
        database file. ``FURL_CCR_BACKEND=memory`` opts back out (any other
        explicit value defers to the library's env-selected loader).
        """
        if self._local_store is None:
            from furl_ctx.cache.compression_store import get_compression_store

            self._local_store = get_compression_store(
                default_ttl=MCP_SESSION_TTL,
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

        # Acquire (and thereby configure) the store singleton BEFORE running
        # the pipeline: compress() persists marker-hash dropped rows through
        # its own no-arg get_compression_store() call, and the singleton's
        # default TTL is fixed on first init. Initializing here first
        # guarantees those embedded-marker entries carry MCP_SESSION_TTL
        # rather than the 1800 s pipeline default, which would silently
        # expire granular retrieval 30 minutes into a session-long window.
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

        result = compress(messages, model=_MCP_TOKEN_MODEL, pipeline=pipeline)

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

        # Store original in local store for later retrieval
        hash_key = store.store(
            original=content,
            compressed=compressed_content
            if isinstance(compressed_content, str)
            else json.dumps(compressed_content),
            original_tokens=input_tokens,
            compressed_tokens=output_tokens,
            compression_strategy="mcp_compress",
            ttl=MCP_SESSION_TTL,
        )

        # Track stats
        strategy = (
            ", ".join(result.transforms_applied) if result.transforms_applied else "passthrough"
        )
        self._stats.record_compression(input_tokens, output_tokens, strategy)

        tokens_saved = max(0, input_tokens - output_tokens)
        savings_pct = round(tokens_saved / input_tokens * 100, 1) if input_tokens > 0 else 0

        return {
            "compressed": compressed_content,
            "hash": hash_key,
            "original_tokens": input_tokens,
            "compressed_tokens": output_tokens,
            "tokens_saved": tokens_saved,
            "savings_percent": savings_pct,
            "transforms": result.transforms_applied,
            "note": f"Original stored with hash={hash_key}. Use mcp__furl__{CCR_TOOL_NAME} to get full content later.",
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
            hashes.append(part["hash"])
            transforms.extend(part.get("transforms", []))
            total_input += part["original_tokens"]
            total_output += part["compressed_tokens"]

        rendered = "\n".join(rendered_parts)
        tokens_saved = max(0, total_input - total_output)
        savings_pct = round(tokens_saved / total_input * 100, 1) if total_input > 0 else 0

        return {
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
            "note": (
                f"Pattern-filtered compression: {len(hashes)} eligible run(s) compressed, "
                f"protected lines passed through verbatim. Retrieve any run via "
                f"mcp__furl__{CCR_TOOL_NAME} with its hash."
            ),
        }

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
                        "retrieve individually); (2) pass hash with pattern/fields/line_range "
                        "to project just part of the original. Filters cannot be combined "
                        "with query."
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
                                    "combined with pattern/line_range."
                                ),
                            },
                        },
                    },
                ),
                Tool(
                    name=STATS_TOOL_NAME,
                    description=(
                        "Show compression statistics for this session: "
                        "total compressions, tokens saved, estimated cost savings, "
                        "and recent compression events."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
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

        # Reject oversized input before compressing it (OOM DoS guard). ``content``
        # is text, so the cap is measured in characters against the same byte-scale
        # ceiling used by furl_read — well above any realistic tool output.
        if len(content) > _MAX_READ_BYTES:
            return _err(
                f"Content too large to compress: {len(content)} chars "
                f"(limit {_MAX_READ_BYTES} chars)"
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

        # Add local store stats if available
        if self._local_store is not None:
            store_stats = self._local_store.get_stats()
            stats["store"] = {
                "entries": store_stats.get("entry_count", 0),
                "max_entries": store_stats.get("max_entries", 0),
            }

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
        ccr_hash = store.store(
            original=content,
            compressed=f"[File: {path.name}, {line_count} lines]",
            original_tokens=token_estimate,
            compressed_tokens=5,
            tool_name="furl_read",
            ttl=MCP_SESSION_TTL,
        )

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


async def main() -> None:
    """Run the Furl MCP server."""
    parser = argparse.ArgumentParser(description="Furl MCP Server — Context engineering toolkit")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    server = FurlMCPServer()

    await server.run_stdio()


if __name__ == "__main__":
    asyncio.run(main())
