# Research: US-04 Triage graph

Each decision: choice — reason (alternatives rejected).

1. **Graph engine**: `langgraph` (added now, first story needing it), `StateGraph` over a frozen
   Pydantic `TriageState`; nodes `classify → draft → gatekeeper → (revise|finalize|fallback)`.
   Compiled **without a checkpointer** — a run is short and fully re-runnable from the stored case;
   a crash rolls the case back to pending (spec edge case). Postgres checkpointer: extra tables and
   setup for no resumable value; it arrives with the assistant (interrupts, ADR 0002).
2. **LLM client**: `anthropic` SDK (async), `AsyncAnthropic(max_retries=0)` and our own bounded
   retry (`infrastructure/retry.py::call_with_retry`, `LLM_MAX_RETRIES`) so every attempt passes
   the budget check and is logged; per-call `with_options(timeout=LLM_TIMEOUT_SECONDS)`.
   Structured output via `messages.parse(output_format=DraftOutput)` (Pydantic, `extra="forbid"`)
   and re-validated by our own model; `stop_reason` `refusal`/`max_tokens` = failed attempt.
   `output_config.effort` from `LLM_EFFORT` (default `low`: short classification-like task).
   Raw `httpx` calls rejected (SDK is the stack's choice). Note: SDK 1.x uses `httpx2`; unit tests
   never reach it (fake `TriageModel` port); the adapter is tested with a fake SDK client object.
3. **Model id**: `ANTHROPIC_MODEL` has no code default; the model is "configured" only when both
   it and `ANTHROPIC_API_KEY` (`SecretStr`) are set, else every case uses the fallback (FR-009,
   FR-011). `.env.example` suggests `claude-opus-5-5`.
4. **Budget**: table `llm_token_usage(day, tokens)`. Before each attempt, in its own short
   transaction: `INSERT … ON CONFLICT (day) DO UPDATE SET tokens = tokens + :cap WHERE
   llm_token_usage.tokens + :cap <= :budget RETURNING` — no row → budget spent → fallback. After
   the attempt: `tokens = tokens - :cap + :actual`. A crash leaves the cap reserved (conservative;
   lost attempts count, SC-005). Per-case cap: attempts stop when the case's spent tokens + cap
   would pass `CASE_TOKEN_CAP`. Redis/in-memory counter rejected: not shared across restarts/workers.
5. **Picking cases**: `SELECT … WHERE status='PENDING_TRIAGE' AND updated_at <= now()-settle
   ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED`, held in one transaction for the whole run
   (graph + save). Concurrent workers skip it (FR-001, SC-006); a crash or shutdown rolls back.
   Lease column rejected: more state, same guarantee at this volume. Index `(status, updated_at)`.
6. **Result + outbox in one transaction**: in the same transaction, insert `triage_results`
   (UNIQUE `case_id`), insert `outbox_events` (PENDING), set the case COMPLETED (FR-015).
   The agent builds a domain `TriageResult`; the store's constructor gets a serializer from
   `messaging/triage_published.py` (wired in `main.py`) that turns it into the contract payload, so
   agents never import messaging and infrastructure never knows the contract.
7. **Publishing**: one code path — a `messaging/outbox_relay.py` loop picks `PENDING` rows with
   `next_attempt_at <= now()` (`FOR UPDATE SKIP LOCKED`), sends via an aiokafka producer
   (`acks=all`, idempotence on) behind a `ProducerPort`, marks RESOLVED, or bumps `attempts`,
   `next_attempt_at = now + min(base·2ⁿ, max)` with jitter, EXHAUSTED at `OUTBOX_MAX_ATTEMPTS`.
   The worker sets an `asyncio.Event` after each commit to wake the relay (SC-004 without tight
   polling). Inline publish + separate retry rejected: two paths for one outcome.
8. **Gatekeeper** (pure, `agents/triage/gatekeeper.py`): rules with codes `PROMISE`, `UNGROUNDED_FACT`,
   `CONTACT_OR_URL`, `LENGTH`, `INTERNAL_NAME`, `NOT_PLAIN`, `ACTION_NOT_ALLOWED`. Facts: regexes
   for amounts/numbers, UUIDs/ids, ISO and written dates, and a fixed status vocabulary; each found
   token must appear in the set of evidence strings (flattened payload values, reasons). Status
   words map to the evidence topics that justify them (`cancelled` ← `orders.order-cancelled`,
   `payment failed` ← `payments.payment-failed`, …). Internal names: topic names, `SCREAMING_CASE`
   identifiers, `CamelCase` class-like tokens, braces/backticks, `Traceback`. Messages are fixed
   strings per rule (never evidence values). LLM-as-judge rejected: not deterministic.
9. **Fallback**: `agents/triage/templates.py`, one fixed sentence set per cause, default action,
   fixed confidence (1.0 for CANCELLATION_REQUESTED, 0.9 PAYMENT_FAILED/INSUFFICIENT_STOCK/
   UNKNOWN_PRODUCT, 0.3 UNKNOWN); a unit test runs every template through the gatekeeper.
10. **Prompt**: `agents/prompts/triage.v1.md` loaded by `agents/prompts/loader.py::load_prompt(
    "triage", "v1")` via `importlib.resources`; system part and user template with
    `<evidence>…</evidence>` data blocks (JSON-escaped values); prompt version `triage.v1`. A test
    pins the file's SHA-256 (immutability). Check the wheel ships `.md` files (uv_build module dir).
11. **Worker loop**: `agents/triage/worker.py::TriageWorker` (asyncio only, ports only): poll every
    `TRIAGE_POLL_INTERVAL_SECONDS`, loop immediately while cases remain; transient DB errors →
    backoff `TRIAGE_BACKOFF_*` with `WorkerState.last_error` (class name); stop event + shutdown
    timeout like the consumer. Lives in agents because it holds the triage decision flow and uses
    ports only; the relay (pure I/O) lives in messaging.
12. **Intake after COMPLETED**: `TriageIntakeService.record` still stores evidence but leaves cause,
    status and `updated_at` of a COMPLETED case unchanged (spec edge case).
13. **Observability**: one `triage_case` log line per case (case id, order id, source, revisions,
    tokens, latency, outcome, violation codes) and one `outbox_publish` line per attempt. Tracing
    backend deferred to US-08 (see plan Complexity Tracking).
