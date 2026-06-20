---
name: DEFAULT_WORKFLOW
version: 3.0.0
description: 6-phase adaptive workflow for feature development, bug fixes, and refactoring
phases:
  - classify task complexity and execution depth
  - research
  - plan
  - implement
  - verify
  - deliver
success_criteria:
  - "Task confirmed as a clear implementation task before entry"
  - "All phases completed for the chosen classification level"
  - "No ambiguity in requirements — clarified up front, never guessed"
  - "Verification chain is the target: build → typecheck → lint → test"
  - "No stubs, placeholders, or TODOs in final code"
  - "Surgical, philosophy-compliant changes"
philosophy_alignment:
  - principle: Ask, do not guess
    application: Enter only on a clear implementation task; otherwise route back to Q&A
  - principle: Ruthless Simplicity
    application: Adaptive depth — trivial tasks skip unnecessary phases
  - principle: Zero-BS Implementation
    application: No stubs or placeholders in deliverables
  - principle: Test-Driven Development
    application: Tests recommended where they add value; not forced for all UI/AppKit code
  - principle: Type-driven Development
    application: Types defined and verified at each phase gate
references:
  workflows:
    - Q&A_WORKFLOW.md
    - INVESTIGATION_WORKFLOW.md
    - CONSENSUS_WORKFLOW.md
customizable: true
---

# Default Coding Workflow

6-phase adaptive workflow for non-trivial code changes. Each phase has entry criteria, objectives, and exit criteria.

## Entry Gate

Enter this workflow **only when the task is unambiguously an implementation task**. If the request has more than one reading, or an important detail is unspecified, go back to Q&A_WORKFLOW and ask the user — do not guess. If the area is unfamiliar, run INVESTIGATION_WORKFLOW first.

## When This Workflow Applies

- New features
- Bug fixes
- Refactoring
- Any non-trivial, clearly-scoped code change

## TodoWrite Integration

Track progress with phase-based todos:

- Format: `Phase N: [Phase Name] - [Specific Action]`
- Break phases into sub-tasks as needed.
- The task is not complete until Phase 6 exits.

## Git Policy

- Commit directly to `main` in distributed commits as work progresses — one commit per coherent unit of work, with a message describing what that commit did.
- No GitHub CLI, no pull requests, no branches, no worktrees — **unless the user explicitly asks for them.**
- Never force-push.

## Handoff (context survival)

Keep one running handoff file at `.claude/runtime/handoff.md`. Its job: survive a compaction without losing the thread.

- Record **what the user wants and how they want it done**, plus decisions made, files touched, and the current step.
- Write to it as you go. Write **more often as the context window fills** — that is when mistakes and forgetting start.
- Before trimming bloat from the file, move anything still important to the top. Always keep it current.
- It is normally reloaded into context after a compaction if it was edited beforehand, so keeping it fresh is what makes progress persistent.

---

## Phase 1: CLASSIFY

**Objective:** Determine task complexity and select execution depth.

**Entry:** A clear implementation task (entry gate passed).

| Level | Criteria | Pipeline |
|-------|----------|----------|
| TRIVIAL | Single-file fix, typo, config change | → Phase 4 |
| SMALL | Clear scope, 1-3 files, well-understood area | → Phase 3 |
| MEDIUM | Multiple files, design decisions needed | Full pipeline (Phases 2-6) |
| LARGE | Architectural impact | Full pipeline + consider CONSENSUS_WORKFLOW |

**Tasks:**

- Read and confirm the request.
- Classify with the table above.
- For MEDIUM/LARGE: list the explicit user requirements that cannot be optimized away.
- Create TodoWrite entries for the relevant phases.

**Exit:** Classification documented. Pipeline depth selected.

---

## Phase 2: RESEARCH

**Objective:** Understand the problem before designing a solution.

**Entry:** MEDIUM or LARGE. (TRIVIAL/SMALL skip this phase.)

**Tasks:**

- Use **analyzer** to understand existing codebase context.
- Use **ambiguity** if requirements are unclear — and if a user decision is needed, route back to Q&A and ask.
- If the area is unfamiliar, run INVESTIGATION_WORKFLOW first, then return.
- Check for applicable Skills with the Skill tool.
- Use **security** to identify security requirements (if applicable).
- Define success and acceptance criteria.
- Pass the explicit requirements to every subsequent agent.

**Exit:** Problem understood. Requirements clear. Success criteria defined.

---

## Phase 3: PLAN

**Objective:** Create an implementation plan before writing code.

**Entry:** SMALL+ with research complete (if applicable). (TRIVIAL skips this phase.)

**Tasks:**

- Use **architect** to design the solution. Use **database** if data modeling is involved.
- Documentation-first: write the docs for the feature as if it already exists, then have **architect** review for alignment and revise.
- Document module specs, risks, and dependencies.
- Create TodoWrite entries for each implementation step.

**Exit:** Implementation plan documented. Todos created.

---

## Phase 4: IMPLEMENT

**Objective:** Build the solution.

**Entry:** Plan complete (or TRIVIAL/SMALL ready for direct work).

### 4a: Tests (where they add value)

- Use **tester** to write tests for logic that benefits from them.
- Testing is recommended, not mandatory for every UI/AppKit change. Prefer tests for pure logic, parsers, and state machines; skip ceremony where it adds no value.

### 4b: Build

- Use **builder** to implement from the plan.
- Use **integration** for external-service connections (if applicable).
- Follow the architecture; use Skills as needed.
- No stubs, no TODOs, no swallowed exceptions.

### 4c: Refactor

- Use **cleanup** for simplification within the user's constraints.
- Use **optimizer** for performance (if applicable).
- Remove dead code and unnecessary abstractions.
- Validate every explicit user requirement is still preserved.

### 4d: Commit

- Stage and commit directly to `main` with a message describing what the commit did.
- Update the handoff file.

**Exit:** Implementation complete. Code committed.

---

## Phase 5: VERIFY

**Objective:** Run the verification chain. It is the **target** for every change.

**Entry:** Implementation committed.

**Note:** `xcodebuild` must not be run from the terminal (it invalidates TCC permissions). So the build/typecheck/test steps are verified by asking the user to build and run in Xcode (Cmd+R / Cmd+U) and report results. Static review steps run on the agent side.

**Verification chain (target — stop and fix on first failure):**

| Step | Check | How |
|------|-------|-----|
| 1 | **Build** | Project compiles (user builds in Xcode) |
| 2 | **Typecheck** | `swift build` / Xcode compile (user-run) |
| 3 | **Lint** | Linter / formatting correct |
| 4 | **Test** | Test suite passes (user runs in Xcode) |
| 5 | **Security** | **security** agent review |
| 6 | **Code Review** | **reviewer** agent: quality, coverage, zero-BS |
| 7 | **Philosophy** | **reviewer** agent: simplicity, boundaries, no stubs |

**Additional:**

- Test like a user — outside-in, covering simple, edge, and regression cases.
- If pre-commit hooks fail: use **pre-commit-diagnostic**.
- Fix every issue found; iterate until the chain is clean.

**Exit:** Static checks pass; user confirms build/test pass. No outstanding issues.

---

## Phase 6: DELIVER

**Objective:** Final quality pass and clear handoff.

**Entry:** Verification chain satisfied.

**Tasks:**

- Use **reviewer** for a final comprehensive review.
- Use **security** for a final security pass.
- Use **cleanup** for a final quality pass (provide the original user requirements).
- Commit any cleanup directly to `main`.
- Update the handoff file with final state.
- Summarize what changed and confirm it is done.

**Only if the user asked for a PR:** create it with `gh` at this point — otherwise stop here.

**Exit:** Work delivered, reviewed, committed to `main`.

---

## Completion Rule

The task is complete when Phase 6 exits: changes committed to `main`, reviewed, and the verification chain satisfied. A bare "code written" is not completion — review and verification are mandatory.

## Notes

- When delegating to agents, always include all requirements, constraints, and context from this workflow.
- For parallel workstreams, prefix todos: `[WORKSTREAM] Phase N: Description`.
