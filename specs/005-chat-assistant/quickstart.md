# Quickstart: US-05 Chat assistant

1. Gate: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov`
   (unit: `ScriptedChatModel`, fake tools, `InMemorySaver`, in-memory conversation store;
   integration on real PostgreSQL: migration 0003, `ConversationRepository`,
   `PostgresCheckpointSaver`, delete removes state).
2. No model: `uv run python -m ecommerce_ai migrate`, `uv run python -m ecommerce_ai`; `/health`
   200. Post a message → stream `message.start` then `error LLM_UNAVAILABLE`; history has the user
   message only.
3. With a model (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` in `.env`) and the platform upstream URLs:
   `TOKEN=<customer jwt> uv run python scripts/chat_smoke.py "Where is my last order?"` → tool
   events, streamed tokens, `message.end completed`. Ask a follow-up, restart the service, ask
   again: the earlier turn is remembered.
4. Limits: post twice concurrently → second 409 `STREAM_ACTIVE`; 11 posts in 60 s → 429
   `Retry-After`; `DAILY_TOKEN_BUDGET=1` → `error BUDGET_EXCEEDED`; another user's id → 404.
5. Disconnect: Ctrl-C the smoke client mid-answer → history shows the partial answer with
   `client_disconnected`; a new post is accepted at once.
6. Live test: `uv run pytest -m live_llm`.
