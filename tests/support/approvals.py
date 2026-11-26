"""A chat app on fakes for the approval tests (US-06): propose, decide, inspect.

``ApprovalHarness`` holds the in-memory conversation and approval stores, the saver, a fake
clock, fake tools with the mutating ``cancel_order`` and a scripted model. ``client()`` builds a
fresh app over the same stores (a restart); ``propose`` posts a message the model answers with a
``cancel_order`` call; ``decide`` posts a decision. Bypass tests assert
``tools.approved_calls == []`` (SC-001).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import TracerProvider
from pydantic import JsonValue

from ecommerce_ai.infrastructure.settings import Settings
from tests.support.chat import TOOL_NAMES, FakeToolRunner, chat_app, envelope
from tests.support.llm import ScriptedChatModel, TurnStep, text_turn, tool_turn
from tests.support.sse import SseEvent, events_of, parse_sse
from tests.support.stores import InMemoryApprovalStore, InMemoryConversationStore, StoredAction
from tests.support.tokens import make_token
from tests.support.triage import FakeClock

ORDER_ID: Final = "3f2a4c1e-9b8d-4e7f-a6b5-1c2d3e4f5a6b"
OTHER_ORDER_ID: Final = "7d6c5b4a-3e2f-4a1b-9c8d-0e1f2a3b4c5d"
OWNER: Final = "user-1"
TOKEN: Final = make_token(sub=OWNER)
OTHER_TOKEN: Final = make_token(sub="user-2")
CANCEL_CALL_ID: Final = "toolu_cancel"


def order(status: str = "PENDING", order_id: str = ORDER_ID, total: float = 59.9) -> JsonValue:
    """A ``get_order`` data object (camelCase like the real envelope)."""
    return {"orderId": order_id, "status": status, "totalAmount": total}


def cancelled(order_id: str = ORDER_ID) -> dict[str, JsonValue]:
    """The ``cancel_order`` envelope of a confirmed cancel."""
    return envelope({"orderId": order_id, "status": "CANCELLED"})


def cancel_turn(order_id: str = ORDER_ID, call_id: str = CANCEL_CALL_ID) -> list[TurnStep]:
    """A model turn asking for ``cancel_order`` on ``order_id``."""
    return tool_turn((call_id, "cancel_order", {"orderId": order_id}))


def headers(token: str = TOKEN) -> dict[str, str]:
    """The bearer header of ``token``."""
    return {"Authorization": f"Bearer {token}"}


def sse(response: httpx.Response) -> list[SseEvent]:
    """The events of an SSE response body."""
    return events_of(parse_sse(response.text))


def names(events: Iterable[SseEvent]) -> list[str]:
    """The event names, in order."""
    return [event.event for event in events]


def data(events: Sequence[SseEvent], name: str) -> dict[str, JsonValue]:
    """The data of the only event called ``name``."""
    (found,) = [event.data for event in events if event.event == name]
    assert isinstance(found, dict)
    return found


@dataclass
class ApprovalHarness:
    """Stores, saver, clock, tools and model shared by every app built from it."""

    model: ScriptedChatModel = field(default_factory=ScriptedChatModel)
    store: InMemoryConversationStore = field(default_factory=InMemoryConversationStore)
    checkpointer: InMemorySaver = field(default_factory=InMemorySaver)
    clock: FakeClock = field(default_factory=FakeClock)
    tools: FakeToolRunner = field(
        default_factory=lambda: FakeToolRunner(
            results={"get_order": envelope(order()), "cancel_order": cancelled()},
            names=(*TOOL_NAMES, "cancel_order"),
            mutating=frozenset({"cancel_order"}),
        )
    )
    settings: Settings | None = None
    tracer_provider: TracerProvider | None = None
    approvals: InMemoryApprovalStore = field(init=False)

    def __post_init__(self) -> None:
        """Share the conversation store with the approval store."""
        self.approvals = InMemoryApprovalStore(self.store)

    def script(self, *turns: Sequence[TurnStep]) -> None:
        """Queue model turns."""
        self.model.turns.extend(turns)

    def client(self) -> httpx.AsyncClient:
        """A client over a new app on the shared stores (a restart keeps everything)."""
        app = chat_app(
            self.model,
            self.tools,
            self.store,
            self.checkpointer,
            approvals=self.approvals,
            clock=self.clock,
            settings=self.settings,
            tracer_provider=self.tracer_provider,
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        )

    async def conversation(self, client: httpx.AsyncClient, token: str = TOKEN) -> str:
        """Create a conversation; return its id."""
        response = await client.post("/chat/conversations", json={}, headers=headers(token))
        assert response.status_code == 201, response.text
        conversation_id = response.json()["id"]
        assert isinstance(conversation_id, str)
        return conversation_id

    async def post(
        self,
        client: httpx.AsyncClient,
        conversation_id: str,
        content: str = f"Please cancel my order {ORDER_ID}",
        token: str = TOKEN,
    ) -> httpx.Response:
        """Post a chat message."""
        return await client.post(
            f"/chat/conversations/{conversation_id}/messages",
            json={"content": content},
            headers=headers(token),
        )

    async def propose(
        self, client: httpx.AsyncClient, text: str = "I can cancel it for you."
    ) -> tuple[str, str, list[SseEvent]]:
        """A new conversation whose message ends ``awaiting_approval``: ids and events."""
        self.script([*text_turn(text)[:-1], *cancel_turn()])
        conversation_id = await self.conversation(client)
        response = await self.post(client, conversation_id)
        assert response.status_code == 200, response.text
        events = sse(response)
        approval_id = data(events, "approval.required")["approvalId"]
        assert isinstance(approval_id, str)
        return conversation_id, approval_id, events

    async def decide(
        self,
        client: httpx.AsyncClient,
        conversation_id: str,
        approval_id: str,
        decision: object = "approve",
        token: str = TOKEN,
    ) -> httpx.Response:
        """Post a decision."""
        return await client.post(
            f"/chat/conversations/{conversation_id}/approvals/{approval_id}",
            json={"decision": decision},
            headers=headers(token),
        )

    def action(self, approval_id: str) -> StoredAction:
        """The audit row of ``approval_id``."""
        return self.approvals.actions[UUID(approval_id)]
