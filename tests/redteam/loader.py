"""The red-team corpus (US-07 FR-022, research §11): versioned cases and legitimate messages.

``cases.jsonl`` holds one ``RedTeamCase`` per line: the attack (``input``, and ``tool_data`` the
compromised model's tool call returns), what the compromised model does (``compromise``) and the
``expect`` that every guarded run must meet. ``Expect``'s field names map one to one onto a US-09
verdict case. ``legit.jsonl`` holds ordinary customer messages that must not be blocked (SC-005).
"""

from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ecommerce_ai.agents.assistant.model import FinishReason
from tests.support.llm import Compromise

CORPUS_DIR: Final = Path(__file__).parent
CASES_FILE: Final = CORPUS_DIR / "cases.jsonl"
LEGIT_FILE: Final = CORPUS_DIR / "legit.jsonl"
CORPUS_VERSION: Final = "v1"
MIN_CASES: Final = 40
MIN_PER_CATEGORY: Final = 2
MIN_LEGIT: Final = 20
LEGIT_LANGUAGES: Final = frozenset({"en", "pt", "es", "fr", "de"})


class Category(StrEnum):
    """The FR-022 attack categories."""

    DIRECT_INJECTION = "direct_injection"
    INDIRECT_INJECTION = "indirect_injection"
    ROLE_PLAY = "role_play"
    SYSTEM_PROMPT_EXTRACTION = "system_prompt_extraction"
    OWNERSHIP_PROBING = "ownership_probing"
    APPROVAL_BYPASS = "approval_bypass"
    JAILBREAK = "jailbreak"
    OBFUSCATION_UNICODE = "obfuscation_unicode"
    MULTILINGUAL = "multilingual"
    OVER_LONG_INPUT = "over_long_input"
    TOOL_ARGUMENT_MANIPULATION = "tool_argument_manipulation"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Expect(_Frozen):
    """What every guarded run of a case must meet (a US-09 verdict case, field for field)."""

    must_not_call_tools: tuple[str, ...] = Field(
        default=(), description="Tools that must never run (read or approved)."
    )
    must_not_contain: tuple[str, ...] = Field(
        default=(), description="Text that must not reach the customer (the canary always)."
    )
    must_refuse: bool = Field(description="A guard must block or replace something.")
    expected_finish_reason: FinishReason | None = Field(
        default=None, description="How the run must end; ``None``: any sent reason."
    )


class RedTeamCase(_Frozen):
    """One attack of the corpus."""

    id: str = Field(pattern=r"^[a-z]+(-[a-z0-9]+)*-\d{2}$")
    version: str = CORPUS_VERSION
    category: Category
    input: str = Field(min_length=1, max_length=2000)
    tool_data: JsonValue = None
    compromise: Compromise = Field(default_factory=Compromise)
    expect: Expect


class LegitMessage(_Frozen):
    """An ordinary customer message that the guards must let through."""

    id: str
    language: str
    input: str = Field(min_length=1, max_length=2000)


@cache
def load_cases() -> tuple[RedTeamCase, ...]:
    """Every red-team case, in file order."""
    lines = CASES_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(RedTeamCase.model_validate_json(line) for line in lines if line.strip())


@cache
def load_legit() -> tuple[LegitMessage, ...]:
    """Every legitimate message, in file order."""
    lines = LEGIT_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(LegitMessage.model_validate_json(line) for line in lines if line.strip())
