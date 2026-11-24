"""The chat service: the single agent entry point of the ``/chat`` routes (research §16, §17).

``ChatService.start`` runs the checks that decide the HTTP answer before any stream opens: an
unknown or foreign conversation gives ``None`` (404); then the user message is stored (a storage
failure propagates, 503). The returned ``ChatRun`` streams the contract events: one
``message.start``, the graph's events as they come, then exactly one ``message.end`` (the
assistant message is stored first) or one ``error`` (nothing more is stored; an unexpected graph
or storage failure is ``INTERNAL``). One ``chat_run`` log line per run carries counts, timings,
outcomes and a hash of the user, never content, arguments, results or the token (FR-026).
"""

import hashlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

import structlog

from ecommerce_ai.agents.assistant.graph import ChatGraph
from ecommerce_ai.agents.assistant.model import (
    ChatErrorCode,
    MessageEnd,
    MessageStart,
    RunOutcome,
    StreamError,
    StreamEvent,
)
from ecommerce_ai.agents.assistant.ports import ConversationStore
from ecommerce_ai.agents.ports import Clock

USER_HASH_CHARS: Final = 16

_logger = structlog.get_logger(__name__)


def user_hash(subject: str) -> str:
    """A short SHA-256 prefix of the token subject, for logs (never the raw subject)."""
    return hashlib.sha256(subject.encode()).hexdigest()[:USER_HASH_CHARS]


def _internal(outcome: RunOutcome | None) -> RunOutcome:
    """``outcome`` turned into an ``INTERNAL`` error, keeping what it spent and ran."""
    if outcome is None:
        return RunOutcome(error=ChatErrorCode.INTERNAL)
    return outcome.model_copy(update={"finish_reason": None, "error": ChatErrorCode.INTERNAL})


@dataclass(frozen=True, slots=True)
class ChatRun:
    """One accepted message and the stream of its answer."""

    user_id: str
    conversation_id: UUID
    message_id: UUID
    content: str
    store: ConversationStore
    graph: ChatGraph
    clock: Clock

    async def events(self) -> AsyncGenerator[StreamEvent]:
        """``message.start``, the run's events, then ``message.end`` or ``error``."""
        started = self.clock.monotonic()
        yield MessageStart(conversation_id=self.conversation_id, message_id=self.message_id)
        outcome: RunOutcome | None = None
        try:
            async for item in self.graph.run(self.conversation_id, self.content):
                if isinstance(item, RunOutcome):
                    outcome = item
                else:
                    yield item
        except Exception as error:
            _logger.error("chat_run_failed", error_type=type(error).__name__)
            outcome = _internal(outcome)
        if outcome is None:
            outcome = _internal(None)
        if outcome.finish_reason is not None:
            outcome = await self._store_answer(outcome)
        if outcome.error is not None:
            yield StreamError.for_code(outcome.error)
        elif outcome.finish_reason is not None:
            yield MessageEnd(
                message_id=self.message_id,
                finish_reason=outcome.finish_reason,
                prompt_version=self.graph.prompt_version,
                model_id=outcome.model_id,
                usage=outcome.usage,
            )
        self._log(outcome, started)

    async def _store_answer(self, outcome: RunOutcome) -> RunOutcome:
        assert outcome.finish_reason is not None  # noqa: S101  # checked by the caller
        try:
            await self.store.add_assistant_message(
                self.user_id,
                self.conversation_id,
                self.message_id,
                outcome.text,
                outcome.finish_reason,
                self.graph.prompt_version,
                outcome.model_id,
                outcome.usage,
                self.clock.now(),
            )
        except Exception as error:
            _logger.error("chat_store_failed", error_type=type(error).__name__)
            return _internal(outcome)
        return outcome

    def _log(self, outcome: RunOutcome, started: float) -> None:
        _logger.info(
            "chat_run",
            conversation_id=str(self.conversation_id),
            message_id=str(self.message_id),
            user_hash=user_hash(self.user_id),
            latency_ms=round((self.clock.monotonic() - started) * 1000, 1),
            # Not ``*_tokens``: the log redactor hides every field whose name contains "token".
            usage={"input": outcome.usage.input_tokens, "output": outcome.usage.output_tokens},
            tools=[stat.model_dump(mode="json") for stat in outcome.tools],
            finish_reason=None if outcome.finish_reason is None else outcome.finish_reason.value,
            error_code=None if outcome.error is None else outcome.error.value,
            prompt_version=self.graph.prompt_version,
            model_id=outcome.model_id,
        )


class ChatService:
    """Accepts customer messages and deletes conversations with their graph state."""

    def __init__(self, store: ConversationStore, graph: ChatGraph, clock: Clock) -> None:
        """Store in ``store``, answer with ``graph``, stamp with ``clock``."""
        self._store = store
        self._graph = graph
        self._clock = clock

    async def start(self, user_id: str, conversation_id: UUID, content: str) -> ChatRun | None:
        """Store the user message and return its run; ``None`` when the conversation is unknown.

        ``content`` is already trimmed and within limits (validated by the API).
        """
        if await self._store.count_messages(user_id, conversation_id) is None:
            return None
        await self._store.add_user_message(
            user_id, conversation_id, uuid4(), content, self._clock.now()
        )
        return ChatRun(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=uuid4(),
            content=content,
            store=self._store,
            graph=self._graph,
            clock=self._clock,
        )

    async def delete(self, user_id: str, conversation_id: UUID) -> bool:
        """Delete the conversation, its messages and its graph state; ``False`` when unknown."""
        if not await self._store.delete(user_id, conversation_id):
            return False
        await self._graph.forget(conversation_id)
        return True
