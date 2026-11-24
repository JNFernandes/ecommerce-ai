# Quickstart: US-02 validation

## Offline (gate)

```sh
uv run -q ruff check -q && uv run -q ruff format --check -q && uv run -q mypy --no-error-summary && uv run -q pytest -q --no-header --cov --cov-report=
UPDATE_SNAPSHOTS=1 uv run pytest tests/contract   # once, after adding the tools; commit tools.json
```

Expected: green gate; snapshot lists 5 tools. Key scenarios (respx stubs):
`test_inventory_tools.py` (US1, SC-002), `test_notifications_tool.py` (US2, SC-004),
`test_tool_isolation.py` (US3, SC-003/005), token equality and log scan (SC-001).

## Local stack (optional)

1. `ecommerce-infra` up (gateway `:8090`), customer token from ecommerce-identity.
2. `INVENTORY_BASE_URL=http://localhost:8090/api/inventory NOTIFICATIONS_BASE_URL=http://localhost:8090/api uv run python -m ecommerce_ai`
3. Run the existing MCP smoke script with `tools/list`, then `list_products`, `get_product {"productId":"SKU-003"}`,
   `list_my_notifications`. Expected: 5 tools; envelope-wrapped pages; stopping Inventory makes only
   its tools return "inventory unavailable".
