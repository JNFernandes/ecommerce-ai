---
description: "Task list for US-05 Chat assistant with streaming"
---

# Tasks: US-05 Chat assistant with streaming

**Input**: `specs/005-chat-assistant/` (plan.md, spec.md, research.md, data-model.md,
contracts/chat-api.md, quickstart.md)

**Tests**: Required (FR-028, constitution VIII/IX). Write each story's tests first; they must fail
before the implementation. No network in unit tests (fakes from `tests/support/`).

**Per task**: run the quiet gate (CLAUDE.md, Token economy) and commit with
`feat(US-05): ...` / `test(US-05): ...` / `docs(US-05): ...` after it passes.

**Order note**: US2 (conversations CRUD, P1) comes before US1 (streaming, P1) because posting a
message needs an existing conversation; both are P1 and together form the MVP.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an unfinished task)
- **[USn]**: user story from spec.md

---

## Phase 1: Setup

- [X] T001 Add chat settings to `src/ecommerce_ai/infrastructure/settings.py`:
  `CHAT_KEEPALIVE_SECONDS` (15), `CHAT_REQUEST_TOKEN_CAP` (20000), `CHAT_RATE_LIMIT_MESSAGES`
  (10), `CHAT_RATE_LIMIT_WINDOW_SECONDS` (60), `CHAT_MAX_TOOL_ROUNDS` (5),
  `CHAT_MAX_MESSAGES` (100), `CHAT_TOOL_TIMEOUT_SECONDS` (reuse `TOOL_*` if one exists); positive
  bounds; tests in `tests/unit/test_settings.py`; clear the new vars in `tests/conftest.py`;
  document them in `.env.example`
- [X] T002 [P] Add `AUTHENTICATED = {"x-authenticated": True}` to
  `src/ecommerce_ai/api/route_policy.py` and accept it in `_is_declared`; tests in
  `tests/unit/test_route_policy.py`

---

## Phase 2: Foundational (blocks every story)

- [X] T003 [P] Move `TokenBudget` and `Clock` from `src/ecommerce_ai/agents/triage/ports.py` to
  new `src/ecommerce_ai/agents/ports.py`; `agents/triage/ports.py` re-exports them; existing
  triage tests stay green unchanged
- [X] T004 [P] Extract the pure gatekeeper rules (`Evidence.from_strings`, promise, URL/contact,
  ungrounded number/amount/UUID/date, internal name, HTML) from
  `src/ecommerce_ai/agents/triage/gatekeeper.py` into new
  `src/ecommerce_ai/agents/safety/__init__.py` + `agents/safety/rules.py`; triage gatekeeper
  delegates; `tests/unit/test_gatekeeper.py` unchanged and green; add
  `tests/unit/test_safety_rules.py` for the extracted functions (research Â§9)
- [X] T005 [P] Extract tool invocation from `src/ecommerce_ai/tools/server.py` into new
  `src/ecommerce_ai/tools/invoker.py::ToolInvoker` (lookup in `ToolRegistry` + `ALLOWLIST`,
  Pydantic input validation, per-tool `asyncio.timeout`, `ToolFailure` â†’ `{error, message}`,
  unknown name / `ValidationError` â†’ refused result never executed, upstream 401 flagged as
  `upstream_unauthenticated`); `server.py` uses it; tests in `tests/unit/test_tool_invoker.py`;
  `tests/unit/test_mcp_server.py` and the tool snapshot stay green (research Â§7)
- [X] T006 [P] Create `src/ecommerce_ai/api/auth.py::require_caller` FastAPI dependency reusing
  `TokenVerifier` and the exact 401 (`WWW-Authenticate: Bearer`) / 403 / 503 bodies of
  `api/mcp.py` (move them to shared helpers, `mcp.py` imports them); binds `RequestCaller` via
  `bind_caller`; any token with â‰¥1 role passes; tests in `tests/unit/test_chat_auth.py`
  (research Â§14)
- [X] T007 [P] Create domain models in `src/ecommerce_ai/agents/assistant/__init__.py` and
  `agents/assistant/model.py`: `FinishReason`, `ChatErrorCode` + fixed messages (contract table),
  transcript items `UserTurn`/`AssistantText`/`ToolCallItem`/`ToolResultItem` (frozen, JSON
  dumpable), `StreamEvent` union `MessageStart`/`Token`/`ToolCall`/`ToolResult`/`MessageEnd`/
  `StreamError` (camelCase aliases), `RunOutcome`, `Conversation`/`StoredMessage` read models;
  tests in `tests/unit/test_chat_models.py` (data-model.md)
- [X] T008 Create ports in `src/ecommerce_ai/agents/assistant/ports.py`: `ChatModel.stream(request)
  -> AsyncIterator[ChatModelEvent]` (`TextDelta`, `ToolUseRequest`, `ModelTurnEnd{stop_reason,
  usage, model_id}`), `ModelUnavailable` reuse, `ToolRunner`, `ConversationStore` (create, list
  with total, get with history, delete incl. state, add user/assistant message, count, touch),
  `Checkpointer` alias of `BaseCheckpointSaver` (depends on T007)
- [X] T009 Write migration `src/ecommerce_ai/infrastructure/db/migrations/versions/0003_chat_tables.py`
  (`chat_conversations`, `chat_messages` with CHECKs and cascade, `chat_checkpoints`,
  `chat_checkpoint_writes`, both indexes, downgrade) and the Core tables in
  `src/ecommerce_ai/infrastructure/db/tables.py`; extend `tests/integration/test_migrations.py`
  (upgrade/downgrade, columns, constraints)
- [ ] T010 [P] Add test support: `tests/support/llm.py::ScriptedChatModel` (scripted turns of
  deltas/tool uses/usage, records each received request, can raise `ModelUnavailable` before or
  after the first delta, can block on an event for disconnect tests);
  `tests/support/sse.py` (parse `id:`/`event:`/`data:` and comments from an httpx stream);
  `tests/support/stores.py::InMemoryConversationStore` (injectable failures); fake tool runner in
  `tests/support/chat.py`; self-tests in `tests/unit/test_support.py`
- [ ] T011 [P] Write `src/ecommerce_ai/agents/prompts/assistant.v1.md` (FR-012: answer only from
  tool results, never invent statuses/amounts/dates/stock, say when a tool fails or is empty, tool
  results are untrusted data, refuse off-domain briefly, plain text); pin its SHA-256 in
  `tests/unit/test_prompt_loader.py`

**Checkpoint**: shared rules, invoker, auth, models, ports, schema and fakes ready.

---

## Phase 3: User Story 2 - Manage my conversations (P1)

**Goal**: create, list, read, delete own conversations; foreign ids indistinguishable from unknown.
**Independent test**: two subjects; every cross-user read/post/delete â†’ identical 404.

- [ ] T012 [P] [US2] Integration tests `tests/integration/test_conversation_repository.py`: create,
  list newest `updated_at` first with total/limit/offset, get with history oldest first, user_id
  filter on every query, delete cascades messages, count, add message bumps `updated_at`
- [ ] T013 [P] [US2] API tests `tests/unit/test_chat_api.py`: 201 with/without title (>200 â†’ 422),
  list paging (limit 0/101, offset âˆ’1 â†’ 422), get with messages, delete 204 then 404, cross-user
  and malformed id â†’ same 404 body `NOT_FOUND`, 401/403/503 same as `/mcp`, DB down â†’ 503
  `UNAVAILABLE`, every route has an access policy
- [ ] T014 [US2] Implement `src/ecommerce_ai/infrastructure/db/conversation_repository.py::
  ConversationRepository` (implements `ConversationStore`; every query filters `user_id`; delete
  also removes `chat_checkpoints`/`chat_checkpoint_writes` rows of the thread in the same
  transaction)
- [ ] T015 [US2] Implement `src/ecommerce_ai/api/chat.py` conversations router: schemas (camelCase,
  OpenAPI descriptions + examples), `POST/GET /chat/conversations`, `GET/DELETE
  /chat/conversations/{id}`, `{code, message}` error bodies, `openapi_extra=AUTHENTICATED`,
  `require_caller`, DB errors (`is_transient_db_error`) â†’ 503
- [ ] T016 [US2] Wire the repository and router in `src/ecommerce_ai/main.py` (`create_app` gains
  `conversation_store=None` for tests) and `src/ecommerce_ai/api/app.py`; extend
  `tests/unit/test_main.py`

**Checkpoint**: conversations CRUD works end to end.

---

## Phase 4: User Story 1 - Ask a question and see the answer stream (P1) ðŸŽ¯ MVP

**Goal**: post a message, get the SSE event sequence grounded in tool results, both messages stored.
**Independent test**: scripted model calls `list_my_orders`; exact event sequence, ids, payloads.

- [ ] T017 [P] [US1] Tests `tests/unit/test_segmenter.py`: split at `. ! ?`+space and newline,
  remainder at turn end, no empty segments, decimals/ids not split mid-token
- [ ] T018 [P] [US1] Tests `tests/unit/test_chat_sse.py`: frame format (`id`/`event`/`data`, blank
  line), ids strictly increasing from 1, keep-alive comment after `CHAT_KEEPALIVE_SECONDS` of
  silence (injected short interval), headers `text/event-stream`, `Cache-Control: no-cache`,
  `X-Accel-Buffering: no`
- [ ] T019 [P] [US1] Tests `tests/unit/test_chat_graph.py`: happy path with one tool (events
  `message.start`, `tool.call`, `tool.result ok`, `token`+, `message.end completed` with usage
  and `promptVersion` `assistant.v1`); tool failure/empty â†’ `ok false`/empty summary; tool results
  reach the model as `<tool_result name="â€¦">` delimited blocks; tool list = registry only
- [ ] T020 [P] [US1] Tests `tests/unit/test_anthropic_chat_model.py`: maps `messages.stream`
  deltas/tool_use/usage to `ChatModelEvent`s, tools from registry input schemas, `max_retries=0`,
  timeout, effort, API errors â†’ `ModelUnavailable` (fake client, no network)
- [ ] T021 [US1] Implement `src/ecommerce_ai/agents/assistant/segmenter.py` (research Â§10)
- [ ] T022 [P] [US1] Implement `src/ecommerce_ai/agents/assistant/summaries.py` (fixed phrase per
  tool and outcome: count / found / not found / unavailable / refused; never upstream data)
- [ ] T023 [US1] Implement `src/ecommerce_ai/agents/assistant/graph.py`: `ChatState` TypedDict
  (transcript append reducer), nodes `guard_input â†’ agent â‡„ tools â†’ gatekeeper â†’ finalize`, edges
  by state, custom events via `get_stream_writer()`, transcript re-validated on load, model
  request built from the prompt + transcript (research Â§3, Â§4, Â§8); gatekeeper node a pass-through
  until T035
- [ ] T024 [US1] Implement `src/ecommerce_ai/agents/assistant/service.py::ChatService.post` (order
  of checks per research Â§16 minus limits added in US5; stores the user message, runs the graph
  with thread id = conversation id, yields `StreamEvent`s, stores the assistant message on
  `message.end`, one `chat_run` log line per research Â§17)
- [ ] T025 [US1] Implement `src/ecommerce_ai/api/sse.py` framer (ids, keep-alive via
  `asyncio.wait_for`, `StreamingResponse`) and the `POST /chat/conversations/{id}/messages`
  route in `src/ecommerce_ai/api/chat.py` (body 1â€“2000 trimmed â†’ else 422, nothing stored)
- [ ] T026 [US1] Implement `src/ecommerce_ai/infrastructure/llm/anthropic_chat_model.py::
  AnthropicChatModel` + builder (research Â§6); only this package imports `anthropic`
- [ ] T027 [US1] Wire `ChatService`, `ToolInvoker`-backed `ToolRunner`, `InMemorySaver` default for
  tests (`create_app(..., chat_model=None, checkpointer=None)`) and the Anthropic chat model when
  `model_configured` in `src/ecommerce_ai/main.py`; off-domain refusal covered via a scripted
  answer test in `tests/unit/test_chat_graph.py`
- [ ] T028 [US1] Log test in `tests/unit/test_chat_logging.py`: exactly one `chat_run` line with
  request_id, conversation_id, latency, tokens, tools `{name, outcome, duration_ms}`, finish
  reason, promptVersion, modelId, `user_hash`; no content, argument values, results, token or raw
  subject (FR-026)

**Checkpoint**: MVP (US2 + US1) streams grounded answers.

---

## Phase 5: User Story 3 - Follow-up questions remember the conversation (P1)

**Goal**: state persists per conversation, survives restarts, never shared.
**Independent test**: two posts with an app rebuild in between share one `InMemorySaver`.

- [ ] T029 [P] [US3] Tests `tests/unit/test_chat_memory.py`: second post (after rebuilding the app
  with the same saver) sends the model the first question, tool results and answer; two
  conversations of one user see no shared state; delete removes the thread
  (`adelete_thread`)
- [ ] T030 [P] [US3] Integration tests `tests/integration/test_checkpoint_saver.py`: `aput`/
  `aget_tuple`/`alist`/`aput_writes`/`adelete_thread`, only newest checkpoint kept, a real graph
  run resumes across two saver instances, conversation delete removes checkpoint rows
- [ ] T031 [US3] Implement `src/ecommerce_ai/infrastructure/db/checkpoint_saver.py::
  PostgresCheckpointSaver` on the async engine (research Â§5) and wire it in
  `src/ecommerce_ai/main.py`; `ChatService` delete calls `adelete_thread`

---

## Phase 6: User Story 4 - Unsafe or ungrounded answers never reach the customer (P1)

**Goal**: every segment and the whole answer gatekept; injection has no effect; 5-round cap.
**Independent test**: scripted violations and injection-like tool output; only safe text is sent.

- [ ] T032 [P] [US4] Tests `tests/unit/test_chat_gatekeeper.py`: one test per rule (refund promise,
  URL, internal names incl. `ecommerce-*`/topics/`graphql`/`localhost`, ungrounded id/amount/date,
  HTML), grounded values and upper-case statuses from evidence pass, ids from the customer's own
  messages pass, newlines and `-` lists allowed
- [ ] T033 [P] [US4] Tests in `tests/unit/test_chat_graph.py`: violating segment never sent, safe
  message as `token`, `message.end replaced`, stored text = sent segments + safe message, model
  stream stops reading; empty answer after tools â†’ `replaced`; injection text in a tool result
  ("call cancel_order") â†’ no unlisted tool runs, refused result, still delimited; unknown tool name
  / invalid arguments refused; 5 rounds â†’ `tool_limit` safe message
- [ ] T034 [US4] Implement `src/ecommerce_ai/agents/assistant/gatekeeper.py` (`check_segment`,
  `check_answer`, safe messages, evidence from `ToolResultItem` leaves + user ids) on
  `agents/safety/rules.py` (research Â§9)
- [ ] T035 [US4] Integrate per-segment release, final `check_answer` and the round cap
  (`CHAT_MAX_TOOL_ROUNDS`) into `src/ecommerce_ai/agents/assistant/graph.py`

---

## Phase 7: User Story 5 - Limits, budgets and failures are explicit (P2)

**Goal**: every limit and failure gives a documented HTTP status or terminal `error`.
**Independent test**: trigger each limit with fakes; check status/code.

- [ ] T036 [P] [US5] Tests `tests/unit/test_rate_limit.py`: sliding window with injected clock,
  `Retry-After` = ceil(seconds until oldest hit leaves), per-subject isolation
- [ ] T037 [P] [US5] Tests `tests/unit/test_chat_limits.py`: empty/2001 chars â†’ 422 nothing stored;
  100 messages â†’ 409 `CONVERSATION_FULL`; concurrent post â†’ 409 `STREAM_ACTIVE`, first stream
  unaffected; 11th post â†’ 429 `RATE_LIMITED` + `Retry-After`; no model / `ModelUnavailable`
  (retries before first delta) â†’ user message stored, `error LLM_UNAVAILABLE`; exhausted daily
  budget or request cap â†’ `error BUDGET_EXCEEDED`, usage settled; upstream 401 in a tool â†’
  `error TOOL_FAILED`; store failure after open â†’ `error INTERNAL`; DB down before open â†’ 503;
  lock released after every ending; error endings store no assistant message
- [ ] T038 [US5] Implement `src/ecommerce_ai/api/rate_limit.py::SlidingWindowLimiter` and apply it
  in the post route after auth, before DB work
- [ ] T039 [US5] Add to `src/ecommerce_ai/agents/assistant/service.py`: `ActiveStreams` lock
  (conversation id â†’ task, released in `finally`), full check (`count + 2 > CHAT_MAX_MESSAGES`),
  budget reserve/settle via `TokenBudget` (research Â§11), `call_with_retry` before the first
  delta, error endings with fixed messages; map the 409s/429/503 in `src/ecommerce_ai/api/chat.py`

---

## Phase 8: User Story 6 - Client disconnects are clean (P2)

**Goal**: disconnect cancels the run, keeps the partial answer, frees the lock.
**Independent test**: disconnect after the first `token`; partial stored, new post accepted.

- [ ] T040 [P] [US6] Tests `tests/unit/test_chat_disconnect.py`: cancel after first `token` â†’ no
  further model/tool calls, assistant message = sent text with `client_disconnected`, usage
  settled, lock released (new post accepted); delete during an active stream cancels it first,
  then 204
- [ ] T041 [US6] Implement shielded `finish_disconnected` in
  `src/ecommerce_ai/agents/assistant/service.py` and `CancelledError` handling in
  `src/ecommerce_ai/api/sse.py` (research Â§2); delete cancels and awaits the active task

---

## Phase 9: User Story 7 - Runs without dependencies; manual smoke test (P3)

**Goal**: starts without model/Kafka/DB; terminal smoke client.
**Independent test**: app with no model key and failing probes; smoke client against a fake server.

- [ ] T042 [P] [US7] Tests in `tests/unit/test_main.py`: no model, Kafka or DB â†’ `/health` 200,
  `/ready` unchanged rules, chat post â†’ `error LLM_UNAVAILABLE`; tests
  `tests/unit/test_chat_smoke.py` for `scripts/chat_smoke.py` over an httpx `MockTransport`
- [ ] T043 [US7] Implement `scripts/chat_smoke.py` (`TOKEN`, `CHAT_URL` default
  `http://localhost:8002/chat`, creates a conversation, prints tokens and tool events, stdlib SSE
  parsing; research Â§18)
- [ ] T044 [US7] Add the assistant case to `tests/integration/test_live_llm.py` (`live_llm`
  marker, one real streamed answer passes the gatekeeper)

---

## Phase 10: Polish & cross-cutting

- [ ] T045 [P] Write `docs/chat-contract.md` from `contracts/chat-api.md` (every route, event,
  code and HTTP error with a JSON example); `tests/contract/test_chat_contract.py` checks event
  names, error codes and payload fields against the `StreamEvent`/schema models
- [ ] T046 [P] Update `README.md`, `CLAUDE.md` (layering: `agents/assistant`, `agents/safety`,
  `tools/invoker.py`, `api/chat.py`/`sse.py`/`auth.py`/`rate_limit.py`, new tables, CHAT_*
  settings, `/chat` routes) and `.env.example`; mark US-09 `TODO(EVAL_SUITE)` for assistant evals
- [ ] T047 Run quickstart.md steps 1â€“2 locally and the full gate; mark tasks complete

---

## Dependencies

- Setup (T001â€“T002) â†’ Foundational (T003â€“T011) â†’ stories.
- US2 (T012â€“T016) â†’ US1 (T017â€“T028) â†’ US3, US4, US5 (independent of each other) â†’ US6 (needs
  US5's lock) â†’ US7; Polish last.
- Within a story: tests first, then implementation; T023 before T024 before T025.

## Parallel examples

- Foundational: T003, T004, T005, T006, T007, T010, T011 together; T008 after T007.
- US1: T017, T018, T019, T020 together; then T021 and T022 in parallel.
- After US1: US3 (T029â€“T031), US4 (T032â€“T035) and US5 (T036â€“T039) on separate files, except
  `graph.py` (US4) and `service.py` (US5) edits are sequential per file.

## Implementation strategy

1. MVP = Phases 1â€“4 (US2 + US1): stream a grounded answer into a stored conversation; validate
   with `ScriptedChatModel` tests, then stop and demo.
2. Add US3 (memory) and US4 (gatekeeper) before any real customer use (constitution III).
3. Add US5, US6, US7, then polish and docs.
