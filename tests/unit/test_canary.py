"""Per-run canary in the system prompt (US-07 FR-007, research §6)."""

from uuid import uuid4

from ecommerce_ai.agents.assistant.graph import ASSISTANT_PROMPT_VERSION
from ecommerce_ai.agents.guardrails import Guardrails
from ecommerce_ai.agents.guardrails.canary import CANARY_PATTERN, new_canary
from tests.support.chat import chat_graph, collect_run
from tests.support.llm import ScriptedChatModel, text_turn


def test_should_be_cnry_and_24_hex_characters_when_made() -> None:
    assert CANARY_PATTERN.fullmatch(new_canary())


def test_should_differ_on_every_call_when_made() -> None:
    assert len({new_canary() for _ in range(50)}) == 50


async def test_should_render_a_fresh_canary_into_the_system_prompt_of_every_run() -> None:
    model = ScriptedChatModel([text_turn("Hello."), text_turn("Hello again.")])
    graph = chat_graph(model, guardrails=Guardrails(canary_factory=new_canary))
    conversation = uuid4()

    await collect_run(graph, conversation, "Hi")
    await collect_run(graph, conversation, "Hi again")

    canaries = [CANARY_PATTERN.findall(request.system) for request in model.requests]
    assert [len(found) for found in canaries] == [1, 1]
    assert canaries[0] != canaries[1]
    assert ASSISTANT_PROMPT_VERSION == "v4"


async def test_should_never_checkpoint_the_canary() -> None:
    model = ScriptedChatModel([text_turn("Hello.")])
    graph = chat_graph(model, guardrails=Guardrails(canary_factory=new_canary))
    conversation = uuid4()

    await collect_run(graph, conversation, "Hi")

    (canary,) = CANARY_PATTERN.findall(model.requests[0].system)
    state = await graph._compiled.aget_state({"configurable": {"thread_id": str(conversation)}})
    assert canary not in repr(state.values)


async def test_should_render_none_in_the_slot_when_the_bundle_has_no_canary() -> None:
    model = ScriptedChatModel([text_turn("Hello.")])

    await collect_run(chat_graph(model), uuid4(), "Hi")

    assert CANARY_PATTERN.search(model.requests[0].system) is None
    assert "{canary}" not in model.requests[0].system
