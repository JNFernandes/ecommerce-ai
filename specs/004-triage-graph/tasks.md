# Tasks: US-04 Triage graph

**Input**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/triage-completed.md](contracts/triage-completed.md),
[quickstart.md](quickstart.md)

**Tests**: required (constitution VIII/IX). Unit: fake `TriageModel` (`ScriptedModel`), in-memory
stores, `FakeProducer`, fake clock/sleep, no network. Integration (`integration` marker): real
PostgreSQL. One `live_llm` test, excluded by default. Write each story's tests first and see them
fail. After every task: quiet gate (CLAUDE.md); commit after green.

## Phase 1: Setup

- [X] T001 Amend `.specify/memory/constitution.md` to 2.0.0 (IV: one result per order, outbox, `modelId` null on fallback; VI: evals deferred to US-09 with interim gate; VII: per-run log line until US-08) and commit it first: `docs(US-04): amend constitution IV, VI, VII for the triage graph`
- [X] T002 `uv add langgraph anthropic` (commit `uv.lock`); in `pyproject.toml` add mypy overrides only if a package is untyped (justification comment), register pytest marker `live_llm` and exclude it by default (`-m "not live_llm"` in `addopts`) if not already; confirm the wheel ships `agents/prompts/*.md` (uv_build module dir)
- [X] T003 Add settings to `src/ecommerce_ai/infrastructure/settings.py`: `triage_worker_enabled`, `triage_settle_seconds` (3), `triage_poll_interval_seconds`, `triage_backoff_base/max_seconds`, `anthropic_api_key: SecretStr | None`, `anthropic_model: str | None` (no default), `llm_timeout_seconds`, `llm_max_retries`, `llm_max_output_tokens`, `llm_effort` (`low`), `case_token_cap`, `daily_token_budget`, `outbox_max_attempts`, `outbox_backoff_base/max_seconds`, `outbox_poll_interval_seconds`; property `model_configured` (key and model both set); positive bounds, base ≤ max, `llm_max_output_tokens` ≤ `case_token_cap`; add env names to the cleared list in `tests/conftest.py`; tests in `tests/unit/test_settings.py`
- [X] T004 [P] Document the new settings in `.env.example` (`ANTHROPIC_MODEL=claude-opus-5-5` suggested, key empty)
- [X] T005 [P] Open an ecommerce-infra PR updating `docs/events.md` for `ai.order-triage-completed`: one result per order (case), outbox redelivery instead of "dead-lettered", `modelId` null only for `FALLBACK_TEMPLATE`. Link it in this PR's description; this PR merges only after it (constitution V) — opened: https://github.com/JNFernandes/ecommerce-infra/pull/23

## Phase 2: Foundational (blocks all stories)

- [X] T006 [P] Create `src/ecommerce_ai/agents/triage/__init__.py`, `model.py` (`SuggestedAction`, `ResultSource`, `GateRule` StrEnums; frozen `EvidenceItem`, `CaseForTriage`, `DraftOutput` (`extra="forbid"`, explanation 1–500, confidence 0–1), `Violation(rule, message, attempt)`, `TriageResult` per [data-model.md](data-model.md)) and `policy.py` (`default_action(cause)`, `allowed_actions(cause)` = default + REVIEW, FR-006); tests in `tests/unit/test_policy.py` and `tests/unit/test_triage_models.py` (bounds, extra field rejected)
- [X] T007 [P] Create `src/ecommerce_ai/agents/triage/ports.py`: `TriageModel.draft(prompt_input) -> ModelReply(draft, input_tokens, output_tokens, model_id)` (raises `ModelUnavailable`/`ModelOutputInvalid`); `TokenBudget.reserve(day, cap) -> bool`, `settle(day, reserved, actual)`; `TriageCaseQueue` unit of work (`async with queue.next_settled(settle_before) as claim` → `claim.case: CaseForTriage | None`, `claim.complete(result)`); `Clock`
- [X] T008 Add `TriageStatus.COMPLETED` in `src/ecommerce_ai/agents/triage_intake/model.py`; `infrastructure/db/tables.py` + hand-written `migrations/versions/0002_triage_results.py` (status check + COMPLETED, index `(status, updated_at)`, tables `triage_results`, `outbox_events`, `llm_token_usage` with every check/unique of [data-model.md](data-model.md)); extend `tests/integration/test_migrations.py` (constraints exist, `model_id` null ⇔ fallback check, upgrade idempotent) and the per-test TRUNCATE list in `tests/integration/conftest.py`
- [X] T009 [P] Create `src/ecommerce_ai/agents/prompts/__init__.py`, `loader.py::load_prompt(name, version) -> Prompt(version="triage.v1", system, user_template)` via `importlib.resources`, and `triage.v1.md` per the prompt contract in [contracts/triage-completed.md](contracts/triage-completed.md) (rules mirroring the gatekeeper, `<evidence>` data blocks, optional `<feedback>`); `render_user(case, default_action, allowed, feedback)` JSON-escapes evidence values; tests in `tests/unit/test_prompt_loader.py` (sections parsed, SHA-256 pinned, evidence with `</evidence>` cannot close the block)
- [X] T010 [P] Add `OrderTriageCompleted` (envelope + fields, frozen, camelCase, field docstrings, `modelId` null iff FALLBACK_TEMPLATE) to `src/ecommerce_ai/messaging/contracts/events.py` and `messaging/triage_published.py::to_outbox_message(result, case) -> OutboxMessage(event_id, topic, key=order id, payload)`; tests in `tests/contract/test_event_models.py` (fields pinned to `events.md`) and `tests/unit/test_triage_published.py`
- [X] T011 [P] Test support: `tests/support/llm.py` (`ScriptedModel`: queue of replies/exceptions, records prompts), `tests/support/kafka.py` (+ `FakeProducer`: sent records, failures on demand), `tests/support/stores.py` (+ in-memory `TriageCaseQueue`, `TokenBudget`, outbox store with injectable failures); self-tests in `tests/unit/test_triage_fakes.py`
- [X] T012 [P] Tests in `tests/unit/test_gatekeeper.py`: one pass and one fail case per rule (`PROMISE`, `UNGROUNDED_FACT` amounts/ids/dates/status words vs evidence, `CONTACT_OR_URL`, `LENGTH`, `INTERNAL_NAME`, `NOT_PLAIN`, `ACTION_NOT_ALLOWED`); messages are fixed strings with no evidence values
- [X] T013 Implement `src/ecommerce_ai/agents/triage/gatekeeper.py::check(draft, case) -> list[Violation]` (pure, research 8)
- [X] T014 Implement `src/ecommerce_ai/agents/triage/templates.py` (one template per cause, default action, fixed confidence; research 9); tests in `tests/unit/test_templates.py` (every template passes the gatekeeper for a representative case)

**Checkpoint**: gate green; migration 0002 applies; gatekeeper and templates ready; nothing new connects at startup.

## Phase 3: US1 — Explain a failed order (P1) 🎯 MVP

**Independent test**: fake model, one settled case per cause → one result each, case COMPLETED, one event per case keyed by order id matching the contract.

- [X] T015 [P] [US1] Tests in `tests/unit/test_triage_graph.py`: classify keeps cause and sets default action; compliant draft → finalize with source LLM, prompt version, model id, summed tokens, latency (fake clock), revisions 0; draft rejected by the real gatekeeper → fallback template with violations recorded, revisions 0 (revisions arrive in US2)
- [X] T016 [US1] Implement `src/ecommerce_ai/agents/triage/graph.py`: frozen `TriageState`, nodes `classify`, `draft`, `gatekeeper`, `finalize`, `fallback`, conditional edge after gatekeeper (pass → finalize, fail → fallback), compiled `StateGraph` without checkpointer (research 1); `build_triage_graph(model, budget, gatekeeper, prompt, settings, clock)`
- [X] T017 [US1] Implement `src/ecommerce_ai/agents/triage/service.py::TriageService.triage_one() -> bool` (claim → graph → `claim.complete(result)`; False when nothing settled) with one `triage_case` log line (FR-020 fields; constitution VII interim line: latency, tokens, `promptVersion`, `modelId`, `orderId`); tests in `tests/unit/test_triage_service.py` (all five causes, unsettled case untouched, case without evidence → UNKNOWN template)
- [X] T018 [US1] Implement `src/ecommerce_ai/infrastructure/db/triage_queue_repository.py` (pick `FOR UPDATE SKIP LOCKED` with settle cutoff, load evidence, `complete` inserts `triage_results` + `outbox_events` (PENDING, serializer injected) and sets COMPLETED in the same transaction; research 5–6); tests in `tests/integration/test_triage_queue.py` (settle cutoff, one result per case via UNIQUE, rollback leaves case pending)
- [X] T019 [US1] Leave COMPLETED cases unchanged in `src/ecommerce_ai/agents/triage_intake/service.py` (evidence stored; cause, status, `updated_at` untouched); tests in `tests/unit/test_triage_intake.py` and `tests/integration/test_triage_flow.py`
- [X] T020 [US1] Add `ProducerPort` + `AIOKafkaProducerAdapter` (`acks="all"`, idempotence, key/value bytes) to `src/ecommerce_ai/messaging/kafka.py`; `infrastructure/db/outbox_repository.py` (pick PENDING due rows `FOR UPDATE SKIP LOCKED`, mark RESOLVED); `messaging/outbox_relay.py::OutboxRelay.run(stop_event, wake)` happy path (publish stored payload bytes as stored, key, topic); tests in `tests/unit/test_kafka_adapter.py`, `tests/unit/test_outbox_relay.py`, `tests/integration/test_outbox.py`
- [X] T021 [US1] Implement `src/ecommerce_ai/agents/triage/worker.py::TriageWorker.run(stop_event)`: loop `triage_one` while cases remain, else wait `triage_poll_interval_seconds` (injected wait), set the relay `wake` event after each commit; tests in `tests/unit/test_triage_worker.py` (pending-at-start cases processed, idle wait)
- [X] T022 [US1] Wire in `src/ecommerce_ai/main.py`: prompt, model (or none), budget, queue repo with serializer, `TriageService`, worker and relay tasks when `triage_worker_enabled`, both stopped before engine dispose; update `tests/unit/test_main.py` (disabled → no tasks; enabled with fake factories → started and stopped)
- [X] T023 [US1] `tests/integration/test_triage_end_to_end.py`: intake event → settle (fake clock) → worker with `ScriptedModel` → result row, COMPLETED, outbox RESOLVED, `FakeProducer` got one contract-valid event keyed by order id (acceptance US1 1–4)

**Checkpoint**: MVP — every draft is gated; rejected drafts use the template; failed orders get an explanation and an event.

## Phase 4: US2 — Guardrails block unsafe explanations (P1)

**Independent test**: scripted drafts (refund, invented amount, URL) → revisions, recorded violations, fallback result.

- [X] T024 [US2] Revision loop in `src/ecommerce_ai/agents/triage/graph.py`: fail → draft again with violation messages as `<feedback>`, at most 2 revisions, then fallback; invalid model output counts as a failed attempt; violations accumulated with attempt number; tests in `tests/unit/test_triage_graph.py` (acceptance US2 1–3, including the injection case: no stored or published explanation mentions a refund)

## Phase 5: US3 — Works without the model (P1)

**Independent test**: no key, timing-out model, exhausted budget → one template result and event each.

- [X] T025 [P] [US3] Tests in `tests/unit/test_triage_graph.py`: no model → fallback with no call, `model_id` null; `ModelUnavailable` on every attempt → fallback after `llm_max_retries`; budget `reserve` False → fallback, no call; per-case cap reached → fallback; budget `settle` called with actual tokens after each attempt (also after a failed one)
- [X] T026 [US3] Fallback paths and budget/per-case checks in `graph.py` draft node, retries via `infrastructure/retry.py::call_with_retry` (each attempt reserves first; research 2, 4)
- [X] T027 [US3] Implement `src/ecommerce_ai/infrastructure/llm/__init__.py` and `anthropic_model.py::AnthropicTriageModel` (`AsyncAnthropic(max_retries=0)`, `with_options(timeout=…)`, `messages.parse(output_format=DraftOutput)`, `max_tokens`, effort from settings; `refusal`/`max_tokens` stop reason and parse errors → `ModelOutputInvalid`; SDK timeout/API errors → `ModelUnavailable`; usage returned); tests in `tests/unit/test_anthropic_model.py` with a fake SDK client object (no network)
- [X] T028 [US3] Implement `src/ecommerce_ai/infrastructure/db/token_budget_repository.py` (conditional `ON CONFLICT … WHERE tokens + cap <= budget RETURNING`, settle `tokens - reserved + actual`, UTC day); tests in `tests/integration/test_token_budget.py` (reserve until budget, concurrent reserves never exceed it, next UTC day resets, SC-005)
- [X] T029 [P] [US3] `tests/integration/test_live_llm.py` (`live_llm`, skipped without key/model): one case per cause through the real model passes the gatekeeper or falls back

## Phase 6: US4 — Reliable, exactly-once publishing (P2)

**Independent test**: producer down → result stored, event PENDING; producer back → published with the same `eventId`; concurrent workers → one result.

- [X] T030 [P] [US4] Tests in `tests/unit/test_outbox_relay.py` (fake clock/sleep): send failure → attempts+1, `next_attempt_at = now + min(base·2ⁿ, max)` with jitter, `last_error` class name, `RelayState.last_error`; success later with identical event id/key/payload → RESOLVED; `outbox_max_attempts` reached → EXHAUSTED, no more sends; wake event shortens the wait; one `outbox_publish` log line per attempt
- [X] T031 [US4] Complete `messaging/outbox_relay.py` (retry scheduling, EXHAUSTED, `RelayState`, transient DB error backoff) and `outbox_repository.py` (`mark_failed`, `mark_exhausted`)
- [X] T032 [US4] Integration tests: `tests/integration/test_outbox.py` (two relays concurrently → each row sent once, SKIP LOCKED), `tests/integration/test_triage_queue.py` (two workers concurrently → each case triaged once, one result/event id, SC-006), `tests/integration/test_triage_end_to_end.py` (producer failing → case COMPLETED, outbox PENDING; then RESOLVED with the same `eventId`; acceptance US4 1–4)

## Phase 7: US5 — Starts and reports without its dependencies (P2)

**Independent test**: every dependency down → `/health` 200, `/ready` 503 only for the database, worker/relay state reported.

- [X] T033 [P] [US5] Tests in `tests/unit/test_triage_worker.py`: transient DB error → bounded growing backoff, `WorkerState.last_error` class name, recovery clears it; stop event interrupts backoff; in-flight case abandoned on stop; shutdown within `consumer_shutdown_timeout_seconds`
- [X] T034 [US5] `WorkerState` and backoff in `src/ecommerce_ai/agents/triage/worker.py`; bounded cancellation of worker and relay in `main.py` before engine dispose (FR-004)
- [X] T035 [P] [US5] Tests in `tests/unit/test_health_api.py`: `triageWorker` (`running`, `lastError`, `modelConfigured`) and `outboxRelay` (`running`, `lastError`) present when enabled, `null` when disabled, never change the status; app with no DB/Kafka/model answers `/health` within 1 s (SC-007)
- [X] T036 [US5] Extend `ReadinessResponse` in `src/ecommerce_ai/api/health.py` (OpenAPI descriptions, camelCase) reading `WorkerState`/`RelayState`; pass state from `main.py`

## Phase 8: Polish

- [ ] T037 [P] FR-020 test in `tests/unit/test_triage_service.py` / `test_outbox_relay.py`: captured `triage_case`, `outbox_publish` and model log lines contain no evidence payload, reason, customer id, explanation or prompt text
- [ ] T038 [P] Confirm the constitution 2.0.0 text (IV, VI, VII) matches the implementation; fix any drift in the code or a PATCH amendment
- [ ] T039 [P] Update `README.md`, `CLAUDE.md` (agents/triage, prompts, llm, outbox relay, new tables, settings, `/ready` entries, `live_llm`, constitution 2.0.0) and verify the `Dockerfile` image contains `triage.v1.md`
- [ ] T040 Run the full quiet gate (coverage ≥ 80%) and the [quickstart.md](quickstart.md) automated steps; manual steps if the infra stack is up

## Dependencies

Setup (T001 first) → Foundational (T008 after T003; T006, T007, T009, T010, T011, T012 parallel;
T013 after T012; T014 after T013) → US1 (T015 → T016 → T017; T018 after T008/T010; T020 after
T010/T011; T021 after T017; T022 → T023) → US2 (T024 after T016) → US3 (T027, T028 parallel after
T026) → US4 (extends T020) → US5 (extends T021/T022) → Polish. T005 must be merged in
ecommerce-infra before this PR merges.

## Parallel examples

- Setup: T004 ‖ T005.
- Foundational: T006, T007, T009, T010, T011, T012 together.
- US1: T015 ‖ T018 ‖ T020 after Foundational.
- US3: T027 ‖ T028 ‖ T029. US5: T033 ‖ T035.
- Polish: T037–T039 together.

## Strategy

MVP = Phases 1–3: every draft is gated, and rejected drafts use the template; US2 adds revisions.
US3 (fallback, budget, real model) completes the P1 promise; then US4 (outbox retries,
concurrency), US5 (resilience, readiness), Polish. Commit per task after a green gate.
