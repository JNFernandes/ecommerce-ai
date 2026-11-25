"""In-memory stores with injectable failures: triage cases, dead letters, the triage queue,
the token budget, the outbox, the chat conversations, the approvals (with their audit rows), the
guard audit and the per-user usage (US-07).

Every store keeps unit-of-work semantics: changes made inside a transaction or claim become
visible only when the block exits normally and are discarded when it raises. Queue exceptions
in ``failures`` to make the next transactions (or writes) fail.
"""

import copy
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.model import (
    ApprovalOutcome,
    Conversation,
    ConversationHistory,
    Decision,
    FinishReason,
    MessageRole,
    StoredMessage,
    Usage,
)
from ecommerce_ai.agents.assistant.ports import (
    Approval,
    ApprovalStatus,
    ConversationPage,
    ProposalMessage,
)
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.agents.triage.model import CaseForTriage, TriageResult
from ecommerce_ai.agents.triage_intake.model import (
    LockedCase,
    TriageCause,
    TriageEventInput,
    TriageStatus,
)
from ecommerce_ai.infrastructure.outbox import OutboxMessage, OutboxStatus


@dataclass(frozen=True)
class StoredCase:
    """A triage case row."""

    case_id: UUID
    order_id: UUID
    cause: TriageCause
    status: TriageStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoredEvidence:
    """A triage evidence row."""

    case_id: UUID
    source_topic: str
    event_id: UUID
    reason: str | None
    occurred_at: datetime
    received_at: datetime
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class StoredDeadLetter:
    """An inbound dead-letter row."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    raw: bytes
    reason: str


@dataclass
class _Tables:
    cases: dict[UUID, StoredCase]
    evidence: dict[UUID, StoredEvidence]


class _UnitOfWork:
    """``TriageUnitOfWork`` over a private copy of the tables."""

    def __init__(self, tables: _Tables) -> None:
        self.tables = tables

    def _case(self, case_id: UUID) -> StoredCase:
        return next(case for case in self.tables.cases.values() if case.case_id == case_id)

    async def get_or_create_locked_case(
        self, order_id: UUID, cause: TriageCause, now: datetime
    ) -> LockedCase:
        case = self.tables.cases.get(order_id)
        if case is None:
            case = StoredCase(uuid4(), order_id, cause, TriageStatus.PENDING_TRIAGE, now, now)
            self.tables.cases[order_id] = case
        return LockedCase(case_id=case.case_id, cause=case.cause, status=case.status)

    async def add_evidence(
        self, case_id: UUID, event: TriageEventInput, received_at: datetime
    ) -> bool:
        if event.event_id in self.tables.evidence:
            return False
        self.tables.evidence[event.event_id] = StoredEvidence(
            case_id=case_id,
            source_topic=event.topic,
            event_id=event.event_id,
            reason=event.reason,
            occurred_at=event.occurred_at,
            received_at=received_at,
            payload=event.payload,
        )
        return True

    async def update_case(self, case_id: UUID, cause: TriageCause, updated_at: datetime) -> None:
        case = self._case(case_id)
        self.tables.cases[case.order_id] = replace(case, cause=cause, updated_at=updated_at)


class InMemoryTriageCaseStore:
    """``TriageCaseStore`` held in dictionaries."""

    def __init__(self) -> None:
        """Start empty, with no failure queued."""
        self._tables = _Tables(cases={}, evidence={})
        self.failures: deque[BaseException] = deque()
        self.commits = 0
        self.rollbacks = 0

    @property
    def cases(self) -> list[StoredCase]:
        """Committed cases."""
        return list(self._tables.cases.values())

    @property
    def evidence(self) -> list[StoredEvidence]:
        """Committed evidence."""
        return list(self._tables.evidence.values())

    def case_for(self, order_id: UUID) -> StoredCase:
        """The committed case of ``order_id``."""
        return self._tables.cases[order_id]

    def mark_completed(self, order_id: UUID) -> None:
        """Set the committed case of ``order_id`` to COMPLETED, as the triage worker does."""
        case = self._tables.cases[order_id]
        self._tables.cases[order_id] = replace(case, status=TriageStatus.COMPLETED)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_UnitOfWork]:
        """Raise the next queued failure, else run the block on a copy and keep it on success."""
        if self.failures:
            raise self.failures.popleft()
        work = _UnitOfWork(copy.deepcopy(self._tables))
        try:
            yield work
        except BaseException:
            self.rollbacks += 1
            raise
        self._tables = work.tables
        self.commits += 1


class InMemoryDeadLetterStore:
    """``DeadLetterStore`` held in a list."""

    def __init__(self) -> None:
        """Start empty, with no failure queued."""
        self.rows: list[StoredDeadLetter] = []
        self.failures: deque[BaseException] = deque()

    async def add(
        self,
        topic: str,
        partition: int,
        offset: int,
        key: bytes | None,
        raw: bytes,
        reason: str,
    ) -> None:
        """Raise the next queued failure, else keep the row."""
        if self.failures:
            raise self.failures.popleft()
        self.rows.append(StoredDeadLetter(topic, partition, offset, key, raw, reason))


type Serializer = Callable[[TriageResult, CaseForTriage], OutboxMessage]


@dataclass(frozen=True)
class QueuedCase:
    """A case in the in-memory triage queue."""

    case: CaseForTriage
    updated_at: datetime
    status: TriageStatus = TriageStatus.PENDING_TRIAGE


class _CaseClaim:
    """``CaseClaim`` of the in-memory queue; ``complete`` takes effect when the block commits."""

    def __init__(self, case: CaseForTriage | None, failures: deque[BaseException]) -> None:
        self._case = case
        self._failures = failures
        self.result: TriageResult | None = None

    @property
    def case(self) -> CaseForTriage | None:
        return self._case

    async def complete(self, result: TriageResult) -> None:
        if self._case is None:
            raise RuntimeError("no case was claimed")
        if self._failures:
            raise self._failures.popleft()
        self.result = result


class InMemoryTriageCaseQueue:
    """``TriageCaseQueue`` held in a dictionary, with ``SKIP LOCKED``-like claims.

    Each ``next_settled`` block claims the oldest pending, unlocked case last updated at or
    before ``settle_before``. A completed claim commits its result, the serialized outbox message
    (when a serializer is given) and the COMPLETED status together; a raised block rolls back.
    Queue exceptions in ``failures`` (raised on entering ``next_settled``) or in
    ``complete_failures`` (raised by ``claim.complete``).
    """

    def __init__(self, serializer: Serializer | None = None) -> None:
        """Start empty, with no failure queued."""
        self._serializer = serializer
        self._cases: dict[UUID, QueuedCase] = {}
        self._locked: set[UUID] = set()
        self.results: dict[UUID, TriageResult] = {}
        self.outbox: list[OutboxMessage] = []
        self.failures: deque[BaseException] = deque()
        self.complete_failures: deque[BaseException] = deque()
        self.commits = 0
        self.rollbacks = 0

    def add(self, case: CaseForTriage, updated_at: datetime) -> None:
        """Add (or replace) a pending case last updated at ``updated_at``."""
        self._cases[case.case_id] = QueuedCase(case=case, updated_at=updated_at)

    def status(self, case_id: UUID) -> TriageStatus:
        """Committed status of a case."""
        return self._cases[case_id].status

    @asynccontextmanager
    async def next_settled(self, settle_before: datetime) -> AsyncIterator[_CaseClaim]:
        """Claim the oldest settled case (or none); commit on normal exit, roll back on raise."""
        if self.failures:
            raise self.failures.popleft()
        settled = sorted(
            (
                queued
                for queued in self._cases.values()
                if queued.status is TriageStatus.PENDING_TRIAGE
                and queued.updated_at <= settle_before
                and queued.case.case_id not in self._locked
            ),
            key=lambda queued: queued.updated_at,
        )
        case = settled[0].case if settled else None
        if case is not None:
            self._locked.add(case.case_id)
        claim = _CaseClaim(case, self.complete_failures)
        try:
            yield claim
        except BaseException:
            self.rollbacks += 1
            raise
        finally:
            if case is not None:
                self._locked.discard(case.case_id)
        if case is not None and claim.result is not None:
            self.results[case.case_id] = claim.result
            if self._serializer is not None:
                self.outbox.append(self._serializer(claim.result, case))
            self._cases[case.case_id] = replace(
                self._cases[case.case_id], status=TriageStatus.COMPLETED
            )
        self.commits += 1


@dataclass(frozen=True)
class BudgetCall:
    """One ``reserve`` or ``settle`` call on the in-memory budget."""

    day: date
    reserved: int
    actual: int | None = None
    granted: bool | None = None


class InMemoryTokenBudget:
    """``TokenBudget`` of ``budget`` tokens per day, recording every call.

    Queue exceptions in ``failures`` to make the next ``reserve`` calls raise.
    """

    def __init__(self, budget: int) -> None:
        """Start with no tokens used."""
        self.budget = budget
        self.usage: dict[date, int] = {}
        self.reserves: list[BudgetCall] = []
        self.settles: list[BudgetCall] = []
        self.failures: deque[BaseException] = deque()

    async def reserve(self, day: date, cap: int) -> bool:
        """Add ``cap`` to the day's usage when it fits in the budget."""
        if self.failures:
            raise self.failures.popleft()
        used = self.usage.get(day, 0)
        granted = used + cap <= self.budget
        if granted:
            self.usage[day] = used + cap
        self.reserves.append(BudgetCall(day=day, reserved=cap, granted=granted))
        return granted

    async def settle(self, day: date, reserved: int, actual: int) -> None:
        """Replace the reservation with the actual spend."""
        self.usage[day] = self.usage.get(day, 0) - reserved + actual
        self.settles.append(BudgetCall(day=day, reserved=reserved, actual=actual))


@dataclass(frozen=True)
class StoredOutboxEvent:
    """An ``outbox_events`` row."""

    message: OutboxMessage
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None = None
    updated_at: datetime | None = None


class _OutboxClaim:
    """One claimed due outbox event; the chosen outcome takes effect when the block commits."""

    def __init__(self, event: StoredOutboxEvent | None) -> None:
        self._event = event
        self.outcome: StoredOutboxEvent | None = None

    @property
    def event(self) -> StoredOutboxEvent | None:
        return self._event

    def _require(self) -> StoredOutboxEvent:
        if self._event is None:
            raise RuntimeError("no outbox event was claimed")
        return self._event

    async def resolve(self, now: datetime) -> None:
        event = self._require()
        self.outcome = replace(
            event,
            status=OutboxStatus.RESOLVED,
            attempts=event.attempts + 1,
            last_error=None,
            updated_at=now,
        )

    async def fail(self, error: str, next_attempt_at: datetime, now: datetime) -> None:
        event = self._require()
        self.outcome = replace(
            event,
            attempts=event.attempts + 1,
            next_attempt_at=next_attempt_at,
            last_error=error,
            updated_at=now,
        )

    async def exhaust(self, error: str, now: datetime) -> None:
        event = self._require()
        self.outcome = replace(
            event,
            status=OutboxStatus.EXHAUSTED,
            attempts=event.attempts + 1,
            last_error=error,
            updated_at=now,
        )


class InMemoryOutboxStore:
    """Outbox held in a dictionary, with ``SKIP LOCKED``-like claims of due PENDING events.

    Queue exceptions in ``failures`` to make the next ``claim_due`` calls raise.
    """

    def __init__(self) -> None:
        """Start empty, with no failure queued."""
        self._events: dict[UUID, StoredOutboxEvent] = {}
        self._locked: set[UUID] = set()
        self.failures: deque[BaseException] = deque()
        self.commits = 0
        self.rollbacks = 0

    @property
    def events(self) -> list[StoredOutboxEvent]:
        """Committed events, in insertion order."""
        return list(self._events.values())

    def event(self, event_id: UUID) -> StoredOutboxEvent:
        """The committed event with ``event_id``."""
        return self._events[event_id]

    def add(self, message: OutboxMessage, now: datetime) -> None:
        """Store a PENDING event due at ``now``."""
        self._events[message.event_id] = StoredOutboxEvent(
            message=message, status=OutboxStatus.PENDING, attempts=0, next_attempt_at=now
        )

    @asynccontextmanager
    async def claim_due(self, now: datetime) -> AsyncIterator[_OutboxClaim]:
        """Claim the earliest due PENDING event (or none); commit on exit, roll back on raise."""
        if self.failures:
            raise self.failures.popleft()
        due = sorted(
            (
                event
                for event in self._events.values()
                if event.status is OutboxStatus.PENDING
                and event.next_attempt_at <= now
                and event.message.event_id not in self._locked
            ),
            key=lambda event: event.next_attempt_at,
        )
        event = due[0] if due else None
        if event is not None:
            self._locked.add(event.message.event_id)
        claim = _OutboxClaim(event)
        try:
            yield claim
        except BaseException:
            self.rollbacks += 1
            raise
        finally:
            if event is not None:
                self._locked.discard(event.message.event_id)
        if claim.outcome is not None:
            self._events[claim.outcome.message.event_id] = claim.outcome
        self.commits += 1


class InMemoryConversationStore:
    """A ``ConversationStore`` in memory, scoped by user like the repository.

    Queue exceptions in ``failures[method]`` (e.g. ``"add_user_message"``) to make the next
    calls of that method raise; nothing is changed by a failing call.
    """

    def __init__(self, tool_allowlist: Sequence[str] | None = None) -> None:
        """Start without conversations; new ones get ``tool_allowlist`` (``None``: none)."""
        self.tool_allowlist = None if tool_allowlist is None else tuple(tool_allowlist)
        self.conversations: dict[UUID, tuple[str, Conversation]] = {}
        self.messages: dict[UUID, list[StoredMessage]] = {}
        self.failures: dict[str, deque[BaseException]] = {}

    def _fail(self, method: str) -> None:
        queued = self.failures.get(method)
        if queued:
            raise queued.popleft()

    def _owned(self, user_id: str, conversation_id: UUID) -> Conversation | None:
        entry = self.conversations.get(conversation_id)
        return entry[1] if entry is not None and entry[0] == user_id else None

    def _touch(self, conversation_id: UUID, now: datetime) -> None:
        user_id, conversation = self.conversations[conversation_id]
        self.conversations[conversation_id] = (
            user_id,
            conversation.model_copy(update={"updated_at": now}),
        )

    async def create(
        self, user_id: str, customer_id: UUID | None, title: str | None, now: datetime
    ) -> Conversation:
        """Create an empty conversation."""
        self._fail("create")
        conversation = Conversation(
            id=uuid4(),
            title=title,
            created_at=now,
            updated_at=now,
            tool_allowlist=self.tool_allowlist,
        )
        self.conversations[conversation.id] = (user_id, conversation)
        self.messages[conversation.id] = []
        return conversation

    async def list(self, user_id: str, limit: int, offset: int) -> ConversationPage:
        """The user's conversations, newest ``updated_at`` first."""
        self._fail("list")
        owned = sorted(
            (c for owner, c in self.conversations.values() if owner == user_id),
            key=lambda c: (c.updated_at, c.id),
            reverse=True,
        )
        return ConversationPage(items=tuple(owned[offset : offset + limit]), total=len(owned))

    async def get(self, user_id: str, conversation_id: UUID) -> ConversationHistory | None:
        """The conversation and its messages, or ``None``."""
        self._fail("get")
        conversation = self._owned(user_id, conversation_id)
        if conversation is None:
            return None
        return ConversationHistory(
            conversation=conversation, messages=tuple(self.messages[conversation_id])
        )

    async def delete(self, user_id: str, conversation_id: UUID) -> bool:
        """Delete the conversation and its messages."""
        self._fail("delete")
        if self._owned(user_id, conversation_id) is None:
            return False
        del self.conversations[conversation_id]
        del self.messages[conversation_id]
        return True

    async def find(self, user_id: str, conversation_id: UUID) -> Conversation | None:
        """The conversation without its messages, or ``None``."""
        self._fail("find")
        return self._owned(user_id, conversation_id)

    async def count_messages(self, user_id: str, conversation_id: UUID) -> int | None:
        """Number of messages, or ``None`` when unknown."""
        self._fail("count_messages")
        if self._owned(user_id, conversation_id) is None:
            return None
        return len(self.messages[conversation_id])

    async def add_user_message(
        self, user_id: str, conversation_id: UUID, message_id: UUID, content: str, now: datetime
    ) -> StoredMessage:
        """Store a user message and bump ``updated_at``."""
        self._fail("add_user_message")
        if self._owned(user_id, conversation_id) is None:
            raise LookupError("unknown conversation")
        message = StoredMessage(
            id=message_id, role=MessageRole.USER, content=content, created_at=now
        )
        self.messages[conversation_id].append(message)
        self._touch(conversation_id, now)
        return message

    async def add_assistant_message(
        self,
        user_id: str,
        conversation_id: UUID,
        message_id: UUID,
        content: str,
        finish_reason: FinishReason,
        prompt_version: str,
        model_id: str | None,
        usage: Usage,
        now: datetime,
    ) -> StoredMessage:
        """Store an assistant message and bump ``updated_at``."""
        self._fail("add_assistant_message")
        if self._owned(user_id, conversation_id) is None:
            raise LookupError("unknown conversation")
        message = StoredMessage(
            id=message_id,
            role=MessageRole.ASSISTANT,
            content=content,
            created_at=now,
            finish_reason=finish_reason,
            prompt_version=prompt_version,
            model_id=model_id,
            usage=usage,
        )
        self.messages[conversation_id].append(message)
        self._touch(conversation_id, now)
        return message


@dataclass
class StoredAction:
    """One audit row (``chat_actions``) of the in-memory approval store."""

    approval_id: UUID
    conversation_id: UUID
    user_id: str
    action: str
    order_id: UUID
    request_id: str | None
    proposed_at: datetime
    decision: Decision | None = None
    outcome: ApprovalOutcome | None = None
    decided_at: datetime | None = None


_DECIDED: dict[Decision, ApprovalStatus] = {
    Decision.APPROVE: ApprovalStatus.APPROVED,
    Decision.REJECT: ApprovalStatus.REJECTED,
}
_CLOSING: frozenset[ApprovalStatus] = frozenset({ApprovalStatus.EXPIRED, ApprovalStatus.WITHDRAWN})


class InMemoryApprovalStore:
    """An ``ApprovalStore`` in memory, with the repository's semantics.

    Approvals live and die with their conversation in ``conversations`` (cascade); audit rows in
    ``actions`` are kept forever and their decision and outcome are set once. Transitions happen
    only from ``pending``, so a second decision changes nothing. Queue exceptions in
    ``failures[method]`` (e.g. ``"decide"``) to make the next calls of that method raise; nothing
    is changed by a failing call.
    """

    def __init__(self, conversations: InMemoryConversationStore) -> None:
        """Share ``conversations``: proposals store their assistant message there."""
        self._conversations = conversations
        self._approvals: dict[UUID, Approval] = {}
        self.actions: dict[UUID, StoredAction] = {}
        self.failures: dict[str, deque[BaseException]] = {}

    def _fail(self, method: str) -> None:
        queued = self.failures.get(method)
        if queued:
            raise queued.popleft()

    @property
    def approvals(self) -> list[Approval]:
        """Every approval whose conversation still exists, in creation order."""
        return [a for a in self._approvals.values() if self._alive(a)]

    def _alive(self, approval: Approval) -> bool:
        return approval.conversation_id in self._conversations.conversations

    def _owned(self, user_id: str, conversation_id: UUID) -> list[Approval]:
        return [
            a
            for a in self.approvals
            if a.user_id == user_id and a.conversation_id == conversation_id
        ]

    async def create_proposal(
        self, approval: Approval, message: ProposalMessage, request_id: str | None
    ) -> None:
        """Store the approval, its audit row and the ``awaiting_approval`` message together."""
        self._fail("create_proposal")
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError("a proposal starts pending")
        if any(a.status is ApprovalStatus.PENDING for a in self._owned_any(approval)):
            raise ValueError("one pending approval per conversation")
        await self._conversations.add_assistant_message(
            approval.user_id,
            approval.conversation_id,
            message.message_id,
            message.content,
            FinishReason.AWAITING_APPROVAL,
            message.prompt_version,
            message.model_id,
            message.usage,
            approval.proposed_at,
        )
        self._approvals[approval.id] = approval
        self.actions[approval.id] = StoredAction(
            approval_id=approval.id,
            conversation_id=approval.conversation_id,
            user_id=approval.user_id,
            action=approval.action,
            order_id=approval.order_id,
            request_id=request_id,
            proposed_at=approval.proposed_at,
        )

    def _owned_any(self, approval: Approval) -> list[Approval]:
        return [a for a in self.approvals if a.conversation_id == approval.conversation_id]

    async def get(self, user_id: str, conversation_id: UUID, approval_id: UUID) -> Approval | None:
        """The approval in any status, or ``None`` (also for another user's)."""
        self._fail("get")
        return next((a for a in self._owned(user_id, conversation_id) if a.id == approval_id), None)

    async def pending_for(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's pending approval, or ``None``."""
        self._fail("pending_for")
        owned = self._owned(user_id, conversation_id)
        return next((a for a in owned if a.status is ApprovalStatus.PENDING), None)

    async def awaiting_outcome(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's decided approval without an audit outcome, or ``None``."""
        self._fail("awaiting_outcome")
        return next(
            (
                a
                for a in self._owned(user_id, conversation_id)
                if a.status in _DECIDED.values() and self.actions[a.id].outcome is None
            ),
            None,
        )

    async def decide(
        self,
        user_id: str,
        conversation_id: UUID,
        approval_id: UUID,
        decision: Decision,
        now: datetime,
    ) -> Approval | None:
        """Decide a pending, unexpired approval once; ``None`` when nothing changed."""
        self._fail("decide")
        approval = await self.get(user_id, conversation_id, approval_id)
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return None
        if approval.is_expired(now):
            return None
        decided = approval.model_copy(update={"status": _DECIDED[decision], "decided_at": now})
        self._approvals[approval_id] = decided
        action = self.actions[approval_id]
        action.decision = decision
        action.decided_at = now
        return decided

    async def close(
        self, approval_id: UUID, status: ApprovalStatus, outcome: ApprovalOutcome, now: datetime
    ) -> bool:
        """Expire or withdraw a pending approval; ``False`` when it was no longer pending."""
        self._fail("close")
        if status not in _CLOSING:
            raise ValueError("close only expires or withdraws")
        approval = self._approvals.get(approval_id)
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            return False
        self._approvals[approval_id] = approval.model_copy(
            update={"status": status, "decided_at": now}
        )
        action = self.actions[approval_id]
        action.outcome = outcome
        action.decided_at = now
        return True

    async def record_outcome(self, approval_id: UUID, outcome: ApprovalOutcome) -> None:
        """Set the audit outcome once; later calls change nothing."""
        self._fail("record_outcome")
        action = self.actions.get(approval_id)
        if action is not None and action.outcome is None:
            action.outcome = outcome

    async def proposals_since(self, user_id: str, since: datetime) -> int:
        """Audit rows of ``user_id`` proposed at or after ``since`` (kept after deletes)."""
        self._fail("proposals_since")
        return sum(
            1 for a in self.actions.values() if a.user_id == user_id and a.proposed_at >= since
        )


# Guardrails (US-07) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredGuardEvent:
    """One row of the in-memory guard audit."""

    user_id: str
    conversation_id: UUID | None
    request_id: str | None
    event: GuardEvent
    created_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryGuardrailEventStore:
    """A ``GuardrailEventStore`` in memory, append-only like the table.

    ``now`` stamps appended events. Queue exceptions in ``failures[method]`` (e.g. ``"append"``)
    to make the next calls of that method raise; a failing ``append`` stores nothing.
    """

    def __init__(self, now: Callable[[], datetime] = _utc_now) -> None:
        """Start without events."""
        self.now = now
        self.rows: list[StoredGuardEvent] = []
        self.failures: dict[str, deque[BaseException]] = {}

    def _fail(self, method: str) -> None:
        queued = self.failures.get(method)
        if queued:
            raise queued.popleft()

    @property
    def events(self) -> list[GuardEvent]:
        """Every stored event, oldest first."""
        return [row.event for row in self.rows]

    async def append(
        self,
        user_id: str,
        conversation_id: UUID | None,
        request_id: str | None,
        events: Sequence[GuardEvent],
    ) -> None:
        """Store ``events``, all stamped with the same ``now``."""
        self._fail("append")
        created_at = self.now()
        self.rows.extend(
            StoredGuardEvent(user_id, conversation_id, request_id, event, created_at)
            for event in events
        )

    async def count_blocks_since(
        self, user_id: str, since: datetime, min_severity: GuardSeverity
    ) -> int:
        """Input ``blocked`` events of ``user_id`` at or after ``since`` with ≥ ``min_severity``."""
        self._fail("count_blocks_since")
        return sum(
            1
            for row in self.rows
            if row.user_id == user_id
            and row.created_at >= since
            and row.event.stage is GuardStage.INPUT
            and row.event.action is GuardAction.BLOCKED
            and row.event.severity.rank >= min_severity.rank
        )

    async def latest_cooldown_at(self, user_id: str) -> datetime | None:
        """Time of the user's newest ``cooldown`` event, ``None`` when there is none."""
        self._fail("latest_cooldown_at")
        times = [
            row.created_at
            for row in self.rows
            if row.user_id == user_id and row.event.action is GuardAction.COOLDOWN
        ]
        return max(times, default=None)


@dataclass
class UserDayUsage:
    """Messages and tokens of one user on one UTC day."""

    messages: int = 0
    tokens: int = 0


class InMemoryUserUsageStore:
    """A ``UserUsageStore`` in memory with the repository's conditional reserve.

    Queue exceptions in ``failures[method]`` (e.g. ``"reserve"``) to make the next calls of that
    method raise; nothing is changed by a failing call. ``settles`` records every settle call.
    """

    def __init__(self) -> None:
        """Start with no usage."""
        self.usage: dict[tuple[str, date], UserDayUsage] = {}
        self.settles: list[tuple[str, date, int, int]] = []
        self.failures: dict[str, deque[BaseException]] = {}

    def _fail(self, method: str) -> None:
        queued = self.failures.get(method)
        if queued:
            raise queued.popleft()

    def of(self, user_id: str, day: date) -> UserDayUsage:
        """The user's usage on ``day`` (zero when untouched)."""
        return self.usage.get((user_id, day), UserDayUsage())

    async def accept_message(self, user_id: str, day: date) -> int:
        """Count one accepted message on ``day``; return the day's count including it."""
        self._fail("accept_message")
        usage = self.usage.setdefault((user_id, day), UserDayUsage())
        usage.messages += 1
        return usage.messages

    async def reserve(self, user_id: str, day: date, tokens: int, cap: int) -> bool:
        """Reserve ``tokens`` on ``day`` if the total stays ≤ ``cap``, else ``False``."""
        self._fail("reserve")
        usage = self.usage.get((user_id, day), UserDayUsage())
        if usage.tokens + tokens > cap:
            return False
        usage.tokens += tokens
        self.usage[(user_id, day)] = usage
        return True

    async def settle(self, user_id: str, day: date, reserved: int, actual: int) -> None:
        """Replace a reservation with the actual spend (never below 0)."""
        self._fail("settle")
        self.settles.append((user_id, day, reserved, actual))
        usage = self.usage.get((user_id, day))
        if usage is not None:
            usage.tokens = max(usage.tokens - reserved + actual, 0)
