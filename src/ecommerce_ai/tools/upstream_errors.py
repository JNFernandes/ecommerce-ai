"""One mapping from upstream exceptions to tool error results, shared by every tool.

``map_upstream_errors`` wraps an upstream call and the validation of its answer:
``Unauthenticated`` flags the caller (the MCP endpoint then answers HTTP 401) and fails the call,
``Forbidden`` → ``not_allowed``, ``NotFound`` → ``not_found`` with the tool's message when the
route gives 404 a meaning (otherwise ``upstream_error``), and ``UpstreamError`` or a Pydantic
``ValidationError`` of the upstream answer → ``upstream_error`` with the service's message
("inventory unavailable"). Messages are constants: no upstream text ever reaches a result.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import ValidationError

from ecommerce_ai.infrastructure.caller import current_caller
from ecommerce_ai.infrastructure.upstream import (
    Forbidden,
    NotFound,
    Unauthenticated,
    UpstreamError,
)
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure


@contextmanager
def map_upstream_errors(*, unavailable: str, not_found: str | None = None) -> Iterator[None]:
    """Turn upstream exceptions raised inside the block into ``ToolFailure``.

    ``unavailable`` is the ``upstream_error`` message; ``not_found``, when given, makes a
    ``NotFound`` a ``not_found`` failure with that message. A ``ToolFailure`` passes through.
    """
    try:
        yield
    except Unauthenticated as error:
        current_caller().upstream_unauthenticated = True
        raise ToolFailure(ToolErrorCode.UPSTREAM_ERROR, unavailable) from error
    except Forbidden as error:
        raise ToolFailure(ToolErrorCode.NOT_ALLOWED) from error
    except NotFound as error:
        if not_found is not None:
            raise ToolFailure(ToolErrorCode.NOT_FOUND, not_found) from error
        raise ToolFailure(ToolErrorCode.UPSTREAM_ERROR, unavailable) from error
    except (UpstreamError, ValidationError) as error:
        raise ToolFailure(ToolErrorCode.UPSTREAM_ERROR, unavailable) from error
