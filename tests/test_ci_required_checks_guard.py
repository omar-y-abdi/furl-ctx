"""Guards .github/workflows/ci.yml against re-introducing the required-check deadlock.

GitHub ruleset 18484290 marks three CI jobs as REQUIRED status checks: ``lint``,
``build-wheel``, and ``test`` (a 4-shard matrix). A required check that is SKIPPED does
NOT satisfy the ruleset — GitHub treats "skipped" as "never concluded", so the PR stays
BLOCKED forever even when every context reads green-or-skipped. The only way a required
job can both (a) always conclude on every PR and (b) still avoid its expensive work on a
docs/config/workflow-only diff is to ALWAYS RUN with no job-level ``if:`` and short-
circuit its heavy STEPS at the step level via ``if: needs.changes.outputs.code == 'true'``.

Adding a job-level ``if:`` to any required job re-introduces the deadlock: the job skips,
its required context never concludes, and every config/docs-only PR silently re-blocks.
That exact regression shipped THREE times (incidents #15, #32, #56) before commit 9a164e1
fixed it — and today the fix survives only as a code comment near the top of ci.yml, which
cannot fail CI. This test turns the invariant into an executable check: it parses ci.yml
and fails the build if any required job grows a job-level ``if:``, if the ``changes`` job
stops exporting ``outputs.code``, or if a required job stops depending on ``changes``. A
required job that is renamed also fails loudly here (never a vacuous pass), forcing a
conscious, coordinated update of the ruleset and this test together.

Pure stdlib + PyYAML; it never imports furl_ctx or the compiled ``_core`` extension, so it
exercises the invariant independently of a built wheel. A missing parser or an unreadable/
unparseable ci.yml is a hard failure, never a skip — a meta-test that passed vacuously
would defeat its own purpose.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:
    # Hard-fail (not skip) so the guard can never pass vacuously for want of a
    # parser. PyYAML is declared in the project's [dev] optional-dependencies as
    # `pyyaml>=6`; install with `pip install -e .[dev]`.
    raise ModuleNotFoundError(
        "tests/test_ci_required_checks_guard.py requires PyYAML to parse ci.yml "
        "(declared as `pyyaml>=6` in the [dev] optional-dependencies). Install it "
        "with `pip install -e .[dev]`; this guard must never be silently skipped."
    ) from exc

_ROOT = Path(__file__).resolve().parents[1]
_CI_YML = _ROOT / ".github" / "workflows" / "ci.yml"

# The jobs GitHub ruleset 18484290 marks as REQUIRED status checks. THIS is the
# obvious edit point if the ruleset's required set ever changes: keep it in lockstep
# with the ruleset. Every job named here must ALWAYS run (no job-level `if:`) and
# short-circuit its expensive steps at the step level — see the module docstring.
_REQUIRED_JOBS = ("lint", "build-wheel", "test")

# The paths-filter job whose `outputs.code` every required job reads to short-circuit
# its heavy steps (`if: needs.changes.outputs.code == 'true'`). The step-level gate is
# only possible because this output exists and each required job depends on this job.
_CHANGES_JOB = "changes"
_CHANGES_OUTPUT = "code"

# The `test` job's `strategy.matrix.shard` list. GitHub Actions names each matrix
# job's status check `<job-name> (<matrix-value>)`, so this exact list of four
# values is what produces the four contexts ruleset 18484290 requires: test (1),
# test (2), test (3), test (4). Shrinking, growing, reordering with gaps, or
# converting this to an `include:`/`name:` form changes which contexts GitHub
# produces — any of the four that stops being produced leaves every PR BLOCKED
# forever, waiting on a context that will never exist. Keep this constant, ci.yml's
# matrix, and the ruleset's required-context list in lockstep.
_REQUIRED_TEST_SHARDS = [1, 2, 3, 4]

# Keys forbidden on the `pull_request` trigger. Either one makes the ENTIRE
# workflow not run at all for a PR outside the filter — not just skip a job, but
# never even start — so none of the required contexts (lint, build-wheel,
# test (1)..test (4)) are ever produced for that PR, which is then BLOCKED
# forever. This is the #15/#32/#56 deadlock one level up: instead of a required
# job skipping internally, the whole workflow run never happens.
_FORBIDDEN_TRIGGER_FILTER_KEYS = ("paths", "paths-ignore")


def _load_ci_workflow() -> dict[str, object]:
    """Parse ci.yml, failing loudly if it is missing, empty, or not a YAML mapping.

    A guard that silently passed when the workflow could not be read would be worse
    than no guard — the whole point is that a broken ci.yml cannot merge. So a missing
    file, an empty file, or a syntax error is a hard failure here, never a skip.
    """
    assert _CI_YML.is_file(), (
        f"ci.yml not found at {_CI_YML}; this guard cannot verify the required-check "
        "deadlock invariant. If the workflow moved, update _CI_YML in this test."
    )
    data = yaml.safe_load(_CI_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        f"ci.yml did not parse to a YAML mapping (got {type(data).__name__}); the "
        "workflow is empty or malformed."
    )
    return data


def _jobs(workflow: dict[str, object]) -> dict[str, object]:
    """The workflow's ``jobs:`` mapping, or a hard failure if it is absent/misshapen."""
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), (
        "ci.yml has no `jobs:` mapping; cannot verify the required-check deadlock invariant."
    )
    return jobs


def _required_job(jobs: dict[str, object], name: str) -> dict[str, object]:
    """Return required job ``name``, failing loudly if it is absent or not a mapping.

    A rename must never let this guard pass vacuously: if a required job disappears
    under this name, GitHub's ruleset still expects the OLD context, so the PR would
    silently re-block. Fail here so the rename is a deliberate, coordinated change.
    """
    assert name in jobs, (
        f"required status-check job {name!r} is missing from ci.yml. Ruleset 18484290 "
        f"requires {list(_REQUIRED_JOBS)} to conclude on every PR; a renamed/removed job "
        "leaves GitHub waiting on the old context forever. Update the ruleset AND "
        f"_REQUIRED_JOBS together. Present jobs: {sorted(jobs)}."
    )
    job = jobs[name]
    assert isinstance(job, dict), f"job {name!r} in ci.yml is not a mapping: {job!r}."
    return job


def _needs_list(job: dict[str, object]) -> list[str]:
    """A job's ``needs:`` normalized to a list of job names.

    GitHub accepts ``needs:`` as either a bare string (``needs: changes``) or a list
    (``needs: [changes, build-wheel]``); normalize both. An absent ``needs:`` yields an
    empty list, which makes the dependency check below fail loudly — exactly right.
    """
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    if isinstance(needs, list):
        return [str(item) for item in needs]
    raise AssertionError(f"unexpected `needs:` shape {needs!r} in ci.yml.")


def _job_mapping(jobs: dict[str, object], name: str, context: str) -> dict[str, object]:
    """Fetch job ``name`` from ``jobs``, failing loudly if absent or not a mapping.

    Generic counterpart to ``_required_job`` for jobs discovered while walking a
    transitive ``needs:`` closure, where ``name`` may be any upstream job — required
    or not. ``context`` is folded into the failure message to say which closure walk
    triggered it.
    """
    assert name in jobs, (
        f"{context}: `needs:` references job {name!r}, which does not exist in "
        f"ci.yml. Present jobs: {sorted(jobs)}."
    )
    job = jobs[name]
    assert isinstance(job, dict), f"{context}: job {name!r} in ci.yml is not a mapping: {job!r}."
    return job


def _transitive_needs(jobs: dict[str, object], start: str) -> set[str]:
    """Every job name transitively reachable from ``start`` via ``needs:`` edges.

    Excludes ``start`` itself; includes every direct and indirect dependency. Guards
    against cycles (which GitHub Actions itself rejects at parse time) with a
    ``visited`` set, so a malformed workflow fails this test cleanly instead of
    looping forever.
    """
    ancestors: set[str] = set()
    visited = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        job = _job_mapping(jobs, current, f"walking the needs-closure of {start!r}")
        for dep in _needs_list(job):
            if dep not in visited:
                visited.add(dep)
                ancestors.add(dep)
                frontier.append(dep)
    return ancestors


# Assertion 1 — "the required jobs are present under their expected names" — is
# enforced by ``_required_job()`` itself, which hard-asserts presence and is called
# for every name in _REQUIRED_JOBS by the tests below. A rename therefore still
# fails loudly; a standalone existence test only duplicated that assert.


def test_required_jobs_have_no_job_level_if() -> None:
    # Assertion 2 — THE deadlock guard. A required check that SKIPS does not satisfy
    # ruleset 18484290, so a job-level `if:` on any required job re-blocks every
    # docs/config/workflow-only PR. Gate the expensive STEPS at the step level instead.
    jobs = _jobs(_load_ci_workflow())
    gated = sorted(name for name in _REQUIRED_JOBS if "if" in _required_job(jobs, name))
    assert not gated, (
        f"required status-check job(s) {gated} have a job-level `if:` key. A GitHub-"
        "required check (ruleset 18484290) that SKIPS does NOT satisfy the ruleset — it "
        "leaves docs/config/workflow-only PRs BLOCKED forever on a context that never "
        "concludes. This exact deadlock shipped and re-blocked PRs three times (incidents "
        "#15, #32, #56) before commit 9a164e1 fixed it. Required jobs MUST always run; "
        "short-circuit their EXPENSIVE STEPS at the step level with "
        "`if: needs.changes.outputs.code == 'true'` instead. Do NOT re-add a job-level `if:`."
    )


def test_changes_job_declares_code_output() -> None:
    # Assertion 3: the `changes` job exists and exports `outputs.code`, the foundation
    # every required job's step-level short-circuit reads.
    jobs = _jobs(_load_ci_workflow())
    assert _CHANGES_JOB in jobs, (
        f"the {_CHANGES_JOB!r} job is missing from ci.yml; it is the foundation of the "
        "step-level short-circuit that lets required jobs always run yet conclude in "
        f"seconds on docs/config-only PRs. Present jobs: {sorted(jobs)}."
    )
    changes = _required_job(jobs, _CHANGES_JOB)
    # The gate itself must ALWAYS run: a job-level `if:` on `changes` that skips
    # (on a non-PR event, or under any condition false on a PR) cascade-skips every
    # required job that lists `needs: changes` without `if: always()`, so the required
    # contexts never conclude — the same #15/#32/#56 deadlock, one level up. Condition
    # at the step level, never on this job.
    assert "if" not in changes, (
        f"the {_CHANGES_JOB!r} gate job has a job-level `if:` key. If it skips, every "
        f"required job that depends on it cascade-skips and its required status check "
        "never concludes — re-opening the #15/#32/#56 deadlock from the gate. The gate "
        "must always run; keep any conditioning at the step level."
    )
    outputs = changes.get("outputs")
    declared = sorted(outputs) if isinstance(outputs, dict) else outputs
    assert isinstance(outputs, dict) and _CHANGES_OUTPUT in outputs, (
        f"the {_CHANGES_JOB!r} job must declare `outputs.{_CHANGES_OUTPUT}`; every required "
        f"job short-circuits its heavy steps on "
        f"`needs.{_CHANGES_JOB}.outputs.{_CHANGES_OUTPUT} == 'true'`. Without this output "
        "the short-circuit silently evaluates false-y (heavy steps never run) — or a "
        "maintainer 'fixes' that with a job-level `if:` and re-introduces the deadlock. "
        f"Declared outputs: {declared}."
    )


def test_required_jobs_depend_on_changes() -> None:
    # Assertion 4: each required job lists `changes` in `needs:`, so the step-level
    # short-circuit dependency stays wired (`test` needs `changes` and `build-wheel`;
    # we only require `changes` here). `needs:` may be a string or a list.
    jobs = _jobs(_load_ci_workflow())
    missing_dep = sorted(
        name
        for name in _REQUIRED_JOBS
        if _CHANGES_JOB not in _needs_list(_required_job(jobs, name))
    )
    assert not missing_dep, (
        f"required job(s) {missing_dep} do not list {_CHANGES_JOB!r} in `needs:`. Without "
        f"that dependency `needs.{_CHANGES_JOB}.outputs.{_CHANGES_OUTPUT}` is undefined, so "
        "the step-level short-circuit evaluates false-y and the heavy steps never run (or "
        "the job errors). Keep the short-circuit dependency wired."
    )


def test_test_job_matrix_shard_is_pinned() -> None:
    # Assertion 5: the `test` job's matrix produces EXACTLY the four required
    # contexts test (1)..test (4) (ruleset 18484290) — never more, fewer, or a
    # different shape. GitHub Actions names each matrix job's status check
    # `<job-name> (<matrix-value>)`; changing `shard` — e.g. to `[1, 2, 3]` — or
    # converting the matrix to an `include:`/`name:` form changes which contexts are
    # produced. Any of the four required contexts that stops being produced leaves
    # every PR BLOCKED forever, waiting on a context that will never exist — the
    # same shape of deadlock as incidents #15/#32/#56, triggered by a matrix edit
    # instead of a job-level `if:`.
    jobs = _jobs(_load_ci_workflow())
    test_job = _required_job(jobs, "test")
    strategy = test_job.get("strategy")
    assert isinstance(strategy, dict), (
        f"job 'test' in ci.yml has no `strategy:` mapping (got {strategy!r}). Ruleset "
        "18484290 requires the four contexts test (1)..test (4), which only exist "
        "because of this job's matrix strategy."
    )
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), (
        f"job 'test' `strategy:` has no `matrix:` mapping (got {matrix!r}). Ruleset "
        "18484290 requires contexts test (1)..test (4); converting the matrix to an "
        "`include:`-only or other form changes/removes those exact contexts."
    )
    shard = matrix.get("shard")
    assert shard == _REQUIRED_TEST_SHARDS, (
        f"job 'test' `strategy.matrix.shard` is {shard!r}; expected exactly "
        f"{_REQUIRED_TEST_SHARDS!r}. GitHub ruleset 18484290 requires the FOUR status "
        "checks test (1), test (2), test (3), test (4) to conclude on every PR. "
        "Shrinking the shard list (e.g. to [1, 2, 3]), growing it, or reshaping the "
        "matrix changes which of those four contexts GitHub actually produces — any "
        "required context that is no longer produced leaves every PR BLOCKED forever. "
        "Update the ruleset's required-context list AND this constant together, in "
        "lockstep."
    )


def test_pull_request_trigger_has_no_path_filters() -> None:
    # Assertion 6: the `on.pull_request` trigger carries neither `paths:` nor
    # `paths-ignore:`. PyYAML's SafeLoader implements YAML 1.1, under which the bare
    # `on:` key parses as the boolean `True` (the classic YAML "Norway problem"),
    # not the string "on" — so the trigger mapping actually lives at
    # `workflow[True]`, not `workflow["on"]`. Handle both in case a future PyYAML
    # release changes this.
    workflow = _load_ci_workflow()
    trigger_block = workflow.get("on", workflow.get(True))
    assert isinstance(trigger_block, dict), (
        "ci.yml's `on:` block did not parse to a mapping (got "
        f"{type(trigger_block).__name__}); cannot verify the pull_request trigger has "
        "no path filter. Note PyYAML parses a bare `on:` key as the boolean True, not "
        "the string 'on' — this test reads workflow.get('on', workflow.get(True))."
    )
    assert "pull_request" in trigger_block, (
        "ci.yml's `on:` block has no `pull_request` trigger. Ruleset 18484290's "
        "required checks (lint, build-wheel, test (1)..test (4)) only conclude on "
        "pull_request runs; without this trigger none of those contexts are ever "
        "produced and every PR is blocked waiting on them forever."
    )
    pull_request_trigger = trigger_block["pull_request"]
    if isinstance(pull_request_trigger, dict):
        forbidden = sorted(
            key for key in _FORBIDDEN_TRIGGER_FILTER_KEYS if key in pull_request_trigger
        )
        assert not forbidden, (
            f"ci.yml's `on.pull_request` trigger has forbidden key(s) {forbidden}. A "
            "`paths:`/`paths-ignore:` filter under `on.pull_request` stops the ENTIRE "
            "workflow from running at all for a filtered-out PR — every required "
            "status check (lint, build-wheel, test (1)..test (4), ruleset 18484290) is "
            "then never produced for that PR, which is blocked forever. This is the "
            "#15/#32/#56 deadlock one level up: the whole run never starts, instead of "
            "a job inside it skipping. Filter WHICH STEPS do heavy work via the "
            "`changes` job's step-level short-circuit instead — never filter which "
            "PRs the workflow runs on."
        )
    # else: `pull_request` maps to `None` (a bare trigger with no config), which
    # cannot carry a `paths`/`paths-ignore` key — nothing further to check.


def test_required_job_transitive_dependencies_have_no_job_level_if() -> None:
    # Assertion 7: no job anywhere in a required job's TRANSITIVE `needs:` closure —
    # not just its direct dependencies — has a job-level `if:`. Inserting a new
    # upstream job between a required job and its existing dependencies (e.g.
    # changing `build-wheel`'s `needs:` from `[changes]` to `[changes, precheck]`
    # where `precheck` has `if: false`) cascade-skips the required job exactly like
    # a job-level `if:` placed directly on it would (assertion 2) — just one or
    # more hops further away, where a name-based check over `_REQUIRED_JOBS` alone
    # cannot see it. `changes` itself is already asserted if-free by
    # test_changes_job_declares_code_output (assertion 3); this test closes the gap
    # for every OTHER job that could be inserted upstream of a required job.
    jobs = _jobs(_load_ci_workflow())
    offenders: dict[str, list[str]] = {}
    for required_name in _REQUIRED_JOBS:
        ancestors = _transitive_needs(jobs, required_name)
        gated = sorted(
            name
            for name in ancestors
            if "if" in _job_mapping(jobs, name, f"needs-closure of {required_name!r}")
        )
        if gated:
            offenders[required_name] = gated
    assert not offenders, (
        f"job(s) with a job-level `if:` found in the transitive `needs:` closure of "
        f"required job(s): {offenders}. A GitHub-required check (ruleset 18484290) "
        "cascade-skips — and never concludes — if ANY job it transitively depends on "
        "skips, not only if the required job itself carries the `if:`. This is the "
        "#15/#32/#56 deadlock, reached through an upstream dependency instead of a "
        "direct job-level `if:` on the required job. Remove the job-level `if:` and "
        "gate its heavy steps at the step level instead, exactly like `changes`, "
        "`lint`, `build-wheel`, and `test` already do."
    )


# ─── The `code` filter's COVERAGE (distinct from the deadlock invariant above) ──
#
# The assertions above keep every required job CONCLUDING. These keep the filter
# that decides whether those jobs do any real WORK from silently under-covering.
#
# `changes.outputs.code` gates the heavy steps. When it is false, the test shards
# do not run at all — so any path that can change what the suite tests, yet is
# absent from the filter, is a hole: that PR merges with the tests that would
# have caught it never executed. `verify/**` and `benchmarks/**` were exactly
# that hole (the suite imports both as Python packages), and `conftest.py` — the
# root config every test loads — was a third.
#
# Derived, not hand-listed: the coverage test below walks the suite's REAL AST
# imports. A grep would not do; `tests/test_effective_savings_offload_cost.py`
# mentions "verify/measure.py" in prose several times AND imports it, while
# other files only mention it, so only an import graph distinguishes a genuine
# dependency from a docstring. Add a test that imports a new top-level package
# and forget the filter, and this goes red naming the package.
#
# Deliberately NOT asserted here, each verified rather than assumed:
#   * `llms.txt` — uncovered on purpose. The `lint` job runs its anchor guard
#     UNCONDITIONALLY (no `needs.changes.outputs.code` gate) precisely because
#     llms.txt is not `.md` and would never trip the filter; ci.yml says so.
#   * `docs/`, `site/` — no test reads them; the apparent hits are incidental
#     (`tmp_path / "site"` builds a temp directory, not a repo path).
#   * `rust-toolchain.toml` — ci.yml pins `toolchain: 1.95.0` inline and never
#     reads the file, so editing it alone cannot change what CI builds.

_ROOT_CONFTEST = "conftest.py"

# Pattern shapes `_path_matches_filter` understands. dorny/paths-filter uses
# picomatch; this is a deliberate SUBSET covering every shape ci.yml's `code`
# filter actually uses. An unrecognized shape is a hard failure rather than a
# silent non-match, so a future pattern this matcher cannot reason about can
# never make the coverage test pass vacuously.
_SUFFIX_GLOB_PREFIX = "**/"
_RECURSIVE_DIR_SUFFIX = "/**"


def _code_filter_patterns() -> list[str]:
    """The `code:` filter's path patterns, parsed out of the `changes` job.

    ``filters:`` is an inline YAML *string* (a block scalar), so it needs a
    second parse. Every failure mode — missing job, missing step, unparseable
    inner YAML, absent `code` key — is a hard failure: a coverage guard that
    silently found no patterns would pass vacuously and defeat itself.
    """
    jobs = _jobs(_load_ci_workflow())
    changes = _job_mapping(jobs, _CHANGES_JOB, "path-filter coverage")
    steps = changes.get("steps")
    assert isinstance(steps, list), (
        f"the {_CHANGES_JOB!r} job has no `steps:` list; cannot read the `code` path filter."
    )
    for step in steps:
        if not isinstance(step, dict):
            continue
        with_block = step.get("with")
        if not isinstance(with_block, dict) or "filters" not in with_block:
            continue
        parsed = yaml.safe_load(str(with_block["filters"]))
        assert isinstance(parsed, dict) and _CHANGES_OUTPUT in parsed, (
            "the paths-filter step's `filters:` did not parse to a mapping containing "
            f"{_CHANGES_OUTPUT!r}; got {parsed!r}."
        )
        patterns = parsed[_CHANGES_OUTPUT]
        assert isinstance(patterns, list) and patterns, (
            f"the {_CHANGES_OUTPUT!r} filter is empty or not a list: {patterns!r}. An empty "
            "filter would make `code` false for every PR, skipping all heavy steps silently."
        )
        return [str(p) for p in patterns]
    raise AssertionError(
        f"no step with a `with.filters:` block found in the {_CHANGES_JOB!r} job; the "
        "paths-filter action was removed or restructured. This guard reads that block to "
        "verify the filter covers everything the suite depends on — re-point it."
    )


def _path_matches_filter(path: str, patterns: list[str]) -> bool:
    """True if *path* is matched by any pattern, over the subset of glob shapes
    ci.yml uses. Unknown shapes raise rather than silently return False."""
    for pattern in patterns:
        if pattern.startswith(_SUFFIX_GLOB_PREFIX):
            if path.endswith(pattern[len(_SUFFIX_GLOB_PREFIX) + 1 :]):
                return True
        elif pattern.endswith(_RECURSIVE_DIR_SUFFIX):
            if path.startswith(pattern[: -len(_RECURSIVE_DIR_SUFFIX)] + "/"):
                return True
        elif "*" in pattern or "?" in pattern or "[" in pattern:
            raise AssertionError(
                f"path filter pattern {pattern!r} uses a glob shape this guard cannot "
                "evaluate. Extend _path_matches_filter to understand it — do not leave it "
                "unmatched, or the coverage assertions below start passing vacuously."
            )
        elif path == pattern:
            return True
    return False


def _top_level_packages_imported_by_tests() -> dict[str, str]:
    """Map top-level repo package -> the test file importing it.

    Uses the AST, not text search: a docstring naming ``verify/measure.py`` is not
    a dependency, an ``import`` of it is. Only names that exist as directories in
    the repo root count, which excludes stdlib and third-party imports without
    needing a list of them.
    """
    import ast

    found: dict[str, str] = {}
    for path in sorted((_ROOT / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - a broken test file fails elsewhere
            raise AssertionError(f"could not parse {path} to collect imports: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for name in names:
                root_name = name.split(".")[0]
                if (_ROOT / root_name).is_dir() and root_name not in found:
                    found[root_name] = str(path.relative_to(_ROOT))
    return found


def test_code_filter_covers_every_top_level_package_the_tests_import() -> None:
    # Assertion 8: every top-level repo package the suite actually imports is
    # covered by the `code` filter. Derived from the import graph, so a new
    # dependency that nobody adds to the filter fails here instead of silently
    # skipping the heavy steps on the PR that introduces it.
    patterns = _code_filter_patterns()
    imported = _top_level_packages_imported_by_tests()
    assert imported, (
        "no top-level repo packages found imported by tests/ — the import scan is broken, "
        "and this guard would pass vacuously. Check _top_level_packages_imported_by_tests."
    )
    uncovered = {
        package: importer
        for package, importer in sorted(imported.items())
        if not _path_matches_filter(f"{package}/probe.py", patterns)
    }
    assert not uncovered, (
        f"top-level package(s) imported by the test suite but NOT covered by ci.yml's "
        f"`{_CHANGES_OUTPUT}` path filter: {uncovered}. A PR touching only such a package "
        f"leaves `{_CHANGES_OUTPUT}` false, so every heavy step short-circuits and the test "
        "shards never run — the very tests that import it, and would have caught the "
        f"change, do not execute. Add the package as '<name>/**' to the `{_CHANGES_OUTPUT}` "
        "filter in .github/workflows/ci.yml. Adding paths only makes the filter true more "
        "often, so it cannot re-create the #15/#32/#56 required-check deadlock."
    )


def test_code_filter_covers_the_root_conftest() -> None:
    # Assertion 9: the root conftest.py is covered. It is not a package and so is
    # invisible to the import scan above, but pytest loads it for EVERY test in the
    # repo — it holds the unbuilt-extension guard and shared fixtures. A PR editing
    # only conftest.py could otherwise break or silently disarm collection for the
    # entire suite while the suite never ran to notice.
    patterns = _code_filter_patterns()
    assert _path_matches_filter(_ROOT_CONFTEST, patterns), (
        f"{_ROOT_CONFTEST!r} is not covered by ci.yml's `{_CHANGES_OUTPUT}` path filter. "
        "pytest loads the root conftest for every test in the repo, so a change to it can "
        "alter or disarm the entire suite's collection; leaving it unfiltered means such a "
        f"PR skips every heavy step. Add '{_ROOT_CONFTEST}' to the `{_CHANGES_OUTPUT}` "
        "filter in .github/workflows/ci.yml."
    )


def test_verify_and_benchmarks_only_pr_shapes_trigger_the_heavy_steps() -> None:
    # Assertion 10: the concrete PR shapes this guard exists for. Each of these
    # diffs touches ONLY a path that the suite imports, and each must set
    # `code` true so the shards run. Before these paths were filtered, every
    # shape below yielded false and the heavy steps short-circuited — the
    # `verify.run` contract (degradations/hash_failures/silent_loss/
    # cache_prefix_violations) could be edited by a PR whose tests never ran.
    patterns = _code_filter_patterns()
    shapes = {
        "verify/measure.py": "imported by tests/test_effective_savings_offload_cost.py",
        "verify/run.py": "the tool-regression contract every gate asserts",
        "verify/independent_recheck.py": "imported by tests/test_recheck_unflatten.py",
        "benchmarks/metrics.py": "imported by tests/test_benchmarks_fail_closed.py",
        _ROOT_CONFTEST: "root pytest config loaded by every test",
    }
    missed = {
        path: why
        for path, why in sorted(shapes.items())
        if not _path_matches_filter(path, patterns)
    }
    assert not missed, (
        f"single-file PR shape(s) that must run the heavy CI steps but do NOT: {missed}. "
        f"Each leaves `{_CHANGES_OUTPUT}` false, so the four test shards short-circuit and "
        "the suite never runs on that PR. Restore the corresponding entries in the "
        f"`{_CHANGES_OUTPUT}` filter in .github/workflows/ci.yml."
    )
