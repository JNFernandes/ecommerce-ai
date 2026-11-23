"""Default deny: every route must declare whether it is public or which roles it requires.

A route declares its access through ``openapi_extra`` (``x-public`` or ``x-roles``). Building the
application fails for any route that declares neither (constitution II).
"""

from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute

PUBLIC: Final[dict[str, object]] = {"x-public": True}
"""``openapi_extra`` of a route that needs no access token (health, readiness)."""


class RoutePolicyError(RuntimeError):
    """A route does not declare its access."""


def assert_route_policies(app: FastAPI) -> None:
    """Raise ``RoutePolicyError`` naming every API route without an access declaration."""
    undeclared = [
        f"{','.join(sorted(route.methods or ()))} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        and not (route.openapi_extra or {}).get("x-public")
        and not (route.openapi_extra or {}).get("x-roles")
    ]
    if undeclared:
        raise RoutePolicyError(f"routes without an access declaration: {', '.join(undeclared)}")
