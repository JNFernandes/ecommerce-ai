# Research: US-03 Kafka triage consumer

Each decision: choice — reason (alternatives rejected).

1. **Consumer runtime**: one `asyncio.Task` started in the FastAPI lifespan (`resources`), stopped
   via a stop `Event` + `consumer_shutdown_timeout_seconds` — no extra process, shares settings and
   logging (separate worker process: more deploy surface for one consumer).
2. **Connect retry**: outer loop builds the consumer, `start()`s it, and on any Kafka error closes it
   and sleeps `min(base·2ⁿ, max)` with jitter; forever but bounded per step, interruptible by the
   stop event (FR-011). Startup never waits for it (SC-005).
3. **Offsets**: `enable_auto_commit=False`, `auto_offset_reset="earliest"`, commit `{tp: offset+1}`
   after each message's transaction (FR-008). Per-message commit is fine at this volume (batch
   commit: more redelivery on crash, more code).
4. **DB retry**: reuse `infrastructure/retry.py::call_with_retry` with `db_retry_*` settings;
   transient = SQLAlchemy `OperationalError`/`InterfaceError`, `DBAPIError.connection_invalidated`,
   `OSError`, `TimeoutError`. Exhausted → raise to the outer loop, which closes the consumer without
   committing and reconnects → redelivery from the last commit (spec edge case "DB down longer").
5. **Kafka port**: `messaging/kafka.py` wraps `AIOKafkaConsumer` behind a `ConsumerPort` Protocol
   (`start/stop/getmany/commit/assignment/highwater/position`). Unit tests use `FakeConsumer`
   (`tests/support/kafka.py`); no real Kafka in this story (spec assumption).
6. **Typing**: aiokafka and asyncpg ship no `py.typed`. Narrow mypy override
   `ignore_missing_imports` for `aiokafka.*` only, used solely inside `messaging/kafka.py`; asyncpg is
   never imported (driver via SQLAlchemy URL; errors via SQLAlchemy). testcontainers likewise if
   untyped.
7. **Contracts**: `messaging/contracts/` package, Pydantic v2 `frozen`, `extra="ignore"`,
   `version: Literal[1]`, UUID ids, aware `datetime`; `TOPIC_MODELS` maps topic → model (FR-002).
   `StockRejectReason` is a `StrEnum` of the three documented values; other reasons are free text.
8. **Layering of processing**: `agents/triage_intake/` holds `derive_cause`, `merge_cause` (pure)
   and `TriageIntakeService.record(event)`; it talks to a `TriageCaseStore` Protocol (unit-of-work)
   in `agents/triage_intake/ports.py`. SQL lives in `infrastructure/db/triage_repository.py`. The
   consumer only decodes, validates, delegates and commits (constitution I).
9. **Atomic, race-safe save**: one transaction per event: `INSERT case … ON CONFLICT (order_id) DO
   NOTHING` → `SELECT … FOR UPDATE` → `INSERT evidence … ON CONFLICT (event_id) DO NOTHING
   RETURNING id`; no row → duplicate, rollback; else service merges cause, repo updates
   `cause`/`updated_at`. Row lock serialises same-order events across partitions (spec edge cases).
10. **Dead letters**: invalid JSON / UTF-8 / schema / version / non-UUID → `inbound_dead_letters`
    (raw `bytea`, key `bytea`, reason ≤ 500 chars, no payload in logs); write goes through the same
    DB retry; then commit (FR-009). Repository in infrastructure behind `DeadLetterStore` Protocol.
11. **Engine**: `create_async_engine` lazily from `DATABASE_URL` with driver swapped to
    `postgresql+asyncpg` (`make_url(...).set(drivername=...)`); `pool_pre_ping=True`,
    `connect_args={"timeout", "command_timeout"}` from `database_timeout_seconds`. Nothing connects
    at startup; engine disposed in lifespan.
12. **Migrations**: Alembic inside the package (`ecommerce_ai/infrastructure/db/migrations`,
    `script_location = ecommerce_ai.infrastructure.db:migrations`) so the wheel in the image carries
    them; `uv run python -m ecommerce_ai migrate` runs `upgrade head`. Container: `docker-entrypoint.sh`
    runs migrate (failure logged, non-fatal) then `exec`s the app (FR-015, SC-005). Autogenerate off;
    hand-written revision `0001`.
13. **Readiness**: `ConsumerState` (connected, caught_up, last_error = exception class name only, no
    message → no secrets) exposed as an optional `consumer` object in `ReadinessResponse`; never
    affects status (FR-012). `caught_up` = Σ(highwater − position) == 0 over the assignment.
14. **Logging**: one line per message: `topic`, `partition`, `offset`, `event_id`, `order_id`,
    `outcome`; never payload, `customerId`, reason (FR-014, SC-006).
15. **Integration DB**: `TEST_DATABASE_URL` set → use it (compose `postgres-ai` :5436); else Docker
    reachable → Testcontainers `postgres:16-alpine`; else skip. Session fixture runs Alembic
    `upgrade head`; per-test `TRUNCATE`. Marker `integration`, included by default and in CI
    (ubuntu runners have Docker).
