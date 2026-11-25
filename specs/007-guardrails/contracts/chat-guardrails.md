# Contract delta: chat guardrails (US-07)

Changes to `docs/chat-contract.md`; `tests/contract/test_chat_contract.py` changes with it.

## Errors before the stream

| Status | Code | Body | When |
|---|---|---|---|
| 429 | `RATE_LIMITED` | `{code, message, scope: "user"}`, `Retry-After: <s>` | > `USER_MESSAGES_PER_MINUTE` posts in 60 s (seconds until a slot frees) |
| 429 | `RATE_LIMITED` | same | `USER_MESSAGES_PER_DAY` reached (seconds until next UTC midnight) |
| 429 | `RATE_LIMITED` | same | cooldown active (remaining cooldown seconds); also on the decision endpoint |
| 409 | `STREAM_ACTIVE` | `{code, message}` | the user has a stream open in any conversation |
| 422 | `VALIDATION_FAILED` | `{code, message}` | message empty after normalisation |

The decision endpoint gets the cooldown and minute 429s, never the day limit.

## SSE

- `message.end.finishReason` gains `blocked` (input guard refusal; no model call) and `tool_loop`
  (repeated identical tool call; safe message).
- `error` `BUDGET_EXCEEDED` and `RATE_LIMITED` carry `scope`: `user` | `global`.
- A leaked canary/secret in a streamed segment → `message.replaced` (unchanged mechanism).

## Blocked refusal (fixed text)

"I can't help with that request. I can help with your orders, products and notifications."
