"""The tool guard: allowlist, seen ids for ``cancel_order``, repeated calls (FR-010..012)."""

from pydantic import JsonValue

from ecommerce_ai.agents.guardrails import ToolContext
from ecommerce_ai.agents.guardrails.events import GuardAction, GuardSeverity, GuardStage
from ecommerce_ai.agents.guardrails.tool_guard import (
    CallGuard,
    ToolRule,
    call_key,
    refusal_payload,
)

ORDER_ID = "6f1c2d3e-4b5a-4c7d-8e9f-0a1b2c3d4e5f"
OTHER_ID = "11111111-2222-4333-8444-555555555555"
GUARD = CallGuard({"cancel_order": "orderId"})
ALLOWED = frozenset({"get_order", "list_my_orders", "cancel_order"})


def context(*, seen: frozenset[str] = frozenset(), previous: tuple[str, ...] = ()) -> ToolContext:
    return ToolContext(allowlist=ALLOWED, seen_ids=seen, previous_calls=previous)


def test_should_refuse_a_tool_off_the_allowlist_when_it_is_called() -> None:
    verdict = GUARD.check("delete_everything", {}, context())

    assert verdict is not None
    assert verdict.event.rule == ToolRule.NOT_ALLOWED
    assert (verdict.event.stage, verdict.event.action) == (GuardStage.TOOL, GuardAction.BLOCKED)
    assert verdict.event.severity is GuardSeverity.HIGH
    assert not verdict.ends_answer


def test_should_allow_every_tool_when_the_conversation_has_no_allowlist() -> None:
    assert GUARD.check("anything", {}, ToolContext(allowlist=None)) is None


def test_should_refuse_cancel_order_when_the_id_was_never_seen() -> None:
    verdict = GUARD.check(
        "cancel_order", {"orderId": ORDER_ID}, context(seen=frozenset({OTHER_ID}))
    )

    assert verdict is not None
    assert verdict.event.rule == ToolRule.UNSEEN_ARGUMENT
    assert verdict.event.severity is GuardSeverity.HIGH


def test_should_allow_cancel_order_when_the_id_was_seen_in_any_case() -> None:
    seen = frozenset({ORDER_ID})

    assert GUARD.check("cancel_order", {"orderId": ORDER_ID.upper()}, context(seen=seen)) is None
    assert GUARD.check("cancel_order", {"orderId": f" {ORDER_ID} "}, context(seen=seen)) is None


def test_should_leave_a_malformed_id_to_the_approval_checks() -> None:
    assert GUARD.check("cancel_order", {"orderId": "not-an-id"}, context()) is None
    assert GUARD.check("cancel_order", {"orderId": 42}, context()) is None
    assert GUARD.check("cancel_order", {}, context()) is None


def test_should_not_check_ids_of_read_only_tools() -> None:
    assert GUARD.check("get_order", {"orderId": OTHER_ID}, context()) is None


def test_should_refuse_the_first_repeat_of_an_identical_call() -> None:
    first = call_key("get_order", {"orderId": ORDER_ID})

    verdict = GUARD.check("get_order", {"orderId": ORDER_ID}, context(previous=(first,)))

    assert verdict is not None
    assert verdict.event.rule == ToolRule.DUPLICATE_CALL
    assert verdict.event.severity is GuardSeverity.LOW
    assert not verdict.ends_answer


def test_should_end_the_answer_when_an_identical_call_repeats_twice() -> None:
    key = call_key("get_order", {"orderId": ORDER_ID})

    verdict = GUARD.check("get_order", {"orderId": ORDER_ID}, context(previous=(key, key)))

    assert verdict is not None
    assert verdict.event.rule == ToolRule.TOOL_LOOP
    assert verdict.ends_answer


def test_should_allow_the_same_tool_when_its_arguments_differ() -> None:
    previous = (call_key("get_order", {"orderId": ORDER_ID}),)

    assert GUARD.check("get_order", {"orderId": OTHER_ID}, context(previous=previous)) is None


def test_should_build_the_same_key_whatever_the_argument_order() -> None:
    left: dict[str, JsonValue] = {"page": 1, "status": "PENDING"}
    right: dict[str, JsonValue] = {"status": "PENDING", "page": 1}

    assert call_key("list_my_orders", left) == call_key("list_my_orders", right)
    assert call_key("list_my_orders", left) != call_key("get_order", left)


def test_should_answer_a_fixed_refusal_that_never_echoes_the_arguments() -> None:
    for rule in ToolRule:
        payload = refusal_payload(rule.value)
        assert payload["error"] == rule.value
        assert ORDER_ID not in str(payload)
    assert refusal_payload("unknown")["error"] == ToolRule.NOT_ALLOWED.value
