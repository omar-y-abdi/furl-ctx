"""Headroom MCP Server — Context engineering toolkit for AI coding tools.

Exposes Headroom's compression, retrieval, and observability as MCP tools
that any MCP-compatible host (Claude Code, Cursor, Codex, etc.) can use.

Tools:
    headroom_compress   — Compress content on demand
    headroom_retrieve   — Retrieve original uncompressed content by hash
    headroom_stats      — Session compression statistics

Usage:
    # As standalone server (stdio transport, called by AI coding tools)
    headroom mcp serve

    # Add to Claude Code
    headroom mcp install

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

from headroom import paths as _paths
from headroom.ccr.marker_grammar import is_valid_ccr_hash

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

CCR_TOOL_NAME = "headroom_retrieve"
COMPRESS_TOOL_NAME = "headroom_compress"
STATS_TOOL_NAME = "headroom_stats"
READ_TOOL_NAME = "headroom_read"

logger = logging.getLogger("headroom.ccr.mcp")


def _safe_decode_for_logging(raw: bytes) -> str:
    """Decode bytes to a string for tool-output display.

    Uses an incremental UTF-8 decoder with the replacement character (U+FFFD)
    for invalid bytes — acceptable here because this path is for tool output
    display, not the SSE/wire path (lossy decode kwargs are forbidden
    in headroom/ccr/, so this centralizes the single legitimate lossy use).
    """
    import codecs as _codecs

    decoder = _codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(bytes(raw), final=True)


# Maximum bytes/chars a single tool call will read or ingest. Caps the
# headroom_read file read and the headroom_compress content input so a single
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


def _workspace_root() -> Path:
    """Return the resolved root that headroom_read file access is confined to.

    Resolution order (mirrors ``headroom.paths._env`` trim semantics):

    1. ``$HEADROOM_WORKSPACE_DIR`` (trimmed, tilde-expanded) when set to a
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
    env_value = os.environ.get(_paths.HEADROOM_WORKSPACE_DIR_ENV, "").strip()
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


# Feature flag: enable headroom_read tool (file read caching via CCR)
# Set HEADROOM_MCP_READ=on to enable
_READ_ENABLED = os.environ.get("HEADROOM_MCP_READ", "off").lower().strip() in (
    "on",
    "true",
    "1",
    "yes",
    "enabled",
)

# Session-scoped TTL: content persists for the session (1 hour), not 5 minutes.
# The MCP server process lives as long as the coding session.
MCP_SESSION_TTL = 3600

# Shared stats file: all MCP instances (main + sub-agents) append here.
# headroom_stats aggregates across all instances within the session window.
# Respects HEADROOM_WORKSPACE_DIR.
SHARED_STATS_DIR = _paths.workspace_dir()
SHARED_STATS_FILE = _paths.session_stats_path()
SESSION_WINDOW_SECONDS = 7200  # 2 hours — events older than this are pruned


def _append_shared_event(event: dict[str, Any]) -> None:
    """Append an event to the shared stats file (cross-process, file-locked)."""
    try:
        SHARED_STATS_DIR.mkdir(parents=True, exist_ok=True)
        event["pid"] = os.getpid()
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with open(SHARED_STATS_FILE, "a") as f:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass  # Never break compression because of stats


def _read_shared_events(window_seconds: int = SESSION_WINDOW_SECONDS) -> list[dict[str, Any]]:
    """Read shared events within the session time window, pruning old entries."""
    if not SHARED_STATS_FILE.exists():
        return []
    cutoff = time.time() - window_seconds
    events: list[dict[str, Any]] = []
    keep_lines: list[str] = []
    try:
        with open(SHARED_STATS_FILE) as f:
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_SH)
            lines = f.readlines()
            if _HAS_FCNTL:
                fcntl.flock(f, fcntl.LOCK_UN)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get("timestamp", 0) >= cutoff:
                    events.append(evt)
                    keep_lines.append(line + "\n")
            except json.JSONDecodeError:
                continue
        # Prune old entries (only if we dropped some)
        if len(keep_lines) < len(lines):
            try:
                with open(SHARED_STATS_FILE, "w") as f:
                    if _HAS_FCNTL:
                        fcntl.flock(f, fcntl.LOCK_EX)
                    f.writelines(keep_lines)
                    if _HAS_FCNTL:
                        fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                logger.debug("Shared-stats prune failed (non-fatal)", exc_info=True)
    except Exception:
        logger.debug("Shared-stats read failed (non-fatal)", exc_info=True)
    return events


@dataclass
class SessionStats:
    """Track compression statistics for the current MCP session."""

    compressions: int = 0
    retrievals: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens_saved: int = 0
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)

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
        self.events.append(event)
        _append_shared_event(event)
        # Keep last 50 events
        if len(self.events) > 50:
            self.events = self.events[-50:]

    def record_retrieval(self, hash_key: str) -> None:
        self.retrievals += 1
        event = {
            "type": "retrieve",
            "hash": hash_key[:12],
            "timestamp": time.time(),
        }
        self.events.append(event)
        _append_shared_event(event)
        if len(self.events) > 50:
            self.events = self.events[-50:]

    def to_dict(self) -> dict[str, Any]:
        savings_pct = (
            round((self.total_tokens_saved / self.total_input_tokens) * 100, 1)
            if self.total_input_tokens > 0
            else 0
        )
        # Rough cost estimate (blended rate ~$3/1M input tokens)
        cost_saved = round(self.total_tokens_saved * 3.0 / 1_000_000, 4)

        return {
            "session_duration_seconds": round(time.time() - self.started_at),
            "compressions": self.compressions,
            "retrievals": self.retrievals,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens_saved": self.total_tokens_saved,
            "savings_percent": savings_pct,
            "estimated_cost_saved_usd": cost_saved,
            "recent_events": self.events[-10:],
        }


class HeadroomMCPServer:
    """MCP Server exposing Headroom's context engineering toolkit.

    Tools:
        headroom_compress — Compress content on demand. Stores original for
                           retrieval.
        headroom_retrieve — Retrieve original uncompressed content by hash
                           from the local CCR store.
        headroom_stats    — Session statistics: compressions, savings, cost.

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

        self.server: Server = Server("headroom")
        self._setup_handlers()

    def _get_local_store(self) -> Any:
        """Get the shared compression store singleton (lazy init).

        Returns the same instance the compress path uses so retrieval can
        see content compressed in-process. The singleton's config is fixed on
        FIRST init, so this passes ``default_ttl=MCP_SESSION_TTL``: the
        pipeline run inside ``_compress_content`` persists dropped rows under
        the marker hash embedded in the compressed text WITHOUT an explicit
        ttl, and under the store's stock 300 s default those rows expired
        after 5 minutes while the wrapper hash (stored with
        ``ttl=MCP_SESSION_TTL``) advertised session persistence. Sharing the
        session TTL as the store default keeps both retrieval surfaces alive
        for the same window. The compress path still passes its own
        per-entry ``ttl`` at store time.
        """
        if self._local_store is None:
            from headroom.cache.compression_store import get_compression_store

            self._local_store = get_compression_store(default_ttl=MCP_SESSION_TTL)
        return self._local_store

    def _compress_content(self, content: str) -> dict[str, Any]:
        """Compress content using Headroom's pipeline.

        Returns dict with compressed text, token counts, hash, etc.
        """
        from headroom.compress import compress

        # Acquire (and thereby configure) the store singleton BEFORE running
        # the pipeline: compress() persists marker-hash dropped rows through
        # its own no-arg get_compression_store() call, and the singleton's
        # default TTL is fixed on first init. Initializing here first
        # guarantees those embedded-marker entries carry MCP_SESSION_TTL
        # rather than the 300 s pipeline default, which would silently expire
        # granular retrieval 5 minutes into a session-long window.
        store = self._get_local_store()

        # Wrap content as a tool message (most common compression target)
        messages = [{"role": "tool", "content": content}]

        result = compress(messages, model="claude-sonnet-4-5-20250929")

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
            "note": f"Original stored with hash={hash_key}. Use mcp__headroom__{CCR_TOOL_NAME} to get full content later.",
        }

    async def _retrieve_content(
        self,
        hash_key: str,
        query: str | None,
    ) -> dict[str, Any]:
        """Retrieve content from the local CCR store."""
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
        from headroom.cache.compression_store import format_retrieval_miss_detail

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
            "hint": "Content compressed via headroom_compress is stored for the "
            "session using the configured CCR TTL.",
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
                        f"The original is stored and can be retrieved later via mcp__headroom__{CCR_TOOL_NAME}. "
                        "Returns compressed text + a hash for retrieval."
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
                        },
                        "required": ["content"],
                    },
                ),
                Tool(
                    name=CCR_TOOL_NAME,
                    description=(
                        "Retrieve original uncompressed content by hash. "
                        "Use this when you need full details from previously compressed content. "
                        "The hash comes from headroom_compress results or from compression "
                        "markers like [N items compressed... hash=abc123]."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "hash": {
                                "type": "string",
                                "description": "Hash key from compression (e.g., 'abc123' from hash=abc123)",
                            },
                            "query": {
                                "type": "string",
                                "description": (
                                    "Optional search query to filter results. "
                                    "If provided, returns only items matching the query."
                                ),
                            },
                        },
                        "required": ["hash"],
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

            # Conditionally add headroom_read (behind feature flag)
            if _READ_ENABLED:
                tools.append(
                    Tool(
                        name=READ_TOOL_NAME,
                        description=(
                            "Read a file with smart caching. First read returns full content "
                            "and caches it. Subsequent reads of the same unchanged file return "
                            "a lightweight cache marker (~20 tokens instead of thousands). "
                            f"Use mcp__headroom__{CCR_TOOL_NAME} with the hash to get full content if needed. "
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
                    result = [
                        TextContent(
                            type="text",
                            text=json.dumps({"error": f"Unknown tool: {name}"}),
                        )
                    ]
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
                logger.error(f"Tool {name} failed: {e}", exc_info=True)
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": f"Internal error handling tool: {name}"}),
                    )
                ]

    async def _handle_compress(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle headroom_compress tool call."""
        content = arguments.get("content")
        if not content:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "content parameter is required"}),
                )
            ]

        # Reject oversized input before compressing it (OOM DoS guard). ``content``
        # is text, so the cap is measured in characters against the same byte-scale
        # ceiling used by headroom_read — well above any realistic tool output.
        if len(content) > _MAX_READ_BYTES:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": (
                                f"Content too large to compress: {len(content)} chars "
                                f"(limit {_MAX_READ_BYTES} chars)"
                            )
                        }
                    ),
                )
            ]

        # Run compression in thread pool (it's CPU-bound)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._compress_content, content)

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    async def _handle_retrieve(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle headroom_retrieve tool call."""
        hash_key = arguments.get("hash")
        if not hash_key:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "hash parameter is required"}),
                )
            ]

        # Same width+charset spoofing guard the tool-call parse path applies
        # (marker_grammar.is_valid_ccr_hash) — keep both ccr-hash ingress points
        # consistent. A malformed key is a loud 400 here, never reaches the store.
        if not is_valid_ccr_hash(hash_key):
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": "invalid hash format (expected 12 or 24 lowercase-hex chars)"}
                    ),
                )
            ]

        # Store keys are always lowercase (SHA-256 hexdigest output; store()
        # lowercases explicit hashes) while the format guard above is
        # case-insensitive — normalize at ingress so an upper/title-cased echo
        # of a marker hash HITS instead of missing with a confusing
        # "evicted/never stored" error.
        hash_key = hash_key.lower()

        query = arguments.get("query")
        # INFO: the hash is a content-address (validated 12/24-hex above), safe
        # to log; the query is a user-supplied search string and the result can
        # carry the retrieved ORIGINAL content — neither is logged verbatim. The
        # DEBUG line records whether a query was present and its length only.
        has_query = query is not None
        logger.info("event=mcp_retrieve_started hash=%s has_query=%s", hash_key, has_query)
        logger.debug(
            "event=mcp_retrieve_started_detail hash=%s query_len=%s",
            hash_key,
            len(query) if isinstance(query, str) else 0,
        )
        result = await self._retrieve_content(hash_key, query)
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
        """Handle headroom_stats tool call."""
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
                "estimated_cost_saved_usd": round(all_saved * 3.0 / 1_000_000, 4),
            }

        return [TextContent(type="text", text=json.dumps(stats, indent=2))]

    async def _handle_read(self, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle headroom_read tool call — file read with session caching."""
        import hashlib

        file_path = arguments.get("file_path", "")
        fresh = arguments.get("fresh", False)

        if not file_path:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "file_path parameter is required"}),
                )
            ]

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
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "path outside workspace"}),
                )
            ]

        # Open ONCE and pin the file descriptor, then stat + read from that SAME
        # fd (TOCTOU defense): the old flow re-opened the path by name for the
        # size stat and again for the body read, so a swap between checks could
        # serve a different inode than the one validated. ``O_NOFOLLOW`` refuses
        # a final-component symlink (the jail's resolve() already collapses
        # symlinks before is_relative_to, this is the belt-and-braces at open).
        # ``fstat`` on the fd drives the regular-file, hardlink, and size checks
        # so they describe exactly the inode we will read — no second lookup.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            # Missing path (or a final-component symlink removed between resolve
            # and open). Mirror the prior exists()-check message + path echo.
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"File not found: {file_path}"}),
                )
            ]
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
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "Cannot read file"}),
                )
            ]

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
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": f"Not a file: {file_path}"}),
                    )
                ]

            # Reject a multiply-linked inode: an in-jail hardlink can point at an
            # out-of-jail inode and resolve() cannot see through a hardlink
            # (unlike a symlink), so is_relative_to alone would pass it.
            if st.st_nlink > 1:
                logger.warning(
                    "event=mcp_read_rejected reason=multiply_linked_inode nlink=%d root=%s",
                    st.st_nlink,
                    root,
                )
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "path outside workspace"}),
                    )
                ]

            # Reject oversized files via the fd's own size (OOM DoS guard) BEFORE
            # reading the body, so a file past the cap is never allocated. Read
            # the module global live so the cap stays patchable in tests.
            if st.st_size > _MAX_READ_BYTES:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": (
                                    f"File too large to read: {st.st_size} bytes "
                                    f"(limit {_MAX_READ_BYTES} bytes)"
                                )
                            }
                        ),
                    )
                ]

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
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": "Cannot read file"}),
                )
            ]
        finally:
            if not adopted_for_read:
                os.close(fd)

        if len(raw) > _MAX_READ_BYTES:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": (
                                f"File too large to read: >{_MAX_READ_BYTES} bytes "
                                f"(limit {_MAX_READ_BYTES} bytes)"
                            )
                        }
                    ),
                )
            ]

        # Decode the bytes read from the pinned fd. Avoid lossy decode kwargs
        # in headroom/ccr/ — use the centralized safe-log decoder (this path
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
                    # CCR alive — return cache marker
                    self._stats.record_compression(cached_tokens, 5, "read_cache_hit")
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
                                        f"from the first read. Call mcp__headroom__{CCR_TOOL_NAME}(hash='{ccr_hash}') "
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

        # Fresh read: store in CCR and cache the hash
        store = self._get_local_store()
        ccr_hash = store.store(
            original=content,
            compressed=f"[File: {path.name}, {line_count} lines]",
            original_tokens=len(content.split()),
            compressed_tokens=5,
            tool_name="headroom_read",
            ttl=MCP_SESSION_TTL,
        )

        token_estimate = len(content.split())
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
            logger.info("Headroom MCP Server starting (local CCR store)")
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )

    async def cleanup(self) -> None:
        """Clean up resources (no-op; retained for lifecycle symmetry)."""
        return None


def create_ccr_mcp_server() -> HeadroomMCPServer:
    """Create a Headroom MCP server instance.

    Returns:
        HeadroomMCPServer instance.
    """
    return HeadroomMCPServer()


async def main() -> None:
    """Run the Headroom MCP server."""
    parser = argparse.ArgumentParser(
        description="Headroom MCP Server — Context engineering toolkit"
    )
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

    server = HeadroomMCPServer()

    try:
        await server.run_stdio()
    finally:
        await server.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
