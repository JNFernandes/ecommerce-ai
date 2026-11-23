# 0001. Call the platform with the caller's token, not a service account

- **Status**: Accepted
- **Date**: 2026-11-23

## Context

The MCP tools and the assistant call the Orders, Inventory, Notifications and Identity APIs through
the gateway. A model chooses which tool to call and with which arguments, and its input includes
untrusted text (user messages, tool results, events). If the tools ran with a service account or an
admin token, a prompt injection or a model error could read or change data the user cannot reach
(confused deputy). Each platform service already enforces ownership and roles on its own API.

## Decision

We will forward the caller's own RS256 access token, verified locally against the Identity JWKS,
on every tool call. Tools never use a service account, an admin token or any other credential.
The token is never put into a prompt, a trace or a tool result shown to the model. The triage
worker, which has no caller, works from event payloads only and calls no tool until
`ecommerce-identity` documents a read-only service identity.

## Consequences

- The AI can never see or do more than the user; ownership stays enforced by the owning service.
- Tools are thin: no authorization logic of their own beyond the allowlist.
- A tool call fails when the caller's token expires mid-conversation; the assistant must surface
  that as a re-authentication need.
- The triage worker cannot enrich explanations with API data until a service identity exists
  (`TODO(WORKER_SERVICE_IDENTITY)` in the constitution).
