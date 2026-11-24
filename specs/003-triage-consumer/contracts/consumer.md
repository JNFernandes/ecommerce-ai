# Contracts: US-03

## Consumed events (source of truth: `ecommerce-infra/docs/events.md`)

Group `KAFKA_CONSUMER_GROUP` (default `ai-triage`). Topics: `orders.order-cancelled`,
`payments.payment-failed`, `inventory.stock-rejected`. Fields and order id: see
[data-model.md](../data-model.md#event-models-messagingcontracts-frozen-extraignore). Contract tests
check each model's fields against `events.md`. Nothing is published.

## `GET /ready` (extended, backward compatible)

```json
{
  "status": "UP",
  "dependencies": {"database": "UP", "kafka": "UP"},
  "consumer": {"connected": true, "caughtUp": false, "lastError": null}
}
```

- `consumer` is `null` when the consumer is disabled (`TRIAGE_CONSUMER_ENABLED=false`).
- `lastError`: exception class name only (e.g. `"KafkaConnectionError"`), never a message.
- `consumer` never changes `status` or the HTTP code; `database` DOWN → 503 (unchanged).

## Log line (one per consumed message)

`event="triage_message"`, `topic`, `partition`, `offset`, `event_id`, `order_id`,
`outcome` ∈ {`processed`, `duplicate`, `dead_lettered`}. Never payload, `customerId`, reason.

## Commands

- `uv run python -m ecommerce_ai migrate` → Alembic `upgrade head` on `DATABASE_URL`.
- `uv run python -m ecommerce_ai` → app (unchanged).

## New settings

| env | default |
|---|---|
| `TRIAGE_CONSUMER_ENABLED` | `true` |
| `KAFKA_CONSUMER_GROUP` | `ai-triage` |
| `KAFKA_RECONNECT_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `1` / `30` |
| `DB_RETRY_MAX_ATTEMPTS` | `5` |
| `DB_RETRY_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `0.5` / `10` |
| `DATABASE_TIMEOUT_SECONDS` | `5` |
| `CONSUMER_SHUTDOWN_TIMEOUT_SECONDS` | `10` |
