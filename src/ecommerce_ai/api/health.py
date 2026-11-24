"""Liveness and readiness endpoints: public, they never need an access token."""

from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ecommerce_ai.api.route_policy import PUBLIC
from ecommerce_ai.infrastructure.health import DependencyStatus, ReadinessChecker


class LivenessResponse(BaseModel):
    """Liveness of the process."""

    model_config = ConfigDict(frozen=True)

    status: DependencyStatus = Field(description="Always `UP` when the process answers.")


class ConsumerStateSource(Protocol):
    """Live state of the triage consumer, as kept by messaging (read only here)."""

    connected: bool
    caught_up: bool
    last_error: str | None


class ConsumerStatus(BaseModel):
    """State of the Kafka triage consumer; informative only, it never changes readiness."""

    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)

    connected: bool = Field(description="Whether the consumer is connected to Kafka.")
    caught_up: bool = Field(description="Whether every record of its partitions has been read.")
    last_error: str | None = Field(
        description="Class name of the last error (never its message), or `null`."
    )


class WorkerStateSource(Protocol):
    """Live state of the triage worker, as kept by agents (read only here)."""

    model_configured: bool
    running: bool
    last_error: str | None


class RelayStateSource(Protocol):
    """Live state of the outbox relay, as kept by messaging (read only here)."""

    running: bool
    last_error: str | None


class TriageWorkerStatus(BaseModel):
    """State of the triage worker; informative only, it never changes readiness."""

    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)

    running: bool = Field(description="Whether the worker loop is running.")
    last_error: str | None = Field(
        description="Class name of the last error (never its message), or `null`."
    )
    model_configured: bool = Field(
        description="Whether an LLM is configured; without one every case uses its template."
    )


class OutboxRelayStatus(BaseModel):
    """State of the outbox relay; informative only, it never changes readiness."""

    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)

    running: bool = Field(description="Whether the relay is connected to Kafka and running.")
    last_error: str | None = Field(
        description="Class name of the last error (never its message), or `null`."
    )


class ReadinessResponse(BaseModel):
    """Readiness of the service and the state of its dependencies."""

    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)

    status: DependencyStatus = Field(
        description="`UP` when PostgreSQL is reachable. Kafka never makes the service unready."
    )
    dependencies: dict[str, DependencyStatus] = Field(
        description="State of each dependency, by name (`database`, `kafka`)."
    )
    consumer: ConsumerStatus | None = Field(
        default=None,
        description="State of the triage consumer; `null` when it is disabled. Never affects "
        "`status` or the HTTP code.",
    )
    triage_worker: TriageWorkerStatus | None = Field(
        default=None,
        description="State of the triage worker; `null` when it is disabled. Never affects "
        "`status` or the HTTP code.",
    )
    outbox_relay: OutboxRelayStatus | None = Field(
        default=None,
        description="State of the outbox relay; `null` when the worker is disabled. Never "
        "affects `status` or the HTTP code.",
    )


def get_readiness_checker(request: Request) -> ReadinessChecker:
    """Return the readiness checker created with the application."""
    checker = getattr(request.app.state, "readiness_checker", None)
    if not isinstance(checker, ReadinessChecker):
        raise RuntimeError("ReadinessChecker is not configured on the application state")
    return checker


def get_consumer_state(request: Request) -> ConsumerStateSource | None:
    """Return the triage consumer's state, or ``None`` when the consumer is disabled."""
    state: ConsumerStateSource | None = getattr(request.app.state, "consumer_state", None)
    return state


def get_worker_state(request: Request) -> WorkerStateSource | None:
    """Return the triage worker's state, or ``None`` when the worker is disabled."""
    state: WorkerStateSource | None = getattr(request.app.state, "worker_state", None)
    return state


def get_relay_state(request: Request) -> RelayStateSource | None:
    """Return the outbox relay's state, or ``None`` when the worker is disabled."""
    state: RelayStateSource | None = getattr(request.app.state, "relay_state", None)
    return state


router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness probe",
    description="Answers `UP` whenever the process can serve HTTP requests. No dependency checks.",
    openapi_extra=PUBLIC,
)
def liveness() -> LivenessResponse:
    """Report that the process is alive."""
    return LivenessResponse(status=DependencyStatus.UP)


@router.get(
    "/ready",
    summary="Readiness probe",
    description=(
        "Reports the state of each dependency. Answers 503 when PostgreSQL is unreachable; Kafka "
        "is reported but never makes the service unready, nor does the state of the triage "
        "consumer, the triage worker or the outbox relay. Never affects startup."
    ),
    openapi_extra=PUBLIC,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    checker: Annotated[ReadinessChecker, Depends(get_readiness_checker)],
    consumer_state: Annotated[ConsumerStateSource | None, Depends(get_consumer_state)],
    worker_state: Annotated[WorkerStateSource | None, Depends(get_worker_state)],
    relay_state: Annotated[RelayStateSource | None, Depends(get_relay_state)],
    response: Response,
) -> ReadinessResponse:
    """Report whether the service can handle requests."""
    report = await checker.check()
    if report.status is not DependencyStatus.UP:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    consumer = (
        None
        if consumer_state is None
        else ConsumerStatus(
            connected=consumer_state.connected,
            caught_up=consumer_state.caught_up,
            last_error=consumer_state.last_error,
        )
    )
    worker = (
        None
        if worker_state is None
        else TriageWorkerStatus(
            running=worker_state.running,
            last_error=worker_state.last_error,
            model_configured=worker_state.model_configured,
        )
    )
    relay = (
        None
        if relay_state is None
        else OutboxRelayStatus(running=relay_state.running, last_error=relay_state.last_error)
    )
    return ReadinessResponse(
        status=report.status,
        dependencies=report.dependencies,
        consumer=consumer,
        triage_worker=worker,
        outbox_relay=relay,
    )
