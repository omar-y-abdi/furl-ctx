---
name: INVESTIGATION_WORKFLOW
version: 3.0.0
description: 5-phase workflow for systematic investigation and understanding
steps: 5
phases:
  - scope-definition
  - exploration-strategy
  - parallel-deep-dives
  - verification
  - synthesis
success_criteria:
  - "All investigation questions answered"
  - "Understanding verified through testing"
  - "Findings synthesized into clear, evidence-backed answers"
philosophy_alignment:
  - principle: Analysis First
    application: Understand before building
  - principle: Ask, do not guess
    application: When the goal itself is unclear, route to Q&A and ask the user
  - principle: Parallel Execution
    application: Phase 3 uses parallel agent exploration
references:
  workflows:
    - Q&A_WORKFLOW.md
    - DEFAULT_WORKFLOW.md
---

# Investigation Workflow

For exploration and understanding. Use when the task is "understand X", not "build X".

**Keywords:** investigate, explain, understand, how does, why does, analyze, research, explore.

**Not for development.** If the task is implement / build / create / add and is clear, use DEFAULT_WORKFLOW. If the goal of the investigation itself is unclear, go to Q&A_WORKFLOW and ask the user before exploring — do not guess what to look into.

## Handoff (context survival)

Keep one running handoff file at `.claude/runtime/handoff.md`. Its job: survive a compaction without losing the thread.

- Record **what the user wants answered and why**, the open questions, key findings so far, and the next action.
- Write to it as you go. Write **more often as the context window fills** — that is when forgetting starts.
- Before trimming bloat, move anything still important to the top. Always keep it current.
- It is normally reloaded into context after a compaction if it was edited beforehand, so keeping it fresh is what makes progress persistent.

---

## Phase 1: Scope Definition

**Goal:** Define investigation boundaries before any exploration.

- Identify the explicit user questions — what must be answered?
- If the scope is ambiguous, route to Q&A and ask the user. Do not assume what they meant.
- Define success criteria (e.g. "can explain the system flow", "can diagram the architecture").
- Set boundaries: in scope vs. out of scope.
- Estimate depth: surface-level vs. deep dive.

**Exit:** Clear question list + defined scope + measurable success criteria.

---

## Phase 2: Exploration Strategy

**Goal:** Plan agent deployment — prevent random exploration.

- Use **architect** to design the exploration strategy.
- Use **patterns** to check for similar past investigations.
- Select agents for parallel deployment in Phase 3.
- Build a prioritized roadmap and note likely dead ends to avoid.

**Agent selection by investigation type:**

| Type | Agents |
|------|--------|
| Code understanding | analyzer, patterns |
| System architecture | architect |
| Performance issues | optimizer, analyzer |
| Security concerns | security, patterns |
| Integration flows | integration, database |

**Exit:** Exploration roadmap + agent plan + prioritized areas.

---

## Phase 3: Parallel Deep Dives

**Uses parallel execution by default.**

- Deploy the selected agents in parallel per the Phase 2 strategy.
- Each agent explores independently.
- Collect findings, identify connections, note unexpected discoveries.

**Parallel patterns:**

```
"How does the reflection system work?"
→ [analyzer(hooks), patterns(reflection), integration(logging)]

"Why is the build failing?"
→ [analyzer(build-config), patterns(build-failures), fix-agent(errors)]

"Understand the push-to-talk flow"
→ [analyzer(Voice/), patterns(audio), integration(CGEvent-tap)]
```

**Exit:** All agents complete + findings collected + connections identified.

---

## Phase 4: Verification

**Goal:** Test understanding through practical application.

- Form hypotheses from the Phase 3 findings.
- Design practical tests: trace code paths, examine logs, check edge cases.
- Run the tests, document results, fill gaps.

**Exit:** Hypotheses tested + understanding verified + gaps filled.

---

## Phase 5: Synthesis

**Goal:** Compile findings into clear answers to the Phase 1 questions.

- Use **reviewer** to check completeness.
- Answer each Phase 1 question directly.
- Create diagrams or flow charts if they help.
- Note remaining uncertainties.

**Output structure:**

1. Executive summary (2-3 sentences)
2. Detailed explanation with evidence
3. Visual aids (optional)
4. Key insights (non-obvious discoveries)
5. Remaining unknowns

**Exit:** All Phase 1 questions answered, explanation clear and evidence-backed.

---

## Transitioning to Development

After investigation, if implementation is needed and the task is clear, transition to DEFAULT_WORKFLOW:

- Resume at RESEARCH if design guidance is clear.
- Resume at PLAN if more design work is needed.
- If the implementation task is still ambiguous after investigating, go to Q&A and ask the user first.

```
Task: "investigate the cursor flight, then make it smoother"
→ INVESTIGATION (5 phases) → understand the animation
→ if "smoother" is ambiguous → Q&A → ask the user
→ DEFAULT (resume at RESEARCH) → implement
```
