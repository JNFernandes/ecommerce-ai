"""Scripted ``TriageModel`` for tests: no network, replies and failures given in order."""

from collections import deque
from collections.abc import Iterable

from ecommerce_ai.agents.triage.model import DraftOutput, SuggestedAction
from ecommerce_ai.agents.triage.ports import ModelError, ModelReply, PromptInput

MODEL_ID = "claude-test-model"


def reply(
    explanation: str = "The payment was declined, so the order was cancelled.",
    suggested_action: SuggestedAction = SuggestedAction.CONTACT_CUSTOMER,
    confidence: float = 0.8,
    *,
    input_tokens: int = 300,
    output_tokens: int = 40,
    model_id: str = MODEL_ID,
) -> ModelReply:
    """A valid model reply with the given draft and usage."""
    return ModelReply(
        draft=DraftOutput(
            explanation=explanation, suggested_action=suggested_action, confidence=confidence
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_id=model_id,
    )


class ScriptedModel:
    """Answers each ``draft`` with the next scripted reply, or raises the next scripted error.

    Every prompt is recorded in ``prompts``. Running out of script fails the test loudly.
    """

    def __init__(self, script: Iterable[ModelReply | ModelError] = ()) -> None:
        """Queue the replies and errors to hand out, in order."""
        self.script: deque[ModelReply | ModelError] = deque(script)
        self.prompts: list[PromptInput] = []

    @property
    def calls(self) -> int:
        """Number of ``draft`` calls so far."""
        return len(self.prompts)

    async def draft(self, prompt_input: PromptInput) -> ModelReply:
        """Record the prompt, then return or raise the next scripted item."""
        self.prompts.append(prompt_input)
        if not self.script:
            raise AssertionError("ScriptedModel was called more often than scripted")
        item = self.script.popleft()
        if isinstance(item, ModelError):
            raise item
        return item
