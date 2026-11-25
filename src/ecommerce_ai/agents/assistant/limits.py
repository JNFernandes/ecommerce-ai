"""Limits of the chat (US5, research §11, §12): token allowance, stream lock, full conversation.

- ``RunAllowance``: the token allowance of one message. The first model call reserves the whole
  ``request_token_cap`` from the daily ``TokenBudget``; a call is admitted only while what the run
  spent plus one call's ``max_output_tokens`` fits in the cap. ``settle`` replaces the reservation
  with the actual spend, once, on every ending (also a disconnect).
  With a ``UserBudget`` (US-07), the user's day budget is reserved first and the global one
  second; if the global one refuses, the user reservation is settled back to 0. ``denied_scope``
  says which one refused (the ``scope`` of ``BUDGET_EXCEEDED``).
- ``ActiveStreams``: at most one answer per conversation, and per user, at a time (409
  ``STREAM_ACTIVE``). The slot is taken before the stream opens and released when the run ends;
  the running task is attached so a delete can cancel it and wait for it. In process only
  (single instance, spec assumption).
- ``ProposalQuota``: the cancellation proposals one user may still get in the rolling hour
  (``CANCEL_PROPOSALS_PER_HOUR``, FR-015), counted from the audit rows so it survives restarts.
- ``ConversationFull`` / ``StreamActive``: the 409s answered before any stream opens.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from ecommerce_ai.agents.assistant.model import Usage
from ecommerce_ai.agents.assistant.ports import ApprovalStore
from ecommerce_ai.agents.guardrails.events import UserUsageStore
from ecommerce_ai.agents.guardrails.quotas import QuotaScope
from ecommerce_ai.agents.ports import Clock, TokenBudget

DEFAULT_MAX_MESSAGES: Final = 100
DEFAULT_REQUEST_TOKEN_CAP: Final = 20_000
DEFAULT_MAX_OUTPUT_TOKENS: Final = 1024
MESSAGES_PER_EXCHANGE: Final = 2
DEFAULT_PROPOSALS_PER_HOUR: Final = 5
PROPOSAL_WINDOW: Final = timedelta(hours=1)
"""A post adds the user message and the assistant message."""


class ConversationFull(Exception):
    """The conversation cannot take another exchange (409 ``CONVERSATION_FULL``)."""


class StreamActive(Exception):
    """An answer is already streaming in the conversation (409 ``STREAM_ACTIVE``)."""


@dataclass(frozen=True, slots=True)
class ChatLimits:
    """Per-conversation and per-message limits (``CHAT_MAX_MESSAGES``, token caps)."""

    max_messages: int = DEFAULT_MAX_MESSAGES
    request_token_cap: int = DEFAULT_REQUEST_TOKEN_CAP
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


@dataclass(frozen=True, slots=True)
class UserBudget:
    """One user's day token budget (``USER_DAILY_TOKEN_BUDGET``) kept in ``store``."""

    store: UserUsageStore
    user_id: str
    daily_cap: int


class RunAllowance:
    """Token allowance of one message, reserved from the daily budget(s) on first use."""

    def __init__(
        self,
        budget: TokenBudget,
        day: date,
        limits: ChatLimits,
        user: UserBudget | None = None,
    ) -> None:
        """Reserve on ``day`` from ``user``'s budget, then ``budget``, within ``limits``."""
        self._budget = budget
        self._user = user
        self._day = day
        self._cap = limits.request_token_cap
        self._max_output = limits.max_output_tokens
        self._reserved = False
        self._settled = False
        self.usage = Usage()
        self.model_id: str | None = None
        self.denied_scope: QuotaScope | None = None

    async def admit(self) -> bool:
        """Whether one more model call may run; the first one reserves the cap."""
        if self.usage.total + self._max_output > self._cap:
            self.denied_scope = QuotaScope.USER
            return False
        if not self._reserved:
            user = self._user
            if user is not None and not await user.store.reserve(
                user.user_id, self._day, self._cap, user.daily_cap
            ):
                self.denied_scope = QuotaScope.USER
                return False
            try:
                admitted = await self._budget.reserve(self._day, self._cap)
            except BaseException:
                await self._release_user()
                raise
            if not admitted:
                await self._release_user()
                self.denied_scope = QuotaScope.GLOBAL
                return False
            self._reserved = True
        return True

    async def _release_user(self) -> None:
        if self._user is not None:
            await self._user.store.settle(self._user.user_id, self._day, self._cap, 0)

    def spend(self, usage: Usage, model_id: str | None = None) -> None:
        """Count the tokens one model call spent."""
        self.usage = self.usage + usage
        if model_id is not None:
            self.model_id = model_id

    async def settle(self) -> None:
        """Replace the reservation with the actual spend; later calls do nothing."""
        if not self._reserved or self._settled:
            return
        self._settled = True
        try:
            await self._budget.settle(self._day, self._cap, self.usage.total)
        finally:
            if self._user is not None:
                await self._user.store.settle(
                    self._user.user_id, self._day, self._cap, self.usage.total
                )


class ProposalQuota:
    """Whether a user may get one more cancellation proposal in the rolling hour (FR-015)."""

    def __init__(self, store: ApprovalStore, user_id: str, clock: Clock, per_hour: int) -> None:
        """Count ``user_id``'s audit rows in ``store``; at most ``per_hour`` per hour."""
        self._store = store
        self._user_id = user_id
        self._clock = clock
        self._per_hour = per_hour

    async def allows(self) -> bool:
        """True while fewer than ``per_hour`` proposals were made in the last hour."""
        since = self._clock.now() - PROPOSAL_WINDOW
        return await self._store.proposals_since(self._user_id, since) < self._per_hour


class ActiveStreams:
    """Conversations with an answer in progress, and the task running each one."""

    def __init__(self) -> None:
        """Start with no active stream."""
        self._runs: dict[UUID, asyncio.Task[object] | None] = {}
        self._users: dict[UUID, str] = {}

    def acquire(self, conversation_id: UUID, user_id: str | None = None) -> None:
        """Take the conversation's (and ``user_id``'s) slot; ``StreamActive`` when taken."""
        if conversation_id in self._runs or (
            user_id is not None and user_id in self._users.values()
        ):
            raise StreamActive
        self._runs[conversation_id] = None
        if user_id is not None:
            self._users[conversation_id] = user_id

    def is_active(self, conversation_id: UUID) -> bool:
        """Whether an answer is in progress in the conversation."""
        return conversation_id in self._runs

    def attach(self, conversation_id: UUID) -> None:
        """Record the current task as the one running the conversation's answer."""
        if conversation_id in self._runs:
            self._runs[conversation_id] = asyncio.current_task()

    def release(self, conversation_id: UUID) -> None:
        """Free the conversation's slot (and its user's)."""
        self._runs.pop(conversation_id, None)
        self._users.pop(conversation_id, None)

    async def cancel(self, conversation_id: UUID) -> None:
        """Cancel the conversation's running answer and wait until it has ended."""
        task = self._runs.get(conversation_id)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.wait({task})
