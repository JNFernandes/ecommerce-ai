"""Tool results: untrusted envelope, text truncation and fixed error results."""

import pytest
from pydantic import BaseModel, ValidationError

from ecommerce_ai.tools.envelope import (
    ToolEnvelope,
    ToolErrorCode,
    ToolErrorResult,
    ToolFailure,
    truncate_text,
)


class Item(BaseModel):
    name: str


def test_should_mark_data_untrusted_when_wrapping_a_result() -> None:
    envelope = ToolEnvelope[Item](data=Item(name="a"))

    assert envelope.model_dump(mode="json") == {
        "source": "ecommerce-orders",
        "trust": "untrusted_data",
        "data": {"name": "a"},
        "truncated": [],
    }


def test_should_reject_other_trust_values_when_validating_an_envelope() -> None:
    with pytest.raises(ValidationError):
        ToolEnvelope[Item].model_validate({"trust": "trusted", "data": {"name": "a"}})


def test_should_cut_to_limit_with_mark_and_record_path_when_text_is_too_long() -> None:
    truncated: list[str] = []

    result = truncate_text("x" * 20, 10, "data.cancellationReason", truncated)

    assert result == "x" * 9 + "…"
    assert len(result) == 10
    assert truncated == ["data.cancellationReason"]


@pytest.mark.parametrize("value", [None, "", "x" * 10])
def test_should_keep_text_unchanged_when_within_limit(value: str | None) -> None:
    truncated: list[str] = []

    assert truncate_text(value, 10, "data.reason", truncated) == value
    assert truncated == []


@pytest.mark.parametrize(
    ("code", "value", "message"),
    [
        (ToolErrorCode.NOT_FOUND, "not_found", "order not found"),
        (ToolErrorCode.NOT_ALLOWED, "not_allowed", "not allowed"),
        (ToolErrorCode.INVALID_INPUT, "invalid_input", "invalid input"),
        (ToolErrorCode.UPSTREAM_ERROR, "upstream_error", "orders service unavailable"),
    ],
)
def test_should_use_fixed_message_when_building_error_result(
    code: ToolErrorCode, value: str, message: str
) -> None:
    failure = ToolFailure(code)

    assert code.value == value
    assert failure.code is code
    assert str(failure) == message
    assert failure.result.model_dump(mode="json") == {"error": value, "message": message}
    assert ToolErrorResult.for_code(code) == failure.result


def test_should_have_exactly_the_contract_codes_when_listing_error_codes() -> None:
    assert {code.value for code in ToolErrorCode} == {
        "not_found",
        "not_allowed",
        "invalid_input",
        "upstream_error",
    }
