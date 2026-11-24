"""Shared fixtures: isolate every test from the developer's environment variables."""

import pytest

from ecommerce_ai.infrastructure.settings import Settings


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable a ``Settings`` field reads, so tests only see the defaults.

    The one exception: the triage consumer is disabled, so an app built by a test never tries to
    reach a real Kafka broker. Tests of the consumer enable it with a fake consumer factory.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("TRIAGE_CONSUMER_ENABLED", "false")
