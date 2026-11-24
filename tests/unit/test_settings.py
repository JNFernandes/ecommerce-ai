"""Settings come from the environment, with safe local defaults and validation."""

import pytest
from pydantic import ValidationError

from ecommerce_ai.infrastructure.settings import LogLevel, Settings


def test_should_use_local_defaults_when_the_environment_is_empty() -> None:
    settings = Settings()

    assert settings.port == 8002
    assert settings.log_level is LogLevel.INFO
    assert settings.cors_allowed_origins == []
    assert settings.orders_base_url == "http://localhost:8090/api/orders"
    assert settings.inventory_base_url == "http://localhost:8090/api/inventory"
    assert settings.notifications_base_url == "http://localhost:8090/api"
    assert settings.database_url.get_secret_value() == "postgresql://localhost:5436/ecommerce_ai"
    assert settings.kafka_brokers == ["localhost:9092"]
    assert settings.anthropic_model is None
    assert settings.jwt_clock_tolerance_seconds == 5


def test_should_read_every_value_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://a.example, http://b.example")
    monkeypatch.setenv("KAFKA_BROKERS", "kafka:29092,kafka2:29092")
    monkeypatch.setenv("ORDERS_BASE_URL", "http://gateway:8090/api/orders/")
    monkeypatch.setenv("ANTHROPIC_MODEL", "some-model")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "0")

    settings = Settings()

    assert settings.port == 9000
    assert settings.cors_allowed_origins == ["http://a.example", "http://b.example"]
    assert settings.kafka_brokers == ["kafka:29092", "kafka2:29092"]
    assert settings.orders_base_url == "http://gateway:8090/api/orders"
    assert settings.anthropic_model == "some-model"
    assert settings.daily_token_budget == 0


def test_should_treat_a_blank_model_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "  ")

    assert Settings().anthropic_model is None


def test_should_never_show_the_database_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://ai:s3cret@localhost:5436/ecommerce_ai")

    settings = Settings()

    assert "s3cret" not in repr(settings)
    assert "s3cret" not in str(settings.model_dump())


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PORT", "0"),
        ("JWT_CLOCK_TOLERANCE_SECONDS", "301"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("DAILY_TOKEN_BUDGET", "-1"),
        ("ORDERS_BASE_URL", "ftp://gateway/api/orders"),
        ("INVENTORY_BASE_URL", "inventory"),
        ("NOTIFICATIONS_BASE_URL", "ftp://gateway/api"),
        ("IDENTITY_JWKS_URL", "not-a-url"),
        ("DATABASE_URL", "mysql://localhost:3306/ai"),
        ("KAFKA_BROKERS", "localhost"),
        ("KAFKA_BROKERS", " , "),
        ("LOG_LEVEL", "TRACE"),
    ],
)
def test_should_reject_an_invalid_value(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings()


def test_should_use_mcp_tool_defaults_when_the_environment_is_empty() -> None:
    settings = Settings()

    assert settings.orders_graphql_url == "http://localhost:8090/api/graphql"
    assert settings.jwks_timeout_seconds == 3.0
    assert settings.jwks_refetch_cooldown_seconds == 30.0
    assert settings.tool_timeout_seconds == 5.0
    assert settings.tool_max_response_bytes == 262_144
    assert settings.tool_text_max_chars == 500
    assert settings.upstream_max_retries == 2
    assert settings.upstream_backoff_base_seconds == 0.2


def test_should_read_mcp_tool_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORDERS_GRAPHQL_URL", "http://gateway:8090/api/graphql/")
    monkeypatch.setenv("INVENTORY_BASE_URL", "http://gateway:8090/api/inventory/")
    monkeypatch.setenv("NOTIFICATIONS_BASE_URL", "http://gateway:8090/api/")
    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "0")

    settings = Settings()

    assert settings.orders_graphql_url == "http://gateway:8090/api/graphql"
    assert settings.inventory_base_url == "http://gateway:8090/api/inventory"
    assert settings.notifications_base_url == "http://gateway:8090/api"
    assert settings.tool_timeout_seconds == 0.5
    assert settings.upstream_max_retries == 0


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("ORDERS_GRAPHQL_URL", "graphql"),
        ("JWKS_TIMEOUT_SECONDS", "0"),
        ("JWKS_REFETCH_COOLDOWN_SECONDS", "0"),
        ("TOOL_TIMEOUT_SECONDS", "0"),
        ("TOOL_MAX_RESPONSE_BYTES", "0"),
        ("TOOL_TEXT_MAX_CHARS", "0"),
        ("UPSTREAM_MAX_RETRIES", "-1"),
        ("UPSTREAM_BACKOFF_BASE_SECONDS", "0"),
    ],
)
def test_should_reject_an_invalid_mcp_tool_setting(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings()
