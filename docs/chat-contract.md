# Chat API and SSE contract (US-05, US-06)

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
| POST | `/chat/conversations/{id}/approvals/{approvalId}` | 200 `text/event-stream` | 401 403 404 409 410 413 422 429 503 |

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
user messages; an assistant message holds exactly the text the customer received.
`pendingApproval` is the action waiting for the caller's decision (the same object as the
`approval.required` event) or `null`; an approval past `expiresAt` is closed when read and shows
as `null`:

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
  ],
  "pendingApproval": null
}
```

A conversation paused on a proposed cancellation:

<!-- schema: ConversationDetailResponse -->
```json
{
  "id": "9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11",
  "title": null,
  "createdAt": "2026-11-25T10:00:00Z",
  "updatedAt": "2026-11-25T10:02:00Z",
  "messages": [
    {
      "id": "c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b",
      "role": "assistant",
      "content": "I can cancel it for you once you approve.",
      "createdAt": "2026-11-25T10:02:00Z",
      "finishReason": "awaiting_approval",
      "promptVersion": "assistant.v2",
      "modelId": "claude-opus-5-5",
      "usage": {"inputTokens": 1900, "outputTokens": 60}
    }
  ],
  "pendingApproval": {
    "approvalId": "9b1e6c2d-3a4f-4b5c-8d6e-7f8a9b0c1d2e",
    "action": "cancel_order",
    "orderId": "3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b",
    "summary": {"status": "PENDING", "totalAmount": 59.9, "currency": null},
    "expiresAt": "2026-11-25T10:12:00Z"
  }
}
```

Post a message (`content` 1 to 2000 characters after trimming; otherwise 422 and nothing is
stored). The response is the event stream below:

<!-- schema: PostMessageRequest -->
```json
{"content": "Where is my last order?"}
```

Decide a pending approval, once (US-06). `approve` re-reads the order and cancels it with the
caller's token only if it is still `PENDING`; `reject` changes nothing. A message posted while an
approval is pending gets 409 `APPROVAL_PENDING`: typing "yes" never approves. The response is the
decision stream below:

<!-- schema: DecisionRequest -->
```json
{"decision": "approve"}
```

`finishReason` values: `completed`, `replaced`, `tool_limit`, `awaiting_approval` (the run paused
for an approval, US-06), `blocked` (an input guard refused the message; no model call, US-07),
`tool_loop` (a repeated identical tool call ended the answer with the safe message, US-07), and
`client_disconnected` (stored only, when the client went away mid-answer; never sent in
`message.end`).

Every message is normalised before any check (Unicode NFKC, zero-width, bidi and control
characters removed except newline and tab); the 2000-character limit applies to the raw text, and
the normalised text is what is stored and answered. A message an input rule blocks gets one
`token` with the fixed refusal "I can't help with that request. I can help with your orders,
products and notifications." and `finishReason` `blocked` (`modelId` `null`, no tokens spent); it
is stored, but neither it nor the refusal is shown to the model in later turns.

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
| 422 | `VALIDATION_FAILED` | The request is not valid. | bad body, `limit` or `offset`; a message empty once normalised |
| 429 | `RATE_LIMITED` | You are sending messages too fast. Please wait a moment. | more than 10 posts in 60 s per user; `Retry-After: <seconds>` |
| 503 | `UNAVAILABLE` | The assistant is unavailable right now. Please try again later. | the database is down |
| 409 | `APPROVAL_PENDING` | Please approve or reject the pending action first. | a message posted while an approval is pending |
| 409 | `APPROVAL_ALREADY_DECIDED` | This action has already been decided. | a decision for an approval no longer pending |
| 410 | `APPROVAL_EXPIRED` | This action has expired. Please ask again. | a decision after `expiresAt` (`APPROVAL_TTL_SECONDS`, 600) |

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
data: {"messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b","finishReason":"completed","promptVersion":"assistant.v1","modelId":"claude-opus-5-5","usage":{"inputTokens":1830,"outputTokens":42},"violations":[]}
```

An answer whose whole text broke a rule after it was streamed (`violations` names the rules):

```text
id: 1
event: message.start
data: {"conversationId":"9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11","messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"}

id: 2
event: token
data: {"text":"Your order is <b"}

id: 3
event: token
data: {"text":" class=x>ready."}

id: 4
event: message.replaced
data: {"messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b","text":"Sorry, I can't give you a reliable answer to that. Please ask me about your orders, products or notifications."}

id: 5
event: message.end
data: {"messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b","finishReason":"replaced","promptVersion":"assistant.v3","modelId":"claude-opus-5-5","usage":{"inputTokens":900,"outputTokens":12},"violations":["html"]}
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
| `message.replaced` | `messageId`, `text` (the full text the message now holds) |
| `message.end` | `messageId`, `finishReason`, `promptVersion`, `modelId` (`null` when no model answered), `usage`, `violations` (names of the gatekeeper rules a replaced answer broke, e.g. ungrounded_fact; never text; else empty) |
| `error` | `code`, `message` |
| `approval.required` | `approvalId`, `action`, `orderId`, `summary`, `expiresAt` |
| `approval.resolved` | `approvalId`, `decision`, `outcome` |

A proposed cancellation (US-06): the run pauses before any change, sends `approval.required` and
ends with `finishReason` `awaiting_approval`. `summary` holds `status`, `totalAmount` (`null` when
unknown) and `currency` (`null` unless Orders reports one), read from Orders just now:

```text
id: 1
event: message.start
data: {"conversationId":"9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11","messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"}

id: 2
event: tool.call
data: {"callId":"toolu_02","name":"cancel_order","arguments":{"orderId":"3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b"}}

id: 3
event: approval.required
data: {"approvalId":"9b1e6c2d-3a4f-4b5c-8d6e-7f8a9b0c1d2e","action":"cancel_order","orderId":"3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b","summary":{"status":"PENDING","totalAmount":59.9,"currency":null},"expiresAt":"2026-11-25T10:12:00Z"}

id: 4
event: message.end
data: {"messageId":"c41a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b","finishReason":"awaiting_approval","promptVersion":"assistant.v2","modelId":"claude-opus-5-5","usage":{"inputTokens":1900,"outputTokens":60},"violations":[]}
```

The decision stream starts with `approval.resolved`; `outcome` is `cancelled`, `rejected`,
`not_cancellable` (the order is no longer `PENDING`) or `failed` (Orders could not confirm):

```text
id: 1
event: message.start
data: {"conversationId":"9b1f0c2e-6a4d-4c3e-8f5a-2d7e1c0b9a11","messageId":"d52b3c4d-5e6f-4a70-9b8c-0d1e2f3a4b5c"}

id: 2
event: approval.resolved
data: {"approvalId":"9b1e6c2d-3a4f-4b5c-8d6e-7f8a9b0c1d2e","decision":"approve","outcome":"cancelled"}

id: 3
event: tool.result
data: {"callId":"toolu_02","name":"cancel_order","ok":true,"summary":"Order cancelled"}

id: 4
event: token
data: {"text":"Your order 3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b is now cancelled."}

id: 5
event: message.end
data: {"messageId":"d52b3c4d-5e6f-4a70-9b8c-0d1e2f3a4b5c","finishReason":"completed","promptVersion":"assistant.v2","modelId":"claude-opus-5-5","usage":{"inputTokens":2100,"outputTokens":30},"violations":[]}
```

`token.text` has always passed the gatekeeper. When a segment breaks a rule, it is never sent: a
fixed safe message is sent instead and the stream stops reading the model. When only the whole
answer breaks a rule (one that spans segments, so the customer already saw the text),
`message.replaced` comes before `message.end`: the client shows its `text` in place of everything
the message showed so far, and the stored message holds only that text. Either way `finishReason`
is `replaced`; past 5 tool rounds
(`CHAT_MAX_TOOL_ROUNDS`) the answer ends with a fixed message and `tool_limit`.

`tool.result.summary` is a fixed phrase, never upstream data: `<n> orders found` /
`No orders found` (list tools: orders, products, notifications), `Order found` /
`Order not found` (lookups: order, product), `Orders unavailable` (any other failure),
`Request refused` (unknown tool or invalid arguments, never run). For `cancel_order`:
`Order cancelled`, `Cancellation rejected, nothing changed`, `Order cannot be cancelled`,
`Cancellation could not be confirmed`, `Cancellation expired, nothing changed`,
`Cancellation withdrawn, nothing changed`, and, when no approval was created, `Order not found`,
`Approval already pending`, `Too many cancellation requests` (5 proposals per hour,
`CANCEL_PROPOSALS_PER_HOUR`) or `Orders unavailable`. The `/mcp` endpoint never offers
`cancel_order`.

### Terminal error codes

| Code | Fixed message | When |
|---|---|---|
| `LLM_UNAVAILABLE` | The assistant is unavailable right now. Please try again later. | no model configured, timeout or retries exhausted |
| `BUDGET_EXCEEDED` | The assistant has reached its usage limit. Please try again later. | daily token budget or per-message cap reached |
| `RATE_LIMITED` | You are sending messages too fast. Please wait a moment. | reserved (the rate limit is a 429 before the stream) |
| `TOOL_FAILED` | Your session has expired. Please sign in again. | an upstream rejected the caller's token |
| `INTERNAL` | Something went wrong. Please try again. | storage failure after the stream opened |
