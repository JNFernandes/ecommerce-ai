"""Service configuration read from environment variables, with defaults for local development.

Every field maps to the upper-case environment variable of the same name (``PORT``,
``DATABASE_URL``...). Defaults point at the shared ``ecommerce-infra`` stack on localhost and the
platform APIs behind the gateway. No default holds a secret: credentials come from the
environment only (constitution II).
"""

from enum import StrEnum
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LogLevel(StrEnum):
    """Log levels accepted by ``LOG_LEVEL``."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def _split_csv(value: object) -> object:
    """Turn a comma-separated environment value into a list; leave other values untouched."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _require_http_url(value: str) -> str:
    """Accept only absolute http(s) URLs, without a trailing slash."""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("must be an absolute http(s) URL")
    return value.rstrip("/")


class Settings(BaseSettings):
    """Settings for the ecommerce-ai service."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=False)

    host: str = Field(
        default="127.0.0.1",
        min_length=1,
        description="Interface the HTTP server binds to (`0.0.0.0` in the container).",
    )
    port: int = Field(default=8002, ge=1, le=65535, description="HTTP port of the service.")
    log_level: LogLevel = Field(default=LogLevel.INFO, description="Root log level.")
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated origins allowed by CORS. Empty means no cross-origin access.",
    )

    identity_jwks_url: str = Field(
        default="http://localhost:3001/.well-known/jwks.json",
        description="ecommerce-identity JWKS, fetched lazily on first use and never at startup.",
    )
    jwt_issuer: str = Field(
        default="http://localhost:3001", min_length=1, description="Required `iss` claim."
    )
    jwt_audience: str = Field(default="ecommerce", min_length=1, description="Required `aud`.")
    jwt_clock_tolerance_seconds: int = Field(
        default=5, ge=0, le=300, description="Clock skew allowed on `exp` and `iat`."
    )

    orders_base_url: str = Field(
        default="http://localhost:8090/api/orders",
        description="Orders API base URL, through the gateway.",
    )
    inventory_base_url: str = Field(
        default="http://localhost:8090/api/inventory",
        description="Inventory API base URL, through the gateway.",
    )
    notifications_base_url: str = Field(
        default="http://localhost:8090/api",
        description="Notifications API base URL via the gateway; `/notifications` is appended.",
    )

    orders_graphql_url: str = Field(
        default="http://localhost:8090/api/graphql",
        description="Orders GraphQL endpoint (`order`, `orders` queries), through the gateway.",
    )

    jwks_timeout_seconds: float = Field(
        default=3.0, gt=0, le=30, description="Timeout of one JWKS fetch from Identity."
    )
    jwks_refetch_cooldown_seconds: float = Field(
        default=30.0,
        gt=0,
        le=3600,
        description="Minimum time between JWKS refetches triggered by an unknown `kid`.",
    )

    tool_timeout_seconds: float = Field(
        default=5.0, gt=0, le=60, description="Overall budget of one tool call, retries included."
    )
    tool_max_response_bytes: int = Field(
        default=262_144, gt=0, le=10_485_760, description="Largest upstream body a tool accepts."
    )
    tool_text_max_chars: int = Field(
        default=500, gt=0, le=10_000, description="Length limit of free-text fields in results."
    )
    upstream_max_retries: int = Field(
        default=2, ge=0, le=5, description="Retries after the first attempt of a read call."
    )
    upstream_backoff_base_seconds: float = Field(
        default=0.2, gt=0, le=5, description="First retry delay; doubles per retry, full jitter."
    )

    database_url: SecretStr = Field(
        default=SecretStr("postgresql://localhost:5436/ecommerce_ai"),
        description="PostgreSQL connection URL. Credentials come from the environment only.",
    )
    kafka_brokers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost:9092"],
        min_length=1,
        description="Comma-separated Kafka brokers (`host:port`).",
    )

    triage_consumer_enabled: bool = Field(
        default=True, description="Run the Kafka triage consumer as a background task."
    )
    kafka_consumer_group: str = Field(
        default="ai-triage", min_length=1, description="Consumer group of the triage consumer."
    )
    kafka_reconnect_backoff_base_seconds: float = Field(
        default=1.0, gt=0, le=60, description="First Kafka reconnect delay; doubles per attempt."
    )
    kafka_reconnect_backoff_max_seconds: float = Field(
        default=30.0, gt=0, le=600, description="Upper bound of one Kafka reconnect delay."
    )
    db_retry_max_attempts: int = Field(
        default=5, ge=1, le=20, description="Attempts of one database save, the first included."
    )
    db_retry_backoff_base_seconds: float = Field(
        default=0.5, gt=0, le=60, description="First database retry delay; doubles per retry."
    )
    db_retry_backoff_max_seconds: float = Field(
        default=10.0, gt=0, le=600, description="Upper bound of one database retry delay."
    )
    database_timeout_seconds: float = Field(
        default=5.0, gt=0, le=60, description="Connect and statement timeout of PostgreSQL."
    )
    consumer_shutdown_timeout_seconds: float = Field(
        default=10.0, gt=0, le=120, description="Time the consumer gets to stop on shutdown."
    )

    anthropic_model: str | None = Field(
        default=None,
        description="LLM model id. Unset means every LLM-backed step uses its fallback.",
    )
    llm_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300, description="Timeout of one LLM call."
    )
    daily_token_budget: int = Field(
        default=50_000, ge=0, description="Hard cap on LLM tokens per UTC day (0 disables LLM)."
    )

    @field_validator("cors_allowed_origins", "kafka_brokers", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator(
        "identity_jwks_url",
        "orders_base_url",
        "orders_graphql_url",
        "inventory_base_url",
        "notifications_base_url",
    )
    @classmethod
    def _check_http_url(cls, value: str) -> str:
        return _require_http_url(value)

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"postgresql", "postgres"} or not parts.hostname:
            raise ValueError("must be a postgresql:// URL with a host")
        return value

    @field_validator("kafka_brokers")
    @classmethod
    def _check_brokers(cls, value: list[str]) -> list[str]:
        for broker in value:
            host, _, port = broker.rpartition(":")
            if not host or not port.isdigit():
                raise ValueError(f"broker {broker!r} must be host:port")
        return value

    @field_validator("anthropic_model", mode="before")
    @classmethod
    def _blank_model_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> Self:
        """Each backoff base must not exceed its maximum."""
        pairs = (
            (
                "kafka_reconnect_backoff",
                self.kafka_reconnect_backoff_base_seconds,
                self.kafka_reconnect_backoff_max_seconds,
            ),
            (
                "db_retry_backoff",
                self.db_retry_backoff_base_seconds,
                self.db_retry_backoff_max_seconds,
            ),
        )
        for prefix, base, maximum in pairs:
            if base > maximum:
                raise ValueError(f"{prefix}_base_seconds must not exceed {prefix}_max_seconds")
        return self
