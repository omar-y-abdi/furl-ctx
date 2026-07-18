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


def test_required_jobs_exist() -> None:
    # Assertion 1: the required jobs are present under their expected names. A rename
    # must force a conscious update here, never a vacuous pass of the checks below.
    jobs = _jobs(_load_ci_workflow())
    missing = [name for name in _REQUIRED_JOBS if name not in jobs]
    assert not missing, (
        f"ci.yml is missing required status-check job(s) {missing}. Ruleset 18484290 "
        f"requires {list(_REQUIRED_JOBS)} to conclude on every PR; if a job was renamed, "
        "GitHub still waits on the old context and every PR re-blocks. Update the ruleset "
        f"AND _REQUIRED_JOBS together. Present jobs: {sorted(jobs)}."
    )


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
