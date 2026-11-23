"""Liveness and readiness endpoints: public, they never need an access token."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ecommerce_ai.api.route_policy import PUBLIC
from ecommerce_ai.infrastructure.health import DependencyStatus, ReadinessChecker


class LivenessResponse(BaseModel):
    """Liveness of the process."""

    model_config = ConfigDict(frozen=True)

    status: DependencyStatus = Field(description="Always `UP` when the process answers.")


class ReadinessResponse(BaseModel):
    """Readiness of the service and the state of its dependencies."""

    model_config = ConfigDict(frozen=True)

    status: DependencyStatus = Field(
        description="`UP` when PostgreSQL is reachable. Kafka never makes the service unready."
    )
    dependencies: dict[str, DependencyStatus] = Field(
        description="State of each dependency, by name (`database`, `kafka`)."
    )


def get_readiness_checker(request: Request) -> ReadinessChecker:
    """Return the readiness checker created with the application."""
    checker = getattr(request.app.state, "readiness_checker", None)
    if not isinstance(checker, ReadinessChecker):
        raise RuntimeError("ReadinessChecker is not configured on the application state")
    return checker


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
        "is reported but never makes the service unready. Never affects startup."
    ),
    openapi_extra=PUBLIC,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    checker: Annotated[ReadinessChecker, Depends(get_readiness_checker)], response: Response
) -> ReadinessResponse:
    """Report whether the service can handle requests."""
    report = await checker.check()
    if report.status is not DependencyStatus.UP:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status=report.status, dependencies=report.dependencies)
