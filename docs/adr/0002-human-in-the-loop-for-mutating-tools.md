# 0002. Human-in-the-loop approval for mutating tools

- **Status**: Accepted
- **Date**: 2026-11-23

## Context

One allowlisted tool changes state: `POST /api/orders/{orderId}/cancel`. A model can be wrong or
manipulated by injected text, and a cancellation cannot be undone by the AI. Approving a vague
intent ("cancel my order") is not enough: the model could change the target or arguments after the
approval.

## Decision

We will pause the LangGraph run with an `interrupt` before every mutating tool call and show the
user the exact action: tool, target id and arguments as they will be sent. Only an explicit
approval from the token's subject resumes the run, and it covers that single call. Any change to
the action needs a new approval; a rejection, a timeout or an approval from anyone else results in
no call. The triage worker never calls a mutating tool: its `suggestedAction` is advice for a human.

## Consequences

- No state change happens without a human decision on the exact request.
- The assistant needs the PostgreSQL checkpointer so an interrupted run can resume later.
- Approval flows add a round trip to the UI and need eval cases (missing-interrupt cases block the
  merge, ADR 0004).
- New mutating tools inherit this rule automatically; there is no opt-out.
