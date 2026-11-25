"""Manual smoke test of the chat assistant against the running service.

Creates a conversation (or continues the one in ``CONVERSATION_ID``), posts the question given on
the command line and prints the streamed answer: each tool call and result on its own line, the
tokens as they arrive, then the finish reason or the error code and message. The customer access
token comes from ``TOKEN`` and is never printed; ``CHAT_URL`` overrides the default
``http://localhost:8002/chat``. The conversation id is printed first so a follow-up can reuse it.
The SSE stream is parsed with the standard library only (no SSE client dependency).
Usage: ``TOKEN=... uv run python scripts/chat_smoke.py "Where is my last order?"``.
"""

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import TextIO

import httpx

DEFAULT_CHAT_URL = "http://localhost:8002/chat"
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
        case _:
            return ""


async def chat(
    client: httpx.AsyncClient, question: str, conversation_id: str | None, out: TextIO
) -> int:
    """Ask ``question``; return 0 after ``message.end``, 1 on any error."""
    if conversation_id is None:
        created = await client.post("/conversations", json={})
        if created.status_code != 201:
            out.write(f"[http {created.status_code}] {created.text}\n")
            return 1
        conversation_id = str(created.json()["id"])
    out.write(f"conversation {conversation_id}\n")
    path = f"/conversations/{conversation_id}/messages"
    async with client.stream("POST", path, json={"content": question}) as response:
        if response.status_code != 200:
            await response.aread()
            out.write(f"[http {response.status_code}] {response.text}\n")
            return 1
        async for event in iter_events(response.aiter_lines()):
            out.write(render(event))
            out.flush()
            if event.name in {"message.end", "error"}:
                return 0 if event.name == "message.end" else 1
    out.write("\n[error] the stream ended without message.end\n")
    return 1


async def smoke(
    url: str,
    token: str,
    question: str,
    conversation_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Run :func:`chat` with a client on ``url`` that sends ``token`` as the bearer."""
    async with httpx.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
        transport=transport,
    ) as client:
        return await chat(client, question, conversation_id, out)


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
