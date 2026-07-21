"""Git diff output compressor — Rust-backed via PyO3.

The Python implementation has been retired (Stage 3b, 2026-04-25). All
diff compression now goes through `furl_ctx._core.DiffCompressor` (built
from `crates/furl-py`). The byte-equality of the two implementations
was verified against 27 recorded fixtures before the Python source was
removed; the Rust crate has its own test coverage in `crates/furl-core/`.

This module retains the public surface — `DiffCompressorConfig`,
`DiffCompressionResult`, `DiffCompressor` — so existing call sites
(ContentRouter, parity recorder, integrations, downstream users) keep
working unchanged. The dataclasses are still pure-Python because they
appear in dataclass-aware code paths (`asdict()`, `__dict__`, dataclass
matching). Only the `DiffCompressor` class delegates to Rust.

The `furl_ctx._core` extension is a hard import: there is no Python
fallback. Build it locally with `scripts/build_rust_extension.sh`
(wraps `maturin develop`) or install a prebuilt wheel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ._ccr_persist import persist_to_python_ccr

logger = logging.getLogger(__name__)


@dataclass
class DiffCompressorConfig:
    """Configuration for diff compression."""

    max_context_lines: int = 2
    max_hunks_per_file: int = 10
    max_files: int = 20
    enable_ccr: bool = True
    min_lines_for_ccr: int = 50
    #: Elide noise hunks before compression: lockfile churn
    #: (package-lock.json / Cargo.lock / uv.lock / yarn.lock) and
    #: whitespace-only hunks, each file summarized with one
    #: ``[noise hunk elided: <path> (+A/-B)]`` line. The CCR marker (when
    #: emitted) backs the FULL original diff, so elided hunks stay
    #: byte-exact recoverable; the store-write veto below still applies.
    #: Default False — output byte-identical to the previous behavior.
    drop_noise_hunks: bool = False


@dataclass
class DiffCompressionResult:
    """Result of diff compression."""

    compressed: str
    original_line_count: int
    compressed_line_count: int
    files_affected: int
    additions: int
    deletions: int
    hunks_kept: int
    hunks_removed: int
    cache_key: str | None = None

    @property
    def compression_ratio(self) -> float:
        if self.original_line_count == 0:
            return 1.0
        return self.compressed_line_count / self.original_line_count

    @property
    def tokens_saved_estimate(self) -> int:
        lines_saved = self.original_line_count - self.compressed_line_count
        chars_saved = lines_saved * 40
        return max(0, chars_saved // 4)


class DiffCompressor:
    """Rust-backed `DiffCompressor` (via PyO3 / `furl_ctx._core`).

    Same `__init__` and `compress` shape as the retired Python class —
    drop-in replacement. Returns Python `DiffCompressionResult` dataclass
    instances so call sites that destructure with `asdict()` or read the
    `@property` fields work unchanged.
    """

    def __init__(self, config: DiffCompressorConfig | None = None):
        # Hard import — no fallback. If the wheel is missing, the user
        # must build it (scripts/build_rust_extension.sh) or install a
        # prebuilt one. Failing loudly here is better than silently
        # degrading; see feedback memory `feedback_no_silent_fallbacks.md`.
        from furl_ctx._core import (
            DiffCompressor as _RustDiffCompressor,
        )
        from furl_ctx._core import (
            DiffCompressorConfig as _RustDiffCompressorConfig,
        )

        cfg = config or DiffCompressorConfig()
        self.config = cfg
        # `min_compression_ratio_for_ccr` was inlined as 0.8 in the Python
        # original; promoted to a config field on the Rust side but left at
        # its 0.8 Rust default here so the existing Python config surface is
        # unchanged (matches search_compressor.py / log_compressor.py).
        self._rust = _RustDiffCompressor(
            _RustDiffCompressorConfig(
                max_context_lines=cfg.max_context_lines,
                max_hunks_per_file=cfg.max_hunks_per_file,
                max_files=cfg.max_files,
                enable_ccr=cfg.enable_ccr,
                min_lines_for_ccr=cfg.min_lines_for_ccr,
                drop_noise_hunks=cfg.drop_noise_hunks,
            )
        )

    def compress(self, content: str, context: str = "") -> DiffCompressionResult:
        r = self._rust.compress(content, context)
        cache_key: str | None = r.cache_key
        if cache_key is not None and not self._persist_to_python_ccr(
            content, r.compressed, cache_key
        ):
            # Store write failed → the CCR marker in r.compressed would
            # dangle (retrieve() can't resolve it) and the dropped hunks
            # would be unrecoverable. Serve the ORIGINAL uncompressed diff
            # instead — no replacement without recoverability (mirrors
            # cross_message_dedup's veto).
            return self._passthrough_result(content, r)
        return DiffCompressionResult(
            compressed=r.compressed,
            original_line_count=r.original_line_count,
            compressed_line_count=r.compressed_line_count,
            files_affected=r.files_affected,
            additions=r.additions,
            deletions=r.deletions,
            hunks_kept=r.hunks_kept,
            hunks_removed=r.hunks_removed,
            cache_key=cache_key,
        )

    def _persist_to_python_ccr(self, original: str, compressed: str, cache_key: str) -> bool:
        """Promote a Rust-emitted cache_key into the production Python
        CompressionStore. Returns ``True`` on success, ``False`` on any
        failure (store import or store write) — the caller then serves the
        ORIGINAL uncompressed diff so a dropped hunk is never
        signalled-but-unrecoverable.

        Thin delegator to the shared :func:`~._ccr_persist.persist_to_python_ccr`
        (ARCH-5; one implementation for the diff/log/search/text/code-aware
        family; ``compression_strategy`` = ``CompressionStrategy.DIFF.value``).
        Kept as a method for the test/monkeypatch seam."""
        return persist_to_python_ccr(
            original,
            compressed,
            cache_key,
            compression_strategy="diff",
            logger=logger,
        )

    @staticmethod
    def _passthrough_result(content: str, r: Any) -> DiffCompressionResult:
        """No-compression result: serve the original diff verbatim with no
        CCR marker, used when the store write vetoes. Stats reflect "nothing
        dropped" so the result object stays honest (compression_ratio → 1.0)."""
        return DiffCompressionResult(
            compressed=content,
            original_line_count=r.original_line_count,
            compressed_line_count=r.original_line_count,
            files_affected=r.files_affected,
            additions=r.additions,
            deletions=r.deletions,
            hunks_kept=r.hunks_kept + r.hunks_removed,
            hunks_removed=0,
            cache_key=None,
        )
