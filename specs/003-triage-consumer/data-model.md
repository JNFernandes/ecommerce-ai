# Data model: US-03

## Enums (`agents/triage_intake/model.py`, `StrEnum`)

- `TriageCause`: `PAYMENT_FAILED`, `INSUFFICIENT_STOCK`, `UNKNOWN_PRODUCT`,
  `CANCELLATION_REQUESTED`, `UNKNOWN` (same values as `events.md`; one enum service-wide).
  Rank: failure causes 2 > `CANCELLATION_REQUESTED` 1 > `UNKNOWN` 0; equal rank keeps current.
- `TriageStatus`: `PENDING_TRIAGE`.
- `IntakeOutcome`: `processed`, `duplicate` (+ `dead_lettered`, set by messaging).

## Tables (Alembic revision `0001`)

### `triage_cases`
| column | type | rule |
|---|---|---|
| `id` | uuid PK | generated in app (`uuid4`) |
| `order_id` | uuid | **UNIQUE** |
| `cause` | varchar(32) | CHECK in `TriageCause` |
| `status` | varchar(32) | CHECK in `TriageStatus` |
| `created_at`, `updated_at` | timestamptz | UTC; `updated_at` bumped on each new evidence |

### `triage_evidence`
| column | type | rule |
|---|---|---|
| `id` | uuid PK | |
| `case_id` | uuid FK → `triage_cases.id` | ON DELETE CASCADE, indexed |
| `source_topic` | varchar(64) | |
| `event_id` | uuid | **UNIQUE** (idempotency, FR-007) |
| `reason` | text NULL | |
| `occurred_at`, `received_at` | timestamptz | |
| `payload` | jsonb | validated event (`model_dump(mode="json")`) |

### `inbound_dead_letters`
| column | type | rule |
|---|---|---|
| `id` | uuid PK | |
| `topic` | varchar(255) | |
| `partition` | int | |
| `offset` | bigint | |
| `message_key` | bytea NULL | stored as-is |
| `raw_payload` | bytea | raw bytes (non-UTF-8 kept) |
| `failure_reason` | varchar(500) | validation summary, no payload values |
| `created_at` | timestamptz | |

No uniqueness on `(topic, partition, offset)`: a redelivered poison message may add a second row
(harmless, avoids a failure path).

## Event models (`messaging/contracts/`, frozen, `extra="ignore"`)

Envelope: `event_id` (`eventId`, UUID), `aggregate_id` (UUID), `occurred_at` (aware datetime),
`version: Literal[1]`.
- `OrderCancelled`: + `customer_id` UUID, `reason: str | None`; order = `aggregate_id`.
- `PaymentFailed`: + `order_id` UUID, `reason: str`; order = `order_id`.
- `StockRejected`: + `order_id` UUID, `reason: StockRejectReason`; order = `order_id`.

Each exposes `order_key` (FR-003) and is mapped by messaging to the agent input
`TriageEventInput(topic, event_id, order_id, reason, occurred_at, payload)`.

## Cause derivation (FR-006)

| event | reason | cause |
|---|---|---|
| payment-failed | any | PAYMENT_FAILED |
| stock-rejected | `insufficient stock` | INSUFFICIENT_STOCK |
| stock-rejected | `unknown product[ and insufficient stock]` | UNKNOWN_PRODUCT |
| order-cancelled | absent/null | CANCELLATION_REQUESTED |
| order-cancelled | starts `payment failed` | PAYMENT_FAILED |
| order-cancelled | `stock rejected: <stock reason>` | stock cause as above |
| order-cancelled | other, incl. bare `stock rejected` | UNKNOWN |
