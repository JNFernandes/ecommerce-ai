"""In-memory stores with injectable failures: triage cases, dead letters, the triage queue,
the token budget and the outbox.

Every store keeps unit-of-work semantics: changes made inside a transaction or claim become
visible only when the block exits normally and are discarded when it raises. Queue exceptions
in ``failures`` to make the next transactions (or writes) fail.
"""

import copy
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

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
        return LockedCase(case_id=case.case_id, cause=case.cause)

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
            event, status=OutboxStatus.RESOLVED, attempts=event.attempts + 1, updated_at=now
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
