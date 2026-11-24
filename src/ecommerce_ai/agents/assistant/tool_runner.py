"""``ToolRunner`` over the shared ``ToolInvoker`` (research §7): the same registry, allowlist,
input validation, timeouts and error mapping as ``/mcp``.

The tools offered to the model are exactly the registry's, with their input models' JSON schemas
(FR-011). Each call runs with the caller bound by the request (``bind_caller``), so handlers
forward the caller's own token (ADR 0001); the result never carries it.
"""

from collections.abc import Mapping

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.ports import ToolOutcome, ToolSpec
from ecommerce_ai.tools.invoker import ToolInvoker


class InvokerToolRunner:
    """The read-only tools, run in-process."""

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
        """Invoke ``name``; refused, failed and upstream-401 calls become the outcome."""
        invocation = await self._invoker.invoke(name, arguments)
        return ToolOutcome(
            ok=invocation.ok,
            payload=invocation.payload,
            outcome=invocation.outcome,
            refused=invocation.refused,
            upstream_unauthenticated=invocation.upstream_unauthenticated,
            duration_ms=invocation.duration_ms,
        )
