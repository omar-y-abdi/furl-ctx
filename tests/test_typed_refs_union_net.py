"""§4.2 R5: typed-refs mirroring + the one-release union safety net.

All six Python mirror sites consume TYPED refs (``dropped_refs`` /
``smart_crush_content_typed`` / ``compact_document_json_typed``) as the
primary recovery path; the legacy text scrape runs alongside for one
release as a UNION net that alarms (``logger.error`` + counter) on any
scrape-only hash — a typed-path bug caught before it can become silent
loss — and still mirrors it best-effort, so recovery never regresses
while the net is up.

Pinned here:

* the normal path never fires the net (counter stays flat, no ERROR);
* a simulated typed-path gap (refs stripped) fires the net loudly AND
  the hash still lands in the Python store (union semantics);
* the scrape's documented false-positive class (payloads QUOTING literal
  ``<<ccr:...>>`` text) fires the net but never raises and never stores
  the fake hash;
* the affix-fold recovery gap is CLOSED: opaque originals whose markers
  the raw-text scrape cannot see (the CSV Affix fold hides the shared
  ``<<ccr:`` prefix in a ``__affix:`` preamble) now reach the Python
  compression_store through the typed refs — pre-§4.2 they were silently
  unmirrorable (see crates/furl-core/tests/typed_dropped_refs.rs).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from furl_ctx.cache import compression_store as cs
from furl_ctx.transforms.smart_crusher import (
    SmartCrusher,
    SmartCrusherConfig,
    get_scrape_only_hash_count,
)

_BLOB_BASE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def _blob(seed: int, repeats: int = 8) -> str:
    return f"{seed}:" + _BLOB_BASE * repeats


def _dict_rows(n: int = 60) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{i:04}",
            "svc": ["api", "worker"][i % 2],
            "lvl": ["INFO", "WARN"][i % 2],
            "msg": f"req {i} done",
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _fresh_python_store() -> Any:
    """Isolate the Python compression_store singleton per test."""
    cs.reset_compression_store()
    yield
    cs.reset_compression_store()


def test_normal_path_mirrors_typed_and_never_fires_the_net(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Row-drop AND opaque refs land in the Python store through the
    typed path; the union net stays silent (counter flat, no ERROR)."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    doc = {"payload": _blob(3), "rows": _dict_rows()}
    before = get_scrape_only_hash_count()

    with caplog.at_level(logging.ERROR, logger="furl_ctx.transforms.smart_crusher"):
        crusher._smart_crush_content(json.dumps(doc, ensure_ascii=False), "q")

    assert get_scrape_only_hash_count() == before, "union net must not fire on the normal path"
    net_errors = [r for r in caplog.records if "union net" in r.getMessage()]
    assert not net_errors, f"unexpected union-net ERRORs: {net_errors}"

    # Every typed ref — row-drop and opaque — is retrievable from the
    # PYTHON store (the store production /v1/retrieve reads).
    _, _, _, refs = crusher._rust.smart_crush_content_typed(
        json.dumps(doc, ensure_ascii=False), "q", 1.0
    )
    kinds = {d.kind_tag for d in refs}
    assert kinds == {"row_drop", "opaque"}, f"fixture must exercise both planes, got {kinds}"
    store = cs.get_compression_store()
    for d in refs:
        entry = store.retrieve(d.hash, record_feedback_signal=False)
        assert entry is not None, f"typed {d.kind_tag} ref {d.hash} missing from the Python store"


class _RefStrippingRust:
    """Delegating proxy that simulates a typed-path BUG: the engine
    renders normally but drops every typed ref on the floor."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def smart_crush_content_typed(self, *args: Any, **kwargs: Any) -> Any:
        crushed, was_modified, info, _refs = self._inner.smart_crush_content_typed(*args, **kwargs)
        return crushed, was_modified, info, []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_union_net_fires_on_typed_gap_and_still_mirrors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ The net's reason to exist: when the typed path LOSES a ref (a
    typed-path bug), the scrape half of the union catches it — counter +
    ``logger.error`` — and the hash is STILL mirrored into the Python
    store, so the bug is loud but never silent loss."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    crusher._rust = _RefStrippingRust(crusher._rust)  # type: ignore[assignment]
    content = json.dumps(_dict_rows(), ensure_ascii=False)
    before = get_scrape_only_hash_count()

    with caplog.at_level(logging.ERROR, logger="furl_ctx.transforms.smart_crusher"):
        crushed, was_modified, _info = crusher._smart_crush_content(content, "q")

    assert was_modified and "<<ccr:" in crushed, "fixture must produce a drop"
    assert get_scrape_only_hash_count() > before, "the net must count the scrape-only hash"
    net_errors = [r for r in caplog.records if "scrape-only hash" in r.getMessage()]
    assert net_errors, "the net must log at ERROR on a scrape-only hash"

    # Union semantics: the hash the typed path lost is still recoverable
    # from the Python store via the scrape half.
    sink: set[str] = set()
    SmartCrusher._collect_ccr_hashes_from_string(crushed, sink)
    row_hashes = [h for h in sink if not h.endswith("#rows")]
    assert row_hashes, "scrape must find the row-drop hash"
    store = cs.get_compression_store()
    for h in row_hashes:
        assert store.retrieve(h, record_feedback_signal=False) is not None, (
            f"union net must best-effort mirror the scrape-only hash {h}"
        )


def test_literal_marker_quote_fires_net_without_raising_or_storing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The documented false-positive class: a payload QUOTING literal
    ``<<ccr:...>>`` text. The scrape collects the fake hash, the typed
    path rightly does not — the net fires (counter + ERROR) but must not
    raise, and the fake hash never lands in the Python store (the
    best-effort mirror misses it in the Rust store and debug-skips,
    exactly the pre-§4.2 behavior)."""
    planted_hash = "aaaaaaaaaaaa"
    planted = f"<<ccr:{planted_hash},base64,2.0KB>>"
    items = [{"id": i, "note": f"saw {planted} in output"} for i in range(40)]
    crusher = SmartCrusher(config=SmartCrusherConfig())
    before = get_scrape_only_hash_count()

    with caplog.at_level(logging.ERROR, logger="furl_ctx.transforms.smart_crusher"):
        crushed, _was_modified, _info = crusher._smart_crush_content(
            json.dumps(items, ensure_ascii=False), "q"
        )

    assert planted in crushed, "the planted literal must survive into the output"
    assert get_scrape_only_hash_count() > before, "the net counts the false positive"
    assert any(planted_hash in r.getMessage() for r in caplog.records), (
        "the net must name the scrape-only hash in its ERROR"
    )
    store = cs.get_compression_store()
    assert store.retrieve(planted_hash, record_feedback_signal=False) is None, (
        "a fake hash the engine never emitted must not be fabricated into the store"
    )


def test_affix_folded_opaque_refs_reach_python_store() -> None:
    """★ The recovery gap the typed path CLOSES: uniform-size blobs make
    the CSV Affix fold hide every ``<<ccr:`` marker from the raw-text
    scrape (shared prefix folded into a ``__affix:`` preamble), so the
    pre-§4.2 mirror never bridged those originals to the Python store.
    The typed refs carry them regardless of rendering."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    # Same-length blobs → identical humanized size → shared marker prefix
    # AND suffix → the affix fold fires (pinned in the Rust suite).
    doc = {"rows": [{"id": i, "blob": _blob(i + 50)} for i in range(12)]}
    compacted = crusher.compact_document_json(json.dumps(doc, ensure_ascii=False))

    _compacted2, refs = crusher._rust.compact_document_json_typed(
        json.dumps(doc, ensure_ascii=False)
    )
    opaque = [d for d in refs if d.kind_tag == "opaque"]
    assert len(opaque) == 12, "fixture must substitute all 12 blobs"

    # The fold hid the markers from the scrape...
    scraped: set[str] = set()
    SmartCrusher._collect_opaque_ccr_hashes_from_string(compacted, scraped)
    assert not scraped, (
        "fixture must exercise the affix fold (no verbatim opaque marker "
        "in the render); if this fails the fold stopped firing"
    )
    # ...but every original is in the PYTHON store via the typed refs.
    store = cs.get_compression_store()
    for d in opaque:
        entry = store.retrieve(d.hash, record_feedback_signal=False)
        assert entry is not None, (
            f"affix-folded opaque original {d.hash} must be mirrored typed "
            f"(pre-§4.2 this was silently unmirrorable)"
        )


def test_crush_array_json_mirrors_row_index_chunks_typed() -> None:
    """The sibling's granular row index now mirrors TYPED (bare
    ``row_index_key`` from the dict, no marker re-parsing): each per-row
    chunk is retrievable from the Python store, so /v1/retrieve stays
    PROPORTIONAL."""
    crusher = SmartCrusher(config=SmartCrusherConfig())
    items = [
        {
            "id": f"{i:04}",
            "svc": ["api", "worker"][i % 2],
            "note": f"row {i}",
        }
        for i in range(60)
    ]
    result = crusher.crush_array_json(json.dumps(items, ensure_ascii=False))
    index_key = result["row_index_key"]
    assert index_key, "fixture must produce a granular row index"

    index_raw = crusher.ccr_get(index_key)
    assert index_raw is not None, "row index must be in the Rust store"
    row_hashes = json.loads(index_raw)
    assert row_hashes, "index must hold per-row chunk hashes"
    store = cs.get_compression_store()
    for row_hash in row_hashes:
        assert store.retrieve(row_hash, record_feedback_signal=False) is not None, (
            f"per-row chunk {row_hash} must be mirrored into the Python store"
        )
