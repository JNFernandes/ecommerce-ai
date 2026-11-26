"""How a tool result reaches the model: a delimited block of inert JSON (FR-013; US-07 FR-005/006).

``<tool_result name="...">JSON</tool_result>``: the name and the payload are JSON-encoded with
``<``, ``>`` and ``&`` escaped, so nothing inside a result can close the block or open another
one. The payload is the tool envelope (``trust: untrusted_data``) or ``{error, message}``; it never
holds the caller's token.

``ToolResultSanitizer`` (the bundle's ``tool_result_guard``, research §5) runs first: every string
leaf goes through the input normaliser (a full-width less-than sign becomes ``<``, then escaped), a
leaf breaking an input rule is recorded as a tool ``sanitized`` event (the data stays inert data;
it is never blocked), and a payload whose encoded form is longer than ``max_chars`` has its
``data`` cut, ending ``…[truncated N chars]`` and listed in ``truncated``, with a ``truncated``
event.
"""

import json
from typing import Final

from pydantic import JsonValue

from ecommerce_ai.agents.assistant.model import ToolResultItem
from ecommerce_ai.agents.guardrails import NOOP_INPUT_GUARD, InputGuard, SanitizedResult
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)
from ecommerce_ai.agents.guardrails.normalizer import normalize
from ecommerce_ai.agents.prompts.loader import inert_json
from ecommerce_ai.agents.safety.rules import json_leaves

DEFAULT_TOOL_RESULT_MAX_CHARS: Final = 8000
"""Longest encoded tool result the model sees (``TOOL_RESULT_MAX_CHARS``)."""
TRUNCATED_RULE: Final = "truncated"
"""Rule of the event recorded when a result was cut."""
_MIN_CHECKED_LEAF: Final = 8
"""Shorter leaves (ids, statuses, amounts) cannot hold an instruction worth checking."""


def tool_result_text(item: ToolResultItem) -> str:
    """The delimited text of ``item`` sent to the model."""
    return f"<tool_result name={inert_json(item.name)}>{inert_json(item.payload)}</tool_result>"


def truncation_marker(removed: int) -> str:
    """What ends a cut result."""
    return f"…[truncated {removed} chars]"


def _normalized(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return normalize(value)
    if isinstance(value, list):
        return [_normalized(child) for child in value]
    if isinstance(value, dict):
        return {key: _normalized(child) for key, child in value.items()}
    return value


class ToolResultSanitizer:
    """Normalises, checks and caps every tool result before the model sees it."""

    def __init__(
        self,
        max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
        input_guard: InputGuard = NOOP_INPUT_GUARD,
    ) -> None:
        """Cap results at ``max_chars``; record leaves ``input_guard`` would block."""
        self._max_chars = max_chars
        self._input_guard = input_guard

    def sanitize(self, name: str, payload: dict[str, JsonValue]) -> SanitizedResult:
        """``payload`` of tool ``name`` normalised and capped, with an event per change."""
        clean = _normalized(payload)
        assert isinstance(clean, dict)  # noqa: S101  # a dict stays a dict
        capped, removed = self._cap(clean)
        events: dict[str, GuardEvent] = {}
        for leaf in dict.fromkeys(json_leaves(capped)):
            if len(leaf) < _MIN_CHECKED_LEAF:
                continue
            matched = self._input_guard.check(leaf)
            if matched is not None and matched.rule not in events:
                events[matched.rule] = GuardEvent(
                    stage=GuardStage.TOOL,
                    rule=matched.rule,
                    action=GuardAction.SANITIZED,
                    severity=matched.severity,
                )
        if removed:
            events[TRUNCATED_RULE] = GuardEvent(
                stage=GuardStage.TOOL,
                rule=TRUNCATED_RULE,
                action=GuardAction.SANITIZED,
                severity=GuardSeverity.LOW,
            )
        return SanitizedResult(payload=capped, events=tuple(events.values()))

    def _cap(self, payload: dict[str, JsonValue]) -> tuple[dict[str, JsonValue], int]:
        """``payload`` within ``max_chars`` encoded, and how many characters were cut."""
        if len(inert_json(payload)) <= self._max_chars:
            return payload, 0
        key = "data" if "data" in payload else "partial"
        rest = {k: v for k, v in payload.items() if k != "data"} if key == "data" else {}
        body = json.dumps(payload["data"] if key == "data" else payload, ensure_ascii=False)
        listed = rest.get("truncated")
        # The envelope lists its cut paths in ``truncated``; the cut field joins them.
        paths: list[JsonValue] = [*(listed if isinstance(listed, list) else []), key]
        keep = len(body)
        while True:
            removed = len(body) - keep
            candidate: dict[str, JsonValue] = rest | {
                key: body[:keep] + truncation_marker(removed),
                "truncated": paths,
            }
            over = len(inert_json(candidate)) - self._max_chars
            if over <= 0 or keep == 0:
                return candidate, removed
            keep = max(0, keep - over)
