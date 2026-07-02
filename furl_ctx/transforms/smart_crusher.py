"""Smart JSON array crusher — Rust-backed via PyO3.

The Python implementation has been retired.
All array compression now goes through `furl_ctx._core.SmartCrusher`
(built from `crates/furl-py`). Byte-equality of the two
implementations was verified against 17 recorded fixtures
(`tests/parity/fixtures/smart_crusher/`) before the Python source was
removed; the Rust crate has its own coverage in `crates/furl-core/`
(388 unit tests + property tests).

This module retains the public surface — `SmartCrusherConfig`,
`CrushResult`, `SmartCrusher`, `smart_crush_tool_output` — so existing
call sites keep working unchanged. The dataclasses are still pure
Python because callers use `asdict()`, `__dict__`, and dataclass
matching on them. Only the `SmartCrusher` class delegates to Rust.

The `furl_ctx._core` extension is a hard import: there is no Python
fallback. Build it locally with `scripts/build_rust_extension.sh`
(wraps `maturin develop`) or install a prebuilt wheel.

# Functionality state (post-audit, 2026-04-29)

- **CCR recovery pointer** — UNCONDITIONAL on every drop. Whenever the
  lossy row-drop path drops a distinct item, the `<<ccr:HASH>>` recovery
  pointer is surfaced in the output AND the CCR store is written,
  regardless of `ccr_config.enabled` / `inject_retrieval_marker`. The
  pointer is the retrieval key — a drop without it is silent loss, which
  the recovery invariant forbids (Defect 1). `enabled` /
  `inject_retrieval_marker` flip the Rust `enable_ccr_marker` field,
  which the router reads back to decide whether to inject the
  heavier `furl_retrieve` TOOL into the request; they no longer gate
  the pointer or the store write. The Python store mirror keys off the
  `<<ccr:` pointer (now always present on a drop), so
  `compression_store.retrieve(hash)` resolves it. Opaque-string CCR
  substitutions likewise always emit their pointer.
- **Custom relevance scorer / scorer override** — fails loud.
  `relevance_config` and `scorer` constructor args remain in the
  signature for source compat, but the shim raises
  `NotImplementedError` when either is non-None. Silently dropping a
  user-supplied scorer is a silent-fallback bug we explicitly refuse
  to ship; full plumbing would land with the relevance-crate
  Python bridge.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

from ..ccr import marker_grammar
from ..config import CCRConfig, TransformResult
from ..tokenizer import Tokenizer
from ..utils import compute_short_hash, create_tool_digest_marker, deep_copy_messages
from .base import Transform

logger = logging.getLogger(__name__)


# Lossless-compaction renderers known to the Rust core — mirrors
# `CompactionStage::SUPPORTED_FORMAT_NAMES` in
# `crates/furl-core/.../compaction/mod.rs`.
_SUPPORTED_COMPACTION_FORMATS = ("csv-schema", "json", "markdown-kv")


class CcrMirrorError(RuntimeError):
    """Raised when the Rust→Python CCR mirror cannot persist a dropped
    payload into the Python ``compression_store``.

    A SmartCrusher row-drop commits the original to the ephemeral,
    process-local Rust store and emits a ``<<ccr:HASH>>`` pointer, but the
    store production ``/v1/retrieve`` reads is the Python
    ``compression_store``. If the mirror into that store fails, the dropped
    rows are UNRECOVERABLE for a consumer holding only the output — exactly
    the silent loss the store's contract forbids (``no SILENT loss``,
    compression_store.py:234-244).

    Raising (instead of swallowing at ``logger.debug``) lets the failure
    propagate to ``compress()``'s fail-open boundary (compress.py:386),
    which discards the lossy output and returns the ORIGINAL uncompressed
    messages — so the lossy drop never stands without a recovery copy.
    """


# ─── CCR sentinel ─────────────────────────────────────────────────────────
#
# When SmartCrusher's lossy path drops rows, it appends a sentinel object
# `{"_ccr_dropped": "<<ccr:HASH N_rows_offloaded>>"}` to the kept-items
# array. The LLM sees this in the prompt and can ask for the original via
# the CCR retrieval tool. Downstream consumers that iterate the array
# expecting a uniform schema (e.g. `for e in entries: e["level"]`) need
# to skip the sentinel — that's what `strip_ccr_sentinels` is for.

CCR_SENTINEL_KEY = "_ccr_dropped"


def is_ccr_sentinel(item: Any) -> bool:
    """True if `item` is a CCR-dropped sentinel object."""
    return isinstance(item, dict) and CCR_SENTINEL_KEY in item


def strip_ccr_sentinels(items: Any) -> Any:
    """Return `items` with any CCR-dropped sentinel objects filtered out.

    Pass this through any iteration over a compressed array's contents
    when your code expects a uniform-schema list of records. The sentinel
    carries a `<<ccr:HASH ...>>` marker for the LLM and shouldn't be
    confused for a record — it has only the `_ccr_dropped` key.

    Non-list inputs pass through unchanged so callers can wrap whatever
    `json.loads` returned without first checking the shape.
    """
    if not isinstance(items, list):
        return items
    return [x for x in items if not is_ccr_sentinel(x)]


# ─── Tool-name attribution ────────────────────────────────────────────────


def _build_tool_name_index(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Map tool_call_id/tool_use_id → tool name across OpenAI + Anthropic formats.

    Skips entries where id or name is missing; those calls still crush, but
    won't contribute a tool-name to the ``smart_crush`` tag.
    """
    index: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id")
            name = (tc.get("function") or {}).get("name")
            if tc_id and name:
                index[tc_id] = name
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                bid = block.get("id")
                name = block.get("name")
                if bid and name:
                    index[bid] = name
    return index


def _format_smart_crush_transform(count: int, tool_names: list[str]) -> str:
    """Format ``smart_crush:<count>[:<name1,name2,...>]``.

    Names are included when known so consumers can show what was crushed. Empty
    names fall back to the count-only form for backwards compatibility.
    """
    if tool_names:
        return f"smart_crush:{count}:{','.join(tool_names)}"
    return f"smart_crush:{count}"


# ─── Public dataclasses ───────────────────────────────────────────────────


@dataclass
class CrushResult:
    """Result from `SmartCrusher.crush()`.

    Used by `ContentRouter` when routing JSON arrays to `SmartCrusher`.
    """

    compressed: str
    original: str
    was_modified: bool
    strategy: str = "passthrough"


@dataclass
class SmartCrusherConfig:
    """Configuration for SmartCrusher.

    SCHEMA-PRESERVING: output contains only items from the original
    array. No wrappers, no generated text, no metadata keys.

    Field names + defaults match the Rust `SmartCrusherConfig` byte-for-
    byte; the shim copies these straight into the PyO3 constructor.
    """

    enabled: bool = True
    min_items_to_analyze: int = 5
    min_tokens_to_crush: int = 200
    variance_threshold: float = 2.0
    uniqueness_threshold: float = 0.1
    similarity_threshold: float = 0.8
    max_items_after_crush: int = 15
    preserve_change_points: bool = True
    factor_out_constants: bool = False
    include_summaries: bool = False
    dedup_identical_items: bool = True
    first_fraction: float = 0.3
    last_fraction: float = 0.15
    # Lossless compaction only replaces the original when it saves at
    # least this byte fraction vs the (minified) input. Mirrors the Rust
    # default; mainly lowered in tests and KV experiments — KV repeats
    # field names per row, so it clears the gate less often than CSV.
    lossless_min_savings_ratio: float = 0.30
    # How the crusher chooses between a lossless render (all rows,
    # encoded) and a lossy-recoverable render (dropped + ``<<ccr:HASH>>``
    # sentinel) when BOTH are available — both are 100% recoverable, so
    # the choice loses no information.
    #
    # * ``"min-tokens"`` (default, max compression): ship whichever render
    #   is fewer TOKENS (a real tokenizer, not bytes — bytes mislead).
    # * ``"lossless-first"``: keep the legacy behavior; lossless wins
    #   whenever it clears ``lossless_min_savings_ratio``. Used by the
    #   lossless round-trip suite, which asserts the lossless rendering.
    routing_policy: str = "min-tokens"


# ─── Rust-backed SmartCrusher ─────────────────────────────────────────────


class SmartCrusher(Transform):
    """Rust-backed `SmartCrusher` (via PyO3 / `furl_ctx._core`).

    Same `__init__` and method shapes as the retired Python class —
    drop-in replacement. The `crush()` and `_smart_crush_content()`
    methods delegate every byte to Rust; `apply()` keeps the
    Transform-protocol orchestration in Python (message walking,
    digest-marker insertion, token counting) since that's mostly glue
    around the per-message compression call.
    """

    name = "smart_crusher"

    def __init__(
        self,
        config: SmartCrusherConfig | None = None,
        relevance_config: Any = None,
        scorer: Any = None,
        ccr_config: CCRConfig | None = None,
        with_compaction: bool = True,
        observer: Any = None,
        compaction_format: str | None = None,
    ):
        # Hard import — no Python fallback. If the wheel is missing the
        # caller must build it (scripts/build_rust_extension.sh) or
        # install a prebuilt one. Failing loudly is better than silent
        # degradation; see feedback memory `feedback_no_silent_fallbacks.md`.
        from furl_ctx._core import (
            SmartCrusher as _RustSmartCrusher,
        )
        from furl_ctx._core import (
            SmartCrusherConfig as _RustSmartCrusherConfig,
        )

        cfg = config or SmartCrusherConfig()
        self.config = cfg
        self._with_compaction = with_compaction
        # `observer`: duck-typed, must expose `record_compression(...)`.
        # Callers that use SmartCrusher.apply() directly (no ContentRouter)
        # would, without an observer here, make those compressions
        # invisible to per-strategy metrics — exactly the
        # silent-regression class we're guarding against.
        self._observer = observer

        # CCR config is preserved on `self` for callers that read it
        # back (external embedders rely on this). Both `enabled=False`
        # and `inject_retrieval_marker=False` collapse to the Rust
        # crusher's `enable_ccr_marker=False` field.
        #
        # That field does NOT skip the marker or the CCR store write
        # (Defect 1 corrected the prior comment, which claimed it did).
        # The lossy row-drop path ALWAYS surfaces the `<<ccr:HASH>>`
        # recovery pointer and ALWAYS writes the store when a distinct
        # item is dropped, regardless of this flag — a drop without a
        # recovery pointer is silent loss, which the recovery invariant
        # forbids on the public `compress()` path. The flag is consumed
        # by the router layer to decide whether to inject the
        # heavier `furl_retrieve` TOOL into the request; it is the
        # retrieval-tool preference, not a data-loss switch.
        #
        # Default falls through to `CCRConfig()` so direct callers
        # (the engine and tests that don't pass an explicit config) get
        # the documented dataclass defaults (`enabled=True,
        # inject_retrieval_marker=True`).
        #
        # Opaque-string CCR substitutions likewise always emit their
        # pointer — they have no Python equivalent.
        if ccr_config is None:
            self._ccr_config = CCRConfig()
        else:
            self._ccr_config = ccr_config

        # `relevance_config` and `scorer` remain in the signature for
        # source compatibility, but the Rust port doesn't support
        # overrides yet (it always uses `HybridScorer` from the
        # relevance crate; the Python-bridged constructor surface
        # is not yet wired). Silently dropping a user-supplied
        # scorer would be a textbook silent fallback — if a caller
        # depends on a custom scoring function and we ignore it, the
        # compression they get back is wrong in a way they cannot see.
        # Fail loud instead. See `feedback_no_silent_fallbacks.md`.
        if relevance_config is not None or scorer is not None:
            raise NotImplementedError(
                "SmartCrusher: custom `relevance_config` / `scorer` "
                "overrides are not yet supported by the Rust-backed "
                "implementation. Pass `None` to use the default "
                "HybridScorer. Tracked in RUST_DEV.md; full support "
                "lands with Stage 3c.2's relevance-crate Python bridge."
            )

        # Build the Rust crusher. Forward EVERY field of the Python
        # `SmartCrusherConfig` dataclass via `asdict(cfg)` so the field
        # set is single-sourced from the dataclass definition — adding a
        # field there flows across the FFI with no extra edit here, and
        # no field can be silently dropped by a forgotten forwarding line.
        # `asdict` yields a flat dict of the 15 scalar fields (all
        # bool/int/float/str — no nesting), matching the pyo3 signature
        # kwargs 1:1 by name (pyo3-side kwargs not covered here fall
        # through to their pyo3 signature defaults).
        #
        # The pyo3 constructor accepts two MORE kwargs that are NOT
        # dataclass fields and so must be passed explicitly:
        #   * `relevance_threshold` (0.3) — lives on
        #     `RelevanceScorerConfig`, not this dataclass; the Rust crusher
        #     needs its own default.
        #   * `enable_ccr_marker` — derived from the CCR config, not a
        #     crusher-config field. Falling through to the pyo3 default
        #     (`true`) would ignore a caller's `CCRConfig(enabled=False)` /
        #     `inject_retrieval_marker=False`, so it MUST be passed here.
        # Neither name collides with a dataclass field, so there is no
        # duplicate-kwarg conflict with `**asdict(cfg)`.
        rust_cfg = _RustSmartCrusherConfig(
            **asdict(cfg),
            relevance_threshold=0.3,
            enable_ccr_marker=(
                self._ccr_config.enabled and self._ccr_config.inject_retrieval_marker
            ),
        )
        # Default: lossless-first compaction. Lossless wins for
        # cleanly tabular input where it saves ≥ 30% bytes; otherwise
        # falls through to the lossy path with CCR-Dropped retrieval
        # markers. Pass `with_compaction=False` to opt into the
        # lossy-only path (used by retention-property tests
        # that depend on row-level item preservation).
        #
        # `compaction_format` picks the lossless renderer:
        # "csv-schema" (default), "json", or "markdown-kv" (opt-in
        # trade of tokens for model read accuracy). Falls back to the
        # FURL_COMPACTION_FORMAT env var when the kwarg is None.
        # Ignored when with_compaction=False.
        resolved_format = compaction_format or os.environ.get(
            "FURL_COMPACTION_FORMAT", "csv-schema"
        )
        # Validate even when with_compaction=False: an explicit bogus
        # format (kwarg or env var) is a misconfiguration that should be
        # visible, not silently accepted because the knob happens to be
        # ignored on this path.
        if resolved_format not in _SUPPORTED_COMPACTION_FORMATS:
            raise ValueError(
                f"unknown compaction format {resolved_format!r}; "
                f"expected one of: {', '.join(_SUPPORTED_COMPACTION_FORMATS)}"
            )
        self._compaction_format = resolved_format if with_compaction else None
        if not with_compaction:
            self._rust = _RustSmartCrusher.without_compaction(rust_cfg)
        elif resolved_format == "csv-schema":
            # Keep the `new()` constructor for the default path so its
            # byte-parity coverage stays on the exact production
            # codepath.
            self._rust = _RustSmartCrusher(rust_cfg)
        else:
            self._rust = _RustSmartCrusher.with_compaction_format(rust_cfg, resolved_format)

    def crush(self, content: str, query: str = "", bias: float = 1.0) -> CrushResult:
        """Crush a single JSON content string.

        Mirrors the retired Python method. Returns a `CrushResult`
        dataclass so call sites that destructure with `asdict()` keep
        working.
        """
        r = self._rust.crush(content, query, bias)
        # ── Row-drop recovery: TYPED, not scraped (parity with the sibling) ──
        #
        # `crush()` now receives the dropped-row CCR hashes (and the granular
        # `#rows` index markers) as TYPED Rust fields, exactly as
        # `crush_array_json` reads `result["ccr_hash"]` (`:483`). Unlike the
        # sibling — which crushes ONE top-level array and so surfaces ONE hash
        # — `crush()` recurses via `process_value` and can drop rows from MANY
        # sub-arrays, so the fields are PLURAL. Mirror each DIRECTLY into the
        # Python compression_store via the same `_mirror_single_hash...` /
        # `_mirror_row_index...` helpers the sibling uses.
        #
        # This retires the row-drop half of the scrape: row-drop recovery no
        # longer DEPENDS on substring-scanning `<<ccr:HASH>>` out of
        # `r.compressed`. The A1 fail-safe is preserved — these are the very
        # methods that raise `CcrMirrorError` on a store failure (and, with
        # `typed=True`, on a Rust store MISS — COR-5: the engine knows it
        # dropped these rows, so a missing entry is a dangling marker, not a
        # foreign one), propagating up through this `crush()` call to
        # ContentRouter's fail-open boundary (the `crush()` call site is
        # `content_router.py:1043`, caught by the `except` at `:1118`), which
        # reverts to the ORIGINAL uncompressed content so the lossy drop
        # never stands without a recovery copy.
        for ccr_hash in r.ccr_hashes:
            self._mirror_single_hash_to_python_store(
                ccr_hash,
                strategy="smart_crusher_row_drop",
                query_context=query,
                tool_name=None,
                typed=True,
            )
        # Granular per-blob row index → per-row chunks, for proportional
        # retrieval. Each typed marker carries one `HASH#rows` key; expand it.
        for index_key in self._row_index_keys(r.row_index_markers):
            self._mirror_row_index_to_python_store(
                index_key,
                strategy="smart_crusher_row_drop",
                query_context=query,
                tool_name=None,
                typed=True,
            )
        # ── OPAQUE-blob markers stay on the scrape (both paths; 1b) ──
        #
        # Opaque substitutions (`<<ccr:HASH,KIND,SIZE>>`, comma shape) are NOT
        # yet typed — the sibling keeps them on the scrape too (`:494-508`).
        # Scope crush()'s scrape to the OPAQUE shape ONLY, so the row-drop
        # marker that is ALSO present in `r.compressed` is not re-handled as a
        # recovery path here (row-drop comes typed, above). De-dup mechanism:
        # SHAPE-scoped scrape — the opaque walker matches the comma delimiter
        # (`<<ccr:HASH,`) and deliberately ignores the space-delimited
        # row-drop and `#rows` shapes.
        self._mirror_opaque_ccr_markers_in_text(
            r.compressed,
            strategy=r.strategy,
            query_context=query,
            tool_name=None,
        )
        return CrushResult(
            compressed=r.compressed,
            original=r.original,
            was_modified=r.was_modified,
            strategy=r.strategy,
        )

    @staticmethod
    def _row_index_keys(row_index_markers: list[str]) -> list[str]:
        """Extract the ``HASH#rows`` index keys from the typed
        ``row_index_markers`` (each a full ``<<ccr:HASH#rows N_chunks>>``
        marker). Uses the owned grammar scan so the key form is single-sourced
        with the producer. Returns de-duplicated keys preserving first-seen
        order."""
        keys: list[str] = []
        seen: set[str] = set()
        for marker in row_index_markers:
            sink: set[str] = set()
            SmartCrusher._collect_ccr_hashes_from_string(marker, sink)
            for h in sink:
                if h.endswith("#rows") and h not in seen:
                    seen.add(h)
                    keys.append(h)
        return keys

    def crush_array_json(
        self,
        items_json: str,
        query: str = "",
        bias: float = 1.0,
    ) -> dict[str, Any]:
        """Crush a JSON array directly and surface the structured result.

        Returns a dict with `items` (kept rows as JSON), `ccr_hash` (12-char
        hash if rows were dropped), `dropped_summary` (the marker text),
        `strategy_info`, `compacted` (rendered bytes when lossless won),
        and `compaction_kind`.

        Used by tests and by the CCR retrieval flow when it needs
        the hash directly rather than parsing it out of a prompt marker.
        """
        result: dict[str, Any] = self._rust.crush_array_json(items_json, query, bias)
        # Row-drop case: Rust returns the structured `ccr_hash` and has
        # already stashed the canonical in its own store. Mirror that
        # entry into the Python compression_store keyed by the same
        # 12-char SHA-256 hash so /v1/retrieve resolves it.
        ccr_hash = result.get("ccr_hash")
        if ccr_hash:
            self._mirror_single_hash_to_python_store(
                ccr_hash,
                strategy=str(result.get("strategy_info") or "smart_crusher_row_drop"),
                query_context=query,
                tool_name=None,
                # `ccr_hash` is a TYPED Rust field — a store miss is a
                # dangling marker, never a foreign one (COR-5).
                typed=True,
            )
        # Opaque-blob substitutions inside the kept items also produce
        # markers. Walk the rendered shape to bridge those too.
        kept_json = result.get("items")
        if isinstance(kept_json, str) and "<<ccr:" in kept_json:
            self._mirror_ccr_markers_in_text(
                kept_json,
                strategy=str(result.get("strategy_info") or "smart_crusher"),
                query_context=query,
                tool_name=None,
            )
        compacted = result.get("compacted")
        if isinstance(compacted, str) and "<<ccr:" in compacted:
            self._mirror_ccr_markers_in_text(
                compacted,
                strategy=str(result.get("strategy_info") or "smart_crusher"),
                query_context=query,
                tool_name=None,
            )
        return result

    def compact_document_json(self, doc_json: str) -> str:
        """Run the document walker on ``doc_json`` and return compacted JSON.

        Lossless walker pass over objects, arrays, and strings —
        tabular sub-arrays become CSV+schema strings, long opaque
        blobs become ``<<ccr:HASH,KIND,SIZE>>`` markers (originals
        stashed in this crusher's CCR store, so ``ccr_get`` resolves them).

        Use this when callers want pure document-shape compaction
        without per-array lossy crushing.
        """
        result: str = self._rust.compact_document_json(doc_json)
        # Mirror any opaque-blob markers the walker emitted into the
        # Python store so /v1/retrieve resolves them.
        if "<<ccr:" in result:
            self._mirror_ccr_markers_in_text(
                result,
                strategy="smart_crusher_compact_document",
                query_context="",
                tool_name=None,
            )
        return result

    def ccr_get(self, hash_key: str) -> str | None:
        """Look up an original payload by CCR hash from the Rust store.

        Returns the canonical-JSON serialization of the original
        `[item, item, ...]` array that the lossy path stashed before
        emitting `<<ccr:HASH ...>>`. Returns ``None`` if the hash is
        unknown, expired, or no store is configured.

        Used by the CCR retrieval tool to serve the dropped
        rows back to the LLM on demand.
        """
        result: str | None = self._rust.ccr_get(hash_key)
        return result

    def ccr_len(self) -> int:
        """Number of entries currently held by the Rust CCR store."""
        n: int = self._rust.ccr_len()
        return n

    def _smart_crush_content(
        self,
        content: str,
        query_context: str = "",
        tool_name: str | None = None,
        bias: float = 1.0,
    ) -> tuple[str, bool, str]:
        """Apply smart crushing; return `(crushed, was_modified, info)`.

        Mirrors the retired Python method's tuple shape. `tool_name` is
        threaded through to the CCR store mirror's entry metadata; ``None``
        (e.g. the legacy pipeline doesn't have one in scope) is accepted.
        """
        crushed, was_modified, info = self._rust.smart_crush_content(content, query_context, bias)
        # Bridge any CCR markers (row-drop sentinels or opaque-blob
        # substitutions) emitted by the Rust crusher into the Python
        # compression_store so /v1/retrieve resolves them.
        self._mirror_ccr_to_python_store(
            rendered=crushed,
            strategy=info or "smart_crusher",
            query_context=query_context,
            tool_name=tool_name,
        )
        return crushed, was_modified, info

    # ─── CCR Rust → Python store bridge ───────────────────────────────────
    #
    # SmartCrusher's row-drop and opaque-blob paths emit
    # `<<ccr:HASH ...>>` markers and stash the original payload in the
    # Rust process-local CCR store. /v1/retrieve queries the Python
    # `compression_store` via `get_compression_store()` — which is a
    # different store. Without an explicit bridge, every retrieve call
    # for a marker emitted by the Rust crusher returns 404.
    #
    # The bridge is straight Rust→Python mirror: extract every
    # `<<ccr:HASH>>` hash from the rendered output, fetch the canonical
    # bytes via `self._rust.ccr_get(hash)`, and call
    # `compression_store.store(..., explicit_hash=hash)` so the Python
    # store is keyed by the exact hash that's in the prompt marker.
    #
    # Best-effort by design: a missing compression_store import (e.g.
    # in a stripped CLI build) or a transient store error must NOT
    # break compression itself. Compression has already succeeded; the
    # bridge just makes /v1/retrieve work. Errors log at debug.

    def _mirror_ccr_to_python_store(
        self,
        rendered: str,
        strategy: str,
        query_context: str,
        tool_name: str | None,
    ) -> None:
        """Walk `rendered` for any `<<ccr:HASH ...>>` markers and mirror
        each into the Python `compression_store`.

        `rendered` may be a JSON string (the standard SmartCrusher
        output format) or arbitrary text. We try the structured walk
        first; if that fails we fall back to a non-regex token scan.
        """
        # Cheap pre-filter — most outputs have no marker at all.
        if "<<ccr:" not in rendered:
            return
        self._mirror_ccr_markers_in_text(
            rendered,
            strategy=strategy,
            query_context=query_context,
            tool_name=tool_name,
        )

    def _mirror_ccr_markers_in_text(
        self,
        rendered: str,
        strategy: str,
        query_context: str,
        tool_name: str | None,
    ) -> None:
        """Find every distinct `<<ccr:HASH...>>` hash in `rendered` and
        mirror Rust→Python store for each.

        Tries JSON-tree walk first (structured, handles nested shapes);
        falls back to a token scan if `rendered` isn't valid JSON.
        Both paths avoid regex per
        ``feedback_no_silent_fallbacks``-adjacent rule that prefers
        structured parsing.
        """
        hashes: set[str] = set()
        try:
            parsed = json.loads(rendered)
            self._collect_ccr_hashes(parsed, hashes)
        except (json.JSONDecodeError, ValueError):
            # Output isn't valid JSON (rare — `smart_crush_content`
            # always re-serializes via `python_safe_json_dumps`). Fall
            # through to a string-token scan so we still bridge.
            self._collect_ccr_hashes_from_string(rendered, hashes)
        if not hashes:
            return
        for h in hashes:
            self._mirror_single_hash_to_python_store(
                h,
                strategy=strategy,
                query_context=query_context,
                tool_name=tool_name,
            )

    @staticmethod
    def _collect_ccr_hashes(value: Any, sink: set[str]) -> None:
        """Recursively walk a parsed-JSON value, appending every CCR
        hash found inside string leaves to `sink`. Never raises."""
        if isinstance(value, str):
            SmartCrusher._collect_ccr_hashes_from_string(value, sink)
            return
        if isinstance(value, dict):
            for v in value.values():
                SmartCrusher._collect_ccr_hashes(v, sink)
            return
        if isinstance(value, list):
            for v in value:
                SmartCrusher._collect_ccr_hashes(v, sink)
            return
        # ints/bools/None/floats — no markers possible

    @staticmethod
    def _collect_ccr_hashes_from_string(s: str, sink: set[str]) -> None:
        """Extract every `<<ccr:HASH...>>` hash from a string by
        substring scan (no regex). The marker grammar is fixed:

            <<ccr:HASH<sep>...>>

        where ``HASH`` is `[0-9a-f]+` and ``<sep>`` is one of the
        delimiters the Rust emitters use today: a single space (the
        row-drop summary, ``<<ccr:abc 100_rows_offloaded>>``; or the
        GRANULAR row-index marker ``<<ccr:abc#rows 100_chunks>>``) or a
        comma (the opaque-blob marker, ``<<ccr:abc,base64,4.5KB>>``).
        We accept either delimiter and tolerate `>>` as the terminator
        (the case where the marker is just `<<ccr:abc>>` with no
        suffix, used by the bare CCR helpers).

        The granular row-index key carries a literal ``#rows`` suffix
        (``abc#rows``); it is captured WITH the suffix so the mirror can
        tell an index from a row/blob hash and expand it to per-row
        chunks.
        """
        # Prefix + hex alphabet come from the owned grammar spec
        # (marker_grammar). This walker intentionally enforces NO width — it
        # keeps any hex run, unlike the strict-width regex consumer — and keeps
        # the ``#rows`` suffix below. Behavior is unchanged; only the literals
        # are now sourced from the single owner.
        idx = 0
        prefix = marker_grammar.CCR_PREFIX
        n = len(s)
        while True:
            start = s.find(prefix, idx)
            if start == -1:
                return
            cursor = start + len(prefix)
            end = cursor
            while end < n and s[end] in marker_grammar.HEX_ALPHABET:
                end += 1
            if end == cursor:
                # No hex chars after `<<ccr:` — not a real marker.
                idx = cursor
                continue
            hash_str = s[cursor:end].lower()
            # Granular row-index key: `<<ccr:HASH#rows N_chunks>>`. Keep
            # the `#rows` suffix so `_mirror_single_hash_to_python_store`
            # recognizes the index and mirrors the per-row chunks.
            if s[end:].startswith("#rows"):
                hash_str = f"{hash_str}#rows"
                end += len("#rows")
            sink.add(hash_str)
            idx = end

    # ── OPAQUE-only scrape (crush() de-dup, pass 1a) ──────────────────────
    #
    # `crush()` recovers ROW-DROP + row-index from TYPED Rust fields now.
    # The rendered `r.compressed` still contains those row-drop markers, so a
    # full scrape would re-handle them as a recovery path — coupling row-drop
    # recovery back to the scrape, which 1a exists to retire. These two
    # helpers scope crush()'s scrape to the OPAQUE marker shape ONLY
    # (`<<ccr:HASH,KIND,SIZE>>`, comma delimiter — grammar shape C), so:
    #   * row-drop (`<<ccr:HASH N_rows_offloaded>>`, space)   → skipped here,
    #     comes typed;
    #   * row-index (`<<ccr:HASH#rows N_chunks>>`, `#` then space) → skipped,
    #     comes typed;
    #   * opaque (`<<ccr:HASH,...>>`, comma)                  → handled here.
    # Opaque markers are NOT typed yet (pass 1b); the sibling `crush_array_json`
    # keeps them on its own scrape too (`:494-508`). These helpers are
    # crush()-private; the shared `_mirror_ccr_markers_in_text` /
    # `_collect_ccr_hashes_from_string` are UNCHANGED for every other caller
    # (`crush_array_json`, `compact_document_json`, `apply`).

    def _mirror_opaque_ccr_markers_in_text(
        self,
        rendered: str,
        strategy: str,
        query_context: str,
        tool_name: str | None,
    ) -> None:
        """Mirror only OPAQUE-blob (`<<ccr:HASH,...>>`) markers from
        `rendered`. Structured JSON walk first, string-token fallback —
        same two-pass shape as `_mirror_ccr_markers_in_text`, but the
        collector keeps the comma shape only. Each opaque hash is a bare
        12-hex key resolved through the same `_mirror_single_hash...` path
        (which retains its A1 fail-safe)."""
        if "<<ccr:" not in rendered:
            return
        hashes: set[str] = set()
        try:
            parsed = json.loads(rendered)
            self._collect_opaque_ccr_hashes(parsed, hashes)
        except (json.JSONDecodeError, ValueError):
            self._collect_opaque_ccr_hashes_from_string(rendered, hashes)
        for h in hashes:
            self._mirror_single_hash_to_python_store(
                h,
                strategy=strategy,
                query_context=query_context,
                tool_name=tool_name,
            )

    @staticmethod
    def _collect_opaque_ccr_hashes(value: Any, sink: set[str]) -> None:
        """Recursively walk a parsed-JSON value, collecting only OPAQUE
        marker hashes from string leaves. Never raises."""
        if isinstance(value, str):
            SmartCrusher._collect_opaque_ccr_hashes_from_string(value, sink)
            return
        if isinstance(value, dict):
            for v in value.values():
                SmartCrusher._collect_opaque_ccr_hashes(v, sink)
            return
        if isinstance(value, list):
            for v in value:
                SmartCrusher._collect_opaque_ccr_hashes(v, sink)
            return

    @staticmethod
    def _collect_opaque_ccr_hashes_from_string(s: str, sink: set[str]) -> None:
        """Extract OPAQUE-blob hashes (`<<ccr:HASH,KIND,SIZE>>`) from `s` by
        substring scan — the comma delimiter is what distinguishes shape C
        from the space-delimited row-drop (A) and the `#rows` index (B).

        Same no-regex walk + hex alphabet as
        `_collect_ccr_hashes_from_string`, but a hash is collected ONLY when
        the character immediately after the hex run is a comma. Row-drop and
        `#rows` markers are therefore ignored here (they come typed)."""
        idx = 0
        prefix = marker_grammar.CCR_PREFIX
        n = len(s)
        while True:
            start = s.find(prefix, idx)
            if start == -1:
                return
            cursor = start + len(prefix)
            end = cursor
            while end < n and s[end] in marker_grammar.HEX_ALPHABET:
                end += 1
            if end == cursor:
                # No hex after `<<ccr:` — not a real marker.
                idx = cursor
                continue
            # OPAQUE shape iff the delimiter after the hash is a comma.
            if end < n and s[end] == ",":
                sink.add(s[cursor:end].lower())
            idx = end

    def _mirror_single_hash_to_python_store(
        self,
        ccr_hash: str,
        strategy: str,
        query_context: str,
        tool_name: str | None,
        typed: bool = False,
    ) -> None:
        """Mirror a single Rust-stored CCR entry into the Python
        compression_store, keyed by `ccr_hash`.

        ``typed=True`` marks a hash the ENGINE ITSELF surfaced as a typed
        Rust field (``CrushResult.ccr_hashes`` / ``crush_array_json``'s
        ``ccr_hash``) — the engine KNOWS it dropped those rows, so a Rust
        store miss is a dangling marker, not a foreign marker, and the
        mirror raises ``CcrMirrorError`` (COR-5). Scraped hashes (the
        default) keep the graceful debug-skip: a hash substring-scanned out
        of rendered text may genuinely belong to another transform.

        GRANULAR row-index keys (``HASH#rows``) are expanded: the index
        (a JSON array of per-row hashes) is fetched from Rust and EACH
        per-row chunk is mirrored into Python under its own hex hash, so
        the Python ``/v1/retrieve`` can serve a SINGLE row instead of the
        whole blob. The ``#rows`` key itself is not stored in Python (its
        non-hex form fails the store's hex-hash validation, and Python
        retrieve is keyed by the per-row hex hashes anyway).
        """
        if ccr_hash.endswith("#rows"):
            self._mirror_row_index_to_python_store(
                ccr_hash,
                strategy=strategy,
                query_context=query_context,
                tool_name=tool_name,
                typed=typed,
            )
            return
        canonical = self._rust.ccr_get(ccr_hash)
        if canonical is None:
            if typed:
                # COR-5: for a TYPED hash the "marker leaked from
                # elsewhere" excuse is impossible — the engine reported
                # this exact drop, so a miss means the entry was already
                # evicted/expired (COR-4 bounds the chunk flood at the
                # producer, but in_memory.rs documents the window "cannot
                # be fully eliminated"). The surfaced `<<ccr:HASH>>`
                # marker dangles and the dropped rows are UNRECOVERABLE —
                # the silent loss the store contract forbids, and Python
                # is the last place to catch it. Raise so compress()'s
                # fail-open boundary (compress.py:386) discards the lossy
                # output and returns the ORIGINAL messages.
                raise CcrMirrorError(
                    f"CCR mirror: typed row-drop hash {ccr_hash} missing "
                    f"from the Rust store; its <<ccr:{ccr_hash}>> marker "
                    f"dangles and the dropped rows would be unrecoverable"
                )
            # SCRAPED hash the Rust store doesn't have — either the marker
            # came from somewhere else (defensive: another transform's
            # marker leaked into our input), or the entry expired between
            # emission and mirror. Either way, nothing to mirror.
            logger.debug(
                "CCR mirror: hash %s not in Rust store (skipped)",
                ccr_hash,
            )
            return
        try:
            from ..cache.compression_store import get_compression_store
        except ImportError as e:
            # The lossy row-drop has ALREADY happened in the Rust store
            # (ephemeral, process-local). If we cannot reach the Python
            # compression_store — the store production /v1/retrieve reads —
            # the dropped rows are UNRECOVERABLE for the consumer that holds
            # only the output. That is the silent-loss the store's contract
            # forbids ("no SILENT loss," compression_store.py:234-244).
            # Raise so the failure propagates to compress()'s fail-open
            # boundary (compress.py:386), which discards the lossy output and
            # returns the ORIGINAL messages uncompressed — nothing lost.
            raise CcrMirrorError(
                f"CCR mirror: compression_store module unavailable; "
                f"hash {ccr_hash} would be unrecoverable"
            ) from e
        try:
            store = get_compression_store()
        except Exception as e:
            # Same loss class as the ImportError branch above: the row-drop
            # is committed in Rust but the Python store is unreachable, so a
            # later retrieve() returns None silently. Fail-safe via the
            # compress() boundary instead of swallowing.
            raise CcrMirrorError(
                f"CCR mirror: cannot get compression_store ({e}); "
                f"hash {ccr_hash} would be unrecoverable"
            ) from e
        # The TTL on the Python store defaults to 5 minutes — same as
        # the Rust store's `DEFAULT_TTL` (see crates/furl-core/src/
        # ccr/mod.rs). No need to override.
        try:
            store.store(
                original=canonical,
                # The "compressed" payload for the row-drop case isn't
                # readily available here (the rendered output may be
                # only one of many crushed sub-arrays). Use the marker
                # itself as a placeholder — `/v1/retrieve` returns
                # `original_content` and `compressed` isn't surfaced.
                compressed=f"<<ccr:{ccr_hash}>>",
                tool_name=tool_name,
                query_context=query_context if query_context else None,
                compression_strategy=strategy,
                explicit_hash=ccr_hash,
            )
        except ValueError:
            # explicit_hash validation failed — the marker had a
            # malformed hash (shouldn't happen in practice).
            logger.warning(
                "CCR mirror: invalid hash %r from rendered marker",
                ccr_hash,
            )
        except Exception as e:
            # CORE silent-loss branch: the lossy row-drop is committed in the
            # ephemeral Rust store, but the Python store write FAILED, so the
            # dropped rows never reach the store production retrieval reads —
            # a later retrieve() returns None and the recovery data is GONE.
            # Raise so the failure reaches compress()'s fail-open boundary
            # (compress.py:386), which reverts to the ORIGINAL uncompressed
            # messages. The lossy drop never stands without a recovery copy.
            raise CcrMirrorError(
                f"CCR mirror: store.store() failed for hash {ccr_hash} "
                f"({e}); dropped rows would be unrecoverable"
            ) from e

    def _mirror_row_index_to_python_store(
        self,
        index_key: str,
        strategy: str,
        query_context: str,
        tool_name: str | None,
        typed: bool = False,
    ) -> None:
        """Expand a granular row-index key (``HASH#rows``) and mirror each
        per-row chunk into the Python compression_store under its own hex
        hash. This is what makes Python-side ``/v1/retrieve`` PROPORTIONAL:
        a single needed row resolves to exactly that one row, not the
        whole offloaded blob. Best-effort — a missing index or chunk just
        leaves the whole-blob fallback (mirrored from ``_ccr_dropped``)
        in place.

        ``typed=True`` marks an index key carried by a typed
        ``CrushResult.row_index_markers`` entry. An index miss stays
        graceful IFF the whole-blob still resolves; when the blob is ALSO
        gone, a typed index marker is the same dangling-marker loss class
        as a typed row-drop hash and raises ``CcrMirrorError`` (COR-5).
        """
        index_raw = self._rust.ccr_get(index_key)
        if index_raw is None:
            # The per-row index is missing. GRACEFUL DEGRADATION iff the
            # whole-blob entry (mirrored from `_ccr_dropped`) still
            # resolves: it recovers the data — just coarser (the full blob
            # instead of one row). That state is by design: FIFO eviction
            # sheds the redundant chunks/index BEFORE the whole-blob
            # (persist_dropped's write order), and post-COR-4 an OVERSIZED
            # drop (> capacity/4) never writes an index at all. Warn for
            # visibility; do NOT raise.
            if typed and self._rust.ccr_get(index_key.removesuffix("#rows")) is None:
                # TYPED index marker with the whole-blob backstop ALSO
                # gone: nothing recovers the drop — same dangling-marker
                # silent-loss class as a typed blob miss (COR-5). Raise so
                # compress()'s fail-open reverts to the originals.
                raise CcrMirrorError(
                    f"CCR mirror: typed row index {index_key} AND its "
                    f"whole-blob entry are missing from the Rust store; "
                    f"the dropped rows would be unrecoverable"
                )
            logger.warning("CCR mirror: row index %s not in Rust store", index_key)
            return
        try:
            row_hashes = json.loads(index_raw)
        except (json.JSONDecodeError, ValueError):
            # Same graceful degradation: the index is unparseable but the
            # whole-blob fallback still recovers. Warn, do NOT raise.
            logger.warning("CCR mirror: row index %s is not valid JSON", index_key)
            return
        if not isinstance(row_hashes, list):
            return
        for row_hash in row_hashes:
            if not isinstance(row_hash, str):
                continue
            # Each per-row chunk is a pure-hex hash keying a 1-element
            # canonical array — mirror it like any whole-blob entry so
            # Python retrieve can serve that single row.
            self._mirror_single_hash_to_python_store(
                row_hash,
                strategy=strategy,
                query_context=query_context,
                tool_name=tool_name,
            )

    def _extract_context_from_messages(self, messages: list[dict[str, Any]]) -> str:
        """Build a query string from the last 5 user messages + recent
        assistant tool-call arguments. Used by `apply()` to derive the
        relevance context per-request.

        Pure Python because it walks the message envelope, not the
        compressed payload. The retired implementation lived inline on
        `SmartCrusher`; preserved here unchanged.
        """
        context_parts: list[str] = []
        user_message_count = 0

        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    context_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                context_parts.append(text)

                user_message_count += 1
                if user_message_count >= 5:
                    break

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    if isinstance(tc, dict):
                        func = tc.get("function", {})
                        args = func.get("arguments", "")
                        if isinstance(args, str) and args:
                            context_parts.append(args)

        return " ".join(context_parts)

    def _notify_observer(self, original_tokens: int, compressed_tokens: int) -> None:
        """Forward a compression event to the configured observer via
        its ``record_compression(...)`` method.
        No-op when no observer is set; swallows observer exceptions at
        debug level so a buggy metrics impl doesn't break the
        compression that just succeeded.
        """
        if self._observer is None:
            return
        try:
            self._observer.record_compression(
                strategy="smart_crusher",
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("CompressionObserver raised (non-fatal): %s", e)

    def apply(
        self,
        messages: list[dict[str, Any]],
        tokenizer: Tokenizer,
        **kwargs: Any,
    ) -> TransformResult:
        """Transform-protocol entry point. Walks every tool/tool_result
        message, applies SmartCrusher to large enough payloads, and
        replaces the message content with `<crushed>\\n<digest_marker>`.

        Pure orchestration — the per-message compression delegates to
        Rust via `_smart_crush_content`.
        """
        tokens_before = tokenizer.count_messages(messages)
        result_messages = deep_copy_messages(messages)
        transforms_applied: list[str] = []
        markers_inserted: list[str] = []
        warnings: list[str] = []

        query_context = self._extract_context_from_messages(result_messages)
        crushed_count = 0
        frozen_message_count = kwargs.get("frozen_message_count", 0)

        crushed_tool_names: list[str] = []
        seen_tool_names: set[str] = set()
        tool_names_by_id = _build_tool_name_index(result_messages)

        def _record(tool_id: str | None) -> None:
            name = tool_names_by_id.get(tool_id or "")
            if name and name not in seen_tool_names:
                seen_tool_names.add(name)
                crushed_tool_names.append(name)

        for msg_idx, msg in enumerate(result_messages):
            if msg_idx < frozen_message_count:
                continue

            # OpenAI-style: top-level role=tool with string content.
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str):
                    tokens = tokenizer.count_text(content)
                    if tokens > self.config.min_tokens_to_crush:
                        crushed, was_modified, info = self._smart_crush_content(
                            content, query_context
                        )
                        if was_modified:
                            marker = create_tool_digest_marker(compute_short_hash(content))
                            msg["content"] = crushed + "\n" + marker
                            crushed_count += 1
                            _record(msg.get("tool_call_id"))
                            markers_inserted.append(marker)
                            if info:
                                transforms_applied.append(f"smart:{info}")
                            self._notify_observer(tokens, tokenizer.count_text(crushed))

            # Anthropic-style: content is a list of blocks; each tool_result
            # block has a string content field of its own — or, in the
            # canonical Anthropic/MCP shape, a nested parts list
            # ``[{"type": "text", "text": …}]`` whose text parts crush
            # individually (COR-47 mirror; previously skipped entirely).
            content = msg.get("content")
            if isinstance(content, list):
                for i, block in enumerate(content):
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        for j, part in enumerate(tool_content):
                            if not isinstance(part, dict) or part.get("type") != "text":
                                continue
                            part_text = part.get("text", "")
                            if not isinstance(part_text, str):
                                continue
                            part_tokens = tokenizer.count_text(part_text)
                            if part_tokens <= self.config.min_tokens_to_crush:
                                continue
                            crushed, was_modified, info = self._smart_crush_content(
                                part_text, query_context
                            )
                            if was_modified:
                                marker = create_tool_digest_marker(compute_short_hash(part_text))
                                tool_content[j]["text"] = crushed + "\n" + marker
                                crushed_count += 1
                                _record(block.get("tool_use_id"))
                                markers_inserted.append(marker)
                                if info:
                                    transforms_applied.append(f"smart:{info}")
                                self._notify_observer(part_tokens, tokenizer.count_text(crushed))
                        continue
                    if not isinstance(tool_content, str):
                        continue
                    tokens = tokenizer.count_text(tool_content)
                    if tokens <= self.config.min_tokens_to_crush:
                        continue

                    crushed, was_modified, info = self._smart_crush_content(
                        tool_content, query_context
                    )
                    if was_modified:
                        marker = create_tool_digest_marker(compute_short_hash(tool_content))
                        content[i]["content"] = crushed + "\n" + marker
                        crushed_count += 1
                        _record(block.get("tool_use_id"))
                        markers_inserted.append(marker)
                        if info:
                            transforms_applied.append(f"smart:{info}")
                        self._notify_observer(tokens, tokenizer.count_text(crushed))

        if crushed_count > 0:
            transforms_applied.insert(
                0, _format_smart_crush_transform(crushed_count, crushed_tool_names)
            )

        tokens_after = tokenizer.count_messages(result_messages)

        return TransformResult(
            messages=result_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            transforms_applied=transforms_applied,
            markers_inserted=markers_inserted,
            warnings=warnings,
        )


# ─── Convenience function ─────────────────────────────────────────────────


def smart_crush_tool_output(
    content: str,
    config: SmartCrusherConfig | None = None,
    ccr_config: CCRConfig | None = None,
    with_compaction: bool = True,
) -> tuple[str, bool, str]:
    """Compress a single tool output. Returns `(crushed, was_modified, info)`.

    Convenience wrapper that builds a one-shot `SmartCrusher` per call.
    Defaults to the lossless-first behavior; pass
    `with_compaction=False` to exercise the lossy-only path
    (still useful for retention-property tests).
    """
    crusher = SmartCrusher(config=config, ccr_config=ccr_config, with_compaction=with_compaction)
    return crusher._smart_crush_content(content)
