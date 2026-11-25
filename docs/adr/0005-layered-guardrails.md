# 0005. Layered, deterministic guardrails around the chat model

- **Status**: Accepted
- **Date**: 2026-11-25

## Context

The assistant reads untrusted text from two places: what the customer types and what the tools
return (order notes, product descriptions, notifications). Either can carry a prompt injection.
The model cannot be trusted to resist it: a model that follows an injected instruction must still
be unable to leak secrets, act on someone else's data, cancel an order without approval or run up
cost. Guards that depend on another LLM call add latency, cost and a second thing to attack, and
they cannot be tested without a network. The service runs as a single instance (spec assumption).

## Decision

We will wrap the model in deterministic, independently testable layers, injected as one
`Guardrails` bundle into `ChatGraph`, each with a pass-through `NOOP`:

1. **Input guard**: normalise (NFKC, drop format/control characters) and match versioned regex
   rules (`rules/input.v1.json`) over decoded variants; a match returns a fixed refusal
   (`finishReason: blocked`) before any model call or token reservation.
2. **Tool-result guard**: sanitise and cap every tool result; neutralise delimiters and flag
   injected instructions as data (`sanitized`), never as a block.
3. **Output checks**: a per-run canary in the system prompt (`assistant.v4.md`) plus leak rules
   (canary, tokens, secrets, internal names and hosts, foreign ids, prompt shingles) on every
   streamed segment and the full answer; a leak replaces the answer (`message.replaced`).
4. **Tool guard**: the conversation's stored allowlist, `cancel_order` only for ids the customer
   typed or a tool returned, and no repeated identical calls (`tool_loop`). Mutating calls still
   need the human approval of ADR 0002.
5. **Quotas**: per-user day token budget before the global one (ADR 0003), minute and day message
   limits, one stream per user, and a cooldown after repeated medium/high input blocks.

Every action is a content-free `GuardEvent`: one append-only `guardrail_events` row and one
`guardrail_event` log line. A red-team corpus (`tests/redteam/`) runs through the graph with a
model that obeys every attack, and a coverage test proves each layer is needed (SC-002).

## Consequences

- The guarantees hold whatever the model does, and are proven in the default test run without a
  network; the live run (`-m live_llm`) only reports how often the model itself refuses.
- Regex rules miss paraphrases (e.g. role play that avoids the listed verbs); the output checks
  and tool guard are the backstop, and the corpus grows with each finding. An LLM classifier
  stays future work behind `GUARD_LLM_CLASSIFIER`.
- False positives cost legitimate customers a refusal; the legit set must stay ≥ 95% unblocked
  (SC-005), and the rules file is versioned and hash-pinned so changes are reviewed.
- The minute window and the stream slots live in process; scaling out needs shared counters.
- Guards add < 50 ms at p95 per message (SC-008).
