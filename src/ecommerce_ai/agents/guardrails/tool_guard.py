"""The tool guard (US-07 FR-010..012, research §8): calls stay within what the customer can see.

``CallGuard.check`` runs in the chat graph's ``tools`` node before a call is proposed or run, in
this order:

1. ``not_allowed``: the tool is not on the conversation's allowlist.
2. ``duplicate_call`` / ``tool_loop``: the same tool with the same arguments (canonical JSON,
   ``call_key``) was already called in this message. The first repeat is refused; the second
   ends the answer (``ToolVerdict.ends_answer``).
3. ``unseen_argument``: an id-checked tool (``cancel_order``) names an id that neither the
   customer typed nor a tool returned in this conversation. A malformed id is left to the
   approval checks, which refuse it anyway.

A refused call never runs; the model gets the fixed ``refusal_payload`` of the rule, which never
echoes the call's arguments, so a refused id never becomes "seen".
"""

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import JsonValue

from ecommerce_ai.agents.guardrails import ToolContext, ToolVerdict
from ecommerce_ai.agents.guardrails.events import (
    GuardAction,
    GuardEvent,
    GuardSeverity,
    GuardStage,
)


class ToolRule(StrEnum):
    """Rules of the tool guard, as recorded in ``guardrail_events.rule``."""

    NOT_ALLOWED = "not_allowed"
    UNSEEN_ARGUMENT = "unseen_argument"
    DUPLICATE_CALL = "duplicate_call"
    TOOL_LOOP = "tool_loop"


_SEVERITY: Final = {
    ToolRule.NOT_ALLOWED: GuardSeverity.HIGH,
    ToolRule.UNSEEN_ARGUMENT: GuardSeverity.HIGH,
    ToolRule.DUPLICATE_CALL: GuardSeverity.LOW,
    ToolRule.TOOL_LOOP: GuardSeverity.MEDIUM,
}

_REFUSALS: Final[dict[ToolRule, str]] = {
    ToolRule.NOT_ALLOWED: "This tool is not available in this conversation.",
    ToolRule.UNSEEN_ARGUMENT: (
        "Refused: that id was not given by the customer or returned by a tool in this conversation."
    ),
    ToolRule.DUPLICATE_CALL: (
        "Refused: this exact call was already made for this message; use its earlier result."
    ),
    ToolRule.TOOL_LOOP: "Refused: the same call was repeated too often.",
}

TOOL_GUARD_SUMMARY: Final = "The call was refused by the assistant's safety rules."
"""The fixed ``tool.result`` summary of a refused call."""


def call_key(name: str, arguments: Mapping[str, JsonValue]) -> str:
    """The canonical key of a call: its name and arguments as sorted, compact JSON."""
    return json.dumps(
        {"name": name, "arguments": dict(arguments)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def refusal_payload(rule: str) -> dict[str, JsonValue]:
    """The fixed result the model gets for a call refused by ``rule``; no argument echoed."""
    known = ToolRule(rule) if rule in ToolRule else ToolRule.NOT_ALLOWED
    return {"error": known.value, "message": _REFUSALS[known]}


def _event(rule: ToolRule) -> GuardEvent:
    return GuardEvent(
        stage=GuardStage.TOOL, rule=rule.value, action=GuardAction.BLOCKED, severity=_SEVERITY[rule]
    )


def _canonical_id(value: JsonValue) -> str | None:
    """``value`` as a lower-case UUID string, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return None


class CallGuard:
    """The deterministic ``ToolGuard``: allowlist, seen ids, repeats."""

    def __init__(self, id_arguments: Mapping[str, str]) -> None:
        """Check, for each tool in ``id_arguments``, that its named argument is a seen id."""
        self._id_arguments = dict(id_arguments)

    def check(
        self, name: str, arguments: Mapping[str, JsonValue], context: ToolContext
    ) -> ToolVerdict | None:
        """The verdict refusing the call, ``None`` when it may run."""
        if context.allowlist is not None and name not in context.allowlist:
            return ToolVerdict(event=_event(ToolRule.NOT_ALLOWED))
        repeats = context.previous_calls.count(call_key(name, arguments))
        if repeats >= 2:
            return ToolVerdict(event=_event(ToolRule.TOOL_LOOP), ends_answer=True)
        if repeats == 1:
            return ToolVerdict(event=_event(ToolRule.DUPLICATE_CALL))
        argument = self._id_arguments.get(name)
        if argument is not None:
            target = _canonical_id(arguments.get(argument))
            if target is not None and target not in context.seen_ids:
                return ToolVerdict(event=_event(ToolRule.UNSEEN_ARGUMENT))
        return None
