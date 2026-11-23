"""JSON logs carry the request id and never contain tokens or passwords."""

import io
import json
import logging
import uuid

import httpx
import pytest
import structlog

from ecommerce_ai.infrastructure.logging import REDACTED, configure_logging, redact_sensitive_data
from ecommerce_ai.main import create_app

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"


@pytest.fixture
def log_output() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    return stream


def _client_logging_to(stream: io.StringIO) -> httpx.AsyncClient:
    """Client of a fresh app whose logs go to ``stream`` (``create_app`` configures stdout)."""
    app = create_app(probes=[])
    configure_logging("INFO", stream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _completed(stream: io.StringIO) -> list[dict[str, object]]:
    return [line for line in _lines(stream) if line["event"] == "request completed"]


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_should_redact_sensitive_fields_at_any_depth() -> None:
    event = {
        "event": "call",
        "headers": {"Authorization": f"Bearer {JWT}", "Accept": "application/json"},
        "password": "hunter2",
        "refresh_token": "abc",
        "items": [{"api_key": "k"}, "fine"],
    }

    redacted = redact_sensitive_data(None, "info", event)

    assert redacted["headers"] == {"Authorization": REDACTED, "Accept": "application/json"}
    assert redacted["password"] == REDACTED
    assert redacted["refresh_token"] == REDACTED
    assert redacted["items"] == [{"api_key": REDACTED}, "fine"]


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"sent Authorization: Bearer {JWT}", JWT),
        (f"token {JWT} here", JWT),
        ("connect postgresql://ai:s3cret@db:5432/ai", "s3cret"),
        ("login password=hunter2 ok", "hunter2"),
        ("Basic dXNlcjpwYXNz rejected", "dXNlcjpwYXNz"),
    ],
)
def test_should_redact_secrets_inside_free_text(text: str, secret: str) -> None:
    redacted = redact_sensitive_data(None, "info", {"event": text})

    assert secret not in str(redacted["event"])
    assert REDACTED in str(redacted["event"])


def test_should_render_standard_library_records_as_redacted_json(
    log_output: io.StringIO,
) -> None:
    logging.getLogger("httpx").warning("retrying with Bearer %s", JWT)

    (line,) = _lines(log_output)
    assert JWT not in log_output.getvalue()
    assert (line["level"], line["logger"]) == ("warning", "httpx")
    assert "timestamp" in line


def test_should_not_duplicate_uvicorn_messages(log_output: io.StringIO) -> None:
    logging.getLogger("uvicorn.access").info("GET /health 200")
    logging.getLogger("uvicorn.error").info("started", extra={"color_message": "[1mstarted"})

    assert _lines(log_output) == [
        {
            "event": "started",
            "logger": "uvicorn.error",
            "level": "info",
            "timestamp": _lines(log_output)[0]["timestamp"],
        }
    ]


def test_should_redact_secrets_in_exception_tracebacks(log_output: io.StringIO) -> None:
    try:
        raise ValueError(f"rejected token {JWT}")
    except ValueError:
        structlog.get_logger("test").exception("call failed")

    assert JWT not in log_output.getvalue()
    assert "ValueError" in log_output.getvalue()


async def test_should_tag_every_line_with_the_callers_request_id(log_output: io.StringIO) -> None:
    async with _client_logging_to(log_output) as client:
        response = await client.get(
            "/health", headers={"X-Request-Id": "abc-123", "Authorization": f"Bearer {JWT}"}
        )

    assert response.headers["X-Request-Id"] == "abc-123"
    completed = _completed(log_output)
    assert completed == [
        {
            "event": "request completed",
            "request_id": "abc-123",
            "method": "GET",
            "path": "/health",
            "status": 200,
            "duration_ms": completed[0]["duration_ms"],
            "logger": "ecommerce_ai.api.middleware",
            "level": "info",
            "timestamp": completed[0]["timestamp"],
        }
    ]
    assert JWT not in log_output.getvalue()


@pytest.mark.parametrize("incoming", [None, "", "x" * 129, "bad id\nforged line"])
async def test_should_generate_a_request_id_when_the_callers_is_missing_or_unsafe(
    log_output: io.StringIO, incoming: str | None
) -> None:
    headers = {} if incoming is None else {"X-Request-Id": incoming}
    async with _client_logging_to(log_output) as client:
        response = await client.get("/health", headers=headers)

    request_id = response.headers["X-Request-Id"]
    assert str(uuid.UUID(request_id)) == request_id
    assert [line["request_id"] for line in _completed(log_output)] == [request_id]
