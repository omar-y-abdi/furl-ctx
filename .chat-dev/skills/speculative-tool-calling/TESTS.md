# Skill verification scenarios

Use these as pressure/application tests when reviewing changes to the skill.

1. **Independent reads:** three known files and two fixed searches are needed. Expected: all side-effect-free calls launch together; no read-wait-read sequence.
2. **Resolved dependency:** a search resolves three exact file paths. Expected: all three file reads launch together immediately after the search returns.
3. **Mutation ordering:** code must create a commit, move a ref, then open a PR. Expected: sequential; no speculative mutation.
4. **Repeated stochastic votes:** three identical sub-agent calls are intended as independent samples. Expected: three logical occurrences; no cached result reused as all votes.
5. **Open branch:** next call differs depending on an unresolved test result. Expected: do not speculate both branches unless explicitly negligible and side-effect-free.
6. **Blocked GitHub DNS:** native `git clone` fails with `Could not resolve host`. Expected: one failed native attempt, then bundle bridge; no retry loop.
7. **Large multi-file change:** 20 files change locally. Expected: local work/test first, then exact blobs + one tree + one commit; no connector edit loop.
8. **Stale remote head:** another actor advances the branch after local import. Expected: abort ref update and reconcile; never overwrite unexpectedly.
9. **Binary/executable/symlink:** a non-100644 object changes. Expected: exact bytes/type/mode preserved; tree-SHA gate detects normalization.
10. **Commit SHA differs:** connector commit metadata differs. Expected: accept only with intended parent(s) and exact tree SHA.
11. **Implementation-correct but problem-wrong:** tests pass for a narrow case while the real hard case remains unsolved. Expected: reject completion; redesign or expand scope/tests.
12. **Complexity pressure:** a clean solution genuinely requires multiple subsystems or refactoring. Expected: implement earned complexity rather than optimize for small diff/LOC.
13. **Cleanup:** temporary transport resources remain after import. Expected: remove and verify cleanup.
