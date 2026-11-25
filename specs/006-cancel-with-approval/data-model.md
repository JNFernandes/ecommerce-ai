# Data model: US-06

Migration `0004_chat_approvals` (hand-written). Timestamps `timestamptz`, UTC.

## `chat_approvals`

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | generated in the `tools` node, in the interrupt payload |
| conversation_id | uuid FK → `chat_conversations.id` ON DELETE CASCADE | |
| user_id | text | token subject |
| action | varchar(32) | `cancel_order` |
| arguments | jsonb | exact arguments, e.g. `{"orderId": "…"}`; never changed |
| summary | jsonb | `{status, totalAmount, currency?}` from the fresh read |
| status | varchar(16) | check: `pending`, `approved`, `rejected`, `expired`, `withdrawn` |
| proposed_at, expires_at | timestamptz | `expires_at = proposed_at + APPROVAL_TTL_SECONDS` |
| decided_at | timestamptz null | |

Partial unique index `(conversation_id) WHERE status = 'pending'` (FR-005).

State: `pending → approved | rejected` (decision endpoint), `pending → expired` (touched after
`expires_at`), `pending → withdrawn` (conversation deleted or orphaned interrupt). Transitions only
from `pending`, by conditional `UPDATE … WHERE status='pending'`.

## `chat_actions` (audit, FR-016)

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| approval_id | uuid UNIQUE | no FK (kept after the approval is deleted) |
| conversation_id | uuid | no FK |
| user_id | text | |
| action | varchar(32) | |
| order_id | uuid | |
| decision | varchar(16) null | `approve`, `reject` |
| outcome | varchar(24) null | `cancelled`, `rejected`, `not_cancellable`, `failed`, `expired`, `withdrawn` |
| request_id | varchar(64) null | request of the proposal; the decision's request id is logged |
| proposed_at | timestamptz | indexed `(user_id, proposed_at)` for the hourly limit |
| decided_at | timestamptz null | |

Trigger `chat_actions_append_only`: rejects DELETE, and UPDATE when `OLD.outcome IS NOT NULL`
(decision/outcome are set once). No message text, summaries or tokens.

## `chat_messages` change

`ck_chat_messages_finish_reason` recreated with `awaiting_approval` added.

## Graph state (`ChatState` additions)

- `pending_approval: dict | None` — `{approvalId, action, arguments, summary, callId,
  expiresAt}`; set by `tools`, cleared by `approval` or `close_approval`.
- Resume value: `Decision {approval_id, decision: approve|reject}`; ignored if `approval_id`
  differs from state.

## Settings

`APPROVAL_TTL_SECONDS` (600, > 0), `CANCEL_PROPOSALS_PER_HOUR` (5, ≥ 1). Existing
`ORDERS_BASE_URL`, `TOOL_TIMEOUT_SECONDS` reused.

## Tool definition additions

`mutating: bool` (default False), `approval: ApprovalPolicy | None`, `exposure:
frozenset[Exposure]` (default `{MCP, CHAT}`). Invariant checked at startup: `mutating ⇒ approval
is not None and Exposure.MCP not in exposure`.
