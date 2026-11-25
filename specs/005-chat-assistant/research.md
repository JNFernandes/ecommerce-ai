# Research: US-05 Chat assistant with streaming

Each decision: choice — reason (alternatives rejected).

1. **SSE framing**: own encoder `api/sse.py` (`id:`/`event:`/`data:` lines, `: keep-alive`
   comment) over Starlette `StreamingResponse` (`text/event-stream`, `Cache-Control: no-cache`,
   `X-Accel-Buffering: no`). Keep-alive = `asyncio.wait_for` on the next agent event with
   `CHAT_KEEPALIVE_SECONDS`. — FastAPI's `fastapi.sse` has a fixed 15 s ping (module constant,
   FR-008 needs it configurable); `sse-starlette` is only transitive (fastmcp) and adds nothing we
   need.
2. **Disconnect**: Starlette cancels the body iterator when the client disconnects; the stream
   generator catches `CancelledError`, and a **shielded** `finish_disconnected` stores the text
   already sent (`client_disconnected`), settles tokens, releases the lock, then re-raises.
   Polling `request.is_disconnected()` rejected: slower and racy.
3. **Graph**: LangGraph `StateGraph` `guard_input → agent ⇄ tools → gatekeeper → finalize`, edges
   by state (`pending_tool_calls`, `rounds`, `outcome`); 5-round cap checked in the agent edge →
   `tool_limit` safe message. Events reach the API via `graph.astream(stream_mode="custom")` and
   `get_stream_writer()` inside nodes. The run returns typed `StreamEvent` objects; the router
   only frames them (constitution I).
4. **State + checkpointer**: `ChatState` TypedDict whose `transcript` is a list of JSON dicts
   dumped from frozen Pydantic items (`UserTurn`, `AssistantText`, `ToolCallItem`,
   `ToolResultItem`), re-validated on load — plain JSON keeps `JsonPlusSerializer` off the
   custom-type allowlist path. Thread id = conversation id (FR-014).
5. **Checkpointer backend**: own `infrastructure/db/checkpoint_saver.py::PostgresCheckpointSaver`
   (`BaseCheckpointSaver`: `aget_tuple`, `alist`, `aput`, `aput_writes`, `adelete_thread`) on the
   existing SQLAlchemy async engine (asyncpg), tables `chat_checkpoints` and
   `chat_checkpoint_writes` from migration 0003; keeps only the newest checkpoint per thread (older
   rows deleted in the same transaction). Unit tests use LangGraph's `InMemorySaver` (shared
   instance across two `create_app` calls simulates the restart). — `langgraph-checkpoint-postgres`
   rejected: needs psycopg (second driver/pool, no ProactorEventLoop on Windows dev machines) and
   creates its schema with `setup()` (constitution: schema only via migrations).
6. **Model port**: `agents/assistant/ports.py::ChatModel.stream(request) -> AsyncIterator[
   ChatModelEvent]` (`TextDelta`, `ToolUseRequest {id, name, input}`, `ModelTurnEnd {stop_reason,
   usage, model_id}`). Adapter `infrastructure/llm/anthropic_chat_model.py` uses
   `client.messages.stream(...)` with `tools` from the registry's input schemas, `max_retries=0`,
   per-call timeout, `LLM_MAX_OUTPUT_TOKENS`, effort. Retries (`call_with_retry`,
   `LLM_MAX_RETRIES`) only before the first delta; a failure after output started →
   `LLM_UNAVAILABLE`. Tests: `tests/support/llm.py::ScriptedChatModel` (scripted turns, asserts
   received transcript).
7. **Tools in-process**: `tools/invoker.py::ToolInvoker` over the same `ToolRegistry` built by
   `main.py::build_tool_definitions`: unknown name or input `ValidationError` → refused result
   (never executed); per-tool `asyncio.timeout`; `ToolFailure` → `{error, message}`. Runs inside
   `bind_caller(caller)` so handlers forward the caller's token (ADR 0001). An upstream 401 sets
   `upstream_unauthenticated` → run ends `TOOL_FAILED`. Shared mapping extracted from
   `tools/server.py` so `/mcp` and chat map failures once. Agents reach it through a
   `ToolRunner` port (no tools import in nodes).
8. **Prompt + delimiting**: `agents/prompts/assistant.v1.md` (system text), loaded by
   `load_prompt("assistant", "v1")`, SHA-256 pinned by test; `promptVersion` = `assistant.v1`.
   Tool results go to the model as `tool_result` blocks whose text is
   `<tool_result name="…">JSON</tool_result>` (JSON-escaped envelope, `trust: untrusted_data`);
   tool list sent to the model = registry only, so injected text cannot add a tool (FR-011/013).
9. **Gatekeeper**: shared pure rules extracted from `agents/triage/gatekeeper.py` into
   `agents/safety/rules.py` (`Evidence.from_strings`, promise, URL/contact, ungrounded
   number/amount/UUID/date, internal name, HTML); triage keeps its behaviour (its tests unchanged).
   `agents/assistant/gatekeeper.py::check_segment(text, evidence)` / `check_answer`: no sentence
   or line-count limit, newlines and `-` lists allowed, only HTML tags count as not plain;
   upper-case words are allowed when found in the evidence (order statuses); internal names add
   `ecommerce-*`, topic names, `graphql`, `localhost`. Evidence = leaf values of every
   `ToolResultItem` in the state + ids in the customer's own messages (FR-015).
10. **Segment release** (FR-016): `agents/assistant/segmenter.py` buffers deltas and yields
    segments at a sentence end (`. ! ?` + space) or newline, the rest at turn end; each segment is
    checked before its `token` event. A violation stops the model stream (no more deltas read),
    sends the safe message, ends `replaced`. The full answer is checked again in `gatekeeper`.
11. **Budget**: reuse `TokenBudget` (moved to shared `agents/ports.py`, re-exported by triage):
    reserve `CHAT_REQUEST_TOKEN_CAP` once when the agent first needs the model; before each model
    call stop with `BUDGET_EXCEEDED` when spent + `LLM_MAX_OUTPUT_TOKENS` > cap; settle actual
    usage on every ending (`finally`, shielded on cancel). Overshoot ≤ one call's input (logged).
12. **Stream lock + delete**: in-process `ActiveStreams` registry (`conversation_id → task`),
    acquired before the stream opens (409 `STREAM_ACTIVE`), released in `finally`; delete cancels
    the active task and awaits it before deleting. Single instance, like the rate limiter; shared
    locks/limits arrive with US-07.
13. **Rate limit**: `api/rate_limit.py::SlidingWindowLimiter` (subject → deque of monotonic
    times, injected clock), `CHAT_RATE_LIMIT_MESSAGES`/`_WINDOW_SECONDS`; 429 with `Retry-After` =
    ceil(seconds until the oldest hit leaves). Checked after auth, before DB work.
14. **Auth for chat**: FastAPI dependency `api/auth.py::require_caller` reusing `TokenVerifier`
    and the exact 401/403/503 responses of `api/mcp.py` (moved to a shared helper). New policy
    `AUTHENTICATED = {"x-authenticated": True}` in `route_policy.py` (any role); `_is_declared`
    accepts it.
15. **Ownership**: every repository query filters by `user_id = subject`; unknown, malformed or
    foreign ids → identical 404 `{code: NOT_FOUND}` (FR-003). Customer id from the token claim.
16. **Order of checks on POST**: auth → rate limit → body validation (422) → conversation
    lookup (404) → full (409, `count + 2 > 100`) → lock (409) → store user message (DB down →
    503) → open stream. Nothing is stored before the 4xx checks pass (FR-017).
17. **Observability**: one `chat_run` log line (request_id, conversation_id, latency_ms,
    input/output tokens, tools `[{name, outcome, duration_ms}]`, finish reason or error code,
    promptVersion, modelId, `user_hash` = SHA-256 prefix of the subject). `redact_sensitive_data`
    stays last. A test asserts no content, argument values or results appear (FR-026).
18. **Smoke client**: `scripts/chat_smoke.py` (httpx streaming, stdlib SSE line parsing), like
    `scripts/mcp_smoke.py`; `TOKEN`, `CHAT_URL` default `http://localhost:8002/chat`.
