"""TEST-18: PyO3 boundary error paths — the four ValueError shapes.

Each site converts a Rust-side rejection into a *catchable* Python
``ValueError`` (never a panic/abort, per the FFI's ValueError-not-panic
convention). None of these paths had any test: a refactor that turned one
into a panic (process-fatal under pyo3) or into a silently-wrong success
would have shipped invisibly.

The Rust twin of the format-name case (``CompactionStage::from_format_name``
accepting exactly ``SUPPORTED_FORMAT_NAMES``) is pinned in
``crates/furl-core/src/transforms/smart_crusher/compaction/mod.rs``.
"""

from __future__ import annotations

import pytest

import furl_ctx._core as _core


@pytest.fixture(scope="module")
def crusher() -> _core.SmartCrusher:
    return _core.SmartCrusher()


def test_crush_array_json_invalid_json_raises_valueerror(crusher) -> None:
    with pytest.raises(ValueError, match="items_json must be JSON"):
        crusher.crush_array_json("not json[", "", 1.0)


def test_config_unknown_routing_policy_raises_valueerror() -> None:
    # The message enumerates the accepted names — operators can self-serve.
    with pytest.raises(ValueError, match="unknown routing_policy") as exc_info:
        _core.SmartCrusherConfig(routing_policy="banana")
    assert "min-tokens" in str(exc_info.value)
    assert "lossless-first" in str(exc_info.value)


def test_with_compaction_format_unknown_name_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="unknown compaction format") as exc_info:
        _core.SmartCrusher.with_compaction_format(None, "banana")
    # The error enumerates SUPPORTED_FORMAT_NAMES — the same list the Rust
    # sync test pins against from_format_name.
    assert "csv-schema" in str(exc_info.value)


def test_with_compaction_format_accepts_every_supported_name() -> None:
    # Positive half of the sync pair: every advertised name constructs.
    for name in ("csv-schema", "json", "markdown-kv"):
        assert _core.SmartCrusher.with_compaction_format(None, name) is not None


def test_compact_document_json_invalid_json_raises_valueerror(crusher) -> None:
    with pytest.raises(ValueError, match="doc_json must be JSON"):
        crusher.compact_document_json("{broken")
