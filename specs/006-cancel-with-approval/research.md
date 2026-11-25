# Research: US-06 Cancel order with human approval

No NEEDS CLARIFICATION remained after the spec; these are the design decisions.

## §1 Pause and resume

- **Decision**: a new `approval` node whose first statement is `interrupt(payload)`; the decision
  endpoint resumes the thread with `Command(resume=Decision(approval_id, decision))`.
- **Why**: constitution III requires a LangGraph `interrupt`; the node is re-executed from the
  top on resume, so it has no side effect before `interrupt()` and does all work after it.
- **Rejected**: ending the run and starting a fresh one on decision (loses the paused tool call,
  not an interrupt); `interrupt_before=["tools"]` (pauses every tool round, not only mutating).

## §2 Where the proposal is built

- **Decision**: the `tools` node intercepts `cancel_order`: validates the id, checks the
  per-conversation pending slot and the hourly limit (port), re-reads the order through
  `get_order`, and either answers a refusal `ToolResultItem` (FR-003/005/015) or puts a
  `pending_approval` (new `uuid4` id, exact arguments, summary from the read, expiry) in state and
  routes to `approval`. Other tools of the same turn run normally.
- **Why**: all checks happen once, before the checkpoint that precedes the interrupt; the node
  that is re-executed on resume (§1) stays side-effect free until the decision.
- **Rejected**: doing the read inside `approval` (re-run on resume → duplicate reads/events).

## §3 Persisting the proposal

- **Decision**: the graph has no DB side effects; `ChatService` reads `pending_approval` from the
  final state (`RunOutcome.proposal`), stores the approval row + audit row + assistant message in
  one transaction, then emits `approval.required` and `message.end` `awaiting_approval`.
- **Why**: same pattern as US-05 answer storage; a crash before the store leaves a paused thread
  without approval row, which the next touch closes (§6).

## §4 Single use and binding

- **Decision**: `decide()` = one `UPDATE chat_approvals SET status, decided_at … WHERE id AND
  conversation_id AND user_id AND status='pending' AND expires_at > now RETURNING`, plus the audit
  `decision` in the same transaction, before resuming the graph. 0 rows → reload to pick 404 /
  409 / 410. The `approval` node uses the arguments from state, never from the resume value or
  the model; the resume value carries only id + decision and must match the state's id.
- **Why**: FR-007/008; the row lock makes concurrent decisions yield one execution (SC-002).
- **Rejected**: in-process lock only (does not survive restarts, not atomic with the store).

## §5 Execution after approve

- **Decision**: `approval` node: re-read (`get_order`); not PENDING/not found → `not_cancellable`;
  else `ToolRunner.run_approved("cancel_order", stored_args)`; Orders 409 → `not_cancellable`;
  timeout/5xx/401 → `failed` (401 also ends `TOOL_FAILED`). Emits `approval.resolved` and
  `tool.result`, appends the `ToolResultItem` for the paused `tool_use`, then routes to `agent`.
  Reject → result "not cancelled, nothing changed", no Orders call.
- **Fallback**: if the model is unavailable after the decision, a fixed per-outcome text
  (`APPROVAL_OUTCOME_MESSAGES`) is the answer (ADR 0003); the cancellation is never hidden.

## §6 Expiry and withdrawal (lazy)

- **Decision**: touched on decision, new message, conversation read and delete. Expired or
  orphaned → `ChatGraph.close_approval(conversation_id, outcome)` uses `aupdate_state(...,
  as_node=finalize)` to append a "no change" `ToolResultItem` and clear `pending_approval`
  (same technique as `record_interrupted`), so the transcript stays valid for the model and the
  interrupt is gone. Delete marks `withdrawn` before `forget`. A decided approval whose outcome
  was never recorded (crash) is closed the same way with outcome `failed`; after a decision the
  approval step is shielded from client disconnect, so only a crash can leave that state.
- **Why**: spec assumption (no sweeper); a new input on a paused thread must not leave a
  `tool_use` without result.

## §7 Tool policy and exposure

- **Decision**: `tools/mutating/` holds `Exposure` (`MCP`, `CHAT`), `ApprovalPolicy` (frozen:
  `target_argument`, `ttl_seconds`, `proposals_per_hour`) and `cancel_order_tool`.
  `ToolDefinition` gains `mutating`, `approval`, `exposure`. `ToolRegistry.build` fails on a
  mutating tool without policy or exposed on `MCP`; `registry.exposed(Exposure.MCP)` feeds
  `build_mcp_server`. `ToolInvoker.invoke` refuses mutating tools; only `invoke_approved` runs
  them. The domain rule "only PENDING" lives in `agents/assistant/approvals.py` (no decisions in
  tools).
- **Upstream**: `RestClient.post_json` added; `POST {ORDERS_BASE_URL}/{orderId}/cancel`
  (Orders: 200 `{orderId,status}`, 404 unknown or not owner, 409 not PENDING) with the caller's
  token; allowlist entry `ecommerce-orders POST /api/orders/{orderId}/cancel cancelOrder`.

## §8 Storage

- **Decision**: migration 0004: `chat_approvals` (FK conversation `ON DELETE CASCADE`, partial
  unique index one `pending` per conversation) and `chat_actions` (no FK, append-only enforced by
  a trigger rejecting DELETE and any UPDATE once `outcome` is set); extend the
  `chat_messages.finish_reason` check with `awaiting_approval`. `ApprovalRepository` covers both
  tables. Rate limit = `count(chat_actions WHERE user_id AND proposed_at > now-1h)`.
- **Why**: approvals carry summary/arguments and die with the conversation; the audit must not.

## §9 Gatekeeper rule (FR-012)

- **Decision**: new pure rule in `agents/safety/rules.py`: a cancellation claim (cancel(l)ed /
  will cancel / cancelamento…) about an order id, or without id, is allowed only if the run's
  evidence holds that order (or any order, without id) as `CANCELLED` from `cancel_order` or a
  read. Wired into `check_segment`/`check_answer`.

## §10 Prompt

- **Decision**: `agents/prompts/assistant.v2.md` (v1 untouched, both hashes pinned); graph uses
  v2. Explains proposing, never claiming before `approval.resolved`, typed "yes" is not approval.
