"""PostgreSQL implementation of the ``ApprovalStore`` port (US-06, data-model.md).

``chat_approvals`` holds the proposed call (exact arguments, summary, status) and dies with its
conversation; ``chat_actions`` is the append-only audit, one row per proposal, whose decision and
outcome are set once (a trigger enforces it). Every read filters ``user_id`` and the
conversation, so another user's approval behaves like an unknown one. State changes are
conditional ``UPDATE … WHERE status = 'pending'`` statements: concurrent decisions on the same
approval yield exactly one winner (FR-007, SC-002).
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ecommerce_ai.agents.assistant.model import ApprovalOutcome, Decision, FinishReason
from ecommerce_ai.agents.assistant.ports import Approval, ApprovalStatus, ProposalMessage
from ecommerce_ai.infrastructure.db.conversation_repository import (
    assistant_values,
    insert_message,
)
from ecommerce_ai.infrastructure.db.tables import chat_actions, chat_approvals

_approvals = chat_approvals.c
_actions = chat_actions.c

_DECIDED: dict[Decision, ApprovalStatus] = {
    Decision.APPROVE: ApprovalStatus.APPROVED,
    Decision.REJECT: ApprovalStatus.REJECTED,
}
_CLOSING = frozenset({ApprovalStatus.EXPIRED, ApprovalStatus.WITHDRAWN})


def _approval(row: Mapping[str, object]) -> Approval:
    """An ``Approval`` from a ``chat_approvals`` row joined with its audit ``order_id``."""
    return Approval.model_validate(dict(row))


_APPROVAL_COLUMNS = (*chat_approvals.c, _actions.order_id)


def _owned(user_id: str, conversation_id: UUID) -> tuple[ColumnElement[bool], ...]:
    return (_approvals.user_id == user_id, _approvals.conversation_id == conversation_id)


class ApprovalRepository:
    """``ApprovalStore`` over the async engine; one transaction per call."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Read and write on ``engine``."""
        self._engine = engine

    async def create_proposal(
        self, approval: Approval, message: ProposalMessage, request_id: str | None
    ) -> None:
        """Store the approval, its audit row and the ``awaiting_approval`` message atomically.

        ``LookupError`` when the user has no such conversation; an ``IntegrityError`` when the
        conversation already has a pending approval. Nothing is stored on failure.
        """
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError("a proposal starts pending")
        proposed_at = approval.proposed_at.astimezone(UTC)
        values = assistant_values(
            message.content,
            FinishReason.AWAITING_APPROVAL,
            message.prompt_version,
            message.model_id,
            message.usage,
        )
        async with self._engine.begin() as connection:
            await insert_message(
                connection,
                approval.user_id,
                approval.conversation_id,
                message.message_id,
                values,
                proposed_at,
            )
            await connection.execute(
                insert(chat_approvals).values(
                    id=approval.id,
                    conversation_id=approval.conversation_id,
                    user_id=approval.user_id,
                    action=approval.action,
                    arguments=approval.arguments,
                    summary=approval.summary.model_dump(mode="json", by_alias=True),
                    status=approval.status.value,
                    proposed_at=proposed_at,
                    expires_at=approval.expires_at.astimezone(UTC),
                    decided_at=None,
                )
            )
            await connection.execute(
                insert(chat_actions).values(
                    id=uuid4(),
                    approval_id=approval.id,
                    conversation_id=approval.conversation_id,
                    user_id=approval.user_id,
                    action=approval.action,
                    order_id=approval.order_id,
                    request_id=request_id,
                    proposed_at=proposed_at,
                )
            )

    async def _select_one(self, *conditions: ColumnElement[bool]) -> Approval | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(*_APPROVAL_COLUMNS)
                    .join(chat_actions, _actions.approval_id == _approvals.id)
                    .where(*conditions)
                    .order_by(_approvals.proposed_at.desc())
                    .limit(1)
                )
            ).one_or_none()
        return None if row is None else _approval(dict(row._mapping))

    async def get(self, user_id: str, conversation_id: UUID, approval_id: UUID) -> Approval | None:
        """The approval in any status, or ``None`` (also for another user's)."""
        return await self._select_one(
            *_owned(user_id, conversation_id), _approvals.id == approval_id
        )

    async def pending_for(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's pending approval (expired or not), or ``None``."""
        return await self._select_one(
            *_owned(user_id, conversation_id), _approvals.status == ApprovalStatus.PENDING.value
        )

    async def awaiting_outcome(self, user_id: str, conversation_id: UUID) -> Approval | None:
        """The conversation's decided approval whose audit outcome is unset, or ``None``."""
        return await self._select_one(
            *_owned(user_id, conversation_id),
            _approvals.status.in_([status.value for status in _DECIDED.values()]),
            _actions.outcome.is_(None),
        )

    async def decide(
        self,
        user_id: str,
        conversation_id: UUID,
        approval_id: UUID,
        decision: Decision,
        now: datetime,
    ) -> Approval | None:
        """Decide a pending, unexpired approval once and audit the decision; else ``None``."""
        stamp = now.astimezone(UTC)
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    update(chat_approvals)
                    .where(
                        _approvals.id == approval_id,
                        _approvals.conversation_id == conversation_id,
                        _approvals.user_id == user_id,
                        _approvals.status == ApprovalStatus.PENDING.value,
                        _approvals.expires_at > stamp,
                    )
                    .values(status=_DECIDED[decision].value, decided_at=stamp)
                    .returning(*chat_approvals.c)
                )
            ).one_or_none()
            if row is None:
                return None
            order_id = (
                await connection.execute(
                    update(chat_actions)
                    .where(_actions.approval_id == approval_id)
                    .values(decision=decision.value, decided_at=stamp)
                    .returning(_actions.order_id)
                )
            ).scalar_one()
        return _approval({**row._mapping, "order_id": order_id})

    async def close(
        self, approval_id: UUID, status: ApprovalStatus, outcome: ApprovalOutcome, now: datetime
    ) -> bool:
        """Expire or withdraw a pending approval with that audit outcome; ``False`` otherwise."""
        if status not in _CLOSING:
            raise ValueError("close only expires or withdraws")
        stamp = now.astimezone(UTC)
        async with self._engine.begin() as connection:
            closed = (
                await connection.execute(
                    update(chat_approvals)
                    .where(
                        _approvals.id == approval_id,
                        _approvals.status == ApprovalStatus.PENDING.value,
                    )
                    .values(status=status.value, decided_at=stamp)
                    .returning(_approvals.id)
                )
            ).one_or_none()
            if closed is None:
                return False
            await connection.execute(
                update(chat_actions)
                .where(_actions.approval_id == approval_id)
                .values(outcome=outcome.value, decided_at=stamp)
            )
        return True

    async def record_outcome(self, approval_id: UUID, outcome: ApprovalOutcome) -> None:
        """Set the audit outcome once; later calls change nothing."""
        async with self._engine.begin() as connection:
            await connection.execute(
                update(chat_actions)
                .where(and_(_actions.approval_id == approval_id, _actions.outcome.is_(None)))
                .values(outcome=outcome.value)
            )

    async def proposals_since(self, user_id: str, since: datetime) -> int:
        """Audit rows of ``user_id`` proposed at or after ``since`` (kept after deletes)."""
        async with self._engine.connect() as connection:
            count: int = (
                await connection.execute(
                    select(func.count())
                    .select_from(chat_actions)
                    .where(_actions.user_id == user_id, _actions.proposed_at >= since)
                )
            ).scalar_one()
        return count
