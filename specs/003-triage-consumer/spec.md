# Feature Specification: US-03 Kafka triage consumer

**Feature Branch**: `feature/US-03-triage-consumer`

**Created**: 2026-11-24

**Status**: Draft

**Input**: User description: "US-03 Kafka triage consumer: listen to the events that explain why an
order failed and record one triage case per affected order, reliably and exactly once in effect, so
that the triage graph (US-04) can later explain the cause to the customer. No LLM, publishes
nothing."

## Clarifications

### Session 2026-11-24

- Q: Consumer group? → A: `ai-triage`, as in `events.md` and CLAUDE.md (not `ecommerce-ai-triage`).
- Q: Cause vocabulary on the case? → A: the published contract's `cause` enum (PAYMENT_FAILED,
  INSUFFICIENT_STOCK, UNKNOWN_PRODUCT, CANCELLATION_REQUESTED, UNKNOWN); one enum service-wide.
- Q: Cancellation whose `reason` names a failure? → A: classify by reason prefix, as the contract's
  cause table does.

## User Scenarios & Testing *(mandatory)*

Actor: **the platform** (Orders, Payments and Inventory publish failure events; the AI service
consumes them). The downstream reader is the US-04 triage graph, which reads the recorded cases.

### User Story 1 - Record a triage case for a failed order (Priority: P1)

When an order fails or is cancelled, the AI service records one triage case for that order, with
the cause established so far and the event that showed it as evidence.

**Why this priority**: Without recorded cases there is nothing for US-04 to explain; this is the
minimum useful slice.

**Independent Test**: Deliver one valid event of each consumed topic for three different orders
and check that three cases exist, each with status PENDING_TRIAGE, the expected cause and one piece
of evidence, and that each message is acknowledged only after it is saved.

**Acceptance Scenarios**:

1. **Given** no case for order O, **When** a valid `payments.payment-failed` for O arrives,
   **Then** a case for O is created with cause PAYMENT_FAILED, status PENDING_TRIAGE and one
   evidence item holding the event's id, topic, reason, occurrence time and payload.
2. **Given** no case for order O, **When** a valid `inventory.stock-rejected` for O arrives,
   **Then** a case for O is created with cause INSUFFICIENT_STOCK (reason `insufficient stock`) or
   UNKNOWN_PRODUCT (either `unknown product` reason) and one evidence item.
3. **Given** no case for order O, **When** a valid `orders.order-cancelled` without `reason`
   arrives, **Then** a case for O is created with cause CANCELLATION_REQUESTED and one evidence
   item.
4. **Given** any of the above, **When** the save succeeds, **Then** the message is acknowledged
   (offset committed) only after the save, and one log line records topic, eventId, orderId and
   outcome `processed`.

---

### User Story 2 - Combine events for the same order, in any order (Priority: P1)

Several events usually describe one failed order (a failure and the cancellation it causes), and
they arrive on different topics in no guaranteed order. They all end up in one case whose cause is
the most specific one seen.

**Why this priority**: A failed order normally produces two events; without this the data is wrong
for almost every case.

**Independent Test**: Deliver a cancellation and a failure for the same order in both orders and
check one case with two evidence items and the failure cause.

**Acceptance Scenarios**:

1. **Given** a case for O created by a cancellation, **When** a payment-failed or stock-rejected
   event for O arrives, **Then** the case's cause is upgraded to the failure cause, its update time
   changes, and the event is added as evidence.
2. **Given** a case for O created by a failure event, **When** the cancellation for O arrives,
   **Then** the cause stays the failure cause and the cancellation is added as evidence.
3. **Given** a case for O with cause PAYMENT_FAILED, **When** a stock-rejected event for O arrives
   (or the reverse), **Then** the first failure cause received is kept and the second event is
   added as evidence only.
4. **Given** no case for O, **When** a cancellation with `reason` `payment failed: card declined`
   arrives, **Then** the case is created with cause PAYMENT_FAILED; a later payment-failed event
   only adds evidence.

---

### User Story 3 - Redelivery changes nothing (Priority: P1)

Delivery is at-least-once, so the same event may arrive again. A redelivered event is recognised
and has no effect.

**Why this priority**: "Exactly once in effect" is the core reliability promise of the story.

**Independent Test**: Deliver the same event twice and check one evidence item, an unchanged case,
an acknowledged second message and outcome `duplicate` in the log.

**Acceptance Scenarios**:

1. **Given** event E already recorded, **When** E is delivered again, **Then** no case or evidence
   changes, the message is acknowledged and the log shows outcome `duplicate`.

---

### User Story 4 - Bad messages do not block the stream (Priority: P2)

A message that is not valid JSON, misses a required field, has a wrong type or an unknown schema
version is set aside in an inbound dead-letter store and the consumer moves on.

**Why this priority**: One poison message must not stop triage for every later order.

**Independent Test**: Deliver a malformed message followed by a valid one; check a dead-letter
record for the first, a case for the second, and both acknowledged.

**Acceptance Scenarios**:

1. **Given** a message that fails validation, **When** it is consumed, **Then** a dead-letter
   record holds its topic, partition, offset, key, raw payload and the failure reason; no case is
   touched; the message is acknowledged; the log shows outcome `dead_lettered`.
2. **Given** a message with `version` other than 1, **When** it is consumed, **Then** it is
   dead-lettered as an unknown version.
3. **Given** a valid message with extra unknown fields, **When** it is consumed, **Then** the
   extra fields are ignored and it is processed normally.

---

### User Story 5 - Survive outages and restarts (Priority: P2)

The service starts and answers health checks while Kafka or the database is down, connects in the
background when they come back, retries transient database failures without losing events, and
stops cleanly.

**Why this priority**: Required by the constitution (IV) and by the platform's startup rule; the
service must never be the reason a deploy fails.

**Independent Test**: Start the service with no Kafka and no database; check liveness 200,
readiness reporting both as down; then make the database fail during a save and check the offset
is not committed and the event is processed once the database is back.

**Acceptance Scenarios**:

1. **Given** Kafka is unreachable, **When** the service starts, **Then** it starts, liveness
   answers 200, readiness reports the consumer as not connected with its last error, and the
   consumer keeps retrying with growing, bounded delays.
2. **Given** the database is unreachable, **When** readiness is called, **Then** it answers 503 and
   reports the database as down (existing rule), and startup has not failed.
3. **Given** the consumer is connected, **When** readiness is called, **Then** it reports the
   consumer as connected, whether it has caught up (lag-free) and the last error, if any; consumer
   state never turns readiness into 503.
4. **Given** a transient database error while saving an event, **When** the save fails, **Then**
   it is retried with exponential backoff up to a bounded number of attempts, the offset is not
   committed, and the event is processed after the database recovers (or redelivered after a
   restart).
5. **Given** the service is stopping, **When** shutdown begins, **Then** the consumer finishes or
   abandons the in-flight event without committing it, commits the offsets of events already
   saved, leaves the group and stops within the shutdown timeout.

### Edge Cases

- A cancellation whose `reason` names a failure arrives before or without the failure event: it
  sets the failure cause (FR-006); `stock rejected` with no source reason cannot tell stock from
  product, so it is UNKNOWN until the stock-rejected event upgrades it.
- Two consumers race to create the case for the same order (different partitions): exactly one
  case exists afterwards; the loser adds evidence to it.
- The same event is redelivered while its first delivery is still being saved: one evidence item.
- An event whose `orderId` (or `aggregateId` for Orders) is not a UUID: dead-lettered.
- A message with an empty or missing key: processed by payload content; the key is stored as-is
  when dead-lettered.
- A payload that is not valid UTF-8: dead-lettered with the raw bytes preserved.
- Writing the dead-letter record itself fails (database down): treated as a transient database
  error; the offset is not committed.
- The database stays down longer than the retry bound: the consumer stops advancing (no commit),
  reports the error in readiness and resumes from the last committed offset.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The service MUST consume `orders.order-cancelled`, `payments.payment-failed` and
  `inventory.stock-rejected` in consumer group `ai-triage` (from configuration, defaulting to it).
- **FR-002**: Every message MUST be validated against its contract in `events.md` (envelope
  `eventId`, `aggregateId`, `occurredAt`, `version` plus the topic's fields). Missing required
  fields or wrong types make it invalid; unknown extra fields are ignored. Only `version` 1 is
  accepted.
- **FR-003**: The order MUST be identified by `aggregateId` for Orders events and by `orderId` for
  Payments and Inventory events, never the reverse.
- **FR-004**: The service MUST keep exactly one triage case per order. The first valid event for an
  order creates it with status PENDING_TRIAGE; every later valid event for that order adds evidence.
- **FR-005**: Each evidence item MUST record the source topic, the event's `eventId` (unique across
  all evidence), the event's `reason` when it has one, the event's `occurredAt`, the time it was
  received (UTC) and the full validated payload.
- **FR-006**: The case's cause MUST use the contract's `cause` values and be derived per event:
  payment-failed → PAYMENT_FAILED; stock-rejected with `insufficient stock` → INSUFFICIENT_STOCK,
  with `unknown product` or `unknown product and insufficient stock` → UNKNOWN_PRODUCT; cancellation
  without `reason` → CANCELLATION_REQUESTED; cancellation whose `reason` starts with `payment failed`
  → PAYMENT_FAILED, with `stock rejected: <stock reason>` → the matching stock cause; any other
  cancellation reason (including bare `stock rejected`) → UNKNOWN. Precedence: failure causes
  (PAYMENT_FAILED, INSUFFICIENT_STOCK, UNKNOWN_PRODUCT) over CANCELLATION_REQUESTED over UNKNOWN. A
  higher cause replaces a lower one; between two failure causes the first received is kept.
- **FR-007**: A redelivered event (same `eventId`) MUST change nothing and be acknowledged.
- **FR-008**: The case update, the evidence insert and any cause change for one event MUST be saved
  atomically; the offset MUST be committed only after that save succeeds.
- **FR-009**: An invalid message MUST be stored in an inbound dead-letter store (topic, partition,
  offset, key, raw payload, failure reason, creation time) without retries, and then acknowledged.
- **FR-010**: Transient database errors MUST be retried with exponential backoff and a bounded
  number of attempts, without committing the offset; the bounds come from configuration.
- **FR-011**: The service MUST start, answer liveness and answer readiness without Kafka or the
  database. The consumer MUST connect in the background, retrying with bounded exponential backoff.
- **FR-012**: Readiness MUST report the consumer's state (connected or not, caught up or not, last
  error) without letting it cause a 503; the database rule stays (down → 503).
- **FR-013**: On shutdown the consumer MUST commit offsets of saved events, not commit an unsaved
  one, leave the group and stop within a bounded time.
- **FR-014**: Each consumed message MUST produce one structured log line with topic, eventId,
  orderId (as the correlation id) and outcome `processed`, `duplicate` or `dead_lettered`. Payload
  bodies, `customerId` and free-text reasons MUST NOT be logged.
- **FR-015**: The schema of the stored cases, evidence and dead letters MUST be created and evolved
  through versioned migrations, never at service startup implicitly.
- **FR-016**: The story MUST NOT call an LLM or publish any event.

### Key Entities

- **Triage case**: one per order. Order id (unique), cause, status (PENDING_TRIAGE in this story),
  created and updated times (UTC).
- **Triage evidence**: one per consumed valid event, linked to a case. Source topic, source event id
  (unique), reason (optional), occurred-at, received-at, payload.
- **Inbound dead letter**: one per invalid message. Topic, partition, offset, key, raw payload,
  failure reason, created-at.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every failed order, exactly one case exists, whatever the number, order or
  repetition of its events (verified for all arrival orders of cancellation + each failure).
- **SC-002**: Delivering any event twice leaves the stored data identical to delivering it once.
- **SC-003**: No event is lost when the database fails during processing: after recovery, 100% of
  events delivered before the failure are recorded.
- **SC-004**: A malformed message delays the next valid message by no more than one processing step;
  0 malformed messages are retried.
- **SC-005**: The service is live and answers readiness within 5 seconds of start with Kafka and the
  database both unreachable.
- **SC-006**: No log line produced by the consumer contains a payload body, `customerId` or free-text
  reason.

## Assumptions

- Requested by the user: SQLAlchemy 2 async, asyncpg and Alembic over the service's own PostgreSQL
  (`DATABASE_URL`, `postgres-ai` on 5436); tables `triage_cases`, `triage_evidence`,
  `inbound_dead_letters` as named in the request; aiokafka per the constitution.
- Migrations are applied by an explicit command (and by the container entrypoint before the app),
  not by the app process at startup, so startup without the database still works.
- Recording the case is a domain decision (cause precedence), so it sits behind an agents-layer
  entry point; the consumer only deserializes, validates and calls it (constitution I).
- The contract says the US-04 worker publishes one result per consumed event; keeping one case per
  order with per-event evidence leaves US-04 free to do that.
- Retry bounds, backoff and shutdown timeout have defaults in settings (for example 5 attempts,
  0.5 s doubling to 10 s, 10 s shutdown).
- Integration tests use Testcontainers PostgreSQL with a fake consumer; a real-Kafka test is not
  required in this story. CI gains the integration layer in this story.
- Out of scope: LLM and triage graph, publishing `ai.order-triage-completed`, the outbound dead-letter
  table and retry worker, any HTTP endpoint to read cases, Notifications changes.
