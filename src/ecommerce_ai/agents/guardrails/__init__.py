"""Guardrails: deterministic, model-independent guards around the chat assistant (US-07).

Pure decisions only (no I/O): input normalisation and rules, tool-result sanitising, the tool
guard, canary and leak checks on answers, per-user quotas and cooldown. ``events.py`` holds the
audit event a guard emits and the ports (event store, user usage, rate window) the guards reach
the outside world through; ``infrastructure`` implements them.

The chat graph takes every guard in one ``Guardrails`` bundle (research §1). Each field is a
``Protocol`` with a pass-through ``NOOP`` implementation, so a test can switch off any single
guard with ``dataclasses.replace`` (SC-002) and ``Guardrails.noop()`` changes nothing at all.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, Self

from pydantic import JsonValue

from ecommerce_ai.agents.guardrails.events import GuardEvent

# Input ----------------------------------------------------------------------------------------


class InputGuard(Protocol):
    """Decides whether a customer's (normalised) message may reach the model."""

    def check(self, text: str) -> GuardEvent | None:
        """The ``blocked`` event of the first rule ``text`` breaks, ``None`` when it passes."""
        ...


class _NoInputGuard:
    def check(self, text: str) -> GuardEvent | None:
        """Let every message through."""
        return None


# Tool results ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SanitizedResult:
    """A tool result as the model may see it, with what the guard did to it."""

    payload: dict[str, JsonValue]
    events: tuple[GuardEvent, ...] = ()


class ToolResultGuard(Protocol):
    """Makes a tool result inert and bounded before the model sees it."""

    def sanitize(self, name: str, payload: dict[str, JsonValue]) -> SanitizedResult:
        """``payload`` of tool ``name``, sanitised, with a ``sanitized`` event per change."""
        ...


class _NoToolResultGuard:
    def sanitize(self, name: str, payload: dict[str, JsonValue]) -> SanitizedResult:
        """Return ``payload`` unchanged."""
        return SanitizedResult(payload=payload)


# Output ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutputContext:
    """What the output checks compare an answer with: this run's canary and what was seen."""

    canary: str | None = None
    seen_ids: frozenset[str] = frozenset()
    system_prompt: str = ""


class OutputChecks(Protocol):
    """Finds leaks (canary, secrets, internal names, foreign ids, prompt text) in answer text."""

    def check(self, text: str, context: OutputContext) -> tuple[GuardEvent, ...]:
        """One ``replaced`` event per rule ``text`` breaks; empty when it is clean."""
        ...


class _NoOutputChecks:
    def check(self, text: str, context: OutputContext) -> tuple[GuardEvent, ...]:
        """Find nothing."""
        return ()


# Tool calls -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool call is checked against in one message."""

    allowlist: frozenset[str] | None = None
    """Tools the conversation may call (``None``: every offered tool)."""
    seen_ids: frozenset[str] = frozenset()
    """Ids the customer typed or a tool returned in this conversation."""
    previous_calls: tuple[str, ...] = ()
    """Canonical keys (name + arguments) of the calls already made in this message."""


@dataclass(frozen=True, slots=True)
class ToolVerdict:
    """A refused tool call: the event to record, and whether the answer must end (a loop)."""

    event: GuardEvent
    ends_answer: bool = False


class ToolGuard(Protocol):
    """Keeps the model's tool calls within what the customer can see and ask for."""

    def check(
        self, name: str, arguments: Mapping[str, JsonValue], context: ToolContext
    ) -> ToolVerdict | None:
        """The verdict refusing the call, ``None`` when it may run."""
        ...


class _NoToolGuard:
    def check(
        self, name: str, arguments: Mapping[str, JsonValue], context: ToolContext
    ) -> ToolVerdict | None:
        """Allow every call."""
        return None


# Canary ---------------------------------------------------------------------------------------


class CanaryFactory(Protocol):
    """Makes the per-run canary rendered into the system prompt."""

    def __call__(self) -> str | None:
        """A fresh canary, or ``None`` for none."""
        ...


class _NoCanary:
    def __call__(self) -> str | None:
        """No canary."""
        return None


NOOP_INPUT_GUARD: Final[InputGuard] = _NoInputGuard()
"""Lets every message through."""
NOOP_TOOL_RESULT_GUARD: Final[ToolResultGuard] = _NoToolResultGuard()
"""Leaves every tool result unchanged."""
NOOP_OUTPUT_CHECKS: Final[OutputChecks] = _NoOutputChecks()
"""Finds nothing in any answer."""
NOOP_TOOL_GUARD: Final[ToolGuard] = _NoToolGuard()
"""Allows every tool call."""
NOOP_CANARY: Final[CanaryFactory] = _NoCanary()
"""Renders no canary."""


@dataclass(frozen=True, slots=True)
class Guardrails:
    """Every guard of the chat graph; each defaults to its pass-through ``NOOP``."""

    input_guard: InputGuard = field(default=NOOP_INPUT_GUARD)
    tool_result_guard: ToolResultGuard = field(default=NOOP_TOOL_RESULT_GUARD)
    output_checks: OutputChecks = field(default=NOOP_OUTPUT_CHECKS)
    tool_guard: ToolGuard = field(default=NOOP_TOOL_GUARD)
    canary_factory: CanaryFactory = field(default=NOOP_CANARY)

    @classmethod
    def noop(cls) -> Self:
        """The bundle that guards nothing (the behaviour before US-07)."""
        return cls()
