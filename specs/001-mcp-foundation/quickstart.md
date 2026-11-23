# Quickstart: validate US-01

## Offline (gate)

```sh
uv run pytest -q tests/unit tests/contract   # auth, tools, registry, schema contracts
```

Expected: the cases of spec SC-001…SC-006 pass, no network access.

## Against the local stack

Prerequisites: `ecommerce-infra` up (Identity, Orders, gateway on `:8090`), a customer access
token in `$TOKEN` (login via Identity, see its README).

```sh
uv run python -m ecommerce_ai     # :8002, starts even with Identity/Orders down
curl -s localhost:8002/health     # 200
```

List tools, then call one:

```sh
H='-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream"'
curl -s localhost:8002/mcp/ -H "Authorization: Bearer $TOKEN" $H \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl -s localhost:8002/mcp/ -H "Authorization: Bearer $TOKEN" $H \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_my_orders","arguments":{"limit":5}}}'
```

Expected: two tools; the call returns an `untrusted_data` envelope with only the caller's orders.
Without the header, or with a tampered token: 401 with `WWW-Authenticate: Bearer`. Responses
follow [contracts/mcp-endpoint.md](contracts/mcp-endpoint.md).
