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
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar, cast

from .. import paths as _paths
from ..relevance.bm25 import BM25Scorer
from .storage_safety import MutationGuard, StorageUnavailableError

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


class CollisionSafetyError(DurableWriteError):
    """Veto an explicit-hash write when its spill collision domain is uncertain.

    This subclasses :class:`DurableWriteError` so marker-producing callers that
    already veto on durable-store failures also veto here. The attempted new
    binding is never persisted: an unreadable or uncleanable same-key spill row
    must not become foreign content for the new producer.
    """


# Skip expensive marker regex scans when no marker hint exists, but case-fold the hint because bracket markers accept uppercase `HASH=`.
_MARKER_HINTS: Final = ("ccr:", "hash=")


def _may_reference_marker(text: str) -> bool:
    """Whether *text* might carry a CCR marker (cheap, case-insensitive).

    Uses ``casefold``, not ``lower`` (review B6): the grammar's ``re.IGNORECASE``
    applies full Unicode case-folding, which is WIDER than ``str.lower``, so this
    pre-check must fold at least as widely or it skips entries the grammar would
    have matched. Measured bypass: ``ſ`` (U+017F LATIN SMALL LETTER LONG S) is
    unchanged by ``lower`` but folds to ``s``, so a marker written
    ``[10 rows compressed to 2. Retrieve more: haſh=<24hex>]`` matched the grammar
    while this screen returned False, silently orphaning the nested blob.

    Over-matching is safe here: a false positive only costs one grammar scan that
    finds nothing. A false NEGATIVE is a data-safety bug, which is the asymmetry
    this screen must be tuned for.
    """
    folded = text.casefold()
    return any(hint in folded for hint in _MARKER_HINTS)


@dataclass(frozen=True)
class CascadeOutcome:
    """What one :meth:`CompressionStore.delete_cascade_detailed` actually did.

    ``nested_deleted`` is the DISTINCT nested hashes the cascade removed;
    ``nested_shared_skipped`` is those it deliberately left because another live
    entry still references them (RG3). ``failed_hashes`` names hashes the cascade
    attempted to remove but proved still reachable from at least one tier. Those
    failures propagate to the purge read-back so partial mutation cannot be
    reported as a clean erase. ``deleted_hashes`` is the full set a read-back
    should find gone (RG6) — the top hash only when it truly went.
    """

    top_deleted: bool
    nested_deleted: tuple[str, ...] = ()
    nested_shared_skipped: tuple[str, ...] = ()
    failed_hashes: tuple[str, ...] = ()

    def deleted_hashes(self, hash_key: str) -> tuple[str, ...]:
        """Every hash this cascade decided to delete, for read-back verification."""
        top = (hash_key,) if self.top_deleted else ()
        return top + self.nested_deleted


@dataclass(frozen=True)
class CascadePlanNode:
    existed: bool
    nested: tuple[str, ...]


# Default CCR TTL is 30 minutes so markers survive normal agent sessions. Keep
# Python and Rust defaults equal; operators may override `FURL_CCR_TTL_SECONDS`.
DEFAULT_CCR_TTL_SECONDS = 1800
CCR_TTL_SECONDS_ENV = "FURL_CCR_TTL_SECONDS"

# Retry required-durable writes three times with capped exponential backoff before vetoing. This
# absorbs brief cross-process SQLite contention while keeping sustained lock failures loud and bounded.
_DURABLE_RETRY_MAX_ATTEMPTS = 3
_DURABLE_RETRY_BASE_BACKOFF_SECONDS = 0.05
_DURABLE_RETRY_MAX_BACKOFF_SECONDS = 0.20

# Minimum length for a caller-supplied ``explicit_hash``. This is the LOOSE recovery-floor contract, intentionally distinct from the STRICT consumer set ``marker_grammar.HASH_WIDTHS`` ({12,
# 24}) that the anti-spoofing ingress (the MCP retrieve handler, via ``marker_grammar.is_valid_ccr_hash``) enforces — see the "Two DISTINCT width contracts" note in ``ccr/the module``.
_MIN_EXPLICIT_HASH_LEN = 6

# Binding records must never be content-derived verifiers: arbitrary explicit
# hashes get an opaque random provenance id. Internally-computed hashes carry a
# non-secret mode marker because their key already is the content address.
_BINDING_ID_PREFIX: Final = "id:v1:"
_CONTENT_DERIVED_BINDING_ID: Final = "content:v1"


def _new_binding_id() -> str:
    return f"{_BINDING_ID_PREFIX}{secrets.token_hex(16)}"


def _is_legacy_binding_value(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


# Sentinel for "this backend does not declare a ``max_rows`` at all", kept distinct from a declared ``None`` ("no physical row cap").
_ROW_CAP_UNDECLARED: Final = object()

_RETRIEVAL_LOG_PREVIEW_CHARS = 4096
# Preview-snippet length for cross-store search (``search_all``) hits.
_CROSS_STORE_PREVIEW_CHARS = 200
# ReDoS guard (PERF/SEC). Every preview/log surface keeps only a bounded head bounding the regex input to a constant. The margin lets a
# secret straddling the budget edge be seen whole and masked before truncation; real secrets (keys/tokens/URL passwords) fit inside it.
_REDACT_WINDOW_MARGIN_CHARS = 256
# Match ``<sensitive-key><sep><value>`` in both plain (``api_key=...``) and JSON quoted-key (``"api_key": "..."``) form. Group 2 allows an OPTIONAL closing quote before the
# separator. The value itself is CONDITIONAL on group 3 (SEC-4d): when an opening quote was captured, match to the closing quote (``\\.`` steps over JSON-escaped quotes.
_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)\b([A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)[A-Z0-9_-]*)"
    r"([\"']?\s*[:=]\s*)([\"'])?(?(3)(?:\\.|(?!\3).)*|[^\"'\s,}]+)"
)
_AUTH_VALUE_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}")
_API_KEY_VALUE_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
# Provider-issued tokens that are recognizable by prefix alone, so they leak even when the surrounding key name is absent or unrecognized (a bare
# ``AKIA...`` / ``ghp_...`` in free text). AWS access-key IDs are ``AKIA`` + 16 uppercase alnum; GitHub tokens are ``gh[opsru]_`` + >=36 alnum.
_PROVIDER_TOKEN_RE = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[opsru]_[A-Za-z0-9]{36,})\b")
# SEC-4a Only the password is cut; user and host survive so the log line stays operationally useful.
_URL_CREDENTIAL_RE = re.compile(r"(://[^/?#\s:@]{1,128}:)([^@/\s]{1,256})@")
# SEC-4b
_PEM_ARMOR = "PRIVATE" + " KEY-----"
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]* " + _PEM_ARMOR + r"[\s\S]*?-----END[A-Z0-9 ]* " + _PEM_ARMOR
)
# SEC-4c — bare JWTs: ``eyJ`` (base64 of ``{"``) + two dot-joined base64url segments, no ``Bearer`` prefix and no sensitive key name required.
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
    instead of implying TTL alone.
    """
    default_ttl = status.get("default_ttl_seconds", DEFAULT_CCR_TTL_SECONDS)
    ttl_seconds = status.get("ttl_seconds", default_ttl)

    if status.get("status") == "unavailable":
        return (
            "CCR storage is temporarily unavailable; this read could not prove the hash "
            "absent or retrieve its content. Retry the retrieval rather than recomputing "
            "from a false missing result."
        )

    if status.get("status") == "unsafe":
        return (
            "CCR found data for this hash but could not verify that the hash is bound to "
            "that content. The entry is quarantined rather than serving potentially "
            "foreign bytes; recompute the source content."
        )

    if status.get("status") == "available":
        return (
            "Entry is available in the CCR store, but this retrieval attempt returned "
            "no content. Retry the retrieval."
        )

    if status.get("status") == "expired":
        age_seconds = status.get("age_seconds")
        if isinstance(age_seconds, (int, float)):
            return f"Entry expired (CCR TTL: {ttl_seconds} seconds; age: {age_seconds:.0f} seconds)"
        return f"Entry expired (CCR TTL: {ttl_seconds} seconds)"

    max_entries = status.get("max_entries")
    capacity_note = f" Store capacity is {max_entries} entries." if max_entries else ""
    return (
        f"No entry for this hash in the CCR store. It may never have been "
        f"stored, or it may have been purged, evicted under capacity pressure, "
        f"or expired past its TTL of {default_ttl}s.{capacity_note} "
        f"Recompute the source content."
    )


def _redact_retrieval_log_payload(payload: str) -> str:
    # Order matters. PEM blocks and URL credentials go FIRST: they are structural multi-token shapes, redacted whole before the generic
    # rules can chew on fragments of them. Then ``Bearer``/``Basic`` scheme tokens BEFORE the secret-key rule so the scheme anchor survives.
    redacted = _PEM_PRIVATE_KEY_RE.sub("[REDACTED]", payload)
    redacted = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", redacted)
    redacted = _AUTH_VALUE_RE.sub(r"\1 [REDACTED]", redacted)
    redacted = _SECRET_KEY_VALUE_RE.sub(r"\1\2\3[REDACTED]", redacted)
    redacted = _API_KEY_VALUE_RE.sub("sk-[REDACTED]", redacted)
    redacted = _PROVIDER_TOKEN_RE.sub("[REDACTED]", redacted)
    return _BARE_JWT_RE.sub("[REDACTED]", redacted)


def _payload_for_retrieval_log(payload: str) -> dict[str, Any]:
    # SLICE-before-REDACT (ReDoS guard, see ``_REDACT_WINDOW_MARGIN_CHARS``). Redacting the FULL payload first is O(N^2) on unbroken base64url/hex runs and this fires
    # on EVERY retrieve (full-body log redact). The preview keeps only ``_RETRIEVAL_LOG_PREVIEW_CHARS`` regardless, so redact just the bounded window that feeds it.
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

    compression_strategy: str | None = None
    binding_id: str | None = None

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


@dataclass(frozen=True)
class StoreRead:
    entry: CompressionEntry | None
    status: dict[str, Any]
    source: str | None = None


@dataclass(frozen=True)
class CrossStoreSearchOutcome:
    matches: tuple[CrossStoreMatch, ...]
    complete: bool
    unavailable: tuple[str, ...] = ()


_F = TypeVar("_F", bound=Any)


def _serialized_mutation(method: _F) -> _F:
    """Hold one logical mutation guard across a public store mutation."""

    @wraps(method)
    def wrapped(self: CompressionStore, *args: Any, **kwargs: Any) -> Any:
        with self._mutation_guard.hold():
            return method(self, *args, **kwargs)

    return cast(_F, wrapped)


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

        # Validate backend durability capabilities at construction. Missing `durable`/`set_durable` is a contract error
        # because shipping a recovery marker without verified durability is unsafe; fail early with a clear `TypeError`.
        missing = [
            member for member in ("durable", "set_durable") if not hasattr(self._backend, member)
        ]
        if missing:
            raise TypeError(
                f"CCR backend {type(self._backend).__name__!r} is missing required "
                f"CompressionStoreBackend member(s): {', '.join(missing)}. The store "
                f"cannot report durability for it, and treating that as 'durable' "
                f"would let a require_durable write pass unchecked. See "
                f"furl_ctx.cache.backends.base.CompressionStoreBackend."
            )

        # Warn when a backend's physical `max_rows` cap can bind before logical `max_entries`, or when the cap is undeclared. This
        # is advisory, so `getattr` preserves undeclared/None/int states; durability remains a strict direct-access contract.
        declared_max_rows = getattr(self._backend, "max_rows", _ROW_CAP_UNDECLARED)
        if declared_max_rows is _ROW_CAP_UNDECLARED:
            # WARNING, not DEBUG, and deliberately at the SAME level as the inversion warning below. "The invariant cannot be checked at
            # all" is strictly less knowable than "checked and found inverted", so reporting it more quietly would inverts the severities.
            logger.warning(
                "CompressionStore backend %s declares no max_rows; the cap-ordering "
                "invariant cannot be verified for it",
                type(self._backend).__name__,
            )
        backend_max_rows = None if declared_max_rows is _ROW_CAP_UNDECLARED else declared_max_rows
        if isinstance(backend_max_rows, int) and backend_max_rows < max_entries:
            logger.warning(
                "CompressionStore max_entries=%d exceeds the backend physical "
                "row cap max_rows=%d; the file cap will evict oldest-first (no "
                "TTL ordering) before the logical cap binds, inverting the "
                "documented cap ordering — set FURL_CCR_SQLITE_MAX_ROWS at or "
                "above max_entries to restore it.",
                max_entries,
                backend_max_rows,
            )
        self._enable_feedback = enable_feedback
        self._now: Callable[[], float] = now_fn or time.time

        if durable_retry_attempts < 0:
            raise ValueError(f"durable_retry_attempts must be >= 0, got {durable_retry_attempts!r}")
        if durable_retry_base_backoff_seconds < 0 or durable_retry_max_backoff_seconds < 0:
            raise ValueError("durable retry backoff seconds must be >= 0")
        self._durable_retry_attempts = durable_retry_attempts
        self._durable_retry_base_backoff_seconds = durable_retry_base_backoff_seconds
        self._durable_retry_max_backoff_seconds = durable_retry_max_backoff_seconds

        backends = tuple(b for b in (self._backend, self._spill) if b is not None)
        coordinated = next((b for b in backends if getattr(b, "coordination_identity", None)), None)
        identity = (
            str(getattr(coordinated, "coordination_identity", None))
            if coordinated is not None
            else f"memory:{id(self)}"
        )
        self._mutation_guard = MutationGuard(identity)
        binding_candidates = [
            b
            for b in backends
            if callable(getattr(b, "claim_binding", None))
            and callable(getattr(b, "get_binding", None))
        ]
        self._binding_backend = next(
            (b for b in binding_candidates if bool(getattr(b, "durable", False))),
            binding_candidates[0] if binding_candidates else None,
        )
        # Same-process fallback bindings are evidence only for rows this store
        # itself wrote while durable identity authority was unavailable. Values
        # are opaque provenance ids, never hashes of original content.
        self._volatile_bindings: dict[str, str] = {}
        self._migrate_legacy_bindings()

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

    @staticmethod
    def _is_marker_key(hash_key: str) -> bool:
        return len(hash_key) in {12, 24} and all(c in "0123456789abcdef" for c in hash_key)

    def _checked_get_backend_locked(
        self, backend: Any, hash_key: str, role: str
    ) -> list[CompressionEntry]:
        checked = getattr(backend, "checked_get_all", None)
        try:
            if callable(checked):
                return list(checked(hash_key))
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked read capability"
                )
            entry = backend.get(hash_key)
            return [] if entry is None else [entry]
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} read failed for {hash_key}") from exc

    def _checked_items_backend_locked(
        self, backend: Any, role: str
    ) -> list[tuple[str, CompressionEntry]]:
        checked = getattr(backend, "checked_items", None)
        try:
            if callable(checked):
                return list(checked())
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked enumeration capability"
                )
            return list(backend.items())
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} enumeration failed") from exc

    def _checked_index_backend_locked(self, backend: Any, role: str) -> list[tuple[float, str]]:
        checked = getattr(backend, "checked_created_at_index", None)
        try:
            if callable(checked):
                return list(checked())
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked index capability"
                )
            index = getattr(backend, "created_at_index", None)
            if callable(index):
                return list(index())
            return [(entry.created_at, key) for key, entry in backend.items()]
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} index read failed") from exc

    def _checked_delete_backend_locked(self, backend: Any, hash_key: str, role: str) -> bool:
        checked = getattr(backend, "checked_delete", None)
        try:
            if callable(checked):
                return bool(checked(hash_key))
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked delete capability"
                )
            return bool(backend.delete(hash_key))
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} delete failed for {hash_key}") from exc

    def _checked_clear_backend_locked(self, backend: Any, role: str) -> None:
        checked = getattr(backend, "checked_clear", None)
        try:
            if callable(checked):
                checked()
                return
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked clear capability"
                )
            backend.clear()
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"{role} clear failed") from exc

    def _read_backend_candidates_locked(
        self, backend: Any, hash_key: str, role: str
    ) -> tuple[list[CompressionEntry], bool, str | None]:
        """Best-effort read with an explicit completeness bit."""
        reader = getattr(backend, "read_candidates", None)
        try:
            if callable(reader):
                entries, complete = reader(hash_key)
                reason = None if complete else f"{role} storage could not be fully inspected"
                return list(entries), bool(complete), reason
            entry = backend.get(hash_key)
            return ([] if entry is None else [entry]), True, None
        except Exception as exc:
            logger.warning("CCR %s read failed (non-fatal): %s", role, exc)
            return [], False, f"{role} read failed for {hash_key}: {exc}"

    def _representations_locked(self, hash_key: str) -> list[tuple[str, CompressionEntry]]:
        rows = [
            ("primary", entry)
            for entry in self._checked_get_backend_locked(self._backend, hash_key, "primary")
        ]
        if self._spill is not None:
            rows.extend(
                ("spill", entry)
                for entry in self._checked_get_backend_locked(self._spill, hash_key, "spill")
            )
        return rows

    def _set_entry_binding_id_locked(
        self, backend: Any, hash_key: str, binding_id: str, role: str
    ) -> bool:
        setter = getattr(backend, "checked_set_binding_id", None)
        try:
            if callable(setter):
                return bool(setter(hash_key, binding_id))
            if bool(getattr(backend, "durable", False)):
                raise StorageUnavailableError(
                    f"durable {role} backend has no checked binding-id update capability"
                )
            entry = backend.get(hash_key)
            if entry is None:
                return False
            entry.binding_id = binding_id
            backend.set(hash_key, entry)
            return True
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(
                f"{role} binding-id update failed for {hash_key}"
            ) from exc

    def _migrate_legacy_bindings(self) -> None:
        """Remove pre-v1 content digests from durable identity metadata.

        A legacy binding stored SHA-256(original), which becomes an offline
        verifier for low-entropy secrets once its payload is gone. Under the
        cross-process mutation guard, one unambiguous live legacy row is assigned
        a random opaque provenance id. Orphaned/divergent legacy tombstones are
        replaced by a random id and permanently conflicted, preserving the
        no-foreign-content guarantee without retaining the old verifier.
        """
        backend = self._binding_backend
        if backend is None:
            return
        lister = getattr(backend, "checked_bindings", None)
        replacer = getattr(backend, "replace_binding", None)
        if not callable(lister) or not callable(replacer):
            return

        with self._mutation_guard.hold():
            with self._lock:
                try:
                    records = list(lister())
                except StorageUnavailableError as exc:
                    logger.warning("CCR legacy binding migration unavailable: %s", exc)
                    return
                now = self._now()
                for hash_key, stored_value, conflicted in records:
                    if not _is_legacy_binding_value(str(stored_value)):
                        continue
                    replacement_id = _new_binding_id()
                    if conflicted:
                        replacer(hash_key, replacement_id, True)
                        continue
                    try:
                        rows = self._representations_locked(hash_key)
                    except StorageUnavailableError as exc:
                        logger.warning(
                            "CCR legacy binding %s cannot inspect payloads; retiring identity: %s",
                            hash_key,
                            exc,
                        )
                        replacer(hash_key, replacement_id, True)
                        continue

                    live = [(role, entry) for role, entry in rows if not entry.is_expired(now)]
                    originals = {entry.original_content for _role, entry in live}
                    existing_ids = {
                        entry.binding_id
                        for _role, entry in live
                        if entry.binding_id and entry.binding_id != _CONTENT_DERIVED_BINDING_ID
                    }
                    if len(originals) != 1 or len(existing_ids) > 1:
                        replacer(hash_key, replacement_id, True)
                        continue
                    if existing_ids:
                        replacement_id = next(iter(existing_ids))

                    try:
                        for role, _entry in live:
                            target = self._backend if role == "primary" else self._spill
                            if target is None or not self._set_entry_binding_id_locked(
                                target, hash_key, replacement_id, role
                            ):
                                raise StorageUnavailableError(
                                    f"{role} payload disappeared during legacy binding migration"
                                )
                        replacer(hash_key, replacement_id, False)
                    except StorageUnavailableError as exc:
                        logger.warning(
                            "CCR legacy binding %s migration became indeterminate; retiring identity: %s",
                            hash_key,
                            exc,
                        )
                        replacer(hash_key, _new_binding_id(), True)

    def _binding_record_locked(self, hash_key: str) -> tuple[str, bool] | None:
        backend = self._binding_backend
        if backend is None:
            return None
        getter = getattr(backend, "get_binding", None)
        if not callable(getter):
            return None
        try:
            record = getter(hash_key)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding read failed for {hash_key}") from exc
        if record is None:
            return None
        fingerprint, conflicted = record
        return str(fingerprint), bool(conflicted)

    def _binding_state_locked(
        self,
        hash_key: str,
        entries: list[CompressionEntry],
        *,
        complete: bool,
    ) -> str:
        """Return ``safe``, ``unsafe`` or ``unavailable`` for one key."""
        if not entries:
            return "safe"
        if len({entry.original_content for entry in entries}) != 1:
            return "unsafe"
        if not self._is_marker_key(hash_key):
            return "safe"

        binding_ids = {entry.binding_id for entry in entries}
        if len(binding_ids) != 1:
            return "unsafe"
        binding_id = next(iter(binding_ids))
        if binding_id == _CONTENT_DERIVED_BINDING_ID:
            return "safe"
        if binding_id is None:
            return "unsafe" if complete else "unavailable"

        try:
            record = self._binding_record_locked(hash_key)
        except StorageUnavailableError:
            return "safe" if self._volatile_bindings.get(hash_key) == binding_id else "unavailable"
        if record is not None:
            recorded, conflicted = record
            return "safe" if not conflicted and recorded == binding_id else "unsafe"
        if self._volatile_bindings.get(hash_key) == binding_id:
            return "safe"
        return "unsafe" if complete else "unavailable"

    def _binding_safe_locked(self, hash_key: str, entries: list[CompressionEntry]) -> bool:
        state = self._binding_state_locked(hash_key, entries, complete=True)
        if state == "unavailable":
            raise StorageUnavailableError(
                f"hash/content binding authority unavailable for {hash_key}"
            )
        return state == "safe"

    def _claim_binding_locked(self, hash_key: str, binding_id: str) -> str:
        if not self._is_marker_key(hash_key):
            return "same"
        backend = self._binding_backend
        if backend is None:
            raise StorageUnavailableError(
                f"no authoritative binding store exists for explicit hash {hash_key}"
            )
        claimer = getattr(backend, "claim_binding", None)
        if not callable(claimer):
            raise StorageUnavailableError(
                f"binding backend cannot atomically claim explicit hash {hash_key}"
            )
        try:
            return str(claimer(hash_key, binding_id))
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding claim failed for {hash_key}") from exc

    def _poison_binding_locked(
        self, hash_key: str, old_binding_id: str | None, new_binding_id: str | None
    ) -> None:
        if not self._is_marker_key(hash_key) or self._binding_backend is None:
            return
        claimer = getattr(self._binding_backend, "claim_binding", None)
        if not callable(claimer):
            raise StorageUnavailableError(f"binding backend cannot poison {hash_key}")
        old_id = old_binding_id or _new_binding_id()
        new_id = new_binding_id or _new_binding_id()
        if new_id == old_id:
            new_id = _new_binding_id()
        try:
            claimer(hash_key, old_id)
            claimer(hash_key, new_id)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding poison failed for {hash_key}") from exc

    def _release_binding_locked(self, hash_key: str) -> None:
        backend = self._binding_backend
        if backend is None:
            return
        release = getattr(backend, "release_binding", None)
        if not callable(release):
            return
        try:
            release(hash_key)
        except StorageUnavailableError:
            raise
        except Exception as exc:
            raise StorageUnavailableError(f"binding release failed for {hash_key}") from exc

    def _read_live_entry_locked(self, hash_key: str) -> StoreRead:
        base: dict[str, Any] = {
            "hash": hash_key,
            "default_ttl_seconds": self._default_ttl,
            "max_entries": self._max_entries,
        }
        rows: list[tuple[str, CompressionEntry]] = []
        complete = True
        reasons: list[str] = []
        for role, backend in (("primary", self._backend), ("spill", self._spill)):
            if backend is None:
                continue
            entries, tier_complete, reason = self._read_backend_candidates_locked(
                backend, hash_key, role
            )
            rows.extend((role, entry) for entry in entries)
            complete = complete and tier_complete
            if reason:
                reasons.append(reason)

        now = self._now()
        live = [(role, entry) for role, entry in rows if not entry.is_expired(now)]
        if not live:
            if not complete:
                return StoreRead(
                    None,
                    {
                        **base,
                        "status": "unavailable",
                        "reason": "; ".join(reasons),
                    },
                )
            if rows:
                entry = rows[0][1]
                return StoreRead(
                    None,
                    {
                        **base,
                        "status": "expired",
                        "ttl_seconds": entry.ttl,
                        "created_at": entry.created_at,
                        "expires_at": entry.created_at + entry.ttl,
                        "age_seconds": now - entry.created_at,
                    },
                )
            return StoreRead(None, {**base, "status": "missing"})

        state = self._binding_state_locked(
            hash_key, [entry for _role, entry in live], complete=complete
        )
        if state != "safe":
            return StoreRead(
                None,
                {
                    **base,
                    "status": state,
                    "reason": (
                        "; ".join(reasons)
                        if state == "unavailable" and reasons
                        else "hash/content binding is ambiguous or unproven"
                    ),
                },
            )

        source, entry = next((row for row in live if row[0] == "primary"), live[0])
        copied = replace(entry, search_queries=list(entry.search_queries))
        return StoreRead(
            copied,
            {
                **base,
                "status": "available",
                "ttl_seconds": entry.ttl,
                "created_at": entry.created_at,
                "expires_at": entry.created_at + entry.ttl,
                "age_seconds": now - entry.created_at,
                "source": source,
                "complete": complete,
            },
            source,
        )

    def _delete_hash_verified_locked(self, hash_key: str, *, release_binding: bool) -> bool:
        primary_deleted = self._checked_delete_backend_locked(self._backend, hash_key, "primary")
        if primary_deleted:
            self._stale_heap_entries += 1
        spill_deleted = False
        if self._spill is not None:
            spill_deleted = self._checked_delete_backend_locked(self._spill, hash_key, "spill")
        residual = self._representations_locked(hash_key)
        if residual:
            raise StorageUnavailableError(
                f"hash {hash_key} remained reachable after verified delete"
            )
        if release_binding:
            self._release_binding_locked(hash_key)
        return primary_deleted or spill_deleted

    @property
    def default_ttl_seconds(self) -> int:
        """Default TTL applied to new entries when callers do not override it."""
        return self._default_ttl

    @_serialized_mutation
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
                Rust row-drop path emits SHA-256[:24] for new content, and
                SHA-256[:12] for legacy keys only). If not a hex
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
        # content_kind threading: a writer that did not attribute a tool (the router CCR offload, SmartCrusher on a single wrapped tool
        # output) inherits the request-scoped originating tool that compress() bound for this call, so its entry still surfaces a content_kind.
        if tool_name is None:
            tool_name = _request_tool_name.get()

        # Reject a non-positive TTL loudly. ttl=0 (or negative) produces an entry that is_expired() immediately (time.time()-created_at
        # > 0) so it would be stored in the backend + heap, never retrievable, and leak until the next store() reaps it.
        if ttl is not None and ttl <= 0:
            raise ValueError(
                f"ttl must be a positive number of seconds (or None for the default), "
                f"got {ttl!r} — a non-positive ttl creates an immediately-expired entry"
            )

        # Generate hash from original content. 24 chars (96 bits) was chosen for collision resistance
        # under the birthday bound: 50% collision probability at ~280 trillion entries (2^48).
        if explicit_hash is not None:
            # Validate as hex and bail LOUDLY on a bad key: silently falling back to the computed default when the
            # caller asked for a specific key would break the marker<->store consistency the recovery plane needs.
            if not explicit_hash or not all(c in "0123456789abcdefABCDEF" for c in explicit_hash):
                raise ValueError(
                    f"explicit_hash must be a non-empty hex string, got {explicit_hash!r}"
                )
            # Reject trivially-collidable short keys (e.g. a 1-char hash).
            if len(explicit_hash) < _MIN_EXPLICIT_HASH_LEN:
                raise ValueError(
                    f"explicit_hash must be at least {_MIN_EXPLICIT_HASH_LEN} hex chars "
                    f"(collidable below that), got {explicit_hash!r} ({len(explicit_hash)} chars)"
                )
            hash_key = explicit_hash.lower()
        else:
            # SHA-256 truncated to 24 hex chars (96 bits) — same collision space as the MD5[:24] this replaced. Switched from MD5 to silence CodeQL's
            # `py/weak-sensitive-data-hashing` rule (the `usedforsecurity=False` parameter and the `lgtm` comment marker both failed to suppress it).
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
            binding_id=(_CONTENT_DERIVED_BINDING_ID if explicit_hash is None else None),
        )

        durable = False
        collision_dropped = False
        with self._lock:
            self._evict_if_needed()

            # Hash collision handling. A caller-supplied key can alias unrelated
            # content, so live representations are inspected before any rebind.
            existing = self._backend.get(hash_key)
            spilled: CompressionEntry | None = None
            expired_spill = False
            if self._spill is not None:
                if explicit_hash is None:
                    spilled = self._recover_from_spill(hash_key)
                else:
                    try:
                        spilled = self._spill.get(hash_key)
                    except Exception as exc:
                        logger.error(
                            "CCR spill collision-domain read failed for explicit hash %s; "
                            "refusing the new binding: %s",
                            hash_key,
                            exc,
                        )
                        raise CollisionSafetyError(
                            f"Cannot safely bind explicit hash {hash_key}: the spill tier "
                            "could not be inspected for an older same-key binding.",
                            hash_key=hash_key,
                        ) from exc
                    if spilled is not None and spilled.is_expired(self._now()):
                        expired_spill = True
                        spilled = None

            if explicit_hash is not None and existing is None and expired_spill:
                try:
                    self._delete_hash_verified_locked(hash_key, release_binding=False)
                except StorageUnavailableError as exc:
                    raise CollisionSafetyError(
                        f"Cannot safely retire expired binding {hash_key}: {exc}",
                        hash_key=hash_key,
                    ) from exc

            conflicting = next(
                (
                    candidate
                    for candidate in (existing, spilled)
                    if candidate is not None and candidate.original_content != original
                ),
                None,
            )
            binding_conflict = False
            if explicit_hash is not None and conflicting is None:
                candidate_binding_ids = {
                    candidate.binding_id
                    for candidate in (existing, spilled)
                    if candidate is not None
                    and candidate.original_content == original
                    and candidate.binding_id is not None
                    and candidate.binding_id != _CONTENT_DERIVED_BINDING_ID
                }
                if len(candidate_binding_ids) > 1:
                    binding_conflict = True
                else:
                    entry.binding_id = (
                        next(iter(candidate_binding_ids))
                        if candidate_binding_ids
                        else _new_binding_id()
                    )
                    try:
                        binding_conflict = (
                            self._claim_binding_locked(hash_key, entry.binding_id) == "conflict"
                        )
                    except StorageUnavailableError as exc:
                        # Durability fail-open is safe only after identity authority
                        # accepted this key/content pair. Before that proof, writing
                        # a volatile shadow under an explicit key can make an older
                        # durable marker resolve to the new producer's foreign bytes.
                        raise CollisionSafetyError(
                            f"Cannot safely bind explicit hash {hash_key}: {exc}",
                            hash_key=hash_key,
                        ) from exc

            if conflicting is not None or binding_conflict:
                existing_len = len(conflicting.original_content) if conflicting is not None else -1
                logger.error(
                    "Hash collision detected: hash=%s tool=%s (existing_len=%d, new_len=%d) — "
                    "dropping the ambiguous binding from every tier; NEITHER content is served",
                    hash_key,
                    tool_name,
                    existing_len,
                    len(original),
                )
                try:
                    self._delete_hash_verified_locked(hash_key, release_binding=False)
                except StorageUnavailableError as exc:
                    raise CollisionSafetyError(
                        f"Cannot safely resolve collision for hash {hash_key}: cleanup could not be verified.",
                        hash_key=hash_key,
                    ) from exc
                # Do not poison a healthy old binding until its conflicting row
                # has actually been removed. Otherwise a failed cleanup makes
                # still-valid old content needlessly unretrievable.
                if conflicting is not None:
                    try:
                        self._poison_binding_locked(
                            hash_key, conflicting.binding_id, entry.binding_id
                        )
                    except StorageUnavailableError as exc:
                        raise CollisionSafetyError(
                            f"Collision cleanup succeeded for {hash_key}, but its "
                            f"identity could not be quarantined: {exc}",
                            hash_key=hash_key,
                        ) from exc
                collision_dropped = True
            elif existing is not None:
                logger.debug("Duplicate store for hash=%s, updating entry", hash_key)
                self._stale_heap_entries += 1

            if not collision_dropped:
                durable = self._persist_and_report_durability(hash_key, entry)
                if explicit_hash is not None and entry.binding_id is not None:
                    if durable:
                        self._volatile_bindings.pop(hash_key, None)
                    else:
                        self._volatile_bindings[hash_key] = entry.binding_id
                heapq.heappush(self._eviction_heap, (entry.created_at, hash_key))

        # Collision-drop veto (Bug-6): the ambiguous binding was dropped, so NOTHING is retrievable under this key.
        if collision_dropped:
            if require_durable:
                raise DurableWriteError(
                    f"CCR store for hash {hash_key} hit a true hash collision "
                    "(different content, same key); the ambiguous binding was dropped "
                    "so NEITHER content is served. The original was NOT stored — revert "
                    "to the uncompressed content.",
                    hash_key=hash_key,
                )
            return hash_key

        # Contention retry BEFORE the veto (store-concurrency-honesty).
        if require_durable and not durable:
            durable = self._retry_durable_persist(
                hash_key, entry, ensure_binding=explicit_hash is not None
            )

        # Durability veto (audit #3), raised OUTSIDE the lock, only once the whole retry budget is spent. The entry
        # stays in the volatile tier so SAME-PROCESS retrieval still works right now (hence the hash rides the error).
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

        Gates on the backend's DECLARED ``durable``, not on whether it happens to
        expose a ``set_durable`` attribute. Presence-as-proxy collapsed two
        opposite situations into "durability satisfied": a deliberately volatile
        backend (the in-memory default), and a genuinely DURABLE third-party
        backend that never implemented an undocumented name — the second silently
        disabled ``require_durable``, so a ``<<ccr:HASH>>`` marker could ship for
        content whose durability was never checked. Third-party backends are a
        supported configuration (the ``furl_ctx.ccr_backend`` entry point group),
        and a backend author reading the Protocol could not have known the name
        existed, because it was not declared there.

        Both members are now on the Protocol and reached by DIRECT ATTRIBUTE
        ACCESS. That second half is load-bearing: mypy types a ``getattr`` result
        as ``Any`` and reports nothing, so declaring the name while still reaching
        it through ``getattr`` would restore documentation without restoring
        enforcement.

        A backend declaring ``durable=False`` is durability-satisfied by
        construction — the operator chose volatile storage, so ``require_durable``
        has nothing to veto. Must be called with the store lock held.
        """
        if self._backend.durable:
            return bool(self._backend.set_durable(hash_key, entry))
        self._backend.set(hash_key, entry)
        return True

    def _retry_durable_persist(
        self,
        hash_key: str,
        entry: CompressionEntry,
        *,
        ensure_binding: bool = False,
    ) -> bool:
        """Retry durable identity + payload persistence as one logical write."""
        for attempt in range(1, self._durable_retry_attempts + 1):
            backoff = min(
                self._durable_retry_base_backoff_seconds * (2 ** (attempt - 1)),
                self._durable_retry_max_backoff_seconds,
            )
            if backoff > 0:
                time.sleep(backoff)
            with self._lock:
                if ensure_binding:
                    try:
                        binding = self._claim_binding_locked(hash_key, entry.original_content)
                    except StorageUnavailableError:
                        continue
                    if binding == "conflict":
                        delete_volatile = getattr(self._backend, "delete_volatile", None)
                        if callable(delete_volatile):
                            delete_volatile(hash_key)
                        self._volatile_bindings.pop(hash_key, None)
                        raise CollisionSafetyError(
                            f"Explicit hash {hash_key} was claimed by different content "
                            "while this durable write was retrying.",
                            hash_key=hash_key,
                        )
                if self._persist_and_report_durability(hash_key, entry):
                    self._volatile_bindings.pop(hash_key, None)
                    return True
        return False

    def retrieve(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
        _status_out: dict[str, Any] | None = None,
    ) -> CompressionEntry | None:
        """Retrieve a live entry without letting bookkeeping resurrect a purge.

        The content read itself stays availability-first. Access bookkeeping is
        a second, guarded mutation: it re-reads the current row while holding the
        same cross-process mutation guard as store/delete/cascade. If a purge won
        the race after the content read, bookkeeping observes the miss and does
        not upsert stale bytes back into durable storage.
        """
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            if _status_out is not None:
                _status_out.update(read.status)
            if read.entry is None:
                return None
            entry = read.entry

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
            result = replace(entry, search_queries=list(entry.search_queries))

        # Preserve the spill tier's read-only/no-promotion contract. Primary
        # access bookkeeping is guarded because its old full-row writeback could
        # recreate a row a concurrent process had just verified deleted.
        if read.source != "spill":
            updated = self._record_access_if_present(hash_key, query)
            if updated is not None:
                result = updated

        if record_feedback_signal and self._enable_feedback:
            self._emit_retrieval_signal(result.tool_name, result.compression_strategy)
        return result

    def retrieve_with_status(
        self,
        hash_key: str,
        query: str | None = None,
        *,
        record_feedback_signal: bool = True,
    ) -> tuple[CompressionEntry | None, dict[str, Any]]:
        status: dict[str, Any] = {}
        entry = self.retrieve(
            hash_key,
            query,
            record_feedback_signal=record_feedback_signal,
            _status_out=status,
        )
        return entry, status

    def get_metadata(self, hash_key: str) -> dict[str, Any] | None:
        """Get metadata only for a verified live entry."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            entry = read.entry
            if entry is None:
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
                "compression_strategy": entry.compression_strategy,
                "original_tokens": entry.original_tokens,
                "compressed_tokens": entry.compressed_tokens,
            }

    def search(
        self,
        hash_key: str,
        query: str,
        max_results: int = 20,
        score_threshold: float = 0.3,
        *,
        _status_out: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search one entry and optionally expose same-attempt read status."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
            if _status_out is not None:
                _status_out.update(read.status)
        entry = read.entry
        if entry is None:
            return []

        items = self._search_items_from_original(entry.original_content)
        if not items:
            return []
        item_strs = [json.dumps(item, default=str) for item in items]
        scores = self._scorer.score_batch(item_strs, query)
        scored = (
            (item, score.score)
            for item, score in zip(items, scores)
            if score.score >= score_threshold
        )
        results = [item for item, _ in heapq.nlargest(max_results, scored, key=itemgetter(1))]
        if results:
            self._record_search_access(hash_key, query)
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

    def search_with_status(
        self,
        hash_key: str,
        query: str,
        max_results: int = 20,
        score_threshold: float = 0.3,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        status: dict[str, Any] = {}
        results = self.search(
            hash_key,
            query,
            max_results=max_results,
            score_threshold=score_threshold,
            _status_out=status,
        )
        return results, status

    def search_all(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
        *,
        _outcome_out: dict[str, Any] | None = None,
    ) -> list[CrossStoreMatch]:
        outcome = self._search_all_impl(
            query, max_results=max_results, score_threshold=score_threshold
        )
        if _outcome_out is not None:
            _outcome_out["complete"] = outcome.complete
            _outcome_out["unavailable"] = outcome.unavailable
        return list(outcome.matches)

    def _search_all_impl(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
    ) -> CrossStoreSearchOutcome:
        """Search every readable tier and report whether the scan was complete."""
        if not query or not query.strip():
            return CrossStoreSearchOutcome((), True)

        backends: list[tuple[str, Any]] = [("primary", self._backend)]
        if self._spill is not None:
            backends.append(("spill", self._spill))
        reasons: list[str] = []
        keys: dict[str, None] = {}
        readable_roles: set[str] = set()
        with self._lock:
            for role, backend in backends:
                try:
                    index = self._checked_index_backend_locked(backend, role)
                except StorageUnavailableError as exc:
                    reasons.append(str(exc))
                    continue
                readable_roles.add(role)
                for _created_at, hash_key in index:
                    keys.setdefault(hash_key, None)

        now = self._now()
        live_entries: list[tuple[str, CompressionEntry]] = []
        for hash_key in keys:
            rows: list[tuple[str, CompressionEntry]] = []
            for role, backend in backends:
                if role not in readable_roles:
                    continue
                try:
                    with self._lock:
                        entries = self._checked_get_backend_locked(backend, hash_key, role)
                except StorageUnavailableError as exc:
                    reasons.append(str(exc))
                    readable_roles.discard(role)
                    continue
                rows.extend((role, entry) for entry in entries if not entry.is_expired(now))
            if not rows:
                continue
            try:
                with self._lock:
                    safe = self._binding_safe_locked(hash_key, [entry for _role, entry in rows])
            except StorageUnavailableError as exc:
                reasons.append(str(exc))
                continue
            if not safe:
                reasons.append(f"unsafe hash/content binding for {hash_key}")
                continue
            _role, entry = next((row for row in rows if row[0] == "primary"), rows[0])
            live_entries.append((hash_key, entry))

        if not live_entries:
            return CrossStoreSearchOutcome((), not reasons, tuple(dict.fromkeys(reasons)))
        documents = [entry.original_content for _hash, entry in live_entries]
        scores = self._scorer.score_batch(documents, query)
        ranked = heapq.nlargest(
            max_results,
            (
                (hash_key, entry, score.score)
                for (hash_key, entry), score in zip(live_entries, scores)
                if score.score > score_threshold
            ),
            key=itemgetter(2),
        )
        matches = tuple(
            CrossStoreMatch(
                hash=hash_key,
                score=score,
                preview=self._cross_store_preview(entry.original_content),
                tool_name=entry.tool_name,
            )
            for hash_key, entry, score in ranked
        )
        return CrossStoreSearchOutcome(matches, not reasons, tuple(dict.fromkeys(reasons)))

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
            # The query is caller/model-supplied and can itself carry a secret (e.g. searching retrieved content
            # for a token), so redact it with the same rules as the payload before it reaches the log sink.
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
            # If parsed JSON contains numbers Python cannot round-trip exactly, search the verbatim original instead of reserializing corrupted numeric values.
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
        """Get a live entry from either tier without recording an access.

        COR-37: the access bump happens in :meth:`_record_search_access` only
        after search returns results. Expired primary entries are reaped before
        lookup falls through to spill storage.
        """
        with self._lock:
            entry = self._backend.get(hash_key)
            if entry is None:
                entry = self._recover_from_spill(hash_key)
            elif entry.is_expired(self._now()):
                self._backend.delete(hash_key)
                self._stale_heap_entries += 1
                entry = self._recover_from_spill(hash_key)

            if entry is None:
                return None
            return replace(entry, search_queries=list(entry.search_queries))

    def _record_access_if_present(
        self, hash_key: str, query: str | None
    ) -> CompressionEntry | None:
        """Bookkeep one access only while the same row is still live.

        Access accounting is logically metadata, but both backends expose it by
        writing a ``CompressionEntry``. Without serialization, a process can read
        an entry, another process can purge it, and the stale accounting write can
        INSERT/REPLACE the entire payload back into SQLite. Re-read under the
        mutation guard so every outcome is linearizable with store/delete.
        """
        with self._mutation_guard.hold():
            with self._lock:
                read = self._read_live_entry_locked(hash_key)
                entry = read.entry
                if entry is None:
                    return None

                entry.record_access(query)
                if read.source == "spill":
                    if self._spill is None:
                        return None
                    try:
                        self._spill.set(hash_key, entry)
                    except Exception as exc:  # noqa: BLE001 — bookkeeping is advisory
                        logger.warning("CCR spill access update failed (non-fatal): %s", exc)
                else:
                    self._backend.set(hash_key, entry)
                return replace(entry, search_queries=list(entry.search_queries))

    def _record_search_access(self, hash_key: str, query: str | None) -> None:
        """Record access after search results, without stale-row resurrection."""
        entry = self._record_access_if_present(hash_key, query)
        if self._enable_feedback and entry is not None:
            self._emit_retrieval_signal(entry.tool_name, entry.compression_strategy)

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

    def search_all_detailed(
        self,
        query: str,
        max_results: int = 10,
        score_threshold: float = 0.0,
    ) -> CrossStoreSearchOutcome:
        details: dict[str, Any] = {}
        matches = self.search_all(
            query,
            max_results=max_results,
            score_threshold=score_threshold,
            _outcome_out=details,
        )
        return CrossStoreSearchOutcome(
            tuple(matches),
            bool(details.get("complete", True)),
            tuple(details.get("unavailable", ())),
        )

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

    def exists_any_tier(self, hash_key: str) -> bool:
        """Fail-closed purge predicate: uncertainty/unsafe data counts as present."""
        with self._lock:
            read = self._read_live_entry_locked(hash_key)
        return read.status.get("status") in {"available", "unavailable", "unsafe"}

    def get_entry_status(
        self,
        hash_key: str,
        *,
        clean_expired: bool = False,
    ) -> dict[str, Any]:
        """Return a tri-state-plus safety diagnosis from one checked read."""
        with self._mutation_guard.hold():
            with self._lock:
                read = self._read_live_entry_locked(hash_key)
                if clean_expired and read.status.get("status") == "expired":
                    try:
                        self._delete_hash_verified_locked(hash_key, release_binding=False)
                    except StorageUnavailableError as exc:
                        status = dict(read.status)
                        status["cleanup_unavailable"] = str(exc)
                        return status
                return read.status

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

    def increment_counter(self, name: str, amount: int = 1) -> int | None:
        """Add ``amount`` to a named cross-process observability counter.

        Advisory and FAIL-OPEN: a backend without counter support, or any counter
        error, is a silent no-op returning ``None`` — a broken counter must never
        break storage or a tool call. Returns the new value ONLY when it was
        durably persisted (cross-process visible via the SqliteBackend); ``None``
        otherwise (unsupported backend, or a volatile fallback write). The hook's
        once-per-namespace first-run note reads a ``1`` here (see
        ``counters_durable``); furl_stats reads the totals via ``get_counters``.

        A negative ``amount`` raises ``ValueError``. That rejection is deliberately
        OUTSIDE the fail-open ``try`` below: fail-open exists so an operational
        counter failure (a lock lost, a degraded file) never breaks a tool call,
        and it catches ``Exception`` broadly. A caller bug raised by the backend
        would be caught by that same handler and returned as an indistinguishable
        silent ``None`` — so the guard would exist in the backends yet be invisible
        through this, the public surface every caller actually uses. Validating
        here keeps the caller bug loud and the operational failure quiet.
        """
        # From ``backends.base``, beside the contract it validates
        from .backends.base import reject_negative_counter_amount

        # POSITION IS LOAD-BEARING: this call must stay OUTSIDE the ``try`` below. Caller bugs stay loud here; only OPERATIONAL counter failures fail open.
        reject_negative_counter_amount(name, amount)
        inc = getattr(self._backend, "increment_counter", None)
        if inc is None:
            return None
        try:
            with self._lock:
                result = inc(name, amount)
        except Exception as exc:  # noqa: BLE001 — counters are advisory, never fatal
            logger.debug("CCR counter increment dropped (non-fatal): %s=%s", name, exc)
            return None
        return result if result is None or isinstance(result, int) else None

    def get_counters(self) -> dict[str, int]:
        """Snapshot of all named counters (cumulative, cross-process for sqlite).

        FAIL-OPEN: an unsupported backend or a read error returns ``{}`` — the
        observability read must never raise into furl_stats.
        """
        getter = getattr(self._backend, "get_counters", None)
        if getter is None:
            return {}
        try:
            with self._lock:
                counters = getter()
        except Exception as exc:  # noqa: BLE001 — advisory read, never fatal
            logger.debug("CCR counter read dropped (non-fatal): %s", exc)
            return {}
        return dict(counters) if isinstance(counters, dict) else {}

    @property
    def counters_durable(self) -> bool:
        """True iff this store's backend persists counters DURABLY (cross-process).

        A durable counter backend DECLARES ``durable`` and offers the optional
        ``increment_counter`` extra — the SqliteBackend does both; the in-memory
        default declares itself volatile (its counters are process-local). The
        hook uses this to gate its once-per-namespace first-run note so a
        per-process ``1`` from the volatile backend (library / unit tests) never
        fires it.

        The durability half reads the DECLARATION, closing the same collapse
        ``_persist_and_report_durability`` had: a durable third-party backend
        without a ``set_durable`` attribute used to report its counters as
        non-durable, and a volatile one that happened to define the name would
        have reported them as durable. ``increment_counter`` stays a ``getattr``
        on purpose — it is a genuinely OPTIONAL ARCH-10 observability extra, not a
        Protocol member, and its absence is a documented fail-open no-op rather
        than a silently disabled invariant.
        """
        return (
            self._backend.durable and getattr(self._backend, "increment_counter", None) is not None
        )

    @_serialized_mutation
    def delete(self, hash_key: str) -> bool:
        """Verified all-tier delete; retain identity until natural expiry/full clear."""
        with self._lock:
            try:
                return self._delete_hash_verified_locked(hash_key, release_binding=False)
            except StorageUnavailableError as exc:
                logger.warning("CCR verified delete failed for %s: %s", hash_key, exc)
                return False

    def _entry_marker_hashes(self, entry: CompressionEntry, *, exclude: str) -> list[str]:
        """Marker hashes referenced by *entry*, minus *exclude*, first-seen order."""
        from furl_ctx.ccr.marker_grammar import hashes_in_text

        seen: dict[str, None] = {}
        for text in (entry.compressed_content, entry.original_content):
            if isinstance(text, str) and _may_reference_marker(text):
                for nested_hash in hashes_in_text(text):
                    seen.setdefault(nested_hash, None)
        return [h for h in seen if h != exclude]

    def _is_co_referenced(self, nested_hash: str, *, ignoring: set[str]) -> bool:
        """Whether any readable live representation outside *ignoring* references it."""
        now = self._now()
        with self._lock:
            try:
                items = self._checked_items_backend_locked(self._backend, "primary")
                if self._spill is not None:
                    items.extend(self._checked_items_backend_locked(self._spill, "spill"))
            except StorageUnavailableError as exc:
                logger.warning(
                    "CCR co-reference proof unavailable; preserving nested hash %s: %s",
                    nested_hash,
                    exc,
                )
                return True

        from furl_ctx.ccr.marker_grammar import hashes_in_text

        return any(
            nested_hash in hashes_in_text(text)
            for key, entry in items
            if key != nested_hash and key not in ignoring and not entry.is_expired(now)
            for text in (entry.compressed_content, entry.original_content)
            if isinstance(text, str) and _may_reference_marker(text)
        )

    def delete_cascade(self, hash_key: str) -> tuple[bool, int]:
        """Delete *hash_key* AND every nested blob only it referenced.

        Back-compat wrapper over :meth:`delete_cascade_detailed`; returns
        ``(top_deleted, nested_deleted_count)``.
        """
        outcome = self.delete_cascade_detailed(hash_key)
        return (outcome.top_deleted, len(outcome.nested_deleted))

    def _preflight_cascade_graph(
        self,
        hash_key: str,
        *,
        already_visited: set[str],
    ) -> dict[str, CascadePlanNode] | None:
        """Discover a stable, checked marker graph before any mutation."""
        graph: dict[str, CascadePlanNode] = {}
        seen = set(already_visited)
        pending = [hash_key]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            try:
                with self._lock:
                    rows = self._representations_locked(current)
                    live_entries = [
                        entry for _role, entry in rows if not entry.is_expired(self._now())
                    ]
                    if live_entries and not self._binding_safe_locked(current, live_entries):
                        logger.warning(
                            "CCR cascade preflight found unsafe binding for %s; aborting",
                            current,
                        )
                        return None
            except StorageUnavailableError as exc:
                logger.warning(
                    "CCR checked read during delete_cascade preflight failed for %s; "
                    "aborting the entire cascade before mutation: %s",
                    current,
                    exc,
                )
                return None
            nested_seen: dict[str, None] = {}
            for entry in live_entries:
                for nested_hash in self._entry_marker_hashes(entry, exclude=current):
                    nested_seen.setdefault(nested_hash, None)
            nested = tuple(nested_seen)
            graph[current] = CascadePlanNode(bool(live_entries), nested)
            pending.extend(
                nested_hash for nested_hash in reversed(nested) if nested_hash not in seen
            )
        return graph

    def _delete_cascade_from_graph(
        self,
        hash_key: str,
        *,
        graph: dict[str, CascadePlanNode],
        visited: set[str],
    ) -> CascadeOutcome:
        """Apply one immutable cascade plan through the public delete contract."""
        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)
        node = graph.get(hash_key, CascadePlanNode(False, ()))
        top_deleted = self.delete(hash_key)
        if node.existed and not top_deleted:
            return CascadeOutcome(top_deleted=False, failed_hashes=(hash_key,))
        visited.add(hash_key)

        deleted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for nested_hash in node.nested:
            if nested_hash in visited:
                continue
            if self._is_co_referenced(nested_hash, ignoring=visited):
                skipped.append(nested_hash)
                continue
            child = self._delete_cascade_from_graph(nested_hash, graph=graph, visited=visited)
            if child.top_deleted:
                deleted.append(nested_hash)
            deleted.extend(child.nested_deleted)
            skipped.extend(child.nested_shared_skipped)
            failed.extend(child.failed_hashes)
        deleted_set = set(deleted)
        return CascadeOutcome(
            top_deleted=top_deleted,
            nested_deleted=tuple(deleted),
            nested_shared_skipped=tuple(h for h in skipped if h not in deleted_set),
            failed_hashes=tuple(dict.fromkeys(failed)),
        )

    @_serialized_mutation
    def delete_cascade_detailed(
        self, hash_key: str, *, _visited: set[str] | None = None
    ) -> CascadeOutcome:
        """Delete a marker graph atomically with respect to competing CCR mutations."""
        visited = _visited if _visited is not None else set()
        if hash_key in visited:
            return CascadeOutcome(top_deleted=False)
        graph = self._preflight_cascade_graph(hash_key, already_visited=visited)
        if graph is None:
            return CascadeOutcome(top_deleted=False, failed_hashes=(hash_key,))
        return self._delete_cascade_from_graph(hash_key, graph=graph, visited=visited)

    @_serialized_mutation
    def clear(self) -> int:
        """Verified all-tier wipe with identity reset only after complete erase.

        Payload deletion and explicit-hash provenance are two phases. Clearing
        provenance before every tier is proven empty would strand a surviving
        spill row without the identity evidence needed to retrieve it safely.
        Conversely, resetting provenance after a partial wipe could let a stale
        published marker be rebound to foreign bytes.
        """
        residual = 0
        uncertain = False
        backends = tuple(
            (role, backend)
            for role, backend in (("primary", self._backend), ("spill", self._spill))
            if backend is not None
        )
        with self._lock:
            for role, backend in backends:
                payload_clear = getattr(backend, "checked_clear_payloads", None)
                checked = (
                    payload_clear
                    if callable(payload_clear)
                    else getattr(backend, "checked_clear", None)
                )
                if callable(checked):
                    try:
                        checked()
                    except Exception as exc:
                        logger.warning("CCR verified %s clear failed: %s", role, exc)
                        uncertain = True
                    continue
                if bool(getattr(backend, "durable", False)):
                    logger.warning("CCR durable %s backend has no checked clear capability", role)
                    uncertain = True
                    continue
                try:
                    backend.clear()
                except Exception as exc:
                    logger.warning("CCR %s clear failed: %s", role, exc)
                count = getattr(backend, "count", None)
                if not callable(count):
                    uncertain = True
                    continue
                try:
                    residual += max(0, int(count()))
                except Exception as exc:
                    logger.warning("CCR %s post-clear count failed: %s", role, exc)
                    uncertain = True

            # Identity reuse is permitted only at a verified namespace-reset
            # boundary. If any payload tier survived or was unreadable, retain
            # every binding tombstone so old markers cannot be rebound.
            if residual == 0 and not uncertain:
                for role, backend in backends:
                    reset = getattr(backend, "checked_reset_bindings", None)
                    if not callable(reset):
                        continue
                    try:
                        reset()
                    except Exception as exc:
                        logger.warning("CCR verified %s identity reset failed: %s", role, exc)
                        uncertain = True

            self._retrieval_events.clear()
            self._eviction_heap.clear()
            self._stale_heap_entries = 0
            if residual == 0 and not uncertain:
                self._volatile_bindings.clear()
        return max(residual, 1 if uncertain else 0)

    def _clear_spill_residual(self) -> int:
        """Clear the durable spill; return how many rows survived the attempt.

        Called with ``self._lock`` held. Returns 0 when the spill is disabled or
        cleanly emptied. A spill whose ``clear`` raises still holds its rows
        (reachable via :meth:`retrieve`), so they are COUNTED and surfaced rather
        than swallowed -- that swallow was the ``furl_purge all=true`` false-erase
        bug (review F1: ``get_stats`` counts the primary tier only, so a spill
        survivor was invisible and the purge claimed success).

        Fail-CLOSED, matching :meth:`exists_any_tier`: if the survivors cannot
        even be counted, return 1 -- an un-provably-empty spill is a survivor,
        not an all-clear. ``count`` is a Protocol method the sqlite/in-memory
        backends never raise from; the guard covers a hostile or degraded spill.
        """
        if self._spill is None:
            return 0
        try:
            self._spill.clear()
        except Exception as exc:  # noqa: BLE001 — surfaced as residual, not raised
            logger.warning("CCR spill clear failed (entries may remain): %s", exc)
        else:
            return 0
        try:
            return self._spill.count()
        except Exception as exc:  # noqa: BLE001 — cannot prove the spill empty
            logger.warning("CCR spill count after failed clear failed: %s", exc)
            return 1

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

        # Evict until the backend is below the logical cap. If stale heap entries make no real
        # progress, rebuild from live backend timestamps so the next oldest pop removes a real entry.
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
                # Real oldest entry — evict it.
                self._spill_evicted(hash_key, entry)
                self._backend.delete(hash_key)
                rebuilt_since_progress = False  # made progress
            else:
                # Stale heap reference — decrement the counter. If the heap drains to nothing but
                # ones (no real eviction) the `not heap` branch above rebuilds from the live backend.
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
        # Build new heap from current store entries only.
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

# Request-scoped ORIGINATING TOOL NAME (content_kind threading). ``store()`` reads it as the DEFAULT ``tool_name`` when a writer does not supply one.
_request_tool_name: ContextVar[str | None] = ContextVar("furl_request_tool_name", default=None)

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

FURL_CCR_NAMESPACE_ENV = "FURL_CCR_NAMESPACE"

# When set — the plugin deployment exports it from the project root — an otherwise un-namespaced call is scoped to a per-project
# store instead of the process-global singleton, closing the cross-project commingling + eviction hole with zero user config.
FURL_CCR_PROJECT_DIR_ENV = "FURL_CCR_PROJECT_DIR"

# Registry of namespace-key -> store, so identical (namespace, session, agent) tuples converge on the SAME store
# across calls (cross-turn retrieval works) and in-memory tenants do not lose their entries between compress() calls.
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
    # NUL-joined so distinct segmentations cannot alias (``a`` + ``bc`` vs ``ab`` + ``c``); the raw values are
    # opaque and never touch a filesystem path directly — the sqlite filename is derived by hashing this key.
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


def _ccr_namespace_spill_db_path(namespace_key: str) -> Path:
    """Per-namespace durable SPILL sqlite path (T6 retention), HASHED like the primary.

    ``session_id`` / ``agent_id`` are untrusted request data, so — exactly as in
    :func:`_ccr_namespace_db_path` — the key never interpolates into a path
    verbatim. The filename is ``ccr-ns-<sha256(key)[:16]>-spill.sqlite3``: the
    ``-spill`` marker keeps it ALWAYS distinct from this namespace's own primary
    file, and the per-key digest keeps it distinct from every OTHER tenant's
    spill, so a demoted entry never lands in a database another tenant can read.
    """
    digest = hashlib.sha256(namespace_key.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return _paths.workspace_dir() / f"ccr-ns-{digest}-spill.sqlite3"


def _build_namespace_store(namespace_key: str) -> CompressionStore:
    """Construct a fresh, isolated store for ``namespace_key``.

    Backend selection mirrors the global default (``FURL_CCR_BACKEND``): the
    durable ``sqlite`` backend gets a per-namespace file (so tenants never share
    a database), every other selection — including the in-memory default — gets
    its OWN backend instance via the ``CompressionStore`` constructor. Never
    falls back to the global store: that would defeat isolation.

    When ``FURL_CCR_SPILL`` is on, a PER-NAMESPACE durable spill tier is wired
    too (T6): a capacity-evicted entry is demoted to this namespace's own
    ``-spill`` file instead of being dropped at the 1000-entry cap, so a
    ``<<ccr:HASH>>`` marker stays resolvable past eviction — without demoting any
    tenant into a shared database. See :func:`_build_namespace_spill_backend`
    for why this path does not copy the global builder's sqlite-primary guard.
    """
    backend_type = (os.environ.get("FURL_CCR_BACKEND") or "").strip().lower()
    backend: CompressionStoreBackend | None = None
    if backend_type == "sqlite":
        from .backends.sqlite import SqliteBackend

        backend = SqliteBackend(db_path=_ccr_namespace_db_path(namespace_key))
    return CompressionStore(
        default_ttl=_get_env_default_ttl_seconds(),
        backend=backend,
        spill=_build_namespace_spill_backend(namespace_key),
    )


def _build_namespace_spill_backend(
    namespace_key: str,
) -> CompressionStoreBackend | None:
    """Per-namespace durable SPILL tier for a namespaced store (T6 retention).

    Returns ``None`` (single-tier, byte-identical to before) unless
    ``FURL_CCR_SPILL`` is truthy, so default behavior is unchanged. When on, the
    spill is a ``SqliteBackend`` on this namespace's OWN
    ``ccr-ns-<digest>-spill.sqlite3`` — its own row cap + TTL purge backstop it
    exactly like the global tier, its 0600/0700 permissions match, but the file
    is per-namespace so a demoted entry never enters a shared database.

    Deliberately WITHOUT the global :func:`_create_spill_backend_from_env`'s
    ``isinstance(primary, SqliteBackend) -> None`` guard. That guard exists only
    because the global spill reuses the ONE shared ``ccr.sqlite3`` and so would
    collide with a sqlite primary. Here the spill file is per-namespace and
    ``-spill``-suffixed, so it can never alias the primary — and the shipped
    plugin runs ``FURL_CCR_BACKEND=sqlite``, so applying that guard would leave
    the plugin with NO spill at all, the exact no-op T6 exists to remove.
    """
    if not _ccr_spill_enabled():
        return None

    from .backends.sqlite import SqliteBackend

    return SqliteBackend(db_path=_ccr_namespace_spill_db_path(namespace_key))


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


def _ccr_spill_enabled() -> bool:
    """True when ``FURL_CCR_SPILL`` opts into the durable spill tier (Q10/T6).

    The single truthiness gate shared by BOTH spill builders — the global
    :func:`_create_spill_backend_from_env` and the per-namespace
    :func:`_build_namespace_spill_backend` — so the two paths can never drift on
    what "spill on" means. Unset/blank/false keeps single-tier behavior.
    """
    return (os.environ.get(CCR_SPILL_ENV) or "").strip().lower() in _TRUTHY


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
    if not _ccr_spill_enabled():
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


def _checkpoint_binding_for_entry_locked(
    store: CompressionStore, hash_key: str, entry: CompressionEntry
) -> tuple[str, bool] | None:
    """Return verified opaque provenance needed to restore *entry*."""
    if not store._is_marker_key(hash_key):
        return None
    if entry.binding_id == _CONTENT_DERIVED_BINDING_ID:
        return None
    if entry.binding_id is None:
        raise CollisionSafetyError(
            f"Cannot checkpoint explicit hash {hash_key}: its opaque binding id is missing.",
            hash_key=hash_key,
        )
    record = store._binding_record_locked(hash_key)
    if record is None or record[1] or record[0] != entry.binding_id:
        raise CollisionSafetyError(
            f"Cannot checkpoint explicit hash {hash_key}: its content binding is "
            "missing, conflicted, or does not match the stored payload.",
            hash_key=hash_key,
        )
    return record


def ccr_export(
    path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> int:
    """Checkpoint the primary CCR tier plus explicit-hash provenance.

    The payload copy remains byte-exact at backend level, preserving timestamps,
    TTL and retrieval metadata. Arbitrary explicit hashes additionally carry the
    binding proof introduced by the storage-safety layer; without it a restored
    row would be quarantined (or, worse, guessed safe) because the hash itself
    does not authenticate the payload.
    """
    from .backends.sqlite import SqliteBackend

    source = _active_ccr_store(session_id, agent_id)
    with source._mutation_guard.hold():
        with source._lock:
            grouped: dict[str, list[tuple[str, CompressionEntry]]] = {}
            for hash_key, entry in source._checked_items_backend_locked(source._backend, "primary"):
                grouped.setdefault(hash_key, []).append(("primary", entry))
            if source._spill is not None:
                for hash_key, entry in source._checked_items_backend_locked(source._spill, "spill"):
                    grouped.setdefault(hash_key, []).append(("spill", entry))

            entries: list[tuple[str, CompressionEntry]] = []
            bindings: dict[str, tuple[str, bool]] = {}
            now = source._now()
            for hash_key, rows in grouped.items():
                candidates = [row for row in rows if not row[1].is_expired(now)] or rows
                candidate_entries = [entry for _role, entry in candidates]
                if not source._binding_safe_locked(hash_key, candidate_entries):
                    raise CollisionSafetyError(
                        f"Cannot checkpoint hash {hash_key}: its live representations "
                        "do not have one safe content binding.",
                        hash_key=hash_key,
                    )
                _role, entry = next(
                    (row for row in candidates if row[0] == "primary"),
                    candidates[0],
                )
                entries.append((hash_key, entry))
                record = _checkpoint_binding_for_entry_locked(source, hash_key, entry)
                if record is not None:
                    bindings[hash_key] = record

    destination = SqliteBackend(db_path=path)
    try:
        for hash_key, entry in entries:
            record = bindings.get(hash_key)
            if record is not None:
                binding_id, conflicted = record
                if conflicted:
                    raise AssertionError("verified checkpoint binding unexpectedly conflicted")
                claim = destination.claim_binding(hash_key, binding_id)
                if claim == "conflict":
                    raise CollisionSafetyError(
                        f"Checkpoint destination already has conflicting identity for {hash_key}.",
                        hash_key=hash_key,
                    )
            if not destination.set_durable(hash_key, entry):
                raise DurableWriteError(
                    f"CCR checkpoint export for hash {hash_key} did not reach the "
                    "checkpoint SQLite file.",
                    hash_key=hash_key,
                )
    finally:
        destination.close()
    return len(entries)


def ccr_import(
    path: str | os.PathLike[str],
    *,
    session_id: str | None = None,
    agent_id: str | None = None,
) -> int:
    """Restore a CCR checkpoint without bypassing collision/concurrency safety.

    Import preserves entry metadata exactly, but the write itself participates in
    the same cross-process mutation guard as store/delete/cascade. Checkpoint
    provenance is verified before arbitrary explicit hashes are admitted, and a
    destination collision aborts rather than overwriting a key that existing
    markers may already name.
    """
    from .backends.sqlite import SqliteBackend

    source = SqliteBackend(db_path=path)
    try:
        entries = source.checked_items()
        bindings: dict[str, tuple[str, bool]] = {}
        for hash_key, entry in entries:
            if not CompressionStore._is_marker_key(hash_key):
                continue
            if entry.binding_id == _CONTENT_DERIVED_BINDING_ID:
                continue
            if entry.binding_id is None:
                raise CollisionSafetyError(
                    f"Checkpoint entry {hash_key} has no opaque binding id.",
                    hash_key=hash_key,
                )
            record = source.get_binding(hash_key)
            if record is None or record[1] or record[0] != entry.binding_id:
                raise CollisionSafetyError(
                    f"Checkpoint entry {hash_key} has no verified matching identity record.",
                    hash_key=hash_key,
                )
            bindings[hash_key] = record
    finally:
        source.close()

    destination = _active_ccr_store(session_id, agent_id)
    with destination._mutation_guard.hold():
        with destination._lock:
            # Preflight the whole checkpoint before changing destination state.
            for hash_key, entry in entries:
                rows = destination._representations_locked(hash_key)
                conflict = next(
                    (
                        candidate
                        for _role, candidate in rows
                        if candidate.original_content != entry.original_content
                    ),
                    None,
                )
                if conflict is not None:
                    raise CollisionSafetyError(
                        f"Cannot import checkpoint hash {hash_key}: destination already "
                        "contains different content under that key.",
                        hash_key=hash_key,
                    )
                record = bindings.get(hash_key)
                if record is not None:
                    existing = destination._binding_record_locked(hash_key)
                    if existing is not None and (existing[1] or existing[0] != record[0]):
                        raise CollisionSafetyError(
                            f"Cannot import checkpoint hash {hash_key}: destination identity "
                            "already belongs to different content.",
                            hash_key=hash_key,
                        )

            for hash_key, entry in entries:
                record = bindings.get(hash_key)
                if record is not None:
                    assert entry.binding_id is not None
                    claim = destination._claim_binding_locked(hash_key, entry.binding_id)
                    if claim == "conflict":  # guarded/preflighted; defensive only
                        raise CollisionSafetyError(
                            f"Checkpoint identity for {hash_key} conflicted during import.",
                            hash_key=hash_key,
                        )
                durable = destination._persist_and_report_durability(hash_key, entry)
                if destination._backend.durable and not durable:
                    raise DurableWriteError(
                        f"CCR checkpoint import for hash {hash_key} reached only volatile "
                        "fallback storage; refusing to report a durable restore.",
                        hash_key=hash_key,
                    )

            # Direct backend writes intentionally preserve the old entry metadata;
            # rebuild the eviction projection once so imported timestamps become
            # visible to normal capacity management without fabricating new ones.
            destination._rebuild_heap()
    return len(entries)
