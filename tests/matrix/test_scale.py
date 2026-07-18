"""MATRIX · scale — a known fixture repeated ~100x, plus a huge flat array.

Contract (``assert_array_distinct_recovery``, mirroring the SET-BASED recovery
invariant of ``test_ccr_recovery_invariant.py``): a top-level JSON array routes to
the documented LOSSY:table row-drop path — a CSV survivor table ships inline and
the dropped rows are offloaded under a ``<<ccr:HASH N_rows_offloaded>>`` pointer.
The guaranteed contract is that EVERY DISTINCT row/scalar is recoverable from
(survivors ∪ retrieved-drop); the row JSON is normalized to compact separators, so
this path is documented-lossy at the WHOLE-INPUT byte level but lossless at the
distinct-item level.

The scale point: at 9,000 rows (~1.6 MB) and a 5,000-element array, compression
completes in sane time (a post-hoc latency ceiling — a TRUE hang would hang the
runner itself, since pytest has no timeout here; this catches finite super-linear
blowups) and retrieval still loses no distinct item.
"""

from __future__ import annotations

import time

from tests._fixtures import log_shaped_rows
from tests.matrix import _matrix as m

# Generous post-hoc ceiling: my local run compresses each of these in well under a
# second. This CANNOT fire on a true hang (the call would never return); it is a
# canary for finite super-linear blowups, generous enough not to flake on a slow
# CI box.
_LATENCY_CEILING_SECONDS = 60.0


def test_known_fixture_repeated_100x_recovers_every_distinct_row() -> None:
    items = log_shaped_rows(90) * 100  # 9,000 rows, 90 distinct
    start = time.monotonic()
    result, recovered = m.assert_array_distinct_recovery(items)
    elapsed = time.monotonic() - start
    assert elapsed < _LATENCY_CEILING_SECONDS, (
        f"100x compression took {elapsed:.1f}s (super-linear blowup?)"
    )
    # 90 distinct rows must all survive the 100x-redundant offload.
    assert len({m.canonical_repr(x) for x in items}) == 90
    assert result.ccr_hashes, "the 100x array must route lossy and offload"


def test_huge_flat_array_recovers_every_distinct_scalar() -> None:
    items = list(range(5000))  # 5,000 distinct scalars
    start = time.monotonic()
    result, recovered = m.assert_array_distinct_recovery(items)
    elapsed = time.monotonic() - start
    assert elapsed < _LATENCY_CEILING_SECONDS, (
        f"flat-array compression took {elapsed:.1f}s (super-linear blowup?)"
    )
    assert result.ccr_hashes, "a 5,000-element array must route lossy and offload"
