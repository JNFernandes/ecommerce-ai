"""Default deny: every route must declare whether it is public or which roles it requires.

A route declares its access through ``openapi_extra`` (``x-public`` or ``x-roles``). FastAPI
routes take it as an argument; plain ASGI routes and mounts (the MCP endpoint) use
``PolicyRoute`` / ``PolicyMount``, which carry the same attribute. Building the application
fails for any route that declares neither (constitution II). FastAPI's own documentation routes
are the only exemption.
"""

from collections.abc import Mapping
from typing import Final

from fastapi import FastAPI
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp

PUBLIC: Final[dict[str, object]] = {"x-public": True}
"""``openapi_extra`` of a route that needs no access token (health, readiness)."""


def roles(*names: str) -> dict[str, object]:
    """Return the ``openapi_extra`` of a route that requires one of the role ``names``."""
    return {"x-roles": list(names)}


class RoutePolicyError(RuntimeError):
    """A route does not declare its access."""


class PolicyRoute(Route):
    """An ASGI ``Route`` that declares its access like a FastAPI route."""

    def __init__(
        self,
        path: str,
        endpoint: ASGIApp,
        *,
        openapi_extra: Mapping[str, object],
        methods: list[str] | None = None,
        name: str | None = None,
    ) -> None:
        """Route ``path`` to ``endpoint`` with the access declared in ``openapi_extra``."""
        super().__init__(path, endpoint, methods=methods, name=name)
        self.openapi_extra: dict[str, object] = dict(openapi_extra)


class PolicyMount(Mount):
    """An ASGI ``Mount`` that declares its access like a FastAPI route."""

    def __init__(
        self,
        path: str,
        app: ASGIApp,
        *,
        openapi_extra: Mapping[str, object],
        name: str | None = None,
    ) -> None:
        """Mount ``app`` at ``path`` with the access declared in ``openapi_extra``."""
        super().__init__(path, app=app, name=name)
        self.openapi_extra: dict[str, object] = dict(openapi_extra)


def _is_declared(route: BaseRoute) -> bool:
    extra = getattr(route, "openapi_extra", None)
    return isinstance(extra, dict) and bool(extra.get("x-public") or extra.get("x-roles"))


def _describe(context: RouteContext) -> str:
    path = context.path or repr(context.original_route)
    if isinstance(context.original_route, Mount):
        return f"MOUNT {path}"
    return f"{','.join(sorted(context.methods or ()))} {path}"


def _documentation_paths(app: FastAPI) -> set[str]:
    candidates = (app.openapi_url, app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url)
    return {path for path in candidates if path}


def assert_route_policies(app: FastAPI) -> None:
    """Raise ``RoutePolicyError`` naming every route or mount without an access declaration.

    Routes of included routers are checked one by one, with their full path.
    """
    exempt = _documentation_paths(app)
    undeclared = [
        _describe(context)
        for context in iter_route_contexts(app.routes)
        if context.path not in exempt and not _is_declared(context.original_route)
    ]
    if undeclared:
        raise RoutePolicyError(f"routes without an access declaration: {', '.join(undeclared)}")
