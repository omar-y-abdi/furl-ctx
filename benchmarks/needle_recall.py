"""Needle-recall harness: does a known unique row survive compression?

Injects a single, identifiable "needle" object into REAL row arrays at
varied positions (start / middle / end) and cardinalities (arrays of 30 / 90
/ 300 real rows), then measures whether the needle is:

  - present in the visible output, OR
  - recoverable from the CCR store via the ``<<ccr:HASH>>`` sentinel.

Either counts as a recalled needle; otherwise the needle is LOST. The base
rows are real ``rg --json`` search results (high entropy, distinct fields);
only the needle is constructed — and it is a realistic, distinct match row,
NOT a synthetic low-entropy filler. The needle's purpose is to be a *known,
locatable* item so recall is measurable; the surrounding array is 100% real.

Reports needle-recall % overall and broken down by position and cardinality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from benchmarks.datasets import build_logs_dataset, search_rows
from benchmarks.metrics import (
    BENCH_MODEL,
    _decoded_row_signatures,
    _emitted_ccr_hashes,
    _item_in_recovered,
    _item_present,
    _item_signature,
    _output_row_signatures,
    _recovered_originals,
    _stringify,
)
from headroom import compress

# Two KNOWN, unique needle rows — one per base-row family. Each is distinct,
# identifiable, and shaped like a real row of its family (so it enters the
# same code path), but with an unmistakable sentinel value so recall is
# measurable. The needle is the *probe*; the array it is injected into is
# 100% real captured rows.
#
# SEARCH needle — shaped like an rg --json match (lossless columnar regime).
SEARCH_NEEDLE: dict[str, Any] = {
    "path": "NEEDLE/unique_marker_42.py",
    "line_number": 999999,
    "absolute_offset": 424242,
    "lines": "def __HEADROOM_NEEDLE_DO_NOT_DROP__(): return 0xC0FFEE",
}

# LOGS needle — shaped like a git-log row (lossy drop regime: varying-field
# rows force unique hashes -> the engine drops most rows). This is where the
# audited needle-loss is expected to surface.
LOGS_NEEDLE: dict[str, Any] = {
    "commit": "ffffffffffffffffffffffffffffffffffffffff",
    "author": "Needle Author",
    "email": "needle@headroom.invalid",
    "date": "2099-12-31T23:59:59+00:00",
    "subject": "NEEDLE __HEADROOM_NEEDLE_DO_NOT_DROP__ unique commit",
}

# Back-compat alias (the search needle is the default probe).
NEEDLE = SEARCH_NEEDLE

POSITIONS = ("start", "middle", "end")
CARDINALITIES = (30, 90, 300)


@dataclass(frozen=True)
class NeedleFamily:
    """A base-row source + its matching needle + the query to use."""

    name: str  # "search" or "logs"
    needle: dict[str, Any]
    query: str

    def base_rows(self, cardinality: int) -> list[dict[str, Any]]:
        if self.name == "search":
            return search_rows(limit=cardinality)[:cardinality]
        if self.name == "logs":
            return build_logs_dataset(limit=cardinality).items[:cardinality]
        raise ValueError(f"unknown needle family: {self.name}")


SEARCH_FAMILY = NeedleFamily(
    name="search",
    needle=SEARCH_NEEDLE,
    query="Find the function named __HEADROOM_NEEDLE_DO_NOT_DROP__.",
)
LOGS_FAMILY = NeedleFamily(
    name="logs",
    needle=LOGS_NEEDLE,
    query="Find the commit with subject __HEADROOM_NEEDLE_DO_NOT_DROP__.",
)
FAMILIES = (SEARCH_FAMILY, LOGS_FAMILY)


@dataclass(frozen=True)
class NeedleResult:
    """One needle trial: family, array size, position, recall outcome."""

    family: str
    cardinality: int
    position: str
    recalled: bool
    in_output: bool
    ccr_recoverable: bool
    n_visible_rows: int  # how many distinct rows survived visibly


def _inject(
    rows: list[dict[str, Any]], needle: dict[str, Any], position: str
) -> list[dict[str, Any]]:
    """Return a NEW array with ``needle`` inserted at ``position``.

    Immutable: the input ``rows`` list is not mutated.
    """
    if position == "start":
        return [needle, *rows]
    if position == "end":
        return [*rows, needle]
    mid = len(rows) // 2
    return [*rows[:mid], needle, *rows[mid:]]


def _trial(
    family: NeedleFamily,
    cardinality: int,
    position: str,
    *,
    model: str = BENCH_MODEL,
) -> NeedleResult:
    """Run one needle-injection trial through the real ``compress()``."""
    base = family.base_rows(cardinality)
    # Use exactly ``cardinality`` real base rows; the needle makes it +1.
    array = _inject(base, family.needle, position)
    content = json.dumps(array, ensure_ascii=False)
    messages = [
        {"role": "user", "content": family.query},
        {"role": "tool", "content": content, "tool_call_id": "needle_call"},
    ]
    result = compress(messages, model=model)
    output_text = _stringify(result.messages[-1].get("content"))

    row_sigs = _output_row_signatures(output_text)
    decoded_sigs = _decoded_row_signatures(output_text)
    in_output = _item_present(family.needle, output_text, row_sigs, decoded_sigs)

    emitted = _emitted_ccr_hashes(output_text)
    recovered = _recovered_originals(emitted, family.query)
    ccr_recoverable = (not in_output) and _item_in_recovered(family.needle, recovered)

    n_visible = (
        len(row_sigs)
        if row_sigs is not None
        else _count_visible_columnar(output_text, array, decoded_sigs)
    )

    return NeedleResult(
        family=family.name,
        cardinality=cardinality,
        position=position,
        recalled=in_output or ccr_recoverable,
        in_output=in_output,
        ccr_recoverable=ccr_recoverable,
        n_visible_rows=n_visible,
    )


def _count_visible_columnar(
    output_text: str,
    array: list[dict[str, Any]],
    decoded_sigs: set[str] | None,
) -> int:
    """Count distinct array rows present in a columnar rendering.

    Reconstruction-aware: when the rendering decodes (CSV-schema), a row
    counts only if it is EXACTLY reconstructible from the output alone.
    The verbatim-substring scan remains only as the fallback for
    non-decodable text renderings.
    """
    if decoded_sigs is not None:
        return sum(1 for row in array if _item_signature(row) in decoded_sigs)
    count = 0
    for row in array:
        vals = [str(v) for v in row.values()]
        if all(v in output_text for v in vals):
            count += 1
    return count


def run_needle_recall(
    *,
    families: tuple[NeedleFamily, ...] = FAMILIES,
    cardinalities: tuple[int, ...] = CARDINALITIES,
    positions: tuple[str, ...] = POSITIONS,
    model: str = BENCH_MODEL,
) -> list[NeedleResult]:
    """Run the full needle grid (family x cardinality x position).

    Deterministic. Covers both regimes: ``search`` (lossless columnar — keeps
    all rows) and ``logs`` (varying-field — lossy drop path), so recall is
    measured where the audited needle-loss is actually expected to surface.
    """
    results: list[NeedleResult] = []
    for fam in families:
        for n in cardinalities:
            for pos in positions:
                results.append(_trial(fam, n, pos, model=model))
    return results


def recall_rate(results: list[NeedleResult]) -> float:
    """Overall needle-recall fraction across all trials."""
    if not results:
        return 1.0
    return sum(1 for r in results if r.recalled) / len(results)
