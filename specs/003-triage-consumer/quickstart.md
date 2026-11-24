# Quickstart: US-03 validation

## Automated

```sh
uv run pytest -q                                  # unit + integration (Testcontainers if Docker)
TEST_DATABASE_URL=postgresql://ai:ai@localhost:5436/ecommerce_ai uv run pytest -m integration
```

Expected: scenarios of US1–US5 green (fake consumer for Kafka; real PostgreSQL for repository,
unique constraints and races); coverage ≥ 80%.

## Manual (ecommerce-infra stack up)

1. `uv run python -m ecommerce_ai migrate` → tables `triage_cases`, `triage_evidence`,
   `inbound_dead_letters`, `alembic_version`.
2. `uv run python -m ecommerce_ai`; `GET /ready` → `consumer.connected: true`.
3. Place an order that fails payment (Orders API) → one `triage_cases` row with
   `PAYMENT_FAILED` and two `triage_evidence` rows (payment-failed + order-cancelled).
4. Produce a non-JSON message to `payments.payment-failed` → one `inbound_dead_letters` row; next
   valid event still processed; log outcome `dead_lettered` without payload.
5. Stop Kafka and PostgreSQL, restart the service → `/health` 200 within 5 s, `/ready` 503 with
   `database: DOWN`, `consumer.connected: false`, `lastError` set. Start them → consumer reconnects.
