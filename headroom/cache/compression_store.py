"""Compression Store for CCR (Compress-Cache-Retrieve) architecture.

This module implements reversible compression: when SmartCrusher compresses
tool outputs, the original data is cached here for on-demand retrieval.

Key insight from research: REVERSIBLE compression beats irreversible compression.
If the LLM needs data that was compressed away, it can retrieve it — byte-exact,
but only within the in-memory window (<=1000 entries, <=300s TTL). After eviction
or expiry the entry is gone and retrieval is a loud, cause-honest miss (never a
silent None). See CCR-RETENTION.md for the delivered guarantee vs. the open
durable-retention epic.

Features:
- Thread-safe in-memory storage with TTL expiration
- BM25-based search within cached content
- Retrieval event tracking for feedback loop
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
from typing import TYPE_CHECKING, Any

from ..relevance.bm25 import BM25Scorer

if TYPE_CHECKING:
    from .backends import CompressionStoreBackend

logger = logging.getLogger(__name__)

DEFAULT_CCR_TTL_SECONDS = 300
CCR_TTL_SECONDS_ENV = "HEADROOM_CCR_TTL_SECONDS"

# Minimum length for a caller-supplied ``explicit_hash``. This is the LOOSE
# recovery-floor contract, intentionally distinct from the STRICT consumer set
# ``marker_grammar.HASH_WIDTHS`` ({12, 24}) that the anti-spoofing ingress
# (``tool_injection.parse_tool_call`` + the MCP retrieve handler) enforces — see
# the "Two DISTINCT width contracts" note in ``ccr/marker_grammar.py``. The store
# must accept any hex key a DIRECT lookup can recover (shape I, the read-lifecycle
# marker, is recovered by direct store lookup and never by the strict scanner), so
# its floor is deliberately looser than the spoofing guard. The floor only rejects
# trivially-collidable sub-6 keys; every real producer emits exactly 12- or 24-char
# hashes, well clear of it.
_MIN_EXPLICIT_HASH_LEN = 6

_RETRIEVAL_LOG_PREVIEW_CHARS = 4096
# Match ``<sensitive-key><sep><value>`` in both plain (``api_key=...``) and JSON
# quoted-key (``"api_key": "..."``) form. Group 2 allows an OPTIONAL closing quote
# before the separator: in JSON the key's own closing ``"`` sits between the key
# name and the ``:``, so without it the ``[:=]`` never abuts the key and the whole
# rule silently misses every JSON-embedded secret (the PRIMARY shape in tool
# output) — the value stayed un-redacted unless it independently matched the
# ``sk-`` rule below. Group 3 still captures the value's optional opening quote so
# ``\1\2\3[REDACTED]`` preserves surrounding structure and only the value is cut.
_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)\b([A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)[A-Z0-9_-]*)"
    r"([\"']?\s*[:=]\s*)([\"']?)([^\"'\s,}]+)"
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


def _get_env_default_ttl_seconds() -> int:
    raw_value = os.environ.get(CCR_TTL_SECONDS_ENV)
    if raw_value is None or not raw_value.strip():
        return DEFAULT_CCR_TTL_SECONDS

    try:
        ttl_seconds = int(raw_value)
    except ValueError:
        logger.warning(
            "%s must be a positive integer number of seconds, got %r; using %s",
            CCR_TTL_SECONDS_ENV,
            raw_value,
            DEFAULT_CCR_TTL_SECONDS,
        )
        return DEFAULT_CCR_TTL_SECONDS

    if ttl_seconds <= 0:
        logger.warning(
            "%s must be greater than 0, got %s; using %s",
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
    # Redact ``Bearer``/``Basic`` scheme tokens BEFORE the secret-key rule so the
    # scheme anchor survives. Otherwise ``_SECRET_KEY_VALUE_RE`` (which matches the
    # ``Authorization`` key) consumes the bare ``Bearer`` scheme word as its value,
    # leaving the actual credential after it un-redacted in a plain-text
    # ``Authorization: Bearer <JWT>`` header. Over-redaction is safe.
    redacted = _AUTH_VALUE_RE.sub(r"\1 [REDACTED]", payload)
    redacted = _SECRET_KEY_VALUE_RE.sub(r"\1\2\3[REDACTED]", redacted)
    redacted = _API_KEY_VALUE_RE.sub("sk-[REDACTED]", redacted)
    return _PROVIDER_TOKEN_RE.sub("[REDACTED]", redacted)


def _payload_for_retrieval_log(payload: str) -> dict[str, Any]:
    redacted = _redact_retrieval_log_payload(payload)
    preview = redacted[:_RETRIEVAL_LOG_PREVIEW_CHARS]
    truncated = len(redacted) > len(preview)
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

    # TOIN integration: Store the tool signature hash for retrieval correlation
    # This MUST match the hash used by SmartCrusher when recording compression
    tool_signature_hash: str | None = None
    compression_strategy: str | None = None  # Strategy used for compression

    # Feedback tracking
    retrieval_count: int = 0
    search_queries: list[str] = field(default_factory=list)
    last_accessed: float | None = None

    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.time() - self.created_at > self.ttl

    def record_access(self, query: str | None = None) -> None:
        """Record an access to this entry for feedback tracking."""
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
    tool_signature_hash: str | None = None  # For TOIN correlation


class CompressionStore:
    """Thread-safe store for compressed content with retrieval support.

    This is the core of the CCR architecture. When SmartCrusher compresses
    an array, the original content is stored here. If the LLM needs more
    data, it can retrieve from this cache instantly.

    Design principles:
    - Zero external dependencies (pure Python)
    - Thread-safe for concurrent access
    - TTL-based expiration (default 300 seconds, env-configurable)
    - FIFO-by-creation eviction when capacity is reached (the oldest
      ``created_at`` is evicted first via a min-heap, NOT least-recently-used)
    - Built-in BM25 search for filtering

    Recovery scope (read this before relying on retrieval):
        Stored content is recoverable byte-exact only WITHIN the in-memory
        window: at most ``max_entries`` live entries (default 1000) and at most
        ``default_ttl`` seconds old (default 300s). The store is single-tier —
        on capacity or TTL eviction the entry's payload is deleted outright
        (there is no spill to a durable tier), so a later ``retrieve()`` of an
        evicted/expired hash returns ``None``. That miss is never silent: the
        retrieval callers (e.g. the MCP ``headroom_retrieve`` tool) surface it
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
    ):
        """Initialize the compression store.

        Args:
            max_entries: Maximum number of entries to store.
            default_ttl: Default TTL in seconds.
            enable_feedback: Whether to track retrieval events.
            backend: Storage backend to use. Defaults to InMemoryBackend, the
                     only concrete backend that ships today. A backend
                     implementing the ``CompressionStoreBackend`` protocol could
                     change WHERE entries live, but on its own it does NOT widen
                     the recovery window: eviction still removes the oldest entry
                     at capacity (durability != retention). A persistent CCR
                     backend (e.g. Sqlite/Redis) does not currently exist and
                     would be a net-new build.
        """
        # Import here to avoid circular imports
        from .backends import InMemoryBackend

        self._backend: CompressionStoreBackend = backend or InMemoryBackend()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._enable_feedback = enable_feedback

        # Feedback tracking
        self._retrieval_events: list[RetrievalEvent] = []
        self._max_events = 1000  # Keep last 1000 events
        self._pending_feedback_events: list[RetrievalEvent] = []

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
        tool_signature_hash: str | None = None,
        compression_strategy: str | None = None,
        ttl: int | None = None,
        explicit_hash: str | None = None,
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
            tool_signature_hash: Hash from ToolSignature for TOIN correlation.
            compression_strategy: Strategy used for compression.
            ttl: Custom TTL in seconds (uses default if not specified).
            explicit_hash: Use this exact hex hash as the storage key
                instead of computing SHA-256(original)[:24]. Required when
                the marker that points at this entry was emitted by a
                producer with its own hash function (e.g. SmartCrusher's
                Rust row-drop path uses SHA-256[:12]). If not a hex
                string, raises ``ValueError``. The marker hash and the
                store key MUST match — otherwise ``/v1/retrieve/{hash}``
                returns 404 even though the data is present.

        Returns:
            Hash key for retrieving this content. On a true hash collision
            (same key, different live content) the store KEEPS the first
            binding and refuses the overwrite — the returned key then
            resolves to the earlier content and the refusal is logged at
            ERROR.
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
        # Python store has to mirror so /v1/retrieve resolves it).
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
            hash_key = hashlib.sha256(original.encode()).hexdigest()[:24]

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
            created_at=time.time(),
            ttl=ttl if ttl is not None else self._default_ttl,
            tool_signature_hash=tool_signature_hash,
            compression_strategy=compression_strategy,
        )

        # Process pending feedback BEFORE acquiring lock for eviction.
        # This ensures feedback from entries about to be evicted is captured.
        if self._enable_feedback:
            self.process_pending_feedback()

        with self._lock:
            self._evict_if_needed()

            # Hash collision handling. If the key already exists with
            # DIFFERENT content it is a true hash collision (astronomically
            # rare at 96-/48-bit keys) — KEEP-FIRST: refuse the overwrite.
            # Rebinding would silently corrupt the already-emitted marker that
            # points at the existing entry; refusing means the NEW caller's
            # marker dangles instead, and dangles LOUDLY (error log here,
            # cause-honest miss on retrieval of the changed content) rather
            # than resolving old markers to foreign content. An expired
            # same-key entry never reaches this branch: _evict_if_needed()
            # above already reaped it, so a dead binding cannot wedge its key.
            existing = self._backend.get(hash_key)
            if existing is not None:
                if existing.original_content != original:
                    logger.error(
                        "Hash collision detected: hash=%s tool=%s (existing_len=%d, "
                        "new_len=%d) — keeping first binding, refusing overwrite; "
                        "the new content is NOT stored and its marker will miss",
                        hash_key,
                        tool_name,
                        len(existing.original_content),
                        len(original),
                    )
                    return hash_key
                # Same content being stored again - this is fine, just update
                logger.debug(
                    "Duplicate store for hash=%s, updating entry",
                    hash_key,
                )
                # Mark old heap entry as stale since we're replacing
                self._stale_heap_entries += 1

            self._backend.set(hash_key, entry)
            # Add to eviction heap for O(log n) eviction
            heapq.heappush(self._eviction_heap, (entry.created_at, hash_key))

        return hash_key

    def retrieve(
        self,
        hash_key: str,
        query: str | None = None,
    ) -> CompressionEntry | None:
        """Retrieve original content by hash.

        Args:
            hash_key: Hash key returned by store().
            query: Optional query for feedback tracking.

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
                return None

            if entry.is_expired():
                self._backend.delete(hash_key)
                # CRITICAL FIX: Track stale heap entry
                self._stale_heap_entries += 1
                return None

            # Track access for feedback
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
                    tool_signature_hash=entry.tool_signature_hash,
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

        # Process feedback immediately to ensure TOIN learns in real-time
        if self._enable_feedback:
            self.process_pending_feedback()

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

            if entry.is_expired():
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
        # Get entry without logging (we'll log the search separately)
        entry = self._get_entry_for_search(hash_key, query)
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
                    tool_signature_hash=entry.tool_signature_hash,
                )
            # Process feedback immediately to ensure TOIN learns in real-time
            self.process_pending_feedback()
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
            "event": "headroom_retrieve",
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
            "tool_signature_hash": entry.tool_signature_hash,
            "original_tokens": entry.original_tokens,
            "compressed_tokens": entry.compressed_tokens,
            "original_item_count": entry.original_item_count,
            "compressed_item_count": entry.compressed_item_count,
            **_payload_for_retrieval_log(payload),
        }
        logger.info(
            "event=headroom_retrieve %s",
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        )

    def _search_items_from_original(self, original_content: str) -> list[Any]:
        """Normalize cached originals into searchable items.

        CCR producers store different shapes:
        - SmartCrusher/search-style paths usually store JSON arrays.
        - Kompress stores the original plain text.
        - Some callers store JSON objects or scalar JSON values.

        Search should work for all of them. Preserve the legacy JSON-array
        result shape, but fall back to structured text chunks for everything
        else so `headroom_retrieve(hash, query=...)` can find plain-text
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
        Kompress originals, which are often long single-line text blobs.
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
        query: str | None = None,
    ) -> CompressionEntry | None:
        """Get entry without logging retrieval (used by search to avoid double-logging).

        CRITICAL FIX #4: Returns a copy of the entry to prevent race conditions.
        The caller may use the entry after we release the lock, and another thread
        could modify or evict the original entry.

        Args:
            hash_key: Hash key returned by store().
            query: Optional query for access tracking.

        Returns:
            CompressionEntry copy if found and not expired, None otherwise.
        """
        with self._lock:
            entry = self._backend.get(hash_key)

            if entry is None:
                return None

            if entry.is_expired():
                self._backend.delete(hash_key)
                # CRITICAL FIX: Track stale heap entry
                self._stale_heap_entries += 1
                return None

            # Track access but don't log retrieval event (search will log separately)
            entry.record_access(query)
            # Update the backend with the modified entry
            self._backend.set(hash_key, entry)

            # CRITICAL FIX #4: Return a copy to prevent race conditions
            # The entry contains mutable fields (search_queries list) that could be
            # modified by other threads after we release the lock
            return replace(entry, search_queries=list(entry.search_queries))

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
            if entry.is_expired():
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
        now = time.time()
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

    def get_retrieval_events(
        self,
        limit: int = 100,
        tool_name: str | None = None,
    ) -> list[RetrievalEvent]:
        """Get recent retrieval events for feedback analysis.

        Args:
            limit: Maximum number of events to return.
            tool_name: Filter by tool name if specified.

        Returns:
            List of recent retrieval events (copies to prevent mutation).
        """
        with self._lock:
            # Take a slice copy immediately to avoid race conditions
            # if another thread modifies _retrieval_events after we release the lock
            events_copy = list(self._retrieval_events)

        # Filter and slice outside lock (safe since we have a copy)
        if tool_name:
            events_copy = [e for e in events_copy if e.tool_name == tool_name]

        return list(reversed(events_copy[-limit:]))

    def clear(self) -> None:
        """Clear all entries. Mainly for testing."""
        with self._lock:
            self._backend.clear()
            self._retrieval_events.clear()
            self._pending_feedback_events.clear()
            self._eviction_heap.clear()  # Clear heap too
            self._stale_heap_entries = 0  # CRITICAL FIX: Reset stale counter

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
        # hit a real oldest entry. Eviction stays oldest-first and still routes
        # every delete through the ``_record_eviction_success`` / loud-miss
        # accounting (no side-door). Bounded: each rebuild yields a heap of
        # exactly the live entries, and every subsequent pop removes one, so the
        # loop terminates in O(live entries).
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
                # Real oldest entry — evict it through the normal accounting.
                if self._enable_feedback and entry.retrieval_count == 0:
                    # Entry was never retrieved = compression was successful;
                    # notify feedback so it knows this strategy worked.
                    self._record_eviction_success(entry)
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

        CRITICAL FIX: Track stale heap entries when deleting to prevent memory leak.
        """
        expired_keys = [key for key, entry in self._backend.items() if entry.is_expired()]
        for key in expired_keys:
            self._backend.delete(key)
            # CRITICAL FIX: Increment stale counter - the heap still has an entry
            # for this key that will be stale when we try to evict
            self._stale_heap_entries += 1

    def _rebuild_heap(self) -> None:
        """Rebuild heap from current store entries. Must be called with lock held.

        CRITICAL FIX: This removes stale heap entries that accumulate when entries
        are deleted or replaced. Without this, the heap grows unboundedly.
        """
        # Build new heap from current store entries only
        self._eviction_heap = [
            (entry.created_at, hash_key) for hash_key, entry in self._backend.items()
        ]
        heapq.heapify(self._eviction_heap)
        # Reset stale counter - heap is now clean
        self._stale_heap_entries = 0
        logger.debug(
            "Rebuilt eviction heap: %d entries",
            len(self._eviction_heap),
        )

    def _record_eviction_success(self, entry: CompressionEntry) -> None:
        """Record successful compression when an entry is evicted without retrieval.

        HIGH FIX: State divergence on eviction
        When an entry is evicted and was NEVER retrieved, this indicates the
        compression was fully successful - the LLM never needed the original data.
        We notify the feedback system so it can learn from this success.

        Must be called with lock held (entry data access).
        Actual feedback notification happens outside lock.

        Args:
            entry: The entry being evicted.
        """
        # Capture entry data while we have the lock
        tool_name = entry.tool_name
        sig_hash = entry.tool_signature_hash
        strategy = entry.compression_strategy

        # We can't call feedback while holding the lock (would cause deadlock)
        # Instead, queue this for deferred processing
        if sig_hash is not None and strategy is not None:
            # Create a synthetic "success" event that we'll process later
            # Use a special retrieval type to indicate this was an eviction success
            success_event = RetrievalEvent(
                hash=entry.hash,
                query=None,
                items_retrieved=0,  # No retrieval happened
                total_items=entry.original_item_count,
                tool_name=tool_name,
                timestamp=time.time(),
                retrieval_type="eviction_success",  # Special marker
                tool_signature_hash=sig_hash,
            )
            self._pending_feedback_events.append(success_event)
            logger.debug(
                "Recorded eviction success: hash=%s strategy=%s",
                entry.hash[:8],
                strategy,
            )

    def _log_retrieval(
        self,
        hash_key: str,
        query: str | None,
        items_retrieved: int,
        total_items: int,
        tool_name: str | None,
        retrieval_type: str,
        tool_signature_hash: str | None = None,
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
            tool_signature_hash=tool_signature_hash,
        )

        self._retrieval_events.append(event)

        # Keep only recent events
        if len(self._retrieval_events) > self._max_events:
            self._retrieval_events = self._retrieval_events[-self._max_events :]

        # Queue event for feedback processing (will be processed after lock release)
        # This is safe because process_pending_feedback() uses the lock to atomically
        # swap out the pending list before processing
        self._pending_feedback_events.append(event)

    def process_pending_feedback(self) -> None:
        """Process pending feedback events.

        Forwards events to:
        1. CompressionFeedback - for learning compression hints
        2. TelemetryCollector - for the data flywheel
        3. TOIN - for cross-user intelligence network

        This is called automatically on each retrieval to ensure the
        feedback loop operates in real-time.
        """
        from ..telemetry import get_telemetry_collector
        from ..telemetry.toin import get_toin
        from .compression_feedback import get_compression_feedback

        # Get pending events and related entry data atomically
        with self._lock:
            events = self._pending_feedback_events
            self._pending_feedback_events = []

            # Gather entry data while holding lock to avoid race conditions
            event_data: list[
                tuple[RetrievalEvent, str | None, str | None, str | None, str | None]
            ] = []
            for event in events:
                entry = self._backend.get(event.hash)
                if entry:
                    # Use the ACTUAL tool_signature_hash stored during compression
                    # This MUST match the hash used by SmartCrusher
                    event_data.append(
                        (
                            event,
                            entry.tool_name,
                            entry.tool_signature_hash,  # The correct hash!
                            entry.compression_strategy,
                            entry.compressed_content,  # For TOIN field-level learning
                        )
                    )
                else:
                    event_data.append((event, None, None, None, None))

        # Process outside lock
        if event_data:
            feedback = get_compression_feedback()
            telemetry = get_telemetry_collector()
            toin = get_toin()

            for event, _tool_name, sig_hash, strategy, compressed_content in event_data:
                # Notify feedback system (pass strategy for success rate tracking)
                feedback.record_retrieval(event, strategy=strategy)

                # Extract query fields if present
                query_fields = None
                if event.query:
                    # Extract field:value patterns
                    query_fields = re.findall(r"(\w+)[=:]", event.query)

                # Notify telemetry for data flywheel
                try:
                    if sig_hash is not None:
                        telemetry.record_retrieval(
                            tool_signature_hash=sig_hash,
                            retrieval_type=event.retrieval_type,
                            query_fields=query_fields,
                        )
                except Exception:
                    # Telemetry should never break the feedback loop
                    logger.debug("Telemetry record_retrieval failed", exc_info=True)

                # Parse compressed content to extract items for TOIN field-level learning
                retrieved_items: list[dict[str, Any]] | None = None
                if compressed_content:
                    try:
                        parsed = json.loads(compressed_content)
                        # Handle both direct arrays and wrapped arrays
                        if isinstance(parsed, list):
                            # Filter to dicts only (field learning needs dict items)
                            retrieved_items = [item for item in parsed if isinstance(item, dict)]
                        elif isinstance(parsed, dict):
                            # Check for common wrapper patterns: {"items": [...], "results": [...]}
                            for key in ("items", "results", "data", "records"):
                                if key in parsed and isinstance(parsed[key], list):
                                    retrieved_items = [
                                        item for item in parsed[key] if isinstance(item, dict)
                                    ]
                                    break
                    except (json.JSONDecodeError, TypeError):
                        # Invalid JSON - skip field learning for this retrieval
                        pass

                # Notify TOIN for cross-user learning
                try:
                    if sig_hash is not None:
                        toin.record_retrieval(
                            tool_signature_hash=sig_hash,
                            retrieval_type=event.retrieval_type,
                            query=event.query,
                            query_fields=query_fields,
                            strategy=strategy,  # Pass strategy for success rate tracking
                            retrieved_items=retrieved_items,  # For field-level learning
                        )
                except Exception:
                    # TOIN should never break the feedback loop
                    logger.debug("TOIN record_retrieval failed", exc_info=True)


# Request-scoped store (for multi-tenant SaaS: one store per request/tenant)
_request_ccr_store: ContextVar[CompressionStore | None] = ContextVar(
    "headroom_request_ccr_store", default=None
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


def _create_default_ccr_backend() -> CompressionStoreBackend | None:
    """Create a CCR backend from env (e.g. HEADROOM_CCR_BACKEND=<your-plugin-name>).

    Loads adapters via setuptools entry point 'headroom.ccr_backend'.
    Returns None to use default InMemoryBackend.
    """
    backend_type = (os.environ.get("HEADROOM_CCR_BACKEND") or "").strip().lower()
    if not backend_type or backend_type == "memory":
        return None
    try:
        from importlib.metadata import entry_points

        all_eps = entry_points(group="headroom.ccr_backend")
        ep = next((e for e in all_eps if e.name == backend_type), None)
        if ep is None:
            logger.warning(
                "HEADROOM_CCR_BACKEND=%s but no entry point headroom.ccr_backend[%s]",
                backend_type,
                backend_type,
            )
            return None
        fn = ep.load()
        kwargs = {
            "url": os.environ.get("HEADROOM_REDIS_URL", ""),
            "tenant_prefix": os.environ.get("HEADROOM_CCR_TENANT_PREFIX", ""),
        }
        backend: CompressionStoreBackend = fn(**kwargs)
        return backend
    except Exception as e:
        logger.warning("Failed to load CCR backend %s: %s", backend_type, e)
        return None


def get_compression_store(
    max_entries: int = 1000,
    default_ttl: int | None = None,
    backend: CompressionStoreBackend | None = None,
) -> CompressionStore:
    """Get the compression store instance.

    If a request-scoped store was set (e.g. by SaaS middleware), returns it.
    Otherwise uses lazy-initialized global singleton. Backend can be supplied
    explicitly or created from env (HEADROOM_CCR_BACKEND) when building the global.

    Args:
        max_entries: Maximum entries (only used on first call for global store).
        default_ttl: Default TTL (only used on first call for global store).
            When omitted, HEADROOM_CCR_TTL_SECONDS overrides the 300-second default.
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
                _compression_store = CompressionStore(
                    max_entries=max_entries,
                    default_ttl=effective_default_ttl,
                    backend=backend,
                )
    return _compression_store


def reset_compression_store() -> None:
    """Reset the global compression store. Mainly for testing."""
    global _compression_store

    with _store_lock:
        if _compression_store is not None:
            _compression_store.clear()
        _compression_store = None
