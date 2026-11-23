"""Orders GraphQL client: one fixed query document per call, sent with the caller's own token.

``OrdersClient.execute`` posts ``{query, variables}`` to ``ORDERS_GRAPHQL_URL`` with the bound
caller's ``Authorization`` and ``X-Request-Id`` (ADR 0001), reads the body as a stream and aborts
past ``max_bytes``, and classifies every failure into ``Unauthenticated``, ``Forbidden`` or
``UpstreamError`` (research 10). GraphQL ``errors`` are checked before ``data``. Retryable
failures are retried inside the call's time budget (``call_with_retry``).
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import httpx

from ecommerce_ai.infrastructure.caller import current_caller
from ecommerce_ai.infrastructure.retry import call_with_retry

REQUEST_ID_HEADER: Final = "X-Request-Id"


class Unauthenticated(Exception):
    """Orders rejected the caller's token (HTTP 401 or ``UNAUTHENTICATED``)."""


class Forbidden(Exception):
    """Orders refused the caller access (HTTP 403 or ``FORBIDDEN``)."""


class UpstreamError(Exception):
    """Orders failed or answered something unusable; ``retryable`` marks transient failures."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        """Keep ``reason`` free of upstream bodies, URLs and tokens."""
        super().__init__(reason)
        self.retryable = retryable


def _is_retryable(error: Exception) -> bool:
    return isinstance(error, UpstreamError) and error.retryable


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
        self._http = http
        self._url = url
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._clock = clock
        self._sleep = sleep

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
        caller = current_caller()
        headers = {
            "Authorization": f"Bearer {caller.token.get_secret_value()}",
            REQUEST_ID_HEADER: caller.request_id,
            "Accept": "application/json",
        }
        payload = {"query": document, "variables": dict(variables)}

        async def attempt(attempt_timeout: float) -> dict[str, object]:
            return await self._attempt(payload, headers, attempt_timeout, max_bytes)

        try:
            return await call_with_retry(
                attempt,
                deadline=self._clock() + timeout,
                max_retries=self._max_retries,
                backoff_base=self._backoff_base,
                attempt_timeout=timeout,
                is_retryable=_is_retryable,
                clock=self._clock,
                sleep=self._sleep,
            )
        except TimeoutError as error:
            raise UpstreamError("orders call budget spent", retryable=False) from error

    async def _attempt(
        self,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> dict[str, object]:
        """Send one request and classify its outcome."""
        try:
            async with asyncio.timeout(timeout):
                body = await self._post(payload, headers, timeout, max_bytes)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise UpstreamError("orders timed out", retryable=True) from error
        except httpx.TransportError as error:
            raise UpstreamError("orders unreachable", retryable=True) from error
        return self._parse(body)

    async def _post(
        self,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> bytes:
        """POST and return the body, aborting the stream once it exceeds ``max_bytes``."""
        async with self._http.stream(
            "POST", self._url, json=payload, headers=headers, timeout=timeout
        ) as response:
            if response.status_code == httpx.codes.UNAUTHORIZED:
                raise Unauthenticated("orders rejected the token")
            if response.status_code == httpx.codes.FORBIDDEN:
                raise Forbidden("orders refused access")
            if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
                raise UpstreamError(f"orders answered {response.status_code}", retryable=True)
            if response.status_code != httpx.codes.OK:
                raise UpstreamError(f"orders answered {response.status_code}", retryable=False)
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise UpstreamError("orders response too large", retryable=False)
            return bytes(chunks)

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
