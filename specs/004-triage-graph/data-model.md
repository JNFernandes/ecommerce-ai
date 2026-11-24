# Data model: US-04 Triage graph

Migration `0002_triage_results.py` (hand-written; `tables.py` mirrors it). All timestamps
`timestamptz`, UTC.

## `triage_cases` (US-03, changed)

- `status` check gains `COMPLETED` (`TriageStatus.COMPLETED`). Transition PENDING_TRIAGE → COMPLETED
  only, in the save transaction. Never back.
- New index `ix_triage_cases_status_updated_at (status, updated_at)` for picking.

## `triage_results` (new)

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `case_id` | uuid FK → `triage_cases.id`, **UNIQUE** | one result per case (FR-015) |
| `event_id` | uuid UNIQUE | `eventId` of the published event |
| `cause` | varchar(32) | `TriageCause` check |
| `explanation` | text | 1–500 chars (check) |
| `suggested_action` | varchar(32) | `SuggestedAction` check |
| `confidence` | numeric(4,3) | 0–1 (check) |
| `source` | varchar(32) | `LLM` \| `FALLBACK_TEMPLATE` |
| `prompt_version` | varchar(32) | `triage.v1` |
| `model_id` | varchar(128) null | null ⇔ `source = FALLBACK_TEMPLATE` (check) |
| `input_tokens`, `output_tokens` | integer ≥ 0 | sums over attempts |
| `latency_ms` | integer ≥ 0 | whole graph run |
| `revisions` | smallint 0–2 | |
| `violations` | jsonb | `[{"rule": "PROMISE", "message": "...", "attempt": 1}]`, no evidence values |
| `created_at` | timestamptz | |

## `outbox_events` (new)

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `event_id` | uuid UNIQUE | same as `triage_results.event_id`, never changes |
| `topic` | varchar(255) | `ai.order-triage-completed` |
| `message_key` | text | order id |
| `payload` | jsonb | full event (envelope + fields), sent byte-stable as stored |
| `status` | varchar(16) | `PENDING` → `RESOLVED` \| `EXHAUSTED` (`OutboxStatus`) |
| `attempts` | integer ≥ 0 | |
| `next_attempt_at` | timestamptz | index with `status` for the relay |
| `last_error` | varchar(200) null | error class name only |
| `created_at`, `updated_at` | timestamptz | |

## `llm_token_usage` (new)

| Column | Type | Rule |
|---|---|---|
| `day` | date PK | UTC day |
| `tokens` | bigint ≥ 0 | reserved + actual (research §4) |
| `updated_at` | timestamptz | |

## Domain models (agents, Pydantic v2 frozen)

- `SuggestedAction` (StrEnum): CONTACT_CUSTOMER, RESTOCK, FIX_CATALOG, REVIEW, NONE.
- `ResultSource` (StrEnum): LLM, FALLBACK_TEMPLATE.
- `CaseForTriage`: case id, order id, cause, evidence list (topic, reason, occurred_at, payload).
- `DraftOutput` (model output, `extra="forbid"`): explanation (1–500), suggested_action,
  confidence (0–1).
- `Violation`: rule (`GateRule` StrEnum), message, attempt.
- `TriageState` (graph): case, default action, drafts, violations, revisions, tokens, source.
- `TriageResult`: everything stored in `triage_results` except ids/timestamps assigned by the store.

Event payload: see [contracts/triage-completed.md](contracts/triage-completed.md).
