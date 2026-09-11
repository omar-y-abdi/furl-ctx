---
name: speculative-tool-calling
description: Use when a task involves more than one tool call, when independent reads/searches/checks can overlap, when designing agent harnesses or code-mode orchestrators, or when doing substantial GitHub repository work from a sandbox where the connector works but direct clone/fetch/push is unavailable or unreliable.
---

# Speculative Tool Calling + GitHub Workspace Bridge

## Overview

Overlap latency without corrupting state. **Launch safe calls as soon as their inputs are known; keep mutations causally ordered. For GitHub work, develop and verify in a real local checkout, then publish the verified Git objects once.**

Implementation correctness is not enough: verify the solution addresses the difficult version of the user's problem. Complexity must earn its existence.

## Execution Policy

Before serializing tool calls, classify them:

1. **Known-input:** all inputs are already known.
2. **Resolved-dependency:** a prior dependency is already resolved.
3. **Same-shape:** independent reads/searches/checks over N items.
4. **Blocked:** any mutation, unresolved dependency, order-sensitive action, unresolved branch/loop, or expensive guess.

Batch or overlap buckets 1-3 when calls are side-effect-free. Launch them immediately; do not wait for planning prose to finish. Keep bucket 4 sequential.

Never speculate writes, deletes, sends, commits, ref updates, PR mutations, or non-deterministic repeated samples. Repeated stochastic calls need occurrence identity, not one cached result reused N times.

## GitHub Local Workspace Bridge

Use normal native Git when network access works. If `git clone/fetch/push` fails once because the sandbox cannot reach GitHub, stop retrying and bridge through the GitHub connector:

1. On a disposable branch, run a temporary Action with full checkout (`fetch-depth: 0`), `git fetch --all --tags --prune`, `git bundle create repo.bundle --all`, `git bundle verify`, and upload the bundle as a short-lived artifact.
2. Download the artifact through the connector. Locally verify the bundle, clone it, run `git fsck`, and verify expected refs/base SHA.
3. Remove the disposable transport branch/workflow after import.

## Work Locally

Use native Git and local tooling for the engineering loop:

```bash
git switch -c <branch> <base>
# edit, test, lint, build, inspect, refactor
git add -A
git commit
git status
```

Run independent reads/searches/tests concurrently when safe. Publish only a clean, verified local commit.

## Publish the Local Commit

1. Read current GitHub base/head SHA. Unexpected movement means reconcile first.
2. Derive changed paths, exact bytes, modes, object types, deletions, and renames from local Git—not connector file-by-file roundtrips.
3. Create GitHub blobs from exact local bytes; preserve binary data, symlinks, executable bits, and tree modes/types.
4. Create one GitHub tree on the intended base tree.
5. **Hard gate:** GitHub tree SHA must equal local `git rev-parse HEAD^{tree}`. Any mismatch means stop.
6. Create commit, update/create ref, then open/update PR in causal order. Commit SHA may differ because metadata can differ; tree equality is the content guarantee.

Avoid `fetch_file → edit → update_file → repeat`. The local checkout owns repository work; the connector transports final Git objects and manages GitHub state.

## Harness Design

For orchestrators/code-mode systems, mark tools speculatable only when pure and safe to pre-launch. Cache in-flight promises/results by inputs **and logical occurrence**. Keep two overlap mechanisms distinct: generation-time streaming speculation and a JIT pass that parallelizes independent calls already present in the generated plan. Never speculate through impure dependencies or unresolved control flow.

## Verification Gate

Require all before claiming success:

```text
problem solved at intended scope       PASS
local tests/lint/build                 PASS
local working tree                     clean
GitHub tree SHA                        == local HEAD^{tree}
GitHub changed paths / PR diff         == local result
remote base/head                       expected
required CI                            PASS
```

If the architecture only solves an easier toy version, expand/refactor the solution and its adversarial tests before accepting the PR.
