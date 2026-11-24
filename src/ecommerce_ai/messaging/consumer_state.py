"""Live state of the triage consumer, reported by ``GET /ready`` (research 13).

``last_error`` holds the class name of the root cause only (``InvalidPasswordError``, not
SQLAlchemy's generic ``DBAPIError``), never its message, so no connection detail or
secret can leak through the readiness endpoint.
"""

from dataclasses import dataclass

from ecommerce_ai.infrastructure.errors import error_name


@dataclass(slots=True)
class ConsumerState:
    """Whether the consumer is connected and caught up, and its last failure."""

    connected: bool = False
    caught_up: bool = False
    last_error: str | None = None

    def mark_connected(self) -> None:
        """The consumer started and joined its group."""
        self.connected = True
        self.last_error = None

    def mark_failed(self, error: BaseException) -> None:
        """The consumer lost its connection or stopped on ``error``."""
        self.connected = False
        self.caught_up = False
        self.last_error = error_name(error)

    def mark_stopped(self) -> None:
        """The consumer stopped on request."""
        self.connected = False
        self.caught_up = False

    def update_lag(self, lag: int) -> None:
        """Record the records left to read over the assignment; zero means caught up."""
        self.caught_up = lag == 0
