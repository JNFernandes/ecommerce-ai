"""Shared fixtures: isolate every test from the developer's environment variables."""

import pytest

from ecommerce_ai.infrastructure.settings import Settings


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable a ``Settings`` field reads and ignore ``.env``: tests see defaults.

    The exceptions: the triage consumer, the triage worker and the outbox relay are disabled, so an
    app built by a test never tries to reach a real Kafka broker or database. Tests of those tasks
    enable them with fake factories.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # never the developer's .env
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("TRIAGE_CONSUMER_ENABLED", "false")
    monkeypatch.setenv("TRIAGE_WORKER_ENABLED", "false")
