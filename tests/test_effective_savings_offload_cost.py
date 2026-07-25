"""Honest retrieval cost for offloads, and the sentinel invariant it rests on.

``verify/measure.py::effective_savings`` used to gate the retrieval charge on
``k = ceil(r * n_dropped_rows)``. A family that offloads content but drops no
rows has ``k = 0`` at every rate, so it was charged nothing and reported
effective savings equal to raw reduction at 25% and 50% — a retrieval that
actually pays back a full payload priced at zero. verify's `code` family hits
this via cross-message DEDUP at the low tier (byte-identical files): the deduped
blob is in ``recovered`` but ``n_dropped_rows`` is 0.

The fix charges the whole ``recovered`` payload at any ``r > 0``. That single
charge is COMPLETE only because of one invariant: EVERY offload path ships a
``{"_ccr_dropped": "<<ccr:HASH>>"}`` sentinel, so ``recovered`` catches all of
them. ``_ccr_dropped`` is a CROSS-MODULE contract, not one function's private
detail — three independent emitters construct it, one per offload path:

* row-drop (Rust)     — ``crates/furl-core/src/transforms/smart_crusher/
  persist.rs::ccr_sentinel_map``
* cross-message dedup — ``furl_ctx/transforms/cross_message_dedup.py::
  duplicate_sentinel`` and ``::near_duplicate_rendering``
* opaque whole-blob   — ``furl_ctx/transforms/router_engine.py::_ccr_offload``

and it is read back by ``furl_ctx/transforms/smart_crusher.py`` and
``furl_ctx/transforms/csv_schema_decoder.py``.

This module pins ALL THREE emitters with REAL ``compress()`` calls, each on a
fixture that isolates one path (asserted, not assumed). Break any one of them so
it ships a bare ``<<ccr:HASH>>`` marker with no sentinel and the corresponding
test goes RED: the elided payload drops out of ``recovered``, the charge silently
falls back to zero, and the family reads flat again — precisely the defect this
module exists to prevent. Row-DROPPING families are otherwise unaffected by the
fix: for them ``k > 0`` iff ``r > 0``, so the numbers are byte-identical to the
pre-fix model — also pinned below.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("tiktoken")

from furl_ctx import compress
from furl_ctx.cache.compression_store import reset_compression_store
from furl_ctx.transforms.cross_message_dedup import MIN_DEDUP_CHARS, _content_hash
from furl_ctx.transforms.router_engine import _OFFLOAD_MIN_CHARS
from verify.measure import (
    RETRIEVE_CALL_OVERHEAD_TOKENS,
    _canonical,
    _decoded_row_sigs,
    _emitted_drop_hashes,
    _recovered_row_sigs,
    _retrieve_originals,
    _stringify,
    _tok,
    effective_savings,
)

MODEL = "gpt-4o"


# --------------------------------------------------------------------------- #
# The cost model itself (no engine involved).
# --------------------------------------------------------------------------- #


def test_dedup_zero_drop_offload_no_longer_reads_flat() -> None:
    """A cross-message dedup offload (payload in ``recovered``) with zero
    dropped rows must decay with retrieval instead of reporting raw reduction
    flat across rates — and go NEGATIVE when the retrieved payload exceeds the
    raw saving, exactly the shape of verify's ``code@7 low`` cell (+22.0% ->
    -1.5%). Pre-fix eff@25 == eff@0 (the bug); now eff@25 < eff@0."""
    tok = _tok()
    blob = json.dumps([{"i": i, "body": "line " * 20} for i in range(20)])
    payload = tok.count_text(blob)
    before = 1000
    after = before - payload // 2  # raw saving is HALF the payload a retrieval pays back
    eff = effective_savings(
        tokens_before=before,
        tokens_after=after,
        recovered={"dead" * 6: blob},
        tok=tok,
        n_dropped_rows=0,  # dedup drops no rows
    )
    assert eff["0"] == pytest.approx((before - after) / before)  # positive raw reduction
    assert eff["25"] == pytest.approx((before - (after + payload)) / before)
    assert eff["25"] < eff["0"], "a retrieval must cost something (was flat pre-fix)"
    assert eff["50"] == eff["25"]  # whole blob, no rows -> identical at both rates
    assert eff["25"] < 0.0  # payload > raw saving -> net negative, like code@7 low


def test_row_drop_path_is_byte_identical_to_pre_fix_model() -> None:
    """Invariance guard for the five row-dropping families: with ``n_dropped_rows
    > 0`` the charge equals the pre-fix formula exactly (whole recovered payload
    once + ``k`` call overheads). This is what keeps logs/search/repeated_logs/
    multiturn/disk from moving — the change is confined to the zero-drop case."""
    tok = _tok()
    blob = json.dumps([{"row": i} for i in range(80)])
    before, after, n_dropped = 5000, 400, 80
    recovered = {"beef" * 6: blob}
    eff = effective_savings(
        tokens_before=before,
        tokens_after=after,
        recovered=recovered,
        tok=tok,
        n_dropped_rows=n_dropped,
    )

    payload = tok.count_text(blob)
    for r_key, r in (("0", 0.0), ("25", 0.25), ("50", 0.50)):
        k = math.ceil(r * n_dropped)
        content = payload if k > 0 else 0  # the pre-fix `k > 0` gate
        call = k * RETRIEVE_CALL_OVERHEAD_TOKENS
        expected = (before - (after + content + call)) / before
        assert eff[r_key] == pytest.approx(expected), f"row-drop eff@{r_key} moved"


# --------------------------------------------------------------------------- #
# The sentinel invariant, once per emitter, through the REAL engine.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Offloaded:
    """One real ``compress()`` call, viewed exactly as the harness views it."""

    result: Any
    sentinelled: frozenset[str]
    recovered: dict[str, str]
    tokens_before: int
    tokens_after: int

    @property
    def transforms(self) -> tuple[str, ...]:
        return tuple(self.result.transforms_applied)

    def effective_savings(self, n_dropped_rows: int) -> dict[str, float]:
        return effective_savings(
            self.tokens_before,
            self.tokens_after,
            self.recovered,
            _tok(),
            n_dropped_rows=n_dropped_rows,
        )


def _compress_and_recover(messages: list[dict[str, Any]], query: str) -> _Offloaded:
    """Compress on a COLD CCR store and rebuild the harness's retrieval view.

    Deliberately identical to ``verify/measure.py``: hashes come from
    ``_emitted_drop_hashes`` (the ``_ccr_dropped`` SENTINEL grammar — a bare
    ``<<ccr:HASH>>`` marker in the output does NOT count) and are resolved
    through the engine's own CCR store. That is what makes a missing sentinel
    observable here: it empties ``recovered`` and zeroes the retrieval charge.
    """
    reset_compression_store()
    result = compress(messages, model=MODEL)
    tok = _tok()
    sentinelled: set[str] = set()
    for message in result.messages:
        sentinelled |= _emitted_drop_hashes(_stringify(message.get("content")))
    return _Offloaded(
        result=result,
        sentinelled=frozenset(sentinelled),
        recovered=_retrieve_originals(sentinelled, query),
        tokens_before=result.tokens_before or tok.count_messages(messages),
        tokens_after=result.tokens_after or tok.count_messages(result.messages),
    )


@pytest.mark.slow
def test_opaque_whole_blob_emitter_is_sentinelled_and_charged() -> None:
    """EMITTER 1/3 — ``router_engine._ccr_offload`` (Python, opaque whole blob).

    The committed opaque code fixture is the canonical whole-blob offload — the
    exact case the retired ``code_roundtrip.py`` measured. RED if that offload
    ever ships a bare marker: ``opaque_offloads`` still reports it (the engine
    surfaces the marker), but the hash falls out of ``sentinelled`` and out of
    ``recovered``, so the payload would be priced at zero.
    """
    repo = Path(__file__).resolve().parents[1]
    snap = json.loads((repo / "benchmarks" / "data" / "code.raw.json").read_text(encoding="utf-8"))
    query = "Review these source files for issues."
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": json.dumps(json.loads(snap["raw"]), ensure_ascii=False)},
    ]

    off = _compress_and_recover(messages, query)

    opaque_hashes = {o.hash for o in off.result.opaque_offloads}
    assert opaque_hashes, "the code fixture must exercise an opaque whole-blob offload"

    # THE INVARIANT: an offload the engine reports is sentinel-backed and thus
    # retrievable via `recovered`. A bare-marker offload would break this.
    assert opaque_hashes <= off.sentinelled, (
        "an offload shipped without a _ccr_dropped sentinel — recovered would miss it "
        f"(offloaded but not sentinelled: {opaque_hashes - off.sentinelled})"
    )
    assert opaque_hashes <= set(off.recovered), "sentinelled offload must resolve from the store"

    # ...so the `recovered`-only charge prices the blob honestly: net-negative,
    # corroborating the removed harness's -4.1% (see BENCHMARKS.md).
    eff = off.effective_savings(n_dropped_rows=0)
    assert eff["0"] > 0.90  # raw marker reduction
    assert eff["25"] < 0.0  # ...that does not survive a retrieval round trip
    assert eff["50"] < 0.0


def test_cross_message_dedup_emitter_is_sentinelled_and_charged() -> None:
    """EMITTER 2/3 — ``cross_message_dedup.duplicate_sentinel`` (Python).

    This is the emitter behind the ``code`` cell in BENCHMARKS.md, so it is the
    one the corrected headline number actually depends on. The dedup sentinel
    must therefore be the ONLY one in the output: a bare marker here leaves
    ``sentinelled`` EMPTY and the family reads flat again, and a SECOND emitter
    firing on the same fixture would refill ``recovered`` and hide exactly that.

    Two measured facts isolate it, and neither is the router's
    ``min_tokens_to_compress`` gate — the payload is 252 tokens, two tokens
    ABOVE that 250 gate at every real tokenizer, so the router does run here and
    simply finds nothing to apply:

    * smart_crusher is excluded STRUCTURALLY: it needs row/array-shaped content.
      This payload is plain text. (Measured: the same data as a JSON array trips
      ``router:smart_crusher`` at 379 tokens; as plain text, nothing fires.)
    * ``_ccr_offload`` is excluded by a CHARACTER floor, ``_OFFLOAD_MIN_CHARS``
      (4000). This payload is 733 chars — 18% of the floor, a 5.5x margin.
      Characters are tokenizer-independent, so no tokenizer or model change can
      erode it. (Measured: plain text stays clean to 3265 chars and first trips
      ``router:ccr_offload`` at 4083 — so OVERSIZING this fixture walks into the
      opaque emitter, NOT into smart_crusher.)

    Both bounds are asserted below rather than left as prose, so a fixture that
    drifts into another emitter's territory fails RED instead of silently
    re-hiding the defect this test exists to catch.
    """
    payload = "\n".join(
        f"PASS test_module_{i:02d}::test_case_{i % 7} ({(i * 37) % 900}ms)" for i in range(18)
    )
    assert len(payload) >= MIN_DEDUP_CHARS, "too small to dedup"
    assert len(payload) < _OFFLOAD_MIN_CHARS, "large enough to trip the opaque emitter too"
    query = "Run the test suite."
    # The duplicate sits at index 3; the trailing turns keep it OUTSIDE the
    # default `protect_recent` window (4), which dedup never replaces into.
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": payload, "tool_call_id": "t1"},
        {"role": "user", "content": "Run it again to confirm."},
        {"role": "tool", "content": payload, "tool_call_id": "t2"},
        {"role": "assistant", "content": "Both runs report identical output."},
        {"role": "user", "content": "Any flakes in the second run?"},
        {"role": "assistant", "content": "None — the outputs are byte-identical."},
        {"role": "user", "content": "Good, wrap up."},
    ]

    off = _compress_and_recover(messages, query)

    assert any(t.startswith("cross_message_dedup:exact") for t in off.transforms), off.transforms
    assert not off.result.opaque_offloads, "fixture must isolate dedup: no opaque offload here"

    # The bytes ARE gone from the later message...
    assert payload not in _stringify(off.result.messages[3].get("content"))
    # ...and the sentinel is the only thing that makes them retrievable.
    dedup_hash = _content_hash(payload)
    assert off.sentinelled == {dedup_hash}, (
        "the elided duplicate must be the one and only _ccr_dropped sentinel here "
        f"(got {sorted(off.sentinelled)})"
    )
    assert off.recovered.get(dedup_hash) == payload, "sentinel must resolve byte-exact"

    # A zero-drop offload: charged the whole payload at r > 0, flat pre-fix.
    eff = off.effective_savings(n_dropped_rows=0)
    assert eff["0"] > 0.0
    assert eff["25"] < eff["0"], "a dedup retrieval must cost something (was flat pre-fix)"
    assert eff["50"] == eff["25"], "whole blob, no rows -> identical at both rates"
    assert eff["25"] < 0.0, "payload outweighs the raw saving, like code@7 low"


@pytest.mark.slow
def test_rust_row_drop_emitter_is_sentinelled_and_charged() -> None:
    """EMITTER 3/3 — ``persist.rs::ccr_sentinel_map`` (Rust, smart_crusher).

    Row drop is where a missing sentinel is worst: the rows are gone from the
    visible output, so without the sentinel they are neither readable NOR
    retrievable — silent loss, and the retrieval charge collapses to zero, which
    INFLATES the five row-drop families' effective savings. RED on both counts
    if the Rust emitter ever ships a bare marker.
    """
    rows = [
        {
            "id": i,
            "ts": f"2025-03-{(i % 28) + 1:02d}T{i % 24:02d}:{i % 60:02d}:{(i * 7) % 60:02d}Z",
            "level": ["INFO", "WARN", "ERROR"][i % 3],
            "service": f"svc-{i:04x}",
            "commit": f"{i:040x}",
            "message": f"request {i} finished with code {(i * 31) % 599} after {(i * 17) % 9999}ms",
        }
        for i in range(900)
    ]
    query = "Find ERROR and WARN log entries and summarize failures"
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": json.dumps(rows, ensure_ascii=False)},
    ]

    off = _compress_and_recover(messages, query)

    assert any(t.startswith("router:smart_crusher") for t in off.transforms), off.transforms
    assert not off.result.opaque_offloads, "fixture must isolate the row-drop path"

    decoded = _decoded_row_sigs(_stringify(off.result.messages[-1].get("content"))) or set()
    dropped = [row for row in rows if _canonical(row) not in decoded]
    assert dropped, "the fixture must actually drop rows"

    # THE INVARIANT: every elided row is recoverable THROUGH THE SENTINEL. A bare
    # marker empties `recovered`, so this fails before the cost assertions do.
    assert off.sentinelled, "the row drop shipped without a _ccr_dropped sentinel"
    recovered_sigs = _recovered_row_sigs(off.recovered)
    unrecoverable = [row for row in dropped if _canonical(row) not in recovered_sigs]
    assert not unrecoverable, f"{len(unrecoverable)} dropped rows are not sentinel-recoverable"

    # ...so the drop is charged. Unlike the zero-drop cases, k scales with the
    # rate here, so 50% costs strictly more call overhead than 25%.
    eff = off.effective_savings(n_dropped_rows=len(dropped))
    assert eff["0"] > 0.0
    assert eff["25"] < eff["0"], "a row-drop retrieval must cost the offloaded payload"
    assert eff["50"] < eff["25"], "k = ceil(r * n_dropped) call overheads scale with the rate"
