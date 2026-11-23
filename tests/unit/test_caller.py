"""The request caller lives in a ContextVar for exactly one request and never shows its token."""

import asyncio
import uuid

import pytest
from pydantic import SecretStr

from ecommerce_ai.infrastructure.caller import (
    CallerIdentity,
    NoCallerError,
    RequestCaller,
    bind_caller,
    current_caller,
)

TOKEN = "header.payload.signature"  # noqa: S105  # fake, test-only


def _caller() -> RequestCaller:
    identity = CallerIdentity(
        subject="user-1", roles=frozenset({"CUSTOMER"}), customer_id=uuid.uuid4()
    )
    return RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id="req-1")


def test_should_return_the_bound_caller_inside_the_context() -> None:
    caller = _caller()

    with bind_caller(caller) as bound:
        assert bound is caller
        assert current_caller() is caller


def test_should_reset_the_caller_when_the_context_exits() -> None:
    with bind_caller(_caller()):
        pass

    with pytest.raises(NoCallerError):
        current_caller()


def test_should_reset_the_caller_when_the_context_raises() -> None:
    with pytest.raises(ValueError, match="boom"), bind_caller(_caller()):
        raise ValueError("boom")

    with pytest.raises(NoCallerError):
        current_caller()


def test_should_raise_when_no_caller_is_bound() -> None:
    with pytest.raises(NoCallerError):
        current_caller()


async def test_should_isolate_callers_between_concurrent_tasks() -> None:
    async def subject_of(caller: RequestCaller) -> str:
        with bind_caller(caller):
            await asyncio.sleep(0)  # yield to the other task while bound
            return current_caller().identity.subject

    first = _caller()
    second = RequestCaller(
        identity=CallerIdentity(subject="user-2", roles=frozenset(), customer_id=None),
        token=SecretStr("other"),
        request_id="req-2",
    )

    subjects = await asyncio.gather(subject_of(first), subject_of(second))

    assert list(subjects) == ["user-1", "user-2"]


def test_should_keep_the_token_secret_and_out_of_repr() -> None:
    caller = _caller()

    assert isinstance(caller.token, SecretStr)
    assert caller.token.get_secret_value() == TOKEN
    assert TOKEN not in repr(caller)
    assert TOKEN not in str(caller)


def test_should_start_without_upstream_unauthenticated_flag() -> None:
    caller = _caller()

    caller.upstream_unauthenticated = True

    assert _caller().upstream_unauthenticated is False
    assert caller.upstream_unauthenticated is True


def test_should_freeze_the_identity() -> None:
    identity = _caller().identity

    with pytest.raises(ValueError, match="frozen"):
        identity.subject = "someone-else"  # type: ignore[misc]  # asserting immutability
