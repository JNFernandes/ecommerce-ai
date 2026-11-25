# Implementation Plan: US-05 Chat assistant with streaming

**Branch**: `feature/US-05-chat-assistant` | **Date**: 2026-11-24 | **Spec**: [spec.md](spec.md)

## Summary

A `/chat` API (conversations CRUD + a message post that answers as SSE) runs one LangGraph graph
per message: guard input → agent ⇄ tools (≤5 rounds, the five read-only tools in-process with the
caller's token) → gatekeeper → finalize. Model text is released one gatekept segment at a time;
state persists per conversation in a Postgres checkpointer on the existing asyncpg engine.
Limits (length, 100 messages, one stream, rate, per-request cap + shared daily budget) and every
failure end in a documented status or terminal `error` event. Decisions: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: existing only (FastAPI, LangGraph + langgraph-checkpoint, anthropic,
SQLAlchemy/asyncpg); no new package
**Storage**: PostgreSQL 16, migration 0003: conversations, messages, checkpoints, checkpoint
writes ([data-model.md](data-model.md))
**Testing**: pytest-asyncio; `ScriptedChatModel`, fake tool handlers, `InMemorySaver`, in-memory
stores; httpx `ASGITransport` SSE parsing; integration on real PostgreSQL; one `live_llm` test
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: `message.start` ≤ 1 s after posting (SC-001); keep-alive every 15 s
**Constraints**: caller's token only (ADR 0001), read-only tools, nothing connects at startup,
token never in prompt/event/log; single instance for lock and rate limit (US-07 shares them)
**Scale/Scope**: 5 routes, 1 graph (5 nodes), 4 tables, ~6 settings, 1 prompt file

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ routers frame SSE and call one `ChatService` entry point; graph, gatekeeper, segmenter, prompt in `agents/assistant`; tool invocation in `tools`; SQL, checkpointer, Anthropic adapter in `infrastructure`; ports only from nodes. |
| II Security | ✅ same RS256 verification and 401/403/503; `x-authenticated` policy (default deny kept); caller's own JWT via `bind_caller`; registry allowlist only; ownership filter → identical 404; tool output delimited as untrusted data; token/content never logged. |
| III AI safety | ✅ no mutating tool; deterministic gatekeeper per segment and on the whole answer, safe message on violation; tool arguments validated by Pydantic before running; model output never partially used past a violation. |
| IV Reliability | ✅ starts without deps; LLM timeout + bounded retries (before first delta); fixed error messages as the fallback; per-request cap + daily budget (shared `llm_token_usage`); lock released on every ending. |
| V Event contracts | ✅ not touched (no Kafka in this story). |
| VI Evals | ✅ interim gate: gatekeeper rule tests, injection tests, pinned `assistant.v1` hash, `live_llm`; US-09 suite must add assistant cases (TODO(EVAL_SUITE)). |
| VII Observability | ✅ one `chat_run` log line per run with latency, tokens, tools, promptVersion, modelId; correlation id = request id; hashed user id. |
| VIII/IX Tests | ✅ unit per node/rule/limit/ending, SSE contract tests, integration for SQL + checkpointer, ≥ 80%. |
| XI Gate | ✅ unchanged. |
| Stack | ✅ LangGraph checkpointer in infrastructure; schema via migration only. |

Post-design re-check: no deviations.

## Project Structure

```text
specs/005-chat-assistant/  plan.md research.md data-model.md quickstart.md contracts/chat-api.md

src/ecommerce_ai/
├── agents/
│   ├── ports.py              # TokenBudget, Clock (moved; triage.ports re-exports)
│   ├── safety/rules.py       # shared pure gatekeeper rules + Evidence (triage delegates)
│   ├── prompts/assistant.v1.md
│   └── assistant/
│       ├── model.py          # enums, transcript items, StreamEvent union, RunOutcome
│       ├── ports.py          # ChatModel, ToolRunner, ConversationStore, Checkpointer alias
│       ├── gatekeeper.py     # check_segment / check_answer, safe messages
│       ├── segmenter.py      # deltas -> sentence/line segments
│       ├── summaries.py      # tool.result summary phrases
│       ├── graph.py          # StateGraph + nodes, custom stream writer
│       └── service.py        # ChatService: post/run/finish (lock, budget, store, chat_run log)
├── tools/invoker.py          # ToolInvoker (validate, timeout, map failures) shared with server.py
├── api/
│   ├── chat.py               # routers + schemas + OpenAPI examples
│   ├── sse.py                # framer: ids, keep-alive, disconnect handling
│   ├── auth.py               # require_caller dependency, shared 401/403/503 bodies
│   ├── rate_limit.py         # SlidingWindowLimiter
│   └── route_policy.py       # + AUTHENTICATED
├── infrastructure/
│   ├── settings.py           # + CHAT_* settings
│   ├── llm/anthropic_chat_model.py
│   └── db/ tables.py conversation_repository.py checkpoint_saver.py
│          migrations/versions/0003_chat_tables.py
└── main.py                   # wire ChatService, checkpointer, limiter, active streams

scripts/chat_smoke.py   docs/chat-contract.md
tests/
├── support/ llm.py (+ScriptedChatModel) sse.py (parser) stores.py (+conversation store)
├── unit/    test_chat_api.py test_chat_sse.py test_chat_graph.py test_chat_gatekeeper.py
│            test_segmenter.py test_rate_limit.py test_tool_invoker.py test_chat_limits.py
│            test_chat_disconnect.py test_anthropic_chat_model.py test_prompt_loader.py (+hash)
├── contract/ test_chat_contract.py (event names, payload schemas vs docs/chat-contract.md)
└── integration/ test_conversation_repository.py test_checkpoint_saver.py
                 test_migrations.py (+0003) test_live_llm.py (+assistant case)
```

**Also in this story**: `README.md`, `CLAUDE.md`, `.env.example` (CHAT_* settings); gateway route
`/api/chat` is a follow-up PR in ecommerce-infra (SSE: no buffering, long timeout).

## Complexity Tracking

| Deviation | Why | Simpler alternative rejected because |
|---|---|---|
| Own checkpointer on SQLAlchemy | one driver, schema via migrations, Windows dev loops | `langgraph-checkpoint-postgres` needs psycopg and `setup()` (research §5) |
