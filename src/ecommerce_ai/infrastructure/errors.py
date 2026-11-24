"""Describe an exception for logs and readiness without leaking secrets or payload values.

SQLAlchemy wraps every asyncpg error in a generic ``DBAPIError`` whose own class says nothing
(an authentication failure, a missing database and a bad statement all look alike). These helpers
unwrap it to the driver's exception: ``error_name`` gives its class name (safe for ``/ready``) and
``error_message`` its first line, cut short, with credentials scrubbed and the values of query
arguments removed (asyncpg echoes them, and they can hold event data).
"""

import re
from typing import Final

from sqlalchemy.exc import DBAPIError

from ecommerce_ai.infrastructure.logging import REDACTED, redact_text

MAX_MESSAGE_LENGTH: Final = 300
_MAX_CHAIN: Final = 10
_QUERY_ARGUMENT: Final = re.compile(r"(query argument \$\d+).*", re.DOTALL)


def root_error(error: BaseException) -> BaseException:
    """For a ``DBAPIError``, the database driver's own exception; any other error as it is."""
    if not isinstance(error, DBAPIError) or error.orig is None:
        return error
    # SQLAlchemy's asyncpg adapter wraps the driver's exception once more, as ``__cause__``.
    current: BaseException = error.orig
    for _ in range(_MAX_CHAIN):
        cause = current.__cause__
        if cause is None:
            break
        current = cause
    return current


def error_name(error: BaseException) -> str:
    """Class name of the root cause, e.g. ``InvalidPasswordError``; never a message."""
    return type(root_error(error)).__name__


def error_message(error: BaseException) -> str:
    """One sanitized line saying what failed: no password, no token, no query argument values."""
    text = " ".join(str(root_error(error)).split())
    text = _QUERY_ARGUMENT.sub(rf"\1: {REDACTED}", text)
    return redact_text(text)[:MAX_MESSAGE_LENGTH]


def error_fields(error: BaseException) -> dict[str, str]:
    """Log fields describing ``error``: ``error`` (class name) and ``error_message``."""
    return {"error": error_name(error), "error_message": error_message(error)}
