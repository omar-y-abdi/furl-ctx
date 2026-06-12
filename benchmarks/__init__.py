"""Imp3 honest benchmark suite for the Headroom compression engine.

Measures the CURRENT engine on REAL, high-entropy data (no synthetic
low-entropy inputs). Three metrics, reported separately, per dataset:

1. Lossless token reduction — savings on the lossless/passthrough path.
2. Lossy drop ratio — fraction of distinct items removed from the output.
3. Information retention — % of distinct input items present in the output
   OR recoverable from the CCR store (the ``<<ccr:HASH>>`` sentinel).

Plus a needle-recall harness that injects a known unique row at varied
positions/cardinalities and measures how often it survives or is
CCR-recoverable.

Run with::

    .venv/bin/python -m benchmarks.run_bench
"""

from __future__ import annotations

__all__ = ["datasets", "metrics", "needle_recall"]
