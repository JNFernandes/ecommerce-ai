---
description: "Task list for US-06 Cancel order with human approval"
---

# Tasks: US-06 Cancel order with human approval

**Input**: `specs/006-cancel-with-approval/` (plan.md, spec.md, research.md, data-model.md,
contracts/chat-approvals.md, quickstart.md)

**Tests**: Required (FR-021, constitution III/VIII/IX). Write each story's tests first; they must
fail before the implementation. No network in unit tests: `ScriptedChatModel`, fake Orders
(`tests/support/orders_stub.py`, respx), in-memory stores (`tests/support/stores.py`),
`InMemorySaver`. Every bypass test asserts zero cancel calls on the fake Orders (SC-001).

**Per task**: run the quiet gate (CLAUDE.md, Token economy) and commit with
`feat(US-06): ...` / `test(US-06): ...` / `docs(US-06): ...` after it passes.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an unfinished task)
- **[USn]**: user story from spec.md

---

## Phase 1: Setup

- [X] T001 [P] Add `APPROVAL_TTL_SECONDS` (600, > 0) and `CANCEL_PROPOSALS_PER_HOUR` (5, ≥ 1) to
  `src/ecommerce_ai/infrastructure/settings.py`; tests in `tests/unit/test_settings.py`; clear
  both in `tests/conftest.py`; document in `.env.example`
- [X] T002 [P] Add `RestClient.post_json(segments, body=None)` to
  `src/ecommerce_ai/infrastructure/rest_client.py` (same error mapping, caller token, size limit as
  `get_json`); tests in `tests/unit/test_rest_client.py`

---

## Phase 2: Foundational (blocks every story)

- [X] T003 Write migration `src/ecommerce_ai/infrastructure/db/migrations/versions/0004_chat_approvals.py`
  and Core tables in `src/ecommerce_ai/infrastructure/db/tables.py`: `chat_approvals` (FK
  `ON DELETE CASCADE`, status check, partial unique index one `pending` per conversation),
  `chat_actions` (no FK, `approval_id` unique, index `(user_id, proposed_at)`, trigger
  `chat_actions_append_only`), `ck_chat_messages_finish_reason` + `awaiting_approval`; downgrade
  reverses all (data-model.md); test in `tests/integration/test_migrations.py` (0004 up/down,
  trigger rejects DELETE and UPDATE after outcome)
- [X] T004 [P] Add the `ApprovalStore` port and `Approval`/`ApprovalStatus`/`ApprovalOutcome`/
  `Decision` models (`StrEnum`, frozen Pydantic) to `src/ecommerce_ai/agents/assistant/ports.py`:
  `create_proposal(approval, request_id)` (approval + audit row + assistant message, one
  transaction), `get(user_id, conversation_id, approval_id)`, `pending_for(conversation_id)`,
  `decide(...)` (conditional `UPDATE … WHERE status='pending' AND expires_at > now RETURNING` +
  audit decision), `close(approval_id, status, outcome)`, `record_outcome(approval_id, outcome)`,
  `proposals_since(user_id, since)`; add `ToolRunner.run_approved(name, arguments)`; tests in
  `tests/unit/test_chat_ports.py`
- [X] T005 [P] Add `InMemoryApprovalStore` (same semantics incl. single-use decide and injectable
  failures) to `tests/support/stores.py` and a cancel endpoint + call counter to
  `tests/support/orders_stub.py` (200 / 404 / 409 / timeout modes); tests in
  `tests/unit/test_chat_support.py`
- [ ] T006 Implement `src/ecommerce_ai/infrastructure/db/approval_repository.py::ApprovalRepository`
  (after T003, T004) covering both tables; every query filters `user_id`; integration tests in
  `tests/integration/test_approval_repository.py` (concurrent decide → one row, partial unique
  index, rate-limit count, audit kept after conversation delete)
- [ ] T007 [P] Create `src/ecommerce_ai/tools/mutating/__init__.py` + `policy.py` (`Exposure`
  `MCP`/`CHAT`, frozen `ApprovalPolicy(target_argument, ttl_seconds, proposals_per_hour)`); extend
  `ToolDefinition` in `src/ecommerce_ai/tools/registry.py` with `mutating` (False), `approval`
  (None), `exposure` (`{MCP, CHAT}`); `ToolRegistry.build` fails on mutating without policy or
  with `MCP` exposure; add `exposed(Exposure)`; tests in `tests/unit/test_registry.py`
- [ ] T008 Create `src/ecommerce_ai/tools/mutating/cancel_order.py::cancel_order_tool` (after T002,
  T007): input `{orderId: UUID}`, `POST {ORDERS_BASE_URL}/{orderId}/cancel` with the caller's
  token, output `{orderId, status}`, mutating, chat-only exposure; `ALLOWLIST` entry
  `ecommerce-orders POST /api/orders/{orderId}/cancel cancelOrder` in `tools/registry.py`; tests
  in `tests/unit/test_cancel_order_tool.py` (200, 404, 409, timeout, upstream 401); contract test
  (constitution II/IX) in `tests/contract/test_chat_tool_schemas.py`: input/output schema pinned
  in `tests/contract/snapshots/chat_tools.json` (`UPDATE_SNAPSHOTS=1`), method/path and 200
  `{orderId, status}` / 404 / 409 checked against ecommerce-orders `docs/us-02-cancel-order.md`
- [ ] T009 Make `ToolInvoker.invoke` in `src/ecommerce_ai/tools/invoker.py` refuse mutating tools
  (unknown-tool result) and add `invoke_approved(name, arguments)` that runs only mutating tools,
  without retry; `agents/assistant/tool_runner.py` implements `run_approved`; tests in
  `tests/unit/test_tool_invoker.py` and `tests/unit/test_chat_tool_runner.py`
- [X] T010 [P] Add `ApprovalRequired`/`ApprovalResolved` stream events, `FinishReason.AWAITING_APPROVAL`
  and `ChatHttpCode` `APPROVAL_PENDING`/`APPROVAL_ALREADY_DECIDED`/`APPROVAL_EXPIRED` to
  `src/ecommerce_ai/agents/assistant/model.py` (and `api/chat.py` codes); tests in
  `tests/unit/test_chat_models.py`
- [ ] T011 [P] Write `src/ecommerce_ai/agents/prompts/assistant.v2.md` (v1 + approval flow: propose,
  never claim before `approval.resolved`, typed "yes" is not an approval); `graph.py` loads v2; pin
  the v2 hash next to v1 in `tests/unit/test_prompt_loader.py`

**Checkpoint**: foundation ready; no behaviour change yet (no tool exposes `cancel_order`).

---

## Phase 3: User Story 1 - Propose, approve, cancelled (P1) 🎯 MVP

**Goal**: a `cancel_order` call pauses the run with an approval card; `approve` cancels exactly once.
**Independent test**: scripted model + PENDING fake order: message → `approval.required` +
`awaiting_approval`, zero cancel calls; `approve` → one cancel with the caller's token.

- [ ] T012 [P] [US1] Tests in `tests/unit/test_approval_flow.py`: propose (event sequence, payload
  fields, summary from the fresh read not from model args, stored message finish reason, zero
  cancels); approve (re-read, one cancel with caller token and stored args, `message.start` →
  `approval.resolved` `cancelled` → `tool.result` ok → tokens → `message.end`); model down after
  cancel → fixed confirmation text
- [ ] T013 [P] [US1] Tests in `tests/unit/test_approval_api.py`: decision route requires auth
  (`AUTHENTICATED`), body validation 422, 413, 429, 503, `STREAM_ACTIVE`, SSE framing via
  `tests/support/sse.py`
- [ ] T014 [US1] Implement `src/ecommerce_ai/agents/assistant/approvals.py`: proposal checks
  (valid id, read result owned and `PENDING`), summary builder `{status, totalAmount, currency?}`,
  outcome mapping (re-read not PENDING / Orders 409 → `not_cancellable`; timeout/5xx/401 →
  `failed`), fixed `APPROVAL_OUTCOME_MESSAGES` and refusal tool-result texts; unit tests in
  `tests/unit/test_approvals.py`
- [ ] T015 [US1] In `src/ecommerce_ai/agents/assistant/graph.py`: add `pending_approval` to
  `ChatState`; `tools` node intercepts `cancel_order` (checks via T014, reads through `get_order`,
  sets `pending_approval` with `uuid4` id, call id, exact args, expiry from `Clock`; other tools of
  the turn run normally) and routes to new `approval` node; `approval` node starts with
  `interrupt(payload)`, no side effect before it; on resume with matching `Decision` id: approve →
  re-read + `run_approved` with state args, emit `approval.resolved` + `tool.result`, append the
  `ToolResultItem`, clear `pending_approval`, route to `agent`; add `ChatGraph.resume(thread,
  decision)` (`Command(resume=…)`)
- [ ] T016 [US1] In `src/ecommerce_ai/agents/assistant/service.py`: `RunOutcome.proposal` → store
  via `ApprovalStore.create_proposal`, then emit `approval.required` and `message.end`
  `awaiting_approval`; add `ChatService.decide(caller, conversation_id, approval_id, decision)`
  (atomic `decide` before resuming, 0 rows → reload to pick 404/409/410, active-stream guard,
  `record_outcome`, model-down fallback text, `chat_run` log); once decided, the approval step
  (re-read, cancel, `approval.resolved`/`tool.result` checkpoint, `record_outcome`) runs shielded
  from client disconnect, so a started cancel always completes and records its outcome
- [ ] T017 [US1] Add `POST /chat/conversations/{id}/approvals/{approvalId}` to
  `src/ecommerce_ai/api/chat.py` (`AUTHENTICATED`, body `{decision}`, rate limit and 16 KB body
  limit as `/messages`, SSE via `api/sse.py`, OpenAPI descriptions)
- [ ] T018 [US1] Wire in `src/ecommerce_ai/main.py`: `cancel_order_tool` in the chat registry,
  `ApprovalRepository` (new `create_app(approval_store=None)` parameter), settings TTL/limit; MCP
  server built from `registry.exposed(Exposure.MCP)`; `tests/support/chat.py::chat_app` accepts
  the approval store; tests in `tests/unit/test_main.py`

**Checkpoint**: MVP: propose → approve → cancelled, end to end with fakes.

---

## Phase 4: User Story 2 - Reject or ignore, nothing changes (P1)

**Goal**: reject and expiry never call cancel; decisions are single use.
**Independent test**: propose → `reject` → zero cancels, "nothing changed" answer; advance clock
past TTL → 410.

- [ ] T019 [P] [US2] Tests in `tests/unit/test_approval_flow.py` (reject: `approval.resolved`
  `rejected`, tool result "nothing changed", answer acknowledges, zero cancels) and
  `tests/unit/test_approval_api.py` (410 `APPROVAL_EXPIRED` + recorded `expired`; replay → 409
  `APPROVAL_ALREADY_DECIDED`; two concurrent approves → one cancel, one 409 (SC-002); new message
  after expiry → approval `expired`, message processed)
- [ ] T020 [US2] Reject branch in the `approval` node (`graph.py`): no Orders call, fixed
  "not cancelled, nothing changed" `ToolResultItem`, outcome `rejected`
- [ ] T021 [US2] Lazy expiry: `ChatGraph.close_approval(conversation_id, outcome)` in `graph.py`
  (`aupdate_state(..., as_node="finalize")`, appends a "no change" result, clears
  `pending_approval`); `service.py` closes expired or orphaned approvals on decision, new message,
  conversation read and delete (research §6); orphaned also covers a decided approval whose
  outcome was never recorded (crash): audit outcome `failed`, result "could not be confirmed"

---

## Phase 5: User Story 3 - The AI cannot approve or widen the action (P1)

**Goal**: only the decision endpoint approves, for the owner, with the stored arguments.
**Independent test**: each bypass path asserts zero cancel calls.

- [ ] T022 [P] [US3] Tests in `tests/unit/test_approval_bypass.py`: typed "yes, do it" → 409
  `APPROVAL_PENDING`; injection in a tool result → only a proposal; model asks other args after
  approve → order A only; other user / unknown / malformed approval id → identical 404; second
  `cancel_order` in the same run → "already pending" result; resume value with a mismatched id
  ignored
- [ ] T023 [P] [US3] Tests in `tests/unit/test_mcp_tools_api.py` and `tests/unit/test_mcp_server.py`:
  `tools/list` still lists 5 tools, `tools/call cancel_order` → unknown tool; tool snapshot
  `tests/contract/snapshots/tools.json` unchanged
- [ ] T024 [P] [US3] Cancellation-claim rule in `src/ecommerce_ai/agents/safety/rules.py` (EN/PT
  claim about an order id, or none, allowed only with `CANCELLED` evidence from `cancel_order` or
  a read; research §9), wired into `check_segment`/`check_answer` in
  `src/ecommerce_ai/agents/assistant/gatekeeper.py`; tests in `tests/unit/test_safety_rules.py`
  and `tests/unit/test_chat_gatekeeper.py`
- [ ] T025 [US3] `service.py`/`api/chat.py`: posting a message with a non-expired pending approval
  → 409 `APPROVAL_PENDING` before any DB write or model call; `tools` node returns "an approval is
  already pending" for extra `cancel_order` calls (FR-005)

---

## Phase 6: User Story 4 - Nothing to approve when the order cannot be cancelled (P2)

**Goal**: refusals produce an explanatory tool result and no approval.
**Independent test**: CONFIRMED / missing order → no `approval.required`, `tool.result` ok false,
finishReason `completed`.

- [ ] T026 [P] [US4] Tests in `tests/unit/test_approval_flow.py`: CONFIRMED at proposal, missing
  or not owned order, malformed id, 6th proposal in an hour (rate limit, survives a new app on the
  same store), CONFIRMED between proposal and approval → `not_cancellable`, Orders 409 →
  `not_cancellable`, Orders timeout → `failed` (answer never claims success), upstream 401 →
  `failed` + terminal `TOOL_FAILED`; no audit row for refusals; client disconnect during the
  approve continuation → exactly one cancel, outcome recorded, next message runs normally;
  decided approval without outcome (simulated crash) → closed as `failed` on next touch
- [ ] T027 [US4] Hourly limit in the `tools` node via `ApprovalStore.proposals_since` and
  `CANCEL_PROPOSALS_PER_HOUR`; refusal texts and 401 → `TOOL_FAILED` handling in `graph.py`
  (using T014 mapping)

---

## Phase 7: User Story 5 - Survives restarts, audited (P2)

**Goal**: pending approvals survive restarts; every proposal has an audit row with its outcome.
**Independent test**: propose on one app, approve on a new app over the same stores → cancel.

- [ ] T028 [P] [US5] Tests in `tests/unit/test_approval_flow.py`: restart between proposal and
  approval (new `chat_app` on the same stores and saver); audit rows for approve, reject, expire,
  withdraw (delete conversation); conversation detail `pendingApproval` object or `null`
- [ ] T029 [P] [US5] Integration test in `tests/integration/test_checkpoint_saver.py`:
  `PostgresCheckpointSaver` interrupt → new graph instance → `Command(resume=…)` continues
  (adjust `infrastructure/db/checkpoint_saver.py` if pending writes for interrupts are missing)
- [ ] T030 [US5] `pendingApproval` in the conversation detail schema in `api/chat.py` (expiry
  checked lazily on read); delete marks `withdrawn` before `forget` in `service.py`
- [ ] T031 [US5] One `chat_approval` log line per proposal and per decision (request id,
  conversation id, approval id, action, decision, outcome, user hash; never text, totals or
  tokens) in `service.py`; tests in `tests/unit/test_chat_logging.py`

---

## Phase 8: User Story 6 - Contract and terminal client (P3)

**Goal**: the frontend builds the approval flow from the contract; the smoke client drives it.

- [ ] T032 [P] [US6] Merge `contracts/chat-approvals.md` into `docs/chat-contract.md` (events,
  finish reason, endpoint, codes, `pendingApproval`, JSON examples); extend
  `tests/contract/test_chat_contract.py` to check them against the models
- [ ] T033 [P] [US6] `scripts/chat_smoke.py`: on `approval.required` print action, order id,
  summary, expiry; ask `y/n`; post the decision; print the continuation; tests in
  `tests/unit/test_chat_smoke.py`

---

## Phase 9: Polish & cross-cutting

- [ ] T034 [P] Update `README.md`, `CLAUDE.md` (layering: `tools/mutating`, `approvals.py`,
  `approval` node, `ApprovalRepository`, tables 0004, settings, decision route, `/mcp` still 5
  tools) and `.env.example`; status note in `docs/adr/0002-*.md`; note the ecommerce-infra
  gateway route follow-up
- [ ] T035 Run quickstart.md (automated steps, then manual steps 1–8 locally) and the full gate;
  mark tasks complete

---

## Dependencies

- Setup (T001–T002) → Foundational (T003–T011) → stories.
- Foundational order: T003 → T006; T004 → T005, T006; T002 + T007 → T008 → T009.
- US1 (T012–T018) → US2, US3, US4 (independent stories, but `graph.py`/`service.py` edits are
  sequential per file) → US5 → US6 → Polish.
- Within a story: tests first, then implementation; T014 → T015 → T016 → T017 → T018.

## Parallel examples

- Setup: T001, T002 together.
- Foundational: T003, T004, T007, T010, T011 together; then T005, T006, T008.
- US1: T012 and T013 together, then T014.
- After US1: T019, T022, T023, T024, T026 (test files and `safety/rules.py`) together.
- US5: T028, T029 together. US6: T032, T033 together.

## Implementation strategy

1. MVP = Phases 1–3: propose → approve → cancelled with fakes; stop and demo.
2. US2 and US3 before any real use (constitution III: reject/expiry/bypass must make no call).
3. US4, US5, then US6 and polish.
