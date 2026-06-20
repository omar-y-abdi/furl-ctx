---
name: CONSENSUS_WORKFLOW
version: 2.0.0
description: DEFAULT_WORKFLOW augmented with multi-agent consensus at critical decision points
phases:
  - classify
  - research-with-debate
  - plan-with-debate
  - implement-with-n-version
  - verify-with-expert-panel
  - deliver-with-expert-panel
success_criteria:
  - "All DEFAULT phases completed with consensus augmentation"
  - "Multi-agent debate for ambiguous requirements and design"
  - "Expert panel approval at critical gates"
  - "N-version programming for critical code"
  - "PR is mergeable with panel approval"
philosophy_alignment:
  - principle: Reduced Risk
    application: Multiple perspectives catch errors single approach misses
  - principle: Evidence-Based Decisions
    application: Consensus requires reasoned debate with supporting evidence
  - principle: Quality Over Speed
    application: Accept higher latency for correctness and thoroughness
references:
  workflows:
    - DEFAULT_WORKFLOW.md
customizable: true
---

# Consensus-Augmented Workflow

This workflow extends DEFAULT_WORKFLOW.md with consensus mechanisms at critical decision points. **All DEFAULT phases apply** — this file defines only the consensus augmentations.

## When to Use

- Requirements are ambiguous or complex
- Design decisions have significant architectural impact
- Multiple valid implementation approaches exist
- Critical code requires extra validation (security, financial, safety-critical)
- Public APIs with long-term commitments

## Consensus Mechanisms

Three mechanisms are available. Each is triggered at specific phases below.

### Multi-Agent Debate

**When:** Ambiguous requirements, complex design decisions, architectural choices.

**Protocol:**
1. Deploy 3-5 domain-relevant TeamCreate teammates
2. **Round 1 — Independent Analysis**: Each teammate presents their interpretation/proposal
3. **Round 2 — Cross-Examination**: Challenge assumptions, identify conflicts, explore edge cases
4. **Round 3 — Consensus Building**: Synthesize unified view, resolve conflicts
5. **Orchestrator**: Break deadlocks, document final decision with rationale

**Output:** Decision document with: what was decided, why, alternatives rejected, trade-offs accepted.

### N-Version Programming

**When:** Critical code paths (security checks, financial calculations, data integrity).

**Protocol:**
1. Identify critical code sections from design
2. Deploy 2-3 independent builder teammates
3. Each implements the same spec independently
4. Cross-validate: compare logic, edge cases, error handling, performance
5. Synthesize best approach from all versions
6. Majority approval required

**Output:** Validated critical code with cross-implementation verification.

### Expert Panel Review

**When:** Refactoring validation, PR review, philosophy compliance.

**Protocol:**
1. Deploy panel of relevant expert teammates (reviewer, security, optimizer, patterns, tester)
2. Independent parallel reviews
3. Consolidate findings — prioritize critical vs. optional
4. Unanimous agreement on required changes
5. Document panel decision

**Output:** Consolidated review with clear mandatory vs. optional action items.

---

## Phase Augmentations

Follow DEFAULT_WORKFLOW.md phases. At each phase below, apply the specified consensus mechanism BEFORE proceeding to the next phase.

### Phase 1: CLASSIFY

Follow DEFAULT Phase 1. Classification LARGE automatically selects this workflow.

### Phase 2: RESEARCH — with Multi-Agent Debate

**CONSENSUS TRIGGER:** If requirements are ambiguous, complex, or involve multiple stakeholders.

Follow DEFAULT Phase 2 (prompt-writer, analyzer, ambiguity agents), then:

- [ ] **IF AMBIGUOUS → Multi-Agent Debate:**
  - Deploy 3-5 teammates: architect, security, api-designer, database, tester
  - Round 1: Each presents their interpretation of requirements
  - Round 2: Challenge each other's assumptions
  - Round 3: Synthesize consensus requirements
  - Orchestrator: resolve remaining conflicts
- [ ] Document consensus requirements with rationale
- [ ] **CRITICAL:** Pass consensus requirements to ALL subsequent teammates

### Phase 3: PLAN — with Multi-Agent Debate (MANDATORY)

**MANDATORY CONSENSUS TRIGGER:** Design decisions have long-term architectural impact.

Follow DEFAULT Phase 3 (architect, retcon docs, issue/branch), then:

- [ ] **Multi-Agent Debate for Architecture:**
  - Deploy teammates: architect, api-designer, database, security, tester
  - Round 1 — Independent Analysis:
    - architect: system architecture, module boundaries, patterns
    - api-designer: API contracts, interfaces, integration points
    - database: data models, schemas, query patterns
    - security: threat model, security requirements
    - tester: testability analysis, TDD approach
  - Round 2 — Cross-Examination: identify conflicts, question trade-offs, explore edge cases
  - Round 3 — Consensus Building: resolve conflicts, create integrated design spec
  - Orchestrator: finalize design, document dissenting opinions
- [ ] Record consensus decision log: what, why, alternatives rejected, trade-offs accepted
- [ ] Write consensus-validated design to state bridge

### Phase 4: IMPLEMENT — with N-Version Programming

**CONSENSUS TRIGGER:** Implementation involves critical code paths.

Follow DEFAULT Phase 4 (TDD → build → refactor → commit), with augmentation:

- [ ] **IF CRITICAL CODE → N-Version Programming:**
  - Identify critical sections: security, financial, data integrity, safety-critical
  - Deploy 2-3 independent builder teammates with same spec
  - Cross-validate implementations: logic, edge cases, error handling, performance
  - Synthesize best solution from all versions
  - Majority approval required for critical code
- [ ] **Expert Panel for Refactoring (Phase 4c):**
  - Deploy panel: cleanup, optimizer, reviewer, patterns teammates
  - Provide all teammates with original user requirements
  - Round 1: each proposes simplifications independently
  - Round 2: cross-check — verify no requirements violated
  - Round 3: unanimous agreement on final refactoring plan
  - Apply only consensus-approved changes

### Phase 5: VERIFY — with Expert Panel (MANDATORY)

**MANDATORY CONSENSUS TRIGGER:** Verification requires expert panel validation.

Run DEFAULT Phase 5 verification chain (build → typecheck → lint → test), then:

- [ ] **Expert Panel Review:**
  - Deploy panel: reviewer, security, optimizer, patterns, tester teammates
  - Parallel independent reviews:
    - reviewer: code quality, philosophy compliance, requirements validation
    - security: vulnerability assessment, threat validation
    - optimizer: performance analysis, bottleneck identification
    - patterns: pattern compliance, best practices
    - tester: test coverage analysis, test quality
  - Consolidate findings, prioritize issues
  - Panel must agree on required vs. optional changes
- [ ] **Expert Panel Philosophy Check:**
  - Deploy: reviewer, patterns, cleanup teammates
  - Verify ruthless simplicity, clean module boundaries, zero-BS
  - Unanimous approval required
- [ ] Address ALL required changes, iterate until panel approves
- [ ] Post consolidated review as PR comment

### Phase 6: DELIVER — with Expert Panel

Follow DEFAULT Phase 6 (draft PR → review → finalize), with augmentation:

- [ ] **Expert Panel Final Quality Gate:**
  - Provide all teammates with original user requirements
  - Deploy: cleanup, reviewer, patterns teammates
  - Unanimous approval: no dead code, no stubs, requirements preserved
- [ ] Include in PR description:
  - Consensus decisions made and rationale
  - Which consensus mechanisms were used
  - Trade-offs accepted
  - Critical code sections validated via N-Version
- [ ] Tag PR with `consensus-validated` label

**Consensus Commit Message Format:**

```
feat(scope): brief description

Detailed description of changes.

Consensus Mechanisms Used:
- Multi-Agent Debate: [requirements/architecture]
- N-Version Programming: [critical sections]
- Expert Panel: [refactoring/review/philosophy]

Resolves #123
```

---

## Performance vs. Quality

This workflow is slower but produces higher quality code. Use strategically:

- **DEFAULT_WORKFLOW** for most tasks (faster, sufficient quality)
- **CONSENSUS_WORKFLOW** for critical/complex tasks (thorough, maximum quality)

Switch by updating `selected` in USER_PREFERENCES.md.

---

**Remember:** Consensus adds rigor but also latency. Use for tasks where correctness and quality justify the extra time investment.
