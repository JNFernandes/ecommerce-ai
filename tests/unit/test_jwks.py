"""The JWKS cache fetches lazily, refetches unknown kids once per cooldown and survives outages."""

import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx

from ecommerce_ai.infrastructure.jwks import JwksCache, KeysUnavailable
from tests.support.polling import wait_until
from tests.support.tokens import KID, UNKNOWN_KID, foreign_key, jwks

JWKS_URL = "http://identity.test/.well-known/jwks.json"
COOLDOWN = 30.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _cache(http: httpx.AsyncClient, clock: FakeClock) -> JwksCache:
    return JwksCache(http, JWKS_URL, timeout=3.0, refetch_cooldown=COOLDOWN, clock=clock)


def test_should_not_fetch_when_created(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    route = router.get(JWKS_URL).respond(json=jwks())

    _cache(http, clock)

    assert route.call_count == 0


async def test_should_fetch_on_first_use_and_then_serve_from_cache(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    route = router.get(JWKS_URL).respond(json=jwks())
    cache = _cache(http, clock)

    first = await cache.get_key(KID)
    second = await cache.get_key(KID)

    assert first is not None
    assert first is second
    assert first.algorithm_name == "RS256"
    assert route.call_count == 1


async def test_should_refetch_unknown_kid_at_most_once_per_cooldown(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    route = router.get(JWKS_URL).respond(json=jwks())
    cache = _cache(http, clock)
    await cache.get_key(KID)

    clock.now += COOLDOWN
    assert await cache.get_key(UNKNOWN_KID) is None
    assert await cache.get_key(UNKNOWN_KID) is None
    clock.now += COOLDOWN - 1
    assert await cache.get_key(UNKNOWN_KID) is None

    assert route.call_count == 2


async def test_should_pick_up_a_rotated_key_when_cooldown_has_elapsed(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    rotated = jwks(kid="rotated", key=foreign_key())
    router.get(JWKS_URL).mock(
        side_effect=[httpx.Response(200, json=jwks()), httpx.Response(200, json=rotated)]
    )
    cache = _cache(http, clock)
    await cache.get_key(KID)

    clock.now += COOLDOWN

    assert await cache.get_key("rotated") is not None


async def test_should_keep_cached_keys_when_identity_is_down(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    router.get(JWKS_URL).mock(
        side_effect=[httpx.Response(200, json=jwks()), httpx.ConnectError("down")]
    )
    cache = _cache(http, clock)
    await cache.get_key(KID)

    clock.now += COOLDOWN

    assert await cache.get_key(UNKNOWN_KID) is None
    assert await cache.get_key(KID) is not None


async def test_should_share_one_fetch_when_concurrent_requests_miss(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    release = asyncio.Event()
    started: list[None] = []

    async def slow_jwks(_request: httpx.Request) -> httpx.Response:
        started.append(None)
        await release.wait()
        return httpx.Response(200, json=jwks())

    route = router.get(JWKS_URL).mock(side_effect=slow_jwks)
    cache = _cache(http, clock)

    tasks = [asyncio.create_task(cache.get_key(KID)) for _ in range(5)]
    await wait_until(lambda: len(started) == 1)
    release.set()
    keys = await asyncio.gather(*tasks)

    assert all(key is not None for key in keys)
    assert route.call_count == 1


async def test_should_share_one_failed_fetch_when_concurrent_requests_find_no_keys(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    release = asyncio.Event()
    started: list[None] = []

    async def failing(_request: httpx.Request) -> httpx.Response:
        started.append(None)
        await release.wait()
        return httpx.Response(503)

    route = router.get(JWKS_URL).mock(side_effect=failing)
    cache = _cache(http, clock)

    tasks = [asyncio.create_task(cache.get_key(KID)) for _ in range(3)]
    await wait_until(lambda: len(started) == 1)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, KeysUnavailable) for result in results)
    assert route.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"keys": []}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(
            200,
            json={
                "keys": [
                    "junk",
                    {"kty": "RSA"},
                    {"kid": "ec", "kty": "EC"},
                    {"kid": "enc", "kty": "RSA", "use": "enc"},
                    {"kid": "broken", "kty": "RSA", "n": "!", "e": "!"},
                ]
            },
        ),
    ],
)
async def test_should_raise_keys_unavailable_when_no_keys_and_fetch_fails(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock, response: httpx.Response
) -> None:
    router.get(JWKS_URL).mock(return_value=response)
    cache = _cache(http, clock)

    with pytest.raises(KeysUnavailable):
        await cache.get_key(KID)


async def test_should_raise_keys_unavailable_when_identity_is_unreachable(
    router: respx.MockRouter, http: httpx.AsyncClient, clock: FakeClock
) -> None:
    router.get(JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
    cache = _cache(http, clock)

    with pytest.raises(KeysUnavailable):
        await cache.get_key(KID)
