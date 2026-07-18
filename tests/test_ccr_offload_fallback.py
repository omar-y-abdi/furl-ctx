"""CCR-offload fallback: large content no compressor can shrink is stored
byte-exact in the compression store and shipped as an identity preview +
``{"_ccr_dropped": "<<ccr:HASH>>"}`` sentinel + ``Retrieve more`` marker.

Also covers the two router fixes that unlock it: strict marker-grammar
pinning (mentioning marker text is not the same as carrying a marker) and
error protection that no longer pins structured JSON.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    _is_unstructured_error_output,
    _looks_like_ccr_output,
)
from furl_ctx.transforms.router_policy import CompressionStrategy


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_compression_store()
    yield
    reset_compression_store()


def _files_payload(n_files: int = 3, blob_lines: int = 250) -> str:
    """JSON array shaped like file-read tool output: few rows, each with a
    large distinct source-code-like content cell (wordy text, deliberately
    not base64/hex-classifiable so the crusher's opaque-cell path does not
    claim it — mirroring real source files). Deterministic."""
    rows = []
    for i in range(n_files):
        lines = []
        for j in range(blob_lines):
            h = hashlib.sha256(f"{i}:{j}".encode()).hexdigest()
            lines.append(f"def fn_{h[:10]}(value, flag={j}):  # {h[10:22]}")
            lines.append(f"    return transform(value, key='{h[22:40]}', mode={j % 7})")
        rows.append({"path": f"src/module_{i}.py", "content": "\n".join(lines)})
    return json.dumps(rows, ensure_ascii=False)


def _trace_payload(n_events: int = 200) -> str:
    """JSON OBJECT with a dominant inner array — the Chrome DevTools trace
    shape: ``{"metadata": {...header boilerplate...}, "traceEvents": [...]}``.
    The head keys are the object's metadata, NOT the events, so a head/tail
    LINES preview would show almost nothing about the events. Deterministic."""
    metadata = {
        "enhancedTraceVersion": 1,
        "source": "DevTools",
        "hostDPR": 2.0,
        "sourceMaps": [],
    }
    events = [
        {"name": f"Event::{i}", "cat": "devtools.timeline", "ph": "X", "ts": 1000 + i, "pid": 1}
        for i in range(n_events)
    ]
    return json.dumps({"metadata": metadata, "traceEvents": events}, ensure_ascii=False)


def _offload_router() -> ContentRouter:
    """SmartCrusher disabled so the strategy chain deterministically declines
    and the fallback itself is the unit under test."""
    return ContentRouter(ContentRouterConfig(enable_smart_crusher=False))


def _recover(compressed_marker_text: str) -> str | None:
    """Retrieve the stored original for the ``<<ccr:HASH>>`` in *text*."""
    ccr_hash = compressed_marker_text.split("<<ccr:", 1)[1].split(">>", 1)[0]
    entry = get_compression_store().retrieve(ccr_hash)
    return entry.original_content if entry else None


def test_offload_fires_and_original_is_byte_exact_recoverable():
    content = _files_payload()
    result = _offload_router().compress(content)

    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    assert result.compression_ratio < 0.1
    # One well-formed JSON array: a signal-aware `_ccr_summary` as the first
    # row, sentinel (with both marker grammars) as the last row.
    rows = json.loads(result.compressed)
    summary = rows[0]["_ccr_summary"]
    assert summary["count"] == 3  # three file rows in the payload
    assert "path" in summary["value_counts"]  # the `path` field is summarized
    assert set(rows[-1]) == {"_ccr_dropped"}
    assert "Retrieve more: hash=" in rows[-1]["_ccr_dropped"]
    # Byte-exact recovery, and the output pins against recompression.
    assert _recover(rows[-1]["_ccr_dropped"]) == content
    assert _looks_like_ccr_output(result.compressed)


def test_offload_fires_on_incompressible_plain_text():
    blob = "\n".join(hashlib.sha256(f"line{i}".encode()).hexdigest() for i in range(200))
    result = ContentRouter().compress(blob)
    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    assert "_ccr_dropped" in result.compressed
    assert _recover(result.compressed) == blob


def test_offload_fires_on_code_benchmark_snapshot_default_config():
    """The committed code@7 snapshot (7 real source files as one JSON tool
    output) previously shipped at 0% reduction; it must offload under the
    DEFAULT config and stay byte-exact recoverable."""
    repo = Path(__file__).resolve().parents[1]
    snap = json.loads((repo / "benchmarks" / "data" / "code.raw.json").read_text(encoding="utf-8"))
    content = json.dumps(json.loads(snap["raw"]), ensure_ascii=False)

    result = ContentRouter().compress(content)
    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    assert result.compression_ratio < 0.2
    assert _recover(json.loads(result.compressed)[-1]["_ccr_dropped"]) == content


def test_offload_object_with_dominant_array_previews_inner_array():
    """A JSON OBJECT with a dominant inner array (Chrome-trace shape) must
    summarize that array — not the object's metadata header — while staying
    byte-exact recoverable. Regression: the head/tail LINES fallback shipped the
    metadata boilerplate and hid the events."""
    content = _trace_payload()
    result = _offload_router().compress(content)

    assert result.strategy_used == CompressionStrategy.CCR_OFFLOAD
    rows = json.loads(result.compressed)
    summary = rows[0]["_ccr_summary"]

    # The summary names the dominant array and keeps the object's OTHER
    # top-level fields WITH their values (review F2) — here "metadata" and its
    # tiny scalar leaves, not just the bare key name.
    assert summary["array"] == "traceEvents"
    assert summary["other_fields"] == {
        "metadata": {
            "enhancedTraceVersion": 1,
            "source": "DevTools",
            "hostDPR": 2.0,
            "sourceMaps": [],
        }
    }
    assert summary["count"] == 200

    # The summary is built from the ACTUAL events, not the metadata header: the
    # event fields appear (in schema/value_counts/examples) and the metadata
    # boilerplate does not. (`name` here is all-unique per the synthetic
    # payload, so `cat`/`ph` carry the categorical histogram.)
    assert {"cat", "ph", "pid", "ts"} <= set(summary["schema"])
    assert "cat" in summary["value_counts"]
    example_rows = list(summary["examples"]["by_value"].values())
    assert example_rows and {"name", "cat", "ph", "ts"} <= set(example_rows[0])
    # Metadata is NOT summarized as if it were events — it never leaks into the
    # event schema. It IS kept (review F2) as the sibling field values, confined
    # to `other_fields`, so the header's scalars stay readable inline.
    assert "enhancedTraceVersion" not in summary["schema"]
    assert summary["other_fields"]["metadata"]["enhancedTraceVersion"] == 1

    # Sentinel + both marker grammars intact; byte-exact recovery preserved.
    assert set(rows[-1]) == {"_ccr_dropped"}
    assert _recover(rows[-1]["_ccr_dropped"]) == content
    assert _looks_like_ccr_output(result.compressed)


def test_offload_store_failure_fails_open(monkeypatch):
    content = _files_payload()
    monkeypatch.setattr(
        type(get_compression_store()),
        "store",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down")),
    )
    result = _offload_router().compress(content)
    # No marker may be emitted when recovery cannot be guaranteed.
    assert result.compressed == content
    assert result.strategy_used != CompressionStrategy.CCR_OFFLOAD


@pytest.mark.parametrize(
    "cfg",
    [
        ContentRouterConfig(ccr_offload_fallback=False, enable_smart_crusher=False),
        ContentRouterConfig(ccr_enabled=False, enable_smart_crusher=False),
    ],
)
def test_offload_respects_disable_flags(cfg):
    result = ContentRouter(cfg).compress(_files_payload())
    assert result.strategy_used != CompressionStrategy.CCR_OFFLOAD
    assert result.compressed == _files_payload()


def test_offload_skips_small_and_already_compressed_content():
    router = _offload_router()
    small = json.dumps([{"a": hashlib.sha256(b"x").hexdigest()}])
    assert router.compress(small).strategy_used != CompressionStrategy.CCR_OFFLOAD

    marked = _files_payload(2) + f"\n[5 items compressed to 2. Retrieve more: hash={'a' * 24}]"
    assert router.compress(marked).strategy_used != CompressionStrategy.CCR_OFFLOAD


# ---------------------------------------------------------------------------
# Strict marker grammar (pin check).
# ---------------------------------------------------------------------------


def test_mentioning_marker_text_is_not_pinned():
    # Placeholder / f-string forms — what documentation and this repo's own
    # source contain. None carries a real hex hash.
    for text in (
        'marker = f" Retrieve more: hash={cache_key}]"',
        "Look for markers like [N items compressed to M. Retrieve more: hash=abc123]",
        "the sentinel shape is <<ccr:HASH N_rows_offloaded>>",
    ):
        assert not _looks_like_ccr_output(text), text


def test_real_markers_are_pinned():
    h24, h12 = "0123456789abcdef01234567", "0123456789ab"
    for text in (
        f"[90 items compressed to 12. Retrieve more: hash={h24}]",
        f"[Read content stale: superseded. Retrieve original: hash={h24}]",
        f"<<ccr:{h12}>>",
        f'{{"_ccr_dropped": "<<ccr:{h24} 78 rows offloaded>>"}}',
    ):
        assert _looks_like_ccr_output(text), text


# ---------------------------------------------------------------------------
# Error protection: raw dumps verbatim, structured JSON compressible.
# ---------------------------------------------------------------------------


def test_raw_traceback_still_counts_as_error_output():
    trace = (
        "Traceback (most recent call last):\n"
        '  File "app.py", line 10, in <module>\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom\n"
        "ERROR: process exited with code 1\n"
    )
    assert _is_unstructured_error_output(trace)


def test_json_rows_mentioning_errors_are_not_error_output():
    rows = json.dumps(
        [
            {"commit": "abc", "subject": "fix(ffi): contain Rust panics - error fail-open"},
            {"commit": "def", "subject": "fix: exception handling failure in traceback path"},
        ]
    )
    assert not _is_unstructured_error_output(rows)
