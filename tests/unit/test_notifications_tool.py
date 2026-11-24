"""``list_my_notifications`` reads only the caller's notifications, scoped by their claim."""

from collections.abc import AsyncIterator, Iterator
from uuid import UUID

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError
from pydantic import SecretStr, ValidationError

from ecommerce_ai.infrastructure.caller import CallerIdentity, RequestCaller, bind_caller
from ecommerce_ai.infrastructure.rest_client import RestClient
from ecommerce_ai.infrastructure.upstream import UpstreamTransport
from ecommerce_ai.tools.envelope import ToolErrorCode, ToolFailure
from ecommerce_ai.tools.notifications import (
    LIST_NOTIFICATIONS_ENTRY,
    ListMyNotificationsInput,
    list_my_notifications_tool,
)
from ecommerce_ai.tools.registry import ALLOWLIST
from ecommerce_ai.tools.server import RegistryTool
from tests.support.notifications_stub import (
    CUSTOMER_A,
    CUSTOMER_B,
    NOTIFICATIONS_BASE_URL,
    ORDER_ID,
    NotificationsStub,
    sample_notification,
)

TOKEN = "caller-token"  # noqa: S105  # fake bearer value for header assertions
REQUEST_ID = "req-notifications-1"
LIMIT = 500


@pytest.fixture
def stub() -> Iterator[NotificationsStub]:
    with respx.mock(assert_all_called=False) as router:
        yield NotificationsStub(router)


@pytest.fixture
async def notifications() -> AsyncIterator[RestClient]:
    async with httpx.AsyncClient() as http:
        transport = UpstreamTransport(http, "notifications", max_retries=0, backoff_base=0.01)
        yield RestClient(transport, NOTIFICATIONS_BASE_URL)


def _bind(
    customer_id: str | None = CUSTOMER_A, roles: frozenset[str] = frozenset({"CUSTOMER"})
) -> RequestCaller:
    identity = CallerIdentity(
        subject="user-1",
        roles=roles,
        customer_id=UUID(customer_id) if customer_id is not None else None,
    )
    return RequestCaller(identity=identity, token=SecretStr(TOKEN), request_id=REQUEST_ID)


@pytest.fixture
def caller() -> Iterator[RequestCaller]:
    with bind_caller(_bind()) as bound:
        yield bound


def _tool(notifications: RestClient) -> RegistryTool:
    return RegistryTool.from_definition(
        list_my_notifications_tool(notifications, timeout=5.0, max_bytes=65536, text_limit=LIMIT)
    )


async def _call(notifications: RestClient, arguments: dict[str, object]) -> dict[str, object]:
    result = await _tool(notifications).run(arguments)
    content = result.structured_content
    assert isinstance(content, dict)
    return content


async def _failure(notifications: RestClient, arguments: dict[str, object]) -> ToolFailure:
    with pytest.raises(ToolError) as raised:
        await _tool(notifications).run(arguments)
    cause = raised.value.__cause__
    assert isinstance(cause, ToolFailure)
    return cause


def _items(content: dict[str, object]) -> list[dict[str, object]]:
    data = content["data"]
    assert isinstance(data, dict)
    items = data["items"]
    assert isinstance(items, list)
    return items


async def test_should_return_default_page_in_given_order_scoped_to_caller(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller
) -> None:
    newest = sample_notification(id="11111111-1111-4111-8111-111111111111")
    older = sample_notification(createdAt="2026-11-19T08:00:00Z")
    stub.respond_page([newest, older], total=2)

    content = await _call(notifications, {})

    assert (content["source"], content["trust"]) == ("notifications", "untrusted_data")
    assert [item["id"] for item in _items(content)] == [newest["id"], older["id"]]
    assert stub.notifications.params() == {"customerId": CUSTOMER_A, "limit": "20", "offset": "0"}
    (request,) = stub.notifications.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Request-Id"] == REQUEST_ID


async def test_should_scope_by_claim_when_caller_is_also_admin(
    stub: NotificationsStub, notifications: RestClient
) -> None:
    stub.respond_page([], total=0)

    with bind_caller(_bind(CUSTOMER_B, frozenset({"CUSTOMER", "ADMIN"}))):
        await _call(notifications, {})

    assert stub.notifications.params()["customerId"] == CUSTOMER_B


async def test_should_fail_not_allowed_without_request_when_token_has_no_customer_id(
    stub: NotificationsStub, notifications: RestClient
) -> None:
    with bind_caller(_bind(None)):
        failure = await _failure(notifications, {})

    assert failure.code is ToolErrorCode.NOT_ALLOWED
    assert stub.notifications.requests == []


async def test_should_show_each_customer_only_their_own_notifications(
    stub: NotificationsStub, notifications: RestClient
) -> None:
    stub.respond_by_customer(
        {
            CUSTOMER_A: [sample_notification(message="for A")],
            CUSTOMER_B: [sample_notification(customerId=CUSTOMER_B, message="for B")],
        }
    )

    with bind_caller(_bind(CUSTOMER_A)):
        seen_by_a = await _call(notifications, {})
    with bind_caller(_bind(CUSTOMER_B)):
        seen_by_b = await _call(notifications, {})

    assert [item["message"] for item in _items(seen_by_a)] == ["for A"]
    assert [item["message"] for item in _items(seen_by_b)] == ["for B"]


async def test_should_forward_order_filter_and_page_when_given(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller
) -> None:
    stub.respond_page([], total=0, limit=5, offset=10)

    await _call(notifications, {"orderId": ORDER_ID, "limit": 5, "offset": 10})

    assert stub.notifications.params() == {
        "customerId": CUSTOMER_A,
        "limit": "5",
        "offset": "10",
        "orderId": ORDER_ID,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"customerId": CUSTOMER_B},
        {"orderId": "not-a-uuid"},
        {"orderId": "../admin"},
        {"limit": 0},
        {"limit": 51},
        {"offset": -1},
    ],
)
async def test_should_reject_invalid_arguments_without_calling_notifications(
    stub: NotificationsStub,
    notifications: RestClient,
    caller: RequestCaller,
    arguments: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        await _tool(notifications).run(arguments)

    assert stub.notifications.requests == []


async def test_should_fail_not_allowed_when_notifications_answers_403(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller
) -> None:
    stub.notifications.respond_status(403)

    failure = await _failure(notifications, {})

    assert failure.code is ToolErrorCode.NOT_ALLOWED


async def test_should_flag_caller_unauthenticated_when_notifications_answers_401(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller
) -> None:
    stub.notifications.respond_status(401)

    await _failure(notifications, {})

    assert caller.upstream_unauthenticated is True


@pytest.mark.parametrize("status", [404, 422, 503])
async def test_should_fail_notifications_unavailable_when_upstream_fails(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller, status: int
) -> None:
    stub.notifications.respond_status(status)

    failure = await _failure(notifications, {})

    assert failure.code is ToolErrorCode.UPSTREAM_ERROR
    assert failure.message == "notifications unavailable"


async def test_should_drop_internal_fields_and_normalise_created_at_to_utc(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller
) -> None:
    stub.respond_page([sample_notification(createdAt="2026-11-20T11:15:06+01:00")], total=1)

    content = await _call(notifications, {})

    (item,) = _items(content)
    assert "customerId" not in item
    assert "sourceEventId" not in item
    assert item["createdAt"] == "2026-11-20T10:15:06Z"
    assert item["orderId"] == ORDER_ID


async def test_should_truncate_long_message_and_flag_it(
    stub: NotificationsStub, notifications: RestClient, caller: RequestCaller
) -> None:
    stub.respond_page([sample_notification(message="m" * 900)], total=1)

    content = await _call(notifications, {})

    assert content["truncated"] == ["data.items[0].message"]
    (item,) = _items(content)
    assert isinstance(item["message"], str)
    assert len(item["message"]) == LIMIT


@pytest.mark.parametrize(
    "overrides",
    [
        {"createdAt": "2026-11-20T10:15:06"},
        {"type": "T" * 65},
        {"orderId": "nope"},
        {"message": None},
    ],
)
async def test_should_fail_notifications_unavailable_when_item_is_malformed(
    stub: NotificationsStub,
    notifications: RestClient,
    caller: RequestCaller,
    overrides: dict[str, object],
) -> None:
    stub.respond_page([sample_notification(**overrides)], total=1)

    failure = await _failure(notifications, {})

    assert failure.code is ToolErrorCode.UPSTREAM_ERROR
    assert failure.message == "notifications unavailable"


def test_should_declare_a_read_only_allowlisted_tool_without_customer_input() -> None:
    client = RestClient.__new__(RestClient)
    definition = list_my_notifications_tool(client, timeout=5.0, max_bytes=1, text_limit=LIMIT)

    assert definition.name == "list_my_notifications"
    assert definition.allowlist_entry == LIST_NOTIFICATIONS_ENTRY
    assert LIST_NOTIFICATIONS_ENTRY in ALLOWLIST
    assert definition.read_only is True
    assert definition.input_model is ListMyNotificationsInput
    properties = ListMyNotificationsInput.model_json_schema()["properties"]
    assert set(properties) == {"limit", "offset", "orderId"}
