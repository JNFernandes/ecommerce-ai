# Threat model: the customer assistant (US-07)

Scope: `/chat` (US-05/06) and the tools it calls. The triage worker (US-04) has no free-text user
input and is out of scope except for the shared token budget. Decisions: [ADR 0001](adr/0001-token-passthrough.md),
[0002](adr/0002-human-in-the-loop-for-mutating-tools.md), [0003](adr/0003-fallback-and-cost-caps.md),
[0005](adr/0005-layered-guardrails.md).

## Assets

| Asset | Why it matters |
|---|---|
| Customer data (orders, notifications) | Personal data; one customer must never see another's |
| Order state | `cancel_order` changes it; a wrong cancel harms the customer |
| The caller's JWT | Grants the customer's rights on every upstream |
| System prompt, canary, rules | Knowing them makes attacks easier |
| Secrets and infrastructure names | API keys, DB URLs, hosts, table names |
| Token budget | Money; exhausting it is a denial of service for everyone |
| Audit trail | Evidence of what the guards did; must not be altered or leak content |

## Trust boundaries

1. **Customer → API**: authenticated (RS256 JWT, ADR 0001) but the message text is untrusted.
2. **API → model**: the model is treated as compromisable; its output is untrusted.
3. **Tools → model**: tool results are untrusted data from upstreams (and the text other people
   wrote into them), delimited and sanitised before the model sees them.
4. **Model → tools**: every call the model asks for is checked before it runs; mutating calls
   need the customer's approval through the decision endpoint (ADR 0002).
5. **Service → database**: audit rows are append-only (trigger), with no content columns.

## Threats and mitigations (OWASP Top 10 for LLM applications, 2025)

| OWASP | Threat | Mitigation | Proof |
|---|---|---|---|
| LLM01 Prompt injection | Direct: "ignore previous instructions", fake `SYSTEM:` headers, role play, jailbreaks, obfuscated (zero-width, full-width, leet, base64/hex) and multilingual variants | Normaliser + versioned input rules over decoded variants; blocked before the model, no tokens spent; cooldown after repeated attempts | `tests/redteam` (direct, role play, jailbreak, obfuscation, multilingual, over-long), `test_input_rules.py` |
| LLM01 Prompt injection | Indirect: instructions inside notifications, product text, order notes | Tool results sanitised, delimited, capped (`TOOL_RESULT_MAX_CHARS`); injected rules flagged as data; tool guard and output checks stop what an obeyed injection would do | `tests/redteam` (indirect), `test_tool_results.py` |
| LLM02 Sensitive information disclosure | Answers leaking tokens, API keys, other customers' ids, internal hosts or tables | Output checks on every streamed segment and the full answer: token/secret patterns, foreign UUIDs vs seen ids, internal names; replaced answer | `test_output_checks.py`, `tests/redteam` (ownership, indirect) |
| LLM06 Excessive agency | The model cancels orders the customer never saw, calls unlisted tools, loops | Stored per-conversation allowlist, seen-id rule for `cancel_order`, duplicate-call loop stop, human approval of the exact call, no chat-text approvals | `test_chat_tool_guard.py`, `tests/redteam` (approval bypass, tool arguments) |
| LLM07 System prompt leakage | Extracting the prompt or its secrets | Extraction rules on input; per-run canary and 12-word prompt shingles on output; no secrets in the prompt at all | `test_canary.py`, `tests/redteam` (extraction) |
| LLM08 Vector and embedding weaknesses | Not applicable: no retrieval store yet | Revisit when RAG is added | — |
| LLM10 Unbounded consumption | Flooding messages, long inputs, token exhaustion, parallel streams | 16 KB body limit, 2000-char message, per-message token cap, per-user day budget before the global one, minute/day message limits, one stream per user, cooldown, tool-round cap | `test_quotas.py`, `test_chat_limits.py` |

Ownership itself is enforced upstream: tools forward the caller's token (ADR 0001), so a probe
for another customer's order gets the upstream's 404/403 even if every guard were bypassed.

## Residual risks

- **Paraphrased attacks** that avoid the rule vocabulary reach the model and spend tokens (the DAN persona and
  customer-id probes were such gaps until fixed in v1); the output checks and tool guard still hold, but the model may answer
  off-topic. Mitigation path: grow the corpus and rules; optional LLM classifier
  (`GUARD_LLM_CLASSIFIER`, not implemented).
- **Semantic leaks** that match no pattern (e.g. a paraphrased prompt shorter than a shingle).
- **False positives** on legitimate messages (tracked by `legit.jsonl`, ≥ 95% pass).
- **In-process limits**: the minute window and stream slots reset on restart and do not span
  instances; day counters and the cooldown are persisted.
- **Upstream content**: a malicious seller or admin can write text that tool results carry; it is
  treated as data, but its presence is only flagged, not removed.
