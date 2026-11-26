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

Deleting a conversation withdraws its pending approval first (``expired`` once past its expiry);
the audit row stays. One ``chat_approval`` log line is written per proposal and per decision (with
its outcome): ids, action, decision, outcome and the user hash, never text, totals or tokens.

When the run stops early (the client went away, or a delete cancelled it), a shielded
``finish_disconnected`` stores the text already sent as ``client_disconnected`` and puts it in
the graph's transcript (US6). On every ending the allowance is settled and the conversation's
slot released. One ``chat_run`` log line per run carries counts, timings, outcomes and a hash of
the user, never content, arguments, results or the token (FR-026).

Guard events (US-07, research §10): the graph publishes each ``GuardEvent`` as the guard acts;
the run collects them, and the shielded close logs one ``guardrail_event`` line per event and
appends them to the ``GuardrailEventStore``, also after a disconnect. ``chat_run`` carries their
counts per stage and action (``guards``). A failing append is logged and changes nothing else.

Per-user quotas (US-07 US5, research §9): the API checks cooldown and the minute window before
the body; ``start`` then counts the day limit (``QuotaExceeded``) before the conversation-full and
stream checks, and a user holds at most one stream across conversations. The allowance reserves
the user's day tokens before the global budget; ``BUDGET_EXCEEDED`` carries the refusing
``scope``. After the audit, ``UserQuotas.after_run`` may add a ``cooldown`` event. Quota refusals
come before any approval is touched, so a pending approval is never withdrawn by them.
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Iterable
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
    DEFAULT_PROPOSALS_PER_HOUR,
    MESSAGES_PER_EXCHANGE,
    ActiveStreams,
    ChatLimits,
    ConversationFull,
    ProposalQuota,
    RunAllowance,
    UserBudget,
)
from ecommerce_ai.agents.assistant.model import (
    AnswerReplaced,
    ApprovalExecution,
    ApprovalOutcome,
    ChatErrorCode,
    Conversation,
    Decision,
    FinishReason,
    MessageEnd,
    MessageReplaced,
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
from ecommerce_ai.agents.guardrails.events import GuardEvent, GuardrailEventStore, UserUsageStore
from ecommerce_ai.agents.guardrails.normalizer import normalize
from ecommerce_ai.agents.guardrails.quotas import UserQuotas
from ecommerce_ai.agents.ports import Clock, TokenBudget
from ecommerce_ai.infrastructure.errors import error_fields

USER_HASH_CHARS: Final = 16

_logger = structlog.get_logger(__name__)


def user_hash(subject: str) -> str:
    """A short SHA-256 prefix of the token subject, for logs (never the raw subject)."""
    return hashlib.sha256(subject.encode()).hexdigest()[:USER_HASH_CHARS]


def _log_approval(
    user_id: str, approval: Approval, decision: Decision | None, outcome: ApprovalOutcome | None
) -> None:
    """One ``chat_approval`` line (FR-017): a proposal, or a decision with its outcome."""
    _logger.info(
        "chat_approval",
        conversation_id=str(approval.conversation_id),
        approval_id=str(approval.id),
        action=approval.action,
        decision=None if decision is None else decision.value,
        outcome=None if outcome is None else outcome.value,
        user_hash=user_hash(user_id),
    )


def _guard_counts(events: Iterable[GuardEvent]) -> dict[str, dict[str, int]]:
    """How often each guard action happened per stage, e.g. ``{"input": {"blocked": 1}}``."""
    counts: dict[str, dict[str, int]] = {}
    for event in events:
        per_stage = counts.setdefault(event.stage.value, {})
        per_stage[event.action.value] = per_stage.get(event.action.value, 0) + 1
    return counts


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


def _allowlist(conversation: Conversation) -> frozenset[str] | None:
    """The tools ``conversation`` may call (US-07 FR-010), ``None`` when it has no allowlist."""
    allowlist = conversation.tool_allowlist
    return None if allowlist is None else frozenset(allowlist)


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
        quota: ProposalQuota | None = None,
        guard_store: GuardrailEventStore | None = None,
        tool_allowlist: frozenset[str] | None = None,
        quotas: UserQuotas | None = None,
    ) -> None:
        """Answer ``content``, or resume from ``decided``; the caller has taken the slot.

        The run's guard events go to ``guard_store`` (``None``: logged only); the model may call
        only the conversation's ``tool_allowlist`` (``None``: every offered tool); ``quotas``
        decides whether the run's blocks start a cooldown.
        """
        self._quotas = quotas
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
        self._quota = quota
        self._guard_store = guard_store
        self._tool_allowlist = tool_allowlist
        self._guard_events: list[GuardEvent] = []
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
                    if isinstance(item, GuardEvent):
                        self._guard_events.append(item)
                        continue
                    if isinstance(item, AnswerReplaced):
                        sent = [item.text]  # what the customer now sees
                        yield MessageReplaced(message_id=self.message_id, text=item.text)
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
                scope = None if self._allowance is None else self._allowance.denied_scope
                yield StreamError.for_code(outcome.error, scope)
            elif outcome.finish_reason is not None:
                if outcome.proposal is not None:
                    yield outcome.proposal.required_event()
                yield MessageEnd(
                    message_id=self.message_id,
                    finish_reason=outcome.finish_reason,
                    prompt_version=self._graph.prompt_version,
                    model_id=outcome.model_id,
                    usage=outcome.usage,
                    violations=outcome.violations,
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
            return self._graph.run(
                self.conversation_id,
                self.content or "",
                self._allowance,
                self._quota,
                tool_allowlist=self._tool_allowlist,
            )
        resumed = self._graph.resume(
            self.conversation_id,
            self._decided.approval.id,
            self._decided.decision,
            self._allowance,
            self._quota,
            tool_allowlist=self._tool_allowlist,
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
            if isinstance(item, GuardEvent):
                self._guard_events.append(item)
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
        approval = self._decided.approval
        _log_approval(self.user_id, approval, self._decided.decision, outcome)
        try:
            await self._approvals.record_outcome(approval.id, outcome)
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
            await self._audit()
        finally:
            self._streams.release(self.conversation_id)

    async def _audit(self) -> None:
        """Log and store the run's guard events (FR-019, FR-021); a failure changes nothing else.

        The guards have already acted: an audit that cannot be written is logged, never undone.
        """
        events = tuple(self._guard_events)
        if not await self._append(events) or self._quotas is None:
            return
        try:
            cooldown = await self._quotas.after_run(self.user_id, events)
        except Exception as error:
            _logger.error("guardrail_cooldown_failed", **error_fields(error))
            return
        if cooldown is not None:
            await self._append((cooldown,))

    async def _append(self, events: tuple[GuardEvent, ...]) -> bool:
        """Log and store ``events``; whether they were stored."""
        for event in events:
            _logger.info(
                "guardrail_event",
                rule=event.rule,
                stage=event.stage.value,
                action=event.action.value,
                severity=event.severity.value,
                user_hash=user_hash(self.user_id),
            )
        if not events or self._guard_store is None:
            return False
        try:
            await self._guard_store.append(
                self.user_id, self.conversation_id, self._request_id, events
            )
        except Exception as error:
            _logger.error("guardrail_audit_failed", **error_fields(error))
            return False
        return True

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
        _log_approval(self.user_id, approval, None, None)
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
            rules_version=self._graph.guardrails.input_guard.version,
            model_id=outcome.model_id,
            # Rule names only, never the text that broke them.
            violations=list(outcome.violations),
            guards=_guard_counts(self._guard_events),
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
        proposals_per_hour: int = DEFAULT_PROPOSALS_PER_HOUR,
        guard_store: GuardrailEventStore | None = None,
        quotas: UserQuotas | None = None,
        user_usage: UserUsageStore | None = None,
        user_daily_tokens: int | None = None,
    ) -> None:
        """Store in ``store``, answer with ``graph``, stamp with ``clock``.

        ``budget`` is the daily token budget (``None``: no token limit), ``limits`` the
        per-conversation and per-message limits, ``streams`` the answers in progress,
        ``approvals`` the proposed actions (``None``: a proposal cannot be stored), of which one
        user gets at most ``proposals_per_hour`` (FR-015). ``guard_store`` keeps the guard audit
        (``None``: guard events are only logged). ``quotas`` holds the day limit and cooldown;
        ``user_usage`` with ``user_daily_tokens`` the per-user token budget (``None``: off).
        """
        self._quotas = quotas
        self._user_usage = user_usage
        self._user_daily_tokens = user_daily_tokens
        self._store = store
        self._graph = graph
        self._clock = clock
        self._budget = budget
        self._limits = limits or ChatLimits()
        self.streams = streams or ActiveStreams()
        self._approvals = approvals
        self._proposals_per_hour = proposals_per_hour
        self._guard_store = guard_store

    def _quota(self, user_id: str) -> ProposalQuota | None:
        if self._approvals is None:
            return None
        return ProposalQuota(self._approvals, user_id, self._clock, self._proposals_per_hour)

    def _allowance(self, user_id: str) -> RunAllowance | None:
        if self._budget is None:
            return None
        user = None
        if self._user_usage is not None and self._user_daily_tokens is not None:
            user = UserBudget(self._user_usage, user_id, self._user_daily_tokens)
        return RunAllowance(self._budget, self._clock.now().date(), self._limits, user)

    async def start(
        self, user_id: str, conversation_id: UUID, content: str, request_id: str | None = None
    ) -> ChatRun | None:
        """Store the user message and return its run; ``None`` when the conversation is unknown.

        ``content`` is already trimmed, within limits and not empty once normalised (validated by
        the API); it is stored and answered in its normalised form (FR-001). Raises
        ``QuotaExceeded`` (day limit), ``ConversationFull``, ``StreamActive`` or
        ``ApprovalPending`` (nothing stored).
        """
        content = normalize(content).strip()
        conversation = await self._store.find(user_id, conversation_id)
        count = await self._store.count_messages(user_id, conversation_id)
        if conversation is None or count is None:
            return None
        if self._quotas is not None:
            await self._quotas.before_message(user_id)
        if count + MESSAGES_PER_EXCHANGE > self._limits.max_messages:
            raise ConversationFull
        self.streams.acquire(conversation_id, user_id)
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
            allowance=self._allowance(user_id),
            approvals=self._approvals,
            request_id=request_id,
            quota=self._quota(user_id),
            guard_store=self._guard_store,
            tool_allowlist=_allowlist(conversation),
            quotas=self._quotas,
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
        conversation = await self._store.find(user_id, conversation_id)
        if conversation is None:
            return None
        self.streams.acquire(conversation_id, user_id)
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
            allowance=self._allowance(user_id),
            approvals=approvals,
            request_id=request_id,
            decided=_Decided(decided, decision),
            quota=self._quota(user_id),
            guard_store=self._guard_store,
            tool_allowlist=_allowlist(conversation),
            quotas=self._quotas,
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

    async def _withdraw(self, user_id: str, conversation_id: UUID) -> None:
        """Close what the conversation still has open before it is deleted (filtered by user)."""
        approvals = self._approvals
        if approvals is None:
            return
        stale = await approvals.awaiting_outcome(user_id, conversation_id)
        if stale is not None:
            await approvals.record_outcome(stale.id, ApprovalOutcome.FAILED)
        pending = await approvals.pending_for(user_id, conversation_id)
        if pending is None:
            return
        now = self._clock.now()
        if pending.is_expired(now):
            await approvals.close(pending.id, ApprovalStatus.EXPIRED, ApprovalOutcome.EXPIRED, now)
        else:
            await approvals.close(
                pending.id, ApprovalStatus.WITHDRAWN, ApprovalOutcome.WITHDRAWN, now
            )

    async def _expire(self, approval: Approval, now: datetime) -> None:
        approvals = self._approvals
        assert approvals is not None  # noqa: S101  # only called with a store
        if await approvals.close(approval.id, ApprovalStatus.EXPIRED, ApprovalOutcome.EXPIRED, now):
            await self._graph.close_approval(approval.conversation_id, ApprovalOutcome.EXPIRED)

    async def delete(self, user_id: str, conversation_id: UUID) -> bool:
        """Delete the conversation, its messages and its graph state; ``False`` when unknown.

        An answer in progress is cancelled and awaited first (US6); a pending approval is then
        withdrawn (``expired`` once past its expiry), its audit row kept (FR-016).
        """
        if self.streams.is_active(conversation_id):
            # Only the owner may cancel: a foreign id stays indistinguishable from unknown.
            if await self._store.count_messages(user_id, conversation_id) is None:
                return False
            await self.streams.cancel(conversation_id)
        await self._withdraw(user_id, conversation_id)
        if not await self._store.delete(user_id, conversation_id):
            return False
        await self._graph.forget(conversation_id)
        return True
