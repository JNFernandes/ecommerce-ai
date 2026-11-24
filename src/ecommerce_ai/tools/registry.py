"""Tool registry: every tool is checked against the allowlist and its limits before startup.

A ``ToolDefinition`` names the one allowlisted platform operation it calls, its input and output
models, its timeout and its largest accepted upstream response. ``ToolRegistry.build`` rejects
any definition with a gap, so ``create_app`` fails instead of serving an unchecked tool
(constitution II, research 15).
"""

from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One platform operation a tool may call."""

    service: str
    method: str
    path: str
    operation: str


ALLOWLIST: Final[frozenset[AllowlistEntry]] = frozenset(
    {
        AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "order"),
        AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "orders"),
        AllowlistEntry("ecommerce-inventory", "GET", "/api/inventory/products", "listProducts"),
        AllowlistEntry("ecommerce-inventory", "GET", "/api/inventory/products/{id}", "getProduct"),
        AllowlistEntry("ecommerce-notifications", "GET", "/api/notifications", "listNotifications"),
    }
)
"""Allowlisted platform operations, all reads (constitution 1.1.0): Orders GraphQL `order` and
`orders`, Inventory products list and item, the caller's Notifications. For a REST route the
operation is a label and the path is the route template."""


@dataclass(frozen=True, slots=True)
class ToolDefinition[InputT: BaseModel, OutputT: BaseModel]:
    """A tool as served over MCP: schema, limits, the operation it calls and its handler."""

    name: str
    description: str
    allowlist_entry: AllowlistEntry | None
    input_model: type[InputT] | None
    output_model: type[OutputT] | None
    timeout_seconds: float
    max_response_bytes: int
    handler: Callable[[InputT], Awaitable[OutputT]]
    read_only: bool = True


# Any: the registry holds tools with different input/output models; each handler only ever
# receives arguments validated by its own ``input_model``.
AnyToolDefinition = ToolDefinition[Any, Any]


class ToolRegistryError(ValueError):
    """A tool definition is incomplete or calls an operation that is not allowlisted."""


def _problems(definition: AnyToolDefinition, allowlist: frozenset[AllowlistEntry]) -> list[str]:
    """Return every gap of one definition, empty when it is valid."""
    problems: list[str] = []
    if not definition.name.strip():
        problems.append("has no name")
    if definition.allowlist_entry is None:
        problems.append("has no allowlist entry")
    elif definition.allowlist_entry not in allowlist:
        problems.append(f"calls a non-allowlisted operation {definition.allowlist_entry}")
    if definition.input_model is None:
        problems.append("has no input model")
    if definition.output_model is None:
        problems.append("has no output model")
    if not definition.timeout_seconds > 0:
        problems.append("needs a timeout > 0")
    if not definition.max_response_bytes > 0:
        problems.append("needs a max response size > 0")
    return problems


class ToolRegistry:
    """The validated, immutable set of tools the MCP server serves."""

    def __init__(self, tools: tuple[AnyToolDefinition, ...]) -> None:
        """Hold already validated ``tools``; use ``build`` to validate."""
        self._tools = {tool.name: tool for tool in tools}

    @classmethod
    def build(
        cls,
        definitions: Iterable[AnyToolDefinition],
        allowlist: frozenset[AllowlistEntry] = ALLOWLIST,
    ) -> "ToolRegistry":
        """Validate ``definitions``; raise ``ToolRegistryError`` listing every gap found."""
        tools = tuple(definitions)
        errors: list[str] = []
        seen: set[str] = set()
        for tool in tools:
            errors.extend(f"tool {tool.name!r} {problem}" for problem in _problems(tool, allowlist))
            if tool.name in seen:
                errors.append(f"tool {tool.name!r} is defined more than once")
            seen.add(tool.name)
        if errors:
            raise ToolRegistryError("; ".join(errors))
        return cls(tools)

    def __iter__(self) -> Iterator[AnyToolDefinition]:
        """Iterate over the tools in registration order."""
        return iter(self._tools.values())

    def __len__(self) -> int:
        """Return the number of tools."""
        return len(self._tools)

    def get(self, name: str) -> AnyToolDefinition | None:
        """Return the tool called ``name``, or ``None``."""
        return self._tools.get(name)
