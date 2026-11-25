"""Fake ``ToolRunner`` for the chat assistant tests: scripted results, no upstream calls.

Each tool is given a payload (``ok``) or a ``FakeToolError``; an unknown name is refused like the
real ``ToolInvoker`` (``unknown_tool``, never run). Every call is recorded; a ``gate`` holds a call
open for disconnect tests. ``chat_graph`` / ``collect_run`` build and run the real graph on fakes.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import JsonValue

from ecommerce_ai.agents.assistant.graph import ChatGraph, GraphEvent, load_assistant_prompt
from ecommerce_ai.agents.assistant.model import RunOutcome
from ecommerce_ai.agents.assistant.ports import (
    ChatModel,
    Checkpointer,
    ConversationStore,
    ToolOutcome,
    ToolRunner,
    ToolSpec,
)
from ecommerce_ai.agents.ports import TokenBudget
from ecommerce_ai.main import create_app
from tests.support.stores import InMemoryConversationStore, InMemoryTokenBudget

UNLIMITED_BUDGET = 10**9
"""A daily budget no test reaches."""

TOOL_NAMES = (
    "get_order",
    "list_my_orders",
    "list_products",
    "get_product",
    "list_my_notifications",
)
"""The five read-only tools of the registry."""


@dataclass(frozen=True, slots=True)
class FakeToolError:
    """A scripted tool failure: an error code, optionally an upstream 401."""

    error: str = "upstream_error"
    message: str = "orders service unavailable"
    upstream_unauthenticated: bool = False


def envelope(data: JsonValue, source: str = "ecommerce-orders") -> dict[str, JsonValue]:
    """A successful tool payload, marked untrusted like the real envelopes."""
    return {"source": source, "trust": "untrusted_data", "data": data, "truncated": []}


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One call the fake received."""

    name: str
    arguments: dict[str, JsonValue]


@dataclass
class FakeToolRunner:
    """Answers each registered tool with its scripted result."""

    results: dict[str, dict[str, JsonValue] | FakeToolError] = field(default_factory=dict)
    names: tuple[str, ...] = TOOL_NAMES
    gate: asyncio.Event | None = None
    invalid_arguments: frozenset[str] = frozenset()
    calls: list[ToolCallRecord] = field(default_factory=list)
    mutating: frozenset[str] = frozenset()
    """Names that run only through ``run_approved`` (and are refused by ``run``)."""
    approved_calls: list[ToolCallRecord] = field(default_factory=list)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """One spec per registered tool name."""
        return tuple(
            ToolSpec(name=name, description=f"The {name} tool.", input_schema={"type": "object"})
            for name in self.names
        )

    async def run(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Record the call and return its scripted outcome.

        Unknown names and the tools listed in ``invalid_arguments`` are refused, never run.
        """
        if name not in self.names or name in self.invalid_arguments or name in self.mutating:
            unknown = name not in self.names or name in self.mutating
            return ToolOutcome(
                ok=False,
                payload={
                    "error": "not_allowed" if unknown else "invalid_arguments",
                    "message": "not allowed" if unknown else "invalid arguments",
                },
                outcome="unknown_tool" if unknown else "invalid_arguments",
                refused=True,
                upstream_unauthenticated=False,
                duration_ms=0.0,
            )
        self.calls.append(ToolCallRecord(name, dict(arguments)))
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.get(name, envelope([]))
        if isinstance(result, FakeToolError):
            return ToolOutcome(
                ok=False,
                payload={"error": result.error, "message": result.message},
                outcome=result.error,
                refused=False,
                upstream_unauthenticated=result.upstream_unauthenticated,
                duration_ms=1.0,
            )
        return ToolOutcome(
            ok=True,
            payload=result,
            outcome="ok",
            refused=False,
            upstream_unauthenticated=False,
            duration_ms=1.0,
        )

    async def run_approved(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Record an approved mutating call and return its scripted outcome.

        Only names in ``mutating`` run; every other name is refused like the real invoker.
        """
        if name not in self.mutating:
            return ToolOutcome(
                ok=False,
                payload={"error": "not_allowed", "message": "not allowed"},
                outcome="unknown_tool",
                refused=True,
                upstream_unauthenticated=False,
                duration_ms=0.0,
            )
        self.approved_calls.append(ToolCallRecord(name, dict(arguments)))
        result = self.results.get(name, envelope({}))
        if isinstance(result, FakeToolError):
            return ToolOutcome(
                ok=False,
                payload={"error": result.error, "message": result.message},
                outcome=result.error,
                refused=False,
                upstream_unauthenticated=result.upstream_unauthenticated,
                duration_ms=1.0,
            )
        return ToolOutcome(
            ok=True,
            payload=result,
            outcome="ok",
            refused=False,
            upstream_unauthenticated=False,
            duration_ms=1.0,
        )


async def collect_run(
    graph: ChatGraph, conversation_id: UUID, text: str
) -> tuple[list[GraphEvent], RunOutcome]:
    """Run ``graph`` once; return its events and its outcome."""
    events: list[GraphEvent] = []
    outcome: RunOutcome | None = None
    async for item in graph.run(conversation_id, text):
        if isinstance(item, RunOutcome):
            outcome = item
        else:
            events.append(item)
    assert outcome is not None
    return events, outcome


def chat_graph(
    model: ChatModel | None,
    tools: ToolRunner | None = None,
    checkpointer: Checkpointer | None = None,
) -> ChatGraph:
    """The chat graph with the real prompt, fake tools and an in-memory saver by default."""
    return ChatGraph(
        model=model,
        tools=tools or FakeToolRunner(),
        prompt=load_assistant_prompt(),
        checkpointer=checkpointer or InMemorySaver(),
    )


def chat_app(
    model: ChatModel | None,
    tools: ToolRunner | None = None,
    store: ConversationStore | None = None,
    checkpointer: Checkpointer | None = None,
    budget: TokenBudget | None = None,
) -> FastAPI:
    """The app with the chat on fakes: in-memory store, saver and budget, fake tools, no probes."""
    return create_app(
        probes=[],
        conversation_store=store or InMemoryConversationStore(),
        chat_model=model,
        tool_runner=tools or FakeToolRunner(),
        checkpointer=checkpointer or InMemorySaver(),
        token_budget=budget or InMemoryTokenBudget(UNLIMITED_BUDGET),
    )
