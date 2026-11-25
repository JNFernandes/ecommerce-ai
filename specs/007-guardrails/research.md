# Research: US-07 Guardrails

No NEEDS CLARIFICATION remained after the spec. Each decision below gives its reason in one line;
rejected alternatives only where they were real options.

## §1 Package and injection

- **Decision**: new `agents/guardrails/` (pure, no I/O): `normalizer.py`, `input_rules.py` +
  `rules/input.v1.json`, `output_checks.py`, `tool_guard.py`, `canary.py`, `quotas.py`,
  `events.py`. `ChatGraph` takes one `Guardrails` bundle (frozen dataclass of the guards).
- **Why**: agents own every guard decision (constitution I); one bundle lets the red-team test
  swap any single guard for a no-op (SC-002) without code branches.

## §2 Normaliser (FR-001)

- **Decision**: NFKC → drop `Cf` (zero-width, bidi, BOM) → drop `Cc` except `\n`/`\t`. Matching
  uses extra variants: casefolded, de-leeted (`0→o 1→i 3→e 4→a 5→s 7→t @→a $→s`),
  separator-squashed (`i.g.n.o.r.e` → `ignore`), and bounded base64/hex decodes (runs ≥ 16
  chars, strict UTF-8, total ≤ 2000 chars; undecodable runs stay plain text).
- **Why**: stdlib only (`unicodedata`, `base64`, `binascii`); the 2000-char limit is checked on
  the raw text, so normalising never admits a longer message.
- **Rejected**: a confusables library (heavy dependency, NFKC covers full-width forms).

## §3 Input rules file (FR-002/003)

- **Decision**: JSON (`{"version": "v1", "rules": [{name, category, severity, patterns}]}`),
  loaded with `importlib.resources`, validated by a frozen Pydantic model, regexes compiled once
  in `build_api`; invalid file → build fails. Version in `chat_run`; file SHA-256 pinned by a test.
- **Why**: JSON needs no new dependency (no PyYAML); a rule is data, not a code branch. Approval
  phrases are `low` (do not count towards cooldown, spec assumption).

## §4 Where the input guard runs

- **Decision**: `ChatService` normalises first (empty → 422 `VALIDATION_FAILED`) and stores the
  normalised text; the existing `guard_input` node checks it and routes to `finalize` with the
  fixed refusal, finish `blocked`, never `agent`. Later turns skip a user message whose answer
  ended `blocked` when building the model transcript.
- **Why**: reuses the US-05 node; zero model calls and zero reservation (SC-003); no extra column.

## §5 Tool-result sanitisation (FR-005/006)

- **Decision**: `tool_result_text` normalises every string leaf, serialises with `inert_json`
  (already escapes `<`, `>`, `&`, so delimiters cannot appear), then cuts at
  `TOOL_RESULT_MAX_CHARS` with `…[truncated N chars]`. Input rules also run over the leaves;
  matches are tool-stage `sanitized` events, not blocks.
- **Why**: injected text stays inert data; the cut is visible to the model (prompt v4 explains it).

## §6 Canary and prompt v4 (FR-007)

- **Decision**: `assistant.v4.md` with a `{canary}` slot (hash pinned on the template); per run
  `cnry-` + `secrets.token_hex(12)`. `check_segment` replaces any segment holding it (rule
  `canary`, `high`); an already streamed segment uses `message.replaced` (US-06 mechanism).
- **Why**: per-run randomness means a leaked canary proves a prompt leak; v1–v3 stay immutable.

## §7 Output checks (FR-008/009)

- **Decision**: new `ChatRule` members `canary`, `token_like` (JWT `eyJ…\.…\.…`), `secret_like`
  (`sk-ant-`, `sk-`, `AKIA`, `ghp_`, `key=`/`secret:` assignments), `foreign_id` (UUID not in this
  conversation's user turns or tool results), `prompt_leak` (any 12-word shingle of the rendered
  system prompt). `_ALWAYS_INTERNAL` gains table names (from `tables.py` metadata, test-checked),
  hosts/ports (`*:5436`, `ai:8002`, `*.internal`). Existing rules untouched.
- **Why**: deterministic and cheap; shingles are precomputed per prompt version.

## §8 Tool guard (FR-010/011/012)

- **Decision**: `chat_conversations.tool_allowlist text[]` set on create from
  `registry.exposed(Exposure.CHAT)`. `ToolGuard.check(call, seen_ids, calls_so_far)` in the
  `tools` node before `_propose`/`run`: `not_allowed`, `unseen_argument` (`cancel_order` id not
  in seen ids), `duplicate_call` (same name + canonical JSON args in this message; refusal result,
  not run); a second duplicate ends with `SAFE_MESSAGE`, finish `tool_loop`.
- **Why**: limits agency on top of ADR 0002, which stays unchanged.

## §9 Per-user quotas (FR-014..017)

- **Decision**: `RunAllowance` reserves the cap from the user budget, then the global one (if the
  global fails, the user reservation is settled to 0); the error carries `scope`. Day messages and
  tokens live in one `chat_user_usage (user_id, day)` row, incremented on accept, so deleting a
  conversation cannot reset the day count. The minute window stays the in-process
  `SlidingWindowLimiter`, moved to `infrastructure/rate_limit.py` behind a `RateWindow` port.
  `ActiveStreams` gains a per-user slot. Cooldown: count of `input`/`blocked`/≥ `medium` events
  in the window; at the threshold, append a `cooldown` event; active while the newest `cooldown`
  is younger than `GUARD_COOLDOWN_SECONDS`.
- **Why**: persisted counters survive restarts (spec); single instance keeps the minute window
  in memory (spec assumption). Counter instead of counting stored messages: deletion-proof.
- **Check order** (post): cooldown → minute → day → conversation full → stream active. Decision
  endpoint: cooldown → minute.

## §10 Audit and logs (FR-019/021)

- **Decision**: graph nodes add `GuardEvent`s (stage, rule, action, severity) to state; the
  service appends them through `GuardrailEventStore` after the run (also in the disconnect path)
  and logs one `guardrail_event` line each; `chat_run` gains `guards` counts and `rulesVersion`.
  An insert failure logs `error_fields` and never undoes the action. Trigger enforces append-only
  (same pattern as `chat_actions`); no FK to the conversation so the audit survives deletes.
- **Why**: graph stays free of DB side effects (US-06 §3); content never reaches the table.

## §11 Red-team corpus (FR-022..024)

- **Decision**: `tests/redteam/cases.jsonl` (≥ 40, 11 categories × ≥ 2), `tests/redteam/loader.py`
  (frozen Pydantic `RedTeamCase`, field names = verdict case), `test_redteam.py` parametrised by
  case id through `chat_graph` with `CompromisedChatModel` (`tests/support/llm.py`): it obeys
  `compromise` (calls the tool, repeats injected text, extracts the canary from the system prompt
  it receives). `test_guard_coverage.py` disables each guard in turn and expects ≥ 1 failure.
  `legit.jsonl` (≥ 20, five languages) checks false positives (SC-005). Live run: `live_llm`.
- **Why**: proves the guards, not the model, keep the expectations (SC-001/002).

## §12 Latency (SC-008)

- **Decision**: unit test times input + output checks over 2000-char worst cases (100 runs,
  p95 < 50 ms); all regexes precompiled, no backtracking-prone nested quantifiers.
