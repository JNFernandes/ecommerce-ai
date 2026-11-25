"""Parse a Server-Sent Events body into events and comments, for the chat stream tests.

``parse_sse`` reads a whole body; ``read_sse`` reads an httpx streaming response until it ends
(or until ``stop`` says so, e.g. after the first ``token`` to simulate a disconnect).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched event: ``id:``, ``event:`` and the JSON ``data:``."""

    id: str | None
    event: str
    data: JsonValue


@dataclass(frozen=True, slots=True)
class SseComment:
    """A comment line (``: keep-alive``), without the leading colon and space."""

    text: str


type SseItem = SseEvent | SseComment


def _frame(lines: list[str]) -> list[SseItem]:
    items: list[SseItem] = []
    fields: dict[str, list[str]] = {}
    for line in lines:
        if line.startswith(":"):
            items.append(SseComment(line[1:].removeprefix(" ")))
            continue
        name, _, value = line.partition(":")
        fields.setdefault(name, []).append(value.removeprefix(" "))
    if fields:
        data = "\n".join(fields.get("data", []))
        items.append(
            SseEvent(
                id=fields["id"][-1] if "id" in fields else None,
                event=fields.get("event", ["message"])[-1],
                data=json.loads(data) if data else None,
            )
        )
    return items


def parse_sse(body: str) -> list[SseItem]:
    """Every event and comment of ``body``, in order; frames end at a blank line."""
    items: list[SseItem] = []
    lines: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line:
            lines.append(line)
        elif lines:
            items.extend(_frame(lines))
            lines = []
    if lines:
        items.extend(_frame(lines))
    return items


def events_of(items: list[SseItem]) -> list[SseEvent]:
    """Only the events of ``items``."""
    return [item for item in items if isinstance(item, SseEvent)]


async def read_sse(
    response: httpx.Response, stop: Callable[[SseEvent], bool] | None = None
) -> list[SseItem]:
    """Read ``response`` frame by frame; return early after the first event ``stop`` accepts."""
    items: list[SseItem] = []
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line:
            lines.append(line)
            continue
        if not lines:
            continue
        frame = _frame(lines)
        lines = []
        items.extend(frame)
        if stop is not None and any(isinstance(i, SseEvent) and stop(i) for i in frame):
            return items
    if lines:
        items.extend(_frame(lines))
    return items
