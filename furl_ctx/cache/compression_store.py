"""Compression Store for CCR (Compress-Cache-Retrieve) architecture.

This module implements reversible compression: when SmartCrusher compresses
tool outputs, the original data is cached here for on-demand retrieval.

Key insight from research: REVERSIBLE compression beats irreversible compression.
If the LLM needs data that was compressed away, it can retrieve it — byte-exact,
but only within the in-memory window (<=1000 entries, <=1800s TTL). After eviction
or expiry the entry is gone and retrieval is a loud, cause-honest miss (never a
silent None). See CCR-RETENTION.md for the delivered guarantee vs. the open
durable-retention epic.

Features:
- Thread-safe in-memory storage with TTL expiration
- BM25-based search within cached content
- Local retrieval event tracking
- Automatic eviction when capacity is reached

Usage:
    store = get_compression_store()

    # Store compressed content
    hash_key = store.store(
        original=original_json,
        compressed=compressed_json,
        original_tokens=1000,
        compressed_tokens=100,
        tool_name="search_api",
    )

    # Retrieve later
    entry = store.retrieve(hash_key)

    # Or search within
    results = store.search(hash_key, "user query")
"""

from __future__ import annotations

import decimal
import hashlib
import heapq
import json
import logging
import math
import os
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import paths as _paths
from ..relevance.bm25 import BM25Scorer

if TYPE_CHECKING:
    from collections.abc import Callable

    from .backends import CompressionStoreBackend

logger = logging.getLogger(__name__)


class DurableWriteError(RuntimeError):
    """Raised by ``CompressionStore.store(..., require_durable=True)`` when the
    entry did NOT reach the durable backend within the lock-contention retry
    budget — the write fell open to a volatile in-process fallback (the durable
    backend is degraded, or a sibling process held the SQLite write lock for
    longer than the whole retry budget). The marker-decision caller catches it
    and vetoes to passthrough (serves the original uncompressed), so a
    ``<<ccr:HASH>>`` marker never ships for content whose only surviving copy is
    volatile and dies with the process (audit #3).

    ``hash_key`` is the key the entry IS stored under in the volatile tier: the
    original round-trips from THIS process right now
    (``store.retrieve(hash_key)``); it simply is not durable (gone on restart,
    invisible to other processes). Callers that surface a retrieval handle use
    it to stay honest — return the hash with a precise caveat — instead of
    dropping it and implying total loss when retrieval works this moment.

    Not raised for a non-durable backend (the default in-memory store): there
    the operator explicitly chose volatile storage, so there is no durability to
    lose and ``require_durable`` is a no-op.
    """

    def __init__(self, message: str, *, hash_key: str) -> None:
        super().__init__(message)
        self.hash_key = hash_key


# Session-scale default (Engine P0-3): agentic sessions routinely outlive
# 5 minutes, and an entry that expires mid-session silently converts
# "lossless + retrieval" into lossy. 1800 s (30 min) matches the Rust
# store's `DEFAULT_TTL` (crates/furl-core/src/ccr/mod.rs) — the two stores
# back the same markers, so their defaults must agree. Override via
# FURL_CCR_TTL_SECONDS (validated in `_get_env_default_ttl_seconds`).
DEFAULT_CCR_TTL_SECONDS = 1800
CCR_TTL_SECONDS_ENV = "FURL_CCR_TTL_SECONDS"

# Durable-write contention retry (store-concurrency-honesty). When a
# ``require_durable`` write reports non-durable — the shared file is briefly
# held by ANOTHER furl MCP server process (e.g. a second Claude Code session on
# the same project), or the backend lost its own per-op busy_timeout retry — the
# store re-attempts the durable persist a bounded number of times with capped
# exponential backoff BEFORE vetoing. Everyday two-session contention clears
# within this budget; only a lock held longer than the WHOLE budget (a hung or
# stale sibling) still vetoes, and then with an honest, cause-naming message.
#
# Worst-case ADDED wall-clock is the sum of the backoff sleeps
# (0.05 + 0.10 + 0.20 = 0.35 s) plus, under SUSTAINED contention, up to
# ``attempts`` more of the backend's own busy_timeout budget (~1.3 s each) — a
# few seconds total, far under the MCP tool-call timeout (Claude Code's default
# is 60 s). The happy path adds nothing: the first persist already succeeded.
_DURABLE_RETRY_MAX_ATTEMPTS = 3
_DURABLE_RETRY_BASE_BACKOFF_SECONDS = 0.05
_DURABLE_RETRY_MAX_BACKOFF_SECONDS = 0.20

# Minimum length for a caller-supplied ``explicit_hash``. This is the LOOSE
# recovery-floor contract, intentionally distinct from the STRICT consumer set
# ``marker_grammar.HASH_WIDTHS`` ({12, 24}) that the anti-spoofing ingress
# (the MCP retrieve handler, via ``marker_grammar.is_valid_ccr_hash``) enforces — see
# the "Two DISTINCT width contracts" note in ``ccr/marker_grammar.py``. The store
# must accept any hex key a DIRECT lookup can recover (shape I, the read-lifecycle
# marker, is recovered by direct store lookup and never by the strict scanner), so
# its floor is deliberately looser than the spoofing guard. The floor only rejects
# trivially-collidable sub-6 keys; every real producer emits exactly 12- or 24-char
# hashes, well clear of it.
_MIN_EXPLICIT_HASH_LEN = 6

_RETRIEVAL_LOG_PREVIEW_CHARS = 4096
# Preview-snippet length for cross-store search (``search_all``) hits. Short by
# design: the snippet is a disambiguation preview so the caller can pick a hash
# to retrieve in full, not a content channel. Redacted before truncation.
_CROSS_STORE_PREVIEW_CHARS = 200
# ReDoS guard (PERF/SEC). The credential regexes below — chiefly
# ``_SECRET_KEY_VALUE_RE`` — are O(N^2) on a long unbroken base64url/hex/minified
# run: a ``-`` sits INSIDE the ``[A-Z0-9_-]`` secret-key class yet is a word
# boundary, so ``\b([A-Z0-9_-]*KEYWORD...)`` opens O(N) anchor positions each
# scanning O(N). Empirically a 64 KB dashed run took ~82 s. Every preview/log
# surface keeps only a bounded head, so callers redact the kept window PLUS this
# margin (never the whole multi-MB original) — bounding the regex input to a
# constant. The margin lets a secret straddling the budget edge be seen whole
# and masked before truncation; real secrets (keys/tokens/URL passwords) fit
# inside it.
_REDACT_WINDOW_MARGIN_CHARS = 256
# Match ``<sensitive-key><sep><value>`` in both plain (``api_key=...``) and JSON
# quoted-key (``"api_key": "..."``) form. Group 2 allows an OPTIONAL closing quote
# before the separator: in JSON the key's own closing ``"`` sits between the key
# name and the ``:``, so without it the ``[:=]`` never abuts the key and the whole
# rule silently misses every JSON-embedded secret (the PRIMARY shape in tool
# output) — the value stayed un-redacted unless it independently matched the
# ``sk-`` rule below. Group 3 captures the value's optional opening quote so
# ``\1\2\3[REDACTED]`` preserves surrounding structure and only the value is cut.
# The value itself is CONDITIONAL on group 3 (SEC-4d): when an opening quote was
# captured, match to the closing quote (``\\.`` steps over JSON-escaped quotes;
# ``.`` stops at end-of-line, bounding an unterminated quote) — the old
# ``[^\"'\s,}]+`` class stopped at the first space, redacting ``correct`` and
# leaking ``horse battery staple``. Unquoted values keep the old class.
_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)\b([A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)[A-Z0-9_-]*)"
    r"([\"']?\s*[:=]\s*)([\"'])?(?(3)(?:\\.|(?!\3).)*|[^\"'\s,}]+)"
)
_AUTH_VALUE_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}")
_API_KEY_VALUE_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
# Provider-issued tokens that are recognizable by prefix alone, so they leak even
# when the surrounding key name is absent or unrecognized (a bare ``AKIA...`` /
# ``ghp_...`` in free text). Same prefix-anchored approach as ``_API_KEY_VALUE_RE``.
# AWS access-key IDs are ``AKIA`` + 16 uppercase alnum; GitHub tokens are
# ``gh[opsru]_`` + >=36 alnum. Deliberately NOT a generic long-hex/base64 rule:
# the retrieval log emits SHA-256 store hash keys throughout, and a blanket
# high-entropy rule would redact the log's own hashes and other benign IDs.
_PROVIDER_TOKEN_RE = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[opsru]_[A-Za-z0-9]{36,})\b")
# SEC-4a — URL userinfo credentials (``scheme://user:pass@host``). Only the
# password is cut; user and host survive so the log line stays operationally
# useful. The password class excludes ``/`` (a raw ``/`` cannot appear in URL
# userinfo, and admitting it made ``https://host:8080/x@y`` — a port plus an
# ``@`` later in the path — a false positive). Bounded quantifiers keep the
# scan cheap on the 4096-char preview.
_URL_CREDENTIAL_RE = re.compile(r"(://[^/?#\s:@]{1,128}:)([^@/\s]{1,256})@")
# SEC-4b — PEM private-key blocks (PKCS#8 ``BEGIN PRIVATE KEY`` plus the
# labelled RSA/EC/OPENSSH/ENCRYPTED variants). The WHOLE block goes, base64
# body and armor alike. ``[\s\S]*?`` spans real newlines and the two-char
# ``\n`` escapes of JSON-embedded keys. Public material (``BEGIN
# CERTIFICATE``, ``BEGIN PUBLIC KEY``) is not matched. The armor string is
# assembled at import so no verbatim PEM header sits in source (hook-safe,
# same trick as the redaction tests' ``sk-`` literal).
_PEM_ARMOR = "PRIVATE" + " KEY-----"
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]* " + _PEM_ARMOR + r"[\s\S]*?-----END[A-Z0-9 ]* " + _PEM_ARMOR
)
# SEC-4c — bare JWTs: ``eyJ`` (base64 of ``{"``) + two dot-joined base64url
# segments, no ``Bearer`` prefix and no sensitive key name required. The
# optional third segment covers both signed tokens and the trailing-dot
# unsecured form. Anchored on the ``eyJ`` magic so the store's own hex hash
# keys and ordinary prose can never match.
_BARE_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?")


def _get_env_default_ttl_seconds() -> int:
    raw_value = os.environ.get(CCR_TTL_SECONDS_ENV)
    if raw_value is None or not raw_value.strip():
        return DEFAULT_CCR_TTL_SECONDS

    try:
        ttl_seconds = int(raw_value)
    except ValueError:
        logger.warning(
            "%s must be a positive integer number of seconds, got %r; using %s "
            "(library-store fallback; the MCP server's own writes fall back to 3600 s "
            "separately — see furl_ctx.ccr.mcp_server._mcp_session_ttl)",
            CCR_TTL_SECONDS_ENV,
            raw_value,
            DEFAULT_CCR_TTL_SECONDS,
        )
        return DEFAULT_CCR_TTL_SECONDS

    if ttl_seconds <= 0:
        logger.warning(
            "%s must be greater than 0, got %s; using %s "
            "(library-store fallback; the MCP server's own writes fall back to 3600 s "
            "separately — see furl_ctx.ccr.mcp_server._mcp_session_ttl)",
            CCR_TTL_SECONDS_ENV,
            ttl_seconds,
            DEFAULT_CCR_TTL_SECONDS,
        )
        return DEFAULT_CCR_TTL_SECONDS

    return ttl_seconds


def format_retrieval_miss_detail(status: dict[str, Any]) -> str:
    """Return an operator-facing miss reason for CCR retrieval failures.

    The miss is always LOUD — the model receives this string as an explicit
    error (``success=False``), never a silent empty result or ``None``. That
    invariant is what keeps "no silent loss" true even when the store evicts.

    Cause honesty: an ``expired`` status has an exact cause (TTL elapsed), so we
    quote the TTL and age. A ``missing`` status is genuinely ambiguous without
    per-eviction tracking — the entry may have been evicted under capacity
    pressure, expired-then-reaped, or never stored — so we name every real cause
    instead of implying TTL alone (the old "Entry not found (CCR TTL: ...)"
    wording misattributed capacity evictions to the TTL, which is misleading for
    the common 1000-entry-overflow case).
    """
    default_ttl = status.get("default_ttl_seconds", DEFAULT_CCR_TTL_SECONDS)
    ttl_seconds = status.get("ttl_seconds", default_ttl)

    if status.get("status") == "expired":
        age_seconds = status.get("age_seconds")
        if isinstance(age_seconds, (int, float)):
            return f"Entry expired (CCR TTL: {ttl_seconds} seconds; age: {age_seconds:.0f} seconds)"
        return f"Entry expired (CCR TTL: {ttl_seconds} seconds)"

    max_entries = status.get("max_entries")
    capacity_note = f" (store capacity: {max_entries} entries)" if max_entries else ""
    return (
        f"Entry no longer retrievable from the CCR store: it was evicted under "
        f"capacity pressure{capacity_note}, expired (TTL {default_ttl}s), or was "
        f"never stored. Recompute the source content."
    )


def _redact_retrieval_log_payload(payload: str) -> str:
    # Order matters. PEM blocks and URL credentials go FIRST: they are
    # structural multi-token shapes, redacted whole before the generic rules
    # can chew on fragments of them. Then ``Bearer``/``Basic`` scheme tokens
    # BEFORE the secret-key rule so the scheme anchor survives — otherwise
    # ``_SECRET_KEY_VALUE_RE`` (which matches the ``Authorization`` key)
    # consumes the bare ``Bearer`` scheme word as its value, leaving the actual
    # credential after it un-redacted in a plain-text ``Authorization: Bearer
    # <JWT>`` header. The bare-JWT rule runs LAST as the catch-all for tokens
    # no earlier rule anchored on. Over-redaction is safe.
    redacted = _PEM_PRIVATE_KEY_RE.sub("[REDACTED]", payload)
    redacted = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", redacted)
    redacted = _AUTH_VALUE_RE.sub(r"\1 [REDACTED]", redacted)
    redacted = _SECRET_KEY_VALUE_RE.sub(r"\1\2\3[REDACTED]", redacted)
    redacted = _API_KEY_VALUE_RE.sub("sk-[REDACTED]", redacted)
    redacted = _PROVIDER_TOKEN_RE.sub("[REDACTED]", redacted)
    return _BARE_JWT_RE.sub("[REDACTED]", redacted)


def _payload_for_retrieval_log(payload: str) -> dict[str, Any]:
    # SLICE-before-REDACT (ReDoS guard, see ``_REDACT_WINDOW_MARGIN_CHARS``).
    # Redacting the FULL payload first is O(N^2) on unbroken base64url/hex runs
    # and this fires on EVERY retrieve (full-body log redact). The preview keeps
    # only ``_RETRIEVAL_LOG_PREVIEW_CHARS`` regardless, so redact just the bounded
    # window that feeds it. ``payload_chars`` still reports the FULL length, and
    # ``payload_truncated`` still reflects whether content was cut.
    window = payload[: _RETRIEVAL_LOG_PREVIEW_CHARS + _REDACT_WINDOW_MARGIN_CHARS]
    redacted_window = _redact_retrieval_log_payload(window)
    preview = redacted_window[:_RETRIEVAL_LOG_PREVIEW_CHARS]
    truncated = len(payload) > len(window) or len(redacted_window) > _RETRIEVAL_LOG_PREVIEW_CHARS
    return {
        "payload_chars": len(payload),
        "payload_preview_chars": len(preview),
        "payload_truncated": truncated,
        "payload_preview": preview,
    }


@dataclass
class CompressionEntry:
    """A cached compression entry with metadata for retrieval and feedback."""

    hash: str
    original_content: str
    compressed_content: str
    original_tokens: int
    compressed_tokens: int
    original_item_count: int
    compressed_item_count: int
    tool_name: str | None
    tool_call_id: str | None
    query_context: str | None
    created_at: float
    ttl: int = DEFAULT_CCR_TTL_SECONDS

    compression_strategy: str | None = None  # Strategy used for compression

    # Access tracking
    retrieval_count: int = 0
    search_queries: list[str] = field(default_factory=list)
    last_accessed: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        """Check if this entry has expired.

        ``now`` lets the owning store inject its clock (TEST-20: tests use a
        fake clock instead of real ``sleep``); ``None`` reads the wall clock.
        """
        reference = time.time() if now is None else now
        return reference - self.created_at > self.ttl

    def record_access(self, query: str | None = None) -> None:
        """Record an access to this entry for local access tracking."""
        self.retrieval_count += 1
        self.last_accessed = time.time()
        if query and query not in self.search_queries:
            self.search_queries.append(query)
            # Keep only last 10 queries
            if len(self.search_queries) > 10:
                self.search_queries = self.search_queries[-10:]


@dataclass
class RetrievalEvent:
    """Event logged when content is retrieved from cache."""

    hash: str
    query: str | None
    items_retrieved: int
    total_items: int
    tool_name: str | None
    timestamp: float
    retrieval_type: str  # "full" or "search"


@dataclass(frozen=True)
class CrossStoreMatch:
    """One ranked hit from a cross-store full-text search (``search_all``).

    Carries only what a caller needs to decide whether to follow up with a
    per-hash retrieve: the content-address ``hash``, the BM25 ``score`` used
    for ranking, and a short ``preview`` snippet of the entry's original
    content. The preview is REDACTED at source with the same rules the
    retrieval-log preview uses (``_redact_retrieval_log_payload``) so a
    cross-store search can never surface a credential a per-hash retrieval's
    log path would have masked. ``tool_name`` is the entry's originating tool
    (``None`` when unknown), included so a caller can disambiguate hits.
    """

    hash: str
    score: float
    preview: str
    tool_name: str | None


class CompressionStore:
    """Thread-safe store for compressed content with retrieval support.

    This is the core of the CCR architecture. When SmartCrusher compresses
    an array, the original content is stored here. If the LLM needs more
    data, it can retrieve from this cache instantly.

    Design principles:
    - Zero external dependencies (pure Python)
    - Thread-safe for concurrent access
    - TTL-based expiration (default 1800 seconds, env-configurable)
    - FIFO-by-creation eviction when capacity is reached (the oldest
      ``created_at`` is evicted first via a min-heap, NOT least-recently-used)
    - Built-in BM25 search for filtering

    Recovery scope (read this before relying on retrieval):
        Stored content is recoverable byte-exact only WITHIN the in-memory
        window: at most ``max_entries`` live entries (default 1000) and at most
        ``default_ttl`` seconds old (default 1800s). The store is single-tier —
        on capacity or TTL eviction the entry's payload is deleted outright
        (there is no spill to a durable tier), so a later ``retrieve()`` of an
        evicted/expired hash returns ``None``. That miss is never silent: the
        retrieval callers (e.g. the MCP ``furl_retrieve`` tool) surface it
        as an explicit, cause-honest error via ``format_retrieval_miss_detail``.
        The guarantee is "no SILENT loss," NOT "never evict." Retention beyond
        this window (a durable/session-scoped backend) is not built here.
    """

    def __init__(
        self,
        max_entries: int = 1000,
        default_ttl: int = DEFAULT_CCR_TTL_SECONDS,
        enable_feedback: bool = True,
        backend: CompressionStoreBackend | None = None,
        now_fn: Callable[[], float] | None = None,
        spill: CompressionStoreBackend | None = None,
        durable_retry_attempts: int = _DURABLE_RETRY_MAX_ATTEMPTS,
        durable_retry_base_backoff_seconds: float = _DURABLE_RETRY_BASE_BACKOFF_SECONDS,
        durable_retry_max_backoff_seconds: float = _DURABLE_RETRY_MAX_BACKOFF_SECONDS,
    ):
        """Initialize the compression store.

        Args:
            max_entries: Maximum number of entries to store.
            default_ttl: Default TTL in seconds.
            enable_feedback: Whether to track retrieval events.
            backend: Storage backend to use. Defaults to InMemoryBackend. A
                     durable ``SqliteBackend`` also ships
                     (``cache.backends.sqlite``, Engine P1-7): it is the MCP
                     server's default and opt-in elsewhere via
                     ``FURL_CCR_BACKEND=sqlite``. A backend only changes WHERE
                     entries live — it does NOT widen the recovery window:
                     eviction still removes the oldest entry at capacity
                     (durability != retention). The durable backend does keep
                     un-evicted entries across restarts and makes them
                     retrievable from other processes.
            now_fn: Clock used for entry timestamps and TTL-expiry checks
                    (TEST-20). Defaults to ``time.time``; tests inject a fake
                    clock so TTL cases advance time without real ``sleep``.
            spill: Optional durable SPILL tier (Q10 retention). When set, an
                   entry evicted from ``backend`` under capacity pressure is
                   DEMOTED to this backend (best-effort) instead of being lost,
                   and ``retrieve`` falls through primary→spill so a
                   ``<<ccr:HASH>>`` marker stays resolvable past the in-memory
                   eviction window. Default ``None`` keeps single-tier behavior
                   byte-identical: no spill site fires, retrieval never consults
                   a spill. Reuses the ``SqliteBackend`` (its own cap + TTL
                   backstop bound the spill); a spill read/write error is
                   fail-open (logged, never breaks the primary or compression).
                   Retrieval from spill is read-only — the entry is NOT promoted
                   back into ``backend`` and its access bookkeeping is untouched,
                   so a spill hit is byte-identical to the value that was evicted.
            durable_retry_attempts: Extra durable-persist attempts after the
                   first, tried under capped-backoff before a ``require_durable``
                   write vetoes (store-concurrency-honesty). Absorbs everyday
                   cross-process (two-session) SQLite lock contention. 0 disables
                   the store-level retry (the backend keeps its own per-op one).
            durable_retry_base_backoff_seconds: Base sleep between retries
                   (doubles each attempt, capped by the max below).
            durable_retry_max_backoff_seconds: Cap on the per-retry sleep.
        """
        # Import here to avoid circular imports
        from .backends import InMemoryBackend

        self._backend: CompressionStoreBackend = backend or InMemoryBackend()
        self._spill: CompressionStoreBackend | None = spill
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._enable_feedback = enable_feedback
        self._now: Callable[[], float] = now_fn or time.time

        if durable_retry_attempts < 0:
            raise ValueError(f"durable_retry_attempts must be >= 0, got {durable_retry_attempts!r}")
        if durable_retry_base_backoff_seconds < 0 or durable_retry_max_backoff_seconds < 0:
            raise ValueError("durable retry backoff seconds must be >= 0")
        self._durable_retry_attempts = durable_retry_attempts
        self._durable_retry_base_backoff_seconds = durable_retry_base_backoff_seconds
        self._durable_retry_max_backoff_seconds = durable_retry_max_backoff_seconds

        # Local retrieval-event tracking
        self._retrieval_events: list[RetrievalEvent] = []
        self._max_events = 1000  # Keep last 1000 events

        # Use a min-heap for O(log n) eviction instead of O(n).
        # Heap entries are (created_at, hash_key) tuples
        self._eviction_heap: list[tuple[float, str]] = []
        # CRITICAL FIX: Track stale entries count to know when heap cleanup is needed
        self._stale_heap_entries = 0
        # Threshold for triggering heap rebuild (when 50% are stale)
        self._heap_rebuild_threshold = 0.5

        # BM25 scorer for search
        self._scorer = BM25Scorer()

    @property
    def default_ttl_seconds(self) -> int:
        """Default TTL applied to new entries when callers do not override it."""
        return self._default_ttl

    def store(
        self,
        original: str,
        compressed: str,
        *,
        original_tokens: int = 0,
        compressed_tokens: int = 0,
        original_item_count: int = 0,
        compressed_item_count: int = 0,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        query_context: str | None = None,
        compression_strategy: str | None = None,
        ttl: int | None = None,
        explicit_hash: str | None = None,
        require_durable: bool = False,
    ) -> str:
        """Store compressed content and return hash for retrieval.

        Args:
            original: Original JSON content before compression.
            compressed: Compressed JSON content.
            original_tokens: Token count of original content.
            compressed_tokens: Token count of compressed content.
            original_item_count: Number of items in original array.
            compressed_item_count: Number of items after compression.
            tool_name: Name of the tool that produced this output.
            tool_call_id: ID of the tool call.
            query_context: User query context for relevance matching.
            compression_strategy: Strategy used for compression.
            ttl: Custom TTL in seconds (uses default if not specified).
            explicit_hash: Use this exact hex hash as the storage key
                instead of computing SHA-256(original)[:24]. Required when
                the marker that points at this entry was emitted by a
                producer with its own hash function (e.g. SmartCrusher's
                Rust row-drop path uses SHA-256[:12]). If not a hex
                string, raises ``ValueError``. The marker hash and the
                store key MUST match — otherwise a ``furl_retrieve`` of the
                marker hash misses even though the data is present.
            require_durable: When True and the configured backend is durable
                (exposes ``set_durable``), raise ``DurableWriteError`` if the
                write only reached the volatile fallback (backend degraded or
                lock-contention retry lost). Marker-decision callers pass True so
                a lost durable write vetoes the ``<<ccr:HASH>>`` marker instead
                of shipping one whose original dies with the process (audit #3).
                No-op for the in-memory backend (nothing durable to lose).

        Returns:
            Hash key for retrieving this content. On a true hash collision
            (same key, different live content) the store DROPS the ambiguous
            binding — the stored entry is deleted, the new one refused, and the
            collision logged at ERROR — so a later ``retrieve`` of the key is a
            LOUD, cause-honest miss (recompute) rather than a silent resolution
            to the other producer's foreign content. The key is still returned
            (signature unchanged); its marker simply no longer resolves.
        """
        # Reject a non-positive TTL loudly. ttl=0 (or negative) produces an
        # entry that is_expired() immediately (time.time()-created_at > 0), so it
        # would be stored in the backend + heap, never retrievable, and leak until
        # the next store() reaps it. No live caller passes ttl<=0; this is an
        # invalid input only reachable via direct API misuse. Reject (matching the
        # explicit_hash style) rather than silently clamp, which would mask the
        # caller bug. ttl=None (use default) and ttl>0 are unaffected.
        if ttl is not None and ttl <= 0:
            raise ValueError(
                f"ttl must be a positive number of seconds (or None for the default), "
                f"got {ttl!r} — a non-positive ttl creates an immediately-expired entry"
            )

        # Generate hash from original content. Default: SHA-256[:24] of the
        # original. When the caller provides `explicit_hash`, use it
        # verbatim — required when the hash that ends up in the prompt
        # marker is produced by another component (e.g. the Rust
        # SmartCrusher row-drop path emits SHA-256[:12], which the
        # Python store has to mirror so the MCP furl_retrieve tool resolves it).
        # 24 chars (96 bits) was chosen for collision resistance under the
        # birthday bound: 50% collision probability at ~280 trillion entries
        # (2^48), versus ~4 billion (2^32) for the previous 16-char default.
        if explicit_hash is not None:
            # Validate as hex and bail LOUDLY on a bad key: silently falling back
            # to the computed default when the caller asked for a specific key
            # would break the marker<->store consistency the recovery plane needs.
            if not explicit_hash or not all(c in "0123456789abcdefABCDEF" for c in explicit_hash):
                raise ValueError(
                    f"explicit_hash must be a non-empty hex string, got {explicit_hash!r}"
                )
            # Reject trivially-collidable short keys (e.g. a 1-char hash). This is
            # the loose recovery floor (``_MIN_EXPLICIT_HASH_LEN``), intentionally
            # looser than the strict {12, 24} anti-spoofing ingress — the store
            # must accept every hex key a DIRECT lookup can recover, not only the
            # strict scanner's widths. Real producers emit 12- or 24-char hashes,
            # well clear of the floor.
            if len(explicit_hash) < _MIN_EXPLICIT_HASH_LEN:
                raise ValueError(
                    f"explicit_hash must be at least {_MIN_EXPLICIT_HASH_LEN} hex chars "
                    f"(collidable below that), got {explicit_hash!r} ({len(explicit_hash)} chars)"
                )
            hash_key = explicit_hash.lower()
        else:
            # SHA-256 truncated to 24 hex chars (96 bits) — same collision
            # space as the MD5[:24] this replaced. Switched from MD5
            # to silence CodeQL's `py/weak-sensitive-data-hashing`
            # rule (the `usedforsecurity=False` parameter and the `lgtm`
            # comment marker both failed to suppress it). The cache is
            # in-memory, so changing the hash function on upgrade has no
            # persistence-side effect — the same content always hashes
            # deterministically under whichever function is in use.
            # ``surrogatepass`` keeps this path total: a lone-surrogate
            # original (JSON delivers them via \uD800 escapes, reachable
            # through the MCP furl_compress tool) hashes instead of raising
            # UnicodeEncodeError. For every valid-UTF8 original the emitted
            # bytes are identical to a strict encode, so existing keys are
            # unchanged.
            hash_key = hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:24]

        entry = CompressionEntry(
            hash=hash_key,
            original_content=original,
            compressed_content=compressed,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            original_item_count=original_item_count,
            compressed_item_count=compressed_item_count,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            query_context=query_context,
            created_at=self._now(),
            ttl=ttl if ttl is not None else self._default_ttl,
            compression_strategy=compression_strategy,
        )

        with self._lock:
            self._evict_if_needed()

            # Hash collision handling. If the key already exists with
            # DIFFERENT content it is a true hash collision (astronomically
            # rare at 96-/48-bit keys). Both producers' ``<<ccr:HASH>>`` markers
            # now point at the SAME key, and retrieval — a bare
            # ``backend.get(hash_key)`` with no per-marker content identity —
            # cannot tell them apart. Serving the stored entry would hand the
            # SECOND producer the FIRST producer's bytes: foreign content, i.e.
            # silent corruption, the exact outcome this store exists to prevent.
            # We cannot safely serve EITHER binding, so we DROP the binding
            # entirely: delete the stored entry and refuse the new one. Every
            # marker on this key then resolves to a LOUD, cause-honest miss
            # (recompute) instead of foreign content. An expired same-key entry
            # never reaches this branch: _evict_if_needed() above already reaped
            # it, so a dead binding cannot wedge its key.
            existing = self._backend.get(hash_key)
            if existing is not None:
                if existing.original_content != original:
                    logger.error(
                        "Hash collision detected: hash=%s tool=%s (existing_len=%d, "
                        "new_len=%d) — dropping the ambiguous binding; NEITHER content "
                        "is served (both markers now loud-miss) rather than resolving "
                        "to foreign content",
                        hash_key,
                        tool_name,
                        len(existing.original_content),
                        len(original),
                    )
                    # Drop the stored entry so retrieve() loud-misses instead of
                    # serving foreign bytes; its heap tuple is now stale.
                    self._backend.delete(hash_key)
                    self._stale_heap_entries += 1
                    return hash_key
                # Same content being stored again - this is fine, just update
                logger.debug(
                    "Duplicate store for hash=%s, updating entry",
                    hash_key,
                )
                # Mark old heap entry as stale since we're replacing
                self._stale_heap_entries += 1

            durable = self._persist_and_report_durability(hash_key, entry)
            # Add to eviction heap for O(log n) eviction
            heapq.heappush(self._eviction_heap, (entry.created_at, hash_key))

        # Contention retry BEFORE the veto (store-concurrency-honesty). A durable
        # write that reported non-durable most often lost a brief cross-process
        # lock race — a second furl MCP server (another Claude Code session)
        # writing the shared file. Re-attempt the persist under a bounded,
        # capped-backoff budget (sleeps OUTSIDE the lock) so everyday two-session
        # contention lands durably instead of spuriously vetoing.
        if require_durable and not durable:
            durable = self._retry_durable_persist(hash_key, entry)

        # Durability veto (audit #3), raised OUTSIDE the lock, only once the whole
        # retry budget is spent. The entry stays in the volatile tier so
        # SAME-PROCESS retrieval still works right now (hence the hash rides the
        # error); it simply is not durable, so a marker-decision caller reverts to
        # the ORIGINAL uncompressed content (nothing lost) and a hash-surfacing
        # caller reports the volatile handle honestly rather than implying loss.
        if require_durable and not durable:
            raise DurableWriteError(
                f"CCR durable write for hash {hash_key} did not reach durable "
                f"SQLite storage within the lock-contention retry budget "
                f"({1 + self._durable_retry_attempts} attempts). The original IS "
                f"in this process's volatile in-memory tier — retrievable now via "
                f"this same server (store.retrieve / furl_retrieve) — but it will "
                f"NOT survive a restart of this server and is invisible to other "
                f"furl processes. Likely cause: another furl MCP server process — "
                f"possibly a second, live or stale, Claude Code session on this "
                f"project — holds the store's SQLite write lock (or the backend "
                f"degraded). See LIBRARY.md “Multiple sessions on one "
                f"project”.",
                hash_key=hash_key,
            )
        return hash_key

    def _persist_and_report_durability(self, hash_key: str, entry: CompressionEntry) -> bool:
        """Write ``entry`` and report whether it reached a DURABLE backend.

        A backend that distinguishes durable from volatile writes exposes
        ``set_durable`` (the ``SqliteBackend`` does); its bool is returned
        verbatim. A backend without it (the in-memory default) is treated as
        durability-satisfied — the operator chose volatile storage, so
        ``require_durable`` has nothing to veto. Must be called with the store
        lock held.
        """
        set_durable = getattr(self._backend, "set_durable", None)
        if set_durable is not None:
            return bool(set_durable(hash_key, entry))
        self._backend.set(hash_key, entry)
        return True

    def _retry_durable_persist(self, hash_key: str, entry: CompressionEntry) -> bool:
        """Re-attempt the durable persist under a bounded, capped-backoff budget.

        Returns ``True`` as soon as a re-attempt lands the row durably (the
        contention cleared), else ``False`` after ``durable_retry_attempts``
        tries — the caller then vetoes. Backoff sleeps happen OUTSIDE the store
        lock; each persist re-acquires it (the ``_persist_and_report_durability``
        contract). Re-persisting the same ``(hash_key, entry)`` is idempotent —
        the heap already holds the key and the backend upserts — so a healed
        retry never double-counts.
        """
        for attempt in range(1, self._durable_retry_attempts + 1):
            backoff = min(
                self._durable_retry_base_backoff_seconds * (2 ** (attempt - 1)),
                self._durable_retry_max_backoff_seconds,
            )
            if backoff > 0:
                time.sleep(backoff)
            with self._lock:
                if self._persist_and_report_durability(hash_key, entry):
                    return True
        return False

    def retrieve(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
    ) -> CompressionEntry | None:
        """Retrieve original content by hash.

        Args:
            hash_key: Hash key returned by store().
            query: Optional query for retrieval-event tracking.
            record_feedback_signal: When True (default), a hit feeds one
                signal into the local retrieval-feedback loop (Engine P2-13)
                keyed by the entry's compression metadata. Engine-INTERNAL
                verification reads — the CCR-offload round-trip and the
                CCR-mirror backing check — pass False so the loop learns only
                from real (model-driven) retrievals; they otherwise keep the
                store's pre-existing bookkeeping (retrieval_count, event log)
                unchanged.

        Returns:
            CompressionEntry if found and not expired, ``None`` otherwise.
            ``None`` means the hash missed the in-memory window — it was never
            stored, was evicted under capacity pressure (oldest-created-first),
            or its TTL expired and it was deleted. Recovery is window-scoped
            (<=``max_entries``, <=``default_ttl`` seconds), not unbounded. A
            ``None`` here is not a silent loss: retrieval callers turn it into an
            explicit (loud) miss via ``format_retrieval_miss_detail``.
        """
        with self._lock:
            entry = self._backend.get(hash_key)

            if entry is None:
                # Primary miss: fall through to the durable spill tier (Q10). A
                # spill hit is returned as-is (no promotion back into the
                # primary, no access bookkeeping) so it is byte-identical to the
                # evicted value. Spill-off (``self._spill is None``)
                # short-circuits to today's loud miss.
                return self._recover_from_spill(hash_key)

            if entry.is_expired(self._now()):
                self._backend.delete(hash_key)
                # CRITICAL FIX: Track stale heap entry
                self._stale_heap_entries += 1
                return None

            # Track access on the entry
            entry.record_access(query)
            # Update the backend with the modified entry
            self._backend.set(hash_key, entry)

            # Log retrieval event
            if self._enable_feedback:
                self._log_retrieval(
                    hash_key=hash_key,
                    query=query,
                    items_retrieved=entry.original_item_count,
                    total_items=entry.original_item_count,
                    tool_name=entry.tool_name,
                    retrieval_type="full",
                )
            self._log_retrieval_payload(
                hash_key=hash_key,
                query=query,
                retrieval_type="full",
                payload=entry.original_content,
                items_retrieved=entry.original_item_count,
                total_items=entry.original_item_count,
                entry=entry,
            )

            # CRITICAL: Make a deep copy to return
            # (entry could be modified/evicted after lock release)
            # The entry contains mutable fields (search_queries list) that must be copied
            result_entry = replace(entry, search_queries=list(entry.search_queries))

        # Engine P2-13: a real hit is the retrieval-feedback signal — the
        # model needed content this entry's compression dropped. Emitted
        # OUTSIDE the store lock (the aggregator's lock is a leaf; the store
        # never nests into it) and only for model-driven retrievals (see the
        # ``record_feedback_signal`` doc above). Misses and expired entries
        # returned ``None`` earlier and never reach this point.
        if record_feedback_signal and self._enable_feedback:
            self._emit_retrieval_signal(result_entry.tool_name, result_entry.compression_strategy)

        return result_entry

    def get_metadata(
        self,
        hash_key: str,
    ) -> dict[str, Any] | None:
        """Get metadata about a stored entry without retrieving full content.

        Useful for context tracking to know what was compressed without
        fetching the entire original content.

        Args:
            hash_key: Hash key returned by store().

        Returns:
            Dict with metadata if found and not expired, None otherwise.
        """
        with self._lock:
            entry = self._backend.get(hash_key)

            if entry is None:
                return None

            if entry.is_expired(self._now()):
                self._backend.delete(hash_key)
                self._stale_heap_entries += 1
                return None

            return {
                "hash": entry.hash,
                "tool_name": entry.tool_name,
                "original_item_count": entry.original_item_count,
                "compressed_item_count": entry.compressed_item_count,
                "query_context": entry.query_context,
                "compressed_content": entry.compressed_content,
                "created_at": entry.created_at,
                "ttl": entry.ttl,
            }

    def search(
        self,
        hash_key: str,
        query: str,
        max_results: int = 20,
        score_threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Search within cached content using BM25.

        Args:
            hash_key: Hash key of cached content.
            query: Search query.
            max_results: Maximum number of results to return.
            score_threshold: Minimum BM25 score to include.

        Returns:
            List of matching items from original content.
        """
        # Get entry without logging or access-bumping (results aren't known yet)
        entry = self._get_entry_for_search(hash_key)
        if entry is None:
            return []

        items = self._search_items_from_original(entry.original_content)

        if not items:
            return []

        # Score each item using BM25
        item_strs = [json.dumps(item, default=str) for item in items]
        scores = self._scorer.score_batch(item_strs, query)

        # Filter and sort by score
        scored_items = [
            (items[i], scores[i].score)
            for i in range(len(items))
            if scores[i].score >= score_threshold
        ]
        scored_items.sort(key=lambda x: x[1], reverse=True)

        results = [item for item, _ in scored_items[:max_results]]

        # COR-37: record the access only AFTER results are known, and only
        # when the search actually returned items. A zero-result probe must
        # not bump retrieval_count — the MCP retrieve path documents that
        # retrieval metrics reflect ACTUAL retrievals (its no-match branch
        # uses the side-effect-free exists() on the same rationale). The
        # retrieval-EVENT log below still records zero-result probes with an
        # honest items_retrieved=0.
        if results:
            self._record_search_access(hash_key, query)

        # Log retrieval event
        if self._enable_feedback:
            with self._lock:
                self._log_retrieval(
                    hash_key=hash_key,
                    query=query,
                    items_retrieved=len(results),
                    total_items=len(items),
                    tool_name=entry.tool_name,
                    retrieval_type="search",
                )
        self._log_retrieval_payload(
            hash_key=hash_key,
            query=query,
            retrieval_type="search",
            payload=json.dumps(results, ensure_ascii=False),
            items_retrieved=len(results),
            total_items=len(items),
            entry=entry,
        )

        return results

    def search_all(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
    ) -> list[CrossStoreMatch]:
        """Full-text search across ALL live entries, ranked by BM25.

        Unlike :meth:`search` (which searches WITHIN one hash's original),
        this ranks every live entry as a single document against ``query`` and
        returns the top ``max_results`` as ``CrossStoreMatch`` records —
        ``(hash, score, preview, tool_name)`` — so the caller can follow up
        with a per-hash :meth:`retrieve`. With the durable SQLite backend this
        spans cross-session / cross-process entries (they live in the shared
        file), so a query can surface originals another agent stored.

        Redaction: each ``preview`` is passed through the same credential
        redaction the retrieval-log preview uses, so a cross-store search never
        leaks a secret a per-hash retrieval's log path would have masked. The
        BM25 SCORE is computed over the raw original (a float, not content — no
        leak) so ranking quality is unaffected by redaction.

        Expiry: TTL is the store's responsibility, and ``backend.items()``
        returns expired-but-unreaped rows, so each candidate is filtered
        through ``is_expired`` against the store clock — an evicted/expired
        entry can never appear in results.

        This is a pure read: it neither bumps ``retrieval_count`` nor logs a
        retrieval event (nothing is actually retrieved — the caller retrieves
        by hash next), mirroring the side-effect-free ``_get_entry_for_search``
        rationale (COR-37).

        Args:
            query: Free-text search query.
            max_results: Maximum number of ranked hits to return.
            score_threshold: Minimum BM25 score to include (default 0.0 —
                any positive-scoring match qualifies; a term must still match).

        Returns:
            Up to ``max_results`` ``CrossStoreMatch`` records, highest score
            first. Empty when the query is blank, the store is empty, or no
            entry scores above the threshold.
        """
        if not query or not query.strip():
            return []

        now = self._now()
        with self._lock:
            live_entries = [
                (hash_key, entry)
                for hash_key, entry in self._backend.items()
                if not entry.is_expired(now)
            ]

        if not live_entries:
            return []

        # Score every live entry as ONE document. Batch scoring builds a real
        # corpus IDF map, so a discriminative term (a UUID/ID) outranks a term
        # common to many entries — the property that makes this BM25 rather
        # than raw term-frequency ranking.
        documents = [entry.original_content for _hash, entry in live_entries]
        scores = self._scorer.score_batch(documents, query)

        ranked = sorted(
            (
                (live_entries[i][0], live_entries[i][1], scores[i].score)
                for i in range(len(live_entries))
                if scores[i].score > score_threshold
            ),
            key=lambda triple: triple[2],
            reverse=True,
        )

        return [
            CrossStoreMatch(
                hash=hash_key,
                score=score,
                preview=self._cross_store_preview(entry.original_content),
                tool_name=entry.tool_name,
            )
            for hash_key, entry, score in ranked[:max_results]
        ]

    @staticmethod
    def _cross_store_preview(original_content: str) -> str:
        """Redacted, truncated preview of an original for a cross-store hit.

        Slice-before-redact (ReDoS guard, see ``_REDACT_WINDOW_MARGIN_CHARS``):
        redact only the bounded window the 200-char preview can show, never the
        whole multi-MB original. The margin still lets a secret straddling the
        preview edge be seen whole and masked before truncation, so a truncated
        head cannot leave a recognizable credential prefix in the clear. The
        redaction rules are shared with the retrieval-log preview so both
        surfaces mask exactly the same secret shapes.
        """
        window = original_content[: _CROSS_STORE_PREVIEW_CHARS + _REDACT_WINDOW_MARGIN_CHARS]
        redacted = _redact_retrieval_log_payload(window)
        preview = redacted[:_CROSS_STORE_PREVIEW_CHARS]
        if len(original_content) > len(window) or len(redacted) > _CROSS_STORE_PREVIEW_CHARS:
            preview = preview + "…"
        return preview

    def _log_retrieval_payload(
        self,
        *,
        hash_key: str,
        query: str | None,
        retrieval_type: str,
        payload: str,
        items_retrieved: int,
        total_items: int,
        entry: CompressionEntry,
    ) -> None:
        event = {
            "event": "furl_retrieve",
            "hash": hash_key,
            "retrieval_type": retrieval_type,
            # The query is caller/model-supplied and can itself carry a secret
            # (e.g. searching retrieved content for a token), so redact it with
            # the same rules as the payload before it reaches the log sink.
            "query": _redact_retrieval_log_payload(query) if query else query,
            "items_retrieved": items_retrieved,
            "total_items": total_items,
            "tool_name": entry.tool_name,
            "tool_call_id": entry.tool_call_id,
            "compression_strategy": entry.compression_strategy,
            "original_tokens": entry.original_tokens,
            "compressed_tokens": entry.compressed_tokens,
            "original_item_count": entry.original_item_count,
            "compressed_item_count": entry.compressed_item_count,
            **_payload_for_retrieval_log(payload),
        }
        logger.info(
            "event=furl_retrieve %s",
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        )

    def _search_items_from_original(self, original_content: str) -> list[Any]:
        """Normalize cached originals into searchable items.

        CCR producers store different shapes:
        - SmartCrusher/search-style paths usually store JSON arrays.
        - Text producers (e.g. the router's CCR offload) store plain text.
        - Some callers store JSON objects or scalar JSON values.

        Search should work for all of them. Preserve the legacy JSON-array
        result shape, but fall back to structured text chunks for everything
        else so `furl_retrieve(hash, query=...)` can find plain-text
        originals — and for canonicals whose numeric literals a Python float
        round-trip would corrupt, so query results carry the exact source
        bytes.
        """

        try:
            parsed, numerics_lossy = self._loads_detecting_numeric_loss(original_content)
        except json.JSONDecodeError:
            return self._plain_text_search_items(original_content)

        if numerics_lossy:
            # Numeric-fidelity fallback: the canonical parses, but at least
            # one numeric literal cannot survive Python's float round-trip —
            # e.g. 1e400 overflows to inf (re-emitted by json.dumps as the
            # RFC-invalid bare Infinity) and >17-significant-digit decimals
            # collapse. The engine's serde is configured with
            # arbitrary_precision precisely to preserve these, and the
            # no-query path returns verbatim bytes; serve text chunks sliced
            # from the verbatim original instead of silently-corrupted
            # re-parsed numbers.
            return self._plain_text_search_items(original_content)

        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return self._json_object_search_items(parsed)
        if isinstance(parsed, str):
            return self._plain_text_search_items(parsed)
        if parsed is None:
            return []
        return [{"type": "json_scalar", "value": parsed}]

    @staticmethod
    def _loads_detecting_numeric_loss(original_content: str) -> tuple[Any, bool]:
        """Parse JSON, flagging numeric literals a float round-trip corrupts.

        Returns ``(parsed, lossy)`` where ``lossy`` is True when any float
        literal overflows (``1e400`` → ``inf``), carries more precision than
        a Python float can represent, or is a bare ``Infinity``/``NaN``
        constant (parseable by Python, RFC-invalid to re-emit). The check is
        value-level, not textual: ``1e3`` and ``1000.0`` denote the same
        number, so they are NOT lossy. Integers are exempt — Python ints are
        arbitrary-precision and re-serialize exactly. Raises
        ``json.JSONDecodeError`` exactly like ``json.loads``.
        """
        lossy = False

        def parse_float(literal: str) -> float:
            nonlocal lossy
            value = float(literal)
            if not math.isfinite(value):
                lossy = True
            elif repr(value) != literal and decimal.Decimal(literal) != decimal.Decimal(
                repr(value)
            ):
                lossy = True
            return value

        def parse_constant(name: str) -> float:
            nonlocal lossy
            lossy = True
            return float(name)

        parsed = json.loads(
            original_content, parse_float=parse_float, parse_constant=parse_constant
        )
        return parsed, lossy

    def _json_object_search_items(self, value: dict[str, Any]) -> list[dict[str, Any]]:
        """Return searchable leaf records for a JSON object."""

        items: list[dict[str, Any]] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    walk(child, child_path)
                return
            if isinstance(node, list):
                for idx, child in enumerate(node):
                    walk(child, f"{path}[{idx}]")
                return
            if node is None:
                return
            items.append({"type": "json_leaf", "path": path, "value": node})

        walk(value, "")
        if items:
            return items
        return [{"type": "json_object", "value": value}]

    def _plain_text_search_items(self, text: str) -> list[dict[str, Any]]:
        """Chunk arbitrary text into searchable records.

        Line-aware chunks work well for logs/source. Word-window chunks handle
        long single-line text blobs.
        """

        if not text or not text.strip():
            return []

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        if len(lines) > 1:
            return self._line_text_search_items(lines)

        words = normalized.split()
        if not words:
            return []
        max_words = 350
        overlap_words = 50
        if len(words) <= max_words:
            return [
                {
                    "type": "text",
                    "text": normalized,
                    "chunk_index": 0,
                    "word_start": 1,
                    "word_end": len(words),
                }
            ]

        items: list[dict[str, Any]] = []
        start = 0
        chunk_index = 0
        step = max_words - overlap_words
        while start < len(words):
            end = min(len(words), start + max_words)
            items.append(
                {
                    "type": "text",
                    "text": " ".join(words[start:end]),
                    "chunk_index": chunk_index,
                    "word_start": start + 1,
                    "word_end": end,
                }
            )
            if end == len(words):
                break
            start += step
            chunk_index += 1
        return items

    @staticmethod
    def _line_text_search_items(lines: list[str]) -> list[dict[str, Any]]:
        max_chars = 2000
        items: list[dict[str, Any]] = []
        current: list[str] = []
        line_start = 1
        char_count = 0

        for idx, line in enumerate(lines, start=1):
            line_len = len(line) + 1
            if current and char_count + line_len > max_chars:
                items.append(
                    {
                        "type": "text",
                        "text": "\n".join(current),
                        "chunk_index": len(items),
                        "line_start": line_start,
                        "line_end": idx - 1,
                    }
                )
                current = []
                line_start = idx
                char_count = 0
            current.append(line)
            char_count += line_len

        if current:
            items.append(
                {
                    "type": "text",
                    "text": "\n".join(current),
                    "chunk_index": len(items),
                    "line_start": line_start,
                    "line_end": len(lines),
                }
            )
        return items

    def _get_entry_for_search(
        self,
        hash_key: str,
    ) -> CompressionEntry | None:
        """Get entry without logging retrieval or recording an access.

        CRITICAL FIX #4: Returns a copy of the entry to prevent race conditions.
        The caller may use the entry after we release the lock, and another thread
        could modify or evict the original entry.

        COR-37: this read is side-effect-free (beyond expiry reaping) — the
        access bump happens in ``_record_search_access`` only after search
        knows it returned results, so zero-result probes never count as
        retrievals.

        Args:
            hash_key: Hash key returned by store().

        Returns:
            CompressionEntry copy if found and not expired, None otherwise.
        """
        with self._lock:
            entry = self._backend.get(hash_key)

            if entry is None:
                return None

            if entry.is_expired(self._now()):
                self._backend.delete(hash_key)
                # CRITICAL FIX: Track stale heap entry
                self._stale_heap_entries += 1
                return None

            # CRITICAL FIX #4: Return a copy to prevent race conditions
            # The entry contains mutable fields (search_queries list) that could be
            # modified by other threads after we release the lock
            return replace(entry, search_queries=list(entry.search_queries))

    def _record_search_access(self, hash_key: str, query: str | None) -> None:
        """Record an access on an entry AFTER a search returned results.

        Runs after scoring, so the entry may have expired or been evicted
        between the search's read and this bump — in that case there is
        nothing to record and the results (built from a pre-eviction copy)
        still ship to the caller.

        Engine P2-13: this bump is also the search-side retrieval-feedback
        emission point. ``search()`` calls here only when results actually
        shipped (COR-37), so the feedback loop inherits the same honesty —
        zero-result probes never emit a signal.
        """
        signal_meta: tuple[str | None, str | None] | None = None
        with self._lock:
            entry = self._backend.get(hash_key)
            if entry is None or entry.is_expired(self._now()):
                return
            entry.record_access(query)
            self._backend.set(hash_key, entry)
            signal_meta = (entry.tool_name, entry.compression_strategy)
        # Emit outside the store lock (aggregator lock is a leaf — no nesting).
        if self._enable_feedback and signal_meta is not None:
            self._emit_retrieval_signal(*signal_meta)

    def _emit_retrieval_signal(
        self,
        tool_name: str | None,
        compression_strategy: str | None,
    ) -> None:
        """Feed one model-driven retrieval into the local feedback loop.

        Never raises: the feedback plane is ADVISORY — a broken aggregator
        must not turn a successful retrieval into a failure. Imported lazily
        so the store stays importable without the feedback module and the
        emission stays monkeypatch-friendly in tests.
        """
        try:
            from . import retrieval_feedback

            retrieval_feedback.record_retrieval_signal(
                tool_name=tool_name,
                compression_strategy=compression_strategy,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("retrieval-feedback signal dropped (non-fatal): %s", e)

    def exists(self, hash_key: str, clean_expired: bool = False) -> bool:
        """Check if a hash key exists and is not expired.

        Args:
            hash_key: The hash key to check.
            clean_expired: If True, delete the entry if expired.
                          Defaults to False to make this a pure check.

        Returns:
            True if the entry exists and is not expired.
        """
        with self._lock:
            entry = self._backend.get(hash_key)
            if entry is None:
                return False
            if entry.is_expired(self._now()):
                # Only delete if explicitly requested.
                # This makes exists() a pure check by default
                if clean_expired:
                    self._backend.delete(hash_key)
                    # CRITICAL FIX: Track stale heap entry
                    self._stale_heap_entries += 1
                return False
            return True

    def get_entry_status(
        self,
        hash_key: str,
        *,
        clean_expired: bool = False,
    ) -> dict[str, Any]:
        """Return availability and TTL metadata for a stored entry."""
        now = self._now()
        with self._lock:
            entry = self._backend.get(hash_key)
            if entry is None:
                return {
                    "hash": hash_key,
                    "status": "missing",
                    "default_ttl_seconds": self._default_ttl,
                    "max_entries": self._max_entries,
                }

            age_seconds = now - entry.created_at
            expires_at = entry.created_at + entry.ttl
            expired = age_seconds > entry.ttl
            status = {
                "hash": hash_key,
                "status": "expired" if expired else "available",
                "ttl_seconds": entry.ttl,
                "default_ttl_seconds": self._default_ttl,
                "created_at": entry.created_at,
                "expires_at": expires_at,
                "age_seconds": age_seconds,
            }

            if expired and clean_expired:
                self._backend.delete(hash_key)
                self._stale_heap_entries += 1

            return status

    def get_stats(self) -> dict[str, Any]:
        """Get store statistics for monitoring."""
        with self._lock:
            # Clean expired entries
            self._clean_expired()

            # Get all entries for statistics
            entries = [entry for _, entry in self._backend.items()]
            total_original_tokens = sum(e.original_tokens for e in entries)
            total_compressed_tokens = sum(e.compressed_tokens for e in entries)
            total_retrievals = sum(e.retrieval_count for e in entries)

            # Include backend stats
            backend_stats = self._backend.get_stats()

            return {
                "entry_count": self._backend.count(),
                "max_entries": self._max_entries,
                "default_ttl_seconds": self._default_ttl,
                "total_original_tokens": total_original_tokens,
                "total_compressed_tokens": total_compressed_tokens,
                "total_retrievals": total_retrievals,
                "event_count": len(self._retrieval_events),
                "backend": backend_stats,
            }

    def delete(self, hash_key: str) -> bool:
        """Delete the entry for *hash_key* from the store. Returns whether one went.

        The purge surface (B3): removes a single stored original outright so its
        content is no longer recoverable via ``retrieve``. Deletes from BOTH the
        primary backend and the durable spill tier (Q10), so a purge leaves no
        recoverable copy behind; the spill delete is fail-open (logged, never
        raises) exactly like the other spill operations. Returns True when an
        entry was removed from EITHER tier, False when the hash was absent from
        both. Bumps the stale-heap counter on a primary hit so the eviction heap
        cleans up the dangling ``(created_at, hash_key)`` tuple, matching every
        other in-store delete path (expiry reaping, collision replace).
        """
        with self._lock:
            primary_deleted = self._backend.delete(hash_key)
            if primary_deleted:
                self._stale_heap_entries += 1
            spill_deleted = False
            if self._spill is not None:
                try:
                    spill_deleted = self._spill.delete(hash_key)
                except Exception as exc:  # noqa: BLE001 — fail-open, logged below
                    logger.warning("CCR spill delete failed (non-fatal): %s", exc)
            return primary_deleted or spill_deleted

    def clear(self) -> None:
        """Clear all entries. Mainly for testing."""
        with self._lock:
            self._backend.clear()
            # Q10 spill tier: clear the durable spill too, so ``clear`` empties
            # every place an entry can live. Fail-open — a spill clear error
            # must not break the primary reset.
            if self._spill is not None:
                try:
                    self._spill.clear()
                except Exception as exc:  # noqa: BLE001 — fail-open, logged below
                    logger.warning("CCR spill clear failed (non-fatal): %s", exc)
            self._retrieval_events.clear()
            self._eviction_heap.clear()  # Clear heap too
            self._stale_heap_entries = 0  # CRITICAL FIX: Reset stale counter

    def close(self) -> None:
        """Release backend resources (sqlite connections / file descriptors).

        Distinct from ``clear`` (which empties entries but keeps the backend
        open): a dropped store — e.g. a per-namespace store retired by
        ``reset_compression_store`` — must close its sqlite handles or they leak
        as ``ResourceWarning``-flagged unclosed connections (P5). Idempotent and
        fail-open: a backend without ``close`` (the in-memory default) is
        skipped, and a close error is logged, never raised, so teardown always
        proceeds. The spill tier is closed too, so no durable handle survives.
        """
        for backend in (self._backend, self._spill):
            if backend is None:
                continue
            close = getattr(backend, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.warning("CCR backend close failed (non-fatal): %s", exc)

    def _spill_evicted(self, hash_key: str, entry: CompressionEntry) -> None:
        """Demote a capacity-evicted (still-live) entry to the spill tier.

        Called with the store lock held, immediately before the primary
        ``delete``. No-op when the spill is disabled. Best-effort and fail-open:
        a spill write error is logged and swallowed so eviction (and thus the
        primary's capacity accounting) always proceeds — the entry simply
        reverts to today's loud-miss behavior for that one key.
        """
        if self._spill is None:
            return
        try:
            self._spill.set(hash_key, entry)
        except Exception as exc:  # noqa: BLE001 — fail-open, logged below
            logger.warning("CCR spill write failed (non-fatal): %s", exc)

    def _recover_from_spill(self, hash_key: str) -> CompressionEntry | None:
        """Resolve a primary miss against the durable spill tier (Q10).

        Called with the store lock held from the ``retrieve`` primary-miss
        branch. Returns ``None`` (today's loud miss) when the spill is disabled,
        the key is absent, or the spilled row has expired — TTL is still honored
        in the spill exactly as in the primary. A hit is returned as an
        independent copy WITHOUT promoting it back into the primary or touching
        access bookkeeping, so it is byte-identical to the evicted value. Fail-open:
        a spill read error is logged and treated as a miss.
        """
        if self._spill is None:
            return None
        try:
            entry = self._spill.get(hash_key)
        except Exception as exc:  # noqa: BLE001 — fail-open, logged below
            logger.warning("CCR spill read failed (non-fatal): %s", exc)
            return None
        if entry is None:
            return None
        if entry.is_expired(self._now()):
            try:
                self._spill.delete(hash_key)
            except Exception as exc:  # noqa: BLE001 — fail-open, logged below
                logger.warning("CCR spill delete of expired row failed (non-fatal): %s", exc)
            return None
        # Copy the mutable field so a caller cannot mutate the spilled entry
        # (mirrors the primary-hit copy in ``retrieve``).
        return replace(entry, search_queries=list(entry.search_queries))

    def _evict_if_needed(self) -> None:
        """Evict old entries if at capacity. Must be called with lock held.

        Uses a heap for O(log n) eviction instead of O(n) scan.
        CRITICAL FIX: Track and clean stale heap entries to prevent memory leak.
        """
        # First, remove expired entries
        self._clean_expired()

        # CRITICAL FIX: Rebuild heap if too many stale entries
        # This prevents unbounded heap growth when entries are deleted/replaced
        heap_size = len(self._eviction_heap)
        if heap_size > 0:
            stale_ratio = self._stale_heap_entries / heap_size
            if stale_ratio >= self._heap_rebuild_threshold:
                self._rebuild_heap()

        # If still at capacity, remove oldest entries using the heap.
        #
        # The eviction loop must GUARANTEE ``count() <= max_entries`` on
        # exit. The old loop ran ``while heap`` and could exit over capacity if
        # the heap held only stale references (deleted/replaced keys, or stale
        # timestamps) — popping those evicts nothing real, so the heap could
        # drain (or a fixed budget could be exhausted by ghost refs) while the
        # backend was still over capacity. The ratio-guard rebuild above only
        # fires when the stale COUNTER is accurate; a heap whose staleness is
        # under-counted slips past it.
        #
        # Fix: track real progress. Each iteration that fails to evict a real
        # entry while over capacity rebuilds the heap from the LIVE backend
        # (correct timestamps, no ghost refs) so the next pop is guaranteed to
        # hit a real oldest entry. Eviction stays oldest-first (no side-door).
        # Bounded: each rebuild yields a heap of exactly the live entries, and
        # every subsequent pop removes one, so the loop terminates in
        # O(live entries).
        rebuilt_since_progress = False
        while self._backend.count() >= self._max_entries:
            if not self._eviction_heap:
                if rebuilt_since_progress:
                    break  # already rebuilt with no live entries to evict — give up
                self._rebuild_heap()
                rebuilt_since_progress = True
                if not self._eviction_heap:
                    break
                continue

            created_at, hash_key = heapq.heappop(self._eviction_heap)
            entry = self._backend.get(hash_key)
            if entry is not None and entry.created_at == created_at:
                # Real oldest entry — evict it. Q10 spill tier: demote the
                # (still-live: ``_clean_expired`` ran above) entry to the durable
                # spill BEFORE dropping it from the primary, so the marker stays
                # resolvable past this eviction. The primary delete is
                # unconditional — capacity MUST be freed even if the spill write
                # fails (best-effort), or this loop would not make progress.
                self._spill_evicted(hash_key, entry)
                self._backend.delete(hash_key)
                rebuilt_since_progress = False  # made progress
            else:
                # Stale heap reference — decrement the counter. If the heap
                # drains to nothing but ones (no real eviction) the `not heap`
                # branch above rebuilds from the live backend.
                if self._stale_heap_entries > 0:
                    self._stale_heap_entries -= 1

    def _clean_expired(self) -> None:
        """Remove expired entries. Must be called with lock held.

        Delegates to the backend's expiry GC (audit #2) instead of
        materializing every row into Python just to find the expired keys —
        this runs on the store() write hot path (via ``_evict_if_needed``), so
        for the durable backend it is now an indexed range delete, not a full
        scan + decode of the whole shared file. Each purged entry leaves a stale
        ``(created_at, hash_key)`` tuple in the eviction heap; the counter bump
        keeps the heap-staleness accounting exactly as the old per-key delete
        loop did (the tuples are found stale on pop, or reaped by the
        ratio-guard rebuild).
        """
        purged = self._backend.purge_expired(self._now())
        self._stale_heap_entries += purged

    def _rebuild_heap(self) -> None:
        """Rebuild heap from current store entries. Must be called with lock held.

        CRITICAL FIX: This removes stale heap entries that accumulate when entries
        are deleted or replaced. Without this, the heap grows unboundedly.
        """
        # Build new heap from current store entries only. Uses the backend's
        # projected (created_at, hash_key) read (audit #2) so a rebuild on the
        # store() hot path does not decode every content BLOB out of the file.
        self._eviction_heap = list(self._backend.created_at_index())
        heapq.heapify(self._eviction_heap)
        # Reset stale counter - heap is now clean
        self._stale_heap_entries = 0
        logger.debug(
            "Rebuilt eviction heap: %d entries",
            len(self._eviction_heap),
        )

    def _log_retrieval(
        self,
        hash_key: str,
        query: str | None,
        items_retrieved: int,
        total_items: int,
        tool_name: str | None,
        retrieval_type: str,
    ) -> None:
        """Log a retrieval event. Must be called with lock held."""
        event = RetrievalEvent(
            hash=hash_key,
            query=query,
            items_retrieved=items_retrieved,
            total_items=total_items,
            tool_name=tool_name,
            timestamp=time.time(),
            retrieval_type=retrieval_type,
        )

        self._retrieval_events.append(event)

        # Keep only recent events
        if len(self._retrieval_events) > self._max_events:
            self._retrieval_events = self._retrieval_events[-self._max_events :]


# Request-scoped store (for multi-tenant SaaS: one store per request/tenant)
_request_ccr_store: ContextVar[CompressionStore | None] = ContextVar(
    "furl_request_ccr_store", default=None
)

# Global store instance (lazy initialization)
_compression_store: CompressionStore | None = None
_store_lock = threading.Lock()


def set_request_compression_store(store: CompressionStore | None) -> None:
    """Set the compression store for the current request context.

    Used by middleware (e.g. SaaS) to provide a tenant-scoped store.
    When set, get_compression_store() returns this store instead of the global one.

    Args:
        store: CompressionStore to use for this request, or None to clear.
    """
    _request_ccr_store.set(store)


def clear_request_compression_store() -> None:
    """Clear the request-scoped compression store."""
    _request_ccr_store.set(None)


# ---------------------------------------------------------------------------
# Per-tenant CCR namespacing (B2 durable-retention).
#
# Real isolation, not prefixing: search/stats/expiry/heap-rebuild all iterate
# the WHOLE backend and the sqlite backend has no namespace column, so a shared
# backend with prefixed keys would leak entries across tenants. Instead each
# namespace gets its OWN ``CompressionStore`` (its own backend / sqlite file),
# swapped in via the ``_request_ccr_store`` ContextVar around the pipeline call.
# An entry written under namespace A is simply not present in namespace B's
# store object, so cross-namespace retrieval returns None — the invariant is
# structural, not a filter that can be forgotten.
# ---------------------------------------------------------------------------

FURL_CCR_NAMESPACE_ENV = "FURL_CCR_NAMESPACE"

# Per-project isolation (audit #4). When set — the plugin deployment exports it
# from the project root — an otherwise un-namespaced call is scoped to a
# per-project store instead of the process-global singleton, closing the
# cross-project commingling + eviction hole with zero user config. Absent
# (library / unit tests) the global singleton serves, byte-for-byte unchanged.
FURL_CCR_PROJECT_DIR_ENV = "FURL_CCR_PROJECT_DIR"

# Registry of namespace-key -> store, so identical (namespace, session, agent)
# tuples converge on the SAME store across calls (cross-turn retrieval works)
# and in-memory tenants do not lose their entries between compress() calls.
_namespace_stores: dict[str, CompressionStore] = {}
_namespace_lock = threading.Lock()


def _project_scope_key() -> str | None:
    """Per-project namespace key from ``FURL_CCR_PROJECT_DIR`` (audit #4).

    Returns ``None`` when the variable is unset/blank, so the caller keeps
    today's global-singleton behavior (library, unit tests). When set, the raw
    project root is canonicalized (``expanduser().resolve()``) so the hook and
    MCP processes — which may observe the same project via different spellings
    or a symlink — converge on ONE key, and thus ONE sqlite file. The ``\\x01``
    prefix marks this as a project-scope key so it can never alias an explicit
    ``(namespace, session, agent)`` tuple; the value is opaque and only ever
    hashed into the sqlite filename, never interpolated into a path.
    """
    raw = (os.environ.get(FURL_CCR_PROJECT_DIR_ENV) or "").strip()
    if not raw:
        return None
    try:
        resolved = str(Path(raw).expanduser().resolve())
    except OSError:
        resolved = raw
    return "\x01".join(("furl-project", resolved))


def _namespace_key(session_id: str | None, agent_id: str | None) -> str | None:
    """Compose the isolation key from ``FURL_CCR_NAMESPACE`` + session + agent.

    The three segments together define the tenant boundary: an identical tuple
    maps to the same store (so a later turn recovers what an earlier turn
    stored), any difference maps to a different store. Blank/None segments
    contribute an empty field. When none of the three is set the call carries no
    explicit tenant identity, so it falls back to the per-project scope
    (``FURL_CCR_PROJECT_DIR``); with neither present this returns ``None`` and
    the global singleton serves — today's behavior, byte-for-byte.
    """
    env_ns = (os.environ.get(FURL_CCR_NAMESPACE_ENV) or "").strip()
    session = (session_id or "").strip()
    agent = (agent_id or "").strip()
    if not env_ns and not session and not agent:
        # No explicit tenant identity: prefer per-project isolation when the
        # deployment provides a project root, else None (global singleton).
        return _project_scope_key()
    # NUL-joined so distinct segmentations cannot alias (``a`` + ``bc`` vs
    # ``ab`` + ``c``); the raw values are opaque and never touch a filesystem
    # path directly — the sqlite filename is derived by hashing this key.
    return "\x00".join((env_ns, session, agent))


def _ccr_namespace_db_path(namespace_key: str) -> Path:
    """Per-namespace durable sqlite path, derived by HASHING the key.

    ``session_id`` / ``agent_id`` are untrusted request data, so the key never
    interpolates into a path verbatim (``session_id="../../x"`` would traverse).
    The filename is ``ccr-ns-<sha256(key)[:16]>.sqlite3`` under the workspace
    dir — 64 bits of hash, far more than enough that two tenants cannot collide
    onto one file. Sits beside the global ``ccr.sqlite3`` so every tenant shares
    the workspace root but never the same database.
    """
    digest = hashlib.sha256(namespace_key.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return _paths.workspace_dir() / f"ccr-ns-{digest}.sqlite3"


def _build_namespace_store(namespace_key: str) -> CompressionStore:
    """Construct a fresh, isolated store for ``namespace_key``.

    Backend selection mirrors the global default (``FURL_CCR_BACKEND``): the
    durable ``sqlite`` backend gets a per-namespace file (so tenants never share
    a database), every other selection — including the in-memory default — gets
    its OWN backend instance via the ``CompressionStore`` constructor. Never
    falls back to the global store: that would defeat isolation.
    """
    backend_type = (os.environ.get("FURL_CCR_BACKEND") or "").strip().lower()
    backend: CompressionStoreBackend | None = None
    if backend_type == "sqlite":
        from .backends.sqlite import SqliteBackend

        backend = SqliteBackend(db_path=_ccr_namespace_db_path(namespace_key))
    # Deliberately NO spill tier here: the env spill (FURL_CCR_SPILL) targets the
    # shared global ``ccr.sqlite3``, so wiring one would demote every tenant into
    # a single shared file — the exact cross-namespace leak this isolation
    # forbids. Each namespace is self-contained in its own backend.
    return CompressionStore(
        default_ttl=_get_env_default_ttl_seconds(),
        backend=backend,
    )


def _resolve_namespace_store(namespace_key: str) -> CompressionStore:
    """Return the store for ``namespace_key``, creating it once (registry).

    Non-throwing by construction: a dict lookup under a lock plus
    ``SqliteBackend.__init__`` (which degrades to in-memory internally rather
    than raising). The double-check keeps store creation single-flight without
    holding the lock across construction longer than needed.
    """
    store = _namespace_stores.get(namespace_key)
    if store is not None:
        return store
    with _namespace_lock:
        store = _namespace_stores.get(namespace_key)
        if store is None:
            store = _build_namespace_store(namespace_key)
            _namespace_stores[namespace_key] = store
    return store


def resolve_ccr_namespace_store(
    session_id: str | None = None,
    agent_id: str | None = None,
) -> CompressionStore | None:
    """Resolve the tenant-scoped store for ``(namespace, session, agent)``.

    Returns ``None`` when no namespace is active (the global singleton serves —
    zero change to today's behavior), otherwise the isolated per-namespace
    store. This is the seam ``compress()`` binds onto ``_request_ccr_store`` so
    the inline ``get_compression_store()`` calls in the transforms pick up the
    tenant store for the duration of the call.
    """
    key = _namespace_key(session_id, agent_id)
    if key is None:
        return None
    return _resolve_namespace_store(key)


def _backend_opts_from_env() -> dict[str, Any]:
    """Parse ``FURL_CCR_BACKEND_OPTS`` (a JSON object) into factory kwargs.

    Unset/blank means the entry-point factory is called with no arguments.
    Malformed JSON or a non-object value raises ``ValueError`` — an operator
    who set the variable asked for those kwargs; guessing is worse.
    """
    raw = (os.environ.get("FURL_CCR_BACKEND_OPTS") or "").strip()
    if not raw:
        return {}
    try:
        opts = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"FURL_CCR_BACKEND_OPTS is not valid JSON: {e}") from e
    if not isinstance(opts, dict):
        raise ValueError(
            "FURL_CCR_BACKEND_OPTS must be a JSON object of factory kwargs, "
            f"got {type(opts).__name__}"
        )
    return opts


def _create_default_ccr_backend() -> CompressionStoreBackend | None:
    """Create a CCR backend from env (``FURL_CCR_BACKEND=<name>``).

    Built-in names resolve first: ``memory`` (the default, returns None) and
    ``sqlite`` (the durable workspace-file backend, Engine P1-7 — its
    constructor handles DB corruption internally). Anything else loads via
    the setuptools entry point group ``furl_ctx.ccr_backend``, with factory
    kwargs taken from ``FURL_CCR_BACKEND_OPTS`` (a JSON object; unset means
    a zero-argument factory call).

    An operator who EXPLICITLY selects a backend asked for its durability
    semantics, so failure to deliver that backend RAISES instead of silently
    downgrading to the in-memory store (API-5):

    * unknown backend name or malformed ``FURL_CCR_BACKEND_OPTS`` →
      ``ValueError`` (misconfiguration);
    * entry-point load / factory failure → ``RuntimeError`` (cause chained).

    Returns None to use the default InMemoryBackend.
    """
    backend_type = (os.environ.get("FURL_CCR_BACKEND") or "").strip().lower()
    if not backend_type or backend_type == "memory":
        return None
    if backend_type == "sqlite":
        from .backends.sqlite import SqliteBackend

        return SqliteBackend()

    import importlib.metadata

    opts = _backend_opts_from_env()
    all_eps = importlib.metadata.entry_points(group="furl_ctx.ccr_backend")
    ep = next((e for e in all_eps if e.name == backend_type), None)
    if ep is None:
        raise ValueError(
            f"FURL_CCR_BACKEND={backend_type!r} selected, but no entry point "
            f"furl_ctx.ccr_backend[{backend_type}] is installed. Install the "
            "backend package, or unset FURL_CCR_BACKEND / set it to "
            "'memory'/'sqlite'."
        )
    try:
        factory = ep.load()
        backend: CompressionStoreBackend = factory(**opts)
    except Exception as e:
        raise RuntimeError(
            f"CCR backend {backend_type!r} failed to load/construct "
            f"(FURL_CCR_BACKEND_OPTS kwargs: {sorted(opts)}): {e}"
        ) from e
    return backend


CCR_SPILL_ENV = "FURL_CCR_SPILL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _create_spill_backend_from_env(
    primary: CompressionStoreBackend | None,
) -> CompressionStoreBackend | None:
    """Build the durable SPILL tier from env (``FURL_CCR_SPILL``), Q10 retention.

    Returns ``None`` (spill disabled → single-tier, byte-identical) unless
    ``FURL_CCR_SPILL`` is truthy (``1``/``true``/``yes``/``on``). When enabled,
    the spill is a ``SqliteBackend`` (its own row cap + TTL backstop bound it).

    Redundant-combo guard: if the PRIMARY is already durable (the operator set
    ``FURL_CCR_BACKEND=sqlite`` → ``primary`` is a ``SqliteBackend``), a spill
    would demote sqlite→sqlite for no benefit, so this returns ``None`` and the
    store stays single-tier durable. Spill mode is designed as fast in-memory
    primary + durable sqlite spill.
    """
    flag = (os.environ.get(CCR_SPILL_ENV) or "").strip().lower()
    if flag not in _TRUTHY:
        return None

    from .backends.sqlite import SqliteBackend

    if isinstance(primary, SqliteBackend):
        # Primary already durable — spill is redundant. Unsupported combo.
        return None
    return SqliteBackend()


def get_compression_store(
    max_entries: int = 1000,
    default_ttl: int | None = None,
    backend: CompressionStoreBackend | None = None,
) -> CompressionStore:
    """Get the compression store instance.

    If a request-scoped store was set (e.g. by SaaS middleware), returns it.
    Otherwise uses lazy-initialized global singleton. Backend can be supplied
    explicitly or created from env (FURL_CCR_BACKEND) when building the global.

    Args:
        max_entries: Maximum entries (only used on first call for global store).
        default_ttl: Default TTL (only used on first call for global store).
            When omitted, FURL_CCR_TTL_SECONDS overrides the 1800-second default.
        backend: Custom storage backend (only used on first call for global store).
                 Defaults to InMemoryBackend if not provided; env backend used if backend is None.

    Returns:
        Request-scoped CompressionStore if set, else global CompressionStore instance.
    """
    request_store = _request_ccr_store.get()
    if request_store is not None:
        return request_store

    global _compression_store
    if _compression_store is None:
        with _store_lock:
            if _compression_store is None:
                if backend is None:
                    backend = _create_default_ccr_backend()
                effective_default_ttl = (
                    default_ttl if default_ttl is not None else _get_env_default_ttl_seconds()
                )
                spill = _create_spill_backend_from_env(backend)
                _compression_store = CompressionStore(
                    max_entries=max_entries,
                    default_ttl=effective_default_ttl,
                    backend=backend,
                    spill=spill,
                )
    return _compression_store


def reset_compression_store() -> None:
    """Reset the global compression store. Mainly for testing.

    Also drops every per-namespace store (B2): the existing autouse fixtures
    call this between tests, so folding the namespace registry in here keeps
    tenant stores from leaking retrievable entries across tests.
    """
    global _compression_store

    with _store_lock:
        if _compression_store is not None:
            _compression_store.clear()
            _compression_store.close()  # release sqlite fds before dropping (P5 leak)
        _compression_store = None

    with _namespace_lock:
        for store in _namespace_stores.values():
            store.clear()
            store.close()  # each per-namespace backend holds its own fds — close them
        _namespace_stores.clear()


def _active_ccr_store(
    session_id: str | None,
    agent_id: str | None,
) -> CompressionStore:
    """The store ``ccr_export``/``ccr_import`` act on for a given tenant.

    When a namespace is active (``FURL_CCR_NAMESPACE`` or ``session_id`` /
    ``agent_id``) this is that tenant's isolated store; otherwise it is
    whatever ``get_compression_store()`` resolves — the request-scoped store if
    middleware set one, else the global singleton. Checkpointing a specific
    tenant is thus a matter of passing the same ids used at ``compress()`` time.
    """
    namespace_store = resolve_ccr_namespace_store(session_id, agent_id)
    if namespace_store is not None:
        return namespace_store
    return get_compression_store()


def ccr_export(
    path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> int:
    """Checkpoint a CCR store to a durable sqlite file at ``path``.

    Copies entries at the BACKEND level (``items()`` -> ``set()``) so every
    field round-trips byte-exact: routing through ``store.store()`` would
    recompute the key and reset ``created_at`` / ``ttl`` / ``retrieval_count``.
    The destination is a fresh ``SqliteBackend`` on ``path`` — its
    ``surrogatepass`` BLOB encoding preserves hostile payloads (lone
    surrogates, NULs, control chars) unchanged.

    ``session_id`` / ``agent_id`` (default ``None``) select the tenant store to
    export, matching the values passed to ``compress()``; with none set the
    active/global store is exported.

    Returns the number of entries written.
    """
    from .backends.sqlite import SqliteBackend

    source = _active_ccr_store(session_id, agent_id)
    destination = SqliteBackend(db_path=path)
    try:
        entries = source._backend.items()
        for hash_key, entry in entries:
            destination.set(hash_key, entry)
    finally:
        destination.close()
    return len(entries)


def ccr_import(
    path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> int:
    """Restore a CCR checkpoint written by ``ccr_export`` into a store.

    Reads the sqlite file at ``path`` and copies each entry into the target
    store's backend (``items()`` -> ``backend.set()``), preserving
    ``created_at`` / ``ttl`` / ``retrieval_count`` so a restored entry is
    byte-identical to the one exported. TTL is honored on later ``retrieve()``
    (an entry that has since expired correctly misses — TTL-on-access promotion
    is deliberately out of scope for B2).

    ``session_id`` / ``agent_id`` (default ``None``) select the destination
    tenant store; with none set the active/global store receives the entries.

    Returns the number of entries restored.
    """
    from .backends.sqlite import SqliteBackend

    source = SqliteBackend(db_path=path)
    try:
        entries = source.items()
    finally:
        source.close()
    destination = _active_ccr_store(session_id, agent_id)
    for hash_key, entry in entries:
        destination._backend.set(hash_key, entry)
    return len(entries)
