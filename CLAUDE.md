# CLAUDE.md — Instructions for the Claude AI Assistant

This file contains standing instructions for Claude (via Claude Code or any Claude agent) when working on this codebase. All rules below apply automatically unless the user explicitly overrides them for a specific request.

---

## Rule: Multiple-point requests → parallel agents

Whenever the user provides a message with multiple numbered or bulleted points (more than 1 point), Claude must:

- Spawn a **separate agent** for **each point simultaneously** (in parallel, in a single message)
- Never handle multiple investigation/fix points sequentially or inline in one agent
- Explanations and clarifications **can** be handled inline by Claude directly (no agent needed)
- Git/deployment steps **can** be handled inline
- This applies to both investigation tasks **and** fix tasks

**Example trigger phrases:** numbered lists, "couple of things", "a few issues", "investigate these", any message with 3+ distinct tasks.

### Examples

**Triggers parallel agents:**
- "Look into 1) the login bug 2) the slow dashboard query 3) the broken CSV export"
- "A couple of things — the API is returning 500s and the tests are failing"
- "Investigate these issues: ..."

**Handled inline (no agent needed):**
- Answering a question or explaining a concept
- Running git commits, pushes, or deploys
- A single-point request, even if it is complex
