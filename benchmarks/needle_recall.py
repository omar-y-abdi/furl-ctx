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

Every trial runs in TWO query arms (EFF-7 / TEST-16a), reported separately
and never blended:

* ``naming``  — the query NAMES the needle's sentinel token. Query-aware
  keep pins matching rows, so this arm is best-case recall BY CONSTRUCTION.
* ``control`` — the query describes the information need WITHOUT any of the
  needle's distinguishing literal tokens (no sentinel name, no marker path,
  no sentinel field values). This is the honest number for a user who needs
  the row but cannot quote it; it is EXPECTED to be lower on the lossy path.

Each trial starts from cold engine state (store + pipeline reset): the
router's result cache is keyed by content, NOT by query, so without the
reset the second arm would be served the FIRST arm's compression — computed
under the other arm's query context — and the A/B would be meaningless.

Reports needle-recall % per arm, broken down by family/position/cardinality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from benchmarks.datasets import build_logs_dataset, search_rows
from benchmarks.metrics import (
    BENCH_MODEL,
    _abort_if_fail_open,
    _decoded_row_signatures,
    _emitted_recovery_hashes,
    _item_in_recovered,
    _item_present,
    _item_signature,
    _output_row_signatures,
    _recovered_originals,
    _reset_engine_state,
    _stringify,
)
from furl_ctx import compress

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
    "lines": "def __FURL_NEEDLE_DO_NOT_DROP__(): return 0xC0FFEE",
}

# LOGS needle — shaped like a git-log row (lossy drop regime: varying-field
# rows force unique hashes -> the engine drops most rows). This is where the
# audited needle-loss is expected to surface.
LOGS_NEEDLE: dict[str, Any] = {
    "commit": "ffffffffffffffffffffffffffffffffffffffff",
    "author": "Needle Author",
    "email": "needle@headroom.invalid",
    "date": "2099-12-31T23:59:59+00:00",
    "subject": "NEEDLE __FURL_NEEDLE_DO_NOT_DROP__ unique commit",
}

# Back-compat alias (the search needle is the default probe).
NEEDLE = SEARCH_NEEDLE

POSITIONS = ("start", "middle", "end")
CARDINALITIES = (30, 90, 300)

# Query arms (EFF-7): "naming" quotes the needle's sentinel token in the
# query; "control" describes the need without ANY of the needle's literal
# tokens. Reported separately — the committed floor gate reads the naming
# number, the control number is the honest non-quoting-user figure.
ARMS = ("naming", "control")


@dataclass(frozen=True)
class NeedleFamily:
    """A base-row source + its matching needle + the two query arms."""

    name: str  # "search" or "logs"
    needle: dict[str, Any]
    query: str  # naming arm: quotes the needle's sentinel token
    control_query: str  # control arm: describes the need, no needle literals

    def base_rows(self, cardinality: int) -> list[dict[str, Any]]:
        if self.name == "search":
            return search_rows(limit=cardinality)[:cardinality]
        if self.name == "logs":
            return build_logs_dataset(limit=cardinality).items[:cardinality]
        raise ValueError(f"unknown needle family: {self.name}")

    def query_for(self, arm: str) -> str:
        if arm == "naming":
            return self.query
        if arm == "control":
            return self.control_query
        raise ValueError(f"unknown needle query arm: {arm!r}")


SEARCH_FAMILY = NeedleFamily(
    name="search",
    needle=SEARCH_NEEDLE,
    query="Find the function named __FURL_NEEDLE_DO_NOT_DROP__.",
    # Describes the need without ANY needle literal (not even sub-tokens of
    # its code line, e.g. "return" — pinned by the routing tests).
    control_query=(
        "One of these search results is a planted probe that does nothing "
        "but yield a fixed constant. Which result is it?"
    ),
)
LOGS_FAMILY = NeedleFamily(
    name="logs",
    needle=LOGS_NEEDLE,
    query="Find the commit with subject __FURL_NEEDLE_DO_NOT_DROP__.",
    # Avoids "commit"/"author"/"needle" — all literal tokens of the needle
    # row's field values.
    control_query=(
        "One of these history entries is dated far in the future. Which "
        "entry is it and what is its subject line?"
    ),
)
FAMILIES = (SEARCH_FAMILY, LOGS_FAMILY)


@dataclass(frozen=True)
class NeedleResult:
    """One needle trial: family, arm, array size, position, recall outcome."""

    family: str
    cardinality: int
    position: str
    recalled: bool
    in_output: bool
    ccr_recoverable: bool
    n_visible_rows: int  # how many distinct rows survived visibly
    arm: str = "naming"  # query arm: "naming" | "control"


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
    arm: str = "naming",
    model: str = BENCH_MODEL,
) -> NeedleResult:
    """Run one needle-injection trial through the real ``compress()``.

    Cold engine state per trial: the router result cache is keyed by
    content, not query, so without the reset the two arms of the same
    (family, cardinality, position) cell would share one compression.
    """
    _reset_engine_state()
    query = family.query_for(arm)
    base = family.base_rows(cardinality)
    # Use exactly ``cardinality`` real base rows; the needle makes it +1.
    array = _inject(base, family.needle, position)
    content = json.dumps(array, ensure_ascii=False)
    messages = [
        {"role": "user", "content": query},
        {"role": "tool", "content": content, "tool_call_id": "needle_call"},
    ]
    result = compress(messages, model=model)
    # A2 / RG2: fail closed, never measure a broken engine. A fail-open returns
    # the ORIGINAL messages, so the needle is trivially present and recall reads
    # a fabricated 100% -- the most flattering possible number from a dead
    # engine. Same contract as the dataset path in ``metrics``.
    _abort_if_fail_open(f"needle:{family.name}@{cardinality}/{position}/{arm}", result)
    output_text = _stringify(result.messages[-1].get("content"))

    row_sigs = _output_row_signatures(output_text)
    decoded_sigs = _decoded_row_signatures(output_text)
    in_output = _item_present(family.needle, output_text, row_sigs, decoded_sigs)

    emitted = _emitted_recovery_hashes(output_text)
    recovered = _recovered_originals(emitted, query)
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
        arm=arm,
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
    arms: tuple[str, ...] = ARMS,
    model: str = BENCH_MODEL,
) -> list[NeedleResult]:
    """Run the full needle grid (arm x family x cardinality x position).

    Deterministic. Covers both regimes: ``search`` (lossless columnar — keeps
    all rows) and ``logs`` (varying-field — lossy drop path), so recall is
    measured where the audited needle-loss is actually expected to surface.
    Both query arms run over the identical injected arrays; only the user
    query differs. Consumers MUST aggregate per arm (``r.arm``) — blending
    naming and control trials into one rate would hide exactly the gap the
    control arm exists to expose.
    """
    results: list[NeedleResult] = []
    for arm in arms:
        for fam in families:
            for n in cardinalities:
                for pos in positions:
                    results.append(_trial(fam, n, pos, arm=arm, model=model))
    return results


def recall_rate(results: list[NeedleResult]) -> float:
    """Needle-recall fraction across the given trials (filter by arm first)."""
    if not results:
        return 1.0
    return sum(1 for r in results if r.recalled) / len(results)
