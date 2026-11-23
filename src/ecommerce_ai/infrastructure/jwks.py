"""Identity's signing keys, fetched lazily and cached (constitution II, research 5).

``JwksCache.get_key`` returns the RS256 public key of a ``kid``. The JWKS document is fetched on
first use, never at startup. An unknown ``kid`` triggers a refetch at most once per cooldown
(key rotation without letting forged ``kid`` values hammer Identity). Cached keys are kept when
Identity is down. One ``asyncio.Lock`` makes concurrent misses share a single fetch. When no key
was ever obtained and the fetch fails, ``KeysUnavailable`` is raised (the caller answers 503).
"""

import asyncio
import time
from collections.abc import Callable

import httpx
import structlog
from jwt import PyJWK
from jwt.exceptions import PyJWTError

_logger = structlog.get_logger(__name__)


class KeysUnavailable(Exception):
    """No signing key is cached and Identity's JWKS could not be fetched."""


class JwksCache:
    """Lazy, single-flight cache of Identity's RS256 signing keys, by ``kid``."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        *,
        timeout: float,
        refetch_cooldown: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Fetch ``url`` with ``http`` (owned by the caller) when a key is first needed."""
        self._http = http
        self._url = url
        self._timeout = timeout
        self._cooldown = refetch_cooldown
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._last_fetch: float | None = None
        self._fetches = 0
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> PyJWK | None:
        """Return the key of ``kid``, or ``None`` when Identity does not publish it.

        Raises ``KeysUnavailable`` when no key is cached and the fetch fails.
        """
        key = self._keys.get(kid)
        if key is not None:
            return key
        if self._keys and not self._cooldown_elapsed():
            return None
        fetches_seen = self._fetches
        async with self._lock:
            if self._fetches == fetches_seen:
                await self._refresh()
        if not self._keys:
            raise KeysUnavailable("no signing keys available")
        return self._keys.get(kid)

    def _cooldown_elapsed(self) -> bool:
        return self._last_fetch is None or self._clock() - self._last_fetch >= self._cooldown

    async def _refresh(self) -> None:
        """Fetch the JWKS; keep the cached keys when the fetch fails."""
        try:
            await self._fetch()
        finally:
            self._fetches += 1
            self._last_fetch = self._clock()

    async def _fetch(self) -> None:
        try:
            response = await self._http.get(self._url, timeout=self._timeout)
            response.raise_for_status()
            document: object = response.json()
        except (httpx.HTTPError, ValueError) as error:
            _logger.warning("jwks_fetch_failed", error=type(error).__name__)
            return
        keys = _parse_keys(document)
        if keys:
            self._keys = keys
        else:
            _logger.warning("jwks_fetch_failed", error="no usable keys")


def _parse_keys(document: object) -> dict[str, PyJWK]:
    """Return the usable RS256 signing keys of a JWKS document, by ``kid``."""
    entries = document.get("keys") if isinstance(document, dict) else None
    keys: dict[str, PyJWK] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("kid"), str):
            continue
        if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig":
            continue
        try:
            keys[entry["kid"]] = PyJWK(entry, algorithm="RS256")
        except PyJWTError:
            continue
    return keys
