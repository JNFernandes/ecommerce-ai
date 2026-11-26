"""Shared fixtures: isolate every test from the developer's environment variables."""

import pytest

from ecommerce_ai.infrastructure.langsmith import clear_langsmith_env_cache
from ecommerce_ai.infrastructure.settings import Settings

# Standard variables read outside ``Settings``: OpenTelemetry's exporter selection and the ones
# LangSmith/LangChain read themselves. A developer's shell must never turn tracing on in a test.
_STANDARD_VARIABLES = (
    "OTEL_SDK_DISABLED",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_HIDE_INPUTS",
    "LANGCHAIN_HIDE_OUTPUTS",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable a ``Settings`` field reads and ignore ``.env``: tests see defaults.

    The exceptions: the triage consumer, the triage worker and the outbox relay are disabled, so an
    app built by a test never tries to reach a real Kafka broker or database. Tests of those tasks
    enable them with fake factories.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # never the developer's .env
    for name in (*(field.upper() for field in Settings.model_fields), *_STANDARD_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    clear_langsmith_env_cache()  # LangSmith caches the variables it has read
    monkeypatch.setenv("TRIAGE_CONSUMER_ENABLED", "false")
    monkeypatch.setenv("TRIAGE_WORKER_ENABLED", "false")
