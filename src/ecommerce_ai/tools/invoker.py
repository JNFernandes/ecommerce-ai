"""In-process tool invocation shared by the MCP server and the chat assistant (research §7).

``run_definition`` validates the arguments with the tool's input model and runs its handler under
a per-tool ``asyncio.timeout``; ``failure_result`` maps any failure to the fixed ``{error,
message}`` result once, for both callers. ``ToolInvoker.invoke`` never raises: an unknown or
non-allowlisted name and arguments that fail validation are *refused* (the handler never runs), a
``ToolFailure`` becomes its error result, and an upstream 401 is reported through
``upstream_unauthenticated``. A mutating tool is refused by ``invoke`` and runs only through
``invoke_approved``, once, after the customer's approval (US-06, constitution III). The caller
must be bound (``bind_caller``) so handlers forward the caller's own token (ADR 0001). One
``tool_call`` log line per call: tool, outcome, duration; and one ``tool.call`` span with the
tool's name (only for a registered tool: a requested name is untrusted text) and outcome.
"""

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import structlog
from pydantic import BaseModel, JsonValue, ValidationError

from ecommerce_ai.infrastructure.caller import NoCallerError, current_caller
from ecommerce_ai.telemetry.attributes import TOOL_NAME, TOOL_OUTCOME
from ecommerce_ai.telemetry.spans import TOOL_CALL
from ecommerce_ai.telemetry.tracing import span
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolErrorResult, ToolFailure
from ecommerce_ai.tools.registry import (
    ALLOWLIST,
    AllowlistEntry,
    AnyToolDefinition,
    ToolRegistry,
)

TIMEOUT_GRACE_SECONDS: Final = 0.5
"""Extra time past a tool's own budget before the invoker cancels it; the upstream client enforces
the budget itself, so this only stops a handler that hangs."""

OUTCOME_OK: Final = "ok"
OUTCOME_UNKNOWN_TOOL: Final = "unknown_tool"
UNKNOWN_TOOL_LABEL: Final = "<unknown>"
"""What the log line names a refused unknown tool by: the requested name is untrusted text."""

_logger = structlog.get_logger(__name__)


async def run_definition(definition: AnyToolDefinition, arguments: Mapping[str, Any]) -> BaseModel:
    """Validate ``arguments`` and run the handler of ``definition`` within its timeout.

    Raises the input model's ``ValidationError`` before the handler runs, ``ToolFailure`` from
    the handler and ``TimeoutError`` when it outlives its budget.
    """
    assert definition.input_model is not None  # noqa: S101  # guaranteed by ToolRegistry
    validated = definition.input_model.model_validate(dict(arguments))
    async with asyncio.timeout(definition.timeout_seconds + TIMEOUT_GRACE_SECONDS):
        output: BaseModel = await definition.handler(validated)
    return output


def failure_result(error: BaseException) -> ToolErrorResult | None:
    """Return the error result ``error`` (or the error it wraps) maps to, if it is a known one."""
    for candidate in (error, error.__cause__):
        if isinstance(candidate, ToolFailure):
            return candidate.result
        if isinstance(candidate, ValidationError):
            return ToolErrorResult.for_code(ToolErrorCode.INVALID_INPUT)
        if isinstance(candidate, TimeoutError):
            return ToolErrorResult.for_code(ToolErrorCode.UPSTREAM_ERROR)
    return None


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """The result of one in-process tool call; never carries the caller's token."""

    name: str
    ok: bool
    payload: dict[str, JsonValue]
    """The tool's output (an untrusted-data envelope) or the fixed ``{error, message}`` body."""
    outcome: str
    """``ok``, a ``ToolErrorCode`` value, or ``unknown_tool``."""
    executed: bool
    """False when the call was refused before the handler ran (unknown tool, invalid input)."""
    upstream_unauthenticated: bool
    duration_ms: float

    @property
    def refused(self) -> bool:
        """True when the handler never ran."""
        return not self.executed


def _upstream_unauthenticated() -> bool:
    try:
        return current_caller().upstream_unauthenticated
    except NoCallerError:
        return False


class ToolInvoker:
    """Runs the tools of a validated registry in-process, mapping every ending to a result."""

    def __init__(
        self, registry: ToolRegistry, allowlist: frozenset[AllowlistEntry] = ALLOWLIST
    ) -> None:
        """Invoke only tools of ``registry`` whose operation is in ``allowlist``."""
        self._registry = registry
        self._allowlist = allowlist

    @property
    def registry(self) -> ToolRegistry:
        """The tools this invoker may run (the tool list offered to a model)."""
        return self._registry

    def _definition(self, name: str) -> AnyToolDefinition | None:
        definition = self._registry.get(name)
        if definition is None or definition.allowlist_entry not in self._allowlist:
            return None
        return definition

    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocation:
        """Run read-only tool ``name``; never raises except on cancellation.

        A mutating tool is refused exactly like an unknown one: it only ever runs through
        ``invoke_approved`` (constitution III).
        """
        return await self._call(name, arguments, mutating=False)

    async def invoke_approved(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocation:
        """Run mutating tool ``name`` once, for a call the customer approved; never raises.

        Only the chat's approval step calls this, with the arguments stored at proposal time. A
        read-only or unknown name is refused; the call is never retried (the upstream ``POST``
        is sent once), so a timeout is reported, not repeated.
        """
        return await self._call(name, arguments, mutating=True)

    async def _call(
        self, name: str, arguments: Mapping[str, Any], *, mutating: bool
    ) -> ToolInvocation:
        """Run ``name`` if it is a registered, allowlisted tool of that kind, in a span."""
        with span(TOOL_CALL) as call:
            started = time.perf_counter()
            definition = self._definition(name)
            if definition is None or definition.mutating is not mutating:
                invocation = self._unknown(name, started)
            else:
                call.set_attribute(TOOL_NAME, definition.name)
                invocation = await self._run(name, definition, arguments, started)
            call.set_attribute(TOOL_OUTCOME, invocation.outcome)
            return invocation

    def _unknown(self, name: str, started: float) -> ToolInvocation:
        result = ToolErrorResult.for_code(ToolErrorCode.NOT_ALLOWED)
        logged = name if self._registry.get(name) is not None else UNKNOWN_TOOL_LABEL
        return self._finish(
            name, started, result, OUTCOME_UNKNOWN_TOOL, executed=False, logged=logged
        )

    async def _run(
        self,
        name: str,
        definition: AnyToolDefinition,
        arguments: Mapping[str, Any],
        started: float,
    ) -> ToolInvocation:
        """Validate and run ``definition``, mapping every ending to a result."""
        try:
            output = await run_definition(definition, arguments)
        except ValidationError:
            result = ToolErrorResult.for_code(ToolErrorCode.INVALID_INPUT)
            return self._finish(name, started, result, result.error.value, executed=False)
        except Exception as error:
            mapped = failure_result(error)
            if mapped is None:
                _logger.exception("tool_call_crashed", tool=name)
                mapped = ToolErrorResult.for_code(ToolErrorCode.UPSTREAM_ERROR)
            return self._finish(name, started, mapped, mapped.error.value, executed=True)
        payload = output.model_dump(mode="json", by_alias=True)
        return self._finish(name, started, payload, OUTCOME_OK, executed=True)

    def _finish(
        self,
        name: str,
        started: float,
        result: ToolErrorResult | dict[str, JsonValue],
        outcome: str,
        *,
        executed: bool,
        logged: str | None = None,
    ) -> ToolInvocation:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        ok = not isinstance(result, ToolErrorResult)
        payload = result if isinstance(result, dict) else result.model_dump(mode="json")
        log = _logger.info if ok or executed else _logger.warning
        log("tool_call", tool=logged or name, outcome=outcome, duration_ms=duration_ms)
        return ToolInvocation(
            name=name,
            ok=ok,
            payload=payload,
            outcome=outcome,
            executed=executed,
            upstream_unauthenticated=executed and _upstream_unauthenticated(),
            duration_ms=duration_ms,
        )
