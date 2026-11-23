"""Structured JSON logging with the correlation id on every line and redaction of secrets.

structlog renders every record, its own and the standard library's (uvicorn, httpx...), as one
JSON object per line. ``redact_sensitive_data`` runs last before rendering, so nothing reaches the
output without passing through it (constitution II and VII).
"""

import logging
import re
import sys
from collections.abc import Mapping
from typing import IO, Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED: Final = "[REDACTED]"
"""Replacement for every redacted value."""

_SENSITIVE_KEY: Final = re.compile(
    r"authorization|cookie|token|passw(or)?d|secret|api[-_]?key|credential|jwt", re.IGNORECASE
)
_VALUE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Authorization header values: "Bearer <token>", "Basic <credentials>".
    (re.compile(r"\b(Bearer|Basic)\s+[^\s\"',;]+", re.IGNORECASE), rf"\1 {REDACTED}"),
    # Bare JWTs (header.payload.signature, base64url, header starting with '{"').
    (re.compile(r"\beyJ[\w-]*\.[\w-]+\.[\w-]*"), REDACTED),
    # Credentials embedded in URLs: scheme://user:password@host.
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), rf"\1{REDACTED}@"),
    # key=value or key: value pairs inside free text.
    (
        re.compile(
            r"\b(password|passwd|pwd|secret|token|access_token|refresh_token|api[-_]?key)"
            r"(\s*[=:]\s*)[^\s&\"',;]+",
            re.IGNORECASE,
        ),
        rf"\1\2{REDACTED}",
    ),
)


def _redact_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_mapping[K](mapping: Mapping[K, object]) -> dict[K, object]:
    return {
        key: REDACTED if isinstance(key, str) and _SENSITIVE_KEY.search(key) else _redact(item)
        for key, item in mapping.items()
    }


def _redact(value: object) -> object:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact(item) for item in value]
    return value


def redact_sensitive_data(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor (the log filter) that hides tokens, passwords and other secrets.

    A field whose name looks sensitive (``authorization``, ``*token*``, ``password``, ``secret``,
    ``api_key``, ``cookie``...) is replaced whole, at any nesting depth. Every string is also
    scrubbed of bearer/basic credentials, JWTs, passwords in URLs and ``key=value`` secrets.
    """
    return _redact_mapping(event_dict)


def _drop_color_message(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Remove uvicorn's ANSI-coloured duplicate of the message."""
    event_dict.pop("color_message", None)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _drop_color_message,
    ]


def configure_logging(level: str, stream: IO[str] | None = None) -> None:
    """Send every log record, as redacted JSON, to ``stream`` (stdout by default)."""
    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_sensitive_data,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # RequestIdMiddleware logs one line per request with its request id; uvicorn's is redundant.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
