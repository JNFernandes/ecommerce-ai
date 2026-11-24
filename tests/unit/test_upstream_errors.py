"""Upstream exceptions map to fixed tool error results, the same way for every service."""

from collections.abc import Iterator
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from ecommerce_ai.infrastructure.caller import (
    CallerIdentity,
    RequestCaller,
    bind_caller,
    current_caller,
)
from ecommerce_ai.infrastructure.upstream import Forbidden, NotFound, Unauthenticated, UpstreamError
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure
from ecommerce_ai.tools.upstream_errors import map_upstream_errors

UNAVAILABLE = "inventory unavailable"


class _Shape(BaseModel):
    count: int


def _validation_error() -> ValidationError:
    try:
        _Shape.model_validate({"count": "many"})
    except ValidationError as error:
        return error
    raise AssertionError("expected a validation error")


@pytest.fixture(autouse=True)
def _caller() -> Iterator[None]:
    identity = CallerIdentity(
        subject="user-1",
        roles=frozenset({"CUSTOMER"}),
        customer_id=UUID("6f1c2b8e-4d3a-4f6e-9a7b-1c2d3e4f5a6b"),
    )
    caller = RequestCaller(identity=identity, token=SecretStr("t"), request_id="req-1")
    with bind_caller(caller):
        yield


def _fail(error: Exception, *, not_found: str | None = None) -> ToolFailure:
    with (
        pytest.raises(ToolFailure) as caught,
        map_upstream_errors(unavailable=UNAVAILABLE, not_found=not_found),
    ):
        raise error
    return caught.value


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (Forbidden("inventory refused access"), ToolErrorCode.NOT_ALLOWED, "not allowed"),
        (
            UpstreamError("inventory answered 503", retryable=True),
            ToolErrorCode.UPSTREAM_ERROR,
            UNAVAILABLE,
        ),
        (NotFound("inventory has no such resource"), ToolErrorCode.UPSTREAM_ERROR, UNAVAILABLE),
        (_validation_error(), ToolErrorCode.UPSTREAM_ERROR, UNAVAILABLE),
    ],
)
def test_should_map_upstream_exception_to_fixed_result_when_call_fails(
    error: Exception, code: ToolErrorCode, message: str
) -> None:
    failure = _fail(error)

    assert failure.code is code
    assert failure.result.message == message
    assert current_caller().upstream_unauthenticated is False


def test_should_report_not_found_with_tool_message_when_route_gives_404_a_meaning() -> None:
    failure = _fail(NotFound("inventory has no such resource"), not_found="product not found")

    assert failure.code is ToolErrorCode.NOT_FOUND
    assert failure.result.message == "product not found"


def test_should_flag_caller_when_upstream_rejects_the_token() -> None:
    failure = _fail(Unauthenticated("inventory rejected the token"))

    assert failure.code is ToolErrorCode.UPSTREAM_ERROR
    assert current_caller().upstream_unauthenticated is True


def test_should_pass_tool_failure_through_unchanged_when_raised_inside() -> None:
    original = ToolFailure(ToolErrorCode.NOT_FOUND)

    assert _fail(original) is original
