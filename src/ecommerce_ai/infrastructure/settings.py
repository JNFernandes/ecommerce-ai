"""Service configuration read from environment variables, with defaults for local development.

Every field maps to the upper-case environment variable of the same name (``PORT``,
``DATABASE_URL``...). Defaults point at the shared ``ecommerce-infra`` stack on localhost and the
platform APIs behind the gateway. No default holds a secret: credentials come from the
environment only (constitution II), or from a git-ignored ``.env`` file in the working directory,
which is read when present; real environment variables always win over it.
"""

from enum import StrEnum
from typing import Annotated, Final, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LogLevel(StrEnum):
    """Log levels accepted by ``LOG_LEVEL``."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LlmEffort(StrEnum):
    """Reasoning effort requested from the model (``LLM_EFFORT``)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


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


ENV_FILE: Final = ".env"
"""Optional file of ``NAME=value`` lines, relative to the working directory."""

DATABASE_SCHEMES: Final = frozenset({"postgresql", "postgres", "postgresql+asyncpg"})
"""Accepted ``DATABASE_URL`` schemes; the engine always connects with asyncpg."""


class Settings(BaseSettings):
    """Settings for the ecommerce-ai service."""

    model_config = SettingsConfigDict(
        frozen=True,
        extra="ignore",
        case_sensitive=False,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        hide_input_in_errors=True,  # a rejected DATABASE_URL must not echo its password
    )

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

    triage_worker_enabled: bool = Field(
        default=True,
        description="Run the triage worker and the outbox relay as background tasks.",
    )
    triage_settle_seconds: float = Field(
        default=3.0,
        ge=0,
        le=300,
        description="Quiet time after a case's last event before it is triaged.",
    )
    triage_poll_interval_seconds: float = Field(
        default=1.0, gt=0, le=60, description="Wait of the triage worker when no case is settled."
    )
    triage_backoff_base_seconds: float = Field(
        default=1.0, gt=0, le=60, description="First worker delay after a database failure."
    )
    triage_backoff_max_seconds: float = Field(
        default=30.0, gt=0, le=600, description="Upper bound of one worker failure delay."
    )

    anthropic_api_key: SecretStr | None = Field(
        default=None,
        description="Anthropic API key. Unset means every LLM-backed step uses its fallback.",
    )
    anthropic_model: str | None = Field(
        default=None,
        description="LLM model id. Unset means every LLM-backed step uses its fallback.",
    )
    llm_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300, description="Timeout of one LLM call."
    )
    llm_max_retries: int = Field(
        default=2, ge=0, le=5, description="LLM retries after the first attempt of one draft."
    )
    llm_max_output_tokens: int = Field(
        default=1024, gt=0, le=32_000, description="Output token limit of one LLM call."
    )
    llm_effort: LlmEffort = Field(
        default=LlmEffort.LOW, description="Reasoning effort requested from the model."
    )
    case_token_cap: int = Field(
        default=8000, gt=0, description="Most LLM tokens one triage case may spend."
    )
    daily_token_budget: int = Field(
        default=50_000, ge=0, description="Hard cap on LLM tokens per UTC day (0 disables LLM)."
    )

    outbox_max_attempts: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Publish attempts of one outbox event before giving up.",
    )
    outbox_backoff_base_seconds: float = Field(
        default=1.0, gt=0, le=60, description="First delay after a failed publish; doubles per try."
    )
    outbox_backoff_max_seconds: float = Field(
        default=60.0, gt=0, le=3600, description="Upper bound of one publish retry delay."
    )
    outbox_poll_interval_seconds: float = Field(
        default=1.0, gt=0, le=60, description="Wait of the outbox relay when nothing is due."
    )

    chat_keepalive_seconds: float = Field(
        default=15.0,
        gt=0,
        le=300,
        description="Silence on a chat stream after which a `: keep-alive` comment is sent.",
    )
    chat_request_token_cap: int = Field(
        default=20_000, gt=0, description="Most LLM tokens one chat message may spend."
    )
    chat_rate_limit_messages: int = Field(
        default=10, gt=0, le=1000, description="Chat messages one user may post per window."
    )
    chat_rate_limit_window_seconds: float = Field(
        default=60.0, gt=0, le=3600, description="Sliding window of the chat rate limit."
    )
    chat_max_tool_rounds: int = Field(
        default=5, gt=0, le=20, description="Most model→tool rounds in one chat answer."
    )
    chat_max_messages: int = Field(
        default=100,
        ge=2,
        le=10_000,
        description="Most messages (user + assistant) one conversation may hold.",
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
        if parts.scheme not in DATABASE_SCHEMES or not parts.hostname:
            # Never echo the value: it may hold a password.
            raise ValueError("must be a postgresql:// (or postgresql+asyncpg://) URL with a host")
        return value

    @field_validator("kafka_brokers")
    @classmethod
    def _check_brokers(cls, value: list[str]) -> list[str]:
        for broker in value:
            host, _, port = broker.rpartition(":")
            if not host or not port.isdigit():
                raise ValueError(f"broker {broker!r} must be host:port")
        return value

    @field_validator("anthropic_model", "anthropic_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def model_configured(self) -> bool:
        """True when both the API key and the model id are set, so the LLM may be called."""
        return self.anthropic_api_key is not None and self.anthropic_model is not None

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
            (
                "triage_backoff",
                self.triage_backoff_base_seconds,
                self.triage_backoff_max_seconds,
            ),
            (
                "outbox_backoff",
                self.outbox_backoff_base_seconds,
                self.outbox_backoff_max_seconds,
            ),
        )
        for prefix, base, maximum in pairs:
            if base > maximum:
                raise ValueError(f"{prefix}_base_seconds must not exceed {prefix}_max_seconds")
        return self

    @model_validator(mode="after")
    def _check_token_limits(self) -> Self:
        """One LLM call must fit in the per-case and the per-chat-message token caps."""
        if self.llm_max_output_tokens > self.case_token_cap:
            raise ValueError("llm_max_output_tokens must not exceed case_token_cap")
        if self.llm_max_output_tokens > self.chat_request_token_cap:
            raise ValueError("llm_max_output_tokens must not exceed chat_request_token_cap")
        return self
