"""Load a versioned prompt file and render its user part with untrusted evidence as inert data.

Evidence values are JSON-encoded and ``<``, ``>`` and ``&`` are written as ``\\u`` escapes, so no
evidence text can close an ``<evidence>`` block or open a ``<feedback>`` one (FR-014). The JSON
stays valid and decodes back to the original text.
"""

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib.resources import files
from string import Formatter
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ecommerce_ai.agents.triage.model import CaseForTriage, SuggestedAction

USER_PLACEHOLDERS: Final = ("cause", "default_action", "allowed_actions", "evidence", "feedback")
"""Placeholders the user template must contain, each exactly by name."""

_SECTIONS: Final = re.compile(r"\A# System\n(?P<system>.*?)^# User\n(?P<user>.*)\Z", re.S | re.M)
_PACKAGE: Final = "ecommerce_ai.agents.prompts"
_INERT: Final = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"}


def _inert_json(value: JsonValue) -> str:
    """JSON text of ``value`` in which no character can form a markup tag."""
    text = json.dumps(value, ensure_ascii=True)
    for char, escape in _INERT.items():
        text = text.replace(char, escape)
    return text


def _inert_text(value: str) -> str:
    """``value`` with the characters that could form a markup tag escaped."""
    return _inert_json(value)[1:-1]


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Prompt(BaseModel):
    """A loaded prompt: its version, system text and user template."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, description="Stored and published, e.g. `triage.v1`.")
    system: str = Field(min_length=1, description="System part: role and rules.")
    user_template: str = Field(min_length=1, description="User part with `{name}` placeholders.")

    def render_user(
        self,
        case: CaseForTriage,
        default_action: SuggestedAction,
        allowed: Iterable[SuggestedAction],
        feedback: Iterable[str],
    ) -> str:
        """Fill the user template: one ``<evidence>`` block per event, optional ``<feedback>``."""
        blocks = [
            "<evidence>\n"
            + _inert_json(
                {
                    "topic": item.topic,
                    "reason": item.reason,
                    "occurredAt": _iso_utc(item.occurred_at),
                    "payload": item.payload,
                }
            )
            + "\n</evidence>"
            for item in case.evidence
        ]
        lines = [f"- {_inert_text(message)}" for message in feedback]
        feedback_block = "\n<feedback>\n" + "\n".join(lines) + "\n</feedback>\n" if lines else ""
        return self.user_template.format_map(
            {
                "cause": case.cause.value,
                "default_action": default_action.value,
                "allowed_actions": ", ".join(sorted(action.value for action in allowed)),
                "evidence": "\n".join(blocks) if blocks else "(no stored events)",
                "feedback": feedback_block,
            }
        )


def parse_prompt(version: str, text: str) -> Prompt:
    """Split ``text`` into its sections; ``ValueError`` when a section or placeholder is wrong."""
    match = _SECTIONS.match(text.replace("\r\n", "\n"))
    if match is None:
        raise ValueError(f"prompt {version} needs a '# System' and a '# User' section")
    system, user = match["system"].strip(), match["user"].strip() + "\n"
    if not system:
        raise ValueError(f"prompt {version} has an empty system section")
    found = sorted({name for _, name, _, _ in Formatter().parse(user) if name is not None})
    if found != sorted(USER_PLACEHOLDERS):
        raise ValueError(f"prompt {version} placeholders must be exactly {USER_PLACEHOLDERS}")
    return Prompt(version=version, system=system, user_template=user)


def _read(name: str, version: str) -> str:
    return files(_PACKAGE).joinpath(f"{name}.{version}.md").read_text(encoding="utf-8")


def load_prompt(name: str, version: str) -> Prompt:
    """Load ``<name>.<version>.md`` packaged with this module (``FileNotFoundError`` if absent)."""
    return parse_prompt(f"{name}.{version}", _read(name, version))


class SystemPrompt(BaseModel):
    """A prompt with only a system part (the chat assistant: the transcript is the user part)."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, description="Reported as `promptVersion`, e.g. v1.")
    system: str = Field(min_length=1, description="System part: role and rules.")


def parse_system_prompt(version: str, text: str) -> SystemPrompt:
    """Parse a ``# System``-only prompt; ``ValueError`` when the section is missing or empty."""
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("# System\n") or re.search(r"^# User$", normalized, re.M):
        raise ValueError(f"prompt {version} needs exactly one '# System' section")
    system = normalized.removeprefix("# System\n").strip()
    if not system:
        raise ValueError(f"prompt {version} has an empty system section")
    return SystemPrompt(version=version, system=system)


def load_system_prompt(name: str, version: str) -> SystemPrompt:
    """Load the system-only ``<name>.<version>.md`` (``FileNotFoundError`` if absent)."""
    return parse_system_prompt(f"{name}.{version}", _read(name, version))
