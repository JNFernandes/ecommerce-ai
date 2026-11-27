"""Shared transport for platform API calls made with the caller's own token (ADR 0001).

``UpstreamTransport.request`` sends one allowlisted request with the bound caller's
``Authorization`` and ``X-Request-Id`` headers, reads the body as a stream and aborts past
``max_bytes``, and classifies every failure into ``Unauthenticated`` (401), ``Forbidden`` (403),
``NotFound`` (404) or ``UpstreamError`` (5xx, timeouts and transport errors are retryable; any
other non-200 status and an ``application/problem+json`` body are not). Retryable failures are
retried inside the call's time budget (``call_with_retry``). Every exception reason starts with
the service name and never carries upstream bodies, URLs or tokens. Each attempt is one
``upstream.http`` span with the status code and, on failure, the error's class name only.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import httpx
from opentelemetry.trace import Span

from ecommerce_ai.infrastructure.caller import current_caller
from ecommerce_ai.infrastructure.retry import call_with_retry
from ecommerce_ai.telemetry.attributes import HTTP_STATUS_CODE
from ecommerce_ai.telemetry.spans import UPSTREAM_HTTP
from ecommerce_ai.telemetry.tracing import span

REQUEST_ID_HEADER: Final = "X-Request-Id"
PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

type QueryParams = Mapping[str, str | int]


class Unauthenticated(Exception):
    """The upstream rejected the caller's token (HTTP 401 or an equivalent error code)."""


class Forbidden(Exception):
    """The upstream refused the caller access (HTTP 403 or an equivalent error code)."""


class NotFound(Exception):
    """The upstream has no resource at the requested path (HTTP 404)."""


class UpstreamError(Exception):
    """The upstream failed or answered something unusable; ``retryable`` marks transient ones."""

    def __init__(self, reason: str, *, retryable: bool, status: int | None = None) -> None:
        """Keep ``reason`` free of upstream bodies, URLs and tokens.

        ``status`` is the HTTP status that caused it, when there was one (e.g. 409).
        """
        super().__init__(reason)
        self.retryable = retryable
        self.status = status


def _is_retryable(error: Exception) -> bool:
    return isinstance(error, UpstreamError) and error.retryable


class UpstreamTransport:
    """Sends caller-authenticated requests to one upstream over an ``httpx.AsyncClient``."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        service: str,
        *,
        max_retries: int,
        backoff_base: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Use ``http`` (owned by the caller); ``service`` names the upstream in error reasons."""
        self._http = http
        self._service = service
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._clock = clock
        self._sleep = sleep

    @property
    def service(self) -> str:
        """The upstream's name, as used in error reasons."""
        return self._service

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: QueryParams | None = None,
        json: Mapping[str, object] | None = None,
        timeout: float,
        max_bytes: int,
        max_retries: int | None = None,
    ) -> bytes:
        """Send the request and return the 200 body.

        ``timeout`` is the whole call's budget, retries included; ``max_retries`` overrides the
        transport's retries for this call (``0`` for a non-idempotent request). Raises
        ``Unauthenticated``, ``Forbidden``, ``NotFound`` or ``UpstreamError``.
        """
        caller = current_caller()
        headers = {
            "Authorization": f"Bearer {caller.token.get_secret_value()}",
            REQUEST_ID_HEADER: caller.request_id,
            "Accept": "application/json",
        }

        async def attempt(attempt_timeout: float) -> bytes:
            return await self._attempt(
                method, url, params, json, headers, attempt_timeout, max_bytes
            )

        try:
            return await call_with_retry(
                attempt,
                deadline=self._clock() + timeout,
                max_retries=self._max_retries if max_retries is None else max_retries,
                backoff_base=self._backoff_base,
                attempt_timeout=timeout,
                is_retryable=_is_retryable,
                clock=self._clock,
                sleep=self._sleep,
            )
        except TimeoutError as error:
            raise UpstreamError(f"{self._service} call budget spent", retryable=False) from error

    async def _attempt(
        self,
        method: str,
        url: str,
        params: QueryParams | None,
        json: Mapping[str, object] | None,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
    ) -> bytes:
        """Send one request within ``timeout`` and classify transport failures."""
        with span(UPSTREAM_HTTP) as attempt_span:
            try:
                async with asyncio.timeout(timeout):
                    return await self._send(
                        method, url, params, json, headers, timeout, max_bytes, attempt_span
                    )
            except (TimeoutError, httpx.TimeoutException) as error:
                raise UpstreamError(f"{self._service} timed out", retryable=True) from error
            except httpx.TransportError as error:
                raise UpstreamError(f"{self._service} unreachable", retryable=True) from error

    async def _send(
        self,
        method: str,
        url: str,
        params: QueryParams | None,
        json: Mapping[str, object] | None,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int,
        attempt_span: Span,
    ) -> bytes:
        """Return the body, aborting the stream once it exceeds ``max_bytes``."""
        async with self._http.stream(
            method,
            url,
            params=dict(params) if params is not None else None,
            json=dict(json) if json is not None else None,
            headers=headers,
            timeout=timeout,
        ) as response:
            attempt_span.set_attribute(HTTP_STATUS_CODE, response.status_code)
            self._raise_for_status(response)
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise UpstreamError(f"{self._service} response too large", retryable=False)
            return bytes(chunks)

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise the exception matching a non-200 status or a problem body."""
        status = response.status_code
        service = self._service
        if status == httpx.codes.UNAUTHORIZED:
            raise Unauthenticated(f"{service} rejected the token")
        if status == httpx.codes.FORBIDDEN:
            raise Forbidden(f"{service} refused access")
        if status == httpx.codes.NOT_FOUND:
            raise NotFound(f"{service} has no such resource")
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise UpstreamError(f"{service} answered {status}", retryable=True, status=status)
        if status != httpx.codes.OK:
            raise UpstreamError(f"{service} answered {status}", retryable=False, status=status)
        content_type = response.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() == PROBLEM_CONTENT_TYPE:
            raise UpstreamError(f"{service} answered a problem", retryable=False)
