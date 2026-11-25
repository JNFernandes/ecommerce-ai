# Quickstart: validate US-06

## Automated

```sh
uv run -q ruff check -q && uv run -q ruff format --check -q && uv run -q mypy --no-error-summary && uv run -q pytest -q --no-header --cov --cov-report=
uv run pytest -q tests/unit -k "approval"          # FR-021 scenarios, no network
uv run pytest -q -m integration -k "approval or migration or checkpoint"
```

Expected: green gate; every bypass test asserts zero cancel calls on the fake Orders (SC-001).

## Manual (local stack)

Prereqs: `ecommerce-infra` up (gateway `:8090`, `postgres-ai :5436`), `ANTHROPIC_API_KEY` and
`ANTHROPIC_MODEL` set, a CUSTOMER token in `TOKEN` with one PENDING order.

1. `uv run python -m ecommerce_ai migrate` → revision 0004 applied.
2. `uv run python -m ecommerce_ai`, then `uv run python scripts/chat_smoke.py` and write
   "Cancel my order <id>". Expect the card (action, order id, status, total, expiry) and a `y/n`
   prompt; the order is still PENDING in Orders.
3. Answer `n` → `approval.resolved` `rejected`, answer says nothing changed; order still PENDING.
4. Repeat, answer `y` → `cancelled`, answer confirms; Orders shows CANCELLED.
5. Repeat, restart the service before answering `y` → still cancels (FR-013).
6. Repeat, type "yes" as a chat message instead → 409 `APPROVAL_PENDING`.
7. `SELECT decision, outcome FROM chat_actions ORDER BY proposed_at` → one row per proposal.
8. MCP smoke (`scripts/mcp_smoke.py`): `tools/list` has no `cancel_order`.

Contract details: [contracts/chat-approvals.md](contracts/chat-approvals.md); tables:
[data-model.md](data-model.md).
