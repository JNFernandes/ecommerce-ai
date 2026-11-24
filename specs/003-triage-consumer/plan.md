# Implementation Plan: US-03 Kafka triage consumer

**Branch**: `feature/US-03-triage-consumer` | **Date**: 2026-11-24 | **Spec**: [spec.md](spec.md)

## Summary

An aiokafka consumer (lifespan background task, reconnect with bounded backoff, graceful stop)
decodes and validates the three failure events, then calls an agents-layer intake service that
records one triage case per order with per-event evidence and cause precedence, atomically and
idempotently (SQLAlchemy 2 async + asyncpg, Alembic). Invalid messages go to an inbound dead-letter
table. Offsets are committed only after the save. Decisions: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: + aiokafka, sqlalchemy[asyncio], asyncpg, alembic (added); dev +
testcontainers[postgres]
**Storage**: PostgreSQL 16 (`postgres-ai`, 5436): 3 tables, see [data-model.md](data-model.md)
**Testing**: pytest-asyncio; `FakeConsumer`; integration via Testcontainers or `TEST_DATABASE_URL`
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: live + ready answers ≤ 5 s with no deps (SC-005); one message per step
**Constraints**: nothing connects at startup; commit after save; no LLM, no publish
**Scale/Scope**: 1 consumer, 3 topics, 3 tables, ~9 settings

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ messaging decodes/validates/delegates; cause decisions in `agents/triage_intake`; SQL in `infrastructure/db` behind Protocol ports. |
| II Security | ✅ no new route; logs exclude payload, `customerId`, reason; `lastError` is a class name; `DATABASE_URL` stays `SecretStr`. |
| III HITL | ✅ no mutation of platform state, no LLM. |
| IV Reliability | ✅ starts without Kafka/DB; bounded DB retries; commit after save; idempotent by `eventId` (UNIQUE); inbound DL without retries. |
| V Event contracts | ✅ frozen models matching `events.md`, unknown fields ignored, `orderId` for Payments/Inventory; contract tests. |
| VI Evals | ✅ n/a (no LLM). |
| VII Observability | ✅ one structured line per message, `order_id` as correlation id. |
| VIII/IX Tests | ✅ unit (fake consumer, fake store) + integration (real PostgreSQL); CI gains integration layer. |
| XI Gate | ✅ unchanged; narrow mypy override for untyped `aiokafka` only (justified). |
| Stack | ✅ all deps are listed in the constitution stack (no ADR). Schema only via migrations. |

Post-design re-check: no violations.

## Project Structure

```text
specs/003-triage-consumer/  plan.md research.md data-model.md quickstart.md contracts/consumer.md

src/ecommerce_ai/
├── __main__.py                       # + `migrate` subcommand
├── main.py                           # engine, repos, intake service, consumer task in lifespan
├── agents/triage_intake/
│   ├── __init__.py
│   ├── model.py                      # TriageCause, TriageStatus, IntakeOutcome, TriageEventInput
│   ├── cause.py                      # derive_cause, merge_cause (pure)
│   ├── ports.py                      # TriageCaseStore / TriageUnitOfWork Protocols
│   └── service.py                    # TriageIntakeService.record(event) -> IntakeOutcome
├── messaging/
│   ├── contracts/  __init__.py events.py   # envelope + 3 models, TOPIC_MODELS
│   ├── kafka.py                      # ConsumerPort + AIOKafkaConsumer adapter
│   ├── decoding.py                   # bytes -> model | DecodeFailure(reason)
│   ├── consumer_state.py             # ConsumerState (connected, caught_up, last_error)
│   └── triage_consumer.py            # run loop: reconnect, decode, delegate, DL, commit, stop
├── infrastructure/
│   ├── settings.py                   # + consumer/DB retry settings
│   └── db/
│       ├── engine.py                 # lazy async engine, session factory, transient-error check
│       ├── tables.py                 # SQLAlchemy Core/ORM tables
│       ├── triage_repository.py      # TriageCaseStore impl (upsert, lock, evidence, cause)
│       ├── dead_letter_repository.py # inbound DL insert
│       ├── migrate.py                # run Alembic upgrade head programmatically
│       └── migrations/  env.py script.py.mako versions/0001_triage_tables.py
└── api/health.py                     # + optional `consumer` in ReadinessResponse

tests/
├── support/   kafka.py (FakeConsumer, record builder) stores.py (in-memory fake ports)
├── unit/      test_event_contracts.py test_decoding.py test_cause.py test_triage_intake.py
│              test_triage_consumer.py test_consumer_state.py (+ settings/health/main/logging)
├── contract/  test_event_models.py   # models vs events.md field tables
└── integration/ conftest.py (DB fixture: TEST_DATABASE_URL → Testcontainers → skip)
               test_triage_repository.py test_dead_letters.py test_migrations.py
               test_triage_flow.py   # FakeConsumer + real DB: all arrival orders, races, retries
```

**Also in this story**: `docker-entrypoint.sh` + Dockerfile `ENTRYPOINT`; `alembic.ini`-free config
in code; mypy override for `aiokafka.*`; `integration` marker in `pyproject.toml`; `ci.yml` runs
integration (Docker on runners); `README.md`, `CLAUDE.md`, `.env.example` for new settings and the
`migrate` command.

## Complexity Tracking

None.
