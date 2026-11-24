"""Orders GraphQL client: one fixed query document per call, sent with the caller's own token.

``OrdersClient.execute`` posts ``{query, variables}`` to ``ORDERS_GRAPHQL_URL`` through an
``UpstreamTransport`` (caller token and ``X-Request-Id`` per ADR 0001, streamed size cap,
deadline-bounded retries, status classification) and decodes the GraphQL response: ``errors``
are checked before ``data``. A 404 from the endpoint is an ``UpstreamError``, as before the
shared transport existed.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping

import httpx

from ecommerce_ai.infrastructure.upstream import (
    Forbidden,
    NotFound,
    Unauthenticated,
    UpstreamError,
    UpstreamTransport,
)


def _raise_for_graphql_errors(errors: object) -> None:
    """Raise the exception matching the first GraphQL error code, if there are errors."""
    if not errors:
        return
    codes: list[object] = []
    if isinstance(errors, list):
        for item in errors:
            extensions = item.get("extensions") if isinstance(item, dict) else None
            codes.append(extensions.get("code") if isinstance(extensions, dict) else None)
    if "UNAUTHENTICATED" in codes:
        raise Unauthenticated("orders rejected the token")
    if "FORBIDDEN" in codes:
        raise Forbidden("orders refused access")
    if "SERVICE_UNAVAILABLE" in codes:
        raise UpstreamError("orders unavailable", retryable=True)
    raise UpstreamError("orders returned a GraphQL error", retryable=False)


class OrdersClient:
    """Executes allowlisted Orders GraphQL documents over a shared ``httpx.AsyncClient``."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        *,
        max_retries: int,
        backoff_base: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Use ``http`` (owned by the caller) for every request to ``url``."""
        self._url = url
        self._transport = UpstreamTransport(
            http,
            "orders",
            max_retries=max_retries,
            backoff_base=backoff_base,
            clock=clock,
            sleep=sleep,
        )

    async def execute(
        self,
        document: str,
        variables: Mapping[str, object],
        *,
        timeout: float,
        max_bytes: int,
    ) -> dict[str, object]:
        """Run ``document`` with ``variables`` and return its ``data`` object.

        ``timeout`` is the whole call's budget, retries included. Raises ``Unauthenticated``,
        ``Forbidden`` or ``UpstreamError``.
        """
        payload = {"query": document, "variables": dict(variables)}
        try:
            body = await self._transport.request(
                "POST", self._url, json=payload, timeout=timeout, max_bytes=max_bytes
            )
        except NotFound as error:
            raise UpstreamError("orders answered 404", retryable=False) from error
        return self._parse(body)

    @staticmethod
    def _parse(body: bytes) -> dict[str, object]:
        """Decode a GraphQL response: ``errors`` first, then a ``data`` object."""
        try:
            decoded: object = json.loads(body)
        except ValueError as error:
            raise UpstreamError("orders answered non-JSON", retryable=False) from error
        if not isinstance(decoded, dict):
            raise UpstreamError("orders answered an unexpected shape", retryable=False)
        _raise_for_graphql_errors(decoded.get("errors"))
        data = decoded.get("data")
        if not isinstance(data, dict):
            raise UpstreamError("orders answered without data", retryable=False)
        return data
