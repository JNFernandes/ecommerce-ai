"""Sanity checks for the shared test helpers, so later suites can trust them."""

import asyncio
from collections.abc import Callable

import httpx
import jwt
import pytest
import respx

from tests.support.mcp import mcp_headers, tool_result, tools_call_request
from tests.support.orders_stub import ORDERS_GRAPHQL_URL, OrdersStub, sample_order
from tests.support.tokens import (
    AUDIENCE,
    ISSUER,
    UNKNOWN_KID,
    foreign_key,
    jwks,
    make_token,
    tamper,
)


def _decode(token: str) -> dict[str, object]:
    key = jwt.PyJWK(jwks()["keys"][0])
    claims: dict[str, object] = jwt.decode(
        token, key, algorithms=["RS256"], issuer=ISSUER, audience=AUDIENCE
    )
    return claims


def test_should_verify_default_token_against_published_jwks() -> None:
    claims = _decode(make_token())

    assert claims["roles"] == ["CUSTOMER"]
    assert "customerId" in claims


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: tamper(make_token()), id="tampered"),
        pytest.param(lambda: make_token(key=foreign_key()), id="foreign-key"),
        pytest.param(lambda: make_token(iss="https://evil.example"), id="wrong-issuer"),
    ],
)
def test_should_fail_verification_when_token_is_bad(token: Callable[[], str]) -> None:
    with pytest.raises(jwt.PyJWTError):
        _decode(token())


def test_should_build_hs256_none_and_unknown_kid_tokens() -> None:
    assert jwt.get_unverified_header(make_token(alg="HS256"))["alg"] == "HS256"
    assert jwt.get_unverified_header(make_token(alg="none"))["alg"] == "none"
    assert jwt.get_unverified_header(make_token(kid=UNKNOWN_KID))["kid"] == UNKNOWN_KID
    assert "sub" not in jwt.decode(make_token(omit=["sub"]), options={"verify_signature": False})


async def test_should_answer_and_record_requests_when_stub_is_configured() -> None:
    with respx.mock(assert_all_called=False) as router:
        stub = OrdersStub(router)
        stub.respond_order(sample_order())
        async with httpx.AsyncClient() as client:
            response = await client.post(
                ORDERS_GRAPHQL_URL, json={"query": "q"}, headers={"Authorization": "Bearer t"}
            )

    assert response.json()["data"]["order"]["orderId"] == sample_order()["orderId"]
    assert stub.requests[0].headers["Authorization"] == "Bearer t"
    assert stub.request_body() == {"query": "q"}


async def test_should_never_answer_when_stub_hangs() -> None:
    with respx.mock(assert_all_called=False) as router:
        OrdersStub(router).respond_hang()
        async with httpx.AsyncClient() as client:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await client.post(ORDERS_GRAPHQL_URL, json={})


def test_should_parse_tool_result_and_build_requests() -> None:
    response = httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "structuredContent": {}}},
    )

    assert tool_result(response) == (True, {})
    assert tools_call_request("get_order")["method"] == "tools/call"
    assert "Authorization" not in mcp_headers(None)
