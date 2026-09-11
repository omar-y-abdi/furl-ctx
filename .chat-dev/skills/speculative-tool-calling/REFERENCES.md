# References and rationale

## Speculative Programmatic Tool Calling

Inspired by Alex Zhang (MIT CSAIL), “Speculative Programmatic Tool Calling,” Aug 2026:
https://alexzhang13.github.io/blog/2026/spec-ptc/

Related work cited there includes Conveyor (Xu et al., 2024), Speculative Interaction Agents (Hooper et al., 2026), and AsyncFC (Feng et al., 2026).

The skill uses the general futures/promises pattern: launch safe high-latency work once inputs are known, reuse the in-flight/result object when execution reaches the call, and keep side effects outside speculation.

## GitHub bridge evidence

The bridge workflow was empirically verified in ChatGPT's restricted sandbox:

- GitHub Actions produced a complete `git bundle` and uploaded it as an artifact.
- The GitHub connector downloaded the artifact into the sandbox.
- Native local Git cloned the bundle and operated on full history/refs without GitHub network access.
- A locally edited/staged/committed file produced the same Git blob SHA on GitHub.
- The GitHub-created tree SHA exactly matched the local commit tree SHA.
- A PR created from the published tree showed the same patch as the local Git diff.

Therefore tree-SHA identity, not commit-SHA identity, is the primary content-integrity invariant when connector-created commit metadata differs.
