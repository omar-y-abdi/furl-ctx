"""Every Rust toolchain pin in CI must equal ``rust-toolchain.toml``'s channel.

Why this file exists
--------------------
``rust-toolchain.toml`` pins the compiler so "passes locally" means "passes in
CI". Every workflow that installs Rust repeats that version as a ``toolchain:``
input, and nothing enforced the agreement: it survived only as a "keep in sync
with rust-toolchain.toml" comment, which cannot fail CI.

That is the same defect class as the silently-disarmed test extras and the
unfiltered ``verify/`` path — an invariant living in prose, checked by nobody.
Worse here, because the written bump procedure was itself stale: it named five
``toolchain:`` inputs when there were eight, so following it EXACTLY would bump
five and leave ``perf.yml`` (x2) and ``autofix.yml`` (x1) behind. The
instructions did not merely fail to prevent the drift; followed literally they
produced it.

What a drift actually costs (mechanism verified against the pinned action, not
assumed)
------------------------------------------------------------------------------
The comment this file replaces claimed ``dtolnay/rust-toolchain`` exports
``RUSTUP_TOOLCHAIN``, which would OVERRIDE the pin file and let a stale input
silently win. That is not what the pinned action does. At the SHA used by all
eight sites it never reads ``rust-toolchain.toml`` and never sets
``RUSTUP_TOOLCHAIN`` (zero occurrences at that SHA, at ``@master``, at ``@v1``,
and in a repo-wide code search). It runs ``rustup toolchain install <input>``
then ``rustup default <input>`` — and ``rustup default`` is the LOWEST-precedence
selector rustup has. A ``rust-toolchain.toml`` in the working directory outranks
it, so in-repo cargo invocations follow the channel, not the input.

So a drifted input does not silently swap the compiler. It does four other
things: the job installs a toolchain it will not use and then fetches the
channel's toolchain again on the first in-repo cargo call; the action's
``cachekey`` output is derived from ``rustc +<input>``, a compiler the build
never runs; any cargo invocation outside the repo directory uses the drifted
default; and — the part that matters most — the channel only wins because the
file is present. Delete or rename ``rust-toolchain.toml`` and the drifted inputs
immediately and silently govern every job.

Keeping the sites equal to the channel is therefore what makes the pin's
guarantee hold, rather than a second copy of it that happens to agree.

Derived, not enumerated
-----------------------
This module never hard-codes a version or a list of sites. It reads the channel
from ``rust-toolchain.toml`` and discovers every pin by walking the workflow
YAML, so a workflow added tomorrow with a ``toolchain:`` input is covered
without anyone remembering — and the count can never go stale the way the prose
did, because nothing counts.

It parses the YAML STRUCTURE rather than grepping text, for a specific reason:
``rust.yml`` carries a comment mentioning ``@1.95.0`` (explaining why the action
REF cannot be pinned that way), and a text scan would count that comment as a
pin site. A mention is not a pin, exactly as a docstring naming a module is not
an import.

Pure stdlib + PyYAML: the module never imports ``furl_ctx`` or the compiled
extension, so no assertion here depends on the Rust build. That independence is
module-level only, and the distinction is worth stating because the obvious
reading is wrong — the repo's ``conftest.py`` hard-exits the whole session when
``furl_ctx._core`` is unimportable, so pytest still needs a built extension to
reach these tests. Verified rather than assumed: collecting this file in a fresh
worktree exits on that guard until the extension is supplied.

Every failure to read or parse an input is a hard failure, never a skip: a sync
guard that passed vacuously because it found nothing to compare would be worse
than no guard at all.

The discovery step is itself guarded, because comparing is the easy half. The
version assertion proves every site the walk FOUND agrees with the channel; it
cannot prove the walk found every site, and a non-empty result set passes even
if the walk silently degraded to two sites out of eight. Two derived
cross-checks close that: every workflow naming the action must contribute at
least one parsed pin, and the walk's count must equal an independent anchored
scan for the ``toolchain:`` key. Both numbers come from the tree at run time,
so neither can rot the way the prose did.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib

try:
    import yaml
except ModuleNotFoundError as exc:
    # Hard-fail rather than skip because PyYAML is a declared development dependency.
    raise ModuleNotFoundError(
        "tests/test_toolchain_pin_sync.py requires PyYAML to parse the workflows "
        "(declared as `pyyaml>=6` in the [dev] optional-dependencies). Install it with "
        "`pip install -e .[dev]`; this guard must never be silently skipped."
    ) from exc

_ROOT = Path(__file__).resolve().parents[1]
_RUST_TOOLCHAIN_TOML = _ROOT / "rust-toolchain.toml"
_WORKFLOWS_DIR = _ROOT / ".github" / "workflows"
_DEVCONTAINER_JSON = _ROOT / ".devcontainer" / "devcontainer.json"

# The devcontainer feature that installs Rust.
_RUST_FEATURE_MARKER = "features/rust"

# The action every pin site uses.
_ACTION_MARKER = "dtolnay/rust-toolchain"

# An anchored scan for the `toolchain:` KEY at the start of a line. This is NOT the version-string scan the module docstring argues against,
# and the distinction is the whole point. It exists only to cross-check the structural walk's COUNT, never to supply the pins themselves.
_TOOLCHAIN_KEY_RE = re.compile(r"^[ \t]*toolchain:[ \t]*\S", re.MULTILINE)


def _workflow_files() -> list[Path]:
    """Every workflow file on disk, hard-failing rather than returning an empty list."""
    assert _WORKFLOWS_DIR.is_dir(), (
        f"workflow directory not found at {_WORKFLOWS_DIR}; this guard cannot discover the "
        "toolchain pins it must compare against rust-toolchain.toml."
    )
    files = sorted(_WORKFLOWS_DIR.glob("*.yml")) + sorted(_WORKFLOWS_DIR.glob("*.yaml"))
    assert files, (
        f"no workflow files found under {_WORKFLOWS_DIR}; the scan is broken and every "
        "assertion below would pass vacuously."
    )
    return files


def _channel() -> str:
    """The pinned toolchain version from ``rust-toolchain.toml``.

    Hard-fails if the file is missing, unparseable, or carries no channel — the
    comparison below would otherwise have nothing to compare against and every
    assertion would pass for the wrong reason.
    """
    assert _RUST_TOOLCHAIN_TOML.is_file(), (
        f"rust-toolchain.toml not found at {_RUST_TOOLCHAIN_TOML}; this guard cannot verify "
        "that the CI toolchain pins match it. If the file moved, update _RUST_TOOLCHAIN_TOML."
    )
    data = tomllib.loads(_RUST_TOOLCHAIN_TOML.read_text(encoding="utf-8"))
    channel = data.get("toolchain", {}).get("channel")
    assert isinstance(channel, str) and channel, (
        f"rust-toolchain.toml has no `[toolchain] channel` string (got {channel!r}). That is "
        "the single source of truth every CI pin must equal; without it this guard is blind."
    )
    return channel


def _workflow_toolchain_pins() -> dict[str, str]:
    """Map ``"<workflow>::<job>::step[<n>]"`` -> the step's ``toolchain:`` input.

    Walks the parsed YAML rather than the raw text so a comment that merely
    mentions a version is not mistaken for a pin. Keyed on any step whose ``with:``
    block carries a ``toolchain`` value, so it stays correct if the action is
    swapped for another that takes the same input.
    """
    pins: dict[str, str] = {}
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                with_block = step.get("with")
                if not isinstance(with_block, dict) or "toolchain" not in with_block:
                    continue
                pins[f"{path.name}::{job_name}::step[{index}]"] = str(with_block["toolchain"])
    return pins


def test_every_workflow_toolchain_pin_matches_rust_toolchain_channel() -> None:
    """Every ``toolchain:`` input in every workflow equals the pinned channel.

    Derived: the sites are discovered, never listed, so this cannot rot the way the
    prose enumeration did (it named five inputs when eight existed). A new workflow
    with a pin is covered the moment it lands.
    """
    channel = _channel()
    pins = _workflow_toolchain_pins()
    assert pins, (
        "no `toolchain:` inputs found in any workflow, so this guard has nothing to compare "
        "and would pass vacuously. Either every workflow stopped pinning the toolchain — in "
        "which case rust-toolchain.toml now governs CI and this guard should be re-scoped — or "
        "_workflow_toolchain_pins() no longer understands the workflow shape."
    )
    mismatched = {site: value for site, value in sorted(pins.items()) if value != channel}
    assert not mismatched, (
        f"workflow toolchain pin(s) disagree with rust-toolchain.toml's channel {channel!r}: "
        f"{mismatched}. The pinned `dtolnay/rust-toolchain` never reads rust-toolchain.toml; it "
        "runs `rustup default <input>`, which rust-toolchain.toml outranks, so in-repo cargo "
        "calls still follow the channel. A drifted site therefore installs a toolchain it will "
        "not use, derives the action's `cachekey` from a compiler the build never runs, and "
        "leaves the drifted version as the default for anything invoked outside the repo "
        "directory — and if rust-toolchain.toml is ever removed or renamed, the drifted inputs "
        f"silently govern every job. Update every site to {channel!r}, or change the channel "
        f"and every site together. This test "
        f"discovered {len(pins)} pin site(s); do not maintain that number by hand anywhere."
    )


def test_devcontainer_rust_feature_matches_rust_toolchain_channel() -> None:
    """The devcontainer's Rust feature pins the same version as the channel.

    Separate from the workflow assertion because it is a different file format and
    a different failure: a contributor whose container ships another compiler gets
    "passes locally, fails in CI", which is precisely the split rust-toolchain.toml
    was introduced to close.
    """
    channel = _channel()
    assert _DEVCONTAINER_JSON.is_file(), (
        f"devcontainer.json not found at {_DEVCONTAINER_JSON}; if the devcontainer was removed, "
        "drop this test deliberately rather than leaving it pointing at a missing file."
    )
    config = json.loads(_DEVCONTAINER_JSON.read_text(encoding="utf-8"))
    features = config.get("features")
    assert isinstance(features, dict), (
        f"devcontainer.json has no `features` mapping (got {type(features).__name__}); cannot "
        "verify the Rust feature's pinned version."
    )
    rust_features = {
        name: spec
        for name, spec in features.items()
        if _RUST_FEATURE_MARKER in name and isinstance(spec, dict)
    }
    assert rust_features, (
        f"no devcontainer feature matching {_RUST_FEATURE_MARKER!r} found in "
        f"{sorted(features)}. If the Rust feature was renamed, update _RUST_FEATURE_MARKER; "
        "leaving this unmatched would let the container drift from CI unnoticed."
    )
    mismatched = {
        name: spec.get("version")
        for name, spec in sorted(rust_features.items())
        if spec.get("version") != channel
    }
    assert not mismatched, (
        f"devcontainer Rust feature version(s) disagree with rust-toolchain.toml's channel "
        f"{channel!r}: {mismatched}. The container would ship a different compiler from CI, "
        "reintroducing the 'passes locally, fails in CI' split the pin exists to prevent."
    )


def test_every_workflow_that_installs_rust_contributes_a_parsed_pin() -> None:
    """Guard the DISCOVERY, not just the comparison.

    The version assertion above proves that every site the walk found agrees with the
    channel. It does not prove the walk found every site — a non-empty ``pins`` passes
    even if the walk silently degraded to 2 of 8, and the six unchecked sites would drift
    in green silence. This closes the partial-emptiness case; the other guards in this
    module only close total emptiness.

    Derived, so it cannot rot: any workflow whose text names the action is installing
    Rust and must contribute at least one parsed pin. Nothing is counted or listed.
    """
    contributing = {site.split("::", 1)[0] for site in _workflow_toolchain_pins()}
    referencing = {p.name for p in _workflow_files() if _ACTION_MARKER in p.read_text("utf-8")}
    assert referencing, (
        f"no workflow references {_ACTION_MARKER!r}, so this cross-check compares nothing and "
        "would pass vacuously. Either Rust is no longer installed in CI — in which case this "
        "module should be re-scoped — or the marker is stale."
    )
    uncovered = sorted(referencing - contributing)
    assert not uncovered, (
        f"workflow(s) install Rust but contributed no parsed toolchain pin: {uncovered}. The "
        "structural walk reads jobs -> steps -> `with:` -> `toolchain`, so it goes blind if a "
        "site moves anywhere else: a job-level `with:` on a reusable workflow, an `env:` "
        "assignment, a matrix expression like ${{ matrix.rust }} in place of a literal, or a "
        "composite action outside .github/workflows/. Any of those yields fewer sites and a "
        "still-green version assertion. Teach _workflow_toolchain_pins() the new shape — do "
        "not silence this by narrowing the marker."
    )


def test_structural_walk_finds_every_anchored_toolchain_key() -> None:
    """The structural walk's count must equal an independent anchored key scan.

    Two instruments that fail differently: the walk understands YAML but only one shape,
    the scan understands one line pattern but no structure. Agreement is evidence the walk
    is complete; disagreement in EITHER direction means a pin exists somewhere the walk
    does not look, or the walk is counting something the file does not literally declare.

    Both numbers are derived from the tree at run time. Neither is maintained by hand,
    which is the same discipline the version assertion uses — applied to discovery.
    """
    pins = _workflow_toolchain_pins()
    anchored = sum(len(_TOOLCHAIN_KEY_RE.findall(p.read_text("utf-8"))) for p in _workflow_files())
    assert anchored, (
        "no anchored `toolchain:` key found in any workflow, so this cross-check has nothing "
        "to compare and would pass vacuously."
    )
    assert anchored == len(pins), (
        f"the structural walk found {len(pins)} toolchain pin(s) but an anchored scan for the "
        f"`toolchain:` key found {anchored}. These must agree. Fewer structural than anchored "
        "means the walk is blind to a real pin — most likely a site that is not "
        "jobs -> steps -> `with:` -> `toolchain`, which would then drift unchecked while this "
        "suite stayed green. More structural than anchored means the walk is synthesising a "
        "pin the file does not literally declare. Note this scan matches the KEY, never the "
        "version: it cannot be fooled by rust.yml's comment mentioning @1.95.0, because a "
        "comment line starts with `#` and fails the anchor. Reconcile the two rather than "
        "adjusting either number."
    )
