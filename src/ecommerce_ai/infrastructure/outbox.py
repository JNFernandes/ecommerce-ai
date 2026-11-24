"""Value types of the transactional outbox (``outbox_events``), shared by storage and messaging.

An outbox message is saved in the same transaction as the result it announces, then published
by the outbox relay; its ``event_id``, key and payload never change between attempts (FR-017).
"""

from enum import StrEnum


class OutboxStatus(StrEnum):
    """Lifecycle of one outbox event."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    EXHAUSTED = "EXHAUSTED"
