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


class ReadinessResponse(BaseModel):
    """Readiness of the service and the state of its dependencies."""

    model_config = ConfigDict(frozen=True)

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
        "is reported but never makes the service unready, nor does the triage consumer's "
        "state. Never affects startup."
    ),
    openapi_extra=PUBLIC,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    checker: Annotated[ReadinessChecker, Depends(get_readiness_checker)],
    consumer_state: Annotated[ConsumerStateSource | None, Depends(get_consumer_state)],
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
    return ReadinessResponse(
        status=report.status, dependencies=report.dependencies, consumer=consumer
    )
