"""``require_caller``: any token with a role; the same 401/403/503 as ``/mcp`` (research §14)."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI

from ecommerce_ai.api.auth import RejectReason, rejection_response, require_caller
from ecommerce_ai.api.middleware import RequestIdMiddleware
from ecommerce_ai.api.route_policy import AUTHENTICATED, assert_route_policies
from ecommerce_ai.infrastructure.auth import TokenVerifier
from ecommerce_ai.infrastructure.caller import NoCallerError, RequestCaller, current_caller
from ecommerce_ai.infrastructure.jwks import JwksCache
from ecommerce_ai.infrastructure.settings import Settings
from tests.support.tokens import jwks, make_token, tamper

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
UNAUTHENTICATED_BODY = rejection_response(RejectReason.INVALID_TOKEN).body


@asynccontextmanager
async def _client() -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings()
    async with httpx.AsyncClient() as http:
        keys = JwksCache(http, JWKS_URL, timeout=1, refetch_cooldown=1)
        verifier = TokenVerifier(
            keys, issuer=settings.jwt_issuer, audience=settings.jwt_audience, leeway=0
        )
        app = FastAPI()
        caller_dependency = require_caller(verifier)

        @app.get("/whoami", openapi_extra=AUTHENTICATED)
        async def whoami(
            caller: Annotated[RequestCaller, Depends(caller_dependency)],
        ) -> dict[str, str]:
            bound = current_caller()
            assert bound is caller
            return {"subject": caller.identity.subject, "requestId": caller.request_id}

        assert_route_policies(app)
        transport = httpx.ASGITransport(app=RequestIdMiddleware(app))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="test").pass_through()
        yield router.get(JWKS_URL).respond(json=jwks())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.usefixtures("identity")
async def test_should_bind_the_caller_when_the_token_has_any_role() -> None:
    async with _client() as client:
        response = await client.get(
            "/whoami", headers={**_bearer(make_token(roles=["ADMIN"])), "X-Request-Id": "req-7"}
        )

    assert response.status_code == 200
    assert response.json() == {"subject": "user-1", "requestId": "req-7"}
    with pytest.raises(NoCallerError):
        current_caller()


@pytest.mark.usefixtures("identity")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        _bearer(make_token(expires_in=timedelta(minutes=-5))),
        _bearer(tamper(make_token(), sub="someone-else")),
        _bearer(make_token(alg="HS256")),
    ],
    ids=["missing", "basic", "empty", "expired", "tampered", "hs256"],
)
async def test_should_answer_the_mcp_401_when_the_token_is_missing_or_bad(
    headers: dict[str, str],
) -> None:
    async with _client() as client:
        response = await client.get("/whoami", headers=headers)

    assert response.status_code == 401
    assert response.content == UNAUTHENTICATED_BODY
    assert response.json() == {"detail": "Not authenticated"}
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.usefixtures("identity")
async def test_should_answer_403_when_the_token_has_no_role() -> None:
    async with _client() as client:
        response = await client.get("/whoami", headers=_bearer(make_token(roles=[])))

    assert response.status_code == 403
    assert response.content == rejection_response(RejectReason.MISSING_ROLE).body


async def test_should_answer_503_when_no_signing_key_can_be_obtained() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.route(host="test").pass_through()
        router.get(JWKS_URL).mock(side_effect=httpx.ConnectError("identity down"))
        async with _client() as client:
            response = await client.get("/whoami", headers=_bearer(make_token()))

    assert response.status_code == 503
    assert response.content == rejection_response(RejectReason.KEYS_UNAVAILABLE).body
