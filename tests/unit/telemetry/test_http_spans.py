"""``http.request``: one root span per request with the route template, status and request id."""

import uuid

import httpx

from ecommerce_ai.infrastructure.health import DependencyStatus
from ecommerce_ai.main import create_app
from ecommerce_ai.telemetry import attributes
from tests.support.probes import FakeProbe
from tests.support.tracing import SpanRecorder

_PROBES = [FakeProbe("database", critical=True, status=DependencyStatus.UP)]


async def _get(
    spans: SpanRecorder, path: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    app = create_app(probes=_PROBES, tracer_provider=spans.provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get(path, headers=headers)


async def test_should_trace_a_request_with_its_route_status_and_request_id(
    spans: SpanRecorder,
) -> None:
    response = await _get(spans, "/health", {"X-Request-Id": "req-42"})

    request = spans.one("http.request")
    assert response.headers["X-Request-Id"] == "req-42"
    assert dict(request.attributes or {}) == {
        attributes.REQUEST_ID: "req-42",
        attributes.HTTP_ROUTE: "/health",
        attributes.HTTP_STATUS_CODE: 200,
    }
    assert request.parent is None


async def test_should_record_the_route_template_not_the_raw_path(spans: SpanRecorder) -> None:
    conversation_id = str(uuid.uuid4())

    response = await _get(spans, f"/chat/conversations/{conversation_id}?q=jane@example.com")

    request = spans.one("http.request")
    assert response.status_code == 401
    assert request.attributes is not None
    assert request.attributes[attributes.HTTP_ROUTE] == "/chat/conversations/{conversation_id}"
    assert request.attributes[attributes.HTTP_STATUS_CODE] == 401
    assert request.attributes[attributes.REQUEST_ID] == response.headers["X-Request-Id"]
    assert conversation_id not in request.to_json()
    assert "jane@example.com" not in request.to_json()


async def test_should_leave_out_the_route_when_none_matched(spans: SpanRecorder) -> None:
    response = await _get(spans, "/no/such/route")

    request = spans.one("http.request")
    assert response.status_code == 404
    assert attributes.HTTP_ROUTE not in (request.attributes or {})
    assert (request.attributes or {})[attributes.HTTP_STATUS_CODE] == 404
