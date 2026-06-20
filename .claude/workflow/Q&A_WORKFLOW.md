---
name: Q&A_WORKFLOW
version: 2.0.0
description: Clarification hub. Answer questions, and resolve any uncertainty about user intent before any other workflow runs.
steps: 3
phases:
  - clarify
  - respond
  - escalate
success_criteria:
  - "User intent is unambiguous before work begins"
  - "Question answered clearly and completely"
  - "Routed to the correct workflow when the task is not a plain question"
philosophy_alignment:
  - principle: Ask, do not guess
    application: Surface every uncertainty as a question instead of picking a silent default
  - principle: Ruthless Simplicity
    application: Minimal overhead for plain questions
  - principle: Right-Size Response
    application: Match effort to task complexity
customizable: true
---

# Q&A Workflow

Two jobs:

1. Answer questions that need no code changes.
2. Act as the **clarification hub** for every other workflow. When intent is unclear, the answer is a question to the user, never a guess.

---

## Routing Gate (read first, every task)

Before running any workflow, resolve intent:

| Situation | Action |
|-----------|--------|
| Task is clear AND is a plain question | Answer here (Q&A). |
| Task is clear AND is clearly implementation | Go to DEFAULT_WORKFLOW. |
| Task needs code exploration to be understood | Go to INVESTIGATION_WORKFLOW, then return here if intent is still unclear. |
| Multiple valid interpretations exist | **Stop. Ask the user** with the `AskUserQuestion` tool. Do not pick one. |
| Any important detail is unspecified | **Stop. Ask the user.** Do not choose for them. |

**Hard rule:** Only enter DEFAULT_WORKFLOW when the task is unambiguously an implementation task. If unsure whether it is implementation, clarify here or investigate first — never assume.

**Why this exists:** Silent assumptions are the main failure mode. Picking an interpretation that turns out wrong wastes a whole work cycle. One question up front is cheaper than rework.

---

## When This Workflow Applies

Use Q&A when **any** of these hold:

1. Intent is unclear and needs clarification before work can start.
2. The task can be fully answered in a single response.
3. No files need to be created or modified.
4. The answer is in context or general knowledge — no need to trace code paths.

### Keywords suggesting Q&A

- "What is..."
- "Explain briefly..."
- "Quick question..."
- "How do I run..."
- "What does X mean..."
- Intent is unclear or the request has more than one reading.

### Not Q&A — route elsewhere

| Request | Route |
|---------|-------|
| "Help me understand X" / "What's wrong with this code?" | INVESTIGATION (needs exploration) |
| "Add / Fix / Create X" and the task is clear | DEFAULT (implementation) |
| "Add / Fix / Create X" but the task is ambiguous | Ask here first, then DEFAULT |

---

## The 3 Steps

### Step 1: Clarify

- Confirm whether this is a plain question or a disguised task.
- Check intent for ambiguity. If more than one interpretation exists, list them and ask the user with `AskUserQuestion`.
- Confirm a single-response answer is possible.
- Confirm the answer is available in current context.

If clarity is missing and exploration would resolve it, run INVESTIGATION_WORKFLOW, then come back. If clarity is still missing after that, ask the user. **Never guess.**

### Step 2: Respond

- Answer directly and clearly.
- Keep the response sized to the question.
- Include code examples or file references only when they help.

### Step 3: Escalate

- Confirm the answer fully addresses the question.
- If a follow-up needs deeper understanding → offer INVESTIGATION_WORKFLOW.
- If a follow-up needs implementation → confirm scope, then offer DEFAULT_WORKFLOW.

---

## Escalation Examples

**Q&A → INVESTIGATION**

```
User: "What does the cleanup agent do?"
[Answer provided]
User: "How does it integrate with the other hooks?"
→ "That needs code exploration. Switching to INVESTIGATION_WORKFLOW."
```

**Q&A → DEFAULT**

```
User: "What's the recommended way to add a command?"
[Answer provided]
User: "Add a /status command for me."
→ "Clear implementation task. Switching to DEFAULT_WORKFLOW."
```

**Ambiguity → ask, do not guess**

```
User: "Make the cursor faster."
→ Two readings: animation speed, or response latency? Ask with AskUserQuestion
   before touching any code.
```

---

## Success Criteria

- Intent is unambiguous before any work starts.
- Question fully answered, response right-sized.
- Routed to the correct workflow when the task was not a plain question.
