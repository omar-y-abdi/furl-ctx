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
empty list whether or not ``v1.3.0`` really exists upstream. Asserting drift from
that signal would be a false positive against every shallow checkout, so this test
tells the two cases apart: zero tags visible at all is "no signal" and SKIPS (never
silently passes, never fails), while at least one tag visible with the expected one
absent is real drift and FAILS. A guard that silently passed in a tag-less checkout
would be worse than no guard at all: this repo's ``ci.yml`` ``test`` job (which runs
this file as part of `pytest tests`) sets ``fetch-tags: true`` on its checkout step
specifically so this guard actually arms there instead of skipping on every PR — see
the comment beside that setting.

Pure stdlib; shells out to the system ``git`` binary. A missing manifest, a manifest
that fails to parse, or a manifest missing the ``.`` package is a hard failure,
never a skip — only "we cannot see any tags at all" skips.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / ".release-please-manifest.json"

# release-please-config.json pins this today: include-v-in-tag=true,
# include-component-in-tag=false, single package at ".". If either ever changes,
# the tag-name derivation below must change with it — this constant is the single
# edit point, and a renamed/added package key fails loudly below rather than being
# silently skipped over.
_MANIFEST_PACKAGE_KEY = "."


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
            "`fetch-tags: true` specifically so this guard arms there instead of "
            "skipping. Run with full tag history (a normal local clone, or a "
            "checkout with `fetch-tags: true`/`fetch-depth: 0`) to exercise the "
            "real assertion."
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
