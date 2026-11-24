# Quickstart: US-04 Triage graph

1. Gate: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov`
   (unit with fake model/stores/producer; integration on real PostgreSQL: pick + SKIP LOCKED,
   save transaction, budget reservation, outbox relay, migration 0002).
2. Local run without a model: start `postgres-ai` and Kafka from ecommerce-infra,
   `uv run python -m ecommerce_ai migrate`, `uv run python -m ecommerce_ai`. Produce a
   `inventory.stock-rejected` event; after ~3 s expect a `triage_case` log line with
   `source=FALLBACK_TEMPLATE`, a `triage_results` row, the case COMPLETED, an `outbox_events` row
   RESOLVED and one message on `ai.order-triage-completed` keyed by the order id.
3. With a model: set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` in `.env`; repeat → `source=LLM`,
   `modelId` set. Live test: `uv run pytest -m live_llm`.
4. Resilience: stop Kafka → result stored, outbox PENDING, `/ready` shows `outboxRelay.lastError`;
   start Kafka → RESOLVED with the same `eventId`. Stop PostgreSQL → `/health` 200, `/ready` 503.
5. Budget: set `DAILY_TOKEN_BUDGET=1` → every case uses the template, no model call logged.
