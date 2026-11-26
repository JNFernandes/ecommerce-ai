"""``GET /usage/summary``: ADMIN only, validated range, camelCase body (US-08 FR-012)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy.exc import OperationalError

from ecommerce_ai.agents.usage import UsageGroup, UsageGroupBy, UsageStats, UsageSummary
from ecommerce_ai.api.usage import UsageStatsResponse
from ecommerce_ai.infrastructure.settings import Settings
from ecommerce_ai.main import create_app
from tests.support.tokens import jwks, make_token

JWKS_URL: str = Settings.model_fields["identity_jwks_url"].default
ADMIN = make_token(sub="admin-1", roles=["ADMIN"])
CUSTOMER = make_token(sub="user-1", roles=["CUSTOMER"])
TOTALS = UsageStats(
    calls=4,
    input_tokens=1000,
    output_tokens=100,
    estimated_cost_usd=Decimal("0.004500"),
    latency_p50_ms=800,
    latency_p95_ms=1900,
    error_rate=0.25,
    fallback_rate=0.0,
    guardrail_blocks=2,
)


class FakeSummaryStore:
    """Returns ``TOTALS`` in one group and records each request; ``failure`` raises instead."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.requests: list[tuple[date, date, UsageGroupBy]] = []
        self.failure = failure

    async def summarize(self, start: date, end: date, group_by: UsageGroupBy) -> UsageSummary:
        self.requests.append((start, end, group_by))
        if self.failure is not None:
            raise self.failure
        group = UsageGroup(key="claude-x", **TOTALS.model_dump() | {"guardrail_blocks": None})
        return UsageSummary(groups=(group,), totals=TOTALS)


@pytest.fixture(autouse=True)
def _keys() -> Iterator[None]:
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URL).respond(json=jwks())
        router.route(host="localhost").pass_through()
        yield


@pytest.fixture
async def store() -> FakeSummaryStore:
    return FakeSummaryStore()


async def _get(
    store: FakeSummaryStore, query: str = "", token: str | None = ADMIN
) -> httpx.Response:
    app = create_app(probes=[], usage_summary_store=store)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get(f"/usage/summary{query}", headers=headers)


async def test_should_answer_401_when_no_token_is_sent(store: FakeSummaryStore) -> None:
    response = await _get(store, token=None)

    assert response.status_code == 401
    assert store.requests == []


async def test_should_answer_403_when_the_caller_is_not_an_admin(store: FakeSummaryStore) -> None:
    response = await _get(store, token=CUSTOMER)

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}
    assert store.requests == []


@pytest.mark.parametrize(
    "query",
    [
        "?from=2026-11-10&to=2026-11-01",  # reversed
        "?from=2026-01-01&to=2026-12-31",  # over 90 days
        "?from=yesterday",
        "?groupBy=hour",
    ],
)
async def test_should_answer_422_when_the_query_is_invalid(
    store: FakeSummaryStore, query: str
) -> None:
    response = await _get(store, query)

    assert response.status_code == 422
    assert store.requests == []


async def test_should_summarize_the_last_seven_days_by_day_when_no_query_is_given(
    store: FakeSummaryStore,
) -> None:
    response = await _get(store)

    assert response.status_code == 200
    today = datetime.now(UTC).date()
    [(start, end, group_by)] = store.requests
    assert (end, (end - start).days, group_by) == (today, 6, UsageGroupBy.DAY)
    body = response.json()
    assert (body["from"], body["to"], body["groupBy"]) == (
        start.isoformat(),
        today.isoformat(),
        "day",
    )


async def test_should_return_camel_case_groups_and_totals_when_grouped_by_model(
    store: FakeSummaryStore,
) -> None:
    response = await _get(store, "?from=2026-11-01&to=2026-11-30&groupBy=model")

    assert response.status_code == 200
    assert store.requests == [(date(2026, 11, 1), date(2026, 11, 30), UsageGroupBy.MODEL)]
    body = response.json()
    assert body["totals"] == {
        "calls": 4,
        "inputTokens": 1000,
        "outputTokens": 100,
        "estimatedCostUsd": "0.004500",
        "latencyP50Ms": 800,
        "latencyP95Ms": 1900,
        "errorRate": 0.25,
        "fallbackRate": 0.0,
        "guardrailBlocks": 2,
    }
    [group] = body["groups"]
    assert (group["key"], group["guardrailBlocks"]) == ("claude-x", None)


async def test_should_accept_a_ninety_day_range_when_it_is_the_limit(
    store: FakeSummaryStore,
) -> None:
    response = await _get(store, "?from=2026-09-01&to=2026-11-29")

    assert response.status_code == 200


async def test_should_answer_503_when_the_database_is_unreachable() -> None:
    store = FakeSummaryStore(OperationalError("SELECT 1", {}, ConnectionRefusedError()))

    response = await _get(store)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}


async def test_should_declare_the_admin_role_when_the_schema_is_built() -> None:
    app = create_app(probes=[], usage_summary_store=FakeSummaryStore())

    operation = app.openapi()["paths"]["/usage/summary"]["get"]

    assert operation["x-roles"] == ["ADMIN"]
    assert {param["name"] for param in operation["parameters"]} == {"from", "to", "groupBy"}


def test_should_drop_the_key_when_a_group_becomes_totals() -> None:
    group = UsageGroup(key="day", **TOTALS.model_dump())

    assert UsageStatsResponse.of(group).calls == 4
