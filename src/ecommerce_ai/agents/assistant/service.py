"""The chat service: the single agent entry point of the ``/chat`` routes (research §11-§17).

``ChatService.start`` runs the checks that decide the HTTP answer before any stream opens, in
order: an unknown or foreign conversation gives ``None`` (404); a conversation that cannot take
another exchange raises ``ConversationFull`` and one with an answer in progress ``StreamActive``
(409); then the user message is stored (a storage failure propagates, 503, and frees the slot).

The returned ``ChatRun`` streams the contract events: one ``message.start``, the graph's events as
they come, then exactly one ``message.end`` (the assistant message is stored first) or one
``error`` (nothing more is stored; an unexpected graph or storage failure is ``INTERNAL``). Every
model call is admitted by the run's ``RunAllowance`` (daily budget, per-message cap).

When the run stops early (the client went away, or a delete cancelled it), a shielded
``finish_disconnected`` stores the text already sent as ``client_disconnected`` and puts it in
the graph's transcript (US6). On every ending the allowance is settled and the conversation's
slot released. One ``chat_run`` log line per run carries counts, timings, outcomes and a hash of
the user, never content, arguments, results or the token (FR-026).
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from typing import Final
from uuid import UUID, uuid4

import structlog

from ecommerce_ai.agents.assistant.graph import ChatGraph
from ecommerce_ai.agents.assistant.limits import (
    MESSAGES_PER_EXCHANGE,
    ActiveStreams,
    ChatLimits,
    ConversationFull,
    RunAllowance,
)
from ecommerce_ai.agents.assistant.model import (
    ChatErrorCode,
    FinishReason,
    MessageEnd,
    MessageStart,
    RunOutcome,
    StreamError,
    StreamEvent,
    Token,
    Usage,
)
from ecommerce_ai.agents.assistant.ports import ConversationStore
from ecommerce_ai.agents.ports import Clock, TokenBudget

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


class ChatRun:
    """One accepted message and the stream of its answer; holds the conversation's slot."""

    def __init__(
        self,
        *,
        user_id: str,
        conversation_id: UUID,
        content: str,
        store: ConversationStore,
        graph: ChatGraph,
        clock: Clock,
        streams: ActiveStreams,
        allowance: RunAllowance | None,
    ) -> None:
        """Answer ``content``; the caller has taken the conversation's slot in ``streams``."""
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.message_id = uuid4()
        self.content = content
        self._store = store
        self._graph = graph
        self._clock = clock
        self._streams = streams
        self._allowance = allowance
        self._started = False

    async def events(self) -> AsyncGenerator[StreamEvent]:
        """``message.start``, the run's events, then ``message.end`` or ``error``."""
        self._started = True
        self._streams.attach(self.conversation_id)
        started = self._clock.monotonic()
        sent: list[str] = []
        finished = False
        try:
            yield MessageStart(conversation_id=self.conversation_id, message_id=self.message_id)
            outcome: RunOutcome | None = None
            run = self._graph.run(self.conversation_id, self.content, self._allowance)
            try:
                async for item in run:
                    if isinstance(item, RunOutcome):
                        outcome = item
                        continue
                    if isinstance(item, Token):
                        sent.append(item.text)
                    yield item
            except Exception as error:
                _logger.error("chat_run_failed", error_type=type(error).__name__)
                outcome = _internal(outcome)
            finally:
                await run.aclose()
            if outcome is None:
                outcome = _internal(None)
            if outcome.finish_reason is not None:
                outcome = await self._store_answer(outcome)
            finished = True
            self._log(outcome, started)
            if outcome.error is not None:
                yield StreamError.for_code(outcome.error)
            elif outcome.finish_reason is not None:
                yield MessageEnd(
                    message_id=self.message_id,
                    finish_reason=outcome.finish_reason,
                    prompt_version=self._graph.prompt_version,
                    model_id=outcome.model_id,
                    usage=outcome.usage,
                )
        finally:
            # Shielded: a cancelled stream still stores, settles and frees the slot.
            closing = self._close(finished=finished, sent="".join(sent), started=started)
            await asyncio.shield(closing)

    def discard(self) -> None:
        """Free the slot of a run whose stream never started (the response was never sent)."""
        if not self._started:
            self._streams.release(self.conversation_id)

    async def _close(self, *, finished: bool, sent: str, started: float) -> None:
        try:
            if not finished:
                await self.finish_disconnected(sent, started)
            if self._allowance is not None:
                try:
                    await self._allowance.settle()
                except Exception as error:
                    _logger.error("chat_settle_failed", error_type=type(error).__name__)
        finally:
            self._streams.release(self.conversation_id)

    async def finish_disconnected(self, sent: str, started: float) -> None:
        """Keep the text already sent as a ``client_disconnected`` answer (US6)."""
        allowance = self._allowance
        outcome = RunOutcome(
            finish_reason=FinishReason.CLIENT_DISCONNECTED,
            text=sent,
            usage=Usage() if allowance is None else allowance.usage,
            model_id=None if allowance is None else allowance.model_id,
        )
        try:
            await self._graph.record_interrupted(self.conversation_id, sent)
        except Exception as error:
            _logger.error("chat_record_interrupted_failed", error_type=type(error).__name__)
        self._log(await self._store_answer(outcome), started)

    async def _store_answer(self, outcome: RunOutcome) -> RunOutcome:
        assert outcome.finish_reason is not None  # noqa: S101  # checked by the caller
        try:
            await self._store.add_assistant_message(
                self.user_id,
                self.conversation_id,
                self.message_id,
                outcome.text,
                outcome.finish_reason,
                self._graph.prompt_version,
                outcome.model_id,
                outcome.usage,
                self._clock.now(),
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
            latency_ms=round((self._clock.monotonic() - started) * 1000, 1),
            # Not ``*_tokens``: the log redactor hides every field whose name contains "token".
            usage={"input": outcome.usage.input_tokens, "output": outcome.usage.output_tokens},
            tools=[stat.model_dump(mode="json") for stat in outcome.tools],
            finish_reason=None if outcome.finish_reason is None else outcome.finish_reason.value,
            error_code=None if outcome.error is None else outcome.error.value,
            prompt_version=self._graph.prompt_version,
            model_id=outcome.model_id,
        )


class ChatService:
    """Accepts customer messages and deletes conversations with their graph state."""

    def __init__(
        self,
        store: ConversationStore,
        graph: ChatGraph,
        clock: Clock,
        *,
        budget: TokenBudget | None = None,
        limits: ChatLimits | None = None,
        streams: ActiveStreams | None = None,
    ) -> None:
        """Store in ``store``, answer with ``graph``, stamp with ``clock``.

        ``budget`` is the daily token budget (``None``: no token limit), ``limits`` the
        per-conversation and per-message limits, ``streams`` the answers in progress.
        """
        self._store = store
        self._graph = graph
        self._clock = clock
        self._budget = budget
        self._limits = limits or ChatLimits()
        self.streams = streams or ActiveStreams()

    async def start(self, user_id: str, conversation_id: UUID, content: str) -> ChatRun | None:
        """Store the user message and return its run; ``None`` when the conversation is unknown.

        ``content`` is already trimmed and within limits (validated by the API). Raises
        ``ConversationFull`` or ``StreamActive`` (nothing stored).
        """
        count = await self._store.count_messages(user_id, conversation_id)
        if count is None:
            return None
        if count + MESSAGES_PER_EXCHANGE > self._limits.max_messages:
            raise ConversationFull
        self.streams.acquire(conversation_id)
        try:
            await self._store.add_user_message(
                user_id, conversation_id, uuid4(), content, self._clock.now()
            )
        except BaseException:
            self.streams.release(conversation_id)
            raise
        allowance = (
            None
            if self._budget is None
            else RunAllowance(self._budget, self._clock.now().date(), self._limits)
        )
        return ChatRun(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            store=self._store,
            graph=self._graph,
            clock=self._clock,
            streams=self.streams,
            allowance=allowance,
        )

    async def delete(self, user_id: str, conversation_id: UUID) -> bool:
        """Delete the conversation, its messages and its graph state; ``False`` when unknown.

        An answer in progress is cancelled and awaited first (US6).
        """
        if self.streams.is_active(conversation_id):
            # Only the owner may cancel: a foreign id stays indistinguishable from unknown.
            if await self._store.count_messages(user_id, conversation_id) is None:
                return False
            await self.streams.cancel(conversation_id)
        if not await self._store.delete(user_id, conversation_id):
            return False
        await self._graph.forget(conversation_id)
        return True
