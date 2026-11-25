# Implementation Plan: US-06 Cancel order with human approval

**Branch**: `feature/US-06-cancel-with-approval` | **Date**: 2026-11-25 | **Spec**: [spec.md](spec.md)

## Summary

The US-05 chat graph gains one mutating tool, `cancel_order`, chat-only. The `tools` node turns a
`cancel_order` call into a checked proposal (fresh read, PENDING, ownership, hourly limit); a new
`approval` node pauses with LangGraph `interrupt()` on the Postgres checkpointer. `ChatService`
stores the approval + audit row and streams `approval.required` / `awaiting_approval`. A decision
endpoint records the decision atomically, resumes with `Command(resume=…)` and streams the
continuation through the existing SSE framer. Decisions: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: existing only (FastAPI, LangGraph `interrupt`/`Command`, httpx,
SQLAlchemy/asyncpg, anthropic)
**Storage**: PostgreSQL 16, migration 0004: `chat_approvals`, `chat_actions`, finish-reason check
([data-model.md](data-model.md))
**Testing**: pytest-asyncio; `ScriptedChatModel`, fake Orders (respx / fake tool handlers),
in-memory approval store, `InMemorySaver` and a fresh app on the same stores for restarts;
integration on real PostgreSQL (repository, trigger, saver interrupt round-trip)
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: approval card ≤ 2 s after the model's call (SC-003)
**Constraints**: caller's token only (ADR 0001); no Orders cancel without a recorded `approve`
(ADR 0002); single instance (stream guard in memory)
**Scale/Scope**: 1 route, 1 node, 1 tool, 2 tables, 2 settings, 1 prompt version

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ router → `ChatService.decide`; proposal/approval logic and PENDING rule in `agents/assistant`; `tools/mutating` declarative policy + handler only; SQL in `infrastructure/db`. |
| II Security | ✅ `AUTHENTICATED` policy on the new route; approval bound to token subject + conversation; identical 404; caller's JWT forwarded; `cancel_order` never on `/mcp` (startup check). |
| III Human-in-the-loop | ✅ `interrupt` before the call; exact stored arguments; single-use, TTL; reject/expiry/other user → no call; gatekeeper cancellation rule; text can never approve. |
| IV Reliability | ✅ timeouts via `TOOL_TIMEOUT_SECONDS`, no retry on the cancel (non-idempotent; outcome `failed`); fixed per-outcome text when the model is down; budget unchanged. |
| V Events | ✅ untouched (Orders publishes `order-cancelled`). |
| VI Evals | ✅ interim gate: bypass/injection tests, pinned `assistant.v2` hash; US-09 adds approval cases (TODO(EVAL_SUITE)). |
| VII Observability | ✅ one `chat_approval` log line per proposal and per decision (FR-017), plus `chat_run`. |
| VIII/IX Tests | ✅ FR-021 list as unit tests; integration for 0004, trigger, saver resume; ≥ 80%. |
| XI Gate | ✅ unchanged. |

Post-design re-check: no deviations.

## Project Structure

```text
specs/006-cancel-with-approval/ plan.md research.md data-model.md quickstart.md
                                contracts/chat-approvals.md

src/ecommerce_ai/
├── tools/
│   ├── mutating/{__init__,policy,cancel_order}.py  # Exposure, ApprovalPolicy, cancel_order_tool
│   ├── registry.py        # + mutating/approval/exposure, startup checks, exposed(), allowlist
│   ├── invoker.py         # invoke refuses mutating; invoke_approved
│   └── server.py          # registers only MCP-exposed tools
├── agents/
│   ├── safety/rules.py    # + cancellation-claim rule
│   ├── prompts/assistant.v2.md
│   └── assistant/
│       ├── approvals.py   # proposal checks, PENDING rule, outcome mapping, fixed messages
│       ├── graph.py       # tools intercept, approval node (interrupt), resume(), close_approval()
│       ├── model.py       # ApprovalRequired/Resolved events, AWAITING_APPROVAL, error codes
│       ├── ports.py       # ApprovalStore; ToolRunner.run_approved
│       ├── gatekeeper.py  # wire the new rule
│       └── service.py     # store proposal, decide(), lazy expiry, withdraw on delete
├── api/chat.py            # POST .../approvals/{approvalId}, pendingApproval, 409 APPROVAL_PENDING
├── infrastructure/
│   ├── rest_client.py     # + post_json
│   ├── settings.py        # + APPROVAL_TTL_SECONDS, CANCEL_PROPOSALS_PER_HOUR
│   └── db/ tables.py approval_repository.py migrations/versions/0004_chat_approvals.py
└── main.py                # wire tool, store, settings; MCP gets exposed tools only

scripts/chat_smoke.py (y/n)   docs/chat-contract.md   docs/adr/0002 (status note)
tests/
├── support/ stores.py (+approval store) orders fake for cancel
├── unit/    test_approval_flow.py test_approval_bypass.py test_approval_api.py
│            test_tool_registry.py (+policy) test_mcp_tools (+absent) test_chat_gatekeeper.py
│            test_prompt_loader.py (+v2 hash) test_rest_client.py (+post)
├── contract/ test_chat_contract.py (+events, codes, pendingApproval)
│            test_chat_tool_schemas.py + snapshots/chat_tools.json (cancel_order vs Orders docs)
└── integration/ test_approval_repository.py test_checkpoint_saver.py (+interrupt/resume)
                 test_migrations.py (+0004)
```

**Also**: `README.md`, `CLAUDE.md`, `.env.example`; gateway route for the decision endpoint is a
follow-up in ecommerce-infra (stream, no buffering limit).

## Complexity Tracking

None.
