"""``traced_node``: wrap a LangGraph node in a ``node.<name>`` span at graph build time (D7).

The wrapper keeps the node's signature (``functools.wraps``), so LangGraph still injects
``runtime``/``config`` as before, and stays a coroutine function for an async node. A LangGraph
control-flow exception (``interrupt``, ``Command`` to a parent) is not an error.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import cast, overload

from langgraph.errors import GraphBubbleUp

from ecommerce_ai.telemetry.attributes import NODE_NAME
from ecommerce_ai.telemetry.tracing import span

_CONTROL_FLOW: tuple[type[BaseException], ...] = (GraphBubbleUp,)


@overload
def traced_node[**P, R](name: str, fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]: ...


@overload
def traced_node[**P, R](name: str, fn: Callable[P, R]) -> Callable[P, R]: ...


def traced_node[**P, R](name: str, fn: Callable[P, R]) -> Callable[P, R]:
    """Return ``fn`` wrapped in a ``node.<name>`` span carrying ``node.name``."""
    span_name = f"node.{name}"
    attributes = {NODE_NAME: name}

    if inspect.iscoroutinefunction(fn):
        async_fn = cast("Callable[P, Awaitable[object]]", fn)

        @functools.wraps(fn)
        async def run_async(*args: P.args, **kwargs: P.kwargs) -> object:
            with span(span_name, attributes, ignore=_CONTROL_FLOW):
                return await async_fn(*args, **kwargs)

        return cast("Callable[P, R]", run_async)

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with span(span_name, attributes, ignore=_CONTROL_FLOW):
            return fn(*args, **kwargs)

    return run
