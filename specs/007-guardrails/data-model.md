# Data model: US-07 Guardrails

Migration `0005_guardrails.py` (hand-written). Enums are `StrEnum`, checked by `CHECK`.

## `guardrail_events` (append-only, FR-019)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | text not null | token subject |
| `conversation_id` | uuid null | no FK: survives conversation delete |
| `request_id` | varchar(64) null | `X-Request-Id` |
| `stage` | varchar(8) | `input` \| `tool` \| `output` |
| `rule` | varchar(64) | rule name (input file, `ChatRule`, tool guard) or `cooldown` |
| `action` | varchar(16) | `blocked` \| `replaced` \| `sanitized` \| `cooldown` |
| `severity` | varchar(8) | `low` \| `medium` \| `high` |
| `created_at` | timestamptz | UTC |

Index `(user_id, created_at)`. Trigger `guardrail_events_append_only` raises on UPDATE/DELETE.
No text, tool data or token columns (SC-007).

## `chat_user_usage` (FR-014/015)

| Column | Type | Notes |
|---|---|---|
| `user_id` | text | PK part |
| `day` | date | PK part, UTC |
| `messages` | int ≥ 0 | posts accepted (blocked ones included) |
| `tokens` | int ≥ 0 | reserved + spent, conditional upsert ≤ `USER_DAILY_TOKEN_BUDGET` |
| `updated_at` | timestamptz | |

## Changed

- `chat_conversations.tool_allowlist text[] not null`: backfilled with the chat tools at 0005.
- `ck_chat_messages_finish_reason`: + `blocked`, `tool_loop`; downgrade maps them to `replaced`.

## In-memory / domain

- `GuardRule` (rules file): `name`, `category`, `severity`, `patterns[]`; file `version`.
- `GuardEvent` (state → store): `stage`, `rule`, `action`, `severity`.
- `QuotaVerdict`: allowed, or `limited(scope, retry_after_seconds)`.
- `Canary`: `cnry-` + 24 hex, one per run, never stored or logged.
- `RedTeamCase`: `id`, `category`, `input` | `tool_data`, `compromise?`, `expect{must_not_call_tools,
  must_not_contain, must_refuse, expected_finish_reason}`.
