"""``/mcp`` admits only valid RS256 Identity tokens carrying ``CUSTOMER`` (US3, SC-001)."""

from collections.abc import Iterator, Sequence
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from ecommerce_ai import main
from ecommerce_ai.api.mcp import BearerAuthMiddleware
from ecommerce_ai.infrastructure.auth import TokenVerifier
from ecommerce_ai.infrastructure.caller import NoCallerError, RequestCaller, current_caller
from ecommerce_ai.infrastructure.jwks import JwksCache
from ecommerce_ai.infrastructure.orders_client import OrdersClient
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from ecommerce_ai.tools.registry import AllowlistEntry, AnyToolDefinition, ToolDefinition
from tests.support.mcp import (
    post_rpc,
    rpc_result,
    serve_app,
    tool_result,
    tools_call_request,
    tools_list_request,
)
from tests.support.tokens import UNKNOWN_KID, foreign_key, jwks, make_token, tamper

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default


class NoInput(BaseModel):
    pass


class WhoAmI(BaseModel):
    subject: str


class Recorder:
    """A ``whoami`` tool that records the caller bound while it runs."""

    def __init__(self) -> None:
        self.callers: list[RequestCaller] = []

    async def handler(self, _arguments: NoInput) -> WhoAmI:
        caller = current_caller()
        self.callers.append(caller)
        return WhoAmI(subject=caller.identity.subject)

    def definitions(
        self,
        _settings: Settings,
        _orders: OrdersClient,
        _inventory: RestClient,
        _notifications: RestClient,
    ) -> Sequence[AnyToolDefinition]:
        return [
            ToolDefinition(
                name="whoami",
                description="Return the caller's subject.",
                allowlist_entry=AllowlistEntry("ecommerce-orders", "POST", "/api/graphql", "order"),
                input_model=NoInput,
                output_model=WhoAmI,
                timeout_seconds=1.0,
                max_response_bytes=1024,
                handler=self.handler,
            )
        ]


@pytest.fixture
def identity() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.get(JWKS_URL).respond(json=jwks())


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(main, "build_tool_definitions", recorder.definitions)
    return recorder


@pytest.fixture
def app(identity: respx.Route, recorder: Recorder) -> FastAPI:
    return create_app(probes=[])


async def _post(client: httpx.AsyncClient, headers: dict[str, str]) -> httpx.Response:
    request = tools_call_request("whoami")
    base = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    return await client.post("/mcp", json=request, headers={**base, **headers})


BAD_AUTHORIZATION = {
    "no header": None,
    "empty": "",
    "not bearer": f"Basic {make_token()}",
    "bearer without token": "Bearer ",
    "bearer with two parts": f"Bearer {make_token()} extra",
    "expired": f"Bearer {make_token(expires_in=timedelta(minutes=-10))}",
    "wrong issuer": f"Bearer {make_token(iss='http://evil.test')}",
    "wrong audience": f"Bearer {make_token(aud='other')}",
    "unknown kid": f"Bearer {make_token(kid=UNKNOWN_KID)}",
    "foreign key": f"Bearer {make_token(key=foreign_key())}",
    "HS256": f"Bearer {make_token(alg='HS256')}",
    "alg none": f"Bearer {make_token(alg='none')}",
    "tampered": f"Bearer {tamper(make_token())}",
    "missing roles": f"Bearer {make_token(omit=['roles'])}",
    "garbage": "Bearer not-a-jwt",
}


async def test_should_answer_one_identical_401_when_token_is_missing_or_invalid(
    app: FastAPI, recorder: Recorder
) -> None:
    responses = []
    async with serve_app(app) as client:
        for authorization in BAD_AUTHORIZATION.values():
            headers = {} if authorization is None else {"Authorization": authorization}
            responses.append(await _post(client, headers))

    first = responses[0]
    assert first.status_code == 401
    assert first.headers["WWW-Authenticate"] == "Bearer"
    assert first.json() == {"detail": "Not authenticated"}
    for name, response in zip(BAD_AUTHORIZATION, responses, strict=True):
        assert response.status_code == 401, name
        assert response.content == first.content, name
        assert _stable_headers(response) == _stable_headers(first), name
    assert recorder.callers == []


async def test_should_answer_503_when_no_signing_key_can_be_obtained(
    recorder: Recorder,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).mock(side_effect=httpx.ConnectError("identity down"))
        async with serve_app(create_app(probes=[])) as client:
            response = await post_rpc(client, tools_list_request(), make_token())

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    assert recorder.callers == []


async def test_should_answer_403_and_run_no_tool_when_token_lacks_customer_role(
    app: FastAPI, recorder: Recorder
) -> None:
    token = make_token(roles=["ADMIN"], omit=["customerId"])

    async with serve_app(app) as client:
        response = await post_rpc(client, tools_call_request("whoami"), token)

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert recorder.callers == []


async def test_should_list_tools_when_customer_token_is_valid(app: FastAPI) -> None:
    async with serve_app(app) as client:
        response = await post_rpc(client, tools_list_request(), make_token())

    assert response.status_code == 200
    tools = rpc_result(response)["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == ["whoami"]


async def test_should_bind_caller_during_the_call_and_reset_it_afterwards(
    app: FastAPI, recorder: Recorder
) -> None:
    token = make_token(sub="user-42")

    async with serve_app(app) as client:
        response = await post_rpc(
            client, tools_call_request("whoami"), token, {"X-Request-Id": "req-auth-1"}
        )

    assert tool_result(response) == (False, {"subject": "user-42"})
    (caller,) = recorder.callers
    assert caller.token.get_secret_value() == token
    assert caller.request_id == "req-auth-1"
    with pytest.raises(NoCallerError):
        current_caller()


async def test_should_reach_auth_without_redirect_when_posting_to_mcp_without_slash(
    app: FastAPI,
) -> None:
    async with serve_app(app) as client:
        response = await _post(client, {})

    assert response.status_code == 401
    assert not response.is_redirect


async def test_should_fetch_keys_lazily_when_the_first_mcp_request_arrives(
    app: FastAPI, identity: respx.Route
) -> None:
    async with serve_app(app) as client:
        assert identity.call_count == 0
        await client.get("/health")
        assert identity.call_count == 0
        await post_rpc(client, tools_list_request(), make_token())

    assert identity.call_count == 1


def test_should_declare_customer_role_on_the_mcp_route(app: FastAPI) -> None:
    (route,) = [route for route in app.routes if getattr(route, "path", None) == "/mcp"]

    assert getattr(route, "openapi_extra", None) == {"x-roles": ["CUSTOMER"]}


async def test_should_pass_non_http_scopes_through_untouched() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        raise AssertionError("not called")  # pragma: no cover

    async def send(_message: Message) -> None:
        raise AssertionError("not called")  # pragma: no cover

    async with httpx.AsyncClient() as http:
        keys = JwksCache(http, JWKS_URL, timeout=1, refetch_cooldown=1)
        verifier = TokenVerifier(keys, issuer="i", audience="a", leeway=0)
        await BearerAuthMiddleware(inner, verifier)({"type": "websocket"}, receive, send)

    assert seen == ["websocket"]


def _stable_headers(response: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() != "x-request-id"}
