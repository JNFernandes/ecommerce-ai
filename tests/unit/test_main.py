"""``python -m ecommerce_ai`` serves the application factory on the configured port."""

import pytest
import uvicorn

from ecommerce_ai.main import run


def test_should_serve_the_factory_on_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("PORT", "8123")

    run()

    ((app, kwargs),) = calls
    assert app == "ecommerce_ai.main:create_app"
    assert kwargs["factory"] is True
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8123)
