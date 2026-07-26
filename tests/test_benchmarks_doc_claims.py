"""Transcription guard for documented benchmark numbers.

The repo already guards number GENERATION — ``test_benchmarks_fail_closed.py``
aborts rather than writing fictional numbers, ``perf.yml`` pins verify.run's
counters exactly, and ``benchmarks/compare_baseline.py`` enforces the floor — and
it guards doc STRUCTURE (``test_llms_txt_anchors.py`` for links,
``test_readme_claims.py`` for claim ordering). Nothing guarded TRANSCRIPTION: a
figure hand-copied out of a run into prose kept no link back to the artifact it
came from, so nothing could tell that the copy had gone stale.

That gap is why four documented figures drifted or became unverifiable at once.
Two of them (the ``66%`` tier-table cell, the ``91.5%`` headline) turned out
BONA FIDE on inspection — they had not become false, they had become
uncheckable. The other two (``98.9%`` for ``code@7``, ``86.5%`` for
``multiturn@135``) were exactly right when written against
``benchmarks/baseline_results.json`` at 4b820c72 and silently drifted afterwards.

These checks close the subset that COMMITTED artifacts can answer, with no
benchmark sweep, no network and no CI change (``tests/**`` is already in the
workflow path filter). Figures sourced from ``verify/run.py`` are deliberately
NOT covered: ``verify/raw_results.json`` is gitignored, so there is nothing
committed to check them against, and regenerating it is a ~10-minute
216-subprocess sweep. Pretending otherwise would be coverage theatre.

DATED RECORDS ARE EXEMPT BY DESIGN. Every "Phase-N" section is a historical
record that must NOT be re-derived, and the tier table's ``66%`` cell is a dated
verify measurement anchored to its cell and date in the text. Only figures that
present themselves as CURRENT are checked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BASELINE_JSON = _ROOT / "benchmarks" / "baseline_results.json"
_BASELINE_MD = _ROOT / "benchmarks" / "BASELINE.md"
_BENCHMARKS = _ROOT / "BENCHMARKS.md"
_LLMS_TXT = _ROOT / "site" / "llms.txt"

# tokens_after is an integer count, so an exact match is the right bar.
# Percentages are printed to 2dp, so allow half a unit in the last place.
_PCT_TOL = 0.005

_MD_ROW = re.compile(r"\|\s*([\w@]+)\s*\|\s*\d+\s*\|\s*\d+\s*\|\s*(\d+)\s*\|")


def _datasets() -> dict[str, dict]:
    data = json.loads(_BASELINE_JSON.read_text(encoding="utf-8"))
    return {row["name"]: row for row in data["datasets"]}


def _flatten_blockquote(text: str) -> str:
    """Join blockquote continuation lines so a claim that wraps mid-sentence
    still matches as one string."""
    return re.sub(r"\n>\s*", " ", text)


def _lossless_pct(name: str) -> float:
    return float(_datasets()[name]["lossless_reduction"]) * 100.0


def test_baseline_markdown_agrees_with_the_committed_floor() -> None:
    """C1 — the two committed baseline artifacts must not disagree.

    ``benchmarks/BASELINE.md`` is the human-readable companion of
    ``baseline_results.json`` and BENCHMARKS.md points at it as the "current
    capture". They diverged for four datasets between #164 (which re-floored the
    JSON without regenerating the Markdown) and the re-baseline that fixed it.
    """
    rows = _datasets()
    md = _BASELINE_MD.read_text(encoding="utf-8")
    compared = 0
    mismatched: list[str] = []
    for match in _MD_ROW.finditer(md):
        name, tokens_after = match.group(1), int(match.group(2))
        if name not in rows:
            continue
        compared += 1
        if rows[name]["tokens_after"] != tokens_after:
            mismatched.append(
                f"{name}: BASELINE.md says tokens_after={tokens_after}, "
                f"baseline_results.json says {rows[name]['tokens_after']}"
            )
    assert compared >= 10, f"only matched {compared} dataset rows in BASELINE.md — parser drifted"
    assert not mismatched, "the two committed baseline artifacts disagree:\n  " + "\n  ".join(
        mismatched
    )


def test_headline_figures_match_the_site_copy() -> None:
    """C2 — the three public headline numbers are stated in two committed places
    (BENCHMARKS.md and site/llms.txt) and must not drift apart."""
    bm = _BENCHMARKS.read_text(encoding="utf-8")
    llms = _LLMS_TXT.read_text(encoding="utf-8")
    for figure in ("22,630", "1,922", "91.5"):
        assert figure in bm, f"BENCHMARKS.md lost the headline figure {figure!r}"
        assert figure in llms, f"site/llms.txt lost the headline figure {figure!r}"
    # ...and the ratio the two of them assert must actually be that ratio.
    assert abs((1 - 1922 / 22630) * 100 - 91.5) < 0.05, "the 91.5% headline no longer follows"


def test_current_capture_note_matches_the_committed_baseline() -> None:
    """C3 — the "Update (CCR-offload fallback)" note presents itself as CURRENT
    (it is the block that labels everything else dated), so its figures must
    still equal the committed floor they were derived from.

    Both figures were exactly right at 4b820c72 (code@7 98.85%, multiturn@135
    86.49%) and drifted silently afterwards. NOTE the fixture: these are the
    BENCHMARKS harness's ``code.raw.json`` blob, NOT verify's ``gen_code`` —
    same dataset name, different fixture.
    """
    text = _flatten_blockquote(_BENCHMARKS.read_text(encoding="utf-8"))

    code = re.search(r"`code@7`.*?offloads to the CCR store \(\*\*([\d.]+)%\*\*", text)
    assert code, "the update note's code@7 figure is no longer parseable"
    claimed = float(code.group(1))
    actual = _lossless_pct("code@7")
    assert abs(claimed - actual) <= _PCT_TOL, (
        f"BENCHMARKS.md claims code@7 is {claimed}% but the committed baseline "
        f"says {actual:.2f}% — the 'current capture' note has gone stale"
    )

    multi = re.search(r"`multiturn@135` rose [\d.]+% → \*\*([\d.]+)%\*\*", text)
    assert multi, "the update note's multiturn@135 figure is no longer parseable"
    claimed = float(multi.group(1))
    actual = _lossless_pct("multiturn@135")
    assert abs(claimed - actual) <= _PCT_TOL, (
        f"BENCHMARKS.md claims multiturn@135 is {claimed}% but the committed "
        f"baseline says {actual:.2f}% — the 'current capture' note has gone stale"
    )
