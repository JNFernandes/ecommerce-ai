"""``docs/chat-contract.md`` matches the code: events, payload fields, codes and fixed messages.

Every ``<!-- schema: Name -->`` JSON example validates against that model and uses exactly the
field names it dumps; every SSE example frame validates against its event model; the event
table, both code tables, the finish reasons, the routes and the ``cancel_order`` summaries list
exactly what the code defines.
"""

import json
import re
from pathlib import Path
from typing import Final

import pytest
from pydantic import BaseModel

from ecommerce_ai.agents.assistant.approvals import Refusal, outcome_summary, refusal_summary
from ecommerce_ai.agents.assistant.gatekeeper import BLOCKED_REFUSAL
from ecommerce_ai.agents.assistant.model import (
    ERROR_MESSAGES,
    STREAM_EVENT_TYPES,
    ApprovalOutcome,
    ChatErrorCode,
    FinishReason,
)
from ecommerce_ai.api.chat import (
    HTTP_MESSAGES,
    PREFIX,
    ChatHttpCode,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    CreateConversationRequest,
    DecisionRequest,
    ErrorBody,
    PostMessageRequest,
)
from tests.support.chat import chat_app

DOC: Final = (Path(__file__).parents[2] / "docs" / "chat-contract.md").read_text(encoding="utf-8")

SCHEMAS: Final[dict[str, type[BaseModel]]] = {
    model.__name__: model
    for model in (
        ConversationDetailResponse,
        ConversationListResponse,
        ConversationResponse,
        CreateConversationRequest,
        DecisionRequest,
        ErrorBody,
        PostMessageRequest,
    )
}
EVENTS: Final = {model.EVENT: model for model in STREAM_EVENT_TYPES}
HTTP_STATUS: Final = {
    ChatHttpCode.NOT_FOUND: 404,
    ChatHttpCode.STREAM_ACTIVE: 409,
    ChatHttpCode.CONVERSATION_FULL: 409,
    ChatHttpCode.PAYLOAD_TOO_LARGE: 413,
    ChatHttpCode.VALIDATION_FAILED: 422,
    ChatHttpCode.RATE_LIMITED: 429,
    ChatHttpCode.UNAVAILABLE: 503,
    ChatHttpCode.APPROVAL_PENDING: 409,
    ChatHttpCode.APPROVAL_ALREADY_DECIDED: 409,
    ChatHttpCode.APPROVAL_EXPIRED: 410,
}

_TAGGED: Final = re.compile(r"<!-- schema: (\w+) -->\s*```json\n(.*?)```", re.DOTALL)
_TEXT_BLOCK: Final = re.compile(r"```text\n(.*?)```", re.DOTALL)


def _keys(value: object) -> object:
    """The nested key structure of a JSON value (lists reduced to their items' structures)."""
    if isinstance(value, dict):
        return {key: _keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_keys(item) for item in value]
    return None


def _tagged_examples() -> list[tuple[str, object]]:
    return [(name, json.loads(body)) for name, body in _TAGGED.findall(DOC)]


def _frames() -> list[tuple[int, str, object]]:
    frames: list[tuple[int, str, object]] = []
    for block in _TEXT_BLOCK.findall(DOC):
        for raw in block.strip().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in raw.splitlines())
            frames.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return frames


def _table_rows(first_cell: str) -> list[list[str]]:
    """Rows of every table whose first cell matches ``first_cell`` (a regex)."""
    rows = []
    for line in DOC.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and re.fullmatch(first_cell, cells[0]):
            rows.append(cells)
    return rows


def test_should_tag_every_json_example_with_a_known_schema() -> None:
    names = {name for name, _ in _tagged_examples()}

    assert names == set(SCHEMAS)
    assert DOC.count("```json") == len(_tagged_examples())


@pytest.mark.parametrize(("name", "example"), _tagged_examples())
def test_should_validate_each_json_example_against_its_schema(name: str, example: object) -> None:
    model = SCHEMAS[name].model_validate(example)

    assert _keys(model.model_dump(mode="json", by_alias=True)) == _keys(example)


def test_should_validate_each_sse_frame_and_number_it_from_one() -> None:
    frames = _frames()
    streams: list[list[int]] = []
    for event_id, name, data in frames:
        model = EVENTS[name].model_validate(data)
        assert _keys(model.data()) == _keys(data)
        if event_id == 1:
            streams.append([])
        streams[-1].append(event_id)

    assert {name for _, name, _ in frames} == set(EVENTS)
    assert all(ids == list(range(1, len(ids) + 1)) for ids in streams)


def test_should_list_every_event_with_exactly_its_data_fields() -> None:
    rows = {cells[0].strip("`"): cells[1] for cells in _table_rows(r"`[a-z.]+`")}

    assert set(rows) == set(EVENTS)
    for name, model in EVENTS.items():
        aliases = {field.alias or key for key, field in model.model_fields.items()}
        assert set(re.findall(r"`(\w+)`", rows[name])) - {"null"} == aliases


def test_should_document_every_stream_error_code_with_its_fixed_message() -> None:
    rows = {cells[0].strip("`"): cells[1] for cells in _table_rows(r"`[A-Z_]+`")}

    assert {code: rows[code] for code in ChatErrorCode} == {
        code.value: message for code, message in ERROR_MESSAGES.items()
    }


def test_should_document_every_http_code_with_its_status_and_fixed_message() -> None:
    rows = {cells[1].strip("`"): (int(cells[0]), cells[2]) for cells in _table_rows(r"\d{3}")}

    for code in ChatHttpCode:
        assert rows[code.value] == (HTTP_STATUS[code], HTTP_MESSAGES[code])


def test_should_list_every_finish_reason() -> None:
    reasons = re.search(r"`finishReason` values: (.*?)\.\n", DOC, re.DOTALL)

    assert reasons is not None
    assert set(re.findall(r"`(\w+)`", reasons.group(1))) == {r.value for r in FinishReason}


def test_should_list_exactly_the_chat_routes_of_the_app() -> None:
    documented = {
        (cells[0], re.sub(r"\?.*", "", cells[1].strip("`")).replace("{id}", "{conversation_id}"))
        for cells in _table_rows(r"GET|POST|DELETE")
    }
    paths = chat_app(None).openapi()["paths"]
    app_routes = {
        (method.upper(), path.replace("{approval_id}", "{approvalId}"))
        for path, operations in paths.items()
        if path.startswith(PREFIX + "/")
        for method in operations
    }

    assert documented == app_routes


def test_should_document_every_cancel_order_summary() -> None:
    phrases = {outcome_summary(outcome) for outcome in ApprovalOutcome} | {
        refusal_summary(refusal) for refusal in Refusal
    }

    assert all(f"`{phrase}`" in DOC for phrase in phrases)


def test_should_document_the_fixed_blocked_refusal() -> None:
    assert BLOCKED_REFUSAL in " ".join(DOC.split())
