"""Manual smoke test of the chat assistant against the running service.

Creates a conversation (or continues the one in ``CONVERSATION_ID``), posts the question given on
the command line and prints the streamed answer: each tool call and result on its own line, the
tokens as they arrive, then the finish reason or the error code and message. The customer access
token comes from ``TOKEN`` and is never printed; ``CHAT_URL`` overrides the default
``http://localhost:8002/chat``. The conversation id is printed first so a follow-up can reuse it.
The SSE stream is parsed with the standard library only (no SSE client dependency).

When the answer pauses for an approval (US-06), the card is printed (action, order id, status,
total and expiry) and ``y/n`` is asked; the decision is posted to the approval route and its
continuation streamed the same way. Only an explicit ``y``/``yes`` approves.
Usage: ``TOKEN=... uv run python scripts/chat_smoke.py "Where is my last order?"``.
"""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import TextIO

import httpx

DEFAULT_CHAT_URL = "http://localhost:8002/chat"
AWAITING_APPROVAL = "awaiting_approval"
APPROVE_ANSWERS = frozenset({"y", "yes"})
TIMEOUT = httpx.Timeout(10.0, read=120.0)
"""Connect/write limits for the requests; a long read limit for a slow answer."""


@dataclass(frozen=True, slots=True)
class Event:
    """One dispatched SSE event: its name and its JSON ``data``."""

    name: str
    data: dict[str, object]


def _decode(raw: str) -> dict[str, object]:
    parsed: object = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


async def iter_events(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    """Yield one event per frame (frames end at a blank line); comments are keep-alives."""
    name = "message"
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                yield Event(name, _decode("\n".join(data)))
            name, data = "message", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
    if data:
        yield Event(name, _decode("\n".join(data)))


def render(event: Event) -> str:
    """The text printed for ``event`` (``""`` for ``message.start``)."""
    data = event.data
    match event.name:
        case "tool.call":
            return f"\n[tool] {data.get('name')} {json.dumps(data.get('arguments'))}\n"
        case "tool.result":
            outcome = "ok" if data.get("ok") else "failed"
            return f"[tool] {data.get('name')} {outcome}: {data.get('summary')}\n"
        case "token":
            return str(data.get("text", ""))
        case "message.end":
            usage = data.get("usage")
            tokens = (
                f"{usage.get('inputTokens')} in / {usage.get('outputTokens')} out"
                if isinstance(usage, dict)
                else "no usage"
            )
            return f"\n[end] {data.get('finishReason')} ({data.get('modelId')}, {tokens})\n"
        case "error":
            return f"\n[error] {data.get('code')}: {data.get('message')}\n"
        case "approval.required":
            summary = data.get("summary")
            details = summary if isinstance(summary, dict) else {}
            total = details.get("totalAmount")
            amount = "unknown" if total is None else f"{total} {details.get('currency') or ''}"
            return (
                f"\n[approval] {data.get('action')} order {data.get('orderId')}:"
                f" {details.get('status')}, total {amount.strip()},"
                f" expires {data.get('expiresAt')}\n"
            )
        case "approval.resolved":
            return f"[approval] {data.get('decision')}: {data.get('outcome')}\n"
        case _:
            return ""


type Ask = Callable[[str], str]
"""Reads one answer from the person running the smoke test (``input`` by default)."""


@dataclass(frozen=True, slots=True)
class Ending:
    """How one stream ended: its exit code and the approval it paused on, if any."""

    code: int
    approval_id: str | None = None


async def _stream(
    client: httpx.AsyncClient, path: str, body: dict[str, str], out: TextIO
) -> Ending:
    """POST ``body`` to ``path`` and print the streamed events until the terminal one."""
    approval_id: str | None = None
    async with client.stream("POST", path, json=body) as response:
        if response.status_code != 200:
            await response.aread()
            out.write(f"[http {response.status_code}] {response.text}\n")
            return Ending(1)
        async for event in iter_events(response.aiter_lines()):
            out.write(render(event))
            out.flush()
            if event.name == "approval.required":
                approval_id = str(event.data.get("approvalId"))
            if event.name == "error":
                return Ending(1)
            if event.name == "message.end":
                paused = event.data.get("finishReason") == AWAITING_APPROVAL
                return Ending(0, approval_id if paused else None)
    out.write("\n[error] the stream ended without message.end\n")
    return Ending(1)


async def chat(
    client: httpx.AsyncClient,
    question: str,
    conversation_id: str | None,
    out: TextIO,
    ask: Ask = input,
) -> int:
    """Ask ``question``, then decide every approval with ``ask``; 0 after ``message.end``."""
    if conversation_id is None:
        created = await client.post("/conversations", json={})
        if created.status_code != 201:
            out.write(f"[http {created.status_code}] {created.text}\n")
            return 1
        conversation_id = str(created.json()["id"])
    out.write(f"conversation {conversation_id}\n")
    base = f"/conversations/{conversation_id}"
    ending = await _stream(client, f"{base}/messages", {"content": question}, out)
    while ending.code == 0 and ending.approval_id is not None:
        answer = ask("Approve this action? [y/N] ").strip().lower()
        decision = "approve" if answer in APPROVE_ANSWERS else "reject"
        path = f"{base}/approvals/{ending.approval_id}"
        ending = await _stream(client, path, {"decision": decision}, out)
    return ending.code


async def smoke(
    url: str,
    token: str,
    question: str,
    conversation_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    out: TextIO = sys.stdout,
    ask: Ask = input,
) -> int:
    """Run :func:`chat` with a client on ``url`` that sends ``token`` as the bearer."""
    async with httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
        transport=transport,
    ) as client:
        return await chat(client, question, conversation_id, out, ask)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the smoke test; 2 without ``TOKEN`` or a question, 130 on Ctrl-C."""
    args = list(sys.argv[1:] if argv is None else argv)
    token = os.environ.get("TOKEN")
    if not token:
        print("Set the TOKEN environment variable first.", file=sys.stderr)
        return 2
    question = " ".join(args).strip()
    if not question:
        print('Usage: chat_smoke.py "your question"', file=sys.stderr)
        return 2
    url = os.environ.get("CHAT_URL", DEFAULT_CHAT_URL)
    conversation_id = os.environ.get("CONVERSATION_ID") or None
    try:
        return asyncio.run(smoke(url, token, question, conversation_id))
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
