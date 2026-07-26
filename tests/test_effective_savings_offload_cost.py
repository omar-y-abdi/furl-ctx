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
module exists to prevent.

The same defect survived one level up in the CALL term until #19: the content
half was fixed to charge the whole payload, while ``call_cost`` stayed
``k * overhead`` — ``k`` SEPARATE retrievals of a blob that has no row index and
therefore cannot be retrieved in parts. One marker is one call at any non-zero
rate, so the charge is ``len(recovered)`` calls, and the row-dropping families
are NOT byte-identical to the pre-fix model: they were overcharged (logs@900 paid
300 calls at r=0.5) exactly as the zero-drop families were undercharged (k=0, no
call at all). Both halves of that are pinned below, including the old formula as
WRONG, so a revert to per-row calls is red rather than merely different.
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
    RETRIEVE_ROUND_TRIP_TOKENS,
    _canonical,
    _decoded_row_sigs,
    _emitted_drop_hashes,
    _recovered_row_sigs,
    _retrieve_originals,
    _stringify,
    _tok,
    effective_savings,
    retrieved_blob_tokens,
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
    # The ESCAPED payload: what the model reads out of the retrieve response,
    # not the raw bytes the library hands back. Strictly larger; pinned against
    # the real handler in tests/test_retrieval_charge_matches_mcp_surface.py.
    payload = retrieved_blob_tokens(blob, tok)
    assert payload > tok.count_text(blob), "escaping is not token-neutral"
    before = 1000
    after = before - payload // 2  # raw saving is HALF the payload a retrieval pays back
    eff = effective_savings(
        tokens_before=before,
        tokens_after=after,
        recovered={"dead" * 6: blob},
        tok=tok,
    )
    one_call = RETRIEVE_ROUND_TRIP_TOKENS  # one blob in `recovered`
    assert eff["0"] == pytest.approx((before - after) / before)  # positive raw reduction
    assert eff["25"] == pytest.approx((before - (after + payload + one_call)) / before)
    assert eff["25"] < eff["0"], "a retrieval must cost something (was flat pre-fix)"
    assert eff["50"] == eff["25"]  # whole blob, one marker -> identical at both rates
    assert eff["25"] < 0.0  # payload > raw saving -> net negative, like code@7 low


def test_row_drop_is_charged_one_call_per_blob_not_one_per_row() -> None:
    """The CALL term for the five row-dropping families, and the old term pinned
    as WRONG so a revert to per-row calls is red.

    Elided rows sit behind ONE ``_ccr_dropped`` marker carrying no row index, so
    the model cannot retrieve a fraction of them: any non-zero rate is the same
    single call returning the same whole payload. The charge is therefore
    ``len(recovered)`` calls at every ``r > 0``, NOT ``k = ceil(r * n_dropped)``.

    The old formula overcharged here — 80 dropped rows priced 20 calls at r=0.25
    and 40 at r=0.50 for retrievals that cannot be issued separately — which is
    the same defect, one level up, as pricing a zero-drop offload at zero calls.
    Both directions are asserted: the new charge exactly, and the old charge as
    strictly more expensive than the truth.
    """
    tok = _tok()
    blob = json.dumps([{"row": i} for i in range(80)])
    before, after, n_dropped = 5000, 400, 80
    recovered = {"beef" * 6: blob}
    eff = effective_savings(
        tokens_before=before,
        tokens_after=after,
        recovered=recovered,
        tok=tok,
    )

    payload = retrieved_blob_tokens(blob, tok)
    one_call = len(recovered) * RETRIEVE_ROUND_TRIP_TOKENS
    assert eff["0"] == pytest.approx((before - after) / before), "r=0 pays nothing"
    for r_key in ("25", "50"):
        expected = (before - (after + payload + one_call)) / before
        assert eff[r_key] == pytest.approx(expected), f"row-drop eff@{r_key} is not one call"
    assert eff["50"] == eff["25"], "one marker, one call: the rate axis cannot separate them"

    # The retired per-row term, reconstructed. It must NOT be what we report.
    for r_key, r in (("25", 0.25), ("50", 0.50)):
        k = math.ceil(r * n_dropped)
        assert k * RETRIEVE_ROUND_TRIP_TOKENS > one_call, "fixture must expose the difference"
        old = (before - (after + payload + k * RETRIEVE_ROUND_TRIP_TOKENS)) / before
        assert eff[r_key] > old, f"eff@{r_key} still charges {k} calls for a single retrieval"


def test_call_term_scales_with_blob_count_not_a_flat_one() -> None:
    """PER BLOB, not per compression. Three offloads are three markers with three
    hashes, so they are three separate calls.

    This case exists because no single-engine fixture can see it. Measured on the
    real harness (largest size, all three tiers): every family offloads exactly
    ONE blob per case except ``multiturn`` (6, every tier) and ``code`` low (2).
    A flat ``call_cost = RETRIEVE_ROUND_TRIP_TOKENS`` would therefore pass all
    three emitter tests below while undercharging multiturn 5 calls per case.
    """
    tok = _tok()
    blobs = {tag * 12: json.dumps([{"row": i, "tag": tag} for i in range(30)]) for tag in "abc"}
    payload = sum(retrieved_blob_tokens(blob, tok) for blob in blobs.values())
    before, after = 5000, 400
    eff = effective_savings(tokens_before=before, tokens_after=after, recovered=blobs, tok=tok)

    calls = len(blobs) * RETRIEVE_ROUND_TRIP_TOKENS
    assert eff["25"] == pytest.approx((before - (after + payload + calls)) / before)
    flat = (before - (after + payload + RETRIEVE_ROUND_TRIP_TOKENS)) / before
    assert eff["25"] < flat, "3 blobs must cost 3 calls, not 1"


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

    def effective_savings(self) -> dict[str, float]:
        return effective_savings(self.tokens_before, self.tokens_after, self.recovered, _tok())

    def charged_at_nonzero_rate(self) -> float:
        """The savings ratio the charge model OWES for this compression: the
        whole recovered payload as ESCAPED in the retrieve response, plus ONE
        round trip per offloaded blob."""
        tok = _tok()
        payload = sum(retrieved_blob_tokens(b, tok) for b in self.recovered.values())
        calls = len(self.recovered) * RETRIEVE_ROUND_TRIP_TOKENS
        return (self.tokens_before - (self.tokens_after + payload + calls)) / self.tokens_before


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
    eff = off.effective_savings()
    assert eff["0"] > 0.90  # raw marker reduction
    assert eff["25"] == pytest.approx(off.charged_at_nonzero_rate())  # payload + one call/blob
    assert eff["25"] < 0.0  # ...that does not survive a retrieval round trip
    assert eff["50"] == eff["25"], "one marker per blob, one call, whatever the rate"


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
      This payload is plain text. (Measured: rendering the SAME data as a JSON
      array of one row per line with the three fields split out —
      ``{"test": "test_module_NN::test_case_M", "status": "PASS", "ms": N}`` —
      is 469 tokens and trips ``router:smart_crusher:0.29`` with one sentinel;
      as plain text, nothing fires. The array form is named here because the
      figure is only reproducible against a stated rendering: the minimal
      ``{"line": "..."}`` form is 344 tokens and also trips it.)
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

    # A zero-drop offload: charged the whole payload plus ONE call at r > 0. The
    # call term is what the pre-#19 `k = ceil(r * 0) = 0` gate also zeroed.
    eff = off.effective_savings()
    assert eff["0"] > 0.0
    assert eff["25"] < eff["0"], "a dedup retrieval must cost something (was flat pre-fix)"
    assert eff["25"] == pytest.approx(off.charged_at_nonzero_rate())
    assert eff["50"] == eff["25"], "whole blob, one marker -> identical at both rates"
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

    # ...so the drop is charged — payload plus ONE call per marker, pinned to the
    # exact figure rather than to an ordering. `dropped` is in the hundreds and
    # `recovered` holds a handful of blobs, so a per-ROW call term would miss this
    # by two orders of magnitude and could not pass as rounding.
    eff = off.effective_savings()
    assert len(dropped) > 10 * len(off.recovered), (
        f"{len(dropped)} rows behind {len(off.recovered)} marker(s) — the fixture must make "
        "per-row and per-blob call terms unmistakably different"
    )
    assert eff["0"] > 0.0
    assert eff["25"] < eff["0"], "a row-drop retrieval must cost the offloaded payload"
    assert eff["25"] == pytest.approx(off.charged_at_nonzero_rate())
    assert eff["50"] == eff["25"], (
        "the rows sit behind one marker with no row index: 25% and 50% are the same call"
    )
