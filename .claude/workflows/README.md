# Workflow library (this project)

> Reuse: `Workflow({ name: '<name>', args: {...} })`. Drafts live in `_drafts/`.

> Find one before building: `python3 <skill>/scripts/wf_lib.py find <keyword>`.


## _drafts

- **headroom-adversarial-verify** — Independent adversarial verification of compression claims on fresh out-of-sample data, with a second agent auditing the harness for cheating.
- **headroom-amputation-map** — Map a forked repo to a keep-set: evidence-backed KEEP/DROP/TRIM verdicts + ordered delete plan. — args: groups, keepset, repo
- **headroom-codebase-map** — Read-only recon: parallel subsystem mappers -> one tight navigation map (where everything is + what it does + key file:line + build/bench) to prime downstream eval agents.
- **headroom-engine-map** — Map a compression engine\ — args: facets, repo
- **headroom-fix-weaknesses** — Fix the held-out-verified weaknesses: granular per-chunk CCR retrieval, and strict/honest verification + benchmarks.
- **headroom-fullscale-review** — Two fable agents review + fix the Rust engine and the Python layer, optimizing without breaking the green build/tests/contracts.
- **headroom-implement** — opus plans INDEPENDENT disjoint-file units (dependents bundled) -> parallel sonnet TDD builders commit in isolated worktrees. Orchestrator integrates + verifies (no agent touches main git).
- **headroom-max-compression** — Two fable agents maximize compression (lossless frontier, then lossy-recoverable frontier) within the recovery + cache-safety + parity contracts.
- **headroom-parallel-eval** — Loop-until-dry fleet of isolated MEASURED experiment agents (optimize|break|quality) with an anti-repeat ledger; opus synthesizes a ranked action doc.
- **headroom-recoverability-refute** — Adversarially refute "no silent loss / every dropped item is CCR-recoverable" by constructing counterexamples. — args: angles, repo
- **simplify-audit** — Lazy-dev over-engineering audit of the Headroom codebase: per-area reachability + complexity scan, adversarially verify the big delete claims, rank biggest-safe-cut-first. Report-only.

## amputate

- **headroom-amputation-map** — Map a forked repo to a keep-set: evidence-backed KEEP/DROP/TRIM verdicts + ordered delete plan. — args: groups, keepset, repo

## cleanup

- **simplify-audit** — Lazy-dev over-engineering audit of the Headroom codebase: per-area reachability + complexity scan, adversarially verify the big delete claims, rank biggest-safe-cut-first. Report-only.

## compression

- **headroom-engine-map** — Map a compression engine\ — args: facets, repo
- **headroom-max-compression** — Two fable agents maximize compression (lossless frontier, then lossy-recoverable frontier) within the recovery + cache-safety + parity contracts.
- **headroom-recoverability-refute** — Adversarially refute "no silent loss / every dropped item is CCR-recoverable" by constructing counterexamples. — args: angles, repo

## eval

- **headroom-codebase-map** — Read-only recon: parallel subsystem mappers -> one tight navigation map (where everything is + what it does + key file:line + build/bench) to prime downstream eval agents.
- **headroom-implement** — opus plans INDEPENDENT disjoint-file units (dependents bundled) -> parallel sonnet TDD builders commit in isolated worktrees. Orchestrator integrates + verifies (no agent touches main git).
- **headroom-parallel-eval** — Loop-until-dry fleet of isolated MEASURED experiment agents (optimize|break|quality) with an anti-repeat ledger; opus synthesizes a ranked action doc.

## review

- **headroom-fullscale-review** — Two fable agents review + fix the Rust engine and the Python layer, optimizing without breaking the green build/tests/contracts.
