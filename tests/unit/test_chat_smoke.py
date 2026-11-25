"""``scripts/chat_smoke.py`` over an httpx ``MockTransport``: no running service, no network."""

import io
import json

import httpx
import pytest
from scripts import chat_smoke
from scripts.chat_smoke import smoke

URL = "http://chat.test/chat"
CONVERSATION = "9b1f0c2e-0000-4000-8000-000000000001"


def _frame(index: int, event: str, data: object) -> str:
    return f"id: {index}\nevent: {event}\ndata: {json.dumps(data)}\n\n"


def _stream(*events: tuple[str, object]) -> str:
    body = ": keep-alive\n\n"
    return body + "".join(_frame(i, e, d) for i, (e, d) in enumerate(events, start=1))


ANSWER = _stream(
    ("message.start", {"conversationId": CONVERSATION, "messageId": "m-1"}),
    ("tool.call", {"callId": "t1", "name": "list_my_orders", "arguments": {"limit": 5}}),
    ("tool.result", {"callId": "t1", "name": "list_my_orders", "ok": True, "summary": "3 orders"}),
    ("token", {"text": "Your last order "}),
    ("token", {"text": "is SHIPPED."}),
    (
        "message.end",
        {
            "messageId": "m-1",
            "finishReason": "completed",
            "promptVersion": "assistant.v1",
            "modelId": "claude-test",
            "usage": {"inputTokens": 120, "outputTokens": 8},
        },
    ),
)


class FakeChat:
    """Answers the two chat routes and records every request."""

    def __init__(self, stream: str = ANSWER, post_status: int = 200) -> None:
        self.stream = stream
        self.post_status = post_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/chat/conversations":
            return httpx.Response(201, json={"id": CONVERSATION, "title": None})
        if self.post_status != 200:
            return httpx.Response(self.post_status, json={"code": "STREAM_ACTIVE"})
        return httpx.Response(200, text=self.stream, headers={"content-type": "text/event-stream"})


async def _run(fake: FakeChat, conversation_id: str | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = await smoke(
        URL, "secret-token", "Where is my order?", conversation_id, httpx.MockTransport(fake), out
    )
    return code, out.getvalue()


async def test_should_print_tools_tokens_and_end_when_the_answer_completes() -> None:
    fake = FakeChat()
    code, output = await _run(fake)

    assert code == 0
    assert [r.url.path for r in fake.requests] == [
        "/chat/conversations",
        f"/chat/conversations/{CONVERSATION}/messages",
    ]
    assert json.loads(fake.requests[1].content) == {"content": "Where is my order?"}
    assert all(r.headers["authorization"] == "Bearer secret-token" for r in fake.requests)
    assert f"conversation {CONVERSATION}" in output
    assert '[tool] list_my_orders {"limit": 5}' in output
    assert "[tool] list_my_orders ok: 3 orders" in output
    assert "Your last order is SHIPPED." in output
    assert "[end] completed (claude-test, 120 in / 8 out)" in output
    assert "secret-token" not in output


async def test_should_reuse_the_conversation_when_an_id_is_given() -> None:
    fake = FakeChat()
    code, _ = await _run(fake, CONVERSATION)

    assert code == 0
    assert [r.url.path for r in fake.requests] == [f"/chat/conversations/{CONVERSATION}/messages"]


async def test_should_print_the_error_and_fail_when_the_stream_ends_with_an_error() -> None:
    stream = _stream(
        ("message.start", {"conversationId": CONVERSATION, "messageId": "m-1"}),
        ("error", {"code": "LLM_UNAVAILABLE", "message": "The assistant is unavailable."}),
    )
    code, output = await _run(FakeChat(stream))

    assert code == 1
    assert "[error] LLM_UNAVAILABLE: The assistant is unavailable." in output


async def test_should_fail_when_the_stream_ends_without_a_terminal_event() -> None:
    stream = _stream(("message.start", {"conversationId": CONVERSATION, "messageId": "m-1"}))
    code, output = await _run(FakeChat(stream))

    assert code == 1
    assert "without message.end" in output


async def test_should_print_the_status_and_body_when_the_post_is_rejected() -> None:
    code, output = await _run(FakeChat(post_status=409))

    assert code == 1
    assert "[http 409]" in output
    assert "STREAM_ACTIVE" in output


def test_should_exit_2_when_the_token_or_the_question_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKEN", raising=False)
    assert chat_smoke.main(["Hi"]) == 2

    monkeypatch.setenv("TOKEN", "secret-token")
    assert chat_smoke.main(["  "]) == 2


def test_should_use_the_environment_for_the_url_and_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str, str | None]] = []

    async def fake_smoke(url: str, token: str, question: str, conversation_id: str | None) -> int:
        calls.append((url, token, question, conversation_id))
        return 0

    monkeypatch.setattr(chat_smoke, "smoke", fake_smoke)
    monkeypatch.setenv("TOKEN", "secret-token")
    monkeypatch.delenv("CHAT_URL", raising=False)
    monkeypatch.delenv("CONVERSATION_ID", raising=False)
    assert chat_smoke.main(["Where", "is", "it?"]) == 0

    monkeypatch.setenv("CHAT_URL", URL)
    monkeypatch.setenv("CONVERSATION_ID", CONVERSATION)
    assert chat_smoke.main(["Again"]) == 0

    assert calls == [
        (chat_smoke.DEFAULT_CHAT_URL, "secret-token", "Where is it?", None),
        (URL, "secret-token", "Again", CONVERSATION),
    ]


APPROVAL_ID = "9b1e6c2d-3a4f-4b5c-8d6e-7f8a9b0c1d2e"
ORDER = "3f2a9c1e-7b4d-4e5f-8a6b-1c2d3e4f5a6b"


def _end(finish_reason: str) -> tuple[str, object]:
    return (
        "message.end",
        {
            "messageId": "m-1",
            "finishReason": finish_reason,
            "promptVersion": "assistant.v2",
            "modelId": "claude-test",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        },
    )


PAUSED = _stream(
    ("message.start", {"conversationId": CONVERSATION, "messageId": "m-1"}),
    ("tool.call", {"callId": "t1", "name": "cancel_order", "arguments": {"orderId": ORDER}}),
    (
        "approval.required",
        {
            "approvalId": APPROVAL_ID,
            "action": "cancel_order",
            "orderId": ORDER,
            "summary": {"status": "PENDING", "totalAmount": 59.9, "currency": "EUR"},
            "expiresAt": "2026-11-25T10:12:00Z",
        },
    ),
    _end("awaiting_approval"),
)


def _resolved(decision: str, outcome: str) -> str:
    return _stream(
        ("message.start", {"conversationId": CONVERSATION, "messageId": "m-2"}),
        (
            "approval.resolved",
            {"approvalId": APPROVAL_ID, "decision": decision, "outcome": outcome},
        ),
        ("token", {"text": "Done."}),
        _end("completed"),
    )


class FakeApprovalChat(FakeChat):
    """Pauses on an approval, then answers the decision route with ``continuation``."""

    def __init__(self, continuation: str) -> None:
        super().__init__(PAUSED)
        self.continuation = continuation

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/approvals/{APPROVAL_ID}"):
            self.requests.append(request)
            return httpx.Response(
                200, text=self.continuation, headers={"content-type": "text/event-stream"}
            )
        return super().__call__(request)


@pytest.mark.parametrize(
    ("answer", "decision", "outcome"),
    [("y", "approve", "cancelled"), ("YES ", "approve", "cancelled"), ("n", "reject", "rejected")],
)
async def test_should_print_the_card_ask_and_post_the_decision(
    answer: str, decision: str, outcome: str
) -> None:
    fake = FakeApprovalChat(_resolved(decision, outcome))
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    out = io.StringIO()
    code = await smoke(
        URL, "secret-token", "Cancel it", CONVERSATION, httpx.MockTransport(fake), out, ask
    )

    output = out.getvalue()
    assert code == 0
    assert prompts == ["Approve this action? [y/N] "]
    assert (
        f"[approval] cancel_order order {ORDER}: PENDING, total 59.9 EUR,"
        " expires 2026-11-25T10:12:00Z"
    ) in output
    assert "[end] awaiting_approval" in output
    decided = fake.requests[-1]
    assert decided.url.path == f"/chat/conversations/{CONVERSATION}/approvals/{APPROVAL_ID}"
    assert json.loads(decided.content) == {"decision": decision}
    assert f"[approval] {decision}: {outcome}" in output
    assert output.rstrip().endswith("[end] completed (claude-test, 1 in / 1 out)")


async def test_should_print_an_unknown_total_and_fail_when_the_decision_is_refused() -> None:
    paused = PAUSED.replace('"totalAmount": 59.9, "currency": "EUR"', '"totalAmount": null')
    fake = FakeApprovalChat("")
    fake.stream = paused

    def refused(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/approvals/" + APPROVAL_ID):
            return httpx.Response(410, json={"code": "APPROVAL_EXPIRED"})
        return fake(request)

    out = io.StringIO()
    code = await smoke(
        URL, "t", "Cancel it", CONVERSATION, httpx.MockTransport(refused), out, lambda _: ""
    )

    assert code == 1
    assert "total unknown" in out.getvalue()
    assert "[http 410]" in out.getvalue()
