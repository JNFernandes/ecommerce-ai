"""Span names of the service (plan D7) and the attributes of an ``llm.call`` span.

Names are fixed strings, never built from data; ``node.<name>`` spans come from ``traced_node``.
"""

from typing import Final

from ecommerce_ai.telemetry.attributes import (
    FINISH_REASON,
    INPUT_TOKENS,
    LATENCY_MS,
    LLM_OUTCOME,
    MODEL,
    OUTPUT_TOKENS,
)
from ecommerce_ai.telemetry.tracing import Attributes

HTTP_REQUEST: Final = "http.request"
CHAT_RUN: Final = "chat.run"
CHAT_RESUME: Final = "chat.resume"
LLM_CALL: Final = "llm.call"
TOOL_CALL: Final = "tool.call"
UPSTREAM_HTTP: Final = "upstream.http"
APPROVAL_PAUSE: Final = "approval.pause"
APPROVAL_RESUME: Final = "approval.resume"
TRIAGE_RUN: Final = "triage.run"
TRIAGE_CONSUME: Final = "triage.consume"
OUTBOX_PUBLISH: Final = "outbox.publish"


def llm_call_attributes(
    *,
    outcome: str,
    latency_ms: int,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reason: str | None = None,
) -> Attributes:
    """The closing attributes of an ``llm.call`` span; ``None`` values are left out."""
    return {
        LLM_OUTCOME: str(outcome),  # a StrEnum member exports as its plain value
        LATENCY_MS: latency_ms,
        MODEL: model,
        INPUT_TOKENS: input_tokens,
        OUTPUT_TOKENS: output_tokens,
        FINISH_REASON: finish_reason,
    }
