"""A dead or raising trace exporter never changes a chat answer (FR-003, plan D4/D5)."""

import asyncio
import time
from collections.abc import Iterator, Sequence

import httpx
import pytest
import respx
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.infrastructure.tracing import build_tracer_provider, shutdown_tracer_provider
from ecommerce_ai.telemetry.tracing import use_provider
from tests.support.chat import chat_app
from tests.support.llm import ScriptedChatModel, text_turn
from tests.support.sse import events_of, parse_sse
from tests.support.tokens import jwks, make_token

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
HEADERS = {"Authorization": f"Bearer {make_token(sub='user-1')}"}
SHUTDOWN_BOUND_SECONDS = 10.0


class RaisingExporter(SpanExporter):
    """An exporter whose every call raises."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise RuntimeError("collector exploded")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        raise RuntimeError("collector exploded")

    def shutdown(self) -> None:
        raise RuntimeError("collector exploded")


@pytest.fixture(autouse=True)
def identity() -> Iterator[None]:
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield


async def _answer(provider: TracerProvider | None) -> list[tuple[str, object]]:
    """One chat message's SSE events, without the ids that differ per run."""
    model = ScriptedChatModel([text_turn("Your order ", "is on its way.")])
    app = chat_app(model, tracer_provider=provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            created = await client.post("/chat/conversations", json={}, headers=HEADERS)
            response = await client.post(
                f"/chat/conversations/{created.json()['id']}/messages",
                json={"content": "Where is my order?"},
                headers=HEADERS,
            )
    finally:
        use_provider(None)
    assert response.status_code == 200
    answer: list[tuple[str, object]] = []
    for event in events_of(parse_sse(response.text)):
        data = event.data
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in {"messageId", "conversationId"}}
        answer.append((event.event, data))
    return answer


async def _shutdown_within_bound(provider: TracerProvider) -> float:
    started = time.monotonic()
    await asyncio.wait_for(
        asyncio.to_thread(shutdown_tracer_provider, provider), SHUTDOWN_BOUND_SECONDS
    )
    return time.monotonic() - started


async def test_should_answer_identically_when_the_otlp_collector_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    baseline = await _answer(None)
    provider = build_tracer_provider({"OTEL_TRACES_EXPORTER": "otlp"})
    assert provider is not None

    traced = await _answer(provider)

    assert traced == baseline
    assert await _shutdown_within_bound(provider) < SHUTDOWN_BOUND_SECONDS


async def test_should_answer_identically_when_the_exporter_raises() -> None:
    baseline = await _answer(None)
    provider = build_tracer_provider(
        {"OTEL_TRACES_EXPORTER": "raising"}, exporters={"raising": RaisingExporter}
    )
    assert provider is not None

    traced = await _answer(provider)

    assert traced == baseline
    await _shutdown_within_bound(provider)
