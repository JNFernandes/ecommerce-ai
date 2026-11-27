"""``TriageModel`` over the Anthropic Messages API (research 2, FR-007, FR-011).

One call per ``draft``: ``messages.parse`` with ``DraftOutput`` as the structured output format,
``max_tokens`` and the per-call timeout from settings, and the configured effort. The SDK's own
retries are off (``max_retries=0``): the graph retries, so every attempt passes the budget check.
A refusal, a truncated answer or output that does not validate is ``ModelOutputInvalid``; a
timeout, connection or API error is ``ModelUnavailable``. Logs carry the error class only, never
prompt text or model output (FR-020).
"""

from typing import Protocol

import anthropic
import structlog
from anthropic.types import MessageParam, OutputConfigParam, ParsedMessage
from pydantic import ValidationError

from ecommerce_ai.agents.triage.model import DraftOutput
from ecommerce_ai.agents.triage.ports import (
    ModelOutputInvalid,
    ModelReply,
    ModelTimeout,
    ModelUnavailable,
    PromptInput,
)
from ecommerce_ai.infrastructure.settings import LlmEffort, Settings

logger = structlog.get_logger(__name__)

_INVALID_STOP_REASONS = frozenset({"refusal", "max_tokens", "model_context_window_exceeded"})


class MessagesParser(Protocol):
    """The part of ``AsyncAnthropic().messages`` the adapter uses (a fake in unit tests)."""

    async def parse(
        self,
        *,
        max_tokens: int,
        messages: list[MessageParam],
        model: str,
        output_config: OutputConfigParam,
        output_format: type[DraftOutput],
        system: str,
        timeout: float,
    ) -> ParsedMessage[DraftOutput]:
        """Send one message and parse its text into ``output_format``."""
        ...


class AnthropicTriageModel:
    """The triage draft model, one Messages API call per draft."""

    def __init__(
        self,
        messages: MessagesParser,
        *,
        model: str,
        max_output_tokens: int,
        timeout_seconds: float,
        effort: LlmEffort,
    ) -> None:
        """Call ``model`` through ``messages`` with the given limits."""
        self._messages = messages
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._effort = effort

    async def draft(self, prompt_input: PromptInput) -> ModelReply:
        """Ask for one structured draft; raise ``ModelUnavailable`` or ``ModelOutputInvalid``."""
        try:
            message = await self._messages.parse(
                max_tokens=self._max_output_tokens,
                messages=[{"role": "user", "content": prompt_input.user}],
                model=self._model,
                output_config={"effort": self._effort.value},
                output_format=DraftOutput,
                system=prompt_input.system,
                timeout=self._timeout_seconds,
            )
        except ValidationError as error:
            # Output that does not match the schema; its usage is lost with the response, so
            # charge the whole output limit (the budget stays conservative).
            logger.warning("llm_call_failed", error_type=type(error).__name__)
            raise ModelOutputInvalid(
                "the model output does not match the schema",
                output_tokens=self._max_output_tokens,
            ) from None
        except anthropic.APITimeoutError as error:
            logger.warning("llm_call_failed", error_type=type(error).__name__)
            raise ModelTimeout("the model did not answer in time") from None
        except anthropic.APIError as error:
            logger.warning("llm_call_failed", error_type=type(error).__name__)
            raise ModelUnavailable("the model did not answer") from None
        input_tokens = message.usage.input_tokens
        output_tokens = message.usage.output_tokens
        draft = message.parsed_output
        if message.stop_reason in _INVALID_STOP_REASONS or draft is None:
            logger.warning("llm_call_failed", stop_reason=message.stop_reason)
            raise ModelOutputInvalid(
                "the model gave no usable answer",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        logger.info(
            "llm_call",
            model_id=message.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return ModelReply(
            draft=DraftOutput.model_validate(draft.model_dump()),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_id=message.model,
        )


def build_anthropic_model(
    settings: Settings,
) -> tuple[AnthropicTriageModel, anthropic.AsyncAnthropic]:
    """Build the model from settings; the caller closes the returned client at shutdown.

    Raises ``ValueError`` when the model is not configured (``Settings.model_configured``).
    Building the client opens no connection.
    """
    if settings.anthropic_api_key is None or settings.anthropic_model is None:
        raise ValueError("the LLM is not configured")
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), max_retries=0
    )
    model = AnthropicTriageModel(
        client.messages,
        model=settings.anthropic_model,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        effort=settings.llm_effort,
    )
    return model, client
