"""Ports the triage intake needs from storage, implemented in ``infrastructure/db``.

``TriageCaseStore.transaction()`` opens one unit of work per event: everything done through it
commits together when the block exits normally and rolls back when it raises (FR-004).
"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ecommerce_ai.agents.triage_intake.model import LockedCase, TriageCause, TriageEventInput


class TriageUnitOfWork(Protocol):
    """Operations on triage cases inside one transaction."""

    async def get_or_create_locked_case(
        self, order_id: UUID, cause: TriageCause, now: datetime
    ) -> LockedCase:
        """Return the order's case, created with ``cause`` if absent, locked until commit."""
        ...

    async def add_evidence(
        self, case_id: UUID, event: TriageEventInput, received_at: datetime
    ) -> bool:
        """Attach the event to the case; ``False`` when its ``eventId`` is already stored."""
        ...

    async def update_case(self, case_id: UUID, cause: TriageCause, updated_at: datetime) -> None:
        """Set the case's cause and ``updated_at``."""
        ...


class TriageCaseStore(Protocol):
    """Source of triage-case transactions."""

    def transaction(self) -> AbstractAsyncContextManager[TriageUnitOfWork]:
        """Open a transaction; commit on normal exit, roll back on an exception."""
        ...


class DeadLetterStore(Protocol):
    """Storage of inbound messages that could not be decoded or validated (FR-009)."""

    async def add(
        self,
        topic: str,
        partition: int,
        offset: int,
        key: bytes | None,
        raw: bytes,
        reason: str,
    ) -> None:
        """Save the raw message with a reason that holds no payload values."""
        ...
