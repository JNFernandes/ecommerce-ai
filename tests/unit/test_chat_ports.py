"""Chat assistant ports: model events, tool outcomes and the checkpointer alias."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from ecommerce_ai.agents.assistant.model import Usage, UserTurn
from ecommerce_ai.agents.assistant.ports import (
    ChatModelEvent,
    ChatModelRequest,
    Checkpointer,
    ModelTurnEnd,
    ModelUnavailable,
    TextDelta,
    ToolOutcome,
    ToolSpec,
    ToolUseRequest,
)
from ecommerce_ai.agents.ports import ModelError


def test_should_reuse_the_shared_model_unavailable_error() -> None:
    assert issubclass(ModelUnavailable, ModelError)


def test_should_build_a_request_from_the_prompt_transcript_and_tools() -> None:
    request = ChatModelRequest(
        system="You are the shop assistant.",
        transcript=(UserTurn(text="Hi"),),
        tools=(ToolSpec(name="get_order", description="One order.", input_schema={}),),
    )

    assert request.tools[0].name == "get_order"
    with pytest.raises(ValidationError):
        ChatModelRequest(system="", transcript=(), tools=())


def test_should_tag_each_model_event_with_its_kind() -> None:
    events: list[ChatModelEvent] = [
        TextDelta(text="Hel"),
        ToolUseRequest(id="toolu_01", name="get_order", input={"orderId": "x"}),
        ModelTurnEnd(stop_reason="end_turn", usage=Usage(input_tokens=3), model_id="m"),
    ]

    assert [event.kind for event in events] == ["text", "tool_use", "turn_end"]


def test_should_keep_a_tool_outcome_immutable() -> None:
    outcome = ToolOutcome(
        ok=True,
        payload={"data": []},
        outcome="ok",
        refused=False,
        upstream_unauthenticated=False,
        duration_ms=1.5,
    )

    with pytest.raises(ValidationError):
        outcome.ok = False  # type: ignore[misc]  # frozen model, the assignment must fail


def test_should_accept_the_in_memory_saver_as_a_checkpointer() -> None:
    saver: Checkpointer = InMemorySaver()

    assert saver is not None
