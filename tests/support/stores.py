"""In-memory triage case and dead-letter stores with injectable failures.

``InMemoryTriageCaseStore`` keeps unit-of-work semantics: changes made inside ``transaction()``
become visible only when the block exits normally and are discarded when it raises. Queue
exceptions in ``failures`` to make the next transactions (or dead-letter writes) fail.
"""

import copy
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from ecommerce_ai.agents.triage_intake.model import (
    LockedCase,
    TriageCause,
    TriageEventInput,
    TriageStatus,
)


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
