"""Floor-drift guard: keep the committed floor describing reality.

``compare_baseline.py`` answers "did this candidate REGRESS against the floor".
It cannot answer "is the floor still the right floor", and that second question
is where the gate silently rots. After PR #172 the engine got cheaper on five
datasets, the committed floor stayed where it was, and the gate kept passing —
printing five IMPROVED lines that were read as noise through a whole night of
runs. A floor sitting above reality is silent regression headroom: at that
point ``repeated_logs@90`` could have inflated 141 -> 174 tokens, a 23.4%
regression, and still passed.

WHAT THIS CANNOT DO, STATED PLAINLY. A legitimate improvement and unintentional
drift are BYTE-IDENTICAL in the artifacts — both are simply a candidate whose
tokens sit below the committed floor. Intent is not present in the data, and no
amount of parsing recovers it. This guard therefore does not try to judge WHY
the floor stopped matching. It only refuses to let the question go unasked, and
it refuses to let the answer be recorded dishonestly.

TWO CHECKS, AND THEY ONLY WORK TOGETHER.

``DRIFT`` — the candidate sits more than ``TOKEN_SLACK`` BELOW the PR's
committed floor. ``TOKEN_SLACK`` is the inflation the gate already forgives, so
a floor more than that above reality has silently DOUBLED the tolerance without
anyone deciding to. The remedy is to regenerate the floor.

``DIRECTION`` — no floor may move in the LOOSENING direction inside a PR that
changes anything else. For token budgets that direction is UP; recall regimes
invert it, see below. This is what makes the DRIFT remedy safe to recommend. Regenerating the
floor is a WHOLESALE act: ``run_bench`` rewrites every dataset to the candidate's
value, including any dataset that got WORSE. Because the gate compares the
candidate against the PR's OWN committed floor, a wholesale regenerate makes
that comparison self-referential — a PR that improves one dataset and regresses
another passes cleanly with ``failures=0 improvements=0``, the regression fully
masked. DIRECTION closes that: the masked regression shows up as an upward floor
move and fails here. So you MAY regenerate to record an improvement, and you
CANNOT regenerate to hide a regression.

An upward move is a real thing that sometimes has to happen — a deliberate,
argued loosening. It is allowed only in a BASELINE-ONLY PR (one that touches
nothing but the two baseline artifacts), so it is reviewed as itself rather than
riding along inside feature work. That is the same discipline that separates
#182, a standalone re-floor of five downward moves, from #164, which raised four
floors inside a PR about something else and left ``BASELINE.md`` stale behind it.

ENTRIES THAT EXIST ON ONLY ONE SIDE — decided deliberately, because "add a new
one" and "retire an old one" are the obvious shapes for walking past a
per-entry comparison:

* PRESENT IN THE PR'S FLOOR, ABSENT FROM MAIN'S (a new entry) — DIRECTION is
  silent, because there is no previous floor for it to have moved FROM. It is
  not unguarded: DRIFT still compares that new floor against what the entry
  actually measures, so a first floor set far off its own output fails there.
* PRESENT IN MAIN'S FLOOR, ABSENT FROM THE PR'S (a retired entry) — treated
  exactly like a loosening move, because it is a stronger one: it does not move
  the ceiling, it deletes it. ``compare_baseline`` is blind here for the same
  self-referential reason as the wholesale regenerate — it pairs the candidate
  against the PR's own floor, so once an entry is gone from both, the two sides
  agree and no finding is drawn. Retiring therefore requires the same standalone
  PR that loosening a floor does.

THREE FLOORED QUANTITIES, NOT ONE, AND THE LOOSENING DIRECTION INVERTS.
``compare_baseline`` gates exactly three things, and this guard covers exactly
those three — a bounded claim, checkable by reading ``_compare_one_dataset`` and
``_compare_one_regime`` next to ``FloorSnapshot``:

* ``tokens_after`` per dataset — bigger is WORSE, so a LOOSENING moves the floor
  UP and DRIFT is a floor sitting ABOVE what the run measures. The gate forgives
  ``TOKEN_SLACK``, so that is the threshold.
* ``information_retention`` per dataset — bigger is BETTER, so a LOOSENING moves
  the floor DOWN and DRIFT is a floor sitting BELOW reality.
* recall per regime, from ``needle_recall`` — same shape as retention.

The last two forgive nothing: ``compare_baseline`` compares both to within
``_EPS``, so unlike tokens there is no slack to spend and any gap at all is
headroom. They share one implementation (``_higher_is_better``) because a second
hand-written copy is exactly how the halves came to disagree before.

EACH OF THE TWO EXTENSIONS WAS MEASURED BLIND FIRST, NOT ASSUMED. Retire a recall
family from both the harness and the floor and ``compare_baseline`` prints
``regimes=8 failures=0 / RESULT: no regressions``, exit 0. Lower one dataset's
``information_retention`` floor from 1.00 to 0.60 in both the floor and the
candidate and both tools exit 0 again — a 40% loss of retained information,
laundered. Guarding only the token half would have been the failure this module
exists to catch, one level down.

NONE OF IT HAS EVER BEEN USED. Across all ten commits that have touched the file
(119af145 -> d8e48d73) the dataset set grew 3 -> 6 -> 10 and the regime set
6 -> 12, monotonically, zero removals, and the file has never been renamed. Not
even #164 — the one commit DIRECTION retroactively reddens — dropped an entry.
This closes latent holes, not live ones.

WHAT IT STILL CANNOT SEE, AFTER ALL OF THE ABOVE:

* A BASELINE-ONLY PR IS UNPOLICED BY DIRECTION, BY DESIGN. That PR may raise
  every token floor, lower every retention and recall floor, and delete every
  dataset and regime, and this guard passes it. The whole enforcement there is
  a human reading a diff that contains nothing else. That is the deliberate
  trade — see ``is_baseline_only``, which is a heuristic and says so — but it
  means the guard's real guarantee is "a loosening cannot ride along unnoticed",
  never "a loosening cannot happen".
* COVERAGE THAT WAS NEVER FLOORED IS INVISIBLE. This compares floors that exist.
  A code path nobody ever benchmarked has no floor to drop, so it cannot be
  retired and cannot drift; it is simply absent, and absent looks identical to
  healthy from in here.
* THE HARNESS ITSELF IS OUT OF SCOPE. These are floors on OUTPUTS, not on what
  produced them. If a fixture quietly stops exercising the path it is named for
  while still emitting the same token counts, every number stays plausible and
  every check stays green.
* AN ENTRY IN THE CANDIDATE BUT ABSENT FROM THE PR'S FLOOR IS SKIPPED HERE.
  ``compare_baseline`` already fails that as "unknown dataset"/"unknown regime";
  this guard deliberately does not double-report it. The division of labour is
  the point — this module answers only "is the floor still the right floor".

The regimes are flattened with ``compare_baseline._parse_regimes`` — the gate's
OWN parser, deliberately not a copy. Two parsers that must never diverge is how
a subsystem one branch over grew a nested-content bug: the moment this module
had its own idea of what a regime IS, the guard and the gate could disagree
about what was even being floored.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from benchmarks.compare_baseline import (
    _EPS,
    TOKEN_SLACK,
    CompareInputError,
    _parse_regimes,
)

#: Files a deliberate, standalone re-floor is allowed to touch. A PR whose diff
#: is a subset of these is the re-floor itself and may move floors UP.
BASELINE_ARTIFACTS: Final[frozenset[str]] = frozenset(
    {"benchmarks/baseline_results.json", "benchmarks/BASELINE.md"}
)

_EXIT_OK: Final = 0
_EXIT_FINDINGS: Final = 1
_EXIT_INPUT_ERROR: Final = 2


class FloorGuardInputError(Exception):
    """Input could not be turned into a comparable set of floors.

    Kept distinct from a detected finding: the CLI maps this to exit code 2
    while a finding exits 1, so an unreadable artifact can never be mistaken
    for a clean run.
    """


@dataclass(frozen=True)
class Finding:
    """One failed check, with the remedy spelled out for the reader.

    ``scope`` is ``"dataset"`` or ``"regime"``, mirroring how
    ``compare_baseline`` labels its own failures ("dataset x: ...", "regime
    y: ..."), so the two tools name the same thing the same way.
    """

    check: str
    scope: str
    subject: str
    message: str

    def render(self) -> str:
        return f"{self.check} {self.scope} {self.subject}: {self.message}"


@dataclass(frozen=True)
class FloorSnapshot:
    """Both floored coverage sets from one ``baseline_results.json``.

    Held together in one type on purpose: every caller needs all three, and an
    optional-with-default map would let a caller silently guard part of the
    file — which is the exact defect this snapshot was introduced to close.

    The three fields are exactly the three quantities ``compare_baseline``
    gates. That is a bounded, checkable claim rather than an aspiration: if the
    gate ever starts flooring a fourth, this type is where it fails to compile
    into the guard.
    """

    tokens: Mapping[str, int]
    retention: Mapping[str, float]
    regimes: Mapping[str, float]


@dataclass(frozen=True)
class GuardReport:
    findings: tuple[Finding, ...]
    datasets_compared: int
    regimes_compared: int
    baseline_only_pr: bool

    @property
    def ok(self) -> bool:
        return not self.findings


def _dataset_floors(
    source: Mapping[str, object], origin: str
) -> tuple[dict[str, int], dict[str, float]]:
    """Extract both per-dataset floors, failing loudly on schema drift.

    ``tokens_after`` and ``information_retention`` are pulled in one pass
    because ``compare_baseline`` gates BOTH, and reading one without the other
    is how this module previously came to guard part of what the gate enforces.
    """
    rows = source.get("datasets")
    if not isinstance(rows, Sequence):
        raise FloorGuardInputError(f"{origin}: 'datasets' is missing or not a list")
    tokens: dict[str, int] = {}
    retention: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise FloorGuardInputError(f"{origin}: a dataset entry is not an object")
        name, after, kept = (
            row.get("name"),
            row.get("tokens_after"),
            row.get("information_retention"),
        )
        if not isinstance(name, str) or not isinstance(after, int) or isinstance(after, bool):
            raise FloorGuardInputError(f"{origin}: dataset {name!r} has no integer 'tokens_after'")
        if not isinstance(kept, (int, float)) or isinstance(kept, bool):
            raise FloorGuardInputError(
                f"{origin}: dataset {name!r} has no numeric 'information_retention'"
            )
        tokens[name] = after
        retention[name] = float(kept)
    if not tokens:
        raise FloorGuardInputError(f"{origin}: no datasets found")
    return tokens, retention


def _recall_regimes(source: Mapping[str, object], origin: str) -> dict[str, float]:
    """Flatten ``needle_recall`` with the GATE'S OWN parser, never a copy.

    ``_parse_regimes`` is private to ``compare_baseline`` and imported anyway,
    deliberately: a second implementation would drift, and then the guard and
    the gate would disagree about what a regime IS while both looked correct.
    Its ``CompareInputError`` is translated so the CLI's exit-2 contract still
    separates "unreadable artifact" from "clean run".
    """
    if "needle_recall" not in source:
        raise FloorGuardInputError(f"{origin}: missing required key 'needle_recall'")
    try:
        return _parse_regimes(source["needle_recall"], origin)
    except CompareInputError as exc:
        raise FloorGuardInputError(str(exc)) from exc


def load_snapshot(path: Path) -> FloorSnapshot:
    """Read both floored coverage sets, failing loudly on either."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FloorGuardInputError(f"{path}: unreadable ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise FloorGuardInputError(f"{path}: malformed JSON ({exc})") from exc
    if not isinstance(raw, Mapping):
        raise FloorGuardInputError(f"{path}: top level is not an object")
    tokens, retention = _dataset_floors(raw, str(path))
    return FloorSnapshot(
        tokens=tokens,
        retention=retention,
        regimes=_recall_regimes(raw, str(path)),
    )


def is_baseline_only(changed_files: Sequence[str]) -> bool:
    """True when the PR's diff touches nothing but the baseline artifacts.

    A HEURISTIC, NOT A PROOF OF INTENT — the same caveat as the blind spot
    above. "Touches only the baseline artifacts" is a decent proxy for "this PR
    IS the deliberate re-floor and will be reviewed as one", but it cannot
    verify that the new floor was argued for, only that nothing else rode along
    with it. It is chosen because it is mechanical and unspoofable-by-accident,
    not because it reads minds.

    An empty diff is NOT baseline-only: nothing was deliberately re-floored, so
    an upward move could not have been argued for.
    """
    files = {f.strip() for f in changed_files if f.strip()}
    return bool(files) and files <= BASELINE_ARTIFACTS


_REGENERATE_REMEDY: Final = (
    "REMEDY: regenerate the floor IN THIS PR — "
    "`python -m benchmarks.run_bench --out /tmp/bench && "
    "cp /tmp/bench/baseline_results.json /tmp/bench/BASELINE.md benchmarks/`. "
    "That cannot hide a regression: the DIRECTION check fails any floor this PR "
    "moves in the loosening direction."
)


def _retirement_finding(scope: str, subject: str, was: str) -> Finding:
    """The same ruling for a dropped dataset and a dropped regime.

    Removal is the strongest loosening there is: it does not move the ceiling,
    it deletes it. ``compare_baseline`` cannot see it — it pairs the candidate
    against the PR's OWN floor, so a PR that deletes an entry from the harness
    AND from the floor leaves both sides agreeing it is gone and draws no
    finding. Measured on both halves, not assumed for the second.
    """
    return Finding(
        check="DIRECTION",
        scope=scope,
        subject=subject,
        message=(
            f"this PR drops {subject} from the floor entirely (it was {was} on main) "
            f"while also changing other files. Removing a floor is an UNBOUNDED "
            f"loosening — the {scope} keeps no ceiling at all, and compare_baseline "
            f"cannot see it because the candidate and the PR's floor then agree the "
            f"{scope} is gone. REMEDY: retire a {scope} in a standalone PR that touches "
            f"only {sorted(BASELINE_ARTIFACTS)}, so the loss of coverage is reviewed "
            f"on its own."
        ),
    )


def _require_something_to_compare(snapshot: FloorSnapshot, role: str) -> None:
    """Refuse a comparison with nothing in it. ASSERT THE POSITIVE COUNT.

    A guard whose green state does not distinguish "I checked and it held" from
    "there was nothing for me to check" is not a guard — every loop below is a
    no-op over an empty mapping, and the report renders
    ``datasets=0 regimes=0 findings=0 / RESULT: floor still describes reality``.
    That sentence would be false in the most dangerous possible way: it reads as
    a verified floor and means an unread file.

    ``load_snapshot`` already rejects an empty artifact, so on the CI path this
    is unreachable — which is exactly why it is asserted here instead of relied
    upon there. The check belongs to whoever draws the conclusion, not to
    whoever happened to load the data; ``evaluate`` is public and pure, and a
    future caller assembling snapshots by hand gets the same refusal.
    """
    if not snapshot.tokens or not snapshot.regimes:
        raise FloorGuardInputError(
            f"{role}: nothing to compare — {len(snapshot.tokens)} datasets and "
            f"{len(snapshot.regimes)} recall regimes. A floor guard that passes on an "
            f"empty comparison reports a verified floor when it has read nothing, so "
            f"this refuses instead of returning a clean report."
        )


def _higher_is_better(
    *,
    scope: str,
    quantity: str,
    floor_main: Mapping[str, float],
    floor_pr: Mapping[str, float],
    candidate: Mapping[str, float],
    baseline_only: bool,
) -> list[Finding]:
    """DRIFT and DIRECTION for a quantity where a bigger number is a better one.

    Recall and ``information_retention`` are the same shape: the floor is a
    MINIMUM, so a loosening moves it DOWN and drift is a floor sitting BELOW
    what the run measures. Written once rather than twice because the two
    differ only in what they are called — and a second hand-written copy is how
    the token half and the regime half came to disagree in the first place.
    """
    findings: list[Finding] = []
    for key in sorted(candidate):
        floor = floor_pr.get(key)
        if floor is None:
            continue
        measured = candidate[key]
        if measured > floor + _EPS:
            findings.append(
                Finding(
                    check="DRIFT",
                    scope=scope,
                    subject=key,
                    message=(
                        f"the committed {quantity} floor is {floor:.4f} but this run measures "
                        f"{measured:.4f} — the gate would forgive a drop all the way back to "
                        f"{floor:.4f} that this engine does not actually need. "
                        f"{_REGENERATE_REMEDY}"
                    ),
                )
            )

    for key in sorted(floor_pr):
        was = floor_main.get(key)
        if was is None:
            # Mirror of the new-dataset case: no floor on main means no previous
            # floor to have moved DOWN from, so DIRECTION is silent BY
            # CONSTRUCTION rather than by accident. DRIFT above still holds it
            # to what it actually measures.
            continue
        now = floor_pr[key]
        if now < was - _EPS and not baseline_only:
            findings.append(
                Finding(
                    check="DIRECTION",
                    scope=scope,
                    subject=key,
                    message=(
                        f"this PR lowers the {quantity} floor {was:.4f} -> {now:.4f}, which "
                        f"LOOSENS the gate while also changing other files. This is the "
                        f"quantity where bigger is better, so DOWN is the loosening direction. "
                        f"A wholesale regenerate would otherwise launder a {quantity} "
                        f"regression the same way it launders a token one. REMEDY: split the "
                        f"floor change into a standalone PR that touches only "
                        f"{sorted(BASELINE_ARTIFACTS)}, stating the old numbers, the new "
                        f"numbers and why the new floor is honest."
                    ),
                )
            )
    return findings


def evaluate(
    *,
    floor_main: FloorSnapshot,
    floor_pr: FloorSnapshot,
    candidate: FloorSnapshot,
    changed_files: Sequence[str],
    slack: float = TOKEN_SLACK,
) -> GuardReport:
    """Run both checks over every floored quantity. Pure: no I/O, git or clock.

    Refuses to report on a comparison it did not actually make. See
    ``_require_something_to_compare`` — a guard whose green does not separate
    "checked and held" from "nothing to check" is not a guard, and this one had
    that defect until it was pointed at itself.
    """
    _require_something_to_compare(floor_main, "floor_main")
    _require_something_to_compare(floor_pr, "floor_pr")
    _require_something_to_compare(candidate, "candidate")
    baseline_only = is_baseline_only(changed_files)
    findings: list[Finding] = []

    # ---- TOKENS: bigger is worse, so a loosening moves the floor UP. --------
    for name in sorted(candidate.tokens):
        floor = floor_pr.tokens.get(name)
        if floor is None:
            continue
        actual = candidate.tokens[name]
        if actual <= 0:
            continue
        headroom = (floor - actual) / actual
        if headroom > slack:
            findings.append(
                Finding(
                    check="DRIFT",
                    scope="dataset",
                    subject=name,
                    message=(
                        f"the committed floor is {floor} tokens but this run measures "
                        f"{actual} — {headroom * 100:.2f}% of silent regression headroom, "
                        f"above the {slack * 100:.0f}% the gate already forgives. "
                        f"{_REGENERATE_REMEDY}"
                    ),
                )
            )

    for name in sorted(set(floor_main.tokens) - set(floor_pr.tokens)):
        if not baseline_only:
            findings.append(
                _retirement_finding("dataset", name, f"{floor_main.tokens[name]} tokens")
            )

    for name in sorted(floor_pr.tokens):
        was = floor_main.tokens.get(name)
        if was is None:
            # No floor on main means no previous floor to have moved UP from,
            # so DIRECTION is silent by construction rather than by accident.
            # Not unguarded: DRIFT above still compares the new floor against
            # what the dataset actually measures.
            continue
        now = floor_pr.tokens[name]
        if now > was and not baseline_only:
            findings.append(
                Finding(
                    check="DIRECTION",
                    scope="dataset",
                    subject=name,
                    message=(
                        f"this PR raises the floor {was} -> {now}, which LOOSENS the gate by "
                        f"{(now - was) / was * 100:.2f}% while also changing other files. "
                        f"A downward move is a tightening and may ride along with the change "
                        f"that earned it; an upward move is a loosening and must be argued on "
                        f"its own. REMEDY: split the floor change into a standalone PR that "
                        f"touches only {sorted(BASELINE_ARTIFACTS)}, stating the old numbers, "
                        f"the new numbers and why the new floor is honest."
                    ),
                )
            )

    # ---- The two quantities where bigger is BETTER, so a loosening moves the
    # floor DOWN. Same two checks, arithmetic inverted, and no slack to spend:
    # compare_baseline forgives both only to within _EPS, so ANY gap between
    # floor and reality is headroom somebody could regress into.
    findings += _higher_is_better(
        scope="regime",
        quantity="recall",
        floor_main=floor_main.regimes,
        floor_pr=floor_pr.regimes,
        candidate=candidate.regimes,
        baseline_only=baseline_only,
    )
    for key in sorted(set(floor_main.regimes) - set(floor_pr.regimes)):
        if not baseline_only:
            findings.append(
                _retirement_finding("regime", key, f"recall {floor_main.regimes[key]:.4f}")
            )

    # information_retention is the THIRD thing compare_baseline gates, and it
    # was the third blind spot: lowering a retention floor from 1.00 to 0.60 in
    # both the floor and the candidate exits 0 on both tools. It needs no
    # retirement loop of its own — it is keyed by dataset name, so a dropped
    # dataset is already caught by the token retirement check above.
    findings += _higher_is_better(
        scope="dataset",
        quantity="information_retention",
        floor_main=floor_main.retention,
        floor_pr=floor_pr.retention,
        candidate=candidate.retention,
        baseline_only=baseline_only,
    )

    return GuardReport(
        findings=tuple(findings),
        datasets_compared=len(candidate.tokens),
        regimes_compared=len(candidate.regimes),
        baseline_only_pr=baseline_only,
    )


def render(report: GuardReport) -> str:
    status = "PASS" if report.ok else "FAIL"
    lines = [
        f"benchmark floor guard: {status}",
        f"datasets={report.datasets_compared} regimes={report.regimes_compared} "
        f"findings={len(report.findings)} baseline_only_pr={report.baseline_only_pr}",
    ]
    lines += [f"  {finding.render()}" for finding in report.findings]
    lines.append(
        "RESULT: floor still describes reality."
        if report.ok
        else f"RESULT: {len(report.findings)} floor finding(s)."
    )
    return "\n".join(lines)


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FloorGuardInputError(f"{path}: unreadable ({exc})") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--floor-main", required=True, type=Path, help="baseline as committed on main"
    )
    parser.add_argument(
        "--floor-pr", required=True, type=Path, help="baseline as committed on this PR"
    )
    parser.add_argument("--candidate", required=True, type=Path, help="fresh run_bench output")
    parser.add_argument(
        "--changed-files",
        required=True,
        type=Path,
        help="file holding this PR's changed paths, one per line (git diff --name-only)",
    )
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            floor_main=load_snapshot(args.floor_main),
            floor_pr=load_snapshot(args.floor_pr),
            candidate=load_snapshot(args.candidate),
            changed_files=_read_lines(args.changed_files),
        )
    except FloorGuardInputError as exc:
        print(f"benchmark floor guard: INPUT ERROR\n  {exc}", file=sys.stderr)
        return _EXIT_INPUT_ERROR
    print(render(report))
    return _EXIT_OK if report.ok else _EXIT_FINDINGS


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
