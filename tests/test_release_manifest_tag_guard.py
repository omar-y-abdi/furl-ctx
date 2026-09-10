"""Guards against release-please manifest/tag drift: the exact gap that silently
blocked the v1.3.0 release.

``.release-please-manifest.json`` records the version release-please believes is
already shipped for each package path; release-please treats a bump to that file as
"this version is released" and queues no further release work for it. In the #87
era this repo's manifest was bumped to ``1.3.0`` without a git tag, GitHub release,
or PyPI upload ever happening, so release-please considered ``1.3.0`` shipped and
silently queued nothing. The gap went unnoticed until 2026-07-21, when it was
resolved by tagging ``v1.3.0`` on ``main`` at 1c10beee and creating the matching
GitHub release, re-syncing reality with the manifest.

This test turns that invariant into an executable check: the manifest's recorded
version must have a matching ``v``-prefixed git tag.
``.release-please-config.json`` sets ``include-v-in-tag: true`` and
``include-component-in-tag: false`` for the single root package ``.``, so the tag
name is always ``v{version}`` for this repo; see that file if the package layout
ever changes.

Tag visibility depends on how the checkout was made. A shallow clone with no
explicit tag fetch (GitHub's default ``actions/checkout`` behavior: fetch-depth 1,
``fetch-tags`` unset) sees ZERO tags, not a missing one — ``git tag -l`` returns an
empty list whether or not the expected tag really exists upstream. Asserting drift
from that signal would be a false positive, so zero visible tags is "no signal" and
SKIPS (never silently passes, never fails). This repo's ``ci.yml`` ``test`` job sets
``fetch-tags: true`` so scheduled and manually dispatched runs can enforce the
invariant against settled ``main`` state.

Pull-request race: a release-please release PR merges the manifest bump before the
post-merge release action publishes the matching tag. Any ordinary PR created or
re-run during that window checks out a merge ref whose base already contains the
new manifest while the repository tag namespace can still legitimately lag behind.
That mutable repository-global state is not a defect in the PR under test. Therefore
a missing expected tag on a GitHub ``pull_request`` event is SKIPPED, while the same
missing tag on schedule, workflow_dispatch, or a normal local run still FAILS. The
manifest's structural validation still runs before this decision, so malformed or
missing release metadata remains a hard failure everywhere.

The older release-PR escape hatch remains intentionally narrow: when
``FURL_RELEASE_PR_CONTEXT`` is exactly ``"1"``, the tag assertion is skipped before
the git query. ``ci.yml`` sets that variable only for ``release-please--`` head
branches. It is redundant with GitHub's broader pull-request race handling today,
but retaining the boundary keeps nonstandard release-PR checks from regressing and
avoids coupling this test-only repair to workflow edits.

Pure stdlib; shells out to the system ``git`` binary. A missing manifest, a manifest
that fails to parse, or a manifest missing the ``.`` package is a hard failure,
never a skip — only unavailable tag visibility or a missing-tag verdict during a
GitHub pull-request race skips.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / ".release-please-manifest.json"

# release-please-config.json pins this today: include-v-in-tag=true, include-component-in-tag=false, single package at ".". If either ever changes, the tag-name derivation
# below must change with it — this constant is the single edit point, and a renamed/added package key fails loudly below rather than being silently skipped over.
_MANIFEST_PACKAGE_KEY = "."

# Release-PR escape hatch: CI sets this only for release automation branches; all other runs must leave it empty.
_RELEASE_PR_ENV_VAR = "FURL_RELEASE_PR_CONTEXT"

# GitHub Actions provides this automatically. A missing expected tag cannot be classified as transient-vs-stale from a PR merge ref alone
# because tag publication is asynchronous and repository-global, so only that missing-tag verdict is deferred on pull_request events.
_GITHUB_EVENT_NAME_ENV_VAR = "GITHUB_EVENT_NAME"


def _release_pr_context(env_value: str | None) -> bool:
    """Whether the guard is running inside a release-please release PR.

    Total over ``str | None``: True for the single exact string ``"1"`` and False
    for everything else, so a stray ``"true"``, ``" 1"``, ``"0"``, empty, or unset
    value cannot silently disarm the tag-drift assertion.
    """
    return env_value == "1"


def _load_manifest_version() -> str:
    """The recorded version for the root package, failing loudly if unreadable.

    A missing file, unparseable JSON, or an absent ``.`` package key is a structural
    regression in the release tooling itself, not an environment limitation, so
    this never skips.
    """
    assert _MANIFEST.is_file(), (
        f"{_MANIFEST} not found; cannot verify the manifest/tag drift invariant. "
        "If release-please's manifest moved, update _MANIFEST in this test."
    )
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        f"{_MANIFEST} did not parse to a JSON object (got {type(data).__name__})."
    )
    assert _MANIFEST_PACKAGE_KEY in data, (
        f"{_MANIFEST} has no {_MANIFEST_PACKAGE_KEY!r} package entry; present "
        f"entries: {sorted(data)}. If .release-please-config.json now tracks a "
        "different package layout, update _MANIFEST_PACKAGE_KEY here to match."
    )
    assert set(data) == {_MANIFEST_PACKAGE_KEY}, (
        f"{_MANIFEST} now has package entries beyond {_MANIFEST_PACKAGE_KEY!r}: "
        f"{sorted(data)}. This guard only derives and checks a tag for the "
        "single root package; a future editor adding a second package here "
        "must extend this test to load and verify every entry, not just "
        "_MANIFEST_PACKAGE_KEY, or the new package's tag drift will be "
        "silently ignored."
    )
    version = data[_MANIFEST_PACKAGE_KEY]
    assert isinstance(version, str) and version, (
        f"{_MANIFEST}[{_MANIFEST_PACKAGE_KEY!r}] is not a non-empty string: {version!r}."
    )
    return version


def test_manifest_version_has_matching_git_tag() -> None:
    version = _load_manifest_version()
    expected_tag = f"v{version}"

    if _release_pr_context(os.environ.get(_RELEASE_PR_ENV_VAR)):
        pytest.skip(
            f"{_RELEASE_PR_ENV_VAR}=1: a release-please release PR legitimately "
            "carries a manifest version bumped ahead of git tags until the "
            "post-merge tag lands, so the tag-drift assertion is deliberately "
            "skipped for this run. The structural manifest validation above still "
            "ran. Setting this variable outside a release-PR check run disarms the "
            "drift guard, so ci.yml sets it only for release-please-- head "
            "branches; see this module's docstring."
        )

    listed = subprocess.run(
        ["git", "tag", "-l"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert listed.returncode == 0, (
        f"`git tag -l` failed (exit {listed.returncode}): {listed.stderr.strip()}. "
        "This checkout does not appear to be a working git repository, which this "
        "guard cannot verify anything without."
    )
    all_tags = [line for line in listed.stdout.splitlines() if line]

    if not all_tags:
        pytest.skip(
            "No git tags visible in this checkout at all (shallow clone without "
            f"fetch-tags, or a fresh repo) — cannot distinguish '{expected_tag} was "
            "never tagged' from 'tags simply were not fetched here'. This is "
            "expected in some environments; ci.yml's `test` job sets "
            "`fetch-tags: true` so scheduled/manual runs exercise the real "
            "assertion. Run with full tag history (a normal local clone, or a "
            "checkout with `fetch-tags: true`/`fetch-depth: 0`) to exercise the "
            "real assertion."
        )

    if (
        expected_tag not in all_tags
        and os.environ.get(_GITHUB_EVENT_NAME_ENV_VAR) == "pull_request"
    ):
        pytest.skip(
            "GitHub pull_request run: the manifest's expected tag is not visible, "
            "but PR merge refs can race the asynchronous post-merge release/tag "
            "publication of their base branch. Deferring this repository-global "
            "missing-tag verdict to scheduled/manual/local checks; structural "
            "manifest validation already ran."
        )

    assert expected_tag in all_tags, (
        f"{_MANIFEST} claims version {version!r} but no git tag {expected_tag!r} "
        f"exists ({len(all_tags)} tag(s) visible in this checkout). This is the "
        "exact drift that silently blocked the v1.3.0 release: release-please "
        "treats a manifest bump as 'already shipped' and queues no further work, "
        "so an untagged manifest bump goes unnoticed until someone investigates a "
        "release that never happened. Tag the commit release-please's release PR "
        f"merged (`git tag {expected_tag} <sha> && git push origin {expected_tag}`), "
        "or revert the manifest if the bump was a mistake."
    )


# --- Escape-hatch and race boundaries ----------------------------------------


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("1", True),
        (None, False),
        ("", False),
        ("0", False),
        ("true", False),
        ("TRUE", False),
        (" 1", False),
    ],
)
def test_release_pr_context_decision(env_value: str | None, expected: bool) -> None:
    """The pure boundary: only the exact string "1" arms the escape hatch."""
    assert _release_pr_context(env_value) is expected


def test_guard_skips_inside_release_pr_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env var set to "1", the tag assertion is skipped, not evaluated."""
    monkeypatch.delenv(_GITHUB_EVENT_NAME_ENV_VAR, raising=False)
    monkeypatch.setenv(_RELEASE_PR_ENV_VAR, "1")
    with pytest.raises(pytest.skip.Exception):
        test_manifest_version_has_matching_git_tag()


def test_guard_defers_missing_tag_on_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR missing-tag verdict is deferred instead of racing release publication."""
    monkeypatch.delenv(_RELEASE_PR_ENV_VAR, raising=False)
    monkeypatch.setenv(_GITHUB_EVENT_NAME_ENV_VAR, "pull_request")
    monkeypatch.setattr(sys.modules[__name__], "_load_manifest_version", lambda: "9.9.9")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["git", "tag", "-l"],
            returncode=0,
            stdout="v1.3.2\n",
            stderr="",
        ),
    )

    with pytest.raises(pytest.skip.Exception, match="pull_request"):
        test_manifest_version_has_matching_git_tag()


def test_guard_armed_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside PR/release contexts, the guard runs its real tag assertion and passes.

    The manifest read is pinned to ``1.3.0``, which has a real ``v1.3.0`` tag in
    this repo, so the assertion path runs to a deterministic pass regardless of any
    in-flight release bump.
    """
    monkeypatch.delenv(_RELEASE_PR_ENV_VAR, raising=False)
    monkeypatch.delenv(_GITHUB_EVENT_NAME_ENV_VAR, raising=False)
    monkeypatch.setattr(sys.modules[__name__], "_load_manifest_version", lambda: "1.3.0")
    test_manifest_version_has_matching_git_tag()


def test_guard_bites_on_untagged_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduled drift checks still fail on a genuinely missing manifest tag.

    This runs the real ``git tag -l`` subprocess path with the PR deferral disabled,
    so a future edit that deletes or waters down the assertion turns this test red.
    The manifest read is forced to ``9.9.9`` instead of relying on live release
    state, keeping the failure signal deterministic.
    """
    monkeypatch.delenv(_RELEASE_PR_ENV_VAR, raising=False)
    monkeypatch.setenv(_GITHUB_EVENT_NAME_ENV_VAR, "schedule")
    monkeypatch.setattr(sys.modules[__name__], "_load_manifest_version", lambda: "9.9.9")
    with pytest.raises(AssertionError, match="v9.9.9"):
        test_manifest_version_has_matching_git_tag()
