# Implementation Plan: US-07 Guardrails

**Branch**: `feature/US-07-guardrails` | **Date**: 2026-11-25 | **Spec**: [spec.md](spec.md)

## Summary

Deterministic guards around the US-05/06 chat graph, independent of the model: a normaliser and
data-file input rules in the existing `guard_input` node, sanitised and length-capped tool results
in the existing envelope, a tool guard (conversation allowlist, unseen ids, loops) in the `tools`
node, canary and leak checks in the existing gatekeeper, per-user quotas and cooldown before any
model work, and an append-only `guardrail_events` audit. A red-team corpus proves it with a
compromised fake model. Decisions: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (uv)
**Primary Dependencies**: existing only (stdlib `unicodedata`/`base64`/`secrets`, Pydantic,
LangGraph, SQLAlchemy/asyncpg); no new dependency
**Storage**: PostgreSQL 16, migration 0005: `guardrail_events`, `chat_user_usage`,
`chat_conversations.tool_allowlist`, finish reasons ([data-model.md](data-model.md))
**Testing**: pytest; `CompromisedChatModel`, `chat_graph`, in-memory guard/usage stores;
`tests/redteam/` (corpus, loader, parametrised + guard-coverage tests); integration for 0005,
trigger, conditional upsert; `live_llm` corpus run
**Target Platform**: Linux container, port 8002
**Project Type**: web service (single project)
**Performance Goals**: input + output guards < 50 ms p95 per message (SC-008)
**Constraints**: single instance (minute window, stream slot in memory); audit holds no content
**Scale/Scope**: 7 guard modules, 1 rules file, 1 prompt version, 2 tables, 9 settings, ≥ 40 cases

## Constitution Check

| Principle | Status |
|---|---|
| I Layering | ✅ guard decisions in `agents/guardrails` (pure); SQL in `infrastructure/db`; 429/`Retry-After` mapping in `api/chat.py`; ports for stores and the minute window. |
| II Security | ✅ boundary unchanged (read-only tools, caller's JWT, approval); tool data stays delimited data; secrets/tokens never in audit or logs. |
| III HITL | ✅ US-06 rules unchanged; tool guard only refuses more; gatekeeper rules only added (FR-009). |
| IV Reliability/cost | ✅ per-user + global daily budgets, bounded settings; audit failure never fails open. |
| V Events | ✅ untouched. |
| VI Evals | ✅ interim gate: red-team corpus in default run, pinned `assistant.v4` and rules-file hashes, `live_llm`; fields map to verdict (US-09). |
| VII Observability | ✅ one `guardrail_event` line per action, counts on `chat_run`, `user_hash` only. |
| VIII/IX Tests | ✅ unit per guard (happy + edge), red-team, integration for 0005; ≥ 80%. |
| XI Gate | ✅ unchanged. |

Post-design re-check: no deviations.

## Project Structure

```text
specs/007-guardrails/ plan.md research.md data-model.md quickstart.md contracts/chat-guardrails.md

src/ecommerce_ai/
├── agents/
│   ├── guardrails/
│   │   ├── __init__.py      # Guardrails bundle
│   │   ├── normalizer.py    # NFKC, Cf/Cc strip, match variants, bounded decode
│   │   ├── input_rules.py   # load + validate rules file, match → GuardEvent
│   │   ├── rules/input.v1.json
│   │   ├── output_checks.py # canary, token/secret-like, foreign id, prompt shingles
│   │   ├── tool_guard.py    # allowlist, unseen_argument, duplicate/loop
│   │   ├── canary.py        # per-run token
│   │   ├── quotas.py        # UserQuotas, cooldown rule, QuotaVerdict
│   │   └── events.py        # GuardEvent, Stage/Action/Severity, ports GuardrailEventStore/UserUsageStore/RateWindow
│   ├── prompts/assistant.v4.md
│   └── assistant/ graph.py (guard_input, tools, gatekeeper wiring, TOOL_LOOP/BLOCKED)
│                  gatekeeper.py (+rules) tool_results.py (sanitise, cap) limits.py (user budget, per-user stream)
│                  model.py (finish reasons, scope) service.py (normalise, quotas, record events, logs)
├── api/chat.py              # 429 + Retry-After + scope, 422 empty; rate_limit.py moves to infrastructure
├── infrastructure/
│   ├── rate_limit.py        # SlidingWindowLimiter (RateWindow)
│   ├── settings.py          # USER_*, TOOL_RESULT_MAX_CHARS, GUARD_*; drop CHAT_RATE_LIMIT_*
│   └── db/ tables.py guardrail_event_repository.py user_usage_repository.py
│           conversation_repository.py (+allowlist) migrations/versions/0005_guardrails.py
└── main.py                  # build rules, guards, repositories

docs/threat-model.md  docs/adr/0005-layered-guardrails.md (+ index)  docs/chat-contract.md
tests/
├── redteam/ cases.jsonl legit.jsonl loader.py test_redteam.py test_guard_coverage.py test_redteam_live.py
├── support/ llm.py (+CompromisedChatModel) stores.py (+guard events, user usage)
├── unit/    test_normalizer.py test_input_rules.py test_output_checks.py test_tool_guard.py
│            test_quotas.py test_guard_latency.py test_chat_api (+429/scope) test_prompt_loader (+v4)
├── contract/ test_chat_contract.py (+scope, finish reasons)
└── integration/ test_guardrail_repository.py test_user_usage_repository.py test_migrations (+0005)
```

**Also**: `README.md`, `CLAUDE.md`, `.env.example`; `GUARD_LLM_CLASSIFIER=false` documented as
future work.

## Complexity Tracking

None.
