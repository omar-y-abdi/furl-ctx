"""Rust-backed log/build-output compressor.

Phase 3e.5 ported the implementation to
`crates/furl-core/src/transforms/log_compressor.rs`. This module
is now a thin shim that:

1. Keeps the public dataclass and enum surface (`LogLevel`,
   `LogFormat`, `LogLine`, `LogCompressorConfig`,
   `LogCompressionResult`) so existing call sites (`ContentRouter`,
   tests) don't change.
2. Routes `LogCompressor.compress()` entirely through the Rust
   implementation, picking up the bug fixes (chained-exception trace
   survival, conservative warning dedupe, loud CCR failures). The Rust
   crate owns format detection, line classification/scoring, dedupe,
   selection, and output formatting; their behavior is pinned by the
   `log_compressor.rs` unit tests.

# Bug fixes the Rust port carries (and this shim therefore inherits)

* **Stack-trace state machine.** Pre-3e.5 Python terminated on any
  blank line, dropping mid-trace lines from chained-exception traces.
  Rust dispatches per language flavor so blank lines stay inside
  Python tracebacks.
* **Conservative dedupe.** Pre-3e.5 normalised digits/paths/hex
  globally, collapsing distinct error categories that shared a
  trailing variable shape. Rust splits on the first `:`/`=` and only
  normalises the trailing region — message identifiers stay distinct.
* **Loud CCR failures.** Storage failures are logged at warning level
  instead of being silently swallowed.
* **`LogLevel.FAIL` is documented as cosmetic-equivalent to
  `LogLevel.ERROR`.** Both score 1.0 in Python and Rust.

# CCR plumbing note

Same pattern as search_compressor: Rust emits a `cache_key`, the
Python shim writes the original to the production
`CompressionStore`. The Rust crate's CCR store is in-memory and
exists only for unit testing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from ._ccr_persist import persist_to_python_ccr

logger = logging.getLogger(__name__)


class LogFormat(Enum):
    """Detected log format."""

    PYTEST = "pytest"
    NPM = "npm"
    CARGO = "cargo"
    MAKE = "make"
    JEST = "jest"
    GENERIC = "generic"


class LogLevel(Enum):
    """Log level for categorization."""

    ERROR = "error"
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"
    DEBUG = "debug"
    TRACE = "trace"
    UNKNOWN = "unknown"


@dataclass(eq=False)
class LogLine:
    """A single log line with metadata."""

    line_number: int
    content: str
    level: LogLevel = LogLevel.UNKNOWN
    is_stack_trace: bool = False
    is_summary: bool = False
    score: float = 0.0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LogLine):
            return NotImplemented
        return self.line_number == other.line_number

    def __hash__(self) -> int:
        return hash(self.line_number)


@dataclass
class LogCompressorConfig:
    """Configuration for log compression."""

    max_errors: int = 10
    error_context_lines: int = 3
    keep_first_error: bool = True
    keep_last_error: bool = True
    max_stack_traces: int = 3
    stack_trace_max_lines: int = 20
    max_warnings: int = 5
    dedupe_warnings: bool = True
    keep_summary_lines: bool = True
    max_total_lines: int = 100
    enable_ccr: bool = True
    min_lines_for_ccr: int = 50


@dataclass
class LogCompressionResult:
    """Result of log compression."""

    compressed: str
    original: str
    original_line_count: int
    compressed_line_count: int
    format_detected: LogFormat
    compression_ratio: float
    cache_key: str | None = None
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def tokens_saved_estimate(self) -> int:
        chars_saved = len(self.original) - len(self.compressed)
        return max(0, chars_saved // 4)

    @property
    def lines_omitted(self) -> int:
        return self.original_line_count - self.compressed_line_count


# ─── LogCompressor (Rust-backed) ────────────────────────────────────────────


def _format_from_str(name: str) -> LogFormat:
    return {
        "pytest": LogFormat.PYTEST,
        "npm": LogFormat.NPM,
        "cargo": LogFormat.CARGO,
        "make": LogFormat.MAKE,
        "jest": LogFormat.JEST,
    }.get(name, LogFormat.GENERIC)


class LogCompressor:
    """Rust-backed log compressor.

    Drop-in replacement for the retired Python class: `compress()`
    delegates to Rust end-to-end. The retired class's internal parsing
    helpers were NOT preserved; the only Python-side additions are the
    CCR persistence bridge (`_persist_to_python_ccr`) and the
    passthrough result builder.
    """

    def __init__(self, config: LogCompressorConfig | None = None) -> None:
        # Hard import — no fallback. If the wheel is missing, the user
        # must build it. See feedback memory `feedback_no_silent_fallbacks.md`.
        from furl_ctx._core import (
            LogCompressor as _RustLogCompressor,
        )
        from furl_ctx._core import (
            LogCompressorConfig as _RustLogCompressorConfig,
        )

        cfg = config or LogCompressorConfig()
        self.config = cfg
        # `min_compression_ratio_for_ccr` was inlined as 0.5 in Python;
        # the Rust port promoted it to a config field but defaults
        # match.
        self._rust = _RustLogCompressor(
            _RustLogCompressorConfig(
                max_errors=cfg.max_errors,
                error_context_lines=cfg.error_context_lines,
                keep_first_error=cfg.keep_first_error,
                keep_last_error=cfg.keep_last_error,
                max_stack_traces=cfg.max_stack_traces,
                stack_trace_max_lines=cfg.stack_trace_max_lines,
                max_warnings=cfg.max_warnings,
                dedupe_warnings=cfg.dedupe_warnings,
                keep_summary_lines=cfg.keep_summary_lines,
                max_total_lines=cfg.max_total_lines,
                enable_ccr=cfg.enable_ccr,
                min_lines_for_ccr=cfg.min_lines_for_ccr,
                min_compression_ratio_for_ccr=0.5,
            )
        )

    # ─── Public API ─────────────────────────────────────────────────────

    def compress(self, content: str, context: str = "", bias: float = 1.0) -> LogCompressionResult:
        # `context` is unused upstream and unused here (Python original
        # also didn't use it). Kept in the signature for drop-in compat.
        del context
        rust_result = self._rust.compress(content, bias)
        cache_key: str | None = rust_result.cache_key
        if cache_key is not None and not self._persist_to_python_ccr(
            content, rust_result.compressed, cache_key
        ):
            # Store write failed → marker would dangle, dropped lines
            # unrecoverable. Serve the original uncompressed log instead
            # (mirrors cross_message_dedup's veto).
            return self._passthrough_result(content, rust_result)

        stats_dict = {k: int(v) for k, v in cast("dict[str, int]", rust_result.stats).items()}
        return LogCompressionResult(
            compressed=rust_result.compressed,
            original=content,
            original_line_count=rust_result.original_line_count,
            compressed_line_count=rust_result.compressed_line_count,
            format_detected=_format_from_str(rust_result.format_detected),
            compression_ratio=rust_result.compression_ratio,
            cache_key=cache_key,
            stats=stats_dict,
        )

    # ─── Internal CCR persistence ───────────────────────────────────────

    def _persist_to_python_ccr(self, original: str, compressed: str, cache_key: str) -> bool:
        """Promote a Rust-emitted cache_key into the production Python
        CompressionStore. Returns ``True`` on success, ``False`` on any
        failure (store import or store write) — the caller then serves the
        ORIGINAL uncompressed log so the CCR marker never ships dangling.

        Thin delegator to the shared :func:`~._ccr_persist.persist_to_python_ccr`
        (ARCH-5; one implementation for the diff/log/search/text/code-aware
        family; ``compression_strategy`` = ``CompressionStrategy.LOG.value``).
        Kept as a method for the test/monkeypatch seam."""
        return persist_to_python_ccr(
            original,
            compressed,
            cache_key,
            compression_strategy="log",
            logger=logger,
        )

    @staticmethod
    def _passthrough_result(content: str, r: Any) -> LogCompressionResult:
        """No-compression result: serve the original log verbatim with no CCR
        marker, used when the store write vetoes (no dangling marker)."""
        return LogCompressionResult(
            compressed=content,
            original=content,
            original_line_count=r.original_line_count,
            compressed_line_count=r.original_line_count,
            format_detected=_format_from_str(r.format_detected),
            compression_ratio=1.0,
            cache_key=None,
            stats={},
        )


__all__ = [
    "LogCompressor",
    "LogCompressorConfig",
    "LogCompressionResult",
    "LogFormat",
    "LogLevel",
    "LogLine",
]
