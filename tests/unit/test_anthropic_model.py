"""The Anthropic adapter of ``TriageModel``, driven through a fake SDK client (no network)."""

import io
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import MessageParam, OutputConfigParam, ParsedMessage, Usage
from anthropic.types.parsed_message import ParsedTextBlock
from pydantic import SecretStr, ValidationError

from ecommerce_ai.agents.triage.model import DraftOutput, SuggestedAction
from ecommerce_ai.agents.triage.ports import (
    ModelOutputInvalid,
    ModelUnavailable,
    PromptInput,
    TriageModel,
)
from ecommerce_ai.infrastructure.llm.anthropic_model import (
    AnthropicTriageModel,
    build_anthropic_model,
)
from ecommerce_ai.infrastructure.logging import configure_logging
from ecommerce_ai.infrastructure.settings import LlmEffort, Settings

MODEL = "claude-test-model"
PROMPT = PromptInput(system="You explain failed orders.", user="<evidence>secret</evidence>")
DRAFT = DraftOutput(
    explanation="The card was declined.",
    suggested_action=SuggestedAction.CONTACT_CUSTOMER,
    confidence=0.7,
)
REQUEST = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")


def _message(
    draft: DraftOutput | None = DRAFT, stop_reason: str = "end_turn"
) -> ParsedMessage[DraftOutput]:
    text = draft.model_dump_json() if draft else "I can't help with that."
    return ParsedMessage[DraftOutput].model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [ParsedTextBlock[DraftOutput](type="text", text=text, parsed_output=draft)],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": Usage(input_tokens=420, output_tokens=55),
        }
    )


class FakeMessages:
    """Records each ``parse`` call and returns or raises the scripted outcome."""

    def __init__(self, outcome: ParsedMessage[DraftOutput] | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []  # Any: recorded keyword arguments of mixed types

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
        self.calls.append(
            {
                "max_tokens": max_tokens,
                "messages": messages,
                "model": model,
                "output_config": output_config,
                "output_format": output_format,
                "system": system,
                "timeout": timeout,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _model(messages: FakeMessages) -> AnthropicTriageModel:
    return AnthropicTriageModel(
        messages,
        model=MODEL,
        max_output_tokens=512,
        timeout_seconds=12.5,
        effort=LlmEffort.LOW,
    )


def _validation_error() -> ValidationError:
    try:
        DraftOutput.model_validate_json('{"explanation": ""}')
    except ValidationError as error:
        return error
    raise AssertionError("expected a validation error")  # pragma: no cover


def test_should_implement_the_triage_model_port() -> None:
    port: TriageModel = _model(FakeMessages(_message()))
    assert port is not None


async def test_should_send_the_prompt_with_limits_and_return_the_draft_and_usage() -> None:
    messages = FakeMessages(_message())

    reply = await _model(messages).draft(PROMPT)

    assert reply.draft == DRAFT
    assert (reply.input_tokens, reply.output_tokens) == (420, 55)
    assert reply.model_id == MODEL
    assert messages.calls == [
        {
            "max_tokens": 512,
            "messages": [{"role": "user", "content": PROMPT.user}],
            "model": MODEL,
            "output_config": {"effort": "low"},
            "output_format": DraftOutput,
            "system": PROMPT.system,
            "timeout": 12.5,
        }
    ]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_should_raise_invalid_output_with_usage_when_the_answer_is_unusable(
    stop_reason: str,
) -> None:
    messages = FakeMessages(_message(None, stop_reason=stop_reason))

    with pytest.raises(ModelOutputInvalid) as caught:
        await _model(messages).draft(PROMPT)

    assert (caught.value.input_tokens, caught.value.output_tokens) == (420, 55)


async def test_should_raise_invalid_output_when_a_parsed_draft_was_truncated() -> None:
    with pytest.raises(ModelOutputInvalid):
        await _model(FakeMessages(_message(DRAFT, stop_reason="max_tokens"))).draft(PROMPT)


async def test_should_charge_the_output_limit_when_the_output_fails_the_schema() -> None:
    with pytest.raises(ModelOutputInvalid) as caught:
        await _model(FakeMessages(_validation_error())).draft(PROMPT)

    assert (caught.value.input_tokens, caught.value.output_tokens) == (0, 512)


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=REQUEST),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.InternalServerError(
            "overloaded", response=httpx2.Response(529, request=REQUEST), body=None
        ),
    ],
)
async def test_should_raise_unavailable_when_the_api_fails(error: anthropic.APIError) -> None:
    with pytest.raises(ModelUnavailable):
        await _model(FakeMessages(error)).draft(PROMPT)


async def test_should_never_log_prompt_text_or_model_output() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)

    await _model(FakeMessages(_message())).draft(PROMPT)
    with pytest.raises(ModelOutputInvalid):
        await _model(FakeMessages(_message(None, stop_reason="refusal"))).draft(PROMPT)

    logged = stream.getvalue()
    assert "llm_call" in logged
    for text in ("secret", PROMPT.system, DRAFT.explanation, "can't help"):
        assert text not in logged


def test_should_build_the_model_from_settings_when_configured() -> None:
    settings = Settings(anthropic_api_key=SecretStr("sk-test"), anthropic_model=MODEL)

    model, client = build_anthropic_model(settings)

    assert isinstance(model, AnthropicTriageModel)
    assert client.max_retries == 0


def test_should_refuse_to_build_the_model_when_it_is_not_configured() -> None:
    with pytest.raises(ValueError, match="not configured"):
        build_anthropic_model(Settings())
