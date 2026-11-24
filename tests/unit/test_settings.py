"""Settings come from the environment, with safe local defaults and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ecommerce_ai.infrastructure.db.engine import async_database_url
from ecommerce_ai.infrastructure.settings import ENV_FILE, LlmEffort, LogLevel, Settings


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


def test_should_use_triage_consumer_defaults_when_the_environment_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRIAGE_CONSUMER_ENABLED")  # set by tests/conftest.py
    settings = Settings()

    assert settings.triage_consumer_enabled is True
    assert settings.kafka_consumer_group == "ai-triage"
    assert settings.kafka_reconnect_backoff_base_seconds == 1.0
    assert settings.kafka_reconnect_backoff_max_seconds == 30.0
    assert settings.db_retry_max_attempts == 5
    assert settings.db_retry_backoff_base_seconds == 0.5
    assert settings.db_retry_backoff_max_seconds == 10.0
    assert settings.database_timeout_seconds == 5.0
    assert settings.consumer_shutdown_timeout_seconds == 10.0


def test_should_read_triage_consumer_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_CONSUMER_ENABLED", "false")
    monkeypatch.setenv("KAFKA_CONSUMER_GROUP", "ai-triage-test")
    monkeypatch.setenv("DB_RETRY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("DB_RETRY_BACKOFF_BASE_SECONDS", "2")
    monkeypatch.setenv("DB_RETRY_BACKOFF_MAX_SECONDS", "2")

    settings = Settings()

    assert settings.triage_consumer_enabled is False
    assert settings.kafka_consumer_group == "ai-triage-test"
    assert settings.db_retry_max_attempts == 1
    assert settings.db_retry_backoff_base_seconds == 2.0
    assert settings.db_retry_backoff_max_seconds == 2.0


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("KAFKA_CONSUMER_GROUP", ""),
        ("KAFKA_RECONNECT_BACKOFF_BASE_SECONDS", "0"),
        ("KAFKA_RECONNECT_BACKOFF_MAX_SECONDS", "0"),
        ("DB_RETRY_MAX_ATTEMPTS", "0"),
        ("DB_RETRY_BACKOFF_BASE_SECONDS", "0"),
        ("DB_RETRY_BACKOFF_MAX_SECONDS", "-1"),
        ("DATABASE_TIMEOUT_SECONDS", "0"),
        ("CONSUMER_SHUTDOWN_TIMEOUT_SECONDS", "0"),
    ],
)
def test_should_reject_an_invalid_triage_consumer_setting(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings()


@pytest.mark.parametrize("prefix", ["KAFKA_RECONNECT_BACKOFF", "DB_RETRY_BACKOFF"])
def test_should_reject_a_backoff_base_above_its_maximum_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    monkeypatch.setenv(f"{prefix}_BASE_SECONDS", "5")
    monkeypatch.setenv(f"{prefix}_MAX_SECONDS", "4")

    with pytest.raises(ValidationError, match=f"{prefix.lower()}_base_seconds must not exceed"):
        Settings()


def test_should_accept_an_asyncpg_database_url_when_it_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ai_user:ai_pass@db:5436/ecommerce_ai")

    url = async_database_url(Settings())

    assert (url.drivername, url.username, url.password) == (
        "postgresql+asyncpg",
        "ai_user",
        "ai_pass",
    )
    assert (url.host, url.port, url.database) == ("db", 5436, "ecommerce_ai")


def test_should_not_echo_a_rejected_database_url_when_it_holds_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://ai:s3cret@db/ecommerce_ai")

    with pytest.raises(ValidationError) as error:
        Settings()

    assert "database_url" in str(error.value)
    assert "s3cret" not in str(error.value)


def test_should_read_the_env_file_of_the_working_directory_when_it_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", ENV_FILE)  # conftest turns it off
    monkeypatch.chdir(tmp_path)
    (tmp_path / ENV_FILE).write_text(
        "DATABASE_URL=postgresql+asyncpg://ai_user:ai_pass@localhost:5436/ecommerce_ai\n"
        "PORT=8123\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert async_database_url(settings).username == "ai_user"
    assert settings.port == 8123


def test_should_prefer_the_environment_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", ENV_FILE)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ENV_FILE).write_text("PORT=8123\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "9001")

    assert Settings().port == 9001


def test_should_use_defaults_when_there_is_no_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", ENV_FILE)
    monkeypatch.chdir(tmp_path)

    assert Settings().port == 8002


def test_should_use_triage_worker_defaults_when_the_environment_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRIAGE_WORKER_ENABLED")  # set by tests/conftest.py
    settings = Settings()

    assert settings.triage_worker_enabled is True
    assert settings.triage_settle_seconds == 3.0
    assert settings.triage_poll_interval_seconds == 1.0
    assert settings.triage_backoff_base_seconds == 1.0
    assert settings.triage_backoff_max_seconds == 30.0
    assert settings.anthropic_api_key is None
    assert settings.anthropic_model is None
    assert settings.model_configured is False
    assert settings.llm_timeout_seconds == 30.0
    assert settings.llm_max_retries == 2
    assert settings.llm_max_output_tokens == 1024
    assert settings.llm_effort is LlmEffort.LOW
    assert settings.case_token_cap == 8000
    assert settings.daily_token_budget == 50_000
    assert settings.outbox_max_attempts == 10
    assert settings.outbox_backoff_base_seconds == 1.0
    assert settings.outbox_backoff_max_seconds == 60.0
    assert settings.outbox_poll_interval_seconds == 1.0


def test_should_read_triage_worker_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRIAGE_WORKER_ENABLED", "true")
    monkeypatch.setenv("TRIAGE_SETTLE_SECONDS", "0")
    monkeypatch.setenv("TRIAGE_POLL_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "500")
    monkeypatch.setenv("LLM_EFFORT", "medium")
    monkeypatch.setenv("CASE_TOKEN_CAP", "500")
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "3")

    settings = Settings()

    assert settings.triage_worker_enabled is True
    assert settings.triage_settle_seconds == 0.0
    assert settings.triage_poll_interval_seconds == 0.5
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test"
    assert settings.model_configured is True
    assert settings.llm_max_retries == 0
    assert settings.llm_max_output_tokens == 500
    assert settings.llm_effort is LlmEffort.MEDIUM
    assert settings.case_token_cap == 500
    assert settings.outbox_max_attempts == 3


@pytest.mark.parametrize(
    ("key", "model"), [("sk-test", None), (None, "claude-opus-5-5"), ("  ", "claude-opus-5-5")]
)
def test_should_not_configure_the_model_when_the_key_or_the_model_is_missing(
    monkeypatch: pytest.MonkeyPatch, key: str | None, model: str | None
) -> None:
    if key is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    if model is not None:
        monkeypatch.setenv("ANTHROPIC_MODEL", model)

    assert Settings().model_configured is False


def test_should_never_show_the_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")

    settings = Settings()

    assert "sk-very-secret" not in repr(settings)
    assert "sk-very-secret" not in str(settings.model_dump())


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("TRIAGE_SETTLE_SECONDS", "-1"),
        ("TRIAGE_POLL_INTERVAL_SECONDS", "0"),
        ("TRIAGE_BACKOFF_BASE_SECONDS", "0"),
        ("TRIAGE_BACKOFF_MAX_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_MAX_RETRIES", "-1"),
        ("LLM_MAX_RETRIES", "6"),
        ("LLM_MAX_OUTPUT_TOKENS", "0"),
        ("LLM_EFFORT", "extreme"),
        ("CASE_TOKEN_CAP", "0"),
        ("DAILY_TOKEN_BUDGET", "-1"),
        ("OUTBOX_MAX_ATTEMPTS", "0"),
        ("OUTBOX_BACKOFF_BASE_SECONDS", "0"),
        ("OUTBOX_BACKOFF_MAX_SECONDS", "0"),
        ("OUTBOX_POLL_INTERVAL_SECONDS", "0"),
    ],
)
def test_should_reject_an_invalid_triage_worker_setting(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings()


@pytest.mark.parametrize("prefix", ["TRIAGE_BACKOFF", "OUTBOX_BACKOFF"])
def test_should_reject_a_worker_backoff_base_above_its_maximum_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    monkeypatch.setenv(f"{prefix}_BASE_SECONDS", "5")
    monkeypatch.setenv(f"{prefix}_MAX_SECONDS", "4")

    with pytest.raises(ValidationError, match=f"{prefix.lower()}_base_seconds must not exceed"):
        Settings()


def test_should_reject_an_output_limit_above_the_case_cap_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "2000")
    monkeypatch.setenv("CASE_TOKEN_CAP", "1000")

    with pytest.raises(ValidationError, match="llm_max_output_tokens must not exceed"):
        Settings()
