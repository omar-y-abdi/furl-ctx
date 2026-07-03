"""Strict lossless-or-passthrough mode (``lossless_only``, engine P1-8).

The mode under test: with ``ContentRouterConfig(lossless_only=True)`` only
PROVEN-lossless transforms may change the output —

* JSON arrays are either replaced by a decoder-verifiable, opaque-free
  lossless render (SmartCrusher's compaction tier; every row exactly
  reconstructible via the reference decoder) or passed through untouched.
  Lossy-recoverable candidates are never built: no row drops, no
  ``_ccr_dropped`` sentinels, no opaque-blob substitution, no non-dict
  array sampling — and therefore no CCR store writes.
* The lossy compressor routes (search / log / diff — all drop lines) and
  the CCR-offload fallback resolve to passthrough.
* Output carries NO ``<<ccr:`` pointer of any shape and no
  ``Retrieve …: hash=…`` marker line, because nothing is ever dropped or
  substituted. (The recovery invariant is trivially preserved: it
  constrains drops, and this mode has none.)

Default OFF: ``lossless_only=False`` keeps current behavior byte-for-byte
(pinned by the whole existing suite plus the default asserts here).

Also pinned here (engine P1-8, the ``ccr_inject_marker`` decision): the
log route's ``Retrieve more: hash=…`` line is a RECOVERY POINTER — the
only key a consumer holding the output can use to retrieve the dropped
lines — so it is emitted independently of ``ccr_enabled`` /
``ccr_inject_marker``, exactly like the crusher's ``<<ccr:HASH>>``
pointer (Defect 1). A flag must never turn a drop into an
unreachable-key loss; callers who want marker-free output use
``lossless_only`` instead.
"""

from __future__ import annotations

import json

import pytest

from furl_ctx.cache.compression_store import get_compression_store, reset_compression_store
from furl_ctx.transforms.content_router import ContentRouter, ContentRouterConfig
from furl_ctx.transforms.csv_schema_decoder import decode_csv_schema_rows
from furl_ctx.transforms.router_policy import CompressionStrategy


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_compression_store()
    yield
    reset_compression_store()


# ─── Fixtures: one shape per lossy-tempting route ───────────────────────────


def _build_log() -> str:
    """BUILD_OUTPUT shape: compresses hard under defaults (LogCompressor
    drops the INFO noise and appends the ``Retrieve more`` marker)."""
    return "\n".join(
        [f"INFO test_module::test_case_{i} PASSED" for i in range(120)]
        + ["ERROR test_module::test_boom FAILED", "E   AssertionError: kaboom"]
        + [f"INFO more output line {i}" for i in range(80)]
    )


def _search_results() -> str:
    """SEARCH_RESULTS shape (grep-style ``path:line:content``)."""
    return "\n".join(
        f"src/module_{i % 7}.py:{10 + i}:    def handler_{i}(x): return x" for i in range(60)
    )


def _git_diff() -> str:
    """GIT_DIFF shape, large enough for the diff compressor's CCR path."""
    lines = [
        "diff --git a/src/app.py b/src/app.py",
        "index 1111111..2222222 100644",
        "--- a/src/app.py",
        "+++ b/src/app.py",
        "@@ -1,80 +1,80 @@",
    ]
    for i in range(80):
        lines.append(f" context line {i}")
        if i % 10 == 0:
            lines.append(f"-old code {i}")
            lines.append(f"+new code {i}")
    return "\n".join(lines)


def _droppable_string_array() -> tuple[str, list[str]]:
    """The shape from the producer-driven grammar test: 1000 distinct
    strings, which the default engine row-drops with a ``<<ccr:HASH>>``
    sentinel."""
    items = [f"log-line-{i}-payload" for i in range(1000)]
    return json.dumps(items), items


def _tabular_array() -> tuple[str, list[dict]]:
    """Cleanly tabular rows: the lossless CSV-schema render clears the
    savings gate, so strict mode still compacts — losslessly."""
    items = [{"id": i, "name": f"u_{i}", "status": "ok"} for i in range(50)]
    return json.dumps(items), items


def _opaque_blob_array() -> tuple[str, list[dict]]:
    """Base64-ish CONSTANT blob cells: the default lossless render
    substitutes them with ``<<ccr:HASH,base64,SIZE>>`` pointers; strict
    mode instead folds the constant column into the declaration —
    verbatim, exactly once — a pure lossless render with no pointer."""
    blob = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" * 64
    items = [{"path": f"src/f{i}.py", "content": blob} for i in range(30)]
    return json.dumps(items), items


def _distinct_blob_array() -> tuple[str, list[dict]]:
    """Base64-ish blobs DISTINCT at both ends: no constant fold, no
    affix/head-dict encoding applies, and strict mode forbids the opaque
    substitution — nothing can clear the savings gate, so the array must
    pass through untouched."""
    base = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" * 64
    items = [{"path": f"src/f{i}.py", "content": f"{i:04d}{base}{i:04d}"} for i in range(30)]
    return json.dumps(items), items


def _strict_router() -> ContentRouter:
    return ContentRouter(ContentRouterConfig(lossless_only=True))


def _assert_pointer_free(output: str) -> None:
    assert "<<ccr:" not in output, f"strict mode leaked a CCR pointer: {output[:200]!r}"
    assert "hash=" not in output, f"strict mode leaked a retrieval marker: {output[:200]!r}"


# ─── Defaults: OFF everywhere, current behavior unchanged ───────────────────


def test_lossless_only_defaults_off_on_every_plane() -> None:
    """The flag defaults OFF on the router plane, the Python bridge
    dataclass, and the Rust config — flag-off behavior is the entire
    existing suite's pinned behavior."""
    from furl_ctx._core import SmartCrusherConfig as RustSmartCrusherConfig
    from furl_ctx.transforms.smart_crusher import SmartCrusherConfig

    assert ContentRouterConfig().lossless_only is False
    assert SmartCrusherConfig().lossless_only is False
    assert RustSmartCrusherConfig().lossless_only is False


def test_default_mode_counterfactual_markers_still_emitted() -> None:
    """Counterfactual guard: under the DEFAULT config the fixture shapes DO
    produce recovery markers — proving the strict-mode assertions below
    bite on real behavior, not on inert fixtures."""
    router = ContentRouter()
    str_content, _ = _droppable_string_array()
    assert "<<ccr:" in router.compress(str_content).compressed
    blob_content, _ = _opaque_blob_array()
    assert "<<ccr:" in router.compress(blob_content).compressed
    assert "Retrieve more: hash=" in router.compress(_build_log()).compressed


# ─── Strict mode: no pointers, lossless-or-untouched ────────────────────────


def test_strict_mode_droppable_string_array_untouched() -> None:
    content, items = _droppable_string_array()
    result = _strict_router().compress(content)
    _assert_pointer_free(result.compressed)
    assert json.loads(result.compressed) == items, "every item must survive"


def test_strict_mode_tabular_array_compacts_losslessly() -> None:
    content, items = _tabular_array()
    result = _strict_router().compress(content)
    _assert_pointer_free(result.compressed)
    # The array was substituted by the CSV-schema render (a JSON string in
    # the envelope); the reference decoder must rebuild EVERY row exactly.
    render = json.loads(result.compressed)
    assert isinstance(render, str) and render.startswith("[50]{"), render[:80]
    assert decode_csv_schema_rows(render) == items, "lossless render must round-trip"


def test_strict_mode_constant_blobs_compact_losslessly_pointer_free() -> None:
    """With opaque substitution disabled, the constant blob column folds
    into the declaration — the blob appears verbatim exactly once and the
    reference decoder rebuilds every original row. Under defaults this
    same shape ships ``<<ccr:`` pointers (see the counterfactual test)."""
    content, items = _opaque_blob_array()
    result = _strict_router().compress(content)
    _assert_pointer_free(result.compressed)
    render = json.loads(result.compressed)
    assert isinstance(render, str) and render.startswith("[30]{"), render[:80]
    assert decode_csv_schema_rows(render) == items, "lossless render must round-trip"


def test_strict_mode_distinct_blobs_stay_verbatim() -> None:
    content, items = _distinct_blob_array()
    result = _strict_router().compress(content)
    _assert_pointer_free(result.compressed)
    assert json.loads(result.compressed) == items, "blob cells must stay verbatim"


@pytest.mark.parametrize(
    "content_fn, route",
    [
        pytest.param(_build_log, "log", id="log"),
        pytest.param(_search_results, "search", id="search"),
        pytest.param(_git_diff, "diff", id="diff"),
    ],
)
def test_strict_mode_lossy_text_routes_pass_through_verbatim(content_fn, route) -> None:
    """The search/log/diff compressors all DROP lines, so under strict mode
    their routes resolve to byte-identical passthrough (same shape as
    ``enable_*_compressor=False``)."""
    content = content_fn()
    result = _strict_router().compress(content)
    assert result.compressed == content, f"{route} route must pass through verbatim"
    assert CompressionStrategy.PASSTHROUGH.value in result.strategy_chain


def test_strict_mode_never_writes_ccr_stores() -> None:
    """Nothing is dropped in strict mode, so neither the Rust process store
    nor the Python compression store may gain entries."""
    router = _strict_router()
    for content in (
        _droppable_string_array()[0],
        _opaque_blob_array()[0],
        _distinct_blob_array()[0],
        _tabular_array()[0],
    ):
        router.compress(content)
    crusher = router._get_smart_crusher()
    assert crusher.ccr_len() == 0, "strict mode must not write the Rust CCR store"
    assert get_compression_store().get_stats()["entry_count"] == 0, (
        "strict mode must not write the Python compression store"
    )


def test_strict_mode_disables_ccr_offload() -> None:
    """The reversible offload replaces content with a preview + pointer —
    exactly the (recoverable) visible reduction strict mode forbids. The
    offload-triggering shape (large, uncompressible, SmartCrusher off)
    must pass through instead."""
    import hashlib

    blob = "\n".join(hashlib.sha256(f"line{i}".encode()).hexdigest() for i in range(200))
    # Counterfactual: this exact shape offloads when strict mode is off.
    offloaded = ContentRouter(ContentRouterConfig(enable_smart_crusher=False)).compress(blob)
    assert offloaded.strategy_used == CompressionStrategy.CCR_OFFLOAD

    result = ContentRouter(
        ContentRouterConfig(enable_smart_crusher=False, lossless_only=True)
    ).compress(blob)
    assert result.strategy_used != CompressionStrategy.CCR_OFFLOAD
    assert result.compressed == blob
    _assert_pointer_free(result.compressed)


# ─── ccr_inject_marker decision pin (engine P1-8) ───────────────────────────


def test_log_route_recovery_marker_immune_to_ccr_flags() -> None:
    """The log compressor's ``Retrieve more: hash=…`` line is the ONLY
    retrieval key for the lines it drops — same recovery-pointer class as
    the crusher's ``<<ccr:HASH>>`` sentinel, which
    ``test_ccr_recovery_invariant`` pins as unconditional (Defect 1).

    This test pins the deliberate decision that ``ccr_enabled=False`` /
    ``ccr_inject_marker=False`` do NOT suppress that line on the log
    route: suppressing the marker while still dropping lines would strand
    the persisted original behind an unreachable key (the exact
    refuted-in-benchmarks silent-loss class; see BENCHMARKS.md wave 2 —
    fixed by making pointers unconditional). Marker-free output is what
    ``lossless_only=True`` is for.
    """
    router = ContentRouter(ContentRouterConfig(ccr_enabled=False, ccr_inject_marker=False))
    content = _build_log()
    result = router.compress(content)

    assert "Retrieve more: hash=" in result.compressed, (
        "the log route's recovery pointer must be emitted regardless of the "
        "ccr_enabled/ccr_inject_marker flags (a drop without its pointer is "
        "silent loss)"
    )
    # ...and the pointer must actually resolve: the dropped lines are
    # recoverable from the output alone via the Python compression store.
    marker_hash = result.compressed.split("Retrieve more: hash=", 1)[1].split("]", 1)[0]
    entry = get_compression_store().retrieve(marker_hash)
    assert entry is not None, "the surfaced hash must resolve in the store"
    assert entry.original_content == content, "recovery must be byte-exact"
