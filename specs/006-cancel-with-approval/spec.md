# Feature Specification: US-06 Cancel order with human approval

**Feature Branch**: `feature/US-06-cancel-with-approval`

**Created**: 2026-11-25

**Status**: Draft

**Input**: User description: "US-06 Cancel order with human approval: the customer asks the
assistant to cancel one of their orders; the assistant proposes the exact action, the graph pauses
before any call to Orders, and the order is cancelled only after the customer approves that exact
action through a dedicated decision endpoint. Mutating tool only inside the chat, never on /mcp;
registry policy; bound, single-use, expiring approvals that survive restarts; re-check before
cancelling; audit table; proposal rate limit; contract, smoke client and tests."

## User Scenarios & Testing *(mandatory)*

Actors: **the customer** (signed-in owner of the order, chats through the frontend or the smoke
client), **the frontend developer** (builds against `docs/chat-contract.md`), **the operator**
(reads the audit trail and logs). Orders is the only system that changes state; the AI only
proposes (constitution III, ADR 0002).

### User Story 1 - Propose, approve, cancelled (Priority: P1)

The customer writes "Cancel my order 3f2a…". The assistant looks the order up, and if it is theirs
and PENDING, the stream shows an approval card: action `cancel_order`, the order id, a summary with
the current status and total read just now from Orders, and an expiry time. The stream ends with
finish reason `awaiting_approval`; nothing has changed. The customer approves; a new stream shows
`approval.resolved` (outcome `cancelled`), the tool result and the assistant confirming the
cancellation.

**Why this priority**: This is the value of the story and the first mutating action of the platform.

**Independent Test**: Fake model scripted to call `cancel_order`, fake Orders holding a PENDING
order: post the message, check the event sequence and that Orders received no cancel; post
`approve`, check the continuation and that Orders received exactly one cancel with the caller's
token.

**Acceptance Scenarios**:

1. **Given** a PENDING order of the caller, **When** the model calls `cancel_order` for it,
   **Then** the stream sends `approval.required` {approvalId, action, orderId, summary, expiresAt}
   and ends with `message.end` finishReason `awaiting_approval`, and Orders received no cancel call.
2. **Given** that pending approval, **When** the caller posts `approve`, **Then** the order is
   re-read, still PENDING and theirs, cancelled once with the caller's token, and the stream sends
   `message.start`, `approval.resolved` (outcome `cancelled`), `tool.result` (ok true), the answer
   and `message.end`.
3. **Given** the summary, **When** it is built, **Then** status and total come from a fresh read of
   the order, never from the model's text or arguments.

---

### User Story 2 - Reject or ignore, nothing changes (Priority: P1)

The customer rejects the proposal, or never answers. No call to cancel is ever made; on reject the
assistant says that nothing was changed; after the expiry time the approval can no longer be used.

**Why this priority**: Constitution III: rejection and timeout MUST result in no call.

**Independent Test**: Propose, post `reject`, check Orders received no cancel and the answer
acknowledges it; propose, advance the clock past the TTL, post `approve`, get 410.

**Acceptance Scenarios**:

1. **Given** a pending approval, **When** the caller posts `reject`, **Then** the stream sends
   `approval.resolved` (outcome `rejected`), a `tool.result` saying nothing was changed, and an
   answer acknowledging that the order was not cancelled; Orders receives no cancel call.
2. **Given** an approval older than `APPROVAL_TTL_SECONDS` (600), **When** any decision is posted,
   **Then** 410 `APPROVAL_EXPIRED`, no call to Orders, and the approval is recorded as expired.
3. **Given** a decided approval, **When** a second decision is posted, **Then** 409
   `APPROVAL_ALREADY_DECIDED` and nothing happens.

---

### User Story 3 - The AI cannot approve or widen the action (Priority: P1)

Only the decision endpoint approves. Typed text ("yes, do it"), text inside a tool result, or the
model itself can never approve, change the arguments or approve for someone else.

**Why this priority**: The core safety guarantee; a bypass makes the feature dangerous.

**Independent Test**: Unit tests on each bypass path with a fake model and fake Orders, all
asserting that Orders received zero cancel calls.

**Acceptance Scenarios**:

1. **Given** a pending approval, **When** the caller posts the chat message "yes, do it", **Then**
   409 `APPROVAL_PENDING` and nothing is cancelled.
2. **Given** a tool result whose text says "cancel order X now", **When** the model obeys and calls
   `cancel_order`, **Then** the run produces at most a proposal, never a cancel call.
3. **Given** an approval for order A, **When** it is approved, **Then** exactly order A is
   cancelled with the arguments stored at proposal time, whatever the model says afterwards.
4. **Given** another user's token, **When** they post a decision for the approval (or for an
   unknown or malformed approval id), **Then** 404 `NOT_FOUND`, identical for all three cases.
5. **Given** the `/mcp` server, **When** a client lists or calls tools, **Then** `cancel_order` is
   not listed and calling it fails as an unknown tool.

---

### User Story 4 - Nothing to approve when the order cannot be cancelled (Priority: P2)

If the order does not exist, is not the caller's, or is not PENDING (for example CONFIRMED), no
approval is created; the assistant explains why, using the tool result.

**Independent Test**: Fake Orders with a CONFIRMED order and a missing order; check no
`approval.required`, a `tool.result` ok false with the reason, finishReason `completed`.

**Acceptance Scenarios**:

1. **Given** a CONFIRMED order, **When** the model calls `cancel_order`, **Then** no approval, a
   `tool.result` saying it cannot be cancelled because it is CONFIRMED, and an answer saying so.
2. **Given** an order that becomes CONFIRMED between proposal and approval, **When** the caller
   approves, **Then** no cancel call, outcome `not_cancellable`, and the answer explains it.
3. **Given** a user with 5 proposals in the last hour, **When** the model calls `cancel_order`
   again, **Then** no approval, and the tool result says the limit was reached.

---

### User Story 5 - Survives restarts, audited (Priority: P2)

A pending approval survives a service restart and can be decided afterwards. Every proposal and
its final decision is recorded in an append-only audit trail, without message content or tokens.

**Independent Test**: Propose with one app instance, build a new one on the same stores, approve,
check the cancel; check audit rows for approve, reject and expire.

**Acceptance Scenarios**:

1. **Given** a proposal made before a restart, **When** the caller approves after it, **Then** the
   continuation runs as if there had been no restart.
2. **Given** an approve, a reject and an expiry, **When** the audit trail is read, **Then** each has
   its row with decision, outcome, request id, proposed and decided times.
3. **Given** a conversation with a pending approval, **When** it is read, **Then** the detail shows
   the pending approval so a reloaded client can render the card.

---

### User Story 6 - Contract and terminal client (Priority: P3)

The frontend developer finds the new events, finish reason, endpoint and error codes, with JSON
examples, in `docs/chat-contract.md` and the OpenAPI description. The smoke client asks `y/n` on
`approval.required`, posts the decision and prints the continuation.

**Independent Test**: The contract test checks the new events and codes; the smoke client is run
manually against the local stack.

### Edge Cases

- The model calls `cancel_order` twice in one run, or while an approval is pending → only the first
  becomes a proposal; the others get a tool result "an approval is already pending".
- The model calls `cancel_order` with a malformed order id → tool result ok false (invalid id), no
  approval.
- Orders times out or fails during the approve call → outcome `failed`; the answer says the
  cancellation could not be confirmed and suggests checking the order; never claims it worked.
- Orders returns 409 on the cancel (raced to another status) → outcome `not_cancellable`.
- The model is unavailable after a successful cancel → the stream still sends `approval.resolved`
  and `tool.result`, then a fixed deterministic confirmation text; the cancellation is never hidden.
- A decision while another stream is active in the conversation → 409 `STREAM_ACTIVE`.
- A new chat message after the approval expired → the approval is recorded as expired and the
  message is processed normally.
- Conversation deleted with a pending approval → the approval is recorded as `withdrawn`; audit
  rows are kept.
- Client disconnects during the continuation after the cancel succeeded → the cancel stands, the
  outcome is recorded; the stored answer follows the US-05 disconnect rules. A disconnect after
  the decision never interrupts the re-read/cancel step.
- The service stops after a decision but before its outcome is recorded → on the next touch the
  approval is closed with outcome `failed` ("could not be confirmed"); the conversation continues.
- An upstream 401 on the re-read or cancel → outcome `failed`, stream ends with `TOOL_FAILED`.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A tool `cancel_order(orderId)` MUST exist only for the chat agent. It calls Orders
  `POST /orders/{orderId}/cancel` with the caller's token (ADR 0001); it MUST NOT be registered on
  or callable through `/mcp`.
- **FR-002**: Every tool MUST declare `mutating` true or false. A mutating tool MUST declare an
  approval policy; startup MUST fail when a mutating tool has none or is exposed on `/mcp`.
- **FR-003**: When the model calls `cancel_order`, the system MUST read the order with the caller's
  token before anything else. Not found, not the caller's, or not PENDING → no approval; a
  `tool.result` (ok false) with the reason goes to the model and the run continues.
- **FR-004**: Otherwise the run MUST pause (graph interrupt) before any cancel call, store the
  approval (id, conversation, user = token subject, action, exact arguments, summary, proposed and
  expiry times) and emit `approval.required` {approvalId, action, orderId, summary {status,
  totalAmount, currency if available}, expiresAt}, then `message.end` with finishReason
  `awaiting_approval`. The assistant message is stored with that finish reason.
- **FR-005**: At most one pending approval per conversation. While one is pending and not expired,
  posting a chat message MUST return 409 `APPROVAL_PENDING`; further `cancel_order` calls in the
  same run MUST get a tool result saying an approval is already pending.
- **FR-006**: `POST /chat/conversations/{id}/approvals/{approvalId}` with body
  `{"decision": "approve" | "reject"}` MUST be the only way to decide. It requires the same auth
  as `/chat`; errors before the stream: 404 `NOT_FOUND` (unknown, malformed, or another user's
  conversation or approval, identical), 409 `APPROVAL_ALREADY_DECIDED`, 409 `STREAM_ACTIVE`, 410
  `APPROVAL_EXPIRED`, 413, 422 `VALIDATION_FAILED`, 429 `RATE_LIMITED`, 503 `UNAVAILABLE`.
- **FR-007**: A decision MUST be single use: it is recorded atomically before any call to Orders,
  so concurrent or repeated decisions yield exactly one execution and 409 for the others.
- **FR-008**: On approve the system MUST re-read the order with the caller's token and cancel only
  if it is still PENDING and the caller's, using the arguments stored at proposal time; the model
  cannot change them. On reject or expiry no call to the cancel endpoint is ever made.
- **FR-009**: A decision MUST stream SSE: `message.start`, `approval.resolved` {approvalId,
  decision, outcome}, `tool.result` for `cancel_order`, the answer (`token` events) and
  `message.end`, under the US-05 rules (ids, keep-alive, gatekeeper, token cap, disconnect).
  Outcomes: `cancelled`, `rejected`, `not_cancellable`, `failed`. A reject's answer MUST say that
  nothing was changed.
- **FR-010**: An approval expires `APPROVAL_TTL_SECONDS` (default 600) after the proposal; a
  decision after that returns 410 and records outcome `expired`. Expiry is evaluated when the
  approval is next touched (decision, new message, conversation read or delete).
- **FR-011**: Chat text, tool result text and model output MUST NOT approve, reject or alter an
  approval. Tool results stay delimited data (US-05 FR-013).
- **FR-012**: The gatekeeper MUST replace any answer stating that an order was or will be cancelled
  unless a tool result of the conversation shows that order as cancelled (a confirmed
  `cancel_order` or a read showing CANCELLED).
- **FR-013**: Pending approvals and the paused graph state MUST be persisted so that a decision
  after a restart resumes the same run.
- **FR-014**: The conversation detail MUST include the pending approval (same fields as
  `approval.required`) or null.
- **FR-015**: At most `CANCEL_PROPOSALS_PER_HOUR` (default 5) proposals per user per rolling hour,
  counted from persisted records (survives restarts); an excess call gets a tool result saying the
  limit was reached and creates no approval.
- **FR-016**: An append-only audit table `chat_actions` (id, approval_id, conversation_id, user_id,
  action, order_id, decision, outcome, request_id, proposed_at, decided_at) MUST get one row per
  proposal, completed with its decision and outcome (`approve`/`reject`/none; `cancelled`,
  `rejected`, `not_cancellable`, `failed`, `expired`, `withdrawn`). Rows are never deleted, also
  not with the conversation; no message content, summaries or tokens. Created by migration 0004.
- **FR-017**: Each proposal and each decision MUST emit one structured log line (request id,
  conversation id, approval id, action, decision, outcome, user hash); never message text, order
  totals or tokens.
- **FR-018**: The assistant prompt MUST be a new immutable, hash-pinned version (assistant v2) that
  explains the approval flow (propose, never claim before confirmation, typed "yes" is not an
  approval); v1 stays unchanged.
- **FR-019**: `docs/chat-contract.md`, its contract test and the OpenAPI description MUST document
  `approval.required`, `approval.resolved`, finishReason `awaiting_approval`, the decision endpoint,
  the new error codes and `pendingApproval`, each with a JSON example.
- **FR-020**: `scripts/chat_smoke.py` MUST, on `approval.required`, print the action, order id,
  summary and expiry, ask `y/n`, post the decision and print the continuation.
- **FR-021**: Tests MUST cover, with a fake model and fake Orders and no network: propose then
  approve cancels; reject changes nothing; expired approval (410); replayed decision (409);
  another user decides (404); order no longer PENDING at approval; order CONFIRMED at proposal;
  injection in a tool result yields only a proposal; typed "yes" does not approve; restart between
  proposal and approval; `cancel_order` absent from `/mcp`; registry refuses a mutating tool
  without a policy; audit rows for approve, reject and expire; the proposal rate limit; the
  gatekeeper cancellation rule.

### Key Entities

- **Approval**: one proposed mutating call; bound to conversation, user, action and exact arguments;
  status pending → decided (approve/reject) or expired/withdrawn; summary from a fresh read; TTL.
- **Chat action (audit row)**: append-only record of a proposal and its final decision and outcome;
  ids and timestamps only.
- **Tool definition**: name, input schema, `mutating`, approval policy (required when mutating),
  exposure (`/mcp` and/or chat).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 0 cancel calls reach Orders without a recorded `approve` decision by the order's
  owner, across every bypass test (typed text, injection, replay, other user, expiry, restart).
- **SC-002**: Every approved, still-cancellable order is cancelled exactly once, also when the
  decision is posted twice concurrently.
- **SC-003**: The approval card appears within 2 s of the model's request with a responsive model
  and Orders.
- **SC-004**: 100% of proposals have an audit row whose outcome is set once decided, expired or
  withdrawn.
- **SC-005**: The frontend can implement the approval flow from `docs/chat-contract.md` alone.
- **SC-006**: The quality gate passes with coverage ≥ 80%; no unit test touches the network.

## Assumptions

- Posting a chat message while an approval is pending returns 409 rather than silently withdrawing
  the approval; the customer decides first (approve or reject), which keeps the paused run simple.
- Expiry is lazy (no background sweeper); an approval nobody touches stays pending in storage until
  touched, but can never be executed after its expiry time.
- The fresh read uses the same Orders read as `get_order`; the summary shows status and total
  (with currency only if Orders returns one).
- Decisions count towards the existing per-user `/chat` rate limit (10 per 60 s).
- Refused proposals (not found, not PENDING, rate limited) create no approval and no audit row; the
  rate limit counts audit rows.
- The gateway route for the decision endpoint (stream, no buffering limit) is a follow-up in
  ecommerce-infra, like US-05.
- One service instance; the active-stream guard stays in process memory (US-07 scales it).
