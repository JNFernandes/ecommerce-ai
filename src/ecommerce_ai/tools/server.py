"""The MCP server: serves the tools of a validated ``ToolRegistry`` over stateless HTTP.

``build_mcp_server`` turns each ``ToolDefinition`` into a FastMCP tool whose input and output
schemas come from its Pydantic models, annotated ``readOnlyHint``. ``ToolCallMiddleware`` maps
every ``ToolFailure`` and argument ``ValidationError`` to an ``isError`` result with the fixed
``{error, message}`` body, answers an unknown tool with JSON-RPC ``-32602`` and logs one line per
call: tool, outcome and duration (``request_id`` comes from the bound log context, never the
token). ``build_mcp_app`` returns the Streamable HTTP ASGI app (stateless, JSON responses) whose
endpoint is exactly ``MCP_PATH`` (research 1, 2 and 13).
"""

import time
from typing import Any, Final

import mcp.types as mt
import structlog
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import PrivateAttr
from pydantic import ValidationError as PydanticValidationError

from ecommerce_ai.tools.envelope import ToolErrorCode, ToolErrorResult, ToolFailure
from ecommerce_ai.tools.registry import AnyToolDefinition, ToolRegistry

MCP_PATH: Final = "/mcp"
"""Exact path of the MCP endpoint (no trailing slash, no redirect)."""

SERVER_NAME: Final = "ecommerce-ai"

_logger = structlog.get_logger(__name__)


class RegistryTool(Tool):
    """A FastMCP tool backed by one ``ToolDefinition``."""

    _definition: AnyToolDefinition = PrivateAttr()

    @classmethod
    def from_definition(cls, definition: AnyToolDefinition) -> "RegistryTool":
        """Build the tool, its schemas and annotations from ``definition``."""
        assert definition.input_model is not None  # noqa: S101  # guaranteed by ToolRegistry
        assert definition.output_model is not None  # noqa: S101  # guaranteed by ToolRegistry
        tool = cls(
            name=definition.name,
            description=definition.description,
            parameters=definition.input_model.model_json_schema(),
            output_schema=definition.output_model.model_json_schema(),
            annotations=ToolAnnotations(read_only_hint=definition.read_only),
        )
        tool._definition = definition
        return tool

    # Any: the signature of ``Tool.run``; arguments are validated by the input model here.
    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate ``arguments``, run the handler and return its output as structured content.

        A ``ToolFailure`` leaves as a ``ToolError`` caused by it, so FastMCP neither logs a
        traceback nor masks it before ``ToolCallMiddleware`` maps it.
        """
        definition = self._definition
        assert definition.input_model is not None  # noqa: S101  # guaranteed by ToolRegistry
        validated = definition.input_model.model_validate(arguments)
        try:
            output = await definition.handler(validated)
        except ToolFailure as failure:
            raise ToolError(str(failure)) from failure
        return ToolResult(structured_content=output.model_dump(mode="json", by_alias=True))


def _error_result(failure: ToolErrorResult) -> ToolResult:
    body = failure.model_dump(mode="json")
    return ToolResult(content=body["message"], structured_content=body, is_error=True)


def _failure_result(error: BaseException) -> ToolErrorResult | None:
    """Return the error result ``error`` (or the error it wraps) maps to, if it is a known one."""
    for candidate in (error, error.__cause__):
        if isinstance(candidate, ToolFailure):
            return candidate.result
        if isinstance(candidate, PydanticValidationError):
            return ToolErrorResult.for_code(ToolErrorCode.INVALID_INPUT)
    return None


class ToolCallMiddleware(Middleware):
    """Maps tool failures to fixed error results and logs every call."""

    def __init__(self, registry: ToolRegistry) -> None:
        """Serve only the tools of ``registry``."""
        self._registry = registry

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run the call; map failures to ``isError`` results; reject unknown tools."""
        name = context.message.name
        if self._registry.get(name) is None:
            _logger.warning("tool_call", tool=name, outcome="unknown_tool", duration_ms=0.0)
            raise MCPError(code=INVALID_PARAMS, message="unknown tool")
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await call_next(context)
        except Exception as error:
            failure = _failure_result(error)
            if failure is None:
                outcome = ToolErrorCode.UPSTREAM_ERROR.value
                _logger.exception("tool_call_crashed", tool=name)
                return _error_result(ToolErrorResult.for_code(ToolErrorCode.UPSTREAM_ERROR))
            outcome = failure.error.value
            return _error_result(failure)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            _logger.info("tool_call", tool=name, outcome=outcome, duration_ms=duration_ms)


def build_mcp_server(registry: ToolRegistry) -> FastMCP:
    """Return a FastMCP server exposing exactly the tools of ``registry``."""
    server = FastMCP(SERVER_NAME, middleware=[ToolCallMiddleware(registry)])
    for definition in registry:
        server.add_tool(RegistryTool.from_definition(definition))
    return server


def build_mcp_app(server: FastMCP) -> StarletteWithLifespan:
    """Return the stateless, JSON-response Streamable HTTP app serving ``MCP_PATH``."""
    return server.http_app(path=MCP_PATH, stateless_http=True, json_response=True)
