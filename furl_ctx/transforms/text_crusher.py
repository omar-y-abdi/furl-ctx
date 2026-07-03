"""Rust-backed deterministic prose compressor for PLAIN_TEXT (Engine P2-11).

Thin shim over ``crates/furl-core/src/transforms/text_crusher.rs`` —
the extractive, ML-free segment selector that fills the capability gap
left by the ML-compressor excision (PLAIN_TEXT was a passthrough).

The Rust crate owns the whole pipeline: tag protection
(``tag_protector`` placeholder-substitute → crush → restore),
markdown-aware segmentation (code fences atomic, headers/lists
structural, sentence-ish prose splitting), BM25-vs-query + serial
position + salience scoring, shingle/digit-mask dedup, char-budget
selection, and marker emission. This module:

1. Keeps the public dataclass surface (``TextCrusherConfig``,
   ``TextCrushResult``) so callers (``CompressorRegistry`` /
   ``StrategyDispatcher``) match the log/search/diff wrapper shape.
2. Routes ``TextCrusher.compress()`` through the Rust implementation.
3. Owns the production CCR persistence + the store-failure VETO.

# Reversibility contract (stricter than log/search)

A crush that drops segments ships **if and only if** the FULL ORIGINAL
is retrievable behind the emitted
``[N segments compressed to M. Retrieve more: hash=…]`` marker:

* the Rust side already refuses to build a lossy render without a
  store (no store / ``enable_ccr=False`` / marginal savings → the
  original bytes pass through);
* this shim re-persists the original into the production
  ``CompressionStore`` under the marker's exact hash, and on ANY
  failure (import error, store write error) serves the ORIGINAL
  uncompressed content — the marker never ships dangling. Mirrors
  log/search/diff (`_persist_to_python_ccr` + passthrough veto).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ._ccr_persist import persist_to_python_ccr

logger = logging.getLogger(__name__)


# ─── Public dataclasses (match sibling wrapper shape) ───────────────────────


@dataclass
class TextCrusherConfig:
    """Configuration for prose compression. Mirrors the Rust
    ``TextCrusherConfig`` field-for-field (defaults identical).

    Size-floor rationale (see the Rust module docs): ``min_chars=600``
    (~150 tokens) — below that the ~25-token marker line eats the
    savings; ``min_segments=15`` — the mandatory keeps (first 2 +
    last 2) plus the ``min_kept_segments=5`` floor already retain a
    third of a 15-segment document, i.e. the default ``target_ratio``.
    """

    target_ratio: float = 0.35
    min_chars: int = 600
    min_segments: int = 15
    min_kept_segments: int = 5
    always_keep_first: int = 2
    always_keep_last: int = 2
    shingle_size: int = 4
    dedup_threshold: float = 0.9
    max_pairwise_dedup_segments: int = 2000
    enable_ccr: bool = True
    max_shippable_ratio: float = 0.9
    # Secret-mask keep rail (input-side defense): segments carrying
    # secret-shaped tokens (long high-entropy hex/base64 runs, AKIA/ghp_/
    # sk- prefixed keys, PEM armor, JWTs) join the mandatory keeps so the
    # lossy selector can never drop them into CCR-only visibility. Drop
    # protection only — content is never rewritten (store-side redaction
    # owns log exposure). ``False`` restores pre-rail selection exactly.
    secret_keep_rail: bool = True


@dataclass
class TextCrushResult:
    """Result of prose compression."""

    compressed: str
    original: str
    original_segment_count: int
    compressed_segment_count: int
    compression_ratio: float
    cache_key: str | None = None
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def tokens_saved_estimate(self) -> int:
        """Estimate tokens saved (rough: 1 token per 4 chars)."""
        chars_saved = len(self.original) - len(self.compressed)
        return max(0, chars_saved // 4)

    @property
    def segments_omitted(self) -> int:
        return self.original_segment_count - self.compressed_segment_count


# ─── Compressor (Rust-backed) ───────────────────────────────────────────────


class TextCrusher:
    """Deterministic extractive prose compressor via the Rust port.

    ``compress()`` delegates to Rust end-to-end; segmentation, scoring,
    dedup, selection, and tag protection are pinned by the
    ``text_crusher.rs`` / ``tag_protector.rs`` unit tests.
    """

    def __init__(self, config: TextCrusherConfig | None = None) -> None:
        # Hard import — no fallback. If the wheel is missing, the user
        # must build it. See feedback memory `feedback_no_silent_fallbacks.md`.
        from furl_ctx._core import (
            TextCrusher as _RustTextCrusher,
        )
        from furl_ctx._core import (
            TextCrusherConfig as _RustTextCrusherConfig,
        )

        cfg = config or TextCrusherConfig()
        self.config = cfg
        self._rust = _RustTextCrusher(
            _RustTextCrusherConfig(
                target_ratio=cfg.target_ratio,
                min_chars=cfg.min_chars,
                min_segments=cfg.min_segments,
                min_kept_segments=cfg.min_kept_segments,
                always_keep_first=cfg.always_keep_first,
                always_keep_last=cfg.always_keep_last,
                shingle_size=cfg.shingle_size,
                dedup_threshold=cfg.dedup_threshold,
                max_pairwise_dedup_segments=cfg.max_pairwise_dedup_segments,
                enable_ccr=cfg.enable_ccr,
                max_shippable_ratio=cfg.max_shippable_ratio,
                secret_keep_rail=cfg.secret_keep_rail,
            )
        )

    # ─── Public API ─────────────────────────────────────────────────────

    def compress(self, content: str, context: str = "", bias: float = 1.0) -> TextCrushResult:
        """Compress prose. ``context`` feeds the BM25 relevance arm;
        ``bias`` scales the keep budget (>1 keeps more)."""
        rust_result = self._rust.compress(content, context, bias)
        cache_key: str | None = rust_result.cache_key
        if cache_key is not None and not self._persist_to_python_ccr(
            content, rust_result.compressed, cache_key
        ):
            # Store write failed → marker would dangle, dropped segments
            # unrecoverable. Serve the original uncompressed prose
            # instead (mirrors log/search/diff + cross_message_dedup).
            return self._passthrough_result(content, rust_result)

        return TextCrushResult(
            compressed=rust_result.compressed,
            original=content,
            original_segment_count=rust_result.original_segment_count,
            compressed_segment_count=rust_result.compressed_segment_count,
            compression_ratio=rust_result.compression_ratio,
            cache_key=cache_key,
            stats={
                "segments_total": rust_result.segments_total,
                "segments_kept": rust_result.segments_kept,
                "segments_dropped_by_dedup": rust_result.segments_dropped_by_dedup,
                "segments_dropped_by_budget": rust_result.segments_dropped_by_budget,
                "protected_tag_blocks": rust_result.protected_tag_blocks,
                "mandatory_keeps": rust_result.mandatory_keeps,
                "secret_keep_segments": rust_result.secret_keep_segments,
            },
        )

    # ─── Internal CCR persistence ───────────────────────────────────────

    def _persist_to_python_ccr(self, original: str, compressed: str, cache_key: str) -> bool:
        """Promote a Rust-emitted cache_key into the production Python
        CompressionStore. Returns ``True`` on success, ``False`` on any
        failure (store import or store write) — the caller then serves the
        ORIGINAL uncompressed prose so the CCR marker never ships dangling.

        Thin delegator to the shared :func:`~._ccr_persist.persist_to_python_ccr`
        (ARCH-5; one implementation for the diff/log/search/text/code-aware
        family; ``compression_strategy`` = ``CompressionStrategy.TEXT.value``).
        Kept as a method for the test/monkeypatch seam."""
        return persist_to_python_ccr(
            original,
            compressed,
            cache_key,
            compression_strategy="text",
            logger=logger,
        )

    @staticmethod
    def _passthrough_result(content: str, r: Any) -> TextCrushResult:
        """No-compression result: serve the original prose verbatim with no
        CCR marker, used when the store write vetoes (no dangling marker)."""
        return TextCrushResult(
            compressed=content,
            original=content,
            original_segment_count=r.original_segment_count,
            compressed_segment_count=r.original_segment_count,
            compression_ratio=1.0,
            cache_key=None,
            stats={},
        )


__all__ = [
    "TextCrushResult",
    "TextCrusher",
    "TextCrusherConfig",
]
