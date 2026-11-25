"""The chat service: the single agent entry point of the ``/chat`` routes (research §11-§17).

``ChatService.start`` runs the checks that decide the HTTP answer before any stream opens, in
order: an unknown or foreign conversation gives ``None`` (404); a conversation that cannot take
another exchange raises ``ConversationFull`` and one with an answer in progress ``StreamActive``
(409); a non-expired pending approval raises ``ApprovalPending`` (409); then the user message is
stored (a storage failure propagates, 503, and frees the slot).

The returned ``ChatRun`` streams the contract events: one ``message.start``, the graph's events as
they come, then exactly one ``message.end`` (the assistant message is stored first) or one
``error`` (nothing more is stored; an unexpected graph or storage failure is ``INTERNAL``). Every
model call is admitted by the run's ``RunAllowance`` (daily budget, per-message cap).

Approvals (US-06): a run that pauses on a proposal stores the approval, its audit row and the
assistant message together, then sends ``approval.required`` and ``message.end``
``awaiting_approval``. ``ChatService.decide`` records the decision atomically before resuming the
graph (0 rows → 404 / 409 / 410); its stream records the outcome as soon as the graph carried the
decision out. Once decided, that step is shielded from a client disconnect: a started cancel always
completes and its outcome is recorded. Expiry is lazy (research §6): a decision, a new message or a
read closes an expired approval, an orphaned pause (no stored approval) and a decided approval whose
outcome was never recorded (a crash: ``failed``).

When the run stops early (the client went away, or a delete cancelled it), a shielded
``finish_disconnected`` stores the text already sent as ``client_disconnected`` and puts it in
the graph's transcript (US6). On every ending the allowance is settled and the conversation's
slot released. One ``chat_run`` log line per run carries counts, timings, outcomes and a hash of
the user, never content, arguments, results or the token (FR-026).
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

import structlog

from ecommerce_ai.agents.assistant.approvals import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalPending,
)
from ecommerce_ai.agents.assistant.graph import ChatGraph, GraphEvent
from ecommerce_ai.agents.assistant.limits import (
    MESSAGES_PER_EXCHANGE,
    ActiveStreams,
    ChatLimits,
    ConversationFull,
    RunAllowance,
)
from ecommerce_ai.agents.assistant.model import (
    ApprovalExecution,
    ApprovalOutcome,
    ChatErrorCode,
    Decision,
    FinishReason,
    MessageEnd,
    MessageStart,
    RunOutcome,
    StreamError,
    StreamEvent,
    Token,
    Usage,
)
from ecommerce_ai.agents.assistant.ports import (
    Approval,
    ApprovalStatus,
    ApprovalStore,
    ConversationStore,
    ProposalMessage,
)
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
    return outcome.model_copy(
        update={"finish_reason": None, "error": ChatErrorCode.INTERNAL, "proposal": None}
    )


@dataclass(frozen=True, slots=True)
class _Decided:
    """The decision a run resumes from."""

    approval: Approval
    decision: Decision


class _End:
    """Marks the end of the graph's events in the decision queue."""


_END: Final = _End()


@dataclass(frozen=True, slots=True)
class _Failed:
    """The graph failed; the error is raised to the reader."""

    error: Exception


type _Queued = GraphEvent | RunOutcome | _End | _Failed


class ChatRun:
    """One accepted message (or decision) and the stream of its answer; holds the slot."""

    def __init__(
        self,
        *,
        user_id: str,
        conversation_id: UUID,
        content: str | None,
        store: ConversationStore,
        graph: ChatGraph,
        clock: Clock,
        streams: ActiveStreams,
        allowance: RunAllowance | None,
        approvals: ApprovalStore | None = None,
        request_id: str | None = None,
        decided: _Decided | None = None,
    ) -> None:
        """Answer ``content``, or resume from ``decided``; the caller has taken the slot."""
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.message_id = uuid4()
        self.content = content
        self._store = store
        self._graph = graph
        self._clock = clock
        self._streams = streams
        self._allowance = allowance
        self._approvals = approvals
        self._request_id = request_id
        self._decided = decided
        self._execution: ApprovalExecution | None = None
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
            run = self._graph_events()
            try:
                async for item in run:
                    if isinstance(item, RunOutcome):
                        outcome = item
                        continue
                    if isinstance(item, ApprovalExecution):
                        await self._executed(item)
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
            if outcome.proposal is not None:
                outcome = await self._store_proposal(outcome)
            elif outcome.finish_reason is not None:
                outcome = await self._store_answer(outcome)
            finished = True
            self._log(outcome, started)
            if outcome.error is not None:
                yield StreamError.for_code(outcome.error)
            elif outcome.finish_reason is not None:
                if outcome.proposal is not None:
                    yield outcome.proposal.required_event()
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

    def _graph_events(self) -> AsyncGenerator[GraphEvent | RunOutcome]:
        if self._decided is None:
            return self._graph.run(self.conversation_id, self.content or "", self._allowance)
        resumed = self._graph.resume(
            self.conversation_id,
            self._decided.approval.id,
            self._decided.decision,
            self._allowance,
        )
        return self._until_executed(resumed)

    async def _until_executed(
        self, source: AsyncGenerator[GraphEvent | RunOutcome]
    ) -> AsyncGenerator[GraphEvent | RunOutcome]:
        """``source``'s items; if the reader stops early, the decision is still carried out.

        The graph runs in its own task; a reader that goes away before the ``ApprovalExecution``
        waits for it (shielded), so a started cancel always completes and is recorded.
        """
        queue: asyncio.Queue[_Queued] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for item in source:
                    queue.put_nowait(item)
            except Exception as error:  # handed to the reader, which raises it
                queue.put_nowait(_Failed(error))
            finally:
                queue.put_nowait(_END)

        task = asyncio.create_task(pump(), name="chat-decision")
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _End):
                    return
                if isinstance(item, _Failed):
                    raise item.error
                yield item
        finally:
            if self._execution is None and not task.done():
                await asyncio.shield(self._drain_until_executed(queue))
            task.cancel()
            await asyncio.wait({task})

    async def _drain_until_executed(self, queue: asyncio.Queue[_Queued]) -> None:
        while True:
            item = await queue.get()
            if isinstance(item, ApprovalExecution):
                await self._executed(item)
                return
            if isinstance(item, _End | _Failed):
                return

    async def _executed(self, execution: ApprovalExecution) -> None:
        """Remember what the decision did and record its audit outcome once."""
        self._execution = execution
        await self._record_outcome(execution.outcome)

    async def _record_outcome(self, outcome: ApprovalOutcome) -> None:
        if self._approvals is None or self._decided is None:
            return
        try:
            await self._approvals.record_outcome(self._decided.approval.id, outcome)
        except Exception as error:
            _logger.error("chat_record_outcome_failed", error_type=type(error).__name__)

    async def _close(self, *, finished: bool, sent: str, started: float) -> None:
        try:
            if self._decided is not None and self._execution is None:
                # The graph never carried the decision out: it could not be confirmed.
                await self._record_outcome(ApprovalOutcome.FAILED)
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
        results = () if self._execution is None else (self._execution.result,)
        try:
            await self._graph.record_interrupted(self.conversation_id, sent, results)
        except Exception as error:
            _logger.error("chat_record_interrupted_failed", error_type=type(error).__name__)
        self._log(await self._store_answer(outcome), started)

    async def _store_proposal(self, outcome: RunOutcome) -> RunOutcome:
        """Store the approval, its audit row and the ``awaiting_approval`` message together."""
        proposal = outcome.proposal
        assert proposal is not None  # noqa: S101  # checked by the caller
        if self._approvals is None:
            _logger.error("chat_store_failed", error_type="NoApprovalStore")
            return _internal(outcome)
        approval = Approval(
            id=proposal.approval_id,
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            action=proposal.action,
            order_id=proposal.order_id,
            arguments=proposal.arguments,
            summary=proposal.summary,
            proposed_at=proposal.proposed_at,
            expires_at=proposal.expires_at,
        )
        message = ProposalMessage(
            message_id=self.message_id,
            content=outcome.text,
            prompt_version=self._graph.prompt_version,
            model_id=outcome.model_id,
            usage=outcome.usage,
        )
        try:
            await self._approvals.create_proposal(approval, message, self._request_id)
        except Exception as error:
            # The pause is orphaned; the next touch of the conversation closes it.
            _logger.error("chat_store_failed", error_type=type(error).__name__)
            return _internal(outcome)
        return outcome

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
    """Accepts customer messages and decisions; deletes conversations with their graph state."""

    def __init__(
        self,
        store: ConversationStore,
        graph: ChatGraph,
        clock: Clock,
        *,
        budget: TokenBudget | None = None,
        limits: ChatLimits | None = None,
        streams: ActiveStreams | None = None,
        approvals: ApprovalStore | None = None,
    ) -> None:
        """Store in ``store``, answer with ``graph``, stamp with ``clock``.

        ``budget`` is the daily token budget (``None``: no token limit), ``limits`` the
        per-conversation and per-message limits, ``streams`` the answers in progress,
        ``approvals`` the proposed actions (``None``: a proposal cannot be stored).
        """
        self._store = store
        self._graph = graph
        self._clock = clock
        self._budget = budget
        self._limits = limits or ChatLimits()
        self.streams = streams or ActiveStreams()
        self._approvals = approvals

    def _allowance(self) -> RunAllowance | None:
        if self._budget is None:
            return None
        return RunAllowance(self._budget, self._clock.now().date(), self._limits)

    async def start(
        self, user_id: str, conversation_id: UUID, content: str, request_id: str | None = None
    ) -> ChatRun | None:
        """Store the user message and return its run; ``None`` when the conversation is unknown.

        ``content`` is already trimmed and within limits (validated by the API). Raises
        ``ConversationFull``, ``StreamActive`` or ``ApprovalPending`` (nothing stored).
        """
        count = await self._store.count_messages(user_id, conversation_id)
        if count is None:
            return None
        if count + MESSAGES_PER_EXCHANGE > self._limits.max_messages:
            raise ConversationFull
        self.streams.acquire(conversation_id)
        try:
            if await self._settle_approvals(user_id, conversation_id) is not None:
                raise ApprovalPending
            await self._store.add_user_message(
                user_id, conversation_id, uuid4(), content, self._clock.now()
            )
        except BaseException:
            self.streams.release(conversation_id)
            raise
        return ChatRun(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            store=self._store,
            graph=self._graph,
            clock=self._clock,
            streams=self.streams,
            allowance=self._allowance(),
            approvals=self._approvals,
            request_id=request_id,
        )

    async def decide(
        self,
        user_id: str,
        conversation_id: UUID,
        approval_id: UUID,
        decision: Decision,
        request_id: str | None = None,
    ) -> ChatRun | None:
        """Record ``decision`` once and return the run that carries it out (FR-006, FR-007).

        ``None`` when the conversation or approval is unknown or another user's (404). Raises
        ``StreamActive``, ``ApprovalExpired`` (recorded ``expired``) or ``ApprovalAlreadyDecided``;
        then nothing is executed.
        """
        approvals = self._approvals
        if approvals is None or await approvals.get(user_id, conversation_id, approval_id) is None:
            return None
        self.streams.acquire(conversation_id)
        try:
            await self._settle_approvals(user_id, conversation_id)
            now = self._clock.now()
            decided = await approvals.decide(user_id, conversation_id, approval_id, decision, now)
            if decided is None:
                current = await approvals.get(user_id, conversation_id, approval_id)
                if current is not None and current.status is ApprovalStatus.EXPIRED:
                    raise ApprovalExpired
                raise ApprovalAlreadyDecided
        except BaseException:
            self.streams.release(conversation_id)
            raise
        return ChatRun(
            user_id=user_id,
            conversation_id=conversation_id,
            content=None,
            store=self._store,
            graph=self._graph,
            clock=self._clock,
            streams=self.streams,
            allowance=self._allowance(),
            approvals=approvals,
            request_id=request_id,
            decided=_Decided(decided, decision),
        )

    async def pending_approval(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's approval still open for a decision, after closing stale ones.

        While an answer is in progress nothing is closed (its proposal may not be stored yet).
        """
        if self._approvals is None:
            return None
        if self.streams.is_active(conversation_id):
            return await self._approvals.pending_for(user_id, conversation_id)
        return await self._settle_approvals(user_id, conversation_id)

    async def _settle_approvals(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """Close what can no longer be decided (research §6); return the live pending approval.

        Call only while no answer is in progress in the conversation.
        """
        approvals = self._approvals
        if approvals is None:
            return None
        stale = await approvals.awaiting_outcome(user_id, conversation_id)
        if stale is not None:  # decided, but the service stopped before recording the outcome
            await approvals.record_outcome(stale.id, ApprovalOutcome.FAILED)
            await self._graph.close_approval(conversation_id, ApprovalOutcome.FAILED)
        pending = await approvals.pending_for(user_id, conversation_id)
        if pending is None:
            # A pause whose approval was never stored (orphaned) is withdrawn.
            await self._graph.close_approval(conversation_id, ApprovalOutcome.WITHDRAWN)
            return None
        now = self._clock.now()
        if pending.is_expired(now):
            await self._expire(pending, now)
            return None
        return pending

    async def _expire(self, approval: Approval, now: datetime) -> None:
        approvals = self._approvals
        assert approvals is not None  # noqa: S101  # only called with a store
        if await approvals.close(approval.id, ApprovalStatus.EXPIRED, ApprovalOutcome.EXPIRED, now):
            await self._graph.close_approval(approval.conversation_id, ApprovalOutcome.EXPIRED)

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
