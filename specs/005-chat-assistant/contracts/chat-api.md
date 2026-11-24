# Contract: Chat API + SSE (frozen for the frontend)

Published as `docs/chat-contract.md` and in the OpenAPI descriptions. Base path `/chat`; every
route needs `Authorization: Bearer <RS256 token>` (`x-authenticated`: any role). JSON camelCase.

## Routes

| Method | Path | Success | Errors |
|---|---|---|---|
| POST | `/chat/conversations` body `{title?: string ≤200}` | 201 `Conversation` | 401 403 422 503 |
| GET | `/chat/conversations?limit=1..100(20)&offset≥0(0)` | 200 `{items: Conversation[], total, limit, offset}` | 401 403 422 503 |
| GET | `/chat/conversations/{id}` | 200 `Conversation & {messages: Message[]}` (oldest first) | 401 403 404 503 |
| DELETE | `/chat/conversations/{id}` | 204 | 401 403 404 503 |
| POST | `/chat/conversations/{id}/messages` body `{content: string 1..2000 trimmed}` | 200 `text/event-stream` | 401 403 404 409 422 429 503 |

`Conversation`: `{id, title|null, createdAt, updatedAt}`. `Message`: `{id, role: user|assistant,
content, createdAt, finishReason|null, promptVersion|null, modelId|null, usage: {inputTokens,
outputTokens}|null}`.

Errors before the stream: `{"code": "...", "message": "..."}` — `UNAUTHENTICATED` 401
(`WWW-Authenticate: Bearer`, same body as `/mcp`), `FORBIDDEN` 403, `NOT_FOUND` 404 (unknown,
malformed or another user's id — identical), `STREAM_ACTIVE` / `CONVERSATION_FULL` 409,
`VALIDATION_FAILED` 422, `RATE_LIMITED` 429 + `Retry-After: <seconds>`, `UNAVAILABLE` 503.

## SSE events

Each event: `id: <n>` (1, 2, 3… per stream), `event: <name>`, `data: <JSON>`, blank line.
Keep-alive: `: keep-alive` comment every `CHAT_KEEPALIVE_SECONDS` (15) of silence.

```text
id: 1
event: message.start
data: {"conversationId":"9b1…","messageId":"c41…"}

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
data: {"messageId":"c41…","finishReason":"completed","promptVersion":"assistant.v1","modelId":"claude-opus-5-5","usage":{"inputTokens":1830,"outputTokens":42}}
```

Terminal error (instead of `message.end`): `event: error`,
`data: {"code":"LLM_UNAVAILABLE","message":"The assistant is unavailable right now. Please try again later."}`.

| Code | When | Fixed message |
|---|---|---|
| `LLM_UNAVAILABLE` | no model configured, timeout/retries exhausted | The assistant is unavailable right now. Please try again later. |
| `BUDGET_EXCEEDED` | daily budget or per-request cap | The assistant has reached its usage limit. Please try again later. |
| `RATE_LIMITED` | reserved (rate limit is a 429 before the stream) | You are sending messages too fast. Please wait a moment. |
| `TOOL_FAILED` | upstream rejected the caller's token | Your session has expired. Please sign in again. |
| `INTERNAL` | storage failure after the stream opened | Something went wrong. Please try again. |

Rules: exactly one `message.start` first; exactly one `message.end` or `error` last, then close.
`finishReason` sent: `completed | replaced | tool_limit` (`client_disconnected` stored only).
`tool.result.summary` is a fixed phrase per tool and outcome (count or found / not found /
unavailable / refused), never upstream data. `token.text` has always passed the gatekeeper.
