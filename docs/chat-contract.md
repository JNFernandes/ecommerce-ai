# Chat API and SSE contract (US-05)

Frozen contract for the frontend. Base path `/chat` on the service; through the gateway
(ecommerce-infra) every path is prefixed with `/api/ai`, e.g. `POST /api/ai/chat/conversations`.
The gateway exposes exactly these routes; the stream route has a 300 s response timeout. JSON is camelCase; timestamps are UTC ISO 8601.
`tests/contract/test_chat_contract.py` checks every example below against the code: each block
tagged `<!-- schema: Name -->` must validate against that model and use exactly its field names,
and every event, code and fixed message must match the code.

## Authentication

Every route needs `Authorization: Bearer <RS256 access token>` from ecommerce-identity. Any role
is enough (`x-authenticated`); a conversation belongs to the token's `sub`. These bodies are the
same as `POST /mcp`, identical for every bad token:

| Status | When | Body |
|---|---|---|
| 401 | missing, malformed, expired or wrongly signed token (`WWW-Authenticate: Bearer`) | `{"detail": "Not authenticated"}` |
| 403 | token without any role | `{"detail": "Forbidden"}` |
| 503 | the identity signing keys cannot be fetched | `{"detail": "Service unavailable"}` |

## Routes

| Method | Path | Success | Errors |
|---|---|---|---|
| POST | `/chat/conversations` | 201 `Conversation` | 401 403 413 422 503 |
| GET | `/chat/conversations?limit=1..100 (20)&offset≥0 (0)` | 200 `ConversationPage` | 401 403 422 503 |
| GET | `/chat/conversations/{id}` | 200 `ConversationDetail` (messages oldest first) | 401 403 404 503 |
| DELETE | `/chat/conversations/{id}` | 204, no body (an answer in progress is cancelled first) | 401 403 404 503 |
| POST | `/chat/conversations/{id}/messages` | 200 `text/event-stream` | 401 403 404 409 413 422 429 503 |

Create a conversation (`title` optional, ≤ 200 characters, trimmed; blank means none):

<!-- schema: CreateConversationRequest -->
```json
{"title": "My last order"}
```

<!-- schema: ConversationResponse -->
```json
{
  "id": "9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11",
  "title": "My last order",
  "createdAt": "2026-11-25T10:00:00Z",
  "updatedAt": "2026-11-25T10:00:00Z"
}
```

List (newest activity first):

<!-- schema: ConversationListResponse -->
```json
{
  "items": [
    {
      "id": "9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11",
      "title": null,
      "createdAt": "2026-11-25T10:00:00Z",
      "updatedAt": "2026-11-25T10:02:00Z"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

Read one with its history. `finishReason`, `promptVersion`, `modelId` and `usage` are `null` on
user messages; an assistant message holds exactly the text the customer received:

<!-- schema: ConversationDetailResponse -->
```json
{
  "id": "9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11",
  "title": null,
  "createdAt": "2026-11-25T10:00:00Z",
  "updatedAt": "2026-11-25T10:02:00Z",
  "messages": [
    {
      "id": "0d3c7b8a-1f2e-4a5b-9c6d-7e8f9a0b1c2d",
      "role": "user",
      "content": "Where is my last order?",
      "createdAt": "2026-11-25T10:01:58Z",
      "finishReason": null,
      "promptVersion": null,
      "modelId": null,
      "usage": null
    },
    {
      "id": "c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b",
      "role": "assistant",
      "content": "Your last order was placed on 2026-11-20 and is SHIPPED.",
      "createdAt": "2026-11-25T10:02:00Z",
      "finishReason": "completed",
      "promptVersion": "assistant.v1",
      "modelId": "claude-opus-5-5",
      "usage": {"inputTokens": 1830, "outputTokens": 42}
    }
  ]
}
```

Post a message (`content` 1 to 2000 characters after trimming; otherwise 422 and nothing is
stored). The response is the event stream below:

<!-- schema: PostMessageRequest -->
```json
{"content": "Where is my last order?"}
```

`finishReason` values: `completed`, `replaced`, `tool_limit`, and `client_disconnected` (stored
only, when the client went away mid-answer; never sent in `message.end`).

## Errors before the stream

Every error answered before the stream opens has the body `{"code", "message"}` with a fixed
message:

<!-- schema: ErrorBody -->
```json
{"code": "NOT_FOUND", "message": "Conversation not found."}
```

| Status | Code | Fixed message | When |
|---|---|---|---|
| 404 | `NOT_FOUND` | Conversation not found. | unknown, malformed or another user's id (identical) |
| 409 | `STREAM_ACTIVE` | An answer is already in progress in this conversation. | a second post while one streams |
| 409 | `CONVERSATION_FULL` | This conversation is full. Please start a new one. | the conversation holds 100 messages (`CHAT_MAX_MESSAGES`) |
| 413 | `PAYLOAD_TOO_LARGE` | The request is too large. | a `/chat` request body over 16 KB |
| 422 | `VALIDATION_FAILED` | The request is not valid. | bad body, `limit` or `offset` |
| 429 | `RATE_LIMITED` | You are sending messages too fast. Please wait a moment. | more than 10 posts in 60 s per user; `Retry-After: <seconds>` |
| 503 | `UNAVAILABLE` | The assistant is unavailable right now. Please try again later. | the database is down |

## SSE events

Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
Each event is `id: <n>` (1, 2, 3… per stream), `event: <name>`, `data: <JSON>`, then a blank
line. After `CHAT_KEEPALIVE_SECONDS` (15) of silence the server sends the comment
`: keep-alive`. Exactly one `message.start` comes first and exactly one `message.end` or `error`
comes last, then the stream closes.

A grounded answer with one tool call:

```text
id: 1
event: message.start
data: {"conversationId":"9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11","messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"}

id: 2
event: tool.call
data: {"callId":"toolu_01","name":"list_my_orders","arguments":{"limit":5}}

id: 3
event: tool.result
data: {"callId":"toolu_01","name":"list_my_orders","ok":true,"summary":"3 orders found"}

id: 4
event: token
data: {"text":"Your last order was placed on 2026-11-20 and is SHIPPED."}

id: 5
event: message.end
data: {"messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b","finishReason":"completed","promptVersion":"assistant.v1","modelId":"claude-opus-5-5","usage":{"inputTokens":1830,"outputTokens":42}}
```

A failed answer (the `error` event replaces `message.end`; no assistant message is stored, the
user message is):

```text
id: 1
event: message.start
data: {"conversationId":"9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11","messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"}

id: 2
event: error
data: {"code":"LLM_UNAVAILABLE","message":"The assistant is unavailable right now. Please try again later."}
```

| Event | Data fields |
|---|---|
| `message.start` | `conversationId`, `messageId` |
| `tool.call` | `callId`, `name`, `arguments` (never the token) |
| `tool.result` | `callId`, `name`, `ok`, `summary` |
| `token` | `text` |
| `message.end` | `messageId`, `finishReason`, `promptVersion`, `modelId` (`null` when no model answered), `usage` |
| `error` | `code`, `message` |

`token.text` has always passed the gatekeeper. When a segment or the whole answer breaks a rule,
the rest is replaced by a fixed safe message and `finishReason` is `replaced`; past 5 tool rounds
(`CHAT_MAX_TOOL_ROUNDS`) the answer ends with a fixed message and `tool_limit`.

`tool.result.summary` is a fixed phrase, never upstream data: `<n> orders found` /
`No orders found` (list tools: orders, products, notifications), `Order found` /
`Order not found` (lookups: order, product), `Orders unavailable` (any other failure),
`Request refused` (unknown tool or invalid arguments, never run).

### Terminal error codes

| Code | Fixed message | When |
|---|---|---|
| `LLM_UNAVAILABLE` | The assistant is unavailable right now. Please try again later. | no model configured, timeout or retries exhausted |
| `BUDGET_EXCEEDED` | The assistant has reached its usage limit. Please try again later. | daily token budget or per-message cap reached |
| `RATE_LIMITED` | You are sending messages too fast. Please wait a moment. | reserved (the rate limit is a 429 before the stream) |
| `TOOL_FAILED` | Your session has expired. Please sign in again. | an upstream rejected the caller's token |
| `INTERNAL` | Something went wrong. Please try again. | storage failure after the stream opened |
