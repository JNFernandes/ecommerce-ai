"""REST client for one platform API, on the shared ``UpstreamTransport``.

``RestClient.get_json`` and ``post_json`` join fixed path segments to the API's base URL (each
segment percent-encoded, so a value can never add a path level or a query), send the request
with the caller's token through the transport and return the decoded JSON object. A body that is
not JSON or not an object is an ``UpstreamError``: a tool never sees partial or foreign data.
"""

import json
from collections.abc import Mapping, Sequence
from urllib.parse import quote

from ecommerce_ai.infrastructure.upstream import QueryParams, UpstreamError, UpstreamTransport


class RestClient:
    """Reads JSON objects from one REST API through an ``UpstreamTransport``."""

    def __init__(self, transport: UpstreamTransport, base_url: str) -> None:
        """Send every request through ``transport`` to paths under ``base_url``."""
        self._transport = transport
        self._base_url = base_url.rstrip("/")

    def url_for(self, segments: Sequence[str]) -> str:
        """Return ``base_url`` followed by each segment, percent-encoded as a single segment."""
        path = "".join(f"/{quote(segment, safe='')}" for segment in segments)
        return f"{self._base_url}{path}"

    async def get_json(
        self,
        segments: Sequence[str],
        params: QueryParams | None = None,
        *,
        timeout: float,
        max_bytes: int,
    ) -> dict[str, object]:
        """``GET`` the resource at ``segments`` and return its JSON object.

        Raises ``Unauthenticated``, ``Forbidden``, ``NotFound`` or ``UpstreamError``.
        """
        body = await self._transport.request(
            "GET", self.url_for(segments), params=params, timeout=timeout, max_bytes=max_bytes
        )
        return self._decode(body)

    async def post_json(
        self,
        segments: Sequence[str],
        body: Mapping[str, object] | None = None,
        *,
        timeout: float,
        max_bytes: int,
    ) -> dict[str, object]:
        """``POST`` ``body`` (JSON, or nothing) to ``segments`` and return the JSON object answered.

        Same caller token, error mapping and size limit as ``get_json``, but never retried: a
        ``POST`` may change state, so a timeout or 5xx is reported, not repeated. Raises
        ``Unauthenticated``, ``Forbidden``, ``NotFound`` or ``UpstreamError``.
        """
        answer = await self._transport.request(
            "POST",
            self.url_for(segments),
            json=body,
            timeout=timeout,
            max_bytes=max_bytes,
            max_retries=0,
        )
        return self._decode(answer)

    def _decode(self, body: bytes) -> dict[str, object]:
        """Return ``body`` as a JSON object, or raise a non-retryable ``UpstreamError``."""
        service = self._transport.service
        try:
            decoded: object = json.loads(body)
        except ValueError as error:
            raise UpstreamError(f"{service} answered non-JSON", retryable=False) from error
        if not isinstance(decoded, dict):
            raise UpstreamError(f"{service} answered an unexpected shape", retryable=False)
        return decoded
