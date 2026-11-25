"""Default deny covers FastAPI routes, plain ASGI routes and mounts alike."""

import pytest
from fastapi import APIRouter, FastAPI
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from ecommerce_ai.api.route_policy import (
    AUTHENTICATED,
    PUBLIC,
    PolicyMount,
    PolicyRoute,
    RoutePolicyError,
    assert_route_policies,
    roles,
)


async def _asgi(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("ok")(scope, receive, send)


def test_should_pass_when_app_has_only_documentation_routes() -> None:
    assert_route_policies(FastAPI())


def test_should_raise_when_asgi_route_has_no_policy() -> None:
    app = FastAPI()
    app.router.routes.append(Route("/raw", _asgi, methods=["POST"]))

    with pytest.raises(RoutePolicyError, match="POST /raw"):
        assert_route_policies(app)


def test_should_raise_when_mount_has_no_policy() -> None:
    app = FastAPI()
    app.router.routes.append(Mount("/sub", app=_asgi))

    with pytest.raises(RoutePolicyError, match="MOUNT /sub"):
        assert_route_policies(app)


@pytest.mark.parametrize(
    "extra", [{}, {"x-roles": []}, {"x-public": False}, {"x-authenticated": False}]
)
def test_should_raise_when_policy_route_declares_nothing(extra: dict[str, object]) -> None:
    app = FastAPI()
    app.router.routes.append(PolicyRoute("/raw", _asgi, openapi_extra=extra))

    with pytest.raises(RoutePolicyError):
        assert_route_policies(app)


def test_should_pass_when_asgi_route_and_mount_declare_roles() -> None:
    app = FastAPI()
    route = PolicyRoute("/mcp", _asgi, methods=["POST"], openapi_extra=roles("CUSTOMER"))
    app.router.routes.append(route)
    app.router.routes.append(PolicyMount("/sub", _asgi, openapi_extra=PUBLIC))

    assert_route_policies(app)
    assert route.openapi_extra == {"x-roles": ["CUSTOMER"]}


def test_should_raise_when_api_route_has_no_policy() -> None:
    app = FastAPI()

    @app.get("/undeclared")
    async def undeclared() -> str:
        return "x"

    with pytest.raises(RoutePolicyError, match="GET /undeclared"):
        assert_route_policies(app)


def test_should_pass_when_api_routes_declare_public_or_roles() -> None:
    app = FastAPI()

    @app.get("/open", openapi_extra=PUBLIC)
    async def open_route() -> str:
        return "x"

    @app.get("/closed", openapi_extra=roles("ADMIN"))
    async def closed_route() -> str:
        return "x"

    assert_route_policies(app)


def test_should_raise_when_included_router_has_an_undeclared_route() -> None:
    router = APIRouter()

    @router.get("/hidden")
    async def hidden() -> str:
        return "x"

    @router.get("/shown", openapi_extra=PUBLIC)
    async def shown() -> str:
        return "x"

    app = FastAPI()
    app.include_router(router, prefix="/v1")

    with pytest.raises(RoutePolicyError, match="GET /v1/hidden") as caught:
        assert_route_policies(app)
    assert "/v1/shown" not in str(caught.value)


def test_should_pass_when_api_route_declares_authenticated_access() -> None:
    app = FastAPI()

    @app.get("/mine", openapi_extra=AUTHENTICATED)
    async def mine() -> str:
        return "x"

    assert_route_policies(app)
    assert AUTHENTICATED == {"x-authenticated": True}
