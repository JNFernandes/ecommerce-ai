"""Span attribute keys allowed to leave the process (plan D6).

Only identifiers, counts, durations, outcomes and class names: never message text, prompts, tool
arguments or results, tokens or personal data. ``ScrubbingSpanExporter`` drops every key not in
``ALLOWED_KEYS``, so a new attribute must be added here first.
"""

from typing import Final

REQUEST_ID: Final = "request.id"
HTTP_ROUTE: Final = "http.route"
HTTP_STATUS_CODE: Final = "http.response.status_code"
CONVERSATION_ID: Final = "conversation.id"
CASE_ID: Final = "case.id"
ORDER_ID: Final = "order.id"
USER_HASH: Final = "user.hash"
NODE_NAME: Final = "node.name"
TOOL_NAME: Final = "tool.name"
TOOL_OUTCOME: Final = "tool.outcome"
MODEL: Final = "gen_ai.request.model"
PROMPT_VERSION: Final = "prompt.version"
INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
LATENCY_MS: Final = "latency.ms"
COST_USD: Final = "cost.usd"
FINISH_REASON: Final = "gen_ai.response.finish_reason"
GUARD_RULES: Final = "guard.rules"
GUARD_ACTIONS: Final = "guard.actions"
TRIAGE_REVISIONS: Final = "triage.revisions"
TRIAGE_SOURCE: Final = "triage.source"
ERROR_TYPE: Final = "error.type"
LLM_OUTCOME: Final = "llm.outcome"
MESSAGING_DESTINATION: Final = "messaging.destination"

ALLOWED_KEYS: Final = frozenset(
    {
        REQUEST_ID,
        HTTP_ROUTE,
        HTTP_STATUS_CODE,
        CONVERSATION_ID,
        CASE_ID,
        ORDER_ID,
        USER_HASH,
        NODE_NAME,
        TOOL_NAME,
        TOOL_OUTCOME,
        MODEL,
        PROMPT_VERSION,
        INPUT_TOKENS,
        OUTPUT_TOKENS,
        LATENCY_MS,
        COST_USD,
        FINISH_REASON,
        GUARD_RULES,
        GUARD_ACTIONS,
        TRIAGE_REVISIONS,
        TRIAGE_SOURCE,
        ERROR_TYPE,
        LLM_OUTCOME,
        MESSAGING_DESTINATION,
    }
)
"""Every attribute key an exported span may carry."""

RESOURCE_KEYS: Final = frozenset({"service.name", "service.version"})
"""Every resource attribute key an exported span may carry."""
