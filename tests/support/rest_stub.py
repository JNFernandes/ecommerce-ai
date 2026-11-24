"""Shared behaviour of the respx stubs for the platform REST APIs behind the gateway.

``RestRoute`` wraps one respx route: it answers JSON, a status with a text body, a problem body,
an oversized body, a request that never ends or a sequence of outcomes, and exposes every
request it received so tests can check forwarded headers, query parameters and paths.
"""

import asyncio
from collections.abc import Mapping

import httpx
import respx


class RestRoute:
    """Programmable answers for one REST route; it answers 500 until configured."""

    def __init__(self, route: respx.Route) -> None:
        """Take over ``route``."""
        self.route = route
        self.route.respond(500)

    @property
    def requests(self) -> list[httpx.Request]:
        """Return every request the route received, in order."""
        return [call.request for call in self.route.calls]

    def params(self, index: int = -1) -> dict[str, str]:
        """Return the query parameters of one received request."""
        return dict(self.requests[index].url.params)

    def respond_json(self, body: object, status: int = 200) -> None:
        """Answer every request with ``body`` as JSON."""
        self.route.side_effect = None
        self.route.respond(status, json=body)

    def respond_status(self, status: int, text: str = "upstream failure detail") -> None:
        """Answer every request with an HTTP status and a plain-text body."""
        self.route.side_effect = None
        self.route.respond(status, text=text)

    def respond_problem(self, status: int = 200) -> None:
        """Answer an ``application/problem+json`` body."""
        self.route.side_effect = None
        self.route.respond(
            status,
            headers={"content-type": "application/problem+json"},
            json={"title": "problem", "detail": "upstream problem detail"},
        )

    def respond_oversize(self, size: int, body: Mapping[str, object]) -> None:
        """Answer ``body`` plus a padding field, making it at least ``size`` bytes."""
        self.respond_json({**body, "padding": "x" * size})

    def respond_hang(self) -> None:
        """Never answer: the request waits until the caller's timeout cancels it."""

        async def _hang(_request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        self.route.side_effect = _hang

    def respond_sequence(self, *responses: httpx.Response | Exception) -> None:
        """Answer successive requests with ``responses`` (exceptions are raised)."""
        self.route.side_effect = list(responses)
