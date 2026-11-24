# Tasks: US-03 Kafka triage consumer

**Input**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/consumer.md](contracts/consumer.md),
[quickstart.md](quickstart.md)

**Tests**: required (constitution VIII/IX). Unit tests: `FakeConsumer` + in-memory fake stores, no
network. Integration tests (`integration` marker): real PostgreSQL via `TEST_DATABASE_URL` or
Testcontainers. Write each story's tests first and see them fail. After every task: quiet gate
(CLAUDE.md); commit after green.

## Phase 1: Setup

- [X] T001 In `pyproject.toml` (deps already added with `uv add`; commit `uv.lock`): add mypy override `ignore_missing_imports` for `aiokafka.*` (and `testcontainers.*` if untyped) with a justification comment; register pytest marker `integration` (included by default)
- [X] T002 Add settings to `src/ecommerce_ai/infrastructure/settings.py` per [contracts/consumer.md](contracts/consumer.md#new-settings) (`triage_consumer_enabled`, `kafka_consumer_group`, `kafka_reconnect_backoff_base/max_seconds`, `db_retry_max_attempts`, `db_retry_backoff_base/max_seconds`, `database_timeout_seconds`, `consumer_shutdown_timeout_seconds`; positive bounds, base ≤ max); add the env names to the cleared variables in `tests/conftest.py`; cover defaults and invalid values in `tests/unit/test_settings.py`
- [X] T003 [P] Document the new settings in `.env.example`

## Phase 2: Foundational (blocks all stories)

- [X] T004 [P] Create `src/ecommerce_ai/messaging/contracts/__init__.py` and `events.py`: envelope (`eventId`, `aggregateId` UUID, `occurredAt` aware datetime, `version: Literal[1]`), `OrderCancelled` (`customerId`, `reason: str | None`), `PaymentFailed` (`orderId`, `reason`), `StockRejected` (`orderId`, `reason: StockRejectReason` StrEnum), all `frozen`, `extra="ignore"`, camelCase aliases; `order_key` property (FR-003); `TOPIC_MODELS` map. Tests in `tests/unit/test_event_contracts.py` (valid, extra fields ignored, version 2 rejected, non-UUID ids rejected, naive datetime rejected, `orderId` vs `aggregateId`) and `tests/contract/test_event_models.py` (field names/required flags pinned to the `events.md` tables)
- [X] T005 [P] Create `src/ecommerce_ai/agents/triage_intake/__init__.py`, `model.py` (`TriageCause`, `TriageStatus`, `IntakeOutcome` StrEnums; frozen `TriageEventInput(topic, event_id, order_id, reason, occurred_at, payload)`) and `ports.py` (`TriageCaseStore` unit-of-work Protocol: `async with store.transaction() as tx`, `tx.get_or_create_locked_case(order_id, cause, now)`, `tx.add_evidence(case_id, event, received_at) -> bool` (False = duplicate), `tx.update_case(case_id, cause, updated_at)`; `DeadLetterStore.add(topic, partition, offset, key, raw, reason)`)
- [X] T006 [P] Create `src/ecommerce_ai/infrastructure/db/__init__.py` and `engine.py`: lazy `create_async_engine` from `DATABASE_URL` with driver set to `postgresql+asyncpg`, `pool_pre_ping=True`, `connect_args` timeout/command_timeout from `database_timeout_seconds`; `session_factory`; `is_transient_db_error(exc)` (OperationalError, InterfaceError, `connection_invalidated`, OSError, TimeoutError) (research 4, 11). Tests in `tests/unit/test_db_engine.py` (URL rewrite, no connection on build, transient classification)
- [X] T007 Create `src/ecommerce_ai/infrastructure/db/tables.py` (3 tables per [data-model.md](data-model.md), CHECKs from the enums) and Alembic in the package: `migrations/env.py` (async, URL from settings), `script.py.mako`, `versions/0001_triage_tables.py` (hand-written); `migrate.py::run_migrations(settings)` (programmatic `upgrade head`, `script_location = ecommerce_ai.infrastructure.db:migrations`); add `migrate` subcommand to `src/ecommerce_ai/__main__.py`; unit test for argument dispatch in `tests/unit/test_main.py`
- [X] T008 Create `tests/integration/__init__.py` and `conftest.py`: session DB fixture (`TEST_DATABASE_URL` → Testcontainers `postgres:16-alpine` → `pytest.skip`), runs `run_migrations`, per-test `TRUNCATE`, all tests marked `integration`; `tests/integration/test_migrations.py` (tables, unique constraints, CHECKs exist; upgrade is idempotent)
- [X] T009 [P] Create `tests/support/kafka.py` (`FakeConsumer` implementing `ConsumerPort`: queued records, committed offsets, start/stop failures on demand, highwater/position; `make_record(topic, value, key, partition, offset)` and valid payload builders for the 3 topics) and `tests/support/stores.py` (in-memory `TriageCaseStore` and `DeadLetterStore` with injectable failures)
- [X] T010 [P] Create `src/ecommerce_ai/messaging/kafka.py` (`ConsumerPort` Protocol: `start/stop/getmany/commit/assignment/highwater/position`; `AIOKafkaConsumerAdapter` built with `enable_auto_commit=False`, `auto_offset_reset="earliest"`, group from settings; only module importing aiokafka) and `messaging/consumer_state.py` (`ConsumerState`: `connected`, `caught_up`, `last_error` = exception class name); tests in `tests/unit/test_consumer_state.py`

**Checkpoint**: gate green; migrations apply on a real DB; nothing connects at app startup.

## Phase 3: US1 — Record a triage case for a failed order (P1) 🎯 MVP

**Independent test**: one valid event per topic for three orders → three PENDING_TRIAGE cases with the expected cause and one evidence item each; commit only after save.

- [X] T011 [P] [US1] Tests in `tests/unit/test_cause.py` for `derive_cause` covering every row of the data-model cause table (incl. `payment failed: card declined`, `stock rejected: unknown product`, bare `stock rejected` → UNKNOWN, other reason → UNKNOWN)
- [X] T012 [US1] Implement `src/ecommerce_ai/agents/triage_intake/cause.py::derive_cause(topic, reason) -> TriageCause`
- [X] T013 [P] [US1] Tests in `tests/unit/test_triage_intake.py` (fake store): first event creates case with derived cause, PENDING_TRIAGE, one evidence with topic/eventId/reason/occurredAt/receivedAt (UTC, fake clock)/payload; returns `processed`
- [X] T014 [US1] Implement `src/ecommerce_ai/agents/triage_intake/service.py::TriageIntakeService(store, clock).record(event) -> IntakeOutcome`, one transaction per event (FR-004, FR-005, FR-008)
- [X] T015 [US1] Implement `src/ecommerce_ai/infrastructure/db/triage_repository.py` (`INSERT … ON CONFLICT (order_id) DO NOTHING` → `SELECT … FOR UPDATE` → `INSERT evidence … ON CONFLICT (event_id) DO NOTHING RETURNING id` → update cause/updated_at; research 9); tests in `tests/integration/test_triage_repository.py`
- [X] T016 [US1] Implement `src/ecommerce_ai/messaging/decoding.py::decode(record) -> TriageEventInput | DecodeFailure` (valid path: UTF-8 → JSON → `TOPIC_MODELS[topic]` → `TriageEventInput` with `model_dump(mode="json")` payload); valid-path tests in `tests/unit/test_decoding.py`
- [X] T017 [US1] Implement `src/ecommerce_ai/messaging/triage_consumer.py::TriageConsumer(consumer_factory, intake, dead_letters, state, settings, clock, sleep).run(stop_event)` processing loop: `getmany` → decode → `intake.record` → `commit({tp: offset+1})` after save → one log line (`event="triage_message"`, topic, partition, offset, event_id, order_id, outcome); tests in `tests/unit/test_triage_consumer.py` (3 topics → 3 cases, commit after save, commit not called when record raises, log fields)
- [X] T018 [US1] Wire in `src/ecommerce_ai/main.py`: engine, `TriageRepository`, `TriageIntakeService`, `ConsumerState`, consumer task started in `resources()` when `triage_consumer_enabled`, engine disposed on exit; update `tests/unit/test_main.py` (disabled flag → no task; enabled with a fake factory → task started and stopped)
- [X] T019 [US1] `tests/integration/test_triage_flow.py`: `FakeConsumer` + real DB, one event per topic for three orders → three cases (acceptance US1 1–4)

**Checkpoint**: MVP — every failure event yields a case.

## Phase 4: US2 — Combine events for the same order, in any order (P1)

**Independent test**: cancellation + failure for one order in both orders → one case, two evidence items, failure cause.

- [X] T020 [P] [US2] Tests for `merge_cause(current, incoming)` in `tests/unit/test_cause.py` (rank failure > CANCELLATION_REQUESTED > UNKNOWN; equal failure rank keeps the first) and in `tests/unit/test_triage_intake.py` (upgrade bumps `updated_at`; no upgrade keeps cause; evidence always added)
- [X] T021 [US2] Implement `merge_cause` in `src/ecommerce_ai/agents/triage_intake/cause.py` and apply it in `service.py` (update case only when cause changes or evidence added: `updated_at` on each new evidence)
- [X] T022 [US2] Extend `tests/integration/test_triage_flow.py`: all arrival orders of cancellation + each failure, PAYMENT_FAILED then stock-rejected (and reverse), cancellation `payment failed: …` first (SC-001); race: two concurrent `record` calls for the same new order on separate sessions → one case, two evidence items

## Phase 5: US3 — Redelivery changes nothing (P1)

**Independent test**: same event twice → one evidence item, unchanged case, second message committed, outcome `duplicate`.

- [X] T023 [P] [US3] Tests: `tests/unit/test_triage_intake.py` (duplicate → `duplicate`, case untouched, transaction rolled back), `tests/unit/test_triage_consumer.py` (duplicate committed and logged `duplicate`), `tests/integration/test_triage_flow.py` (sequential and concurrent redelivery → stored data identical to one delivery, SC-002)
- [X] T024 [US3] Make `service.py` return `IntakeOutcome.duplicate` and skip the cause update when `add_evidence` reports a duplicate; adjust `triage_repository.py` if the tests reveal a gap

## Phase 6: US4 — Bad messages do not block the stream (P2)

**Independent test**: malformed message then valid message → dead-letter row for the first, case for the second, both committed.

- [X] T025 [P] [US4] Failure-path tests in `tests/unit/test_decoding.py`: non-UTF-8, invalid JSON, non-object JSON, missing field, wrong type, `version` 2, non-UUID `orderId`/`aggregateId`, unknown topic → `DecodeFailure` with a reason ≤ 500 chars and no payload values; extra fields still decode
- [X] T026 [US4] Complete failure paths in `src/ecommerce_ai/messaging/decoding.py` (reason built from Pydantic error locations/types only)
- [X] T027 [US4] Implement `src/ecommerce_ai/infrastructure/db/dead_letter_repository.py` (`DeadLetterStore`: raw bytes, key as-is, reason, `created_at` UTC); tests in `tests/integration/test_dead_letters.py`
- [X] T028 [US4] Dead-letter path in `triage_consumer.py` (no retries of validation, commit after DL write, log `dead_lettered` with no `event_id`/`order_id` when unknown) and wire the repository in `main.py`; tests in `tests/unit/test_triage_consumer.py` (malformed then valid; SC-004: no re-processing of the poison message) and `tests/integration/test_triage_flow.py`

## Phase 7: US5 — Survive outages and restarts (P2)

**Independent test**: no Kafka, no DB → `/health` 200, `/ready` reports both down; DB failure mid-save → no commit, processed after recovery.

- [X] T029 [P] [US5] Tests in `tests/unit/test_triage_consumer.py` (fake clock/sleep): Kafka start failure → reconnect with growing bounded delays (`min(base·2ⁿ, max)` + jitter), `state.connected=False`, `last_error` = class name; stop event interrupts backoff; transient DB error retried up to `db_retry_max_attempts` without commit then succeeds; retries exhausted → consumer closed without commit, reconnect, redelivery processed (SC-003); DL write failure treated as transient; non-transient error not retried; `caught_up` from highwater − position; shutdown: in-flight event not committed, saved offsets committed, `stop()` called within `consumer_shutdown_timeout_seconds`
- [X] T030 [US5] Implement in `src/ecommerce_ai/messaging/triage_consumer.py`: outer reconnect loop, DB retry via `infrastructure/retry.py::call_with_retry` with `is_transient_db_error`, `ConsumerState` updates, graceful stop (research 2–4, 13); task cancellation bounded by the shutdown timeout in `main.py`
- [X] T031 [P] [US5] Tests in `tests/unit/test_health_api.py`: `consumer` object (`connected`, `caughtUp`, `lastError`) present; `null` when disabled; consumer down never changes status/HTTP code; DB down still 503; app with no Kafka/DB answers `/health` and `/ready` within 5 s (SC-005, fake probes + failing fake consumer factory)
- [X] T032 [US5] Extend `ReadinessResponse` in `src/ecommerce_ai/api/health.py` with optional `consumer` (OpenAPI descriptions, camelCase) reading `ConsumerState`; pass state from `main.py`
- [ ] T033 [US5] `tests/integration/test_triage_flow.py`: DB connection failure injected during a save (e.g. dispose/terminate backend) → offset not committed, event recorded after recovery

## Phase 8: Polish

- [ ] T034 [P] SC-006 test in `tests/unit/test_logging.py` / `test_triage_consumer.py`: captured consumer log lines contain no payload body, `customerId` or reason text for processed, duplicate and dead-lettered messages
- [ ] T035 [P] Add `docker-entrypoint.sh` (run `python -m ecommerce_ai migrate`, log and continue on failure, then `exec` the app) and set `ENTRYPOINT` in `Dockerfile`
- [ ] T036 [P] `.github/workflows/ci.yml`: ensure the pytest step runs integration tests (Docker on ubuntu runners, Testcontainers); check with actionlint
- [ ] T037 [P] Update `README.md` and `CLAUDE.md` (consumer, tables, `migrate` command, new settings, readiness `consumer`, integration tests, layering of `agents/triage_intake` and `messaging`)
- [ ] T038 Run the full quiet gate (coverage ≥ 80%) and the [quickstart.md](quickstart.md) automated steps; manual steps if the infra stack is up

## Dependencies

Setup → Foundational (T006 → T007 → T008; T004, T005, T009, T010 parallel) → US1 (T011→T012,
T013→T014, T015 after T007/T008, T016 after T004, T017 after T009/T010/T014/T016, T018 → T019) →
US2 and US3 (both extend `service.py`/tests: run sequentially) → US4 → US5 (T030 extends
`triage_consumer.py` after T028) → Polish.

## Parallel examples

- Foundational: T004, T005, T006, T009, T010 together.
- US1: T011 ‖ T013 (test files), then T012 ‖ T015 ‖ T016.
- US4: T025 ‖ T027. US5: T029 ‖ T031.
- Polish: T034–T037 together.

## Strategy

MVP = Phases 1–3 (cases recorded). Then US2 (merge) and US3 (idempotency) complete the P1 promise,
then US4 (dead letters), US5 (resilience/readiness), Polish. Commit per task after a green gate.
