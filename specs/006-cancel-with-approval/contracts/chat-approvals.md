# Contract additions: approvals (US-06)

Merged into `docs/chat-contract.md` during implementation; checked by
`tests/contract/test_chat_contract.py`.

## New SSE events (message stream)

`approval.required` — then `message.end` with `finishReason: "awaiting_approval"`:

```json
{"approvalId": "9b1e…", "action": "cancel_order", "orderId": "3f2a…",
 "summary": {"status": "PENDING", "totalAmount": "59.90", "currency": "EUR"},
 "expiresAt": "2026-11-25T10:10:00Z"}
```

`approval.resolved` (decision stream only):

```json
{"approvalId": "9b1e…", "decision": "approve", "outcome": "cancelled"}
```

`outcome`: `cancelled` | `rejected` | `not_cancellable` | `failed`.

## Decision endpoint

`POST /chat/conversations/{conversationId}/approvals/{approvalId}` — `AUTHENTICATED`, body
`{"decision": "approve" | "reject"}`, ≤ 16 KB. Success: `200 text/event-stream`:
`message.start` → `approval.resolved` → `tool.result` (`cancel_order`) → `token`* → `message.end`
(or terminal `error`). Same ids/keep-alive/disconnect rules as the message stream.

| Status | code | When |
|---|---|---|
| 404 | `NOT_FOUND` | unknown, malformed or another user's conversation/approval (identical) |
| 409 | `APPROVAL_ALREADY_DECIDED` | approval not pending |
| 409 | `STREAM_ACTIVE` | a stream is running in the conversation |
| 410 | `APPROVAL_EXPIRED` | past `expiresAt` (recorded `expired`) |
| 413 / 422 / 429 / 503 | `PAYLOAD_TOO_LARGE` / `VALIDATION_FAILED` / `RATE_LIMITED` / `UNAVAILABLE` | as US-05 |

## Changes to existing routes

- `POST /chat/conversations/{id}/messages`: 409 `APPROVAL_PENDING` while a non-expired approval
  is pending.
- `GET /chat/conversations/{id}`: adds `"pendingApproval"` (same object as `approval.required`)
  or `null`.
- Message `finishReason` may be `awaiting_approval`.
- `/mcp`: `tools/list` unchanged (5 tools); `tools/call` `cancel_order` → unknown tool.
