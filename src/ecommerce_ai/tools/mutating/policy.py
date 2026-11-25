"""Where a tool is served and what approval a mutating tool requires (data-model.md).

``Exposure`` says which entry points may offer a tool: ``MCP`` (``POST /mcp``) and/or ``CHAT``
(the assistant graph). ``ApprovalPolicy`` is the contract of a mutating tool: the argument that
names its target, how long a proposal stays approvable and how many proposals one user may get
per rolling hour. ``ToolRegistry.build`` refuses a mutating tool without a policy or exposed on
``MCP``.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Exposure(StrEnum):
    """An entry point that may offer a tool."""

    MCP = "mcp"
    CHAT = "chat"


ALL_EXPOSURES: Final = frozenset(Exposure)
"""Default exposure of a read-only tool: both ``/mcp`` and the chat."""


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Approval rules of one mutating tool (constitution III)."""

    target_argument: str
    """Input field naming the target the customer approves, e.g. ``orderId``."""
    ttl_seconds: int
    """Seconds a proposal stays approvable (``APPROVAL_TTL_SECONDS``)."""
    proposals_per_hour: int
    """Most proposals one user may get per rolling hour (``CANCEL_PROPOSALS_PER_HOUR``)."""
