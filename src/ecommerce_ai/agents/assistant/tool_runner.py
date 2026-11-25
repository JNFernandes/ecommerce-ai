"""``ToolRunner`` over the shared ``ToolInvoker`` (research §7): the same registry, allowlist,
input validation, timeouts and error mapping as ``/mcp``.

The tools offered to the model are exactly the registry's, with their input models' JSON schemas
(FR-011). Each call runs with the caller bound by the request (``bind_caller``), so handlers
forward the caller's own token (ADR 0001); the result never carries it.
"""

from collections.abc import Mapping

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.ports import ToolOutcome, ToolSpec
from ecommerce_ai.tools.invoker import ToolInvocation, ToolInvoker


class InvokerToolRunner:
    """The chat's tools, run in-process; a mutating one only once approved (US-06)."""

    def __init__(self, invoker: ToolInvoker) -> None:
        """Run the tools of ``invoker``'s registry."""
        self._invoker = invoker
        self._specs = tuple(
            ToolSpec(
                name=definition.name,
                description=definition.description,
                input_schema=(
                    definition.input_model.model_json_schema()
                    if definition.input_model is not None
                    else {"type": "object"}
                ),
            )
            for definition in invoker.registry
        )

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Every registry tool."""
        return self._specs

    async def run(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Invoke ``name``; refused, failed and upstream-401 calls become the outcome.

        A mutating tool is refused here (``unknown_tool``): it runs only via ``run_approved``.
        """
        return _outcome(await self._invoker.invoke(name, arguments))

    async def run_approved(self, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run mutating tool ``name`` once with the approved, stored ``arguments``."""
        return _outcome(await self._invoker.invoke_approved(name, arguments))


def _outcome(invocation: ToolInvocation) -> ToolOutcome:
    """The port's view of one invocation."""
    return ToolOutcome(
        ok=invocation.ok,
        payload=invocation.payload,
        outcome=invocation.outcome,
        refused=invocation.refused,
        upstream_unauthenticated=invocation.upstream_unauthenticated,
        duration_ms=invocation.duration_ms,
    )
